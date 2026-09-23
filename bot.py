import os
import re
import json
import time
import asyncio

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TIMER_FILE = "timer.json"

# Each group's timer task is stored separately
timer_tasks = {}


# =========================================================
# TIMER STORAGE
# =========================================================

def load_all_timers():
    if not os.path.exists(TIMER_FILE):
        return {}

    try:
        with open(TIMER_FILE, "r") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception as e:
        print("Load timer error:", e)

    return {}


def save_all_timers(data):
    try:
        with open(TIMER_FILE, "w") as f:
            json.dump(data, f)
    except Exception as e:
        print("Save timer error:", e)


def save_group_timer(chat_id, end_time, message_id):
    data = load_all_timers()

    data[str(chat_id)] = {
        "end_time": end_time,
        "message_id": message_id
    }

    save_all_timers(data)


def get_group_timer(chat_id):
    data = load_all_timers()
    return data.get(str(chat_id))


def delete_group_timer(chat_id):
    data = load_all_timers()

    data.pop(str(chat_id), None)

    save_all_timers(data)


# =========================================================
# ADMIN CHECK
# =========================================================

async def is_admin(update: Update):
    user = update.effective_user

    return bool(
        user and
        user.id == ADMIN_ID
    )


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
        f"🔵 <b>{days}𝗗</b>   "
        f"🟣 <b>{hours:02d}𝗛</b>\n"
        f"🟢 <b>{minutes:02d}𝗠</b>   "
        f"🔴 <b>{seconds:02d}𝗦</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "     🎯 <b>𝗡𝗘𝗘𝗧 𝟮𝟬𝟮𝟳 𝗧𝗜𝗠𝗘𝗥 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪𝗡</b>\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )


# =========================================================
# PARSE DURATION
# =========================================================

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

    if total <= 0:
        return None

    return total


# =========================================================
# CANCEL GROUP TASK
# =========================================================

async def cancel_group_task(chat_id):

    task = timer_tasks.get(chat_id)

    if task:

        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print("Cancel task error:", e)

        timer_tasks.pop(chat_id, None)


# =========================================================
# TIMER LOOP
# =========================================================

async def timer_loop(
    application,
    chat_id,
    message_id,
    end_time
):

    last_text = ""

    try:

        while True:

            remaining = int(
                end_time - time.time()
            )

            # =========================
            # TIME'S UP
            # =========================

            if remaining <= 0:

                try:

                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=(
                            "━━━━━━━━━━━━━━━━━━━━\n"
                            "       🚀 <b>𝗧𝗜𝗠𝗘'𝗦 𝗨𝗣!</b>\n"
                            "━━━━━━━━━━━━━━━━━━━━"
                        ),
                        parse_mode=ParseMode.HTML
                    )

                except Exception as e:
                    print(
                        f"Finish error [{chat_id}]:",
                        e
                    )

                delete_group_timer(chat_id)

                break

            # =========================
            # UPDATE TIMER
            # =========================

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

                    print(
                        f"Update error [{chat_id}]:",
                        e
                    )

            await asyncio.sleep(1)

    except asyncio.CancelledError:

        raise

    finally:

        current_task = asyncio.current_task()

        if timer_tasks.get(chat_id) is current_task:

            timer_tasks.pop(
                chat_id,
                None
            )


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = (
        "🎯 <b>𝗦𝗨𝗠𝗜𝗧'𝗦 𝗡𝗘𝗘𝗧 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪𝗡</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "⏳ <b>𝗡𝗘𝗘𝗧 𝟮𝟬𝟮𝟳 𝗟𝗜𝗩𝗘 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪𝗡</b>\n\n"

        "📌 <code>/timer 220d</code>\n"
        "📌 <code>/timer 220d 5h 30m</code>\n"
        "⏹ <code>/stoptimer</code>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n\n"

        "👥 <b>𝗪𝗮𝗻𝘁 𝘁𝗵𝗶𝘀 𝗶𝗻 𝘆𝗼𝘂𝗿 𝗴𝗿𝗼𝘂𝗽?</b>\n"
        "📩 <b>𝗖𝗼𝗻𝘁𝗮𝗰𝘁:</b> <b>@SumitTripathi</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔴 𝗔𝗗𝗗 𝗠𝗘 𝗧𝗢 𝗚𝗥𝗢𝗨𝗣",
                    url=(
                        "https://t.me/"
                        "NEET_TIMERS_BOT"
                        "?startgroup=true"
                    )
                )
            ]
        ]
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


# =========================================================
# TIMER COMMAND
# =========================================================

async def timer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    # Only owner/admin can create timers
    if not await is_admin(update):
        return

    # =========================
    # CHECK ARGUMENT
    # =========================

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

    duration = parse_duration(
        " ".join(context.args)
    )

    if not duration:

        await update.message.reply_text(
            "❌ <b>Invalid duration!</b>\n\n"
            "Use:\n"
            "<code>/timer 220d 5h 30m</code>",
            parse_mode=ParseMode.HTML
        )

        return

    # =========================
    # STOP OLD TIMER
    # ONLY FOR THIS GROUP
    # =========================

    old_timer = get_group_timer(chat_id)

    await cancel_group_task(chat_id)

    if old_timer:

        try:

            await context.bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=old_timer["message_id"]
            )

        except Exception as e:

            print(
                f"Old unpin error [{chat_id}]:",
                e
            )

    delete_group_timer(chat_id)

    # =========================
    # CREATE NEW TIMER
    # =========================

    end_time = time.time() + duration

    message = await update.message.reply_text(
        format_time(duration),
        parse_mode=ParseMode.HTML
    )

    # =========================
    # PIN TIMER
    # =========================

    try:

        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True
        )

    except Exception as e:

        print(
            f"Pin error [{chat_id}]:",
            e
        )

    # =========================
    # SAVE THIS GROUP TIMER
    # =========================

    save_group_timer(
        chat_id,
        end_time,
        message.message_id
    )

    # =========================
    # START THIS GROUP TASK
    # =========================

    task = asyncio.create_task(
        timer_loop(
            context.application,
            chat_id,
            message.message_id,
            end_time
        )
    )

    timer_tasks[chat_id] = task


# =========================================================
# STOP TIMER
# =========================================================

async def stop_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat_id = update.effective_chat.id

    if not await is_admin(update):
        return

    # =========================
    # CANCEL ONLY THIS GROUP
    # =========================

    await cancel_group_task(chat_id)

    timer_data = get_group_timer(chat_id)

    # =========================
    # UNPIN TIMER
    # =========================

    if timer_data:

        try:

            await context.bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=timer_data["message_id"]
            )

        except Exception as e:

            print(
                f"Unpin error [{chat_id}]:",
                e
            )

    delete_group_timer(chat_id)

    await update.message.reply_text(
        "⏹ <b>Timer stopped.</b>",
        parse_mode=ParseMode.HTML
    )


# =========================================================
# RESTORE ALL TIMERS AFTER RESTART
# =========================================================

async def restore_timers(application):

    data = load_all_timers()

    if not data:
        return

    now = time.time()

    for chat_id_string, timer_data in list(data.items()):

        try:

            chat_id = int(chat_id_string)

            end_time = float(
                timer_data["end_time"]
            )

            message_id = int(
                timer_data["message_id"]
            )

        except Exception:

            data.pop(
                chat_id_string,
                None
            )

            continue

        # =========================
        # TIMER ALREADY FINISHED
        # =========================

        if end_time <= now:

            try:

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        "━━━━━━━━━━━━━━━━━━━━\n"
                        "       🚀 <b>𝗧𝗜𝗠𝗘'𝗦 𝗨𝗣!</b>\n"
                        "━━━━━━━━━━━━━━━━━━━━"
                    ),
                    parse_mode=ParseMode.HTML
                )

            except Exception:
                pass

            data.pop(
                chat_id_string,
                None
            )

            continue

        # =========================
        # RESTORE THIS GROUP
        # =========================

        task = asyncio.create_task(
            timer_loop(
                application,
                chat_id,
                message_id,
                end_time
            )
        )

        timer_tasks[chat_id] = task

    save_all_timers(data)


# =========================================================
# POST INIT
# =========================================================

async def post_init(application):

    print("🔄 Restoring group timers...")

    await restore_timers(application)

    print("✅ Group timers restored.")


# =========================================================
# MAIN
# =========================================================

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

    # Commands
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "timer",
            timer_command
        )
    )

    application.add_handler(
        CommandHandler(
            "stoptimer",
            stop_timer
        )
    )

    print(
        "🟢 NEET TIMER BOT STARTED"
    )

    application.run_polling()


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
