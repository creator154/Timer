
import os
import re
import asyncio
import logging
from io import BytesIO

import fitz
from telegram import Update
from telegram.constants import PollType
from telegram.error import TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
MAX_PDF_MB = int(os.getenv("MAX_PDF_MB", "20"))
POLL_DELAY = float(os.getenv("POLL_DELAY", "1.2"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================= PDF TEXT =================

def extract_pdf_text(pdf_bytes):
    """Extract selectable text from PDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages = []

    for page in doc:
        # Sorted extraction helps with normal and many two-column PDFs.
        pages.append(page.get_text("text", sort=True))

    doc.close()
    return "\n".join(pages)


def clean_text(text):
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\bCC\d{3,}\b", "", text)
    text = re.sub(r"\b[A-Z]{2,5}\d{3,}\b", "", text)
    return text.strip(" \n\t.-")


# ================= ANSWER KEY =================

def parse_answer_key(text):
    answers = {}

    # Format:
    # Que. 1 2 3 4 5
    # Ans. 3 2 1 4 2

    pattern = re.compile(
        r"\bQue\.?\s*(.*?)\bAns\.?\s*(.*?)(?=\bQue\.?|$)",
        re.IGNORECASE | re.DOTALL,
    )

    for match in pattern.finditer(text):
        question_part = match.group(1)
        answer_part = match.group(2)

        questions = re.findall(r"\d+", question_part)
        values = re.findall(
            r"(?<![A-Za-z0-9])([1-4]|[A-D])(?![A-Za-z0-9])",
            answer_part,
            re.IGNORECASE,
        )

        for q, ans in zip(questions, values):
            answers[int(q)] = ans.upper()

    # Other common formats:
    # 1. B
    # 2. C
    # 3. 4

    if not answers:
        for match in re.finditer(
            r"(?m)^\s*(\d{1,3})\s*[.)\-:]\s*\(?([1-4]|[A-D])\)?\s*$",
            text,
            re.IGNORECASE,
        ):
            answers[int(match.group(1))] = match.group(2).upper()

    return answers


def answer_to_index(answer):
    if not answer:
        return None

    answer = str(answer).strip().upper()

    if answer in ("A", "B", "C", "D"):
        return ord(answer) - ord("A")

    if answer in ("1", "2", "3", "4"):
        return int(answer) - 1

    return None


# ================= QUESTION PARSER =================

def parse_questions(text, answer_key):
    questions = []

    # Question starts:
    # 1. Question text
    # 2) Question text

    starts = list(
        re.finditer(
            r"(?m)^\s*(\d{1,3})\s*[.)]\s+",
            text,
        )
    )

    for i, start in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        block = text[start.start():end]

        question_number = int(start.group(1))

        # Remove question number.
        block = re.sub(
            r"^\s*\d{1,3}\s*[.)]\s*",
            "",
            block,
            count=1,
        )

        # Supports both:
        # (A), (B), (C), (D)
        # (1), (2), (3), (4)

        option_matches = list(
            re.finditer(
                r"\(([1-4]|[A-D])\)\s*",
                block,
                re.IGNORECASE,
            )
        )

        if len(option_matches) < 4:
            continue

        option_matches = option_matches[:4]

        question_text = clean_text(block[:option_matches[0].start()])

        if not question_text:
            continue

        options = []

        for j, option_match in enumerate(option_matches):
            option_start = option_match.end()

            option_end = (
                option_matches[j + 1].start()
                if j + 1 < len(option_matches)
                else len(block)
            )

            option_text = clean_text(block[option_start:option_end])
            options.append(option_text)

        if len(options) != 4 or any(not option for option in options):
            continue

        answer = answer_key.get(question_number)
        correct_index = answer_to_index(answer)

        if correct_index is None:
            continue

        if correct_index > 3:
            continue

        questions.append({
            "number": question_number,
            "question": question_text[:300],
            "options": [option[:100] for option in options],
            "correct": correct_index,
        })

    return questions


# ================= EXERCISE DETECTION =================

EXERCISE_PATTERN = re.compile(
    r"(?im)^\s*EXERCISE\s*[-–—]?\s*"
    r"([IVXLCDM]+|\d+)\s*"
    r"\(([^)\n]{2,100})\)",
)


def detect_exercises(text):
    matches = list(EXERCISE_PATTERN.finditer(text))
    exercises = []

    if not matches:
        # Old PDF format without exercise headings.
        key_match = re.search(
            r"ANSWER\s*KEY",
            text,
            re.IGNORECASE,
        )

        if key_match:
            question_text = text[:key_match.start()]
            key_text = text[key_match.start():]
        else:
            question_text = text
            key_text = ""

        answer_key = parse_answer_key(key_text)
        questions = parse_questions(question_text, answer_key)

        if questions:
            exercises.append({
                "name": "All Questions",
                "questions": questions,
            })

        return exercises

    for i, match in enumerate(matches):
        exercise_number = match.group(1).upper()
        exercise_name = match.group(2).strip()

        start = match.end()
        end = (
            matches[i + 1].start()
            if i + 1 < len(matches)
            else len(text)
        )

        section = text[start:end]

        # Locate this exercise's answer key.
        key_match = re.search(
            r"ANSWER\s*KEY",
            section,
            re.IGNORECASE,
        )

        if key_match:
            question_text = section[:key_match.start()]
            key_text = section[key_match.start():]
        else:
            question_text = section
            key_text = ""

        answer_key = parse_answer_key(key_text)
        questions = parse_questions(question_text, answer_key)

        if questions:
            exercises.append({
                "name": f"Exercise-{exercise_number} ({exercise_name})",
                "questions": questions,
            })

    return exercises


# ================= ACCESS CONTROL =================

def is_allowed(user_id):
    return OWNER_ID == 0 or user_id == OWNER_ID


# ================= COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    await update.message.reply_text(
        "👋 Welcome to PDF Quiz Bot!\n\n"
        "📄 Mujhe MCQ PDF bhejo.\n"
        "Main automatically questions aur answer key detect karke "
        "Telegram Quiz Polls bana dunga.\n\n"
        "/help - Help\n"
        "/cancel - Cancel current PDF"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    await update.message.reply_text(
        "📚 PDF QUIZ BOT HELP\n\n"
        "1. PDF upload karo.\n"
        "2. Bot exercises detect karega.\n"
        "3. Exercise number select karo.\n"
        "4. 4 select karne par all exercises ke polls banenge.\n\n"
        "Supported formats:\n"
        "• Old Q1 / (A), (B), (C), (D)\n"
        "• New 1. / (1), (2), (3), (4)\n\n"
        "/cancel - Current process cancel"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    context.user_data.pop("exercises", None)
    context.user_data.pop("waiting_selection", None)

    await update.message.reply_text("❌ Current process cancelled.")


# ================= PDF HANDLER =================

async def pdf_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_allowed(user_id):
        return

    document = update.message.document

    if not document.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ Please send a PDF file.")
        return

    max_size = MAX_PDF_MB * 1024 * 1024

    if document.file_size and document.file_size > max_size:
        await update.message.reply_text(
            f"❌ PDF too large. Maximum size: {MAX_PDF_MB} MB"
        )
        return

    status = await update.message.reply_text(
        "⏳ PDF read ho rahi hai...\nPlease wait."
    )

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        pdf_bytes = await telegram_file.download_as_bytearray()

        text = extract_pdf_text(bytes(pdf_bytes))

        if not text.strip():
            await status.edit_text(
                "❌ PDF se text nahi mil paaya. "
                "Ye scanned/image PDF ho sakti hai."
            )
            return

        exercises = detect_exercises(text)

        if not exercises:
            await status.edit_text(
                "❌ Questions aur answer key match nahi ho paaye.\n\n"
                "PDF mein supported question format ya answer key "
                "detect nahi hui."
            )
            return

        context.user_data["exercises"] = exercises
        context.user_data["waiting_selection"] = True

        lines = ["📚 PDF Exercises Found\n"]

        total_questions = 0

        for index, exercise in enumerate(exercises, start=1):
            count = len(exercise["questions"])
            total_questions += count
            lines.append(f"{index}. {exercise['name']} — {count} Questions")

        lines.append(f"\nTotal Exercises: {len(exercises)}")
        lines.append(f"Total Matched Questions: {total_questions}")
        lines.append("\nExercise select karo:\n")

        for index in range(1, len(exercises) + 1):
            lines.append(str(index))

        lines.append(f"{len(exercises) + 1}. All Exercises")

        # Selection 4 is reserved for all exercises in the
        # requested 3-exercise PDF format.
        # For PDFs with another number of exercises, All is last.
        lines.append("\nReply mein sirf number likho.")

        await status.edit_text("\n".join(lines))

    except Exception as e:
        logger.exception("PDF processing error")
        await status.edit_text(
            f"❌ PDF process nahi ho paayi.\nError: {str(e)[:300]}"
        )


# ================= QUIZ SENDER =================

async def send_exercise_polls(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    selected_exercises,
):
    total = sum(len(ex["questions"]) for ex in selected_exercises)

    progress = await update.message.reply_text(
        f"🚀 Quiz banana start ho gaya!\n"
        f"Total Questions: {total}"
    )

    sent = 0
    failed = 0

    for exercise in selected_exercises:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📚 {exercise['name']}\n"
                 f"Questions: {len(exercise['questions'])}",
        )

        for question in exercise["questions"]:
            try:
                await context.bot.send_poll(
                    chat_id=update.effective_chat.id,
                    question=question["question"],
                    options=question["options"],
                    type=PollType.QUIZ,
                    correct_option_id=question["correct"],
                    is_anonymous=True,
                )

                sent += 1
                await asyncio.sleep(POLL_DELAY)

            except TelegramError as e:
                failed += 1
                logger.warning(
                    "Poll failed for question %s: %s",
                    question["number"],
                    e,
                )

    await progress.edit_text(
        "✅ Quiz Completed!\n\n"
        f"📤 Polls Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"📚 Exercises: {len(selected_exercises)}"
    )

    context.user_data.pop("exercises", None)
    context.user_data.pop("waiting_selection", None)


# ================= SELECTION HANDLER =================

async def selection_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_allowed(update.effective_user.id):
        return

    if not context.user_data.get("waiting_selection"):
        return

    exercises = context.user_data.get("exercises")

    if not exercises:
        await update.message.reply_text(
            "❌ Pehle PDF upload karo."
        )
        return

    value = update.message.text.strip()

    if not re.fullmatch(r"\d+", value):
        return

    selection = int(value)
    all_option = len(exercises) + 1

    if selection < 1 or selection > all_option:
        await update.message.reply_text(
            f"❌ Invalid selection. 1 se {all_option} tak number bhejo."
        )
        return

    context.user_data["waiting_selection"] = False

    if selection == all_option:
        selected = exercises
    else:
        selected = [exercises[selection - 1]]

    await send_exercise_polls(update, context, selected)


# ================= MAIN =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing.")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(
        MessageHandler(
            filters.Document.PDF,
            pdf_handler,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            selection_handler,
        )
    )

    logger.info("PDF Quiz Bot Started!")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
