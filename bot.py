import os
import re
import json
import time
import asyncio

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import (
    ParseMode,
    ChatType,
)
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

# One task per chat/channel
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

        return data if isinstance(data, dict) else {}

    except Exception as e:
        print("Load timer error:", e)
        return {}


def save_all_timers(data):
    try:
        with open(TIMER_FILE, "w") as f:
            json.dump(data, f)

    except Exception as e:
        print("Save timer error:", e)


def save_chat_timer(chat_id, end_time, message_id):
    data = load_all_timers()

    data[str(chat_id)] = {
        "end_time": end_time,
        "message_id": message_id,
    }

    save_all_timers(data)


def get_chat_timer(chat_id):
    data = load_all_timers()
    return data.get(str(chat_id))


def delete_chat_timer(chat_id):
    data = load_all_timers()

    data.pop(str(chat_id), None)

    save_all_timers(data)


# =========================================================
# ADMIN CHECK
# =========================================================

async def is_owner(update: Update):
    """
    Normal group/private command:
    checks ADMIN_ID.
    """

    user = update.effective_user

    return bool(
        user and
        user.id == ADMIN_ID
    )


async def bot_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Checks whether the bot itself is admin in the current chat/channel.
    """

    chat = update.effective_chat

    if not chat:
        return False

    try:
        member = await context.bot.get_chat_member(
            chat_id=chat.id,
            user_id=context.bot.id,
        )

        return member.status in (
            "administrator",
            "creator",
        )

    except Exception as e:

        print(
            f"Bot admin check error [{chat.id}]:",
            e
        )

        return False


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
# TIME'S UP
# =========================================================

def time_up_text():

    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "       🚀 <b>𝗧𝗜𝗠𝗘'𝗦 𝗨𝗣!</b>\n"
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

    return total if total > 0 else None


# =========================================================
# CANCEL ONE CHAT'S TIMER
# =========================================================

async def cancel_chat_task(chat_id):

    task = timer_tasks.get(chat_id)

    if not task:
        return

    task.cancel()

    try:
        await task

    except asyncio.CancelledError:
        pass

    except Exception as e:
        print(
            f"Task cancel error [{chat_id}]:",
            e
        )

    timer_tasks.pop(
        chat_id,
        None
    )


# =========================================================
# TIMER LOOP
# =========================================================

async def timer_loop(
    application,
    chat_id,
    message_id,
    end_time,
):

    try:

        last_text = ""

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
                        text=time_up_text(),
                        parse_mode=ParseMode.HTML,
                    )

                except Exception as e:

                    print(
                        f"Time-up edit error [{chat_id}]:",
                        e
                    )

                delete_chat_timer(chat_id)

                break

            # =========================
            # UPDATE
            # =========================

            text = format_time(remaining)

            if text != last_text:

                try:

                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                    )

                    last_text = text

                except Exception as e:

                    print(
                        f"Timer update error [{chat_id}]:",
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
    context: ContextTypes.DEFAULT_TYPE,
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
                    ),
                )
            ]
        ]
    )

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# =========================================================
# TIMER COMMAND
# =========================================================

async def timer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if not chat:
        return

    chat_id = chat.id

    # =====================================================
    # PRIVATE CHAT
    # =====================================================

    if chat.type == ChatType.PRIVATE:

        if not await is_owner(update):
            return

    # =====================================================
    # GROUP / SUPERGROUP
    # =====================================================

    elif chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        if not await is_owner(update):
            return

        if not await bot_is_admin(
            update,
            context
        ):

            await update.effective_message.reply_text(
                "❌ <b>Bot must be an admin in this group.</b>",
                parse_mode=ParseMode.HTML,
            )

            return

    # =====================================================
    # CHANNEL
    # =====================================================

    elif chat.type == ChatType.CHANNEL:

        # Channel posts are handled separately,
        # but this keeps the function safe.
        if not await bot_is_admin(
            update,
            context
        ):
            return

    else:

        return

    # =====================================================
    # ARGUMENT CHECK
    # =====================================================

    if not context.args:

        await update.effective_message.reply_text(
            "❌ <b>Duration missing!</b>\n\n"
            "Example:\n"
            "<code>/timer 220d</code>\n\n"
            "Or:\n"
            "<code>/timer 220d 5h 30m</code>",
            parse_mode=ParseMode.HTML,
        )

        return

    duration = parse_duration(
        " ".join(context.args)
    )

    if not duration:

        await update.effective_message.reply_text(
            "❌ <b>Invalid duration!</b>\n\n"
            "Use:\n"
            "<code>/timer 220d 5h 30m</code>",
            parse_mode=ParseMode.HTML,
        )

        return

    # =====================================================
    # STOP OLD TIMER OF THIS CHAT ONLY
    # =====================================================

    await cancel_chat_task(chat_id)

    old_timer = get_chat_timer(chat_id)

    if old_timer:

        try:

            await context.bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=old_timer["message_id"],
            )

        except Exception as e:

            print(
                f"Old unpin error [{chat_id}]:",
                e
            )

    delete_chat_timer(chat_id)

    # =====================================================
    # CREATE TIMER
    # =====================================================

    end_time = time.time() + duration

    try:

        message = await context.bot.send_message(
            chat_id=chat_id,
            text=format_time(duration),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:

        print(
            f"Send timer error [{chat_id}]:",
            e
        )

        return

    # =====================================================
    # PIN TIMER
    # =====================================================

    try:

        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True,
        )

    except Exception as e:

        print(
            f"Pin error [{chat_id}]:",
            e
        )

    # =====================================================
    # SAVE
    # =====================================================

    save_chat_timer(
        chat_id,
        end_time,
        message.message_id,
    )

    # =====================================================
    # START INDEPENDENT TASK
    # =====================================================

    task = asyncio.create_task(
        timer_loop(
            context.application,
            chat_id,
            message.message_id,
            end_time,
        )
    )

    timer_tasks[chat_id] = task


# =========================================================
# CHANNEL TIMER
# =========================================================

async def channel_timer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    # channel_post exists here
    post = update.channel_post

    if not post:
        return

    chat = post.chat

    chat_id = chat.id

    # Bot must be admin
    if not await bot_is_admin(
        update,
        context
    ):
        return

    # =====================================================
    # ARGUMENT CHECK
    # =====================================================

    if not context.args:

        try:

            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "❌ <b>Duration missing!</b>\n\n"
                    "Example:\n"
                    "<code>/timer 220d</code>\n\n"
                    "Or:\n"
                    "<code>/timer 220d 5h 30m</code>"
                ),
                parse_mode=ParseMode.HTML,
            )

        except Exception as e:

            print(
                f"Channel help error [{chat_id}]:",
                e
            )

        return

    duration = parse_duration(
        " ".join(context.args)
    )

    if not duration:
        return

    # =====================================================
    # STOP OLD CHANNEL TIMER
    # =====================================================

    await cancel_chat_task(chat_id)

    old_timer = get_chat_timer(chat_id)

    if old_timer:

        try:

            await context.bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=old_timer["message_id"],
            )

        except Exception as e:

            print(
                f"Channel old unpin error [{chat_id}]:",
                e
            )

    delete_chat_timer(chat_id)

    # =====================================================
    # CREATE CHANNEL TIMER
    # =====================================================

    end_time = time.time() + duration

    try:

        message = await context.bot.send_message(
            chat_id=chat_id,
            text=format_time(duration),
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:

        print(
            f"Channel timer send error [{chat_id}]:",
            e
        )

        return

    # =====================================================
    # PIN
    # =====================================================

    try:

        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True,
        )

    except Exception as e:

        print(
            f"Channel pin error [{chat_id}]:",
            e
        )

    # =====================================================
    # SAVE
    # =====================================================

    save_chat_timer(
        chat_id,
        end_time,
        message.message_id,
    )

    # =====================================================
    # START TASK
    # =====================================================

    task = asyncio.create_task(
        timer_loop(
            context.application,
            chat_id,
            message.message_id,
            end_time,
        )
    )

    timer_tasks[chat_id] = task


# =========================================================
# STOP TIMER
# =========================================================

async def stop_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    chat = update.effective_chat

    if not chat:
        return

    chat_id = chat.id

    # Owner check for groups/private
    if chat.type != ChatType.CHANNEL:

        if not await is_owner(update):
            return

    # Channel:
    # command comes as channel_post
    # so bot admin status is enough
    else:

        if not await bot_is_admin(
            update,
            context
        ):
            return

    # =====================================================
    # CANCEL ONLY THIS CHAT
    # =====================================================

    await cancel_chat_task(chat_id)

    timer_data = get_chat_timer(chat_id)

    if timer_data:

        try:

            await context.bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=timer_data["message_id"],
            )

        except Exception as e:

            print(
                f"Unpin error [{chat_id}]:",
                e
            )

    delete_chat_timer(chat_id)

    # =====================================================
    # RESPONSE
    # =====================================================

    try:

        await context.bot.send_message(
            chat_id=chat_id,
            text="⏹ <b>Timer stopped.</b>",
            parse_mode=ParseMode.HTML,
        )

    except Exception as e:

        print(
            f"Stop response error [{chat_id}]:",
            e
        )


# =========================================================
# RESTORE ALL TIMERS
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

        # =================================================
        # FINISHED
        # =================================================

        if end_time <= now:

            try:

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=time_up_text(),
                    parse_mode=ParseMode.HTML,
                )

            except Exception:
                pass

            data.pop(
                chat_id_string,
                None
            )

            continue

        # =================================================
        # RESTORE
        # =================================================

        task = asyncio.create_task(
            timer_loop(
                application,
                chat_id,
                message_id,
                end_time,
            )
        )

        timer_tasks[chat_id] = task

    save_all_timers(data)


# =========================================================
# POST INIT
# =========================================================

async def post_init(application):

    print("🔄 Restoring timers...")

    await restore_timers(
        application
    )

    print("✅ Timers restored.")


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

    # =====================================================
    # PRIVATE / GROUP COMMANDS
    # =====================================================

    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "timer",
            timer_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "stoptimer",
            stop_timer,
        )
    )

    # =====================================================
    # CHANNEL COMMANDS
    # =====================================================

    application.add_handler(
        CommandHandler(
            "timer",
            channel_timer_command,
            channel_post=True,
        )
    )

    application.add_handler(
        CommandHandler(
            "stoptimer",
            stop_timer,
            channel_post=True,
        )
    )

    print(
        "🟢 NEET TIMER BOT STARTED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
