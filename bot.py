import os
import re
import json
import asyncio
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TIMER_FILE = "timer.json"

timer_task = None


# =========================
# TIMER STORAGE
# =========================

def save_timer(end_time, chat_id, message_id):
    data = {
        "end_time": end_time,
        "chat_id": chat_id,
        "message_id": message_id
    }

    with open(TIMER_FILE, "w") as f:
        json.dump(data, f)


def load_timer():
    if not os.path.exists(TIMER_FILE):
        return None

    try:
        with open(TIMER_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return None


def delete_timer():
    if os.path.exists(TIMER_FILE):
        os.remove(TIMER_FILE)


# =========================
# FORMAT TIME
# =========================

def format_time(seconds):
    seconds = max(0, int(seconds))

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60
    seconds %= 60

    return f"⏳ {days}D {hours:02d}H {minutes:02d}M {seconds:02d}S"


# =========================
# PARSE DURATION
# =========================

def parse_duration(text):
    pattern = r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?"

    match = re.fullmatch(pattern, text.lower().strip())

    if not match:
        return None

    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = int(match.group(4) or 0)

    total = (
        days * 86400
        + hours * 3600
        + minutes * 60
        + seconds
    )

    return total if total > 0 else None


# =========================
# ADMIN CHECK
# =========================

async def is_admin(update: Update):
    user = update.effective_user

    if not user:
        return False

    return user.id == ADMIN_ID


# =========================
# TIMER LOOP
# =========================

async def timer_loop(app, chat_id, message_id, end_time):

    last_text = ""

    while True:
        remaining = int(end_time - time.time())

        if remaining <= 0:
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text="🚀 TIME'S UP!"
                )
            except Exception:
                pass

            delete_timer()
            break

        text = format_time(remaining)

        if text != last_text:
            try:
                await app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text
                )

                last_text = text

            except Exception as e:
                print("Timer update error:", e)

        await asyncio.sleep(1)


# =========================
# /TIMER
# =========================

async def timer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global timer_task

    if not await is_admin(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Use:\n\n"
            "/timer 200d 25h 25m\n\n"
            "Example:\n"
            "/timer 10d 5h 30m"
        )
        return

    duration_text = " ".join(context.args)

    duration = parse_duration(duration_text)

    if not duration:
        await update.message.reply_text(
            "❌ Invalid timer.\n\n"
            "Example:\n"
            "/timer 200d 25h 25m"
        )
        return

    # Stop old timer
    if timer_task:
        timer_task.cancel()
        timer_task = None

    end_time = time.time() + duration

    # Send tiny timer message
    msg = await update.message.reply_text(
        format_time(duration)
    )

    # Pin message
    try:
        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            disable_notification=True
        )
    except Exception as e:
        print("Pin error:", e)

    save_timer(
        end_time,
        update.effective_chat.id,
        msg.message_id
    )

    timer_task = asyncio.create_task(
        timer_loop(
            context.application,
            update.effective_chat.id,
            msg.message_id,
            end_time
        )
    )


# =========================
# /STOP
# =========================

async def stop_timer(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global timer_task

    if not await is_admin(update):
        return

    if timer_task:
        timer_task.cancel()
        timer_task = None

    delete_timer()

    await update.message.reply_text(
        "⏹ Timer stopped."
    )


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "⏱ Live Timer Bot\n\n"
        "Admin command:\n"
        "/timer 200d 25h 25m\n"
        "/stoptimer"
    )


# =========================
# RESTORE TIMER AFTER RESTART
# =========================

async def restore_timer(app):

    global timer_task

    data = load_timer()

    if not data:
        return

    end_time = data["end_time"]
    chat_id = data["chat_id"]
    message_id = data["message_id"]

    if end_time <= time.time():
        delete_timer()

        try:
            await app.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="🚀 TIME'S UP!"
            )
        except Exception:
            pass

        return

    timer_task = asyncio.create_task(
        timer_loop(
            app,
            chat_id,
            message_id,
            end_time
        )
    )


# =========================
# MAIN
# =========================

async def post_init(application):
    await restore_timer(application)


def main():

    if not TOKEN:
        raise RuntimeError("BOT_TOKEN missing")

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("timer", timer_command))
    app.add_handler(CommandHandler("stoptimer", stop_timer))

    print("Live Timer Bot Started")

    app.run_polling()


if __name__ == "__main__":
    main()
