import os
import re
import json
import time
import asyncio

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TIMER_FILE = "timer.json"

timer_task = None


# =========================================================
# TIMER STORAGE
# =========================================================

def save_timer(end_time, chat_id, message_id):
    data = {
        "end_time": end_time,
        "chat_id": chat_id,
        "message_id": message_id,
    }

    with open(TIMER_FILE, "w") as file:
        json.dump(data, file)


def load_timer():
    if not os.path.exists(TIMER_FILE):
        return None

    try:
        with open(TIMER_FILE, "r") as file:
            return json.load(file)
    except Exception:
        return None


def delete_timer():
    if os.path.exists(TIMER_FILE):
        try:
            os.remove(TIMER_FILE)
        except Exception:
            pass


# =========================================================
# ADMIN CHECK
# =========================================================

async def is_admin(update: Update):
    user = update.effective_user

    if not user:
        return False

    return user.id == ADMIN_ID


# =========================================================
# FORMAT TIMER
# =========================================================

def format_time(seconds):

    seconds = max(0, int(seconds))

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60
    seconds %= 60

    return (
        "⏳ <b>𝗟𝗜𝗩𝗘 𝗧𝗜𝗠𝗘𝗥</b>\n\n"
        f"🟦 <b>{days}D</b>   "
        f"🟪 <b>{hours:02d}H</b>   "
        f"🟩 <b>{minutes:02d}M</b>   "
        f"🟥 <b>{seconds:02d}S</b>"
    )


# =========================================================
# PARSE DURATION
# =========================================================

def parse_duration(text):

    text = text.lower().strip()

    pattern = (
        r"^\s*"
        r"(?:(\d+)\s*d)?\s*"
        r"(?:(\d+)\s*h)?\s*"
        r"(?:(\d+)\s*m)?\s*"
        r"(?:(\d+)\s*s)?\s*$"
    )

    match = re.fullmatch(pattern, text)

    if not match:
        return None

    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = int(match.group(4) or 0)

    total_seconds = (
        days * 86400
        + hours * 3600
        + minutes * 60
        + seconds
    )

    if total_seconds <= 0:
        return None

    return total_seconds


# =========================================================
# TIMER LOOP
# =========================================================

async def timer_loop(
    application,
    chat_id,
    message_id,
    end_time
):

    global timer_task

    last_text = ""

    try:

        while True:

            remaining = int(end_time - time.time())

            # Timer finished
            if remaining <= 0:

                try:
                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text="🚀 <b>𝗧𝗜𝗠𝗘'𝗦 𝗨𝗣!</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception as error:
                    print("Finish message error:", error)

                delete_timer()

                break

            # Current timer text
            text = format_time(remaining)

            # Only edit when text changes
            if text != last_text:

                try:

                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                    )

                    last_text = text

                except Exception as error:

                    print("Timer update error:", error)

                    # If message was deleted,
                    # stop the timer instead of looping forever.
                    if "Message to edit not found" in str(error):
                        delete_timer()
                        break

            await asyncio.sleep(1)

    except asyncio.CancelledError:

        print("Timer task cancelled.")

        raise

    finally:

        if timer_task is asyncio.current_task():
            timer_task = None


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "⏱ <b>𝗦𝗨𝗠𝗜𝗧 𝗧𝗜𝗠𝗘𝗥 𝗭𝗢𝗡𝗘</b>\n\n"
        "Live countdown timer bot.\n\n"
        "<b>Admin Commands:</b>\n"
        "/timer 200d 25h 25m\n"
        "/stoptimer",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# /TIMER
# =========================================================

async def timer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global timer_task

    # Admin only
    if not await is_admin(update):
        return

    # Check command
    if not context.args:

        await update.message.reply_text(
            "❌ <b>Timer duration missing.</b>\n\n"
            "Use:\n"
            "<code>/timer 200d 25h 25m</code>\n\n"
            "Examples:\n"
            "<code>/timer 1d 2h 30m</code>\n"
            "<code>/timer 5h 20m</code>\n"
            "<code>/timer 30m</code>\n"
            "<code>/timer 45s</code>",
            parse_mode=ParseMode.HTML,
        )

        return

    # Join all arguments
    duration_text = " ".join(context.args)

    # Convert duration
    duration = parse_duration(duration_text)

    if not duration:

        await update.message.reply_text(
            "❌ <b>Invalid timer format.</b>\n\n"
            "Example:\n"
            "<code>/timer 200d 25h 25m</code>",
            parse_mode=ParseMode.HTML,
        )

        return

    # Cancel previous timer
    if timer_task:

        timer_task.cancel()

        try:
            await timer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        timer_task = None

    # Remove old saved timer
    delete_timer()

    # Calculate end time
    end_time = time.time() + duration

    # Send timer message
    message = await update.message.reply_text(
        format_time(duration),
        parse_mode=ParseMode.HTML,
    )

    # Pin timer message
    try:

        await context.bot.pin_chat_message(
            chat_id=update.effective_chat.id,
            message_id=message.message_id,
            disable_notification=True,
        )

    except Exception as error:

        print("Pin error:", error)

        await update.message.reply_text(
            "⚠️ Timer created, but I couldn't pin the message.\n"
            "Please give the bot permission to pin messages.",
        )

    # Save timer
    save_timer(
        end_time=end_time,
        chat_id=update.effective_chat.id,
        message_id=message.message_id,
    )

    # Start live timer
    timer_task = asyncio.create_task(
        timer_loop(
            context.application,
            update.effective_chat.id,
            message.message_id,
            end_time,
        )
    )


# =========================================================
# /STOPTIMER
# =========================================================

async def stop_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global timer_task

    # Admin only
    if not await is_admin(update):
        return

    # Cancel running timer
    if timer_task:

        timer_task.cancel()

        try:
            await timer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        timer_task = None

    # Load old timer
    data = load_timer()

    # Delete stored timer
    delete_timer()

    # Try to remove/unpin old timer
    if data:

        try:

            await context.bot.unpin_chat_message(
                chat_id=data["chat_id"],
                message_id=data["message_id"],
            )

        except Exception as error:

            print("Unpin error:", error)

    await update.message.reply_text(
        "⏹ <b>Timer stopped.</b>",
        parse_mode=ParseMode.HTML,
    )


# =========================================================
# RESTORE TIMER AFTER RESTART
# =========================================================

async def restore_timer(application):

    global timer_task

    data = load_timer()

    if not data:
        return

    try:

        end_time = float(data["end_time"])
        chat_id = int(data["chat_id"])
        message_id = int(data["message_id"])

    except Exception:

        delete_timer()
        return

    # Already finished while bot was offline
    if end_time <= time.time():

        try:

            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text="🚀 <b>𝗧𝗜𝗠𝗘'𝗦 𝗨𝗣!</b>",
                parse_mode=ParseMode.HTML,
            )

        except Exception as error:

            print("Restore finish error:", error)

        delete_timer()

        return

    # Continue existing timer
    timer_task = asyncio.create_task(
        timer_loop(
            application,
            chat_id,
            message_id,
            end_time,
        )
    )

    print("Existing timer restored.")


# =========================================================
# BOT STARTUP
# =========================================================

async def post_init(application):

    await restore_timer(application)


# =========================================================
# MAIN
# =========================================================

def main():

    if not TOKEN:

        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # Commands
    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("timer", timer_command)
    )

    application.add_handler(
        CommandHandler("stoptimer", stop_timer)
    )

    print("━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("   SUMIT TIMER ZONE")
    print("   LIVE TIMER BOT STARTED")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━")

    application.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
