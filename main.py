import os
import logging
import json
from datetime import datetime, time
import pytz
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ──────────────────────────────────────────────────────────────
#  НАСТРОЙКИ
# ──────────────────────────────────────────────────────────────
BOT_TOKEN   = os.environ["BOT_TOKEN"]
SHEET_ID    = os.environ["SHEET_ID"]
WEBHOOK_URL = os.environ["WEBHOOK_URL"]   # https://ВАШ_ПРОЕКТ.railway.app
PORT        = int(os.environ.get("PORT", 8080))
TIMEZONE    = pytz.timezone("Asia/Dushanbe")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
#  GOOGLE SHEETS
# ──────────────────────────────────────────────────────────────
def get_gc():
    creds_json = os.environ["GOOGLE_CREDS"]  # JSON строка из переменной окружения
    creds_dict = json.loads(creds_json)
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def get_sheets():
    gc = get_gc()
    ss = gc.open_by_key(SHEET_ID)
    return ss.worksheet("Users"), ss.worksheet("Attendance")

def today_str():
    return datetime.now(TIMEZONE).strftime("%d.%m.%Y")

def find_user(chat_id):
    users_ws, _ = get_sheets()
    rows = users_ws.get_all_values()
    for row in rows[1:]:
        if row and str(row[0]) == str(chat_id):
            return {"chat_id": row[0], "name": row[1], "department": row[2]}
    return None

def save_user(chat_id, name, department):
    users_ws, _ = get_sheets()
    users_ws.append_row([
        str(chat_id), name, department,
        datetime.now(TIMEZONE).strftime("%d.%m.%Y %H:%M"), "Активен"
    ])

def find_att_row(chat_id, date_str):
    _, att_ws = get_sheets()
    rows = att_ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):
        if row and str(row[1]) == str(chat_id) and row[0] == date_str:
            return i, row
    return None, None

def create_att_row(chat_id):
    _, att_ws = get_sheets()
    att_ws.append_row([today_str(), str(chat_id), "", "", "Ожидание", ""])

def update_att_cell(row_num, col, value):
    _, att_ws = get_sheets()
    att_ws.update_cell(row_num, col, value)

# ──────────────────────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ──────────────────────────────────────────────────────────────
def main_keyboard():
    return ReplyKeyboardMarkup(
        [["📊 Статистика", "📅 Сегодня"],
         ["📆 По дате",    "ℹ️ Помощь"]],
        resize_keyboard=True
    )

def calendar_keyboard(year, month):
    import calendar
    months_ru = ["Январь","Февраль","Март","Апрель","Май","Июнь",
                 "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    buttons = [[
        InlineKeyboardButton("◀️", callback_data=f"CAL_{year}_{month-1}"),
        InlineKeyboardButton(f"{months_ru[month]} {year}", callback_data="IGNORE"),
        InlineKeyboardButton("▶️", callback_data=f"CAL_{year}_{month+1}"),
    ]]
    cal = calendar.monthcalendar(year, month + 1)
    for week in cal:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="IGNORE"))
            else:
                row.append(InlineKeyboardButton(str(day), callback_data=f"DATE_{year}_{month}_{day}"))
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)

# ──────────────────────────────────────────────────────────────
#  СОСТОЯНИЕ РЕГИСТРАЦИИ (в памяти — достаточно для бота)
# ──────────────────────────────────────────────────────────────
reg_state = {}  # { chat_id: { "step": 1, "name": "...", "dept": "..." } }

# ──────────────────────────────────────────────────────────────
#  HANDLERS
# ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = find_user(chat_id)

    if user:
        await update.message.reply_text(
            f"👋 С возвращением, <b>{user['name']}</b>!\nИспользуй кнопки 👇",
            parse_mode="HTML", reply_markup=main_keyboard()
        )
    else:
        reg_state[chat_id] = {"step": 1}
        await update.message.reply_text(
            "👋 <b>Привет!</b>\n\nЯ бот учёта рабочего времени.\n"
            "Для доступа нужна регистрация.\n\n"
            "<b>Шаг 1 из 3</b>\nНапиши своё ФИО:\n"
            "<code>Иванов Иван Иванович</code>",
            parse_mode="HTML"
        )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = find_user(chat_id)
    if not user: return

    msg = await update.message.reply_text("⏳ Считаю статистику...")

    _, att_ws = get_sheets()
    rows = att_ws.get_all_values()
    now = datetime.now(TIMEZONE)
    cm, cy = now.month, now.year

    work, rest, pending, total_h = 0, 0, 0, 0.0
    for row in rows[1:]:
        if not row or str(row[1]) != str(chat_id): continue
        try:
            d = datetime.strptime(row[0], "%d.%m.%Y")
        except: continue
        if d.month != cm or d.year != cy: continue
        if row[4] == "Рабочий":
            work += 1
            try: total_h += float(row[5])
            except: pass
        elif row[4] == "Выходной": rest += 1
        else: pending += 1

    months_ru = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
                 "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    avg = round(total_h / work, 1) if work else 0

    await msg.edit_text(
        f"📊 <b>Статистика за {months_ru[cm]} {cy}</b>\n"
        f"👤 {user['name']} · {user['department']}\n\n"
        f"🟢 Рабочих дней: <b>{work}</b>\n"
        f"🔵 Выходных: <b>{rest}</b>\n"
        f"⏳ Нет данных: <b>{pending}</b>\n\n"
        f"⏱ Всего часов: <b>{round(total_h, 1)} ч.</b>\n"
        f"📈 Среднее в день: <b>{avg} ч.</b>",
        parse_mode="HTML"
    )

async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = find_user(chat_id)
    if not user: return

    msg = await update.message.reply_text("⏳ Загружаю...")
    row_num, row = find_att_row(chat_id, today_str())

    if not row_num:
        await msg.edit_text("ℹ️ Данных за сегодня ещё нет.")
        return

    await msg.edit_text(
        f"📅 <b>Сегодня {today_str()}</b>\n"
        f"👤 {user['name']}\n\n"
        f"Статус: <b>{row[4] or '—'}</b>\n"
        f"Начало: <b>{row[2] or '—'}</b>\n"
        f"Конец:  <b>{row[3] or '—'}</b>\n"
        f"Часов:  <b>{row[5] or '—'}</b>",
        parse_mode="HTML"
    )

async def cmd_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TIMEZONE)
    await update.message.reply_text(
        "📅 <b>Выбери дату:</b>",
        parse_mode="HTML",
        reply_markup=calendar_keyboard(now.year, now.month - 1)
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Команды:</b>\n\n"
        "📊 /stats — статистика за месяц\n"
        "📅 /today — данные за сегодня\n"
        "📆 /date — выбрать дату\n"
        "ℹ️ /help — эта справка",
        parse_mode="HTML", reply_markup=main_keyboard()
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    text_low = text.lower()

    # Кнопки меню
    if text == "📊 Статистика": await cmd_stats(update, context); return
    if text == "📅 Сегодня":    await cmd_today(update, context); return
    if text == "📆 По дате":    await cmd_date(update, context);  return
    if text == "ℹ️ Помощь":     await cmd_help(update, context);  return

    # Регистрация
    if chat_id in reg_state:
        state = reg_state[chat_id]
        step = state["step"]

        if step == 1:
            state["name"] = text
            state["step"] = 2
            await update.message.reply_text(
                f"✅ ФИО: <b>{text}</b>\n\n"
                "<b>Шаг 2 из 3</b>\nВ каком отделе работаешь?\n"
                "<code>Разработка / Бухгалтерия / HR</code>",
                parse_mode="HTML"
            )

        elif step == 2:
            state["dept"] = text
            state["step"] = 3
            await update.message.reply_text(
                f"✅ Отдел: <b>{text}</b>\n\n"
                "<b>Шаг 3 из 3 — Подтверди:</b>\n\n"
                f"👤 ФИО: <b>{state['name']}</b>\n"
                f"🏢 Отдел: <b>{text}</b>\n\n"
                "Напиши <b>да</b> для завершения или <b>нет</b> для отмены.",
                parse_mode="HTML"
            )

        elif step == 3:
            if text_low == "да":
                save_user(chat_id, state["name"], state["dept"])
                del reg_state[chat_id]
                await update.message.reply_text(
                    "🎉 <b>Готово! Ты зарегистрирован.</b>\n\n"
                    "Каждый день я буду спрашивать:\n"
                    "• <b>09:00</b> — начало рабочего дня\n"
                    "• <b>16:30</b> — конец рабочего дня\n\n"
                    "Используй кнопки 👇",
                    parse_mode="HTML", reply_markup=main_keyboard()
                )
            else:
                del reg_state[chat_id]
                await update.message.reply_text(
                    "❌ Регистрация отменена.\nНапиши /start чтобы начать заново."
                )
        return

    # Незарегистрированный
    user = find_user(chat_id)
    if not user:
        await update.message.reply_text("❗ Напиши /start чтобы зарегистрироваться.")
        return

    # Ответы на вопросы о времени
    row_num, row = find_att_row(chat_id, today_str())
    if not row_num: return

    status, start_time, end_time = row[4], row[2], row[3]

    # Утренний вопрос
    if status == "Ожидание" and not start_time:
        if text_low in ("выходной", "нет"):
            update_att_cell(row_num, 5, "Выходной")
            await update.message.reply_text("✅ Записал — выходной день 🏖️")
        elif __import__("re").match(r"^\d{1,2}:\d{2}$", text):
            t = text.zfill(5) if len(text) == 4 else text
            t = f"{t[:2].zfill(2)}:{t[3:]}"
            update_att_cell(row_num, 3, t)
            update_att_cell(row_num, 5, "Рабочий")
            await update.message.reply_text(f"✅ Начало: <b>{t}</b> — хорошего дня! 💪", parse_mode="HTML")
        else:
            await update.message.reply_text("❓ Напиши время: <code>09:00</code> или <b>выходной</b>", parse_mode="HTML")
        return

    # Вечерний вопрос
    if status == "Рабочий" and not end_time:
        if text_low == "продолжаю":
            await update.message.reply_text("💪 Ок! Напиши время сам когда закончишь.")
        elif __import__("re").match(r"^\d{1,2}:\d{2}$", text):
            t = f"{text[:2].zfill(2)}:{text[3:]}"
            sp = list(map(int, start_time.split(":")))
            ep = list(map(int, t.split(":")))
            h = round(((ep[0]*60 + ep[1]) - (sp[0]*60 + sp[1])) / 60, 1)
            update_att_cell(row_num, 4, t)
            update_att_cell(row_num, 6, h if h > 0 else 0)
            await update.message.reply_text(
                f"✅ Конец: <b>{t}</b> · Итого: <b>{h if h > 0 else 0} ч.</b>\nОтдыхай! 🌙",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("❓ Напиши: <code>17:00</code> или <b>продолжаю</b>", parse_mode="HTML")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat_id
    data = query.data

    if data == "IGNORE":
        await query.answer()
        return

    if data.startswith("CAL_"):
        _, y, m = data.split("_")
        y, m = int(y), int(m)
        if m > 11: m, y = 0, y + 1
        if m < 0:  m, y = 11, y - 1
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=calendar_keyboard(y, m))
        return

    if data.startswith("DATE_"):
        _, year, month, day = data.split("_")
        year, month, day = int(year), int(month), int(day)
        await query.answer("⏳ Загружаю...")

        ds = f"{day:02d}.{month+1:02d}.{year}"
        row_num, row = find_att_row(chat_id, ds)
        user = find_user(chat_id)

        if row_num:
            text = (
                f"📅 <b>{ds}</b>\n👤 {user['name']}\n\n"
                f"Статус: <b>{row[4] or '—'}</b>\n"
                f"Начало: <b>{row[2] or '—'}</b>\n"
                f"Конец:  <b>{row[3] or '—'}</b>\n"
                f"Часов:  <b>{row[5] or '—'}</b>"
            )
        else:
            text = f"ℹ️ За <b>{ds}</b> нет данных."

        await query.message.reply_text(text, parse_mode="HTML")

# ──────────────────────────────────────────────────────────────
#  РАССЫЛКИ (job_queue)
# ──────────────────────────────────────────────────────────────
async def send_morning(context: ContextTypes.DEFAULT_TYPE):
    users_ws, att_ws = get_sheets()
    users = users_ws.get_all_values()
    td = today_str()

    for row in users[1:]:
        if not row: continue
        chat_id, name = row[0], row[1]
        row_num, _ = find_att_row(chat_id, td)
        if row_num: continue

        att_ws.append_row([td, str(chat_id), "", "", "Ожидание", ""])
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"🌅 <b>Доброе утро, {name}!</b>\n\nСегодня <b>{td}</b>\n\n"
                 f"Во сколько начался твой рабочий день?\n\n"
                 f"Ответь: <code>09:00</code> или напиши <b>выходной</b>",
            parse_mode="HTML"
        )

async def send_evening(context: ContextTypes.DEFAULT_TYPE):
    users_ws, att_ws = get_sheets()
    users = users_ws.get_all_values()
    td = today_str()

    for row in users[1:]:
        if not row: continue
        chat_id, name = row[0], row[1]
        row_num, att_row = find_att_row(chat_id, td)
        if not row_num: continue
        if att_row[4] == "Выходной" or att_row[3]: continue

        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"🌆 <b>Планируешь заканчивать, {name}?</b>\n\n"
                 f"Во сколько завершишь?\n\n"
                 f"Ответь: <code>17:30</code> или напиши <b>продолжаю</b>",
            parse_mode="HTML"
        )

# ──────────────────────────────────────────────────────────────
#  ЗАПУСК
# ──────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("date",  cmd_date))
    app.add_handler(CommandHandler("help",  cmd_help))

    # Сообщения и кнопки
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Рассылки по расписанию
    job_queue = app.job_queue
    job_queue.run_daily(send_morning, time=time(9, 0,  tzinfo=TIMEZONE))
    job_queue.run_daily(send_evening, time=time(16, 30, tzinfo=TIMEZONE))

    # Webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/webhook",
        url_path="/webhook",
    )

if __name__ == "__main__":
    main()
