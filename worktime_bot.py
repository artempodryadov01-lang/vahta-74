
import os
import sys
import logging
import asyncio
import calendar
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List

import pytz
import holidays
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean,
    DateTime, Date, Float, ForeignKey, UniqueConstraint, select,
    func, and_, or_, desc, update, delete
)
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship,
    sessionmaker, Session
)
from sqlalchemy.engine import make_url

from aiohttp import web

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin_secret_2024")
TIMEZONE = os.getenv("TIMEZONE", "Asia/Almaty")
HOLIDAY_COUNTRY = os.getenv("HOLIDAY_COUNTRY", "RU")
DATABASE_URL = os.getenv("DATABASE_URL", "")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
PORT = int(os.getenv("PORT", "8080"))

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Timezone
tz = pytz.timezone(TIMEZONE)

# Holiday calendar
try:
    holiday_calendar = holidays.country_holidays(HOLIDAY_COUNTRY)
except Exception:
    holiday_calendar = holidays.country_holidays("RU")
    logger.warning(f"Could not load holidays for {HOLIDAY_COUNTRY}, using RU")

# Database setup
def get_database_url():
    url = DATABASE_URL
    if not url:
        if os.getenv("RENDER"):
            logger.warning("No DATABASE_URL on Render. Using /tmp which is NOT persistent!")
            url = "sqlite+aiosqlite:///tmp/worktime_bot.db"
        else:
            os.makedirs("data", exist_ok=True)
            url = "sqlite+aiosqlite:///data/worktime_bot.db"
    # Fix postgres:// -> postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    # For async SQLite
    if url.startswith("sqlite://") and "aiosqlite" not in url:
        url = url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url

DB_URL = get_database_url()

# SQLAlchemy models
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    monthly_salary: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    hourly_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    norm_hours_per_month: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    auto_norm_hours: Mapped[bool] = mapped_column(Boolean, default=True)
    weekly_hours: Mapped[float] = mapped_column(Float, default=40.0)
    workdays_per_week: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(tz))

    shifts: Mapped[List["WorkShift"]] = relationship("WorkShift", back_populates="user")
    breaks: Mapped[List["WorkBreak"]] = relationship("WorkBreak", back_populates="user")
    payrolls: Mapped[List["Payroll"]] = relationship("Payroll", back_populates="user")

class WorkShift(Base):
    __tablename__ = "work_shifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    gross_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    break_hours: Mapped[Optional[float]] = mapped_column(Float, default=0.0)
    hours_worked: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(tz))

    user: Mapped["User"] = relationship("User", back_populates="shifts")
    breaks: Mapped[List["WorkBreak"]] = relationship("WorkBreak", back_populates="shift")

class WorkBreak(Base):
    __tablename__ = "work_breaks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    shift_id: Mapped[int] = mapped_column(Integer, ForeignKey("work_shifts.id"), nullable=False)
    break_type: Mapped[str] = mapped_column(String(50), default="other")
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_hours: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(tz))

    user: Mapped["User"] = relationship("User", back_populates="breaks")
    shift: Mapped["WorkShift"] = relationship("WorkShift", back_populates="breaks")

class Payroll(Base):
    __tablename__ = "payrolls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    hours_worked: Mapped[float] = mapped_column(Float, default=0.0)
    norm_hours: Mapped[float] = mapped_column(Float, default=0.0)
    overtime_hours: Mapped[float] = mapped_column(Float, default=0.0)
    undertime_hours: Mapped[float] = mapped_column(Float, default=0.0)
    hourly_rate: Mapped[float] = mapped_column(Float, default=0.0)
    base_amount: Mapped[float] = mapped_column(Float, default=0.0)
    advance_amount: Mapped[float] = mapped_column(Float, default=0.0)
    salary_amount: Mapped[float] = mapped_column(Float, default=0.0)
    advance_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    salary_date: Mapped[Optional[Date]] = mapped_column(Date, nullable=True)
    advance_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    salary_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(tz))

    user: Mapped["User"] = relationship("User", back_populates="payrolls")

    __table_args__ = (
        UniqueConstraint("user_id", "year", "month", name="uq_payroll_user_month"),
    )

# Async engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

# Helper functions
def get_now():
    return datetime.now(tz)

def calculate_month_norm_hours(year, month, hol_calendar, weekly_hours=40.0, workdays_per_week=5):
    """Calculate working hours for a given month."""
    if workdays_per_week <= 0:
        return 0.0

    hours_per_day = weekly_hours / workdays_per_week
    working_days = 0

    num_days = calendar.monthrange(year, month)[1]
    for day in range(1, num_days + 1):
        d = date(year, month, day)
        # weekday: 0=Monday, 6=Sunday
        if d.weekday() < 5:  # Monday-Friday
            if d not in hol_calendar:
                working_days += 1

    return round(working_days * hours_per_day, 2)

def get_effective_hourly_rate(user, norm_hours):
    """Calculate effective hourly rate."""
    if user.monthly_salary and norm_hours and norm_hours > 0:
        return round(user.monthly_salary / norm_hours, 2)
    if user.hourly_rate:
        return user.hourly_rate
    return 0.0

def get_user_norm_hours(user, year, month):
    """Get norm hours for user for a given month."""
    if not user.auto_norm_hours and user.norm_hours_per_month and user.norm_hours_per_month > 0:
        return user.norm_hours_per_month
    return calculate_month_norm_hours(
        year, month, holiday_calendar,
        user.weekly_hours, user.workdays_per_week
    )

def get_advance_date(year, month):
    """Get advance payment date: 30th of current month or last day."""
    num_days = calendar.monthrange(year, month)[1]
    target_day = min(30, num_days)
    d = date(year, month, target_day)
    # Move to previous working day if weekend/holiday
    while d.weekday() >= 5 or d in holiday_calendar:
        d -= timedelta(days=1)
    return d

def get_salary_date(year, month):
    """Get salary payment date: 15th of next month."""
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    d = date(next_year, next_month, 15)
    while d.weekday() >= 5 or d in holiday_calendar:
        d -= timedelta(days=1)
    return d

def format_hours(hours):
    """Format hours nicely."""
    if hours is None:
        return "0.00"
    return f"{hours:.2f}"

def format_money(amount):
    """Format money nicely."""
    if amount is None:
        return "0.00"
    return f"{amount:,.2f}".replace(",", " ")

# Bot and Dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Routers
main_router = Router()
admin_router = Router()
dp.include_router(main_router)
dp.include_router(admin_router)

# FSM States
class AdminStates(StatesGroup):
    waiting_salary = State()
    waiting_norm_hours = State()
    waiting_user_for_salary = State()
    waiting_user_for_norm = State()
    waiting_admin_password = State()

# Keyboards
def get_main_keyboard(is_admin=False):
    buttons = [
        [KeyboardButton(text="▶️ Начать смену"), KeyboardButton(text="🏁 Завершить смену")],
        [KeyboardButton(text="☕ Перерывы"), KeyboardButton(text="📊 Отчёт за сегодня")],
        [KeyboardButton(text="📈 Статистика за месяц"), KeyboardButton(text="💰 Зарплата")],
        [KeyboardButton(text="⚙️ Настройки")]
    ]
    if is_admin:
        buttons.append([KeyboardButton(text="👑 Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_break_keyboard():
    buttons = [
        [KeyboardButton(text="☕ Начать перерыв"), KeyboardButton(text="🔄 Завершить перерыв")],
        [KeyboardButton(text="📊 Информация о перерывах")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_admin_keyboard():
    buttons = [
        [KeyboardButton(text="👥 Сотрудники"), KeyboardButton(text="💰 Расчёт зарплат")],
        [KeyboardButton(text="✏️ Изменить оклад"), KeyboardButton(text="📅 Изменить норму часов")],
        [KeyboardButton(text="🔙 Назад")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_break_type_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🍽 Обед", callback_data="break_type:lunch")],
        [InlineKeyboardButton(text="☕ Кофе", callback_data="break_type:coffee")],
        [InlineKeyboardButton(text="🚬 Перекур", callback_data="break_type:smoke")],
        [InlineKeyboardButton(text="🔧 Технический", callback_data="break_type:technical")],
        [InlineKeyboardButton(text="📋 Другой", callback_data="break_type:other")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="break_type:cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_settings_keyboard(user):
    buttons = [
        [InlineKeyboardButton(text=f"🔄 Авто-норма: {'✅' if user.auto_norm_hours else '❌'}", callback_data="toggle_auto_norm")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="settings_back")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_employee_keyboard(employees):
    buttons = []
    for emp in employees:
        name = emp.full_name or emp.username or str(emp.telegram_id)
        buttons.append([InlineKeyboardButton(text=name, callback_data=f"admin_emp:{emp.telegram_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_employee_actions_keyboard(telegram_id):
    buttons = [
        [InlineKeyboardButton(text="💰 Изменить оклад", callback_data=f"admin_set_salary:{telegram_id}")],
        [InlineKeyboardButton(text="📅 Изменить норму", callback_data=f"admin_set_norm:{telegram_id}")],
        [InlineKeyboardButton(text="🔄 Авто/Ручная норма", callback_data=f"admin_toggle_norm:{telegram_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_employees")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_payroll_actions_keyboard(year, month, telegram_id):
    buttons = [
        [InlineKeyboardButton(text="✅ Аванс выплачен", callback_data=f"pay_advance:{telegram_id}:{year}:{month}")],
        [InlineKeyboardButton(text="✅ Зарплата выплачена", callback_data=f"pay_salary:{telegram_id}:{year}:{month}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_payroll_list")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Database helpers
async def get_user_by_telegram_id(telegram_id: int):
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

async def get_active_shift(telegram_id: int):
    async with async_session() as session:
        result = await session.execute(
            select(WorkShift).where(
                WorkShift.user_id == telegram_id,
                WorkShift.end_time == None
            )
        )
        return result.scalar_one_or_none()

async def get_active_break(telegram_id: int):
    async with async_session() as session:
        result = await session.execute(
            select(WorkBreak).where(
                WorkBreak.user_id == telegram_id,
                WorkBreak.is_active == True
            )
        )
        return result.scalar_one_or_none()

async def get_today_shifts(telegram_id: int):
    async with async_session() as session:
        now = get_now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        result = await session.execute(
            select(WorkShift).where(
                WorkShift.user_id == telegram_id,
                WorkShift.start_time >= today_start,
                WorkShift.start_time < today_end,
                WorkShift.end_time != None
            )
        )
        return result.scalars().all()

async def get_month_shifts(telegram_id: int, year: int, month: int):
    async with async_session() as session:
        month_start = datetime(year, month, 1, tzinfo=tz)
        if month == 12:
            month_end = datetime(year + 1, 1, 1, tzinfo=tz)
        else:
            month_end = datetime(year, month + 1, 1, tzinfo=tz)
        result = await session.execute(
            select(WorkShift).where(
                WorkShift.user_id == telegram_id,
                WorkShift.start_time >= month_start,
                WorkShift.start_time < month_end,
                WorkShift.end_time != None
            )
        )
        return result.scalars().all()

async def get_or_create_payroll(telegram_id: int, year: int, month: int):
    async with async_session() as session:
        result = await session.execute(
            select(Payroll).where(
                Payroll.user_id == telegram_id,
                Payroll.year == year,
                Payroll.month == month
            )
        )
        payroll = result.scalar_one_or_none()
        if payroll:
            return payroll

        user = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = user.scalar_one()

        norm_hours = get_user_norm_hours(user, year, month)
        shifts = await get_month_shifts(telegram_id, year, month)
        total_hours = sum(s.hours_worked or 0 for s in shifts)
        hourly_rate = get_effective_hourly_rate(user, norm_hours)
        base_amount = round(total_hours * hourly_rate, 2)
        advance_amount = round(base_amount * 0.4, 2)
        salary_amount = round(base_amount * 0.6, 2)
        overtime = max(0, total_hours - norm_hours)
        undertime = max(0, norm_hours - total_hours)

        payroll = Payroll(
            user_id=telegram_id,
            year=year,
            month=month,
            hours_worked=total_hours,
            norm_hours=norm_hours,
            overtime_hours=overtime,
            undertime_hours=undertime,
            hourly_rate=hourly_rate,
            base_amount=base_amount,
            advance_amount=advance_amount,
            salary_amount=salary_amount,
            advance_date=get_advance_date(year, month),
            salary_date=get_salary_date(year, month),
            calculated_at=get_now()
        )
        session.add(payroll)
        await session.commit()
        await session.refresh(payroll)
        return payroll

async def recalculate_payroll(telegram_id: int, year: int, month: int):
    async with async_session() as session:
        result = await session.execute(
            select(Payroll).where(
                Payroll.user_id == telegram_id,
                Payroll.year == year,
                Payroll.month == month
            )
        )
        payroll = result.scalar_one_or_none()

        user_result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one()

        norm_hours = get_user_norm_hours(user, year, month)
        shifts = await get_month_shifts(telegram_id, year, month)
        total_hours = sum(s.hours_worked or 0 for s in shifts)
        hourly_rate = get_effective_hourly_rate(user, norm_hours)
        base_amount = round(total_hours * hourly_rate, 2)
        advance_amount = round(base_amount * 0.4, 2)
        salary_amount = round(base_amount * 0.6, 2)
        overtime = max(0, total_hours - norm_hours)
        undertime = max(0, norm_hours - total_hours)

        if payroll:
            payroll.hours_worked = total_hours
            payroll.norm_hours = norm_hours
            payroll.overtime_hours = overtime
            payroll.undertime_hours = undertime
            payroll.hourly_rate = hourly_rate
            payroll.base_amount = base_amount
            payroll.advance_amount = advance_amount
            payroll.salary_amount = salary_amount
            payroll.advance_date = get_advance_date(year, month)
            payroll.salary_date = get_salary_date(year, month)
            payroll.calculated_at = get_now()
        else:
            payroll = Payroll(
                user_id=telegram_id,
                year=year,
                month=month,
                hours_worked=total_hours,
                norm_hours=norm_hours,
                overtime_hours=overtime,
                undertime_hours=undertime,
                hourly_rate=hourly_rate,
                base_amount=base_amount,
                advance_amount=advance_amount,
                salary_amount=salary_amount,
                advance_date=get_advance_date(year, month),
                salary_date=get_salary_date(year, month),
                calculated_at=get_now()
            )
            session.add(payroll)
        await session.commit()
        return payroll

# Handlers
@main_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    telegram_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = message.from_user.full_name or ""

    user = await get_user_by_telegram_id(telegram_id)
    if user:
        keyboard = get_main_keyboard(user.is_admin)
        await message.answer(
            f"👋 С возвращением, {full_name or username}!\n\n"
            f"Используйте меню для управления рабочим временем.",
            reply_markup=keyboard
        )
        return

    # Check if first user -> admin
    async with async_session() as session:
        count_result = await session.execute(select(func.count(User.id)))
        user_count = count_result.scalar()

    is_admin = user_count == 0

    async with async_session() as session:
        new_user = User(
            telegram_id=telegram_id,
            username=username,
            full_name=full_name,
            is_admin=is_admin,
            created_at=get_now()
        )
        session.add(new_user)
        await session.commit()

    keyboard = get_main_keyboard(is_admin)
    admin_text = "\n\n👑 Вы стали администратором системы!" if is_admin else ""
    await message.answer(
        f"✅ Регистрация успешна!\n\n"
        f"👤 {full_name or username}\n"
        f"🆔 ID: {telegram_id}{admin_text}\n\n"
        f"Используйте меню для управления рабочим временем.",
        reply_markup=keyboard
    )

@main_router.message(F.text == "▶️ Начать смену")
async def start_shift(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    active_shift = await get_active_shift(telegram_id)
    if active_shift:
        start_str = active_shift.start_time.strftime("%H:%M")
        await message.answer(
            f"⚠️ У вас уже есть активная смена!\n"
            f"Начало: {start_str}\n\n"
            f"Сначала завершите текущую смену."
        )
        return

    now = get_now()
    async with async_session() as session:
        shift = WorkShift(
            user_id=telegram_id,
            start_time=now,
            created_at=now
        )
        session.add(shift)
        await session.commit()

    time_str = now.strftime("%H:%M")
    await message.answer(
        f"✅ Смена начата!\n\n"
        f"🕐 Время начала: {time_str}\n\n"
        f"Удачной работы! 💪"
    )

@main_router.message(F.text == "🏁 Завершить смену")
async def end_shift(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    active_shift = await get_active_shift(telegram_id)
    if not active_shift:
        await message.answer("⚠️ У вас нет активной смены.")
        return

    now = get_now()

    # End active break if any
    active_break = await get_active_break(telegram_id)
    if active_break:
        async with async_session() as session:
            brk = await session.get(WorkBreak, active_break.id)
            brk.end_time = now
            brk.duration_hours = round((now - brk.start_time).total_seconds() / 3600, 4)
            brk.is_active = False
            await session.commit()

    # Calculate shift duration
    async with async_session() as session:
        shift = await session.get(WorkShift, active_shift.id)
        shift.end_time = now

        gross_seconds = (now - shift.start_time).total_seconds()
        gross_hours = round(gross_seconds / 3600, 4)

        # Calculate total break time
        breaks_result = await session.execute(
            select(WorkBreak).where(
                WorkBreak.shift_id == shift.id,
                WorkBreak.end_time != None
            )
        )
        breaks = breaks_result.scalars().all()
        total_break_seconds = sum(
            (b.end_time - b.start_time).total_seconds() for b in breaks
        )
        total_break_hours = round(total_break_seconds / 3600, 4)

        net_hours = max(0, round(gross_hours - total_break_hours, 2))

        shift.gross_hours = gross_hours
        shift.break_hours = total_break_hours
        shift.hours_worked = net_hours
        await session.commit()

    start_str = shift.start_time.strftime("%H:%M")
    end_str = now.strftime("%H:%M")

    await message.answer(
        f"🏁 Смена завершена!\n\n"
        f"🕐 Начало: {start_str}\n"
        f"🕐 Конец: {end_str}\n"
        f"⏱ Общее время: {format_hours(gross_hours)} ч\n"
        f"☕ Перерывы: {format_hours(total_break_hours)} ч\n"
        f"✅ Отработано: {format_hours(net_hours)} ч"
    )

@main_router.message(F.text == "☕ Перерывы")
async def show_break_menu(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    await message.answer(
        "☕ Меню перерывов\n\n"
        "Выберите действие:",
        reply_markup=get_break_keyboard()
    )

@main_router.message(F.text == "☕ Начать перерыв")
async def start_break(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    active_shift = await get_active_shift(telegram_id)
    if not active_shift:
        await message.answer("⚠️ Сначала начните смену.")
        return

    active_break = await get_active_break(telegram_id)
    if active_break:
        await message.answer("⚠️ У вас уже есть активный перерыв. Завершите его сначала.")
        return

    await message.answer(
        "Выберите тип перерыва:",
        reply_markup=get_break_type_keyboard()
    )

@main_router.callback_query(F.data.startswith("break_type:"))
async def process_break_type(callback: CallbackQuery):
    break_type = callback.data.split(":")[1]

    if break_type == "cancel":
        await callback.message.edit_text("❌ Перерыв отменён.")
        await callback.answer()
        return

    telegram_id = callback.from_user.id
    active_shift = await get_active_shift(telegram_id)
    if not active_shift:
        await callback.answer("⚠️ Нет активной смены", show_alert=True)
        return

    active_break = await get_active_break(telegram_id)
    if active_break:
        await callback.answer("⚠️ Уже есть активный перерыв", show_alert=True)
        return

    now = get_now()
    type_names = {
        "lunch": "🍽 Обед",
        "coffee": "☕ Кофе",
        "smoke": "🚬 Перекур",
        "technical": "🔧 Технический",
        "other": "📋 Другой"
    }
    type_name = type_names.get(break_type, break_type)

    async with async_session() as session:
        brk = WorkBreak(
            user_id=telegram_id,
            shift_id=active_shift.id,
            break_type=break_type,
            start_time=now,
            is_active=True,
            created_at=now
        )
        session.add(brk)
        await session.commit()

    time_str = now.strftime("%H:%M")
    await callback.message.edit_text(
        f"✅ Перерыв начат!\n\n"
        f"Тип: {type_name}\n"
        f"🕐 Время: {time_str}"
    )
    await callback.answer()

@main_router.message(F.text == "🔄 Завершить перерыв")
async def end_break(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    active_break = await get_active_break(telegram_id)
    if not active_break:
        await message.answer("⚠️ У вас нет активного перерыва.")
        return

    now = get_now()
    type_names = {
        "lunch": "🍽 Обед",
        "coffee": "☕ Кофе",
        "smoke": "🚬 Перекур",
        "technical": "🔧 Технический",
        "other": "📋 Другой"
    }

    async with async_session() as session:
        brk = await session.get(WorkBreak, active_break.id)
        brk.end_time = now
        brk.duration_hours = round((now - brk.start_time).total_seconds() / 3600, 4)
        brk.is_active = False
        await session.commit()

    type_name = type_names.get(brk.break_type, brk.break_type)
    start_str = brk.start_time.strftime("%H:%M")
    end_str = now.strftime("%H:%M")
    duration = format_hours(brk.duration_hours)

    await message.answer(
        f"✅ Перерыв завершён!\n\n"
        f"Тип: {type_name}\n"
        f"🕐 Начало: {start_str}\n"
        f"🕐 Конец: {end_str}\n"
        f"⏱ Длительность: {duration} ч"
    )

@main_router.message(F.text == "📊 Информация о перерывах")
async def break_info(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    active_shift = await get_active_shift(telegram_id)
    if not active_shift:
        await message.answer("⚠️ Нет активной смены. Перерывы отображаются во время смены.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(WorkBreak).where(
                WorkBreak.shift_id == active_shift.id
            ).order_by(WorkBreak.start_time)
        )
        breaks = result.scalars().all()

    if not breaks:
        await message.answer("📊 Перерывов за текущую смену пока нет.")
        return

    type_names = {
        "lunch": "🍽 Обед",
        "coffee": "☕ Кофе",
        "smoke": "🚬 Перекур",
        "technical": "🔧 Технический",
        "other": "📋 Другой"
    }

    text = "📊 Перерывы за текущую смену:\n\n"
    total = 0
    for brk in breaks:
        type_name = type_names.get(brk.break_type, brk.break_type)
        if brk.is_active:
            status = "🟢 активен"
            dur = ""
        else:
            status = "✅ завершён"
            dur = f" ({format_hours(brk.duration_hours)} ч)"
            total += brk.duration_hours or 0

        text += f"• {type_name} — {status}{dur}\n"

    text += f"\n⏱ Всего перерывов: {format_hours(total)} ч"
    await message.answer(text)

@main_router.message(F.text == "🔙 Назад")
async def go_back(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return
    keyboard = get_main_keyboard(user.is_admin)
    await message.answer("🏠 Главное меню", reply_markup=keyboard)

@main_router.message(F.text == "📊 Отчёт за сегодня")
async def today_report(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    now = get_now()
    today_shifts = await get_today_shifts(telegram_id)
    active_shift = await get_active_shift(telegram_id)

    total_hours = sum(s.hours_worked or 0 for s in today_shifts)
    total_breaks = sum(s.break_hours or 0 for s in today_shifts)
    total_gross = sum(s.gross_hours or 0 for s in today_shifts)

    text = f"📊 Отчёт за сегодня ({now.strftime('%d.%m.%Y')})\n\n"

    if active_shift:
        elapsed = (now - active_shift.start_time).total_seconds() / 3600
        text += f"🟢 Активная смена (с {active_shift.start_time.strftime('%H:%M')})\n"
        text += f"⏱ Прошло: {format_hours(elapsed)} ч\n\n"

    if today_shifts:
        text += f"✅ Завершённых смен: {len(today_shifts)}\n"
        text += f"⏱ Общее время: {format_hours(total_gross)} ч\n"
        text += f"☕ Перерывы: {format_hours(total_breaks)} ч\n"
        text += f"✅ Отработано: {format_hours(total_hours)} ч\n"
    elif not active_shift:
        text += "Сегодня смен пока нет."

    await message.answer(text)

@main_router.message(F.text == "📈 Статистика за месяц")
async def month_stats(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    now = get_now()
    year, month = now.year, now.month
    month_name = now.strftime("%B")

    shifts = await get_month_shifts(telegram_id, year, month)
    norm_hours = get_user_norm_hours(user, year, month)
    total_hours = sum(s.hours_worked or 0 for s in shifts)
    total_breaks = sum(s.break_hours or 0 for s in shifts)
    total_gross = sum(s.gross_hours or 0 for s in shifts)

    overtime = max(0, total_hours - norm_hours)
    undertime = max(0, norm_hours - total_hours)
    hourly_rate = get_effective_hourly_rate(user, norm_hours)
    base_amount = round(total_hours * hourly_rate, 2)

    month_names = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }
    month_label = month_names.get(month, month)

    text = (
        f"📈 Статистика за {month_label} {year}\n\n"
        f"📅 Рабочих дней: {len(shifts)}\n"
        f"⏱ Общее время: {format_hours(total_gross)} ч\n"
        f"☕ Перерывы: {format_hours(total_breaks)} ч\n"
        f"✅ Отработано: {format_hours(total_hours)} ч\n"
        f"📏 Норма часов: {format_hours(norm_hours)} ч\n\n"
    )

    if overtime > 0:
        text += f"📈 Переработка: +{format_hours(overtime)} ч\n"
    elif undertime > 0:
        text += f"📉 Недоработка: -{format_hours(undertime)} ч\n"
    else:
        text += "✅ Норма выполнена точно\n"

    text += (
        f"\n💰 Часовая ставка: {format_money(hourly_rate)} ₽/ч\n"
        f"💵 Начислено: {format_money(base_amount)} ₽"
    )

    await message.answer(text)

@main_router.message(F.text == "💰 Зарплата")
async def show_salary(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    now = get_now()
    year, month = now.year, now.month

    month_names = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }
    month_label = month_names.get(month, month)

    payroll = await get_or_create_payroll(telegram_id, year, month)

    text = (
        f"💰 Расчёт зарплаты за {month_label} {year}\n\n"
        f"💼 Оклад: {format_money(user.monthly_salary or 0)} ₽\n"
        f"📏 Норма часов: {format_hours(payroll.norm_hours)} ч\n"
        f"💵 Часовая ставка: {format_money(payroll.hourly_rate)} ₽/ч\n\n"
        f"✅ Отработано: {format_hours(payroll.hours_worked)} ч\n"
    )

    if payroll.overtime_hours > 0:
        text += f"📈 Переработка: +{format_hours(payroll.overtime_hours)} ч\n"
    if payroll.undertime_hours > 0:
        text += f"📉 Недоработка: -{format_hours(payroll.undertime_hours)} ч\n"

    text += (
        f"\n💵 Итого начислено: {format_money(payroll.base_amount)} ₽\n\n"
        f"🏦 Аванс (40%): {format_money(payroll.advance_amount)} ₽"
    )
    if payroll.advance_date:
        text += f"\n   📅 Дата: {payroll.advance_date.strftime('%d.%m.%Y')}"
    if payroll.advance_paid:
        text += "\n   ✅ Выплачен"

    text += f"\n\n🏦 Зарплата (60%): {format_money(payroll.salary_amount)} ₽"
    if payroll.salary_date:
        text += f"\n   📅 Дата: {payroll.salary_date.strftime('%d.%m.%Y')}"
    if payroll.salary_paid:
        text += "\n   ✅ Выплачена"

    await message.answer(text)

@main_router.message(F.text == "⚙️ Настройки")
async def show_settings(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user:
        await message.answer("❌ Сначала зарегистрируйтесь: /start")
        return

    now = get_now()
    norm_hours = get_user_norm_hours(user, now.year, now.month)
    hourly_rate = get_effective_hourly_rate(user, norm_hours)

    text = (
        f"⚙️ Ваши настройки\n\n"
        f"💼 Оклад: {format_money(user.monthly_salary or 0)} ₽\n"
        f"💵 Часовая ставка: {format_money(hourly_rate)} ₽/ч\n"
        f"📏 Норма часов (тек. мес.): {format_hours(norm_hours)} ч\n"
        f"🔄 Авто-расчёт нормы: {'✅ Да' if user.auto_norm_hours else '❌ Нет'}\n"
        f"📅 Рабочих дней в неделю: {user.workdays_per_week}\n"
        f"⏱ Часов в неделю: {user.weekly_hours}\n\n"
        f"Для изменения настроек обратитесь к администратору."
    )

    await message.answer(text, reply_markup=get_settings_keyboard(user))

@main_router.callback_query(F.data == "toggle_auto_norm")
async def toggle_auto_norm(callback: CallbackQuery):
    await callback.answer("⚠️ Только администратор может менять настройки нормы.", show_alert=True)

@main_router.callback_query(F.data == "settings_back")
async def settings_back(callback: CallbackQuery):
    try:
        await callback.message.edit_text("🏠 Возврат в главное меню")
    except TelegramBadRequest:
        pass
    await callback.answer()

# Admin handlers
@main_router.message(F.text == "👑 Админ-панель")
async def admin_panel(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён.")
        return

    await message.answer(
        "👑 Админ-панель\n\nВыберите действие:",
        reply_markup=get_admin_keyboard()
    )

@main_router.message(F.text == "👥 Сотрудники")
async def admin_employees(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён.")
        return

    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        employees = result.scalars().all()

    if not employees:
        await message.answer("📭 Список сотрудников пуст.")
        return

    text = f"👥 Сотрудники ({len(employees)}):\n\n"
    for emp in employees:
        name = emp.full_name or emp.username or str(emp.telegram_id)
        admin_badge = " 👑" if emp.is_admin else ""
        salary = format_money(emp.monthly_salary or 0)
        text += f"• {name}{admin_badge}\n  💼 Оклад: {salary} ₽\n  🆔 {emp.telegram_id}\n\n"

    await message.answer(text, reply_markup=get_admin_employee_keyboard(employees))

@admin_router.callback_query(F.data == "admin_employees")
async def admin_employees_cb(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        employees = result.scalars().all()

    if not employees:
        try:
            await callback.message.edit_text("📭 Список сотрудников пуст.")
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    text = f"👥 Сотрудники ({len(employees)}):\n\n"
    for emp in employees:
        name = emp.full_name or emp.username or str(emp.telegram_id)
        admin_badge = " 👑" if emp.is_admin else ""
        salary = format_money(emp.monthly_salary or 0)
        text += f"• {name}{admin_badge}\n  💼 Оклад: {salary} ₽\n  🆔 {emp.telegram_id}\n\n"

    try:
        await callback.message.edit_text(text, reply_markup=get_admin_employee_keyboard(employees))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=get_admin_employee_keyboard(employees))
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_emp:"))
async def admin_employee_detail(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    emp_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == emp_id))
        emp = result.scalar_one_or_none()

    if not emp:
        await callback.answer("❌ Сотрудник не найден", show_alert=True)
        return

    now = get_now()
    norm_hours = get_user_norm_hours(emp, now.year, now.month)
    hourly_rate = get_effective_hourly_rate(emp, norm_hours)

    text = (
        f"👤 {emp.full_name or emp.username or str(emp.telegram_id)}\n"
        f"🆔 {emp.telegram_id}\n"
        f"{'👑 Администратор' if emp.is_admin else '👤 Сотрудник'}\n\n"
        f"💼 Оклад: {format_money(emp.monthly_salary or 0)} ₽\n"
        f"💵 Часовая ставка: {format_money(hourly_rate)} ₽/ч\n"
        f"📏 Норма часов: {format_hours(norm_hours)} ч\n"
        f"🔄 Авто-норма: {'✅' if emp.auto_norm_hours else '❌'}\n"
        f"📅 Дней в неделю: {emp.workdays_per_week}\n"
        f"⏱ Часов в неделю: {emp.weekly_hours}\n"
    )

    try:
        await callback.message.edit_text(text, reply_markup=get_admin_employee_actions_keyboard(emp.telegram_id))
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=get_admin_employee_actions_keyboard(emp.telegram_id))
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_set_salary:"))
async def admin_set_salary_start(callback: CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    emp_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == emp_id))
        emp = result.scalar_one_or_none()

    if not emp:
        await callback.answer("❌ Сотрудник не найден", show_alert=True)
        return

    await state.update_data(target_user_id=emp_id)
    await state.set_state(AdminStates.waiting_salary)

    name = emp.full_name or emp.username or str(emp.telegram_id)
    await callback.message.edit_text(
        f"💰 Введите новый оклад для {name}\n"
        f"Текущий оклад: {format_money(emp.monthly_salary or 0)} ₽\n\n"
        f"Введите число (например: 200000):"
    )
    await callback.answer()

@admin_router.message(AdminStates.waiting_salary)
async def admin_set_salary_process(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён.")
        await state.clear()
        return

    data = await state.get_data()
    target_id = data.get("target_user_id")

    try:
        salary = float(message.text.strip().replace(" ", "").replace(",", "."))
        if salary < 0:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ Введите корректное число. Попробуйте ещё раз:")
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == target_id))
        emp = result.scalar_one_or_none()
        if emp:
            emp.monthly_salary = salary
            await session.commit()

    name = emp.full_name or emp.username or str(emp.telegram_id) if emp else str(target_id)
    await state.clear()
    await message.answer(
        f"✅ Оклад для {name} обновлён!\n"
        f"💰 Новый оклад: {format_money(salary)} ₽",
        reply_markup=get_admin_keyboard()
    )

@admin_router.callback_query(F.data.startswith("admin_set_norm:"))
async def admin_set_norm_start(callback: CallbackQuery, state: FSMContext):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    emp_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == emp_id))
        emp = result.scalar_one_or_none()

    if not emp:
        await callback.answer("❌ Сотрудник не найден", show_alert=True)
        return

    await state.update_data(target_user_id=emp_id)
    await state.set_state(AdminStates.waiting_norm_hours)

    name = emp.full_name or emp.username or str(emp.telegram_id)
    await callback.message.edit_text(
        f"📅 Введите новую норму часов в месяц для {name}\n"
        f"Текущая норма: {format_hours(emp.norm_hours_per_month or 0)} ч\n\n"
        f"Введите число (например: 168):"
    )
    await callback.answer()

@admin_router.message(AdminStates.waiting_norm_hours)
async def admin_set_norm_process(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён.")
        await state.clear()
        return

    data = await state.get_data()
    target_id = data.get("target_user_id")

    try:
        norm = float(message.text.strip().replace(" ", "").replace(",", "."))
        if norm < 0:
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("❌ Введите корректное число. Попробуйте ещё раз:")
        return

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == target_id))
        emp = result.scalar_one_or_none()
        if emp:
            emp.norm_hours_per_month = norm
            emp.auto_norm_hours = False
            await session.commit()

    name = emp.full_name or emp.username or str(emp.telegram_id) if emp else str(target_id)
    await state.clear()
    await message.answer(
        f"✅ Норма часов для {name} обновлена!\n"
        f"📅 Новая норма: {format_hours(norm)} ч\n"
        f"🔄 Авто-расчёт отключён.",
        reply_markup=get_admin_keyboard()
    )

@admin_router.callback_query(F.data.startswith("admin_toggle_norm:"))
async def admin_toggle_norm(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    emp_id = int(callback.data.split(":")[1])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == emp_id))
        emp = result.scalar_one_or_none()
        if emp:
            emp.auto_norm_hours = not emp.auto_norm_hours
            await session.commit()
            status = "✅ Включён" if emp.auto_norm_hours else "❌ Выключен"
            name = emp.full_name or emp.username or str(emp.telegram_id)
            await callback.answer(f"Авто-норма для {name}: {status}", show_alert=True)
        else:
            await callback.answer("❌ Сотрудник не найден", show_alert=True)

@admin_router.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    try:
        await callback.message.edit_text("🏠 Главное меню")
    except TelegramBadRequest:
        pass
    await callback.answer()

@main_router.message(F.text == "💰 Расчёт зарплат")
async def admin_payroll_list(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён.")
        return

    now = get_now()
    year, month = now.year, now.month

    month_names = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }
    month_label = month_names.get(month, month)

    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        employees = result.scalars().all()

    text = f"💰 Расчёт зарплат — {month_label} {year}\n\n"

    for emp in employees:
        payroll = await get_or_create_payroll(emp.telegram_id, year, month)
        name = emp.full_name or emp.username or str(emp.telegram_id)
        adv_status = "✅" if payroll.advance_paid else "⬜"
        sal_status = "✅" if payroll.salary_paid else "⬜"

        text += (
            f"👤 {name}\n"
            f"  ⏱ {format_hours(payroll.hours_worked)}/{format_hours(payroll.norm_hours)} ч\n"
            f"  💵 {format_money(payroll.base_amount)} ₽\n"
            f"  Аванс: {adv_status} | ЗП: {sal_status}\n\n"
        )

    await message.answer(text)

@admin_router.callback_query(F.data == "admin_payroll_list")
async def admin_payroll_list_cb(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    now = get_now()
    year, month = now.year, now.month

    month_names = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }
    month_label = month_names.get(month, month)

    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        employees = result.scalars().all()

    text = f"💰 Расчёт зарплат — {month_label} {year}\n\n"

    for emp in employees:
        payroll = await get_or_create_payroll(emp.telegram_id, year, month)
        name = emp.full_name or emp.username or str(emp.telegram_id)
        adv_status = "✅" if payroll.advance_paid else "⬜"
        sal_status = "✅" if payroll.salary_paid else "⬜"

        text += (
            f"👤 {name}\n"
            f"  ⏱ {format_hours(payroll.hours_worked)}/{format_hours(payroll.norm_hours)} ч\n"
            f"  💵 {format_money(payroll.base_amount)} ₽\n"
            f"  Аванс: {adv_status} | ЗП: {sal_status}\n\n"
        )

    try:
        await callback.message.edit_text(text)
    except TelegramBadRequest:
        await callback.message.answer(text)
    await callback.answer()

@main_router.message(F.text == "✏️ Изменить оклад")
async def admin_change_salary_prompt(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён.")
        return

    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        employees = result.scalars().all()

    await message.answer(
        "✏️ Выберите сотрудника для изменения оклада:",
        reply_markup=get_admin_employee_keyboard(employees)
    )

@main_router.message(F.text == "📅 Изменить норму часов")
async def admin_change_norm_prompt(message: Message):
    telegram_id = message.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await message.answer("⛔ Доступ запрещён.")
        return

    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        employees = result.scalars().all()

    await message.answer(
        "📅 Выберите сотрудника для изменения нормы часов:",
        reply_markup=get_admin_employee_keyboard(employees)
    )

@admin_router.callback_query(F.data.startswith("pay_advance:"))
async def mark_advance_paid(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split(":")
    emp_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])

    async with async_session() as session:
        result = await session.execute(
            select(Payroll).where(
                Payroll.user_id == emp_id,
                Payroll.year == year,
                Payroll.month == month
            )
        )
        payroll = result.scalar_one_or_none()
        if payroll:
            payroll.advance_paid = True
            await session.commit()
            await callback.answer("✅ Аванс отмечен как выплаченный", show_alert=True)
        else:
            await callback.answer("❌ Запись не найдена", show_alert=True)

@admin_router.callback_query(F.data.startswith("pay_salary:"))
async def mark_salary_paid(callback: CallbackQuery):
    telegram_id = callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    parts = callback.data.split(":")
    emp_id = int(parts[1])
    year = int(parts[2])
    month = int(parts[3])

    async with async_session() as session:
        result = await session.execute(
            select(Payroll).where(
                Payroll.user_id == emp_id,
                Payroll.year == year,
                Payroll.month == month
            )
        )
        payroll = result.scalar_one_or_none()
        if payroll:
            payroll.salary_paid = True
            await session.commit()
            await callback.answer("✅ Зарплата отмечена как выплаченная", show_alert=True)
        else:
            await callback.answer("❌ Запись не найдена", show_alert=True)

# Scheduled tasks
async def recalculate_all_payrolls():
    """Recalculate all payrolls for current month."""
    logger.info("Starting scheduled payroll recalculation")
    now = get_now()
    year, month = now.year, now.month

    async with async_session() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()

    for user in users:
        try:
            await recalculate_payroll(user.telegram_id, year, month)
        except Exception as e:
            logger.error(f"Error recalculating payroll for {user.telegram_id}: {e}")

    logger.info("Payroll recalculation complete")

# Web server for health check and webhook
async def health_handler(request):
    return web.json_response({"status": "ok"})

async def create_web_app():
    app = web.Application()
    app.router.add_get("/health", health_handler)

    if RENDER_EXTERNAL_URL:
        webhook_path = f"/webhook/{BOT_TOKEN}"
        async def webhook_handler(request):
            if request.headers.get("content-type") == "application/json":
                data = await request.json()
                update_obj = await bot.session._prepare_value(
                    __import__("aiogram").types.Update, data
                )
                await dp.feed_update(bot, update_obj)
                return web.json_response({"ok": True})
            return web.json_response({"ok": False}, status=400)

        app.router.add_post(webhook_path, webhook_handler)

    return app

# Main startup
async def on_startup():
    await init_db()
    logger.info("Database initialized")

    # Setup scheduler
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        recalculate_all_payrolls,
        CronTrigger(hour=0, minute=0),
        id="recalculate_payrolls",
        replace_existing=True
    )
    scheduler.start()
    logger.info("Scheduler started")

    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook/{BOT_TOKEN}"
        await bot.set_webhook(webhook_url)
        logger.info(f"Webhook set to {webhook_url}")
    else:
        # Delete webhook if switching to polling
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook deleted, using polling")
        except Exception as e:
            logger.warning(f"Could not delete webhook: {e}")

async def on_shutdown():
    await bot.session.close()
    logger.info("Bot session closed")

async def main():
    await on_startup()

    if RENDER_EXTERNAL_URL:
        # Webhook mode
        web_app = await create_web_app()
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"Web server started on port {PORT}")

        # Keep running
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            await on_shutdown()
    else:
        # Polling mode with health server
        web_app = await create_web_app()
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info(f"Health server started on port {PORT}")

        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        except (KeyboardInterrupt, SystemExit):
            await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")