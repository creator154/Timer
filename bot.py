import os
import re
import asyncio
import logging
import tempfile
import subprocess
import shutil
from pathlib import Path

import fitz
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import PollType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
MAX_PDF_MB = int(os.getenv("MAX_PDF_MB", "30") or 30)
POLL_DELAY = float(os.getenv("POLL_DELAY", "0.5") or 0.5)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ================= PDF TEXT EXTRACTION =================
def extract_page_columns(page):
    """Read the rendered page with Hindi+English OCR.

    This Allen PDF uses a custom embedded font whose extracted text is legacy
    glyph data (for example, "fuEu" instead of Devanagari). OCR reads the
    visible Hindi characters and returns Unicode text suitable for Telegram.
    """
    if not shutil.which("tesseract"):
        logger.warning("Tesseract not installed; falling back to PDF text extraction")
        return page.get_text("text", sort=True)

    pix = page.get_pixmap(matrix=fitz.Matrix(1.8, 1.8), alpha=False)
    image_bytes = pix.tobytes("png")
    command = [
        "tesseract", "stdin", "stdout", "-l", "hin+eng", "--psm", "3",
        "-c", "preserve_interword_spaces=1",
    ]
    try:
        result = subprocess.run(
            command,
            input=image_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.decode("utf-8", errors="replace")
        logger.warning("OCR failed on page %s: %s", page.number + 1,
                       result.stderr.decode("utf-8", errors="replace")[-500:])
    except subprocess.TimeoutExpired:
        logger.warning("OCR timed out on page %s", page.number + 1)
    except Exception:
        logger.exception("OCR error on page %s", page.number + 1)

    return page.get_text("text", sort=True)


def clean_text(text):
    text = text.replace("\x00", " ").replace("\u00a0", " ").replace("\ufeff", " ")
    text = text.replace("$//(1®", " ")
    # OCR sometimes reads the printed digit 1 as capital I / lowercase l.
    text = re.sub(r"(?m)^\s*[Il|]\.\s+", "1. ", text)
    text = re.sub(r"(?<!\S)\((?:I|l|॥)\)(?=\s)", "(1)", text)
    # Convert Devanagari digits to ASCII for question numbers and answer keys.
    devanagari_digits = "०१२३४५६७८९"
    text = text.translate(str.maketrans(devanagari_digits, "0123456789"))
    lines = []
    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()
        if not line:
            continue
        if line in ("ALLEN", "ALLEN®"):
            continue
        lines.append(line)
    return "\n".join(lines)


def extract_pdf_pages(pdf_path):
    pages = []
    with fitz.open(pdf_path) as pdf:
        for page_number, page in enumerate(pdf, start=1):
            text = clean_text(extract_page_columns(page))
            pages.append(text)
            logger.info("Extracted PDF page %s: %s characters", page_number, len(text))
    return pages


# ================= OLD PDF FORMAT =================
def parse_old_answer_key(text):
    answers = {}
    pattern = re.compile(r"\bQ\s*(\d{1,3})\s*[\.\):\-]?\s*([A-D])\b", re.I)
    for match in pattern.finditer(text):
        answers[int(match.group(1))] = ord(match.group(2).upper()) - ord("A")
    return answers


def parse_old_questions(text):
    questions = []
    q_pattern = re.compile(r"(?im)^\s*Q\s*(\d{1,3})\s*[\.\):\-]\s*")
    matches = list(q_pattern.finditer(text))

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end].strip()
        option_matches = list(re.finditer(r"(?<!\S)\(([A-D])\)\s*", block, re.I))
        option_map = {}
        for option_match in option_matches:
            label = option_match.group(1).upper()
            if label not in option_map:
                option_map[label] = option_match
        if len(option_map) != 4:
            continue

        ordered = sorted(option_map.values(), key=lambda item: item.start())
        question_text = re.sub(r"\s+", " ", block[:ordered[0].start()]).strip()
        options = [""] * 4
        for pos, option_match in enumerate(ordered):
            option_end = ordered[pos + 1].start() if pos + 1 < len(ordered) else len(block)
            label_index = ord(option_match.group(1).upper()) - ord("A")
            options[label_index] = re.sub(r"\s+", " ", block[option_match.end():option_end]).strip()

        if question_text and all(options):
            questions.append({
                "number": int(match.group(1)),
                "question": question_text,
                "options": options,
            })
    return questions


def build_old_quiz(pages):
    full_text = "\n".join(pages)
    key_match = re.search(r"(?i)ANSWER\s*KEY", full_text)
    if not key_match:
        logger.warning("Old-format answer key not found")
        return []

    questions = parse_old_questions(full_text[:key_match.start()])
    answers = parse_old_answer_key(full_text[key_match.start():])
    quiz = []
    for question in questions:
        answer = answers.get(question["number"])
        if answer is not None:
            question["correct"] = answer
            quiz.append(question)

    logger.info(
        "Old PDF counts: questions=%s answers=%s matched=%s",
        len(questions), len(answers), len(quiz),
    )
    return quiz


# ================= EXERCISE PDF FORMAT =================
def normalize_exercise_number(value):
    return {"I": 1, "II": 2, "III": 3, "1": 1, "2": 2, "3": 3}.get(value.upper())


def find_exercise_headings(text):
    """Find exercise titles and answer-key headings even if PDF splits lines."""
    pattern = re.compile(r"EXERCISE\s*[-–—]?\s*(III|II|I|[123])\b", re.I)
    headings = []

    for match in pattern.finditer(text):
        number = normalize_exercise_number(match.group(1))
        if not number:
            continue
        nearby = text[match.start():match.start() + 220]
        is_key = bool(re.search(r"ANSWER\s*KEY", nearby, re.I))
        headings.append({
            "number": number,
            "start": match.start(),
            "end": match.end(),
            "is_key": is_key,
            "line": re.sub(r"\s+", " ", nearby[:100]).strip(),
        })

    # Remove duplicate matches at the same position.
    unique = {item["start"]: item for item in headings}
    headings = sorted(unique.values(), key=lambda item: item["start"])

    for item in headings:
        logger.info(
            "Exercise heading: number=%s key=%s text=%s",
            item["number"], item["is_key"], item["line"],
        )
    return headings


def parse_new_answer_key(text):
    """Parse repeated Que. number rows paired with Ans. answer rows."""
    answers = {}
    pattern = re.compile(
        r"Que\.?\s*((?:\d+\s*)+)\s*Ans\.?\s*((?:[1-4]\s*)+)",
        re.I,
    )

    for match in pattern.finditer(text):
        question_numbers = [int(value) for value in re.findall(r"\d+", match.group(1))]
        answer_values = [int(value) for value in re.findall(r"\b[1-4]\b", match.group(2))]
        if len(question_numbers) != len(answer_values):
            logger.warning(
                "Answer-key row length mismatch: questions=%s answers=%s",
                len(question_numbers), len(answer_values),
            )
        for question_number, answer in zip(question_numbers, answer_values):
            answers[question_number] = answer - 1

    return answers


def parse_new_questions(text):
    """Parse numbered questions and options (1)-(4), including same-line options."""
    questions = []
    question_pattern = re.compile(r"(?m)^\s*(\d{1,3})\.\s+")
    matches = list(question_pattern.finditer(text))

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end].strip()

        option_matches = list(re.finditer(r"(?<!\S)\(([1-4])\)\s*", block))
        option_map = {}
        for option_match in option_matches:
            label = int(option_match.group(1))
            if label not in option_map:
                option_map[label] = option_match

        if len(option_map) != 4:
            logger.debug(
                "Skipping question %s: found option labels %s",
                match.group(1), sorted(option_map),
            )
            continue

        ordered = sorted(option_map.values(), key=lambda item: item.start())
        question_text = re.sub(r"\s+", " ", block[:ordered[0].start()]).strip()
        options = [""] * 4

        for position, option_match in enumerate(ordered):
            option_end = ordered[position + 1].start() if position + 1 < len(ordered) else len(block)
            option_index = int(option_match.group(1)) - 1
            option_text = block[option_match.end():option_end]
            option_text = re.sub(r"\bCC\d{3,}\b", "", option_text)
            option_text = re.sub(r"\$//\(1®", "", option_text)
            options[option_index] = re.sub(r"\s+", " ", option_text).strip()

        # Some scanned/diagram questions do not expose all four labels as text.
        if not question_text or not all(options):
            logger.debug("Skipping incomplete question %s", match.group(1))
            continue

        questions.append({
            "number": int(match.group(1)),
            "question": question_text,
            "options": options,
        })

    return questions


def build_new_exercises(pages):
    full_text = "\n".join(pages)
    headings = find_exercise_headings(full_text)
    if not headings:
        logger.warning("No exercise headings detected")
        return []

    grouped = {}
    for heading in headings:
        grouped.setdefault(heading["number"], []).append(heading)

    exercises = []
    titles = {
        1: "Exercise-I (Conceptual Questions)",
        2: "Exercise-II (Previous Year Questions)",
        3: "Exercise-III (Analytical Questions)",
    }

    for number in sorted(grouped):
        entries = grouped[number]
        title_heading = next((item for item in entries if not item["is_key"]), None)
        key_heading = next((item for item in entries if item["is_key"]), None)

        if not title_heading or not key_heading:
            logger.warning("Exercise %s title/key heading missing", number)
            continue
        if key_heading["start"] <= title_heading["start"]:
            logger.warning("Exercise %s heading order is invalid", number)
            continue

        question_section = full_text[title_heading["end"]:key_heading["start"]]

        later_titles = [
            item["start"]
            for other_number, other_entries in grouped.items()
            if other_number > number
            for item in other_entries
            if not item["is_key"] and item["start"] > key_heading["start"]
        ]
        key_end = min(later_titles) if later_titles else len(full_text)
        answer_section = full_text[key_heading["end"]:key_end]

        questions = parse_new_questions(question_section)
        answers = parse_new_answer_key(answer_section)
        matched = []

        for question in questions:
            answer = answers.get(question["number"])
            if answer is not None:
                question["correct"] = answer
                matched.append(question)

        logger.info(
            "%s counts: extracted_questions=%s answer_keys=%s matched=%s",
            titles.get(number, f"Exercise {number}"),
            len(questions), len(answers), len(matched),
        )

        exercises.append({
            "number": number,
            "title": titles.get(number, f"Exercise {number}"),
            "questions": matched,
            "answer_count": len(answers),
        })

    return exercises


# ================= TELEGRAM POLLS =================
def fit_telegram_limits(quiz):
    result = []
    for item in quiz:
        question = re.sub(r"\s+", " ", item["question"]).strip()
        options = [re.sub(r"\s+", " ", value).strip() for value in item["options"]]

        if len(question) > 300:
            question = question[:297].rstrip() + "..."
        options = [value if len(value) <= 100 else value[:97].rstrip() + "..." for value in options]

        if len(options) == 4 and all(options) and item["correct"] in range(4):
            result.append({"question": question, "options": options, "correct": item["correct"]})
    return result


async def send_quiz(chat_id, quiz, context):
    quiz = fit_telegram_limits(quiz)
    if not quiz:
        await context.bot.send_message(chat_id=chat_id, text="❌ Is exercise mein valid questions nahi mile.")
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Quiz start ho raha hai!\nTotal Questions: {len(quiz)}",
    )

    sent = 0
    failed = 0
    for item in quiz:
        try:
            await context.bot.send_poll(
                chat_id=chat_id,
                question=item["question"],
                options=item["options"],
                type=PollType.QUIZ,
                correct_option_id=item["correct"],
                is_anonymous=False,
            )
            sent += 1
            await asyncio.sleep(POLL_DELAY)
        except Exception:
            failed += 1
            logger.exception("Poll send failed")

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"✅ Quiz completed!\n\nSent: {sent}\nFailed: {failed}",
    )


# ================= AUTH / COMMANDS =================
def is_authorized(update):
    return OWNER_ID == 0 or bool(
        update.effective_user and update.effective_user.id == OWNER_ID
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "👋 Welcome!\n\nSend me a PDF file and I will convert its questions into Telegram quiz polls.\n\n"
        "Commands:\n/start - Start bot\n/help - Help\n/cancel - Cancel selection"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    await update.message.reply_text(
        "📚 PDF Quiz Bot Help\n\n"
        "1. Send a PDF file.\n"
        "2. Bot extracts questions and answer keys.\n"
        "3. For exercise PDFs, select an exercise.\n"
        "4. Select All Exercises to send every exercise.\n\n"
        "Supported formats:\n"
        "• Q1 with (A), (B), (C), (D)\n"
        "• 1. with (1), (2), (3), (4)\n"
        "• Que. / Ans. answer-key tables"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return
    context.user_data.pop("pending_exercises", None)
    await update.message.reply_text("❌ Current selection cancelled.")


# ================= EXERCISE SELECTION =================
async def show_exercise_selection(update, context, exercises):
    context.user_data["pending_exercises"] = exercises
    keyboard = []

    for index, exercise in enumerate(exercises, start=1):
        keyboard.append([InlineKeyboardButton(
            f"{index}. {exercise['title']} ({len(exercise['questions'])})",
            callback_data=f"exercise:{index}",
        )])

    keyboard.append([InlineKeyboardButton(
        f"{len(exercises) + 1}. All Exercises",
        callback_data="exercise:all",
    )])

    await update.message.reply_text(
        "📚 PDF mein multiple exercises mili hain.\n\nKaunsi exercise ke polls chahiye?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def exercise_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if OWNER_ID and query.from_user.id != OWNER_ID:
        await query.answer("You are not authorized.", show_alert=True)
        return
    await query.answer()

    exercises = context.user_data.get("pending_exercises")
    if not exercises:
        await query.edit_message_text("❌ Selection expire ho gaya. PDF dobara bhejein.")
        return

    selection = query.data.split(":", 1)[1]
    if selection == "all":
        selected = exercises
    else:
        try:
            selected = [exercises[int(selection) - 1]]
        except (ValueError, IndexError):
            await query.edit_message_text("❌ Invalid selection.")
            return

    context.user_data.pop("pending_exercises", None)
    await query.edit_message_text("✅ Selection received. Quiz start ho raha hai...")

    for exercise in selected:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"📖 {exercise['title']}\nQuestions: {len(exercise['questions'])}",
        )
        await send_quiz(query.message.chat_id, exercise["questions"], context)


# ================= PDF HANDLER =================
async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    document = update.message.document
    if not document:
        return

    filename = document.file_name or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        await update.message.reply_text("❌ Please send a PDF file.")
        return

    if (document.file_size or 0) > MAX_PDF_MB * 1024 * 1024:
        await update.message.reply_text(f"❌ PDF size limit {MAX_PDF_MB} MB hai.")
        return

    status = await update.message.reply_text("⏳ PDF download aur analysis ho raha hai...")
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_file:
            temp_path = temp_file.name

        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(custom_path=temp_path)

        pages = extract_pdf_pages(temp_path)
        if not pages:
            await status.edit_text("❌ PDF se text nahi mil paya.")
            return

        full_text = "\n".join(pages)
        headings = find_exercise_headings(full_text)

        if headings:
            exercises = build_new_exercises(pages)
            exercises = [item for item in exercises if item["questions"]]

            if not exercises:
                await status.edit_text(
                    "❌ Exercises detect hui, lekin questions aur answer keys match nahi ho paayi.\n\n"
                    "Heroku logs mein Exercise counts check karein."
                )
                return

            await status.delete()
            await show_exercise_selection(update, context, exercises)
            return

        quiz = build_old_quiz(pages)
        if not quiz:
            await status.edit_text("❌ Questions aur answer key match nahi ho paaye.")
            return

        await status.edit_text(
            f"✅ Old-format PDF detected.\nTotal matched questions: {len(quiz)}\n\nQuiz start ho raha hai..."
        )
        await send_quiz(update.effective_chat.id, quiz, context)

    except Exception:
        logger.exception("PDF processing failed")
        try:
            await status.edit_text("❌ PDF process karte waqt error aa gaya. Heroku logs check karein.")
        except Exception:
            pass
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ================= MAIN =================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN config var missing!")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CallbackQueryHandler(exercise_callback, pattern=r"^exercise:"))
    application.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    logger.info("PDF Quiz Bot Started")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
