# ╔══════════════════════════════════════════════════════════════╗
# ║              WORKTRACK BOT — чистая версия                  ║
# ║                                                              ║
# ║  Переменные окружения на Render:                             ║
# ║    BOT_TOKEN     — токен от @BotFather                       ║
# ║    SHEET_ID      — ID Google таблицы                         ║
# ║    WEBHOOK_URL   — https://ИМЯ.onrender.com                  ║
# ║    GOOGLE_CREDS  — содержимое JSON сервисного аккаунта       ║
# ╚══════════════════════════════════════════════════════════════╝

import os, sys, json, re, logging, traceback
from datetime import datetime, time as dtime
import pytz, gspread
from google.oauth2.service_account import Credentials
from telegram import (Update, ReplyKeyboardMarkup,
                      InlineKeyboardMarkup, InlineKeyboardButton)
from telegram.ext import (Application, CommandHandler,
                          MessageHandler, CallbackQueryHandler,
                          ContextTypes, filters)

# ──────────────────────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
#  КОНФИГ
# ──────────────────────────────────────────────────────────────
def require(key):
    val = os.environ.get(key)
    if not val:
        log.error(f"Переменная {key} не задана"); sys.exit(1)
    return val

BOT_TOKEN    = require("BOT_TOKEN")
SHEET_ID     = require("SHEET_ID")
WEBHOOK_URL  = require("WEBHOOK_URL").rstrip("/")
GOOGLE_CREDS = require("GOOGLE_CREDS")
PORT         = int(os.environ.get("PORT", 8080))
TZ           = pytz.timezone("Asia/Dushanbe")

try:
    json.loads(GOOGLE_CREDS)
    log.info("OK GOOGLE_CREDS valid")
except json.JSONDecodeError as e:
    log.error(f"GOOGLE_CREDS невалидный JSON: {e}"); sys.exit(1)

log.info(f"Бот запускается | webhook={WEBHOOK_URL} | port={PORT}")

# ──────────────────────────────────────────────────────────────
#  GOOGLE SHEETS
# ──────────────────────────────────────────────────────────────
def get_sheets():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDS),
        scopes=["https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive"])
    ss = gspread.authorize(creds).open_by_key(SHEET_ID)
    return ss.worksheet("Users"), ss.worksheet("Attendance")

def today() -> str:
    return datetime.now(TZ).strftime("%d.%m.%Y")

# ── Users ──────────────────────────────────────────────────────
def find_user(chat_id: int) -> dict | None:
    ws, _ = get_sheets()
    for row in ws.get_all_values()[1:]:
        if row and str(row[0]) == str(chat_id):
            return {"chat_id": row[0], "name": row[1], "dept": row[2]}
    return None

def save_user(chat_id: int, name: str, dept: str):
    ws, _ = get_sheets()
    ws.append_row([str(chat_id), name, dept,
                   datetime.now(TZ).strftime("%d.%m.%Y %H:%M"), "Активен"])

# ── Attendance ─────────────────────────────────────────────────
def get_att_row(chat_id: int, date_str: str):
    _, ws = get_sheets()
    for i, row in enumerate(ws.get_all_values()[1:], start=2):
        if row and str(row[1]) == str(chat_id) and row[0] == date_str:
            return i, row
    return None, None

def ensure_att_row(chat_id: int):
    """Берёт существующую строку или создаёт новую"""
    rn, row = get_att_row(chat_id, today())
    if not rn:
        _, ws = get_sheets()
        ws.append_row([today(), str(chat_id), "", "", "Ожидание", ""])
        rn, row = get_att_row(chat_id, today())
    return rn, row

def update_att(row_num: int, col: int, value):
    _, ws = get_sheets()
    ws.update_cell(row_num, col, value)

def get_month_stats(chat_id: int) -> dict:
    _, ws = get_sheets()
    now = datetime.now(TZ)
    work = rest = pending = 0
    hours = 0.0
    for row in ws.get_all_values()[1:]:
        if not row or str(row[1]) != str(chat_id): continue
        try: d = datetime.strptime(row[0], "%d.%m.%Y")
        except: continue
        if d.month != now.month or d.year != now.year: continue
        if   row[4] == "Рабочий":  work += 1; hours += float(row[5] or 0)
        elif row[4] == "Выходной": rest += 1
        else:                      pending += 1
    return {"work": work, "rest": rest, "pending": pending,
            "hours": round(hours, 1),
            "avg": round(hours / work, 1) if work else 0}

def get_day_stats(chat_id: int, date_str: str) -> dict | None:
    _, ws = get_sheets()
    for row in ws.get_all_values()[1:]:
        if row and str(row[1]) == str(chat_id) and row[0] == date_str:
            return {"date": row[0], "start": row[2], "end": row[3],
                    "status": row[4], "hours": row[5]}
    return None

# ──────────────────────────────────────────────────────────────
#  КЛАВИАТУРЫ
# ──────────────────────────────────────────────────────────────
MAIN_KB = ReplyKeyboardMarkup([
    ["📊 Статистика", "📅 Сегодня"],
    ["📆 По дате",    "ℹ️ Помощь"],
], resize_keyboard=True, persistent=True)

def calendar_kb(year: int, month: int) -> InlineKeyboardMarkup:
    import calendar
    MN = ["Январь","Февраль","Март","Апрель","Май","Июнь",
          "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]
    rows = [[
        InlineKeyboardButton("◀️", callback_data=f"CAL_{year}_{month-1}"),
        InlineKeyboardButton(f"{MN[month]} {year}", callback_data="IGN"),
        InlineKeyboardButton("▶️", callback_data=f"CAL_{year}_{month+1}"),
    ]]
    for week in calendar.monthcalendar(year, month + 1):
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(" ", callback_data="IGN"))
            else:
                ds = f"{day:02d}.{month+1:02d}.{year}"
                row.append(InlineKeyboardButton(
                    str(day), callback_data=f"DAY_{ds}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)

# ──────────────────────────────────────────────────────────────
#  СОСТОЯНИЕ РЕГИСТРАЦИИ
# ──────────────────────────────────────────────────────────────
reg: dict = {}   # { chat_id: { step, name, dept } }

# ──────────────────────────────────────────────────────────────
#  ТЕКСТЫ
# ──────────────────────────────────────────────────────────────
MONTHS = ["","Январь","Февраль","Март","Апрель","Май","Июнь",
          "Июль","Август","Сентябрь","Октябрь","Ноябрь","Декабрь"]

def fmt_stats(user: dict, s: dict) -> str:
    now = datetime.now(TZ)
    return (
        f"📊 <b>Статистика за {MONTHS[now.month]} {now.year}</b>\n"
        f"👤 {user['name']} · {user['dept']}\n\n"
        f"🟢 Рабочих дней: <b>{s['work']}</b>\n"
        f"🔵 Выходных: <b>{s['rest']}</b>\n"
        f"⏳ Нет данных: <b>{s['pending']}</b>\n\n"
        f"⏱ Всего часов: <b>{s['hours']} ч.</b>\n"
        f"📈 Среднее/день: <b>{s['avg']} ч.</b>"
    )

def fmt_day(user: dict, d: dict) -> str:
    return (
        f"📅 <b>{d['date']}</b>\n"
        f"👤 {user['name']}\n\n"
        f"Статус: <b>{d['status'] or '—'}</b>\n"
        f"Начало: <b>{d['start'] or '—'}</b>\n"
        f"Конец:  <b>{d['end'] or '—'}</b>\n"
        f"Часов:  <b>{d['hours'] or '—'}</b>"
    )

# ──────────────────────────────────────────────────────────────
#  ХЕЛПЕР: анимация загрузки
# ──────────────────────────────────────────────────────────────
async def loading(update: Update, label: str):
    m = await update.message.reply_text(f"⏳ {label}")
    async def finish(text: str):
        try:    await m.edit_text(text, parse_mode="HTML")
        except: await update.message.reply_text(text, parse_mode="HTML")
    return finish

# ──────────────────────────────────────────────────────────────
#  РЕГИСТРАЦИЯ
# ──────────────────────────────────────────────────────────────
async def start_reg(update: Update):
    chat_id = update.effective_chat.id
    reg[chat_id] = {"step": 1}
    await update.message.reply_text(
        "👋 <b>Привет!</b>\n\n"
        "Я бот учёта рабочего времени.\n"
        "Для доступа нужна регистрация.\n\n"
        "<b>Шаг 1 из 3</b>\n"
        "Напиши своё ФИО:\n<code>Иванов Иван Иванович</code>",
        parse_mode="HTML")

async def process_reg(update: Update, text: str) -> bool:
    chat_id = update.effective_chat.id
    s = reg.get(chat_id)
    if not s: return False

    if s["step"] == 1:
        s["name"] = text; s["step"] = 2
        await update.message.reply_text(
            f"✅ ФИО: <b>{text}</b>\n\n"
            "<b>Шаг 2 из 3</b>\n"
            "В каком отделе работаешь?\n"
            "<code>Разработка / Бухгалтерия / HR</code>",
            parse_mode="HTML")

    elif s["step"] == 2:
        s["dept"] = text; s["step"] = 3
        await update.message.reply_text(
            f"✅ Отдел: <b>{text}</b>\n\n"
            "<b>Шаг 3 из 3 — Подтверди:</b>\n\n"
            f"👤 ФИО: <b>{s['name']}</b>\n"
            f"🏢 Отдел: <b>{text}</b>\n\n"
            "Напиши <b>да</b> для завершения\n"
            "или <b>нет</b> для отмены.",
            parse_mode="HTML")

    elif s["step"] == 3:
        if text.lower() == "да":
            save_user(chat_id, s["name"], s["dept"])
            reg.pop(chat_id, None)
            await update.message.reply_text(
                "🎉 <b>Готово! Ты зарегистрирован.</b>\n\n"
                "Каждый день я буду спрашивать:\n"
                "• <b>09:00</b> — начало рабочего дня\n"
                "• <b>16:30</b> — конец рабочего дня\n\n"
                "Используй кнопки ниже 👇",
                parse_mode="HTML", reply_markup=MAIN_KB)
        else:
            reg.pop(chat_id, None)
            await update.message.reply_text(
                "❌ Регистрация отменена.\n"
                "Напиши /start чтобы начать заново.")
    return True

# ──────────────────────────────────────────────────────────────
#  ОТВЕТЫ НА ВОПРОСЫ О ВРЕМЕНИ
# ──────────────────────────────────────────────────────────────
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")

def parse_time(text: str):
    if TIME_RE.match(text):
        h, m = text.split(":")
        return f"{int(h):02d}:{m}"
    return None

async def handle_time_answer(update: Update, text: str):
    chat_id = update.effective_chat.id
    tlow    = text.lower()

    rn, row = ensure_att_row(chat_id)
    if not rn: return

    status     = row[4]
    start_time = row[2]
    end_time   = row[3]

    # Утренний вопрос
    if status == "Ожидание" and not start_time:
        if tlow in ("выходной", "нет"):
            update_att(rn, 5, "Выходной")
            await update.message.reply_text("✅ Записал — выходной день 🏖️")
        elif t := parse_time(text):
            update_att(rn, 3, t)
            update_att(rn, 5, "Рабочий")
            await update.message.reply_text(
                f"✅ Начало: <b>{t}</b> — хорошего дня! 💪",
                parse_mode="HTML")
        else:
            await update.message.reply_text(
                "❓ Напиши время: <code>09:00</code> или <b>выходной</b>",
                parse_mode="HTML")
        return

    # Вечерний вопрос
    if status == "Рабочий" and not end_time:
        if tlow == "продолжаю":
            await update.message.reply_text(
                "💪 Ок! Напиши время сам когда закончишь.")
        elif t := parse_time(text):
            sp = list(map(int, start_time.split(":")))
            ep = list(map(int, t.split(":")))
            h  = round(((ep[0]*60+ep[1]) - (sp[0]*60+sp[1])) / 60, 1)
            update_att(rn, 4, t)
            update_att(rn, 6, max(h, 0))
            await update.message.reply_text(
                f"✅ Конец: <b>{t}</b> · "
                f"Итого: <b>{max(h,0)} ч.</b>\n"
                f"Отдыхай! 🌙",
                parse_mode="HTML")
        else:
            await update.message.reply_text(
                "❓ Напиши: <code>17:00</code> или <b>продолжаю</b>",
                parse_mode="HTML")

# ──────────────────────────────────────────────────────────────
#  HANDLERS
# ──────────────────────────────────────────────────────────────
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    reg.pop(chat_id, None)
    user = find_user(chat_id)
    if user:
        await update.message.reply_text(
            f"👋 С возвращением, <b>{user['name']}</b>!\n"
            "Используй кнопки ниже 👇",
            parse_mode="HTML", reply_markup=MAIN_KB)
    else:
        await start_reg(update)

async def cmd_stats(update: Update, _: ContextTypes.DEFAULT_TYPE):
    user = find_user(update.effective_chat.id)
    if not user: return
    finish = await loading(update, "Считаю статистику...")
    await finish(fmt_stats(user, get_month_stats(update.effective_chat.id)))

async def cmd_today(update: Update, _: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = find_user(chat_id)
    if not user: return
    finish = await loading(update, "Загружаю...")
    d = get_day_stats(chat_id, today())
    await finish(fmt_day(user, d) if d else "ℹ️ Данных за сегодня ещё нет.")

async def cmd_date(update: Update, _: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)
    await update.message.reply_text(
        "📅 <b>Выбери дату:</b>", parse_mode="HTML",
        reply_markup=calendar_kb(now.year, now.month - 1))

async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ <b>Команды:</b>\n\n"
        "📊 /stats — статистика за месяц\n"
        "📅 /today — данные за сегодня\n"
        "📆 /date  — выбрать дату\n"
        "ℹ️ /help  — эта справка",
        parse_mode="HTML", reply_markup=MAIN_KB)

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()

    # Кнопки меню
    if text == "📊 Статистика": await cmd_stats(update, ctx); return
    if text == "📅 Сегодня":    await cmd_today(update, ctx); return
    if text == "📆 По дате":    await cmd_date(update, ctx);  return
    if text == "ℹ️ Помощь":     await cmd_help(update, ctx);  return

    # Регистрация
    if await process_reg(update, text): return

    # Незарегистрированный
    if not find_user(chat_id):
        await update.message.reply_text(
            "❗ Напиши /start чтобы зарегистрироваться.")
        return

    # Ответы на вопросы о времени
    await handle_time_answer(update, text)

async def on_callback(update: Update, _: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    data = q.data
    await q.answer()

    if data == "IGN": return

    if data.startswith("CAL_"):
        _, y, m = data.split("_"); y, m = int(y), int(m)
        if m > 11: m, y = 0,  y+1
        if m < 0:  m, y = 11, y-1
        await q.edit_message_reply_markup(reply_markup=calendar_kb(y, m))
        return

    if data.startswith("DAY_"):
        date_str = data[4:]
        chat_id  = q.message.chat_id
        user     = find_user(chat_id)
        if not user:
            await q.message.reply_text("❗ Сначала зарегистрируйся.")
            return
        d = get_day_stats(chat_id, date_str)
        await q.message.reply_text(
            fmt_day(user, d) if d
            else f"ℹ️ За <b>{date_str}</b> нет данных.",
            parse_mode="HTML")

# ──────────────────────────────────────────────────────────────
#  РАССЫЛКИ
# ──────────────────────────────────────────────────────────────
async def send_morning(ctx: ContextTypes.DEFAULT_TYPE):
    ws, att_ws = get_sheets()
    td = today()
    for row in ws.get_all_values()[1:]:
        if not row: continue
        chat_id, name = row[0], row[1]
        rn, _ = get_att_row(chat_id, td)
        if rn: continue   # уже есть — не дублируем
        att_ws.append_row([td, str(chat_id), "", "", "Ожидание", ""])
        try:
            await ctx.bot.send_message(
                chat_id=int(chat_id),
                text=f"🌅 <b>Доброе утро, {name}!</b>\n\n"
                     f"Сегодня <b>{td}</b>\n\n"
                     f"Во сколько начался твой рабочий день?\n\n"
                     f"Ответь: <code>09:00</code> "
                     f"или напиши <b>выходной</b>",
                parse_mode="HTML")
        except Exception as e:
            log.error(f"Утренняя рассылка {chat_id}: {e}")

async def send_evening(ctx: ContextTypes.DEFAULT_TYPE):
    ws, _ = get_sheets()
    td = today()
    for row in ws.get_all_values()[1:]:
        if not row: continue
        chat_id, name = row[0], row[1]
        rn, att = get_att_row(chat_id, td)
        if not rn: continue
        if att[4] == "Выходной" or att[3]: continue
        try:
            await ctx.bot.send_message(
                chat_id=int(chat_id),
                text=f"🌆 <b>Планируешь заканчивать, {name}?</b>\n\n"
                     f"Во сколько завершишь?\n\n"
                     f"Ответь: <code>17:30</code> "
                     f"или напиши <b>продолжаю</b>",
                parse_mode="HTML")
        except Exception as e:
            log.error(f"Вечерняя рассылка {chat_id}: {e}")

# ──────────────────────────────────────────────────────────────
#  STARTUP CHECK
# ──────────────────────────────────────────────────────────────
async def startup_check(app: Application):
    now = datetime.now(TZ)
    if now.hour < 9: return
    try:
        _, att_ws = get_sheets()
        rows = att_ws.get_all_values()[1:]
        td   = today()
        if any(r and r[0] == td for r in rows):
            log.info("Startup: утренняя рассылка уже была"); return
        log.info("Startup: отправляю пропущенную рассылку")
        class FakeCtx:
            def __init__(self, bot): self.bot = bot
        await send_morning(FakeCtx(app.bot))
    except Exception as e:
        log.error(f"Startup check: {e}")

# ──────────────────────────────────────────────────────────────
#  ЗАПУСК
# ──────────────────────────────────────────────────────────────
def main():
    app = (Application.builder()
           .token(BOT_TOKEN)
           .post_init(startup_check)
           .build())

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("date",  cmd_date))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_callback))

    app.job_queue.run_daily(send_morning, dtime(9,  0,  tzinfo=TZ))
    app.job_queue.run_daily(send_evening, dtime(16, 30, tzinfo=TZ))

    log.info("Запуск webhook...")
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"{WEBHOOK_URL}/webhook",
        url_path="/webhook")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(f"FATAL: {e}")
        traceback.print_exc()
        sys.exit(1)
