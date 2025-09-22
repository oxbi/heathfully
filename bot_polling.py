#!/usr/bin/env python3
# Interactive Telegram bot for Healthfully Farm availability
# Buttons: "Run once now" and "Set daily time"
# Confirms first /start and confirms time changes with next-send info.

import os, json, re, logging
from datetime import time as dtime, datetime, timedelta
from pathlib import Path
from typing import Dict, Tuple

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

# --- your scraper (already in healthfully_bot.py) ---
from healthfully_bot import fetch, parse_catalog, build_message, SHOP_URL  # type: ignore

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("healthfully-ui")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SHOP = os.getenv("SHOP_URL", SHOP_URL)
DATA_FILE = Path("schedules.json")

WELCOME_TEXT = (
    "👋 *Welcome to Healthfully Farm Notifier!*\n\n"
    "Use the buttons below:\n"
    "• *▶️ Run once now* — sends today's stock report.\n"
    "• *⏰ Set daily time* — send a time like `08:30` (24-hour) and I'll DM the report every day.\n\n"
    "You can also type `/status` anytime to see your schedule."
)

# ---------- persistence ----------
def load_schedules() -> Dict[str, str]:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("Failed to read schedules.json: %s", e)
            return {}
    return {}

def save_schedules(d: Dict[str, str]) -> None:
    DATA_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")

# ---------- report ----------
def build_report() -> str:
    html = fetch(SHOP)
    in_stock, out_stock = parse_catalog(html, SHOP)
    return build_message(in_stock, out_stock, SHOP)

# ---------- time utils (server local tz) ----------
TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

def parse_hhmm(text: str) -> Tuple[int, int] | None:
    m = TIME_RE.match(text or "")
    if not m:
        return None
    h, m2 = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= m2 <= 59:
        return h, m2
    return None

def next_run_dt(hour: int, minute: int) -> datetime:
    now = datetime.now().astimezone()
    run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    return run

def describe_next_run(hour: int, minute: int) -> str:
    return next_run_dt(hour, minute).strftime("%a %Y-%m-%d %H:%M %Z")

# ---------- UI ----------
MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("▶️ Run once now", callback_data="run_now")],
    [InlineKeyboardButton("⏰ Set daily time", callback_data="set_time")],
])

ASK_TIME = (
    "Send a time in *24-hour* format, e.g. `08:30` or `21:05`.\n"
    "_I’ll send the report every day at that time (server local time)._"
)

AWAIT_FLAG = "await_time"  # context.user_data flag

# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    schedules = load_schedules()
    t = schedules.get(str(chat_id))

    if t:
        h, m = [int(x) for x in t.split(":")]
        status = f"Current schedule: *{t}* daily\nNext send: *{describe_next_run(h, m)}*"
        text = f"{WELCOME_TEXT}\n\n{status}"
    else:
        text = f"{WELCOME_TEXT}\n\n_No schedule set yet._ Tap *Set daily time*."

    await update.message.reply_text(
        text, reply_markup=MAIN_KB, parse_mode="Markdown", disable_web_page_preview=True
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        WELCOME_TEXT, reply_markup=MAIN_KB, parse_mode="Markdown"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    schedules = load_schedules()
    t = schedules.get(chat_id)
    if not t:
        await update.message.reply_text("No schedule set.", reply_markup=MAIN_KB)
        return
    h, m = [int(x) for x in t.split(":")]
    await update.message.reply_text(
        f"Current schedule: *{t}* daily\nNext send: *{describe_next_run(h, m)}*",
        parse_mode="Markdown",
        reply_markup=MAIN_KB
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat.id
    log.info("Button pressed by %s: %s", chat_id, q.data)

    if q.data == "run_now":
        try:
            text = build_report()
            await q.message.reply_text(text, parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            log.exception("run_now failed: %s", e)
            await q.message.reply_text("❌ Failed to fetch/send report. See logs.")
        return

    if q.data == "set_time":
        context.user_data[AWAIT_FLAG] = True
        await q.message.reply_text(ASK_TIME, parse_mode="Markdown")
        return

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user’s reply when we’re awaiting a time string."""
    if not context.user_data.get(AWAIT_FLAG):
        return  # ignore unrelated text

    chat_id = update.effective_chat.id
    user_text = (update.message.text or "").strip()
    parsed = parse_hhmm(user_text)
    if not parsed:
        await update.message.reply_text(
            "Please send time like `08:30` or `7:45` (24-hour).",
            parse_mode="Markdown"
        )
        return

    hour, minute = parsed

    # Save schedule
    schedules = load_schedules()
    schedules[str(chat_id)] = f"{hour:02d}:{minute:02d}"
    save_schedules(schedules)

    # Clear flag
    context.user_data[AWAIT_FLAG] = False

    # (Re)register job
    await register_daily_job(context.application, chat_id, hour, minute)

    # ✅ Confirmation message you asked for:
    await update.message.reply_text(
        f"✅ *Your time is set to {hour:02d}:{minute:02d} now.*\n"
        f"Next send: *{describe_next_run(hour, minute)}*",
        parse_mode="Markdown",
        reply_markup=MAIN_KB
    )

# ---- jobs ----
async def run_daily_job(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.chat_id
    try:
        text = build_report()
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as e:
        log.exception("Job send failed for %s: %s", chat_id, e)

async def register_daily_job(app: Application, chat_id: int, hour: int, minute: int):
    name = f"daily_{chat_id}"
    for j in app.job_queue.get_jobs_by_name(name):
        j.schedule_removal()
    app.job_queue.run_daily(
        run_daily_job,
        time=dtime(hour=hour, minute=minute),
        name=name,
        chat_id=chat_id,
    )
    log.info("Registered daily job for %s at %02d:%02d", chat_id, hour, minute)

async def load_all_jobs(app: Application):
    schedules = load_schedules()
    for chat_id_str, hm in schedules.items():
        try:
            h, m = [int(x) for x in hm.split(":")]
            await register_daily_job(app, int(chat_id_str), h, m)
        except Exception as e:
            log.error("Failed to register job for %s: %s", chat_id_str, e)

# ---- errors ----
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler error: %s", context.error)

def main():
    token = BOT_TOKEN
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN")
    app = Application.builder().token(token).build()
    app.post_init = load_all_jobs

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    print("Bot running. Use /start in Telegram.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
