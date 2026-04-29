import os
import sys
import logging
import json
import re
import traceback
from datetime import datetime, time
import pytz
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
#  НАСТРОЙКИ — с явной диагностикой при запуске
# ──────────────────────────────────────────────────────────────
logger.info("=== WORKTRACK BOT STARTING ===")

try:
    BOT_TOKEN = os.environ["BOT_TOKEN"]
    logger.info("OK BOT_TOKEN loaded")
except KeyError:
    logger.error("FAIL BOT_TOKEN not set"); sys.exit(1)

try:
    SHEET_ID = os.environ["SHEET_ID"]
    logger.info("OK SHEET_ID loaded")
except KeyError:
    logger.error("FAIL SHEET_ID not set"); sys.exit(1)

try:
    WEBHOOK_URL = os.environ["WEBHOOK_URL"].rstrip("/")
    logger.info(f"OK WEBHOOK_URL: {WEBHOOK_URL}")
except KeyError:
    logger.error("FAIL WEBHOOK_URL not set"); sys.exit(1)

try:
    creds_raw = os.environ["GOOGLE_CREDS"]
    json.loads(creds_raw)
    logger.info("OK GOOGLE_CREDS loaded and valid")
except KeyError:
    logger.error("FAIL GOOGLE_CREDS not set"); sys.exit(1)
except json.JSONDecodeError as e:
    logger.error(f"FAIL GOOGLE_CREDS invalid JSON: {e}"); sys.exit(1)

PORT     = int(os.environ.get("PORT", 8080))
TIMEZONE = pytz.timezone("Asia/Dushanbe")
logger.info(f"OK PORT: {PORT}")

# ──────────────────────────────────────────────────────────────
#  GOOGLE SHEETS
# ──────────────────────────────────────────────────────────────
def get_sheets():
    creds_dict = json.loads(os.environ["GOOGLE_CREDS"])
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
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
#  СОСТОЯНИЕ РЕГИСТРАЦИИ
# ──────────────────────────────────────────────────────────────
reg_state = {}

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
        try: d = datetime.strptime(row[0], "%d.%m.%Y")
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
        f"⏱ Всего часов: <b>{round(total_h,1)} ч.</b>\n"
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
        "📅 <b>Выбери дату:</b>", parse_mode="HTML",
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

    if text == "📊 Статистика": await cmd_stats(update, context); return
    if text == "📅 Сегодня":    await cmd_today(update, context); return
    if text == "📆 По дате":    await cmd_date(update, context);  return
    if text == "ℹ️ Помощь":     await cmd_help(update, context);  return

    # Регистрация
    if chat_id in reg_state:
        state = reg_state[chat_id]
        step  = state["step"]
        if step == 1:
            state["name"] = text
            state["step"] = 2
            await update.message.reply_text(
                f"✅ ФИО: <b>{text}</b>\n\n<b>Шаг 2 из 3</b>\n"
                "В каком отделе работаешь?\n<code>Разработка / Бухгалтерия / HR</code>",
                parse_mode="HTML"
            )
        elif step == 2:
            state["dept"] = text
            state["step"] = 3
            await update.message.reply_text(
                f"✅ Отдел: <b>{text}</b>\n\n<b>Шаг 3 из 3 — Подтверди:</b>\n\n"
                f"👤 ФИО: <b>{state['name']}</b>\n🏢 Отдел: <b>{text}</b>\n\n"
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
                await update.message.reply_text("❌ Регистрация отменена.\nНапиши /start чтобы начать заново.")
        return

    user = find_user(chat_id)
    if not user:
        await update.message.reply_text("❗ Напиши /start чтобы зарегистрироваться.")
        return

    row_num, row = find_att_row(chat_id, today_str())

    # Если строки нет (бот спал в 09:00) — создаём автоматически
    if not row_num:
        _, att_ws = get_sheets()
        att_ws.append_row([today_str(), str(chat_id), "", "", "Ожидание", ""])
        row_num, row = find_att_row(chat_id, today_str())
        if not row_num: return

    status, start_time, end_time = row[4], row[2], row[3]

    if status == "Ожидание" and not start_time:
        if text_low in ("выходной", "нет"):
            update_att_cell(row_num, 5, "Выходной")
            await update.message.reply_text("✅ Записал — выходной день 🏖️")
        elif re.match(r"^\d{1,2}:\d{2}$", text):
            parts = text.split(":")
            t = f"{parts[0].zfill(2)}:{parts[1]}"
            update_att_cell(row_num, 3, t)
            update_att_cell(row_num, 5, "Рабочий")
            await update.message.reply_text(f"✅ Начало: <b>{t}</b> — хорошего дня! 💪", parse_mode="HTML")
        else:
            await update.message.reply_text("❓ Напиши время: <code>09:00</code> или <b>выходной</b>", parse_mode="HTML")
        return

    if status == "Рабочий" and not end_time:
        if text_low == "продолжаю":
            await update.message.reply_text("💪 Ок! Напиши время сам когда закончишь.")
        elif re.match(r"^\d{1,2}:\d{2}$", text):
            parts = text.split(":")
            t  = f"{parts[0].zfill(2)}:{parts[1]}"
            sp = list(map(int, start_time.split(":")))
            ep = list(map(int, t.split(":")))
            h  = round(((ep[0]*60 + ep[1]) - (sp[0]*60 + sp[1])) / 60, 1)
            update_att_cell(row_num, 4, t)
            update_att_cell(row_num, 6, h if h > 0 else 0)
            await update.message.reply_text(
                f"✅ Конец: <b>{t}</b> · Итого: <b>{h if h > 0 else 0} ч.</b>\nОтдыхай! 🌙",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text("❓ Напиши: <code>17:00</code> или <b>продолжаю</b>", parse_mode="HTML")

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.message.chat_id
    data    = query.data

    if data == "IGNORE":
        await query.answer(); return

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
        ds      = f"{day:02d}.{month+1:02d}.{year}"
        row_num, row = find_att_row(chat_id, ds)
        user    = find_user(chat_id)
        if row_num:
            text = (f"📅 <b>{ds}</b>\n👤 {user['name']}\n\n"
                    f"Статус: <b>{row[4] or '—'}</b>\n"
                    f"Начало: <b>{row[2] or '—'}</b>\n"
                    f"Конец:  <b>{row[3] or '—'}</b>\n"
                    f"Часов:  <b>{row[5] or '—'}</b>")
        else:
            text = f"ℹ️ За <b>{ds}</b> нет данных."
        await query.message.reply_text(text, parse_mode="HTML")

# ──────────────────────────────────────────────────────────────
#  РАССЫЛКИ
# ──────────────────────────────────────────────────────────────
async def send_morning(context: ContextTypes.DEFAULT_TYPE):
    users_ws, att_ws = get_sheets()
    td = today_str()
    for row in users_ws.get_all_values()[1:]:
        if not row: continue
        chat_id, name = row[0], row[1]
        rn, _ = find_att_row(chat_id, td)
        if rn: continue
        att_ws.append_row([td, str(chat_id), "", "", "Ожидание", ""])
        await context.bot.send_message(
            chat_id=int(chat_id),
            text=f"🌅 <b>Доброе утро, {name}!</b>\n\nСегодня <b>{td}</b>\n\n"
                 f"Во сколько начался твой рабочий день?\n\n"
                 f"Ответь: <code>09:00</code> или напиши <b>выходной</b>",
            parse_mode="HTML"
        )

async def send_evening(context: ContextTypes.DEFAULT_TYPE):
    users_ws, _ = get_sheets()
    td = today_str()
    for row in users_ws.get_all_values()[1:]:
        if not row: continue
        chat_id, name = row[0], row[1]
        rn, att_row = find_att_row(chat_id, td)
        if not rn: continue
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
    logger.info("Building application...")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("date",  cmd_date))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    job_queue = app.job_queue
    job_queue.run_daily(send_morning, time=time(9,  0,  tzinfo=TIMEZONE))
    job_queue.run_daily(send_evening, time=time(16, 30, tzinfo=TIMEZONE))

    # Health check — UptimeRobot пингует этот URL чтобы Render не засыпал
    async def health(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("✅ Bot is running")
    app.add_handler(CommandHandler("health", health))

    # Запускаем утреннюю рассылку при старте ТОЛЬКО если
    # Render перезапустился после 09:00 И строк ещё нет в таблице
    async def startup_check(app):
        now = datetime.now(TIMEZONE)
        if now.hour < 9:
            return  # ещё рано — job_queue сам отправит в 09:00
        try:
            # Проверяем — уже есть хоть одна строка за сегодня?
            _, att_ws = get_sheets()
            rows = att_ws.get_all_values()[1:]
            today = now.strftime("%d.%m.%Y")
            already_sent = any(r and r[0] == today for r in rows)
            if already_sent:
                logger.info("Startup check: morning already sent, skipping")
                return
            # Строк нет — значит бот не работал в 09:00, отправляем
            class FakeCtx:
                def __init__(self, bot): self.bot = bot
            await send_morning(FakeCtx(app.bot))
            logger.info("Startup check: morning sent (bot was down at 09:00)")
        except Exception as e:
            logger.error(f"Startup check error: {e}")
    app.post_init = startup_check

    logger.info(f"Starting webhook on port {PORT}...")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/webhook",
        url_path="/webhook",
    )

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)
