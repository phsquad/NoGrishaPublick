#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import random
import os
import asyncio
import traceback
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InlineQueryResultArticle, InputTextMessageContent, User
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    InlineQueryHandler,
    TypeHandler,
)
from telegram.error import BadRequest
from flask import Flask, request
from sqlalchemy import create_engine, Column, Integer, String, MetaData, Table, Boolean, Text, DateTime, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError, IntegrityError
from PIL import Image, ImageDraw, ImageFont
from io import BytesIO
from datetime import datetime, timedelta

# --- НАСТРОЙКИ ---
TOKEN = "ВАШ ТОКЕН ИЗ BOTFATHER"
WEBHOOK_URL = "Ваша ссылка из Render"
DATABASE_URL = os.environ.get('DATABASE_URL')
GROUP_CHAT_ID = os.environ.get('GROUP_CHAT_ID')

# Чтение списка администраторов из переменной окружения (разделенных запятыми)
ADMIN_USERNAMES_RAW = os.environ.get('ADMIN_USERNAMES_LIST', 'ваш ник через "')
ADMIN_USERNAMES = [u.strip() for u in ADMIN_USERNAMES_RAW.split(',')]

# Чтение списка администраторов по ID
ADMIN_IDS_RAW = os.environ.get('ADMIN_IDS_LIST', '')
ADMIN_IDS = [int(i.strip()) for i in ADMIN_IDS_RAW.split(',') if i.strip().isdigit()]
# -----------------

# --- НАСТРОЙКИ "УМНОГО" ВЫБОРА ---
DEBT_WEIGHT_MULTIPLIER = 5
STREAK_WEIGHT_MULTIPLIER = 0.5
COOLDOWN_TURNS = 2
SOS_KEYWORDS = ["я", "готов", "го", "могу", "подменю"]
NOVICE_LEAGUE_THRESHOLD = 3
MAX_DEBT_FOR_AUTO_CLEAR = 3
LOG_CLEANUP_MONTHS = 6

# --- НОВЫЕ НАСТРОЙКИ ---
DUTY_REMINDER = "Напоминание для дежурных: 1. Проверить чистоту доски. 2. Убедиться, что после пар выключен свет. 3. Проветрить помещение. Спасибо за вашу работу! ✨"

# --- НАСТРОЙКА БАЗЫ ДАННЫХ ---
db_available = False
db_session = None
try:
    if not DATABASE_URL: raise ValueError("Переменная окружения DATABASE_URL не найдена.")
    
    connect_args = {}
    if DATABASE_URL.startswith('postgresql://'):
        connect_args['sslmode'] = 'require' 
        
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
    
    metadata = MetaData()
    students = Table('students', metadata,
        Column('id', Integer, primary_key=True),
        Column('user_id', String(100), unique=True, nullable=True),
        Column('name', String(100), nullable=False),
        Column('username', String(100), unique=True, nullable=False),
        Column('duty_count', Integer, default=0),
        Column('duty_debt', Integer, default=0),
        Column('chat_id', String(100), nullable=True),
        Column('is_active', Boolean, default=True),
        Column('last_duty_turn', Integer, default=0),
        Column('streak', Integer, default=0),
        Column('skip_next_turn', Boolean, default=False),
        Column('has_immunity', Integer, default=0)
    )
    system_state = Table('system_state', metadata, Column('id', Integer, primary_key=True), Column('current_turn', Integer, default=0))
    current_pool = Table('current_pool', metadata, Column('id', Integer, primary_key=True), Column('username', String(100), unique=True, nullable=False))
    last_winners = Table('last_winners', metadata, Column('id', Integer, primary_key=True), Column('name', String(100), nullable=False), Column('username', String(100), nullable=False))
    
    duty_log = Table('duty_log', metadata,
        Column('id', Integer, primary_key=True),
        Column('timestamp', DateTime, default=datetime.utcnow),
        Column('turn', Integer, nullable=False),
        Column('winner_username', String(100), nullable=False),
        Column('pool_size', Integer, nullable=False),
        Column('reason', String(50), nullable=True)
    )
    
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db_session = Session()
    if db_session.query(system_state).count() == 0:
        db_session.execute(system_state.insert().values(id=1, current_turn=0))
        db_session.commit()
    db_available = True
    print("✅ Успешное подключение к базе данных.")
except (ValueError, OperationalError) as e:
    print(f"!!! ОШИБКА ПОДКЛЮЧЕНИЯ К БД: {e}")

# --- ИНИЦИАЛИЗАЦИЯ ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
application = Application.builder().token(TOKEN).build()
app = Flask(__name__)

# --- ФУНКЦИИ ДЛЯ РАБОТЫ С БД И УТИЛИТЫ ---

def find_student_record(user: User):
    """
    Ищет запись студента в БД по приоритету: User ID, затем Username.
    Возвращает запись или None.
    """
    session = db_session
    
    # 1. Самый надежный способ: поиск по уникальному User ID
    if user.id:
        record = session.query(students).filter(students.c.user_id == str(user.id)).first()
        if record:
            return record
            
    # 2. Запасной способ: поиск по юзернейму (без учета регистра)
    if user.username:
        username_with_at = f"@{user.username}"
        record = session.query(students).filter(students.c.username.ilike(username_with_at)).first()
        if record:
            # Если нашли по юзернейму, а user_id еще не записан - обновляем его!
            if not record.user_id:
                try:
                    session.execute(
                        students.update().where(students.c.id == record.id).values(user_id=str(user.id))
                    )
                    session.commit()
                except IntegrityError: # Если такой user_id уже занят другим пользователем
                    session.rollback()
                    logger.warning(f"Не удалось обновить user_id для {username_with_at}, т.к. ID {user.id} уже используется.")
            return record
            
    return None

def get_student_by_username(username):
    """Ищет пользователя по юзернейму без учета регистра. Используется в административных командах."""
    if not username.startswith('@'):
        username = '@' + username
    return db_session.query(students).filter(students.c.username.ilike(username)).first()

def get_master_list_from_db(only_active=False):
    query = db_session.query(students)
    if only_active:
        query = query.filter(students.c.is_active == True)
    return [{"id": s.id, "user_id": s.user_id, "name": s.name, "username": s.username, "duty_count": s.duty_count, "duty_debt": s.duty_debt, "is_active": s.is_active, "last_duty_turn": s.last_duty_turn, "streak": s.streak, "skip_next_turn": s.skip_next_turn, "has_immunity": s.has_immunity} 
            for s in query.order_by(students.c.name).all()]

def get_pool_from_db():
    return [s.username for s in db_session.query(current_pool).all()]

def get_winners_from_db():
    return [{"name": s.name, "username": s.username} for s in db_session.query(last_winners).all()]

def get_rank(duty_count):
    if duty_count >= 10000: return "ВЕРХОВНЫЙ МАГИСТР ШВАБРЫ 👑"
    if duty_count >= 10: return "Магистр швабры 🧹"
    elif duty_count >= 5: return "Опытный страж порядка 🛡️"
    elif duty_count >= 1: return "Новобранец чистоты ✨"
    else: return "Гражданский 🧑‍"

def is_admin(username, user_id):
    """Проверяет, является ли пользователь администратором, используя ID (приоритет) или юзернейм."""
    if user_id is not None and user_id in ADMIN_IDS:
        return True
    if username is not None:
        return username.lower() in [u.lower() for u in ADMIN_USERNAMES]
    return False

# --- ФУНКЦИЯ ГРАФИЧЕСКОГО РЕЙТИНГА ---
def generate_stats_image(all_students):
    """Генерирует изображение рейтинга студентов."""
    WIDTH = 850
    HEADER_HEIGHT = 100
    ROW_HEIGHT = 60
    PADDING = 20
    height = HEADER_HEIGHT + len(all_students) * ROW_HEIGHT + PADDING
    img = Image.new('RGB', (WIDTH, height), color='#222222')
    draw = ImageDraw.Draw(img)
    EMOJI_FONT_PATH = os.path.join(os.path.dirname(__file__), "NotoColorEmoji.ttf")
    if not os.path.exists(EMOJI_FONT_PATH):
        EMOJI_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if not os.path.exists(EMOJI_FONT_PATH):
            EMOJI_FONT_PATH = None
    try:
        font_header = ImageFont.truetype(EMOJI_FONT_PATH, 36) if EMOJI_FONT_PATH else ImageFont.load_default()
        font_body = ImageFont.truetype(EMOJI_FONT_PATH, 24) if EMOJI_FONT_PATH else ImageFont.load_default()
        font_small = ImageFont.truetype(EMOJI_FONT_PATH, 18) if EMOJI_FONT_PATH else ImageFont.load_default()
    except Exception as e:
        logger.error(f"Критическая ошибка загрузки шрифта: {e}. Используется дефолтный шрифт.")
        font_header = ImageFont.load_default()
        font_body = ImageFont.load_default()
        font_small = ImageFont.load_default()
    draw.text((PADDING, PADDING), "🏆 Рейтинг Хранителей Порядка", fill='#FFFFFF', font=font_header)
    y_offset = HEADER_HEIGHT
    medals = ["🥇", "🥈", "🥉"] 
    for i, student in enumerate(all_students):
        bg_color = '#333333' if i % 2 == 0 else '#444444'
        draw.rectangle([(0, y_offset), (WIDTH, y_offset + ROW_HEIGHT)], fill=bg_color)
        if i < 3:
            medal_text = medals[i]
            medal_color = '#FFD700'
        else:
            medal_text = "🔹"
            medal_color = '#AAAAAA'
        draw.text((PADDING, y_offset + 15), medal_text, fill=medal_color, font=font_body)
        name_text = f"{student.name}"
        rank_text = get_rank(student.duty_count)
        draw.text((PADDING + 60, y_offset + 10), name_text, fill='#FFFFFF', font=font_body)
        draw.text((PADDING + 60, y_offset + 35), rank_text, fill='#AAAAAA', font=font_small)
        stats_parts = []
        stats_parts.append(f"Дежурств: {student.duty_count}")
        if student.duty_debt > 0:
            stats_parts.append(f"Долг: {student.duty_debt} 💸")
        if student.streak > 0:
            stats_parts.append(f"Серия: {student.streak} 📈")
        if student.has_immunity > 0:
            stats_parts.append(f"Отдых: {student.has_immunity} 🛡️")
        if not student.is_active:
            stats_parts.append("(Неактивен)")
        stats_text = " | ".join(stats_parts)
        text_width = draw.textlength(stats_text, font=font_small)
        draw.text((WIDTH - PADDING - text_width, y_offset + 20), stats_text, fill='#00FF00' if student.duty_count > 0 else '#FF5555', font=font_small)
        y_offset += ROW_HEIGHT
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)
    return buffer

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if update.message.chat.type == 'private':
        student_record = find_student_record(user)
        if student_record:
            db_session.execute(students.update().where(students.c.id == student_record.id).values(
                chat_id=str(user.id), 
                user_id=str(user.id)
            ))
            db_session.commit()
            await update.message.reply_text("Спасибо! Ваш профиль обновлен. Теперь я смогу присылать уведомления и надежно вас узнавать.")
        else:
            await update.message.reply_text("Ваш профиль не найден в базе. Попросите администратора добавить вас.")
    await help_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "✨ Не Гриша: Интеллектуальная Рулетка Дежурств ✨\n\n"
        "Я — продвинутый бот-менеджер, который использует взвешенные алгоритмы и постоянную базу данных для справедливого распределения обязанностей. \n\n"
        "--- \n"
        "**📊 ДЛЯ ВСЕХ УЧАСТНИКОВ:**\n"
        "🧹 `/list` — Показать текущий пул участников, ожидающих розыгрыша.\n"
        "🗓️ `/today` — Узнать, кто назначен дежурным на сегодня (с распределением задач).\n"
        "👑 `/stats` — Посмотреть графический рейтинг, звания и личную статистику (долги, иммунитет).\n"
        "👤 `/profile` — Посмотреть свой личный профиль и текущий вес в рулетке.\n"
        "❓ `/help` — Показать это меню.\n\n"
        "--- \n"
        "**⚙️ ИНСТРУМЕНТЫ АДМИНИСТРАТОРА:**\n"
        "🎲 `/go` — Запустить умный розыгрыш, учитывающий долги, серии пропусков и кулдаун.\n"
        "🆘 `/sos` — Активировать поиск героя-замены в чате (с наградой в виде пропуска хода).\n"
        "🛠️ `/admin` — Открыть интерактивную панель управления ботом.\n"
        "🔄 `/reset` — Начать новый цикл (`soft` / `hard`).\n"
        "🧪 `/test` — Панель для симуляции розыгрыша и проверки логики алгоритма."
    )
    await update.message.reply_text(message)

async def list_participants(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pool_usernames = get_pool_from_db()
    if not pool_usernames:
        await update.message.reply_text("🎲 Пул дежурных пуст.")
        return
    master_list = get_master_list_from_db()
    pool_students = [s for s in master_list if s['username'] in pool_usernames]
    participant_lines = [f"👤 {p['name']} ({p['username']})" for p in pool_students]
    message = "👥 В рулетке остались:\n\n" + "\n".join(participant_lines)
    await update.message.reply_text(message)
    user = update.message.from_user
    current_user_record = find_student_record(user)
    if current_user_record and not current_user_record.chat_id:
        await update.message.reply_text(f"⚠️ {user.first_name}, чтобы получать личные напоминания о дежурстве, напиши мне в личку /start.")

async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    winners = get_winners_from_db()
    if not winners:
        await update.message.reply_text("🤔 Дежурные на сегодня еще не выбраны.")
        return
    master_list = get_master_list_from_db()
    winner_details = {s['username']: s for s in master_list if s['username'] in [w['username'] for w in winners]}
    winner1 = winners[0]
    winner2 = winners[1] if len(winners) > 1 else None
    status1 = " (Неактивен!)" if not winner_details.get(winner1['username'], {}).get('is_active', True) else ""
    status2 = " (Неактивен!)" if winner2 and not winner_details.get(winner2['username'], {}).get('is_active', True) else ""
    if winner2:
        message = (f"👮‍♂️ Сегодня дежурят:\n\n"
                   f"🧹 **Подметает:** 👤 {winner1['name']} ({winner1['username']}){status1}\n"
                   f"🧼 **Моет полы:** 👤 {winner2['name']} ({winner2['username']}){status2}")
    else:
        message = f"🦸‍♂️ Сегодня дежурит последний герой: {winner1['name']} ({winner1['username']}){status1}"
    await update.message.reply_text(message)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    all_students = db_session.query(students).order_by(students.c.duty_count.desc()).all()
    if not all_students:
        await update.message.reply_text("Список студентов пуст.")
        return
    user = update.message.from_user
    current_user_stats = find_student_record(user)
    if current_user_stats and current_user_stats.duty_debt > 0:
        await update.message.reply_text(f"💸 Напоминание: У тебя {current_user_stats.duty_debt} долг(а) по дежурству! Это увеличивает твой шанс быть выбранным.")
    try:
        image_buffer = generate_stats_image(all_students)
        await update.message.reply_photo(photo=image_buffer, caption="📊 Рейтинг Хранителей Порядка:")
    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}")
        stats_lines = []
        medals = ["🥇", "🥈", "🥉"]
        for i, student in enumerate(all_students):
            medal = medals[i] if i < 3 else "🔹"
            rank = get_rank(student.duty_count)
            debt_info = f" (долг: {student.duty_debt})" if student.duty_debt > 0 else ""
            status_info = " (неактивен)" if not student.is_active else ""
            streak_info = f" (серия: {student.streak})" if student.streak > 0 else ""
            immunity_info = f" (Отдых: {student.has_immunity})" if student.has_immunity > 0 else ""
            stats_lines.append(f"{medal} {student.name} - {rank} ({student.duty_count} раз){debt_info}{status_info}{streak_info}{immunity_info}")
        message = "📊 **Рейтинг Хранителей Порядка (текстовая версия):**\n\n" + "\n".join(stats_lines)
        await update.message.reply_text(message)

async def gregory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ну конечно я буду и крутить, и накручивать, и никогда не буду дежурить.... Да. 😉")

async def dev_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "📢 УСЛУГИ ПО РАЗРАБОТКЕ TELEGRAM-БОТОВ НА PYTHON 🐍\n\n"
        "Ищете надежное и умное решение для автоматизации?\n\n"
        "Мы создаем кастомных Telegram-ботов любой сложности, используя Python и современные фреймворки.\n\n"
        "Наш опыт (на примере Duty Bot):\n"
        "1. Продвинутые алгоритмы (взвешенная рулетка, кулдаун, иммунитет).\n"
        "2. Интеграция с PostgreSQL для постоянного хранения данных.\n"
        "3. Современный UX (графический рейтинг, Inline-режим).\n"
        "4. Надежная облачная архитектура (Flask/Gunicorn).\n\n"
        "Свяжитесь с нами для консультации:\n"
        "Разработчик: @phsquadd\n"
        "Начните свой проект сегодня!"
    )
    await update.message.reply_text(message)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    student_record = find_student_record(user)
    if not student_record:
        await update.message.reply_text("❌ Ваш профиль не найден в базе данных. Возможно, вы не были добавлены. Попробуйте написать /start мне в личные сообщения.")
        return
    rank = get_rank(student_record.duty_count)
    debt_w = student_record.duty_debt * DEBT_WEIGHT_MULTIPLIER
    streak_w = student_record.streak * STREAK_WEIGHT_MULTIPLIER
    total_w = 1 + debt_w + streak_w
    status = "Активен ✅" if student_record.is_active else "Неактивен ❌"
    message = (
        f"👤 **ЛИЧНЫЙ ПРОФИЛЬ: {student_record.name}**\n\n"
        f"👑 Звание: {rank}\n"
        f"📊 Дежурств: {student_record.duty_count}\n"
        f"💸 Долг: {student_record.duty_debt} (Вес: +{debt_w:.1f})\n"
        f"📈 Серия пропусков: {student_record.streak} (Вес: +{streak_w:.1f})\n"
        f"🛡️ Отдых (Иммунитет): {student_record.has_immunity} ход(а)\n"
        f"✨ Общий вес в рулетке: **{total_w:.2f}**\n\n"
        f"Статус: {status}"
    )
    await update.message.reply_text(message)

# --- АДМИНИСТРАТИВНЫЕ КОМАНДЫ И ЛОГИКА ---
async def go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    current_turn = db_session.query(system_state).first().current_turn + 1
    db_session.execute(system_state.update().values(current_turn=current_turn))
    db_session.commit()
    pool_usernames = get_pool_from_db()
    if not pool_usernames:
        await update.message.reply_text("🎲 Пул пуст! Начните новый цикл командой `/reset`.")
        return
    master_list = get_master_list_from_db()
    pool_students = [s for s in master_list if s['username'] in pool_usernames and s['is_active']]
    heroes = [s for s in pool_students if s['skip_next_turn']]
    immune_students = [s for s in pool_students if s['has_immunity'] > 0]
    excluded_names = []
    for hero in heroes:
        db_session.execute(students.update().where(students.c.username == hero['username']).values(skip_next_turn=False))
        excluded_names.append(f"{hero['name']} (Герой)")
    for immune in immune_students:
        excluded_names.append(f"{immune['name']} (Отдых: {immune['has_immunity']} ход(а))")
    if excluded_names:
        await update.message.reply_text(f"🦸‍♂️ Следующие участники пропускают розыгрыш: {', '.join(excluded_names)}")
    pool_students = [s for s in pool_students if not s['skip_next_turn'] and s['has_immunity'] == 0]
    eligible_students = [s for s in pool_students if (current_turn - s['last_duty_turn']) > COOLDOWN_TURNS]
    if len(eligible_students) < 2:
        logger.warning(f"Кулдаун проигнорирован, т.к. осталось {len(eligible_students)} кандидатов.")
        eligible_students = pool_students
    if len(eligible_students) < 2:
        await update.message.reply_text("В пуле осталось меньше двух активных участников для выбора пары.")
        return
    novices = [s for s in eligible_students if s['duty_count'] < NOVICE_LEAGUE_THRESHOLD]
    veterans = [s for s in eligible_students if s['duty_count'] >= NOVICE_LEAGUE_THRESHOLD]
    winners = []
    winner_leagues = {}
    if novices and len(eligible_students) > 2:
        target_pool = novices
        weights = [1 + (s['duty_debt'] * DEBT_WEIGHT_MULTIPLIER) + (s['streak'] * STREAK_WEIGHT_MULTIPLIER) for s in target_pool]
        winner1 = random.choices(target_pool, weights=weights, k=1)[0]
        winners.append(winner1)
        eligible_students.remove(winner1)
        winner_leagues[winner1['username']] = "Новичок"
    else:
        target_pool = eligible_students
        weights = [1 + (s['duty_debt'] * DEBT_WEIGHT_MULTIPLIER) + (s['streak'] * STREAK_WEIGHT_MULTIPLIER) for s in target_pool]
        winner1 = random.choices(target_pool, weights=weights, k=1)[0]
        winners.append(winner1)
        eligible_students.remove(winner1)
        winner_leagues[winner1['username']] = "Ветеран" if winner1 in veterans else "Новичок"
    target_pool = eligible_students
    weights = [1 + (s['duty_debt'] * DEBT_WEIGHT_MULTIPLIER) + (s['streak'] * STREAK_WEIGHT_MULTIPLIER) for s in target_pool]
    winner2 = random.choices(target_pool, weights=weights, k=1)[0]
    winners.append(winner2)
    winner_leagues[winner2['username']] = "Ветеран" if winner2 in veterans else "Новичок"
    for student in pool_students:
        if student in winners:
            new_duty_count = student['duty_count'] + 1
            new_duty_debt = 0
            new_immunity_turns = 0
            if student['last_duty_turn'] == current_turn - 1:
                new_immunity_turns = 1 
            db_session.execute(students.update().where(students.c.username == student['username']).values(
                duty_count=new_duty_count, duty_debt=new_duty_debt, streak=0, last_duty_turn=current_turn, has_immunity=new_immunity_turns))
        else:
            new_duty_debt = student['duty_debt']
            if student['duty_debt'] < MAX_DEBT_FOR_AUTO_CLEAR:
                 new_duty_debt += 1
            new_immunity_turns = student['has_immunity']
            if student['has_immunity'] > 0:
                new_immunity_turns -= 1
            db_session.execute(students.update().where(students.c.username == student['username']).values(
                streak=students.c.streak + 1, duty_debt=new_duty_debt, has_immunity=new_immunity_turns))
    for winner in winners:
        db_session.query(current_pool).filter(current_pool.c.username == winner['username']).delete()
        db_session.execute(duty_log.insert().values(
            timestamp=datetime.utcnow(), turn=current_turn, winner_username=winner['username'], pool_size=len(pool_usernames), reason='regular'))
    db_session.query(last_winners).delete()
    for winner in winners:
        db_session.execute(last_winners.insert().values(name=winner['name'], username=winner['username']))
    db_session.commit()
    new_pool_count = db_session.query(current_pool).count()
    winner1_name = winners[0]['name']
    winner1_username = winners[0]['username']
    winner2_name = winners[1]['name']
    winner2_username = winners[1]['username']
    message = (f"✨ Рулетка запущена! Сегодня дежурят:\n\n"
               f"🧹 Подметает ({winner_leagues[winner1_username]}): 👤 {winner1_name} {winner1_username}\n"
               f"🧼 Моет полы ({winner_leagues[winner2_username]}): 👤 {winner2_name} {winner2_username}\n\n"
               f"В рулетке осталось {new_pool_count} участников.")
    await update.message.reply_text(message)
    await update.message.reply_text(DUTY_REMINDER)
    def get_final_weight_info(student_data):
        debt_w = student_data['duty_debt'] * DEBT_WEIGHT_MULTIPLIER
        streak_w = student_data['streak'] * STREAK_WEIGHT_MULTIPLIER
        total_w = 1 + debt_w + streak_w
        reason = []
        if student_data['duty_debt'] > 0: reason.append(f"{student_data['duty_debt']} долг(а)")
        if student_data['streak'] > 0: reason.append(f"Серия {student_data['streak']}")
        return total_w, ", ".join(reason) if reason else "Базовый шанс"
    original_winner1 = next(s for s in master_list if s['username'] == winner1_username)
    original_winner2 = next(s for s in master_list if s['username'] == winner2_username)
    total_w1, reason1 = get_final_weight_info(original_winner1)
    student_record_1 = get_student_by_username(winner1_username)
    if student_record_1 and student_record_1.chat_id:
        try:
            notification = (f"👋 Привет! Напоминаю, что сегодня твоя очередь дежурить. Твоя задача: **Подмести**.\n\n"
                            f"📊 Твой вес в рулетке был: **{total_w1:.2f}** (Шанс: {reason1}).")
            await context.bot.send_message(chat_id=student_record_1.chat_id, text=notification)
        except Exception as e:
            logger.error(f"Не удалось отправить личное уведомление для {winner1_username}: {e}")
    total_w2, reason2 = get_final_weight_info(original_winner2)
    student_record_2 = get_student_by_username(winner2_username)
    if student_record_2 and student_record_2.chat_id:
        try:
            notification = (f"👋 Привет! Напоминаю, что сегодня твоя очередь дежурить. Твоя задача: **Помыть полы**.\n\n"
                            f"📊 Твой вес в рулетке был: **{total_w2:.2f}** (Шанс: {reason2}).")
            await context.bot.send_message(chat_id=student_record_2.chat_id, text=notification)
        except Exception as e:
            logger.error(f"Не удалось отправить личное уведомление для {winner2_username}: {e}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    reset_type = "hard"
    if context.args and context.args[0].lower() == 'soft':
        reset_type = "soft"
    master_list = get_master_list_from_db(only_active=True)
    if reset_type == 'soft':
        current_turn = db_session.query(system_state).first().current_turn
        master_list = [s for s in master_list if (current_turn - s['last_duty_turn']) > COOLDOWN_TURNS]
        reset_message = "мягкий сброс (с учетом кулдауна)"
    else:
        db_session.execute(students.update().values(streak=0))
        reset_message = "полный сброс"
    if not master_list:
        await update.message.reply_text("⚠️ Список активных студентов (с учетом фильтров) пуст!")
        return
    db_session.query(current_pool).delete()
    for student in master_list:
        try:
            db_session.execute(current_pool.insert().values(username=student['username']))
        except IntegrityError:
            db_session.rollback()
            continue
    db_session.query(last_winners).delete()
    db_session.commit()
    message = f"✅ Новый цикл запущен ({reset_message})! В рулетку снова загружено {len(master_list)} участников."
    await update.message.reply_text(message)

async def manage_add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("❌ Неверный формат. Используйте: `/add_user Имя Фамилия @username`")
        return
    username = context.args[-1]
    name = " ".join(context.args[:-1])
    if not username.startswith('@'):
        username = '@' + username
    if username == '@None' or username == '@':
        await update.message.reply_text("❌ Ошибка: Пользователь должен иметь публичный юзернейм (@username).")
        return
    try:
        db_session.execute(students.insert().values(name=name, username=username, duty_count=0, duty_debt=0, is_active=True, has_immunity=0))
        db_session.commit()
        await update.message.reply_text(f"✅ Пользователь {name} ({username}) успешно добавлен. Ему нужно написать /start боту в личку для активации уведомлений.")
    except IntegrityError:
        db_session.rollback()
        await update.message.reply_text(f"⚠️ Ошибка: Пользователь с username {username} уже существует.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка добавления: {e}")

async def manage_bulk_add_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    if not context.args:
        await update.message.reply_text("❌ Неверный формат. Используйте: `/bulk_add Имя1 @user1; Имя2 @user2; ...` (разделяйте точкой с запятой)")
        return
    text = " ".join(context.args)
    lines = [line.strip() for line in text.split(';') if line.strip()]
    added_count = 0
    skipped_count = 0
    for line in lines:
        try:
            parts = line.split('@')
            if len(parts) != 2 or not parts[0].strip() or not parts[1].strip(): 
                skipped_count += 1
                logger.warning(f"Пропущен неверный формат: {line}")
                continue
            name = parts[0].strip()
            username = "@" + parts[1].strip()
            if username == '@None' or username == '@':
                skipped_count += 1
                continue
            if get_student_by_username(username):
                skipped_count += 1
                continue
            db_session.execute(students.insert().values(name=name, username=username, duty_count=0, duty_debt=0, is_active=True, has_immunity=0))
            added_count += 1
        except Exception:
            db_session.rollback()
            skipped_count += 1
    try:
        db_session.commit()
        await update.message.reply_text(
            f"✅ Массовое добавление завершено!\n"
            f"Добавлено новых пользователей: **{added_count}**\n"
            f"Пропущено (неверный формат или уже существуют): **{skipped_count}**"
        )
    except Exception as e:
        db_session.rollback()
        await update.message.reply_text(f"❌ Критическая ошибка при сохранении в БД: {e}")

async def manage_remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    try:
        username_to_remove = context.args[0]
        user_to_remove = get_student_by_username(username_to_remove)
        if user_to_remove:
            db_session.query(students).filter(students.c.id == user_to_remove.id).delete()
            db_session.query(current_pool).filter(current_pool.c.username == user_to_remove.username).delete()
            db_session.commit()
            await update.message.reply_text(f"✅ Пользователь {user_to_remove.name} ({user_to_remove.username}) удален из всех списков.")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {username_to_remove} не найден.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Неверный формат. Используйте: `/remove_user @username`")

async def debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    try:
        username_to_penalize = context.args[0]
        student_record = get_student_by_username(username_to_penalize)
        if student_record:
            db_session.execute(students.update().where(students.c.id == student_record.id).values(duty_debt=students.c.duty_debt + 1))
            db_session.commit()
            await update.message.reply_text(f"✅ Долг для {student_record.name} ({student_record.username}) увеличен.")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {username_to_penalize} не найден.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Неверный формат. Используйте: `/debt @username`")

async def clear_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    try:
        username_to_clear = context.args[0]
        student_record = get_student_by_username(username_to_clear)
        if student_record:
            db_session.execute(students.update().where(students.c.id == student_record.id).values(duty_debt=0))
            db_session.commit()
            await update.message.reply_text(f"✅ Долги для {student_record.name} ({student_record.username}) сброшены до 0.")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {username_to_clear} не найден.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Неверный формат. Используйте: `/clear_debt @username`")

async def reset_debt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    db_session.execute(students.update().values(duty_debt=0))
    db_session.commit()
    await update.message.reply_text("✅ Долги всех участников сброшены до 0.")

async def skip_user(update: Update, context: ContextTypes.DEFAULT_TYPE, make_active: bool):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    try:
        username_to_skip = context.args[0]
        student_record = get_student_by_username(username_to_skip)
        if student_record:
            update_values = {'is_active': make_active}
            if not make_active:
                update_values['duty_debt'] = student_record.duty_debt + 1
                db_session.query(current_pool).filter(current_pool.c.username == student_record.username).delete()
            db_session.execute(students.update().where(students.c.id == student_record.id).values(**update_values))
            db_session.commit()
            status = "возвращен в рулетку" if make_active else "временно убран из рулетки"
            penalty_info = " (Добавлен 1 долг)" if not make_active else ""
            await update.message.reply_text(f"✅ Пользователь {student_record.name} ({student_record.username}) {status}.{penalty_info}")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {username_to_skip} не найден.")
    except (IndexError, ValueError):
        command = "/unskip" if make_active else "/skip"
        await update.message.reply_text(f"❌ Неверный формат. Используйте: `{command} @username`")

async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await skip_user(update, context, make_active=False)

async def unskip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await skip_user(update, context, make_active=True)

async def set_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ Только админ может накручивать очки!")
        return
    try:
        username = context.args[0]
        count = int(context.args[1])
        student_record = get_student_by_username(username)
        if student_record:
            db_session.execute(students.update().where(students.c.id == student_record.id).values(duty_count=count))
            db_session.commit()
            await update.message.reply_text(f"✅ Читерство удалось! Счетчик для {student_record.name} теперь равен {count}.")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {username} не найден.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Неверный формат. Используйте: `/set_stats @username <число>`")

async def set_immunity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    try:
        username = context.args[0]
        turns = int(context.args[1])
        student_record = get_student_by_username(username)
        if student_record:
            db_session.execute(students.update().where(students.c.id == student_record.id).values(has_immunity=turns))
            db_session.commit()
            await update.message.reply_text(f"✅ Иммунитет для {student_record.name} установлен на {turns} ход(а).")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {username} не найден.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Неверный формат. Используйте: `/set_immunity @username <число ходов>`")

async def remove_from_pool(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    try:
        username_to_remove = context.args[0]
        if not username_to_remove.startswith('@'):
            username_to_remove = '@' + username_to_remove
        deleted_count = db_session.query(current_pool).filter(current_pool.c.username == username_to_remove).delete()
        db_session.commit()
        if deleted_count > 0:
            await update.message.reply_text(f"✅ Пользователь {username_to_remove} удален из текущего пула (без штрафа).")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {username_to_remove} не найден в текущем пуле.")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Неверный формат. Используйте: `/remove_from_pool @username`")

async def set_turn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    try:
        new_turn = int(context.args[0])
        db_session.execute(system_state.update().values(current_turn=new_turn))
        db_session.commit()
        await update.message.reply_text(f"✅ Текущий ход системы установлен на {new_turn}. (Влияет на кулдаун).")
    except (IndexError, ValueError):
        await update.message.reply_text("❌ Неверный формат. Используйте: `/set_turn <число>`")

async def show_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    query = db_session.query(duty_log).order_by(duty_log.c.timestamp.desc())
    limit = 10
    filter_username = None
    if context.args:
        if context.args[0].startswith('@'):
            filter_username = context.args[0]
            query = query.filter(duty_log.c.winner_username == filter_username)
            try:
                if len(context.args) > 1:
                    limit = int(context.args[1])
            except ValueError: pass
        else:
            try:
                limit = int(context.args[0])
            except ValueError:
                await update.message.reply_text("❌ Неверный формат. Используйте: `/show_log [@username] [число]`")
                return
    logs = query.limit(limit).all()
    if not logs:
        await update.message.reply_text(f"Журнал событий {'для ' + filter_username if filter_username else ''} пуст.")
        return
    log_lines = [f"**📜 Журнал последних {len(logs)} событий{(' для ' + filter_username) if filter_username else ''}:**"]
    for log in logs:
        time_str = log.timestamp.strftime('%Y-%m-%d %H:%M UTC')
        log_lines.append(f"[{time_str}] Ход {log.turn}: {log.winner_username} ({log.reason})")
    await update.message.reply_text("\n".join(log_lines))

async def sos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    winners = get_winners_from_db()
    if not winners:
        await update.message.reply_text("🤔 Дежурные на сегодня еще не выбраны, некому искать замену.")
        return
    if 'sos_active' in context.chat_data and context.chat_data['sos_active']:
        await update.message.reply_text("⚠️ Поиск героя уже активен в этом чате!")
        return
    context.chat_data['sos_active'] = True
    context.chat_data['sos_winners'] = [w['username'] for w in winners]
    winner_names = ", ".join([w['name'] for w in winners])
    message = (
        f"🆘 Внимание! {winner_names} не может(ут) сегодня дежурить!\n\n"
        f"Кто готов подменить? Первый, кто напишет в чат одно из ключевых слов: "
        f"**{', '.join(SOS_KEYWORDS)}**, станет героем и пропустит свое следующее дежурство!"
    )
    await update.message.reply_text(message)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ Эта команда доступна только администраторам.")
        return
    await update.message.reply_text('Панель администратора:', reply_markup=get_admin_panel_keyboard())

async def test_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    keyboard = [
        [InlineKeyboardButton("🎲 Симуляция розыгрыша", callback_data='test_simulate_go')],
        [InlineKeyboardButton("🛡️ Дать 1 ход иммунитета", callback_data='test_give_immunity')],
        [InlineKeyboardButton("⬅️ Назад в админ-панель", callback_data='back_to_admin')]
    ]
    await update.message.reply_text('Панель тестирования:', reply_markup=InlineKeyboardMarkup(keyboard))

async def test_give_immunity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data['next_step'] = 'set_immunity_user'
    await query.edit_message_text("🛡️ Введите @username и количество ходов иммунитета (например, `@user 2`):")

async def test_simulate_go(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat_id = update.effective_chat.id
    if not is_admin(user.username, user.id):
        await context.bot.send_message(chat_id=chat_id, text="⛔️ У вас нет прав для симуляции.")
        return
    await query.answer("Запуск симуляции...")
    try:
        master_list = get_master_list_from_db()
        pool_usernames = get_pool_from_db()
        if len(pool_usernames) < 2:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ В пуле меньше двух участников. Симуляция невозможна.")
            return
        pool_students = [s for s in master_list if s['username'] in pool_usernames and s['is_active']]
        eligible_students = pool_students
        if len(eligible_students) < 2:
            await context.bot.send_message(chat_id=chat_id, text="В пуле осталось меньше двух активных участников для симуляции.")
            return
        weights = [1 + (s['duty_debt'] * DEBT_WEIGHT_MULTIPLIER) + (s['streak'] * STREAK_WEIGHT_MULTIPLIER) for s in eligible_students]
        winner1 = random.choices(eligible_students, weights=weights, k=1)[0]
        temp_pool = eligible_students.copy()
        temp_weights = weights.copy()
        winner1_index = temp_pool.index(winner1)
        temp_pool.pop(winner1_index)
        temp_weights.pop(winner1_index)
        winner2 = random.choices(temp_pool, weights=temp_weights, k=1)[0]
        winners = [winner1, winner2]
        message = (f"🧪 **СИМУЛЯЦИЯ РОЗЫГРЫША** 🧪\n\n"
                   f"Текущий пул: {len(pool_usernames)} чел.\n"
                   f"Выбраны (без сохранения в БД):\n\n"
                   f"👤 {winners[0]['name']} {winners[0]['username']} (Вес: {weights[eligible_students.index(winner1)]:.2f})\n"
                   f"👤 {winners[1]['name']} {winners[1]['username']} (Вес: {weights[eligible_students.index(winner2)]:.2f})\n\n"
                   f"Это тестовый результат, который не влияет на статистику.")
        await context.bot.send_message(
            chat_id=chat_id, 
            text=message, 
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в Тест-панель", callback_data='back_to_test')]]))
    except Exception as e:
        error_message = f"❌ КРИТИЧЕСКАЯ ОШИБКА СИМУЛЯЦИИ: {type(e).__name__}: {e}"
        logger.error(error_message)
        await context.bot.send_message(chat_id=chat_id, text=error_message)

async def debug_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user 
    if not is_admin(user.username, user.id):
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⛔️ У вас нет прав.")
        return
    db_status = "❌ Недоступна"
    try:
        db_session.query(system_state).first()
        db_status = "✅ Активна"
    except Exception as e:
        db_status = f"❌ Ошибка: {type(e).__name__}"
    current_turn = db_session.query(system_state).first().current_turn
    webhook = await context.bot.get_webhook_info()
    pool_usernames = get_pool_from_db()
    master_list = get_master_list_from_db()
    pool_students = [s for s in master_list if s['username'] in pool_usernames and s['is_active']]
    eligible_students = [s for s in pool_students if (current_turn - s['last_duty_turn']) > COOLDOWN_TURNS]
    ineligible_students = [s for s in pool_students if (current_turn - s['last_duty_turn']) <= COOLDOWN_TURNS]
    cooldown_info = []
    for s in ineligible_students:
        turns_ago = current_turn - s['last_duty_turn']
        cooldown_info.append(f"  - {s['name']}: Дежурил {turns_ago} ход(а) назад (Кулдаун: {COOLDOWN_TURNS})")
    weighted_students = []
    for s in eligible_students:
        debt_weight = s['duty_debt'] * DEBT_WEIGHT_MULTIPLIER
        streak_weight = s['streak'] * STREAK_WEIGHT_MULTIPLIER
        total_weight = 1 + debt_weight + streak_weight
        weighted_students.append({'name': s['name'], 'total': total_weight, 'debt_w': debt_weight, 'streak_w': streak_weight})
    weighted_students.sort(key=lambda x: x['total'], reverse=True)
    weights_info = [f"  - {s['name']} ({s['total']:.2f}): 1 + {s['debt_w']:.1f} (Долг) + {s['streak_w']:.1f} (Серия)" for s in weighted_students[:5]]
    latest_logs = db_session.query(duty_log).order_by(duty_log.c.timestamp.desc()).limit(5).all()
    log_info = [f"  - Ход {log.turn}: {log.winner_username} (Пул: {log.pool_size})" for log in latest_logs]
    message = (
        f"🔍 DEBUG ИНФОРМАЦИЯ 🔍\n\n"
        f"КОНТЕКСТ АДМИНА:\n"
        f"  - Запросил: @{user.username} (ID: {user.id})\n"
        f"  - Статус: {'✅ АДМИН' if is_admin(user.username, user.id) else '❌ НЕ АДМИН'}\n\n"
        f"КОНФИГУРАЦИЯ:\n"
        f"  - Админы (Юзернеймы): {', '.join(ADMIN_USERNAMES) or 'Нет'}\n"
        f"  - Админы (ID): {', '.join(map(str, ADMIN_IDS)) or 'Нет'}\n"
        f"  - ID Группы: `{GROUP_CHAT_ID}`\n"
        f"  - Вес Долга (x{DEBT_WEIGHT_MULTIPLIER}), Вес Серии (x{STREAK_WEIGHT_MULTIPLIER})\n"
        f"  - Кулдаун: {COOLDOWN_TURNS} ход(а)\n\n"
        f"СИСТЕМА И БД:\n"
        f"  - Статус БД: {db_status}\n"
        f"  - Текущий ход: `{current_turn}`\n"
        f"  - Вебхук URL: `{webhook.url}`\n"
        f"  - Ожидают обновлений: `{webhook.pending_update_count}`\n\n"
        f"ИСКЛЮЧЕНЫ КУЛДАУНОМ ({len(ineligible_students)} чел.):\n"
        f"{chr(10).join(cooldown_info) or 'Нет.'}\n\n"
        f"РАСЧЕТ ВЕСОВ (Топ {len(weighted_students[:5])} из {len(eligible_students)}):\n"
        f"{chr(10).join(weights_info) or 'Пул для розыгрыша пуст.'}\n\n"
        f"ПОСЛЕДНИЕ 5 ЛОГОВ:\n"
        f"{chr(10).join(log_info) or 'Логи пусты.'}"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=message)

async def announce(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ Эта команда доступна только администраторам.")
        return
    if update.message.chat.type != 'private':
        await update.message.reply_text("⚠️ Пожалуйста, используйте эту команду в личном чате с ботом, чтобы не засорять общую беседу.")
        return
    context.user_data['next_step'] = 'announce_text'
    await update.message.reply_text("Пришлите мне текст объявления, которое нужно опубликовать в общем чате.")

async def clean_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if not is_admin(user.username, user.id):
        await update.message.reply_text("⛔️ У вас нет прав.")
        return
    cutoff_date = datetime.utcnow() - timedelta(days=30 * LOG_CLEANUP_MONTHS)
    try:
        deleted_count = db_session.query(duty_log).filter(duty_log.c.timestamp < cutoff_date).delete()
        db_session.commit()
        await update.message.reply_text(f"✅ Очистка завершена. Удалено {deleted_count} записей логов старше {LOG_CLEANUP_MONTHS} месяцев.")
    except Exception as e:
        db_session.rollback()
        await update.message.reply_text(f"❌ Ошибка при очистке логов: {e}")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    admin_ids_to_notify = ADMIN_IDS
    if not admin_ids_to_notify and ADMIN_USERNAMES:
        try:
            admin_user = await application.bot.get_chat(ADMIN_USERNAMES[0])
            admin_ids_to_notify = [admin_user.id]
        except Exception:
            logger.error("Не удалось получить ID админа по юзернейму для отправки ошибки.")
            return
    if not admin_ids_to_notify:
        return
    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)
    update_str = str(update)
    if len(update_str) > 500:
        update_str = update_str[:500] + "..."
    if len(tb_string) > 1000:
        tb_string = tb_string[:900] + "\n... (обрезано)"
    message = (
        f"🚨 КРИТИЧЕСКАЯ ОШИБКА В БОТЕ 🚨\n\n"
        f"Update: {update_str}\n"
        f"Ошибка: {context.error}\n\n"
        f"Трассировка:\n"
        f"```python\n{tb_string}\n```"
    )
    for admin_id in admin_ids_to_notify:
        try:
            await context.bot.send_message(chat_id=admin_id, text=message)
        except Exception as e:
            logger.error(f"Не удалось отправить ошибку админу {admin_id}: {e}")

# --- ОБРАБОТЧИКИ ИНТЕРФЕЙСА ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    command = query.data
    try:
        await query.answer()
    except BadRequest as e:
        if "Query is too old" in str(e):
            logger.warning(f"Пропущен просроченный CallbackQuery: {e}")
            return
        raise
    if command == 'admin_students':
        await query.edit_message_text('Меню управления пользователями:', reply_markup=get_manage_users_keyboard())
    elif command == 'admin_duty':
        keyboard = [[InlineKeyboardButton("🆘 Найти замену (SOS)", callback_data='duty_sos')],
                    [InlineKeyboardButton("🔄 Мягкий сброс", callback_data='duty_reset_soft')],
                    [InlineKeyboardButton("💥 Полный сброс", callback_data='duty_reset_hard')],
                    [InlineKeyboardButton("⬅️ Назад в админ-панель", callback_data='back_to_admin')]]
        await query.edit_message_text("Управление дежурством:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif command == 'admin_announce':
        context.user_data['next_step'] = 'announce_text'
        await query.edit_message_text("Пришлите мне текст объявления, которое нужно опубликовать в общем чате.")
    elif command == 'admin_debug':
        await debug_info(update, context)
    elif command == 'admin_test':
        await test_panel(update, context)
    elif command == 'admin_close':
        await query.edit_message_text("Админ-панель закрыта.")
    elif command == 'back_to_admin':
        await query.edit_message_text('Панель администратора:', reply_markup=get_admin_panel_keyboard())
    elif command == 'duty_sos':
        await sos(query, context)
    elif command == 'duty_reset_soft':
        context.args = ['soft']
        await reset(query, context)
    elif command == 'duty_reset_hard':
        context.args = ['hard']
        await reset(query, context)
    elif command.startswith('sos_'):
        await query.edit_message_text("Используйте команду /sos для активации поиска замены.")
    elif command == 'publish_announcement':
        if not GROUP_CHAT_ID:
            await query.edit_message_text("❌ Ошибка: ID общего чата не настроен на сервере.")
            return
        announcement_text = context.user_data.get('announcement_text', 'Пустое объявление.')
        final_text = f"📢 **Объявление от администрации** 📢\n\n{announcement_text}"
        try:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=final_text)
            await query.edit_message_text("✅ Объявление успешно опубликовано в общем чате!")
        except Exception as e:
            await query.edit_message_text(f"❌ Не удалось опубликовать объявление. Ошибка: {e}")
        finally:
            if 'announcement_text' in context.user_data: del context.user_data['announcement_text']
    elif command == 'cancel_announcement':
        if 'announcement_text' in context.user_data: del context.user_data['announcement_text']
        await query.edit_message_text("Публикация отменена.")
    elif command == 'manage_add':
        await query.edit_message_text(text="Для добавления используйте команду: `/add_user Имя Фамилия @username`")
    elif command == 'manage_bulk_add':
        await query.edit_message_text(text="Для массового добавления используйте команду: `/bulk_add Имя1 @user1; Имя2 @user2; ...`")
    elif command == 'manage_list':
        master_list = get_master_list_from_db()
        if not master_list: text = "Список пользователей пуст."
        else:
            user_lines = [f"👤 {user['name']} ({user['username']}){' (неактивен)' if not user['is_active'] else ''}" for user in master_list]
            text = "📋 **Полный список пользователей:**\n\n" + "\n".join(user_lines)
        await query.edit_message_text(text=text, reply_markup=get_manage_users_keyboard())
    elif command == 'manage_remove':
        master_list = get_master_list_from_db()
        if not master_list:
            await query.edit_message_text(text="Список пуст, некого удалять.", reply_markup=get_manage_users_keyboard())
            return
        keyboard = [[InlineKeyboardButton(f"❌ {user['name']}", callback_data=f"remove_{user['id']}")] for user in master_list]
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data='back_to_manage')])
        await query.edit_message_text(text="Выберите пользователя для удаления:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif command.startswith('remove_'):
        user_id_to_remove = int(query.data.split('_', 1)[1])
        user_to_remove = db_session.query(students).filter(students.c.id == user_id_to_remove).first()
        if user_to_remove:
            db_session.query(students).filter(students.c.id == user_id_to_remove).delete()
            db_session.query(current_pool).filter(current_pool.c.username == user_to_remove.username).delete()
            db_session.commit()
            await query.edit_message_text(text=f"✅ Пользователь {user_to_remove.name} удален из всех списков.")
        else:
            await query.edit_message_text(text="⚠️ Ошибка: пользователь не найден.")
    elif command == 'back_to_manage':
        await query.edit_message_text('Меню управления пользователями:', reply_markup=get_manage_users_keyboard())
    elif command == 'back_to_test':
        await test_panel(update, context)
    elif command == 'test_give_immunity':
        await test_give_immunity(update, context)

def get_admin_panel_keyboard():
    keyboard = [
        [InlineKeyboardButton("🧑‍🎓 Управление студентами", callback_data='admin_students')],
        [InlineKeyboardButton("🎲 Управление дежурством", callback_data='admin_duty')],
        [InlineKeyboardButton("📢 Сделать объявление", callback_data='admin_announce')],
        [InlineKeyboardButton("🔍 Диагностика", callback_data='admin_debug')],
        [InlineKeyboardButton("🧪 Тестирование", callback_data='admin_test')],
        [InlineKeyboardButton("❌ Закрыть", callback_data='admin_close')],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_manage_users_keyboard():
    keyboard = [
        [InlineKeyboardButton("➕ Добавить пользователя", callback_data='manage_add')],
        [InlineKeyboardButton("➕ Массовое добавление", callback_data='manage_bulk_add')],
        [InlineKeyboardButton("➖ Удалить пользователя", callback_data='manage_remove')],
        [InlineKeyboardButton("📋 Показать всех", callback_data='manage_list')],
        [InlineKeyboardButton("⬅️ Назад в админ-панель", callback_data='back_to_admin')],
    ]
    return InlineKeyboardMarkup(keyboard)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'sos_active' in context.chat_data and context.chat_data['sos_active']:
        user_text = update.message.text.lower().strip()
        if user_text in SOS_KEYWORDS:
            hero = update.message.from_user
            hero_record = find_student_record(hero)
            if not hero_record:
                await update.message.reply_text("❌ Чтобы стать героем, вы должны быть зарегистрированы в системе. Попросите админа добавить вас.")
                return
            if hero_record.username in context.chat_data['sos_winners']:
                await update.message.reply_text("Нельзя подменять самого себя! 😉")
                return
            sos_user = context.chat_data['sos_winners'][0]
            current_turn = db_session.query(system_state).first().current_turn
            db_session.execute(students.update().where(students.c.id == hero_record.id).values(skip_next_turn=True))
            db_session.execute(duty_log.insert().values(
                timestamp=datetime.utcnow(), turn=current_turn, winner_username=hero_record.username, pool_size=0, reason=f'sos_for_{sos_user}'))
            db_session.commit()
            await update.message.reply_text(
                f"🦸‍♂️ У нас есть герой! {hero.first_name} ({hero_record.username}) подменит дежурных "
                f"и пропускает свой следующий розыгрыш!")
            del context.chat_data['sos_active']
            del context.chat_data['sos_winners']
    elif context.user_data.get('next_step') == 'announce_text':
        del context.user_data['next_step']
        context.user_data['announcement_text'] = update.message.text
        keyboard = [[InlineKeyboardButton("✅ Опубликовать", callback_data='publish_announcement')],
                    [InlineKeyboardButton("❌ Отмена", callback_data='cancel_announcement')]]
        await update.message.reply_text(
            "**Предпросмотр объявления:**\n\n"
            f"{update.message.text}\n\n"
            "--- \n"
            "Публикуем?",
            reply_markup=InlineKeyboardMarkup(keyboard))
    elif context.user_data.get('next_step') == 'set_immunity_user':
        del context.user_data['next_step']
        text = update.message.text.split()
        if len(text) != 2:
            await update.message.reply_text("❌ Неверный формат. Используйте: `@username <число ходов>`")
            return
        username = text[0]
        try:
            turns = int(text[1])
        except ValueError:
            await update.message.reply_text("❌ Количество ходов должно быть числом.")
            return
        student_record = get_student_by_username(username)
        if student_record:
            db_session.execute(students.update().where(students.c.id == student_record.id).values(has_immunity=turns))
            db_session.commit()
            await update.message.reply_text(f"✅ Иммунитет для {student_record.name} установлен на {turns} ход(а).")
        else:
            await update.message.reply_text(f"⚠️ Пользователь {username} не найден.")

async def inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query
    results = []
    if "сегодня" in query.lower() or "today" in query.lower() or not query:
        winners = get_winners_from_db()
        if winners:
            title = "Дежурные на сегодня"
            if len(winners) == 2:
                description = f"{winners[0]['name']} и {winners[1]['name']}"
                message_text = (f"👮‍♂️ Сегодня дежурят:\n\n"
                                f"🧹 Подметает: 👤 {winners[0]['name']} ({winners[0]['username']})\n"
                                f"🧼 Моет полы: 👤 {winners[1]['name']} ({winners[1]['username']})")
            else:
                description = winners[0]['name']
                message_text = f"🦸‍♂️ Сегодня дежурит последний герой: {winners[0]['name']} ({winners[0]['username']})"
            results.append(InlineQueryResultArticle(id=str(random.randint(1000, 9999)), title=title,
                input_message_content=InputTextMessageContent(message_text), description=description))
        else:
            results.append(InlineQueryResultArticle(id=str(random.randint(1000, 9999)), title="Дежурные не выбраны",
                input_message_content=InputTextMessageContent("🤔 Дежурные на сегодня еще не выбраны."),
                description="Запустите /go в групповом чате."))
    await update.inline_query.answer(results)

# --- БЛОК ЗАПУСКА ---
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("go", go))
application.add_handler(CommandHandler("list", list_participants))
application.add_handler(CommandHandler("reset", reset))
application.add_handler(CommandHandler("today", today))
application.add_handler(CommandHandler("admin", admin_panel))
application.add_handler(CommandHandler("manage", admin_panel))
application.add_handler(CommandHandler("stats", stats))
application.add_handler(CommandHandler("profile", profile))
application.add_handler(CommandHandler("debt", debt))
application.add_handler(CommandHandler("clear_debt", clear_debt))
application.add_handler(CommandHandler("reset_debt", reset_debt))
application.add_handler(CommandHandler("add_user", manage_add_user))
application.add_handler(CommandHandler("bulk_add", manage_bulk_add_users))
application.add_handler(CommandHandler("remove_user", manage_remove_user))
application.add_handler(CommandHandler("remove_from_pool", remove_from_pool))
application.add_handler(CommandHandler("set_immunity", set_immunity))
application.add_handler(CommandHandler("set_turn", set_turn))
application.add_handler(CommandHandler("show_log", show_log))
application.add_handler(CommandHandler("clean_logs", clean_logs))
application.add_handler(CommandHandler("skip", skip))
application.add_handler(CommandHandler("unskip", unskip))
application.add_handler(CommandHandler("gregory", gregory))
application.add_handler(CommandHandler("set_stats", set_stats))
application.add_handler(CommandHandler("announce", announce))
application.add_handler(CommandHandler("sos", sos))
application.add_handler(CommandHandler("debug", debug_info))
application.add_handler(CommandHandler("test", test_panel))
application.add_handler(CommandHandler("dev", dev_info))
application.add_handler(InlineQueryHandler(inline_query))
application.add_handler(CallbackQueryHandler(button_handler))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
application.add_error_handler(error_handler)

@app.route('/', methods=['GET', 'POST'])
def webhook():
    if request.method == "POST":
        asyncio.run(handle_update(request.get_json()))
        return '', 200
    else:
        return "Бот жив и здоров!", 200

async def handle_update(update_data):
    async with application:
        await application.process_update(Update.de_json(update_data, application.bot))

async def setup_bot():
    if not db_available:
        logger.error("База данных недоступна. Бот не может быть запущен.")
        return
    await application.initialize()
    await application.bot.set_my_commands([
        ("list", "Показать, кто остался в рулетке"),
        ("stats", "Показать рейтинг дежурных"),
        ("today", "Показать, кто дежурит сегодня"),
        ("profile", "Показать мой профиль"),
        ("help", "Помощь и список команд"),
        ("admin", "Админка"),
    ])
    await application.bot.set_webhook(url=WEBHOOK_URL, allowed_updates=Update.ALL_TYPES)
    logger.info(f"Вебхук установлен на {WEBHOOK_URL}")
    await application.start()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    if loop.is_running():
        loop.create_task(setup_bot())
    else:
        loop.run_until_complete(setup_bot())