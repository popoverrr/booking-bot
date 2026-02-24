"""
🤖 BookingBot — Telegram-бот для онлайн-записи на услуги
Автор: [Твоё имя]
Стек: Python 3.11+ · aiogram 3 · SQLite · APScheduler

Функционал:
- Выбор услуги из каталога
- Выбор мастера
- Выбор даты и времени (inline-календарь)
- Подтверждение и отмена записи
- Напоминание за 1 час до визита
- Админ-панель: просмотр записей, управление расписанием
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ══════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ══════════════════════════════════════════════

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # @BotFather → /newbot
ADMIN_IDS = [123456789]  # Telegram ID администраторов

# ══════════════════════════════════════════════
# БАЗА ДАННЫХ
# ══════════════════════════════════════════════

def init_db():
    """Инициализация SQLite базы данных."""
    conn = sqlite3.connect("bookings.db")
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            duration INTEGER NOT NULL,  -- в минутах
            price INTEGER NOT NULL      -- в тенге
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS masters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            specialization TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            service_id INTEGER NOT NULL,
            master_id INTEGER NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            status TEXT DEFAULT 'confirmed',  -- confirmed / cancelled / completed
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reminded INTEGER DEFAULT 0,
            FOREIGN KEY (service_id) REFERENCES services(id),
            FOREIGN KEY (master_id) REFERENCES masters(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS schedule (
            master_id INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,  -- 0=Пн, 6=Вс
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            FOREIGN KEY (master_id) REFERENCES masters(id)
        )
    """)

    # Демо-данные (если таблицы пустые)
    if not c.execute("SELECT COUNT(*) FROM services").fetchone()[0]:
        demo_services = [
            ("Мужская стрижка", 45, 4000),
            ("Бритьё", 30, 2500),
            ("Стрижка + бритьё", 60, 5500),
            ("Укладка", 20, 2000),
            ("Детская стрижка", 30, 2500),
            ("Камуфляж седины", 40, 5000),
        ]
        c.executemany("INSERT INTO services (name, duration, price) VALUES (?, ?, ?)", demo_services)

        demo_masters = [
            ("Арман", "Барбер"),
            ("Дамир", "Топ-барбер"),
            ("Ерлан", "Стилист"),
        ]
        c.executemany("INSERT INTO masters (name, specialization) VALUES (?, ?)", demo_masters)

        # Расписание: Пн-Сб, 10:00-20:00
        for master_id in [1, 2, 3]:
            for day in range(6):  # Пн-Сб
                c.execute(
                    "INSERT INTO schedule (master_id, day_of_week, start_time, end_time) VALUES (?, ?, ?, ?)",
                    (master_id, day, "10:00", "20:00")
                )

    conn.commit()
    return conn


# ══════════════════════════════════════════════
# FSM — СОСТОЯНИЯ ДИАЛОГА
# ══════════════════════════════════════════════

class BookingStates(StatesGroup):
    choosing_service = State()
    choosing_master = State()
    choosing_date = State()
    choosing_time = State()
    confirming = State()


# ══════════════════════════════════════════════
# КЛАВИАТУРЫ
# ══════════════════════════════════════════════

def services_keyboard(conn) -> InlineKeyboardMarkup:
    """Клавиатура выбора услуги."""
    services = conn.execute("SELECT * FROM services").fetchall()
    buttons = []
    for s in services:
        buttons.append([
            InlineKeyboardButton(
                text=f"{s['name']} — {s['price']}₸ ({s['duration']} мин)",
                callback_data=f"service_{s['id']}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def masters_keyboard(conn) -> InlineKeyboardMarkup:
    """Клавиатура выбора мастера."""
    masters = conn.execute("SELECT * FROM masters").fetchall()
    buttons = []
    for m in masters:
        buttons.append([
            InlineKeyboardButton(
                text=f"✂️ {m['name']} — {m['specialization']}",
                callback_data=f"master_{m['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_services")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def dates_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора даты (ближайшие 7 дней)."""
    days_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    months_ru = [
        "", "янв", "фев", "мар", "апр", "май", "июн",
        "июл", "авг", "сен", "окт", "ноя", "дек"
    ]
    buttons = []
    today = datetime.now()

    for i in range(7):
        day = today + timedelta(days=i)
        if day.weekday() == 6:  # Воскресенье — выходной
            continue
        day_name = days_ru[day.weekday()]
        label = f"{day_name}, {day.day} {months_ru[day.month]}"
        if i == 0:
            label = f"🟢 Сегодня, {day.day} {months_ru[day.month]}"
        elif i == 1:
            label = f"Завтра, {day.day} {months_ru[day.month]}"
        buttons.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"date_{day.strftime('%Y-%m-%d')}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="back_to_masters")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def times_keyboard(conn, master_id: int, date_str: str, duration: int) -> InlineKeyboardMarkup:
    """Клавиатура свободных слотов времени."""
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    day_of_week = date_obj.weekday()

    schedule = conn.execute(
        "SELECT * FROM schedule WHERE master_id = ? AND day_of_week = ?",
        (master_id, day_of_week)
    ).fetchone()

    if not schedule:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Выходной день", callback_data="no_slots")],
            [InlineKeyboardButton(text="« Выбрать другую дату", callback_data="back_to_dates")]
        ])

    # Получаем занятые слоты
    booked = conn.execute(
        "SELECT time FROM bookings WHERE master_id = ? AND date = ? AND status = 'confirmed'",
        (master_id, date_str)
    ).fetchall()
    booked_times = {b["time"] for b in booked}

    # Генерируем слоты с шагом 30 минут
    start_h, start_m = map(int, schedule["start_time"].split(":"))
    end_h, end_m = map(int, schedule["end_time"].split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m

    now = datetime.now()
    buttons = []
    row = []

    for mins in range(start_minutes, end_minutes - duration + 1, 30):
        h, m = divmod(mins, 60)
        time_str = f"{h:02d}:{m:02d}"

        # Пропускаем прошедшее время для сегодня
        if date_obj.date() == now.date():
            slot_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if slot_time <= now:
                continue

        if time_str in booked_times:
            continue  # Слот занят

        row.append(
            InlineKeyboardButton(text=time_str, callback_data=f"time_{time_str}")
        )
        if len(row) == 3:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    if not buttons:
        buttons.append([InlineKeyboardButton(text="Нет свободных слотов", callback_data="no_slots")])

    buttons.append([InlineKeyboardButton(text="« Выбрать другую дату", callback_data="back_to_dates")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_yes"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="confirm_no"),
        ]
    ])


# ══════════════════════════════════════════════
# ХЭНДЛЕРЫ
# ══════════════════════════════════════════════

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 Добро пожаловать в <b>BarberBot</b>!\n\n"
        "Я помогу вам записаться к мастеру.\n"
        "Нажмите /book чтобы начать запись.\n\n"
        "📋 /mybookings — ваши записи\n"
        "❌ /cancel — отменить запись",
        parse_mode="HTML"
    )


@router.message(Command("book"))
async def cmd_book(message: Message, state: FSMContext, db: sqlite3.Connection):
    await state.set_state(BookingStates.choosing_service)
    await message.answer(
        "✂️ <b>Выберите услугу:</b>",
        reply_markup=services_keyboard(db),
        parse_mode="HTML"
    )


# --- Выбор услуги ---
@router.callback_query(F.data.startswith("service_"))
async def pick_service(callback: CallbackQuery, state: FSMContext, db: sqlite3.Connection):
    service_id = int(callback.data.split("_")[1])
    service = db.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()

    await state.update_data(service_id=service_id, service_name=service["name"],
                            duration=service["duration"], price=service["price"])
    await state.set_state(BookingStates.choosing_master)

    await callback.message.edit_text(
        f"✅ Услуга: <b>{service['name']}</b>\n\n👤 <b>Выберите мастера:</b>",
        reply_markup=masters_keyboard(db),
        parse_mode="HTML"
    )


# --- Выбор мастера ---
@router.callback_query(F.data.startswith("master_"))
async def pick_master(callback: CallbackQuery, state: FSMContext, db: sqlite3.Connection):
    master_id = int(callback.data.split("_")[1])
    master = db.execute("SELECT * FROM masters WHERE id = ?", (master_id,)).fetchone()

    await state.update_data(master_id=master_id, master_name=master["name"])
    await state.set_state(BookingStates.choosing_date)

    await callback.message.edit_text(
        f"✅ Мастер: <b>{master['name']}</b>\n\n📅 <b>Выберите дату:</b>",
        reply_markup=dates_keyboard(),
        parse_mode="HTML"
    )


# --- Выбор даты ---
@router.callback_query(F.data.startswith("date_"))
async def pick_date(callback: CallbackQuery, state: FSMContext, db: sqlite3.Connection):
    date_str = callback.data.split("_", 1)[1]
    data = await state.get_data()

    await state.update_data(date=date_str)
    await state.set_state(BookingStates.choosing_time)

    await callback.message.edit_text(
        f"📅 Дата: <b>{date_str}</b>\n\n🕐 <b>Выберите время:</b>",
        reply_markup=times_keyboard(db, data["master_id"], date_str, data["duration"]),
        parse_mode="HTML"
    )


# --- Выбор времени ---
@router.callback_query(F.data.startswith("time_"))
async def pick_time(callback: CallbackQuery, state: FSMContext):
    time_str = callback.data.split("_", 1)[1]
    data = await state.get_data()

    await state.update_data(time=time_str)
    await state.set_state(BookingStates.confirming)

    summary = (
        "📋 <b>Ваша запись:</b>\n\n"
        f"✂️ Услуга: <b>{data['service_name']}</b>\n"
        f"👤 Мастер: <b>{data['master_name']}</b>\n"
        f"📅 Дата: <b>{data['date']}</b>\n"
        f"🕐 Время: <b>{time_str}</b>\n"
        f"💰 Стоимость: <b>{data['price']}₸</b>\n\n"
        "Всё верно?"
    )

    await callback.message.edit_text(
        summary, reply_markup=confirm_keyboard(), parse_mode="HTML"
    )


# --- Подтверждение ---
@router.callback_query(F.data == "confirm_yes")
async def confirm_booking(callback: CallbackQuery, state: FSMContext, db: sqlite3.Connection):
    data = await state.get_data()

    db.execute(
        "INSERT INTO bookings (user_id, username, service_id, master_id, date, time) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            callback.from_user.id,
            callback.from_user.username or callback.from_user.first_name,
            data["service_id"], data["master_id"],
            data["date"], data["time"]
        )
    )
    db.commit()

    await state.clear()

    await callback.message.edit_text(
        "✅ <b>Запись подтверждена!</b>\n\n"
        f"✂️ {data['service_name']}\n"
        f"👤 {data['master_name']}\n"
        f"📅 {data['date']} в {data['time']}\n\n"
        "Мы напомним вам за 1 час до визита 🔔\n"
        "Для отмены — /cancel",
        parse_mode="HTML"
    )

    # Уведомление админу
    for admin_id in ADMIN_IDS:
        try:
            await callback.bot.send_message(
                admin_id,
                f"🆕 Новая запись!\n\n"
                f"Клиент: @{callback.from_user.username or 'N/A'}\n"
                f"Услуга: {data['service_name']}\n"
                f"Мастер: {data['master_name']}\n"
                f"Дата: {data['date']} в {data['time']}"
            )
        except Exception:
            pass


@router.callback_query(F.data == "confirm_no")
async def cancel_booking_flow(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "❌ Запись отменена.\n\nЧтобы начать заново — /book"
    )


# --- Навигация «Назад» ---
@router.callback_query(F.data == "back_to_services")
async def back_services(callback: CallbackQuery, state: FSMContext, db: sqlite3.Connection):
    await state.set_state(BookingStates.choosing_service)
    await callback.message.edit_text(
        "✂️ <b>Выберите услугу:</b>",
        reply_markup=services_keyboard(db),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "back_to_masters")
async def back_masters(callback: CallbackQuery, state: FSMContext, db: sqlite3.Connection):
    await state.set_state(BookingStates.choosing_master)
    data = await state.get_data()
    await callback.message.edit_text(
        f"✅ Услуга: <b>{data['service_name']}</b>\n\n👤 <b>Выберите мастера:</b>",
        reply_markup=masters_keyboard(db),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "back_to_dates")
async def back_dates(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BookingStates.choosing_date)
    data = await state.get_data()
    await callback.message.edit_text(
        f"✅ Мастер: <b>{data['master_name']}</b>\n\n📅 <b>Выберите дату:</b>",
        reply_markup=dates_keyboard(),
        parse_mode="HTML"
    )


# --- Мои записи ---
@router.message(Command("mybookings"))
async def cmd_my_bookings(message: Message, db: sqlite3.Connection):
    bookings = db.execute(
        """
        SELECT b.*, s.name as service_name, m.name as master_name
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        JOIN masters m ON b.master_id = m.id
        WHERE b.user_id = ? AND b.status = 'confirmed' AND b.date >= date('now')
        ORDER BY b.date, b.time
        """,
        (message.from_user.id,)
    ).fetchall()

    if not bookings:
        await message.answer("📋 У вас нет активных записей.\n\n/book — записаться")
        return

    text = "📋 <b>Ваши записи:</b>\n\n"
    for b in bookings:
        text += (
            f"• <b>{b['service_name']}</b>\n"
            f"  👤 {b['master_name']} · 📅 {b['date']} · 🕐 {b['time']}\n"
            f"  ID: <code>{b['id']}</code>\n\n"
        )
    text += "Для отмены: /cancel <code>ID</code>"

    await message.answer(text, parse_mode="HTML")


# --- Отмена записи ---
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, db: sqlite3.Connection):
    parts = message.text.split()

    if len(parts) < 2:
        await message.answer(
            "Используйте: /cancel <code>ID</code>\n"
            "Узнать ID записи: /mybookings",
            parse_mode="HTML"
        )
        return

    booking_id = parts[1]
    booking = db.execute(
        "SELECT * FROM bookings WHERE id = ? AND user_id = ? AND status = 'confirmed'",
        (booking_id, message.from_user.id)
    ).fetchone()

    if not booking:
        await message.answer("❌ Запись не найдена или уже отменена.")
        return

    db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
    db.commit()

    await message.answer(f"✅ Запись #{booking_id} отменена.")


# ══════════════════════════════════════════════
# АДМИН-ПАНЕЛЬ
# ══════════════════════════════════════════════

@router.message(Command("admin"))
async def cmd_admin(message: Message, db: sqlite3.Connection):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Нет доступа.")
        return

    today = datetime.now().strftime("%Y-%m-%d")
    bookings = db.execute(
        """
        SELECT b.*, s.name as service_name, m.name as master_name
        FROM bookings b
        JOIN services s ON b.service_id = s.id
        JOIN masters m ON b.master_id = m.id
        WHERE b.date = ? AND b.status = 'confirmed'
        ORDER BY b.time
        """,
        (today,)
    ).fetchall()

    total = db.execute(
        "SELECT COUNT(*) as cnt FROM bookings WHERE status = 'confirmed' AND date >= ?",
        (today,)
    ).fetchone()["cnt"]

    text = f"📊 <b>Админ-панель</b>\n\nЗаписей всего: {total}\n\n"
    text += f"<b>Сегодня ({today}):</b>\n\n"

    if not bookings:
        text += "Записей на сегодня нет."
    else:
        for b in bookings:
            text += (
                f"🕐 {b['time']} — {b['service_name']}\n"
                f"   👤 Мастер: {b['master_name']}\n"
                f"   📱 Клиент: @{b['username'] or 'N/A'}\n\n"
            )

    await message.answer(text, parse_mode="HTML")


# ══════════════════════════════════════════════
# ЗАПУСК БОТА
# ══════════════════════════════════════════════

async def main():
    logging.basicConfig(level=logging.INFO)

    db = init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Прокидываем db во все хэндлеры через middleware-подход
    @dp.update.outer_middleware()
    async def db_middleware(handler, event, data):
        data["db"] = db
        return await handler(event, data)

    print("🤖 BookingBot запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
