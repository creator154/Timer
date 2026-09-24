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
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

TIMER_FILE = "timer.json"

# chat_id -> asyncio.Task
timer_tasks = {}


# =========================================================
# CHECK TOKEN
# =========================================================

if not TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is missing.")


# =========================================================
# TIMER STORAGE
# =========================================================

def load_all_timers():
    if not os.path.exists(TIMER_FILE):
        return {}

    try:
        with open(TIMER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            return data

    except Exception:
        pass

    return {}


def save_all_timers(data):
    try:
        with open(TIMER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Timer save error: {e}")


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

    if str(chat_id) in data:
        del data[str(chat_id)]

    save_all_timers(data)


# =========================================================
# OWNER CHECK
# =========================================================

async def is_owner(update: Update):
    user = update.effective_user

    if not user:
        return False

    return user.id == ADMIN_ID


# =========================================================
# BOT ADMIN CHECK
# =========================================================

async def bot_is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        print(f"Admin check error: {e}")
        return False


# =========================================================
# FORMAT TIMER
# =========================================================

def format_time(total_seconds):

    total_seconds = max(0, int(total_seconds))

    days = total_seconds // 86400
    total_seconds %= 86400

    hours = total_seconds // 3600
    total_seconds %= 3600

    minutes = total_seconds // 60
    seconds = total_seconds % 60

    return (
        f"🔵 <b>{days}𝗗</b>   🟣 <b>{hours:02d}𝗛</b>\n"
        f"🟢 <b>{minutes:02d}𝗠</b>   🔴 <b>{seconds:02d}𝗦</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"     🎯 <b>𝗡𝗘𝗘𝗧 𝟮𝟬𝟮𝟳 𝗧𝗜𝗠𝗘𝗥 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪𝗡</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )


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

    if not text:
        return None

    pattern = r"(\d+)\s*(d|h|m|s)"

    matches = re.findall(
        pattern,
        text.lower()
    )

    if not matches:
        return None

    total = 0

    for value, unit in matches:

        value = int(value)

        if unit == "d":
            total += value * 86400

        elif unit == "h":
            total += value * 3600

        elif unit == "m":
            total += value * 60

        elif unit == "s":
            total += value

    if total <= 0:
        return None

    return total


# =========================================================
# CANCEL EXISTING TASK
# =========================================================

async def cancel_chat_task(chat_id):

    task = timer_tasks.get(chat_id)

    if task:

        if not task.done():

            task.cancel()

            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    timer_tasks.pop(chat_id, None)


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

        while True:

            remaining = int(end_time - time.time())

            if remaining <= 0:
                break

            try:

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=format_time(remaining),
                    parse_mode=ParseMode.HTML,
                )

            except Exception as e:

                error_text = str(e).lower()

                # Message was deleted / chat unavailable
                if (
                    "message to edit not found" in error_text
                    or "message can't be edited" in error_text
                    or "chat not found" in error_text
                ):
                    break

                print(
                    f"Timer edit error [{chat_id}]: {e}"
                )

            await asyncio.sleep(1)

        # TIME UP
        try:

            await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=time_up_text(),
                parse_mode=ParseMode.HTML,
            )

        except Exception as e:
            print(
                f"Time-up edit error [{chat_id}]: {e}"
            )

        delete_chat_timer(chat_id)

    except asyncio.CancelledError:
        raise

    except Exception as e:

        print(
            f"Timer loop error [{chat_id}]: {e}"
        )

    finally:

        timer_tasks.pop(chat_id, None)


# =========================================================
# START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = (
        "🎯 <b>𝗦𝗨𝗠𝗜𝗧'𝗦 𝗡𝗘𝗘𝗧 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ <b>𝗡𝗘𝗘𝗧 𝟮𝟬𝟮𝟳 𝗟𝗜𝗩𝗘 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪𝗡</b>\n\n"
        "📌 <code>/timer 220d</code>\n"
        "📌 <code>/timer 220d 5h 30m</code>\n"
        "⏹ <code>/stoptimer</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "👥 <b>𝗪𝗮𝗻𝘁 𝘁𝗵𝗶𝘀 𝗶𝗻 𝘆𝗼𝘂𝗿 𝗴𝗿𝗼𝘂𝗽?</b>\n"
        "📩 <b>𝗖𝗼𝗻𝘁𝗮𝗰𝘁:</b> "
        "<b>@SumitTripathi</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔴 𝗔𝗗𝗗 𝗠𝗘 𝗧𝗢 𝗚𝗥𝗢𝗨𝗣",
                url="https://t.me/NEET_TIMERS_BOT?startgroup=true"
            )
        ]
    ])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# =========================================================
# CREATE TIMER
# =========================================================

async def create_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
):

    duration_text = " ".join(context.args)

    duration = parse_duration(duration_text)

    if not duration:

        target = (
            update.message
            or update.channel_post
        )

        if target:

            await target.reply_text(
                "❌ <b>Invalid timer.</b>\n\n"
                "Example:\n"
                "<code>/timer 220d</code>\n"
                "<code>/timer 220d 5h 30m</code>\n"
                "<code>/timer 2h 30m</code>",
                parse_mode=ParseMode.HTML,
            )

        return

    # Cancel previous timer
    await cancel_chat_task(chat_id)

    # Delete old saved timer
    delete_chat_timer(chat_id)

    end_time = time.time() + duration

    # Send timer
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=format_time(duration),
        parse_mode=ParseMode.HTML,
    )

    # Pin timer
    try:

        await context.bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message.message_id,
            disable_notification=True,
        )

    except Exception as e:

        print(
            f"Pin error [{chat_id}]: {e}"
        )

    # Save timer
    save_chat_timer(
        chat_id,
        end_time,
        message.message_id,
    )

    # Start timer task
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
# NORMAL TIMER COMMAND
# PRIVATE / GROUP / SUPERGROUP
# =========================================================

async def timer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat = update.effective_chat

    if not chat:
        return

    # PRIVATE
    if chat.type == ChatType.PRIVATE:

        if not await is_owner(update):

            await update.message.reply_text(
                "❌ <b>Only the bot owner can use this command.</b>",
                parse_mode=ParseMode.HTML,
            )

            return

    # GROUP / SUPERGROUP
    elif chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        if not await is_owner(update):

            await update.message.reply_text(
                "❌ <b>Only the bot owner can start the timer.</b>",
                parse_mode=ParseMode.HTML,
            )

            return

        if not await bot_is_admin(
            update,
            context
        ):

            await update.message.reply_text(
                "❌ <b>Please make me an admin first.</b>",
                parse_mode=ParseMode.HTML,
            )

            return

    else:
        return

    await create_timer(
        update,
        context,
        chat.id,
    )


# =========================================================
# CHANNEL TIMER COMMAND
# =========================================================

async def channel_timer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    post = update.channel_post

    if not post:
        return

    chat = update.effective_chat

    if not chat:
        return

    # Bot must be admin in channel
    if not await bot_is_admin(
        update,
        context
    ):

        print(
            f"Bot is not admin in channel {chat.id}"
        )

        return

    await create_timer(
        update,
        context,
        chat.id,
    )


# =========================================================
# STOP TIMER
# =========================================================

async def stop_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    chat = update.effective_chat

    if not chat:
        return

    # CHANNEL
    if update.channel_post:

        if not await bot_is_admin(
            update,
            context
        ):
            return

    # PRIVATE / GROUP
    else:

        if not await is_owner(update):

            target = update.message

            if target:

                await target.reply_text(
                    "❌ <b>Only the bot owner can stop the timer.</b>",
                    parse_mode=ParseMode.HTML,
                )

            return

        if chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        ):

            if not await bot_is_admin(
                update,
                context
            ):

                await update.message.reply_text(
                    "❌ <b>Please make me an admin first.</b>",
                    parse_mode=ParseMode.HTML,
                )

                return

    timer = get_chat_timer(chat.id)

    if not timer:

        target = (
            update.message
            or update.channel_post
        )

        if target:

            await target.reply_text(
                "ℹ️ <b>No active timer found.</b>",
                parse_mode=ParseMode.HTML,
            )

        return

    message_id = timer.get("message_id")

    # Cancel task
    await cancel_chat_task(chat.id)

    # Delete storage
    delete_chat_timer(chat.id)

    # Try unpin
    if message_id:

        try:

            await context.bot.unpin_chat_message(
                chat_id=chat.id,
                message_id=message_id,
            )

        except Exception as e:

            print(
                f"Unpin error [{chat.id}]: {e}"
            )

    target = (
        update.message
        or update.channel_post
    )

    if target:

        try:

            await target.reply_text(
                "⏹ <b>Timer stopped.</b>",
                parse_mode=ParseMode.HTML,
            )

        except Exception as e:

            print(
                f"Stop message error [{chat.id}]: {e}"
            )


# =========================================================
# CHANNEL COMMAND ROUTER
# =========================================================

async def channel_command_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    post = update.channel_post

    if not post:
        return

    text = post.text or post.caption or ""

    if not text:
        return

    # First word = command
    command = (
        text.split()[0]
        .split("@")[0]
        .lower()
    )

    if command == "/timer":

        await channel_timer_command(
            update,
            context
        )

    elif command == "/stoptimer":

        await stop_timer(
            update,
            context
        )


# =========================================================
# RESTORE TIMERS AFTER RESTART
# =========================================================

async def restore_timers(
    application: Application
):

    data = load_all_timers()

    if not data:
        return

    print(
        f"Restoring {len(data)} timer(s)..."
    )

    for chat_id_str, timer in list(
        data.items()
    ):

        try:

            chat_id = int(chat_id_str)

            end_time = float(
                timer["end_time"]
            )

            message_id = int(
                timer["message_id"]
            )

            remaining = int(
                end_time - time.time()
            )

            if remaining <= 0:

                delete_chat_timer(
                    chat_id
                )

                continue

            # Check message still exists
            try:

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=format_time(remaining),
                    parse_mode=ParseMode.HTML,
                )

            except Exception as e:

                print(
                    f"Could not restore "
                    f"timer {chat_id}: {e}"
                )

                delete_chat_timer(
                    chat_id
                )

                continue

            task = asyncio.create_task(
                timer_loop(
                    application,
                    chat_id,
                    message_id,
                    end_time,
                )
            )

            timer_tasks[chat_id] = task

            print(
                f"Timer restored: {chat_id}"
            )

        except Exception as e:

            print(
                f"Restore error: {e}"
            )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application
):

    await restore_timers(
        application
    )


# =========================================================
# MAIN
# =========================================================

def main():

    application = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    # -----------------------------------------
    # PRIVATE / GROUP / SUPERGROUP
    # -----------------------------------------

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

    # -----------------------------------------
    # CHANNEL
    # -----------------------------------------

    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POSTS
            & filters.COMMAND,
            channel_command_handler
        )
    )

    print(
        "🎯 NEET Timer Bot Started..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
