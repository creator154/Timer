import os
import re
import time
import asyncio

import psycopg2
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
DATABASE_URL = os.getenv("DATABASE_URL")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not ADMIN_ID:
    raise RuntimeError("ADMIN_ID is missing")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is missing. Add Heroku Postgres first."
    )


# chat_id -> asyncio.Task
timer_tasks = {}


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )


def init_db():

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS timers (
                chat_id BIGINT PRIMARY KEY,
                message_id BIGINT NOT NULL,
                end_time DOUBLE PRECISION NOT NULL
            )
        """)

        conn.commit()

        cur.close()

    finally:

        conn.close()

    print("✅ Database initialized")


def save_timer(
    chat_id,
    message_id,
    end_time
):

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO timers
                (chat_id, message_id, end_time)
            VALUES
                (%s, %s, %s)
            ON CONFLICT (chat_id)
            DO UPDATE SET
                message_id = EXCLUDED.message_id,
                end_time = EXCLUDED.end_time
            """,
            (
                chat_id,
                message_id,
                end_time,
            )
        )

        conn.commit()

        cur.close()

    finally:

        conn.close()


def get_timer(chat_id):

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT message_id, end_time
            FROM timers
            WHERE chat_id = %s
            """,
            (chat_id,)
        )

        row = cur.fetchone()

        cur.close()

        if not row:
            return None

        return {
            "message_id": row[0],
            "end_time": row[1],
        }

    finally:

        conn.close()


def get_all_timers():

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            SELECT chat_id, message_id, end_time
            FROM timers
            """
        )

        rows = cur.fetchall()

        cur.close()

        return rows

    finally:

        conn.close()


def delete_timer(chat_id):

    conn = db_connect()

    try:

        cur = conn.cursor()

        cur.execute(
            """
            DELETE FROM timers
            WHERE chat_id = %s
            """,
            (chat_id,)
        )

        conn.commit()

        cur.close()

    finally:

        conn.close()


# =========================================================
# OWNER
# =========================================================

async def is_owner(update: Update):

    user = update.effective_user

    if not user:
        return False

    return user.id == ADMIN_ID


# =========================================================
# BOT ADMIN
# =========================================================

async def bot_is_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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
            f"Admin check error: {e}"
        )

        return False


# =========================================================
# TIMER FORMAT
# =========================================================

def format_time(seconds):

    seconds = max(
        0,
        int(seconds)
    )

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
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"     🎯 <b>𝗡𝗘𝗘𝗧 𝟮𝟬𝟮𝟳 "
        f"𝗧𝗜𝗠𝗘𝗥 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪𝗡</b>\n"
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

    return total if total > 0 else None


# =========================================================
# CANCEL TASK
# =========================================================

async def cancel_task(chat_id):

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
    end_time
):

    try:

        while True:

            remaining = int(
                end_time - time.time()
            )

            if remaining <= 0:
                break

            try:

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=format_time(
                        remaining
                    ),
                    parse_mode=ParseMode.HTML,
                )

            except Exception as e:

                print(
                    f"Edit error [{chat_id}]: {e}"
                )

                error = str(e).lower()

                if (
                    "message to edit not found"
                    in error
                    or "message can't be edited"
                    in error
                    or "chat not found"
                    in error
                ):
                    break

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
                f"Time-up error [{chat_id}]: {e}"
            )

        delete_timer(chat_id)

    except asyncio.CancelledError:

        raise

    except Exception as e:

        print(
            f"Timer loop error [{chat_id}]: {e}"
        )

    finally:

        timer_tasks.pop(
            chat_id,
            None
        )


# =========================================================
# CREATE TIMER
# =========================================================

async def create_timer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    args
):

    duration_text = " ".join(args)

    duration = parse_duration(
        duration_text
    )

    if not duration:

        target = (
            update.message
            or update.channel_post
        )

        if target:

            await target.reply_text(
                "❌ <b>Invalid timer.</b>\n\n"
                "Examples:\n"
                "<code>/timer 220d</code>\n"
                "<code>/timer 220d 5h 30m</code>\n"
                "<code>/timer 2h 30m</code>",
                parse_mode=ParseMode.HTML,
            )

        return

    # Stop old timer
    await cancel_task(chat_id)

    old_timer = get_timer(chat_id)

    if old_timer:

        try:

            await context.bot.unpin_chat_message(
                chat_id=chat_id,
                message_id=old_timer["message_id"],
            )

        except Exception:
            pass

    delete_timer(chat_id)

    # New end time
    end_time = (
        time.time() + duration
    )

    # Send timer
    message = await context.bot.send_message(
        chat_id=chat_id,
        text=format_time(duration),
        parse_mode=ParseMode.HTML,
    )

    # Pin
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

    # SAVE TO DATABASE
    save_timer(
        chat_id=chat_id,
        message_id=message.message_id,
        end_time=end_time,
    )

    # Start live loop
    task = asyncio.create_task(
        timer_loop(
            context.application,
            chat_id,
            message.message_id,
            end_time,
        )
    )

    timer_tasks[chat_id] = task

    print(
        f"✅ Timer started: {chat_id}"
    )


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    text = (
        "🎯 <b>𝗦𝗨𝗠𝗜𝗧'𝗦 𝗡𝗘𝗘𝗧 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪𝗡</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "⏳ <b>𝗡𝗘𝗘𝗧 𝟮𝟬𝟮𝟳 "
        "𝗟𝗜𝗩𝗘 𝗖𝗢𝗨𝗡𝗧𝗗𝗢𝗪𝗡</b>\n\n"
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
                url=(
                    "https://t.me/"
                    "NEET_TIMERS_BOT"
                    "?startgroup=true"
                )
            )
        ]
    ])

    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


# =========================================================
# /TIMER
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

    # GROUP
    elif chat.type in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):

        if not await is_owner(update):

            await update.message.reply_text(
                "❌ <b>Only the bot owner can use this command.</b>",
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
        context.args,
    )


# =========================================================
# CHANNEL COMMAND
# =========================================================

async def channel_command_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    post = update.channel_post

    if not post:
        return

    text = (
        post.text
        or post.caption
        or ""
    ).strip()

    if not text:
        return

    print(
        f"📢 CHANNEL POST: {text}"
    )

    first = text.split()[0]

    command = first.split("@")[0].lower()

    args = text.split()[1:]

    chat = update.effective_chat

    if not chat:
        return

    # Make sure bot is admin
    if not await bot_is_admin(
        update,
        context
    ):

        print(
            f"❌ Bot is not admin: {chat.id}"
        )

        return

    # TIMER
    if command == "/timer":

        await create_timer(
            update,
            context,
            chat.id,
            args,
        )

    # STOP
    elif command == "/stoptimer":

        await stop_timer(
            update,
            context,
        )


# =========================================================
# /STOP TIMER
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

            if update.message:

                await update.message.reply_text(
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

    timer = get_timer(chat.id)

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

    # Stop live task
    await cancel_task(chat.id)

    # Remove DB record
    delete_timer(chat.id)

    # Unpin
    try:

        await context.bot.unpin_chat_message(
            chat_id=chat.id,
            message_id=timer["message_id"],
        )

    except Exception:
        pass

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
                f"Stop reply error: {e}"
            )


# =========================================================
# RESTORE ALL TIMERS
# =========================================================

async def restore_timers(
    application: Application
):

    rows = get_all_timers()

    print(
        f"🔄 Found {len(rows)} saved timer(s)"
    )

    for chat_id, message_id, end_time in rows:

        try:

            remaining = int(
                end_time - time.time()
            )

            if remaining <= 0:

                delete_timer(chat_id)

                try:

                    await application.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=message_id,
                        text=time_up_text(),
                        parse_mode=ParseMode.HTML,
                    )

                except Exception:
                    pass

                continue

            # Verify message can still be edited
            try:

                await application.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=format_time(remaining),
                    parse_mode=ParseMode.HTML,
                )

            except Exception as e:

                print(
                    f"❌ Cannot restore "
                    f"{chat_id}: {e}"
                )

                delete_timer(chat_id)

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
                f"✅ Restored timer: {chat_id}"
            )

        except Exception as e:

            print(
                f"Restore error [{chat_id}]: {e}"
            )


# =========================================================
# STARTUP
# =========================================================

async def post_init(
    application: Application
):

    init_db()

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

    # START
    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # PRIVATE / GROUP / SUPERGROUP
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

    # CHANNEL
    application.add_handler(
        MessageHandler(
            filters.UpdateType.CHANNEL_POST,
            channel_command_handler
        )
    )

    print(
        "🎯 NEET TIMER BOT STARTED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
