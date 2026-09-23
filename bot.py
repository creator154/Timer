import os
import re
import json
import time
import asyncio

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes


TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TIMER_FILE = "timer.json"

timer_task = None


# =========================
# TIMER FILE
# =========================

def save_timer(end_time, chat_id, message_id):
    with open(TIMER_FILE, "w") as f:
        json.dump({
            "end_time": end_time,
            "chat_id": chat_id,
            "message_id": message_id
        }, f)


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
        try:
            os.remove(TIMER_FILE)
        except Exception:
            pass


# =========================
# ADMIN CHECK
# =========================

def format_time(seconds):

    seconds = max(0, int(seconds))

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60
    seconds %= 60

    return (
        "𝗦𝘂𝗺𝗶𝘁 𝗧𝗿𝗶𝗽𝗮𝘁𝗵𝗶 🇮🇳\n\n"

        f"🔵 {days}𝗗   "
        f"🟣 {hours:02d}𝗛\n"

        f"🟢 {minutes:02d}𝗠   "
        f"🔴 {seconds:02d}𝗦\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "     ⏳ 𝗟𝗜𝗩𝗘 𝗧𝗜𝗠𝗘𝗥\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


# =========================
# PARSE DURATION
# =========================

def parse_duration(text):

    pattern = (
        r"^\s*"
        r"(?:(\d+)\s*d)?\s*"
        r"(?:(\d+)\s*h)?\s*"
        r"(?:(\d+)\s*m)?\s*"
        r"(?:(\d+)\s*s)?\s*$"
    )

    match = re.fullmatch(
        pattern,
        text.lower().strip()
    )

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
# TIMER LOOP
# =========================

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

            remaining = int(
                end_time - time.time()
            )

            # TIMER FINISHED
            if remaining <= 0:

                try:
                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=(
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            "     🚀 <b>𝗧𝗜𝗠𝗘'𝗦 𝗨𝗣!</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━"
                        ),
                        parse_mode=ParseMode.HTML
                    )
                except Exception as e:
                    print("Finish error:", e)

                delete_timer()

                break

            # CURRENT TIMER
            text = format_time(remaining)

            if text != last_text:

                try:

                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode=ParseMode.HTML
                    )

                    last_text = text

                except Exception as e:

                    print("Update error:", e)

            await asyncio.sleep(1)

    except asyncio.CancelledError:

        raise

    finally:

        if timer_task is asyncio.current_task():
            timer_task = None


# =========================
# START
# =========================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(

        "⏳ <b>𝗟𝗜𝗩𝗘 𝗧𝗜𝗠𝗘𝗥 𝗕𝗢𝗧</b>\n\n"

        "Admin Commands:\n\n"

        "<code>/timer 220d</code>\n"
        "<code>/timer 220d 5h 30m</code>\n"
        "<code>/timer 220d 5h 30m 20s</code>\n\n"

        "<code>/stoptimer</code>",

        parse_mode=ParseMode.HTML
    )


# =========================
# TIMER COMMAND
# =========================

async def timer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global timer_task

    if not await is_admin(update):
        return

    # NO ARGUMENT
    if not context.args:

        await update.message.reply_text(

            "❌ <b>Duration missing!</b>\n\n"

            "Example:\n"
            "<code>/timer 220d</code>\n\n"

            "Or:\n"
            "<code>/timer 220d 5h 30m</code>",

            parse_mode=ParseMode.HTML
        )

        return

    duration_text = " ".join(context.args)

    duration = parse_duration(duration_text)

    # INVALID
    if not duration:

        await update.message.reply_text(

            "❌ <b>Invalid duration!</b>\n\n"

            "Example:\n"
            "<code>/timer 220d 5h 30m</code>",

            parse_mode=ParseMode.HTML
        )

        return

    # STOP OLD TIMER
    if timer_task:

        timer_task.cancel()

        try:
            await timer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        timer_task = None

    delete_timer()

    # END TIME
    end_time = time.time() + duration

    # CREATE MESSAGE
    msg = await update.message.reply_text(

        format_time(duration),

        parse_mode=ParseMode.HTML
    )

    # PIN MESSAGE
    try:

        await context.bot.pin_chat_message(

            chat_id=update.effective_chat.id,

            message_id=msg.message_id,

            disable_notification=True
        )

    except Exception as e:

        print("Pin error:", e)

    # SAVE
    save_timer(

        end_time,

        update.effective_chat.id,

        msg.message_id
    )

    # START LOOP
    timer_task = asyncio.create_task(

        timer_loop(

            context.application,

            update.effective_chat.id,

            msg.message_id,

            end_time
        )
    )


# =========================
# STOP TIMER
# =========================

async def stop_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    global timer_task

    if not await is_admin(update):
        return

    # CANCEL LOOP
    if timer_task:

        timer_task.cancel()

        try:
            await timer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        timer_task = None

    # GET SAVED TIMER
    data = load_timer()

    delete_timer()

    # UNPIN
    if data:

        try:

            await context.bot.unpin_chat_message(

                chat_id=data["chat_id"],

                message_id=data["message_id"]
            )

        except Exception as e:

            print("Unpin error:", e)

    await update.message.reply_text(

        "⏹ <b>Timer stopped.</b>",

        parse_mode=ParseMode.HTML
    )


# =========================
# RESTORE AFTER RESTART
# =========================

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

    # ALREADY FINISHED
    if end_time <= time.time():

        try:

            await application.bot.edit_message_text(

                chat_id=chat_id,

                message_id=message_id,

                text=(
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "     🚀 <b>𝗧𝗜𝗠𝗘'𝗦 𝗨𝗣!</b>\n"
                    "━━━━━━━━━━━━━━━━━━━━"
                ),

                parse_mode=ParseMode.HTML
            )

        except Exception:
            pass

        delete_timer()

        return

    # CONTINUE TIMER
    timer_task = asyncio.create_task(

        timer_loop(

            application,

            chat_id,

            message_id,

            end_time
        )
    )


# =========================
# POST INIT
# =========================

async def post_init(application):

    await restore_timer(application)


# =========================
# MAIN
# =========================

def main():

    if not TOKEN:

        raise RuntimeError(
            "BOT_TOKEN is missing"
        )

    application = (

        Application.builder()

        .token(TOKEN)

        .post_init(post_init)

        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("timer", timer_command)
    )

    application.add_handler(
        CommandHandler("stoptimer", stop_timer)
    )

    print("🟢 LIVE TIMER BOT STARTED")

    application.run_polling()


if __name__ == "__main__":

    main()
