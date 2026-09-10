import os
import sys
import logging
import asyncio
import calendar
from datetime import datetime, date, timedelta
from typing import Optional, List

import pytz
import holidays
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest

from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Boolean,
    DateTime, Date, Float, ForeignKey, UniqueConstraint, select, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
RESET_DB = os.getenv("RESET_DB", "False")
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

# ==========================================
# ЖЕЛЕЗОБЕТОННАЯ ЛОГИКА ПОДКЛЮЧЕНИЯ К БД
# ==========================================
def get_database_url():
    url = os.getenv("DATABASE_URL", "").strip()
    
    if not url:
        if os.getenv("RENDER"):
            logger.warning("DATABASE_URL not found. Using /tmp SQLite (data resets on restart).")
            return "sqlite+aiosqlite:////tmp/worktime_bot.db"
        else:
            os.makedirs("data", exist_ok=True)
            return "sqlite+aiosqlite:///data/worktime_bot.db"
    
    # 1. Принудительно ставим асинхронный драйвер asyncpg
    url = url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    
    # 2. Исправление ошибки SSL (asyncpg понимает ssl, а не sslmode)
    url = url.replace("sslmode=require", "ssl=require")
    url = url.replace("?sslmode=", "?ssl=")
    
    # 3. ИСПРАВЛЕНИЕ ОШИБКИ NEON (channel_binding)
    # asyncpg не понимает этот параметр, поэтому мы его безжалостно удаляем
    url = url.replace("&channel_binding=disable", "")
    url = url.replace("?channel_binding=disable", "?")
    url = url.replace("&channel_binding=require", "")
    url = url.replace("?channel_binding=require", "?")
    
    # Убираем висячий вопросительный знак, если он остался в конце строки
    if url.endswith("?"):
        url = url[:-1]
        
    # Безопасный лог (скрываем пароль)
    safe_url = url.split("@")[0] + "@***" if "@" in url else url
    logger.info(f"Database URL configured: {safe_url}")
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(tz))
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(tz))
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(tz))
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(tz))
    user: Mapped["User"] = relationship("User", back_populates="payrolls")
    __table_args__ = (UniqueConstraint("user_id", "year", "month", name="uq_payroll_user_month"),)

# Async engine
engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        if RESET_DB:
            logger.warning("⚠️ RESET_DB is True. Dropping all tables and recreating schema...")
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database schema synchronized and initialized successfully.")

# Helper functions
def get_now():
    return datetime.now(tz)

def calculate_month_norm_hours(year, month, hol_calendar, weekly_hours=40.0, workdays_per_week=5):
    if workdays_per_week <= 0:
        return 0.0
    hours_per_day = weekly_hours / workdays_per_week
    working_days = 0
    num_days = calendar.monthrange(year, month)[1]
    for day in range(1, num_days + 1):
        d = date(year, month, day)
        if d.weekday() < 5:
            if d not in hol_calendar:
                working_days += 1
    return round(working_days * hours_per_day, 2)

def get_effective_hourly_rate(user, norm_hours):
    if user.monthly_salary and norm_hours and norm_hours > 0:
        return round(user.monthly_salary / norm_hours, 2)
    if user.hourly_rate:
        return user.hourly_rate
    return 0.0

def get_user_norm_hours(user, year, month):
    if not user.auto_norm_hours and user.norm_hours_per_month and user.norm_hours_per_month > 0:
        return user.norm_hours_per_month
    return calculate_month_norm_hours(year, month, holiday_calendar, user.weekly_hours, user.workdays_per_week)

def get_advance_date(year, month):
    num_days = calendar.monthrange(year, month)[1]
    target_day = min(30, num_days)
    d = date(year, month, target_day)
    while d.weekday() >= 5 or d in holiday_calendar:
        d -= timedelta(days=1)
    return d

def get_salary_date(year, month):
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    d = date(next_year, next_month, 15)
    while d.weekday() >= 5 or d in holiday_calendar:
        d -= timedelta(days=1)
    return d

def format_hours(hours):
    return f"{hours:.2f}" if hours is not None else "0.00"

def format_money(amount):
    return f"{amount:,.2f}".replace(",", " ") if amount is not None else "0.00"

# Bot and Dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
main_router = Router()
admin_router = Router()
dp.include_router(main_router)
dp.include_router(admin_router)

class AdminStates(StatesGroup):
    waiting_salary = State()
    waiting_norm_hours = State()

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

# Database helpers
async def get_user_by_telegram_id(telegram_id: int):
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        return result.scalar_one_or_none()

async def get_active_shift(telegram_id: int):
    async with async_session() as session:
        result = await session.execute(select(WorkShift).where(WorkShift.user_id == telegram_id, WorkShift.end_time == None))
        return result.scalar_one_or_none()

async def get_active_break(telegram_id: int):
    async with async_session() as session:
        result = await session.execute(select(WorkBreak).where(WorkBreak.user_id == telegram_id, WorkBreak.is_active == True))
        return result.scalar_one_or_none()

async def get_today_shifts(telegram_id: int):
    async with async_session() as session:
        now = get_now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)
        result = await session.execute(select(WorkShift).where(WorkShift.user_id == telegram_id, WorkShift.start_time >= today_start, WorkShift.start_time < today_end, WorkShift.end_time != None))
        return result.scalars().all()

async def get_month_shifts(telegram_id: int, year: int, month: int):
    async with async_session() as session:
        month_start = datetime(year, month, 1, tzinfo=tz)
        month_end = datetime(year + 1, 1, 1, tzinfo=tz) if month == 12 else datetime(year, month + 1, 1, tzinfo=tz)
        result = await session.execute(select(WorkShift).where(WorkShift.user_id == telegram_id, WorkShift.start_time >= month_start, WorkShift.start_time < month_end, WorkShift.end_time != None))
        return result.scalars().all()

async def get_or_create_payroll(telegram_id: int, year: int, month: int):
    async with async_session() as session:
        result = await session.execute(select(Payroll).where(Payroll.user_id == telegram_id, Payroll.year == year, Payroll.month == month))
        payroll = result.scalar_one_or_none()
        if payroll:
            return payroll

        user_result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = user_result.scalar_one()

        norm_hours = get_user_norm_hours(user, year, month)
        shifts = await get_month_shifts(telegram_id, year, month)
        total_hours = sum(s.hours_worked or 0 for s in shifts)
        hourly_rate = get_effective_hourly_rate(user, norm_hours)
        base_amount = round(total_hours * hourly_rate, 2)
        advance_amount = round(base_amount * 0.4, 2)
        salary_amount = round(base_amount * 0.6, 2)

        payroll = Payroll(
            user_id=telegram_id, year=year, month=month, hours_worked=total_hours, norm_hours=norm_hours,
            overtime_hours=max(0, total_hours - norm_hours), undertime_hours=max(0, norm_hours - total_hours),
            hourly_rate=hourly_rate, base_amount=base_amount, advance_amount=advance_amount, salary_amount=salary_amount,
            advance_date=get_advance_date(year, month), salary_date=get_salary_date(year, month), calculated_at=get_now()
        )
        session.add(payroll)
        await session.commit()
        await session.refresh(payroll)
        return payroll

# Handlers (Start, Shifts, Breaks, Reports, Salary, Admin)
@main_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    telegram_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = message.from_user.full_name or ""

    user = await get_user_by_telegram_id(telegram_id)
    if user:
        await message.answer(f"👋 С возвращением, {full_name or username}!\nИспользуйте меню для управления рабочим временем.", reply_markup=get_main_keyboard(user.is_admin))
        return

    async with async_session() as session:
        count_result = await session.execute(select(func.count(User.id)))
        is_admin = count_result.scalar() == 0
        new_user = User(telegram_id=telegram_id, username=username, full_name=full_name, is_admin=is_admin, created_at=get_now())
        session.add(new_user)
        await session.commit()

    admin_text = "\n\n👑 Вы стали администратором системы!" if is_admin else ""
    await message.answer(f"✅ Регистрация успешна!\n\n👤 {full_name or username}\n🆔 ID: {telegram_id}{admin_text}\n\nИспользуйте меню для управления рабочим временем.", reply_markup=get_main_keyboard(is_admin))

@main_router.message(F.text == "▶️ Начать смену")
async def start_shift(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    active_shift = await get_active_shift(message.from_user.id)
    if active_shift:
        return await message.answer(f"⚠️ У вас уже есть активная смена с {active_shift.start_time.strftime('%H:%M')}")
    
    now = get_now()
    async with async_session() as session:
        session.add(WorkShift(user_id=message.from_user.id, start_time=now, created_at=now))
        await session.commit()
    await message.answer(f"✅ Смена начата!\n🕐 Время начала: {now.strftime('%H:%M')}\nУдачной работы! 💪")

@main_router.message(F.text == "🏁 Завершить смену")
async def end_shift(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    active_shift = await get_active_shift(message.from_user.id)
    if not active_shift: return await message.answer("⚠️ У вас нет активной смены.")

    now = get_now()
    active_break = await get_active_break(message.from_user.id)
    if active_break:
        async with async_session() as session:
            brk = await session.get(WorkBreak, active_break.id)
            brk.end_time = now
            brk.duration_hours = round((now - brk.start_time).total_seconds() / 3600, 4)
            brk.is_active = False
            await session.commit()

    async with async_session() as session:
        shift = await session.get(WorkShift, active_shift.id)
        shift.end_time = now
        gross_hours = round((now - shift.start_time).total_seconds() / 3600, 4)
        
        breaks_result = await session.execute(select(WorkBreak).where(WorkBreak.shift_id == shift.id, WorkBreak.end_time != None))
        total_break_hours = round(sum((b.end_time - b.start_time).total_seconds() for b in breaks_result.scalars().all()) / 3600, 4)
        
        shift.gross_hours = gross_hours
        shift.break_hours = total_break_hours
        shift.hours_worked = max(0, round(gross_hours - total_break_hours, 2))
        await session.commit()

    await message.answer(f"🏁 Смена завершена!\n🕐 Начало: {shift.start_time.strftime('%H:%M')}\n🕐 Конец: {now.strftime('%H:%M')}\n⏱ Общее время: {format_hours(gross_hours)} ч\n☕ Перерывы: {format_hours(total_break_hours)} ч\n✅ Отработано: {format_hours(shift.hours_worked)} ч")

@main_router.message(F.text == "☕ Перерывы")
async def show_break_menu(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    await message.answer("☕ Меню перерывов\nВыберите действие:", reply_markup=get_break_keyboard())

@main_router.message(F.text == "☕ Начать перерыв")
async def start_break(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    if not await get_active_shift(message.from_user.id): return await message.answer("⚠️ Сначала начните смену.")
    if await get_active_break(message.from_user.id): return await message.answer("⚠️ У вас уже есть активный перерыв.")
    await message.answer("Выберите тип перерыва:", reply_markup=get_break_type_keyboard())

@main_router.callback_query(F.data.startswith("break_type:"))
async def process_break_type(callback: CallbackQuery):
    break_type = callback.data.split(":")[1]
    if break_type == "cancel":
        await callback.message.edit_text("❌ Перерыв отменён.")
        return await callback.answer()

    active_shift = await get_active_shift(callback.from_user.id)
    if not active_shift: return await callback.answer("⚠️ Нет активной смены", show_alert=True)
    if await get_active_break(callback.from_user.id): return await callback.answer("⚠️ Уже есть активный перерыв", show_alert=True)

    now = get_now()
    type_names = {"lunch": "🍽 Обед", "coffee": "☕ Кофе", "smoke": "🚬 Перекур", "technical": "🔧 Технический", "other": "📋 Другой"}
    
    async with async_session() as session:
        session.add(WorkBreak(user_id=callback.from_user.id, shift_id=active_shift.id, break_type=break_type, start_time=now, is_active=True, created_at=now))
        await session.commit()

    await callback.message.edit_text(f"✅ Перерыв начат!\nТип: {type_names.get(break_type, break_type)}\n🕐 Время: {now.strftime('%H:%M')}")
    await callback.answer()

@main_router.message(F.text == "🔄 Завершить перерыв")
async def end_break(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    active_break = await get_active_break(message.from_user.id)
    if not active_break: return await message.answer("⚠️ У вас нет активного перерыва.")

    now = get_now()
    type_names = {"lunch": "🍽 Обед", "coffee": "☕ Кофе", "smoke": "🚬 Перекур", "technical": "🔧 Технический", "other": "📋 Другой"}
    
    async with async_session() as session:
        brk = await session.get(WorkBreak, active_break.id)
        brk.end_time = now
        brk.duration_hours = round((now - brk.start_time).total_seconds() / 3600, 4)
        brk.is_active = False
        await session.commit()

    await message.answer(f"✅ Перерыв завершён!\nТип: {type_names.get(brk.break_type, brk.break_type)}\n🕐 Начало: {brk.start_time.strftime('%H:%M')}\n🕐 Конец: {now.strftime('%H:%M')}\n⏱ Длительность: {format_hours(brk.duration_hours)} ч")

@main_router.message(F.text == "📊 Информация о перерывах")
async def break_info(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    active_shift = await get_active_shift(message.from_user.id)
    if not active_shift: return await message.answer("⚠️ Нет активной смены.")

    async with async_session() as session:
        result = await session.execute(select(WorkBreak).where(WorkBreak.shift_id == active_shift.id).order_by(WorkBreak.start_time))
        breaks = result.scalars().all()

    if not breaks: return await message.answer("📊 Перерывов за текущую смену пока нет.")
    
    type_names = {"lunch": "🍽 Обед", "coffee": "☕ Кофе", "smoke": "🚬 Перекур", "technical": "🔧 Технический", "other": "📋 Другой"}
    text = "📊 Перерывы за текущую смену:\n\n"
    total = 0
    for brk in breaks:
        status = "🟢 активен" if brk.is_active else f"✅ завершён ({format_hours(brk.duration_hours)} ч)"
        if not brk.is_active: total += brk.duration_hours or 0
        text += f"• {type_names.get(brk.break_type, brk.break_type)} — {status}\n"
    text += f"\n⏱ Всего перерывов: {format_hours(total)} ч"
    await message.answer(text)

@main_router.message(F.text == "🔙 Назад")
async def go_back(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    await message.answer("🏠 Главное меню", reply_markup=get_main_keyboard(user.is_admin))

@main_router.message(F.text == "📊 Отчёт за сегодня")
async def today_report(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    now = get_now()
    today_shifts = await get_today_shifts(message.from_user.id)
    active_shift = await get_active_shift(message.from_user.id)

    total_hours = sum(s.hours_worked or 0 for s in today_shifts)
    total_breaks = sum(s.break_hours or 0 for s in today_shifts)
    total_gross = sum(s.gross_hours or 0 for s in today_shifts)

    text = f"📊 Отчёт за сегодня ({now.strftime('%d.%m.%Y')})\n\n"
    if active_shift:
        elapsed = (now - active_shift.start_time).total_seconds() / 3600
        text += f"🟢 Активная смена (с {active_shift.start_time.strftime('%H:%M')})\n⏱ Прошло: {format_hours(elapsed)} ч\n\n"
    if today_shifts:
        text += f"✅ Завершённых смен: {len(today_shifts)}\n⏱ Общее время: {format_hours(total_gross)} ч\n☕ Перерывы: {format_hours(total_breaks)} ч\n✅ Отработано: {format_hours(total_hours)} ч\n"
    elif not active_shift:
        text += "Сегодня смен пока нет."
    await message.answer(text)

@main_router.message(F.text == "📈 Статистика за месяц")
async def month_stats(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    now = get_now()
    year, month = now.year, now.month
    month_names = {1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь", 7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"}
    
    shifts = await get_month_shifts(message.from_user.id, year, month)
    norm_hours = get_user_norm_hours(user, year, month)
    total_hours = sum(s.hours_worked or 0 for s in shifts)
    total_breaks = sum(s.break_hours or 0 for s in shifts)
    total_gross = sum(s.gross_hours or 0 for s in shifts)
    hourly_rate = get_effective_hourly_rate(user, norm_hours)
    base_amount = round(total_hours * hourly_rate, 2)

    text = f"📈 Статистика за {month_names.get(month, month)} {year}\n\n"
    text += f"📅 Рабочих дней: {len(shifts)}\n⏱ Общее время: {format_hours(total_gross)} ч\n☕ Перерывы: {format_hours(total_breaks)} ч\n✅ Отработано: {format_hours(total_hours)} ч\n📏 Норма часов: {format_hours(norm_hours)} ч\n\n"
    if total_hours > norm_hours: text += f"📈 Переработка: +{format_hours(total_hours - norm_hours)} ч\n"
    elif total_hours < norm_hours: text += f"📉 Недоработка: -{format_hours(norm_hours - total_hours)} ч\n"
    else: text += "✅ Норма выполнена точно\n"
    text += f"\n💰 Часовая ставка: {format_money(hourly_rate)} ₽/ч\n💵 Начислено: {format_money(base_amount)} ₽"
    await message.answer(text)

@main_router.message(F.text == "💰 Зарплата")
async def show_salary(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    now = get_now()
    year, month = now.year, now.month
    month_names = {1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь", 7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"}
    
    payroll = await get_or_create_payroll(message.from_user.id, year, month)
    text = f"💰 Расчёт зарплаты за {month_names.get(month, month)} {year}\n\n"
    text += f"💼 Оклад: {format_money(user.monthly_salary or 0)} ₽\n📏 Норма часов: {format_hours(payroll.norm_hours)} ч\n💵 Часовая ставка: {format_money(payroll.hourly_rate)} ₽/ч\n\n"
    text += f"✅ Отработано: {format_hours(payroll.hours_worked)} ч\n"
    if payroll.overtime_hours > 0: text += f"📈 Переработка: +{format_hours(payroll.overtime_hours)} ч\n"
    if payroll.undertime_hours > 0: text += f"📉 Недоработка: -{format_hours(payroll.undertime_hours)} ч\n"
    text += f"\n💵 Итого начислено: {format_money(payroll.base_amount)} ₽\n\n"
    text += f"🏦 Аванс (40%): {format_money(payroll.advance_amount)} ₽\n   📅 Дата: {payroll.advance_date.strftime('%d.%m.%Y') if payroll.advance_date else 'Н/Д'}\n"
    if payroll.advance_paid: text += "   ✅ Выплачен\n"
    text += f"\n🏦 Зарплата (60%): {format_money(payroll.salary_amount)} ₽\n   📅 Дата: {payroll.salary_date.strftime('%d.%m.%Y') if payroll.salary_date else 'Н/Д'}\n"
    if payroll.salary_paid: text += "   ✅ Выплачена"
    await message.answer(text)

@main_router.message(F.text == "⚙️ Настройки")
async def show_settings(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user: return await message.answer("❌ Сначала зарегистрируйтесь: /start")
    now = get_now()
    norm_hours = get_user_norm_hours(user, now.year, now.month)
    hourly_rate = get_effective_hourly_rate(user, norm_hours)
    text = f"⚙️ Ваши настройки\n\n💼 Оклад: {format_money(user.monthly_salary or 0)} ₽\n💵 Часовая ставка: {format_money(hourly_rate)} ₽/ч\n📏 Норма часов (тек. мес.): {format_hours(norm_hours)} ч\n🔄 Авто-расчёт нормы: {'✅ Да' if user.auto_norm_hours else '❌ Нет'}\n📅 Рабочих дней в неделю: {user.workdays_per_week}\n⏱ Часов в неделю: {user.weekly_hours}\n\nДля изменения настроек обратитесь к администратору."
    await message.answer(text, reply_markup=get_settings_keyboard(user))

@main_router.callback_query(F.data == "toggle_auto_norm")
async def toggle_auto_norm(callback: CallbackQuery):
    await callback.answer("⚠️ Только администратор может менять настройки нормы.", show_alert=True)

@main_router.callback_query(F.data == "settings_back")
async def settings_back(callback: CallbackQuery):
    try: await callback.message.edit_text("🏠 Возврат в главное меню")
    except TelegramBadRequest: pass
    await callback.answer()

# Admin handlers
@main_router.message(F.text == "👑 Админ-панель")
async def admin_panel(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user or not user.is_admin: return await message.answer("⛔ Доступ запрещён.")
    await message.answer("👑 Админ-панель\n\nВыберите действие:", reply_markup=get_admin_keyboard())

@main_router.message(F.text == "👥 Сотрудники")
@main_router.callback_query(F.data == "admin_employees")
async def admin_employees(message_or_callback):
    is_cb = isinstance(message_or_callback, CallbackQuery)
    telegram_id = message_or_callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        if is_cb: await message_or_callback.answer("⛔ Доступ запрещён", show_alert=True)
        else: await message_or_callback.answer("⛔ Доступ запрещён.")
        return

    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        employees = result.scalars().all()

    text = f"👥 Сотрудники ({len(employees)}):\n\n"
    for emp in employees:
        name = emp.full_name or emp.username or str(emp.telegram_id)
        text += f"• {name}{' 👑' if emp.is_admin else ''}\n  💼 Оклад: {format_money(emp.monthly_salary or 0)} ₽\n  🆔 {emp.telegram_id}\n\n"

    kb = get_admin_employee_keyboard(employees)
    if is_cb:
        try: await message_or_callback.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest: await message_or_callback.message.answer(text, reply_markup=kb)
        await message_or_callback.answer()
    else:
        await message_or_callback.answer(text, reply_markup=kb)

@admin_router.callback_query(F.data.startswith("admin_emp:"))
async def admin_employee_detail(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user or not user.is_admin: return await callback.answer("⛔ Доступ запрещён", show_alert=True)
    emp_id = int(callback.data.split(":")[1])
    
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == emp_id))
        emp = result.scalar_one_or_none()
    if not emp: return await callback.answer("❌ Сотрудник не найден", show_alert=True)

    now = get_now()
    norm_hours = get_user_norm_hours(emp, now.year, now.month)
    hourly_rate = get_effective_hourly_rate(emp, norm_hours)
    text = f"👤 {emp.full_name or emp.username or str(emp.telegram_id)}\n🆔 {emp.telegram_id}\n{'👑 Администратор' if emp.is_admin else '👤 Сотрудник'}\n\n"
    text += f"💼 Оклад: {format_money(emp.monthly_salary or 0)} ₽\n💵 Часовая ставка: {format_money(hourly_rate)} ₽/ч\n📏 Норма часов: {format_hours(norm_hours)} ч\n🔄 Авто-норма: {'✅' if emp.auto_norm_hours else '❌'}\n📅 Дней в неделю: {emp.workdays_per_week}\n⏱ Часов в неделю: {emp.weekly_hours}"

    try: await callback.message.edit_text(text, reply_markup=get_admin_employee_actions_keyboard(emp.telegram_id))
    except TelegramBadRequest: await callback.message.answer(text, reply_markup=get_admin_employee_actions_keyboard(emp.telegram_id))
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_set_salary:"))
async def admin_set_salary_start(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user or not user.is_admin: return await callback.answer("⛔ Доступ запрещён", show_alert=True)
    emp_id = int(callback.data.split(":")[1])
    await state.update_data(target_user_id=emp_id)
    await state.set_state(AdminStates.waiting_salary)
    
    async with async_session() as session:
        emp = (await session.execute(select(User).where(User.telegram_id == emp_id))).scalar_one_or_none()
    name = emp.full_name or emp.username or str(emp_id) if emp else str(emp_id)
    await callback.message.edit_text(f"💰 Введите новый оклад для {name}\nТекущий: {format_money(emp.monthly_salary or 0) if emp else 0} ₽\n\nВведите число:")
    await callback.answer()

@admin_router.message(AdminStates.waiting_salary)
async def admin_set_salary_process(message: Message, state: FSMContext):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user or not user.is_admin:
        await state.clear()
        return await message.answer("⛔ Доступ запрещён.")
    
    data = await state.get_data()
    try:
        salary = float(message.text.strip().replace(" ", "").replace(",", "."))
        if salary < 0: raise ValueError
    except (ValueError, TypeError):
        return await message.answer("❌ Введите корректное число:")

    async with async_session() as session:
        emp = (await session.execute(select(User).where(User.telegram_id == data.get("target_user_id")))).scalar_one_or_none()
        if emp:
            emp.monthly_salary = salary
            await session.commit()
    
    await state.clear()
    await message.answer(f"✅ Оклад обновлён!\n💰 Новый оклад: {format_money(salary)} ₽", reply_markup=get_admin_keyboard())

@admin_router.callback_query(F.data.startswith("admin_set_norm:"))
async def admin_set_norm_start(callback: CallbackQuery, state: FSMContext):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user or not user.is_admin: return await callback.answer("⛔ Доступ запрещён", show_alert=True)
    emp_id = int(callback.data.split(":")[1])
    await state.update_data(target_user_id=emp_id)
    await state.set_state(AdminStates.waiting_norm_hours)
    
    async with async_session() as session:
        emp = (await session.execute(select(User).where(User.telegram_id == emp_id))).scalar_one_or_none()
    name = emp.full_name or emp.username or str(emp_id) if emp else str(emp_id)
    await callback.message.edit_text(f"📅 Введите новую норму часов для {name}\nТекущая: {format_hours(emp.norm_hours_per_month or 0) if emp else 0} ч\n\nВведите число:")
    await callback.answer()

@admin_router.message(AdminStates.waiting_norm_hours)
async def admin_set_norm_process(message: Message, state: FSMContext):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user or not user.is_admin:
        await state.clear()
        return await message.answer("⛔ Доступ запрещён.")
    
    data = await state.get_data()
    try:
        norm = float(message.text.strip().replace(" ", "").replace(",", "."))
        if norm < 0: raise ValueError
    except (ValueError, TypeError):
        return await message.answer("❌ Введите корректное число:")

    async with async_session() as session:
        emp = (await session.execute(select(User).where(User.telegram_id == data.get("target_user_id")))).scalar_one_or_none()
        if emp:
            emp.norm_hours_per_month = norm
            emp.auto_norm_hours = False
            await session.commit()
    
    await state.clear()
    await message.answer(f"✅ Норма часов обновлена!\n📅 Новая норма: {format_hours(norm)} ч\n🔄 Авто-расчёт отключён.", reply_markup=get_admin_keyboard())

@admin_router.callback_query(F.data.startswith("admin_toggle_norm:"))
async def admin_toggle_norm(callback: CallbackQuery):
    user = await get_user_by_telegram_id(callback.from_user.id)
    if not user or not user.is_admin: return await callback.answer("⛔ Доступ запрещён", show_alert=True)
    emp_id = int(callback.data.split(":")[1])
    
    async with async_session() as session:
        emp = (await session.execute(select(User).where(User.telegram_id == emp_id))).scalar_one_or_none()
        if emp:
            emp.auto_norm_hours = not emp.auto_norm_hours
            await session.commit()
            await callback.answer(f"Авто-норма: {'✅ Включён' if emp.auto_norm_hours else '❌ Выключен'}", show_alert=True)

@admin_router.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    try: await callback.message.edit_text("🏠 Главное меню")
    except TelegramBadRequest: pass
    await callback.answer()

@main_router.message(F.text == "💰 Расчёт зарплат")
@main_router.callback_query(F.data == "admin_payroll_list")
async def admin_payroll_list(message_or_callback):
    is_cb = isinstance(message_or_callback, CallbackQuery)
    telegram_id = message_or_callback.from_user.id
    user = await get_user_by_telegram_id(telegram_id)
    if not user or not user.is_admin:
        if is_cb: await message_or_callback.answer("⛔ Доступ запрещён", show_alert=True)
        else: await message_or_callback.answer("⛔ Доступ запрещён.")
        return

    now = get_now()
    year, month = now.year, now.month
    month_names = {1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь", 7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"}
    
    async with async_session() as session:
        result = await session.execute(select(User).order_by(User.created_at))
        employees = result.scalars().all()

    text = f"💰 Расчёт зарплат — {month_names.get(month, month)} {year}\n\n"
    for emp in employees:
        payroll = await get_or_create_payroll(emp.telegram_id, year, month)
        name = emp.full_name or emp.username or str(emp.telegram_id)
        text += f"👤 {name}\n  ⏱ {format_hours(payroll.hours_worked)}/{format_hours(payroll.norm_hours)} ч\n  💵 {format_money(payroll.base_amount)} ₽\n  Аванс: {'✅' if payroll.advance_paid else '⬜'} | ЗП: {'✅' if payroll.salary_paid else '⬜'}\n\n"

    if is_cb:
        try: await message_or_callback.message.edit_text(text)
        except TelegramBadRequest: await message_or_callback.message.answer(text)
        await message_or_callback.answer()
    else:
        await message_or_callback.answer(text)

@main_router.message(F.text == "✏️ Изменить оклад")
@main_router.message(F.text == "📅 Изменить норму часов")
async def admin_change_prompt(message: Message):
    user = await get_user_by_telegram_id(message.from_user.id)
    if not user or not user.is_admin: return await message.answer("⛔ Доступ запрещён.")
    async with async_session() as session:
        employees = (await session.execute(select(User).order_by(User.created_at))).scalars().all()
    await message.answer("✏️ Выберите сотрудника:", reply_markup=get_admin_employee_keyboard(employees))

@main_router.message(F.text == "🔙 Назад", StateFilter(AdminStates.waiting_salary, AdminStates.waiting_norm_hours))
async def cancel_admin_action(message: Message, state: FSMContext):
    await state.clear()
    user = await get_user_by_telegram_id(message.from_user.id)
    kb = get_main_keyboard(user.is_admin) if user else get_main_keyboard(False)
    await message.answer("🏠 Возврат в главное меню", reply_markup=kb)

# Scheduled tasks
async def recalculate_all_payrolls():
    logger.info("Starting scheduled payroll recalculation")
    now = get_now()
    async with async_session() as session:
        users = (await session.execute(select(User))).scalars().all()
    for user in users:
        try:
            await get_or_create_payroll(user.telegram_id, now.year, now.month)
        except Exception as e:
            logger.error(f"Error recalculating payroll for {user.telegram_id}: {e}")
    logger.info("Payroll recalculation complete")

# Web server for health check and webhook
async def health_handler(request):
    return web.json_response({"status": "ok"})

async def create_web_app():
    from aiogram.types import Update
    
    app = web.Application()
    app.router.add_get("/health", health_handler)
    
    if RENDER_EXTERNAL_URL:
        webhook_path = f"/webhook/{BOT_TOKEN}"
        
        async def webhook_handler(request: web.Request) -> web.Response:
            try:
                # Получаем JSON от Telegram
                data = await request.json()
                # Корректно парсим через Pydantic V2 (стандарт aiogram 3.x)
                update = Update.model_validate(data)
                # Передаем в диспетчер
                await dp.feed_update(bot, update)
                return web.json_response({"ok": True})
            except Exception as e:
                logger.error(f"Webhook processing error: {e}", exc_info=True)
                return web.json_response({"ok": False}, status=500)
        
        app.router.add_post(webhook_path, webhook_handler)
        logger.info(f"Webhook handler registered at {webhook_path}")
        
    return app

async def on_startup():
    await init_db()
    logger.info("Database initialized successfully.")
    
    # Проверка токена
    try:
        me = await bot.get_me()
        logger.info(f"Bot authorized as @{me.username}")
    except Exception as e:
        logger.error(f"Bot authorization failed: {e}")
        raise
    
    # Scheduler
    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(recalculate_all_payrolls, CronTrigger(hour=0, minute=0), id="recalculate_payrolls", replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started")

    # Webhook setup
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}/webhook/{BOT_TOKEN}"
        await bot.set_webhook(
            webhook_url, 
            allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result", "chat_member", "my_chat_member"]
        )
        logger.info(f"Webhook set to {webhook_url}")
    else:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted, using polling")

async def main():
    await on_startup()
    
    web_app = await create_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

    if not RENDER_EXTERNAL_URL:
        try:
            await dp.start_polling(bot)
        except (KeyboardInterrupt, SystemExit):
            await on_shutdown()
    else:
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, SystemExit):
            await on_shutdown()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
