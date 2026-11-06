
import os
import re
import asyncio
import logging
import tempfile

import fitz

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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

MAX_PDF_MB = int(os.getenv("MAX_PDF_MB", "30"))
POLL_DELAY = float(os.getenv("POLL_DELAY", "0.5"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)


# ================= TEXT EXTRACTION =================

def clean_text(text):
    text = text.replace("\x00", " ")
    text = text.replace("\u00a0", " ")
    text = text.replace("\ufeff", " ")

    lines = []

    for line in text.splitlines():
        line = re.sub(r"[ \t]+", " ", line).strip()

        if not line:
            continue

        if re.fullmatch(r"\$//\(1®?", line):
            continue

        if line in ("ALLEN", "ALLEN®"):
            continue

        lines.append(line)

    return "\n".join(lines)


def extract_page_columns(page):
    """
    Extract left column first and then right column.
    """

    words = page.get_text("words", sort=False)

    if not words:
        return page.get_text("text", sort=True)

    grouped = {}

    for word in words:
        x0, y0, x1, y1, value, block_no, line_no, word_no = word

        key = (block_no, line_no)
        grouped.setdefault(key, []).append(word)

    left_lines = []
    right_lines = []

    middle = page.rect.width / 2

    for items in grouped.values():
        items.sort(key=lambda x: x[7])

        line_text = " ".join(item[4] for item in items)

        x0 = min(item[0] for item in items)
        y0 = min(item[1] for item in items)

        line = (y0, x0, line_text)

        if x0 < middle:
            left_lines.append(line)
        else:
            right_lines.append(line)

    left_lines.sort(key=lambda x: (x[0], x[1]))
    right_lines.sort(key=lambda x: (x[0], x[1]))

    result = []

    for _, _, line in left_lines:
        result.append(line)

    for _, _, line in right_lines:
        result.append(line)

    return "\n".join(result)


def extract_pdf_pages(pdf_path):
    pages = []

    with fitz.open(pdf_path) as pdf:
        for page in pdf:
            text = extract_page_columns(page)
            pages.append(clean_text(text))

    return pages


# ================= OLD PDF FORMAT =================

def parse_old_answer_key(text):
    answers = {}

    pattern = re.compile(
        r"\bQ\s*(\d{1,3})\s*[\.\):\-]?\s*([A-D])\b",
        re.IGNORECASE,
    )

    for match in pattern.finditer(text):
        qno = int(match.group(1))
        answer = match.group(2).upper()

        answers[qno] = ord(answer) - ord("A")

    return answers


def parse_old_questions(text):
    questions = []

    question_pattern = re.compile(
        r"(?im)^\s*Q\s*(\d{1,3})\s*[\.\):\-]\s*"
    )

    matches = list(question_pattern.finditer(text))

    for index, match in enumerate(matches):
        start = match.end()

        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        block = text[start:end].strip()

        option_pattern = re.compile(
            r"(?im)^\s*\(([A-D])\)\s*"
        )

        option_matches = list(option_pattern.finditer(block))

        if len(option_matches) < 4:
            continue

        options = []

        for i in range(4):
            option_start = option_matches[i].end()

            option_end = (
                option_matches[i + 1].start()
                if i + 1 < len(option_matches)
                else len(block)
            )

            option_text = block[
                option_start:option_end
            ].strip()

            option_text = re.sub(
                r"\s+",
                " ",
                option_text,
            )

            options.append(option_text)

        question_text = block[
            :option_matches[0].start()
        ].strip()

        question_text = re.sub(
            r"\s+",
            " ",
            question_text,
        )

        if not question_text:
            continue

        questions.append({
            "number": int(match.group(1)),
            "question": question_text,
            "options": options,
        })

    return questions


def build_old_quiz(pages):
    full_text = "\n".join(pages)

    answer_key_match = re.search(
        r"(?i)ANSWER\s*KEY",
        full_text,
    )

    if not answer_key_match:
        logger.warning("Old-format Answer Key not found")
        return []

    question_text = full_text[
        :answer_key_match.start()
    ]

    answer_text = full_text[
        answer_key_match.start():
    ]

    questions = parse_old_questions(question_text)
    answers = parse_old_answer_key(answer_text)

    quiz = []

    for question in questions:
        qno = question["number"]

        if qno not in answers:
            continue

        question["correct"] = answers[qno]
        quiz.append(question)

    logger.info(
        "Old PDF: questions=%s, answers=%s, matched=%s",
        len(questions),
        len(answers),
        len(quiz),
    )

    return quiz


# ================= NEW PDF FORMAT =================

def normalize_exercise_number(value):
    value = value.upper().strip()

    mapping = {
        "I": 1,
        "II": 2,
        "III": 3,
        "1": 1,
        "2": 2,
        "3": 3,
    }

    return mapping.get(value)


def find_exercise_headings(text):
    """
    Supports:
    EXERCISE-I
    EXERCISE - I
    EXERCISE II
    EXERCISE-III
    EXERCISE 1
    """

    pattern = re.compile(
        r"(?im)^\s*EXERCISE\s*[-–—]?\s*"
        r"(III|II|I|[123])\b[^\n]*"
    )

    headings = []

    for match in pattern.finditer(text):
        number = normalize_exercise_number(
            match.group(1)
        )

        if not number:
            continue

        line = match.group(0).strip()

        is_key = bool(
            re.search(
                r"ANSWER\s*[-–—]?\s*KEY",
                line,
                re.IGNORECASE,
            )
        )

        headings.append({
            "number": number,
            "start": match.start(),
            "end": match.end(),
            "line": line,
            "is_key": is_key,
        })

    # Fallback: heading may have leading text on its line.
    if not headings:
        fallback = re.compile(
            r"(?im)EXERCISE\s*[-–—]?\s*"
            r"(III|II|I|[123])\b[^\n]*"
        )

        for match in fallback.finditer(text):
            number = normalize_exercise_number(
                match.group(1)
            )

            if not number:
                continue

            line = match.group(0).strip()

            headings.append({
                "number": number,
                "start": match.start(),
                "end": match.end(),
                "line": line,
                "is_key": bool(
                    re.search(
                        r"ANSWER\s*[-–—]?\s*KEY",
                        line,
                        re.IGNORECASE,
                    )
                ),
            })

    # Remove duplicate heading positions.
    unique = {}
    for heading in headings:
        unique[heading["start"]] = heading

    headings = sorted(
        unique.values(),
        key=lambda x: x["start"],
    )

    for heading in headings:
        logger.info(
            "Heading detected: Exercise %s | key=%s | %s",
            heading["number"],
            heading["is_key"],
            heading["line"],
        )

    return headings


def parse_new_answer_key(text):
    """
    Reads answer tables such as:

    Que. 1 2 3 4 5
    Ans. 1 2 3 4 1

    Also supports wrapped Que/Ans rows.
    """

    answers = {}

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    question_numbers = []
    answer_mode = False
    answer_values = []

    def save_answers():
        nonlocal question_numbers, answer_values

        if question_numbers and answer_values:
            for qno, answer in zip(
                question_numbers,
                answer_values,
            ):
                if answer in (1, 2, 3, 4):
                    answers[qno] = answer - 1

        question_numbers = []
        answer_values = []

    for line in lines:

        # Ignore unrelated headings.
        if re.search(
            r"EXERCISE\s*[-–—]?\s*(III|II|I|[123])",
            line,
            re.IGNORECASE,
        ):
            save_answers()
            answer_mode = False
            continue

        que_match = re.match(
            r"(?i)^Que\.?\s*(.*)$",
            line,
        )

        if que_match:
            save_answers()

            question_numbers = [
                int(x)
                for x in re.findall(
                    r"\d+",
                    que_match.group(1),
                )
            ]

            answer_mode = False
            continue

        ans_match = re.match(
            r"(?i)^Ans\.?\s*(.*)$",
            line,
        )

        if ans_match:
            answer_values = [
                int(x)
                for x in re.findall(
                    r"\b[1-4]\b",
                    ans_match.group(1),
                )
            ]

            answer_mode = True
            continue

        # Continuation of a wrapped Que row.
        if question_numbers and not answer_mode:
            if re.fullmatch(r"[\d\s]+", line):
                question_numbers.extend(
                    int(x)
                    for x in re.findall(r"\d+", line)
                )
                continue

        # Continuation of a wrapped Ans row.
        if answer_mode:
            if re.fullmatch(r"[\d\s]+", line):
                answer_values.extend(
                    int(x)
                    for x in re.findall(
                        r"\b[1-4]\b",
                        line,
                    )
                )
                continue

            # Stop if another unrelated line begins.
            save_answers()
            answer_mode = False

    save_answers()

    return answers


def parse_new_questions(text):
    questions = []

    question_pattern = re.compile(
        r"(?m)^\s*(\d{1,3})\.\s+"
    )

    matches = list(question_pattern.finditer(text))

    for index, match in enumerate(matches):
        start = match.end()

        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        block = text[start:end].strip()

        option_pattern = re.compile(
            r"(?m)^\s*\(([1-4])\)\s*"
        )

        option_matches = list(
            option_pattern.finditer(block)
        )

        selected = []
        seen = set()

        for option_match in option_matches:
            label = int(option_match.group(1))

            if label not in seen:
                selected.append(option_match)
                seen.add(label)

            if len(selected) == 4:
                break

        if len(selected) != 4:
            continue

        selected.sort(key=lambda x: x.start())

        labels = [
            int(x.group(1))
            for x in selected
        ]

        if labels != [1, 2, 3, 4]:
            continue

        question_text = block[
            :selected[0].start()
        ].strip()

        question_text = re.sub(
            r"\s+",
            " ",
            question_text,
        )

        options = []

        for i in range(4):
            option_start = selected[i].end()

            option_end = (
                selected[i + 1].start()
                if i < 3
                else len(block)
            )

            option_text = block[
                option_start:option_end
            ].strip()

            option_text = re.sub(
                r"\s+",
                " ",
                option_text,
            )

            option_text = re.sub(
                r"\bCC\d{3,}\b",
                "",
                option_text,
            ).strip()

            options.append(option_text)

        if not question_text:
            continue

        if any(not option for option in options):
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
        logger.warning("No exercise headings found")
        return []

    grouped = {}

    for heading in headings:
        grouped.setdefault(
            heading["number"],
            [],
        ).append(heading)

    exercises = []

    for number in sorted(grouped):
        entries = grouped[number]

        title_heading = None
        key_heading = None

        for entry in entries:
            if entry["is_key"]:
                key_heading = entry
            elif title_heading is None:
                title_heading = entry

        if not title_heading or not key_heading:
            logger.warning(
                "Exercise %s title/key heading missing",
                number,
            )
            continue

        if key_heading["start"] <= title_heading["start"]:
            logger.warning(
                "Exercise %s: invalid heading order",
                number,
            )
            continue

        question_section = full_text[
            title_heading["end"]:
            key_heading["start"]
        ]

        next_title = None

        for other_number, other_entries in grouped.items():
            if other_number <= number:
                continue

            for entry in other_entries:
                if not entry["is_key"]:
                    if (
                        next_title is None
                        or entry["start"] < next_title["start"]
                    ):
                        next_title = entry

        key_end = (
            next_title["start"]
            if next_title
            else len(full_text)
        )

        answer_section = full_text[
            key_heading["end"]:
            key_end
        ]

        questions = parse_new_questions(
            question_section
        )

        answers = parse_new_answer_key(
            answer_section
        )

        matched = []

        for question in questions:
            qno = question["number"]

            if qno not in answers:
                continue

            question["correct"] = answers[qno]
            matched.append(question)

        title = {
            1: "Exercise-I (Conceptual Questions)",
            2: "Exercise-II (Previous Year Questions)",
            3: "Exercise-III (Analytical Questions)",
        }.get(number, f"Exercise {number}")

        logger.info(
            "%s: questions=%s, answers=%s, matched=%s",
            title,
            len(questions),
            len(answers),
            len(matched),
        )

        exercises.append({
            "number": number,
            "title": title,
            "questions": matched,
        })

    return exercises


# ================= TELEGRAM POLL HELPERS =================

def fit_telegram_limits(quiz):
    result = []

    for item in quiz:
        question = re.sub(
            r"\s+",
            " ",
            item["question"],
        ).strip()

        options = [
            re.sub(r"\s+", " ", option).strip()
            for option in item["options"]
        ]

        if len(question) > 300:
            question = question[:297].rstrip() + "..."

        options = [
            option if len(option) <= 100
            else option[:97].rstrip() + "..."
            for option in options
        ]

        if (
            len(options) != 4
            or not all(options)
            or item["correct"] not in range(4)
        ):
            continue

        result.append({
            "question": question,
            "options": options,
            "correct": item["correct"],
        })

    return result


async def send_quiz(chat_id, quiz, context):
    quiz = fit_telegram_limits(quiz)

    if not quiz:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ Is exercise mein valid questions nahi mile.",
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "✅ Quiz start ho raha hai!\n"
            f"Total Questions: {len(quiz)}"
        ),
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
        text=(
            "✅ Quiz completed!\n\n"
            f"Sent: {sent}\n"
            f"Failed: {failed}"
        ),
    )


# ================= AUTHORIZATION =================

def is_authorized(update):
    if OWNER_ID == 0:
        return True

    return bool(
        update.effective_user
        and update.effective_user.id == OWNER_ID
    )


# ================= COMMANDS =================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        return

    await update.message.reply_text(
        "👋 Welcome!\n\n"
        "Send me a PDF file and I will convert its questions "
        "into Telegram quiz polls.\n\n"
        "Commands:\n"
        "/start - Start bot\n"
        "/help - Help\n"
        "/cancel - Cancel selection"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        return

    await update.message.reply_text(
        "📚 PDF Quiz Bot Help\n\n"
        "1. Send a PDF file.\n"
        "2. Bot will extract questions and answer keys.\n"
        "3. For exercise PDFs, select the exercise.\n"
        "4. Select All Exercises to send every exercise.\n\n"
        "Supported formats:\n"
        "• Q1 with (A), (B), (C), (D)\n"
        "• 1. with (1), (2), (3), (4)\n"
        "• Que. / Ans. answer-key tables"
    )


async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        return

    context.user_data.pop(
        "pending_exercises",
        None,
    )

    await update.message.reply_text(
        "❌ Current selection cancelled."
    )


# ================= EXERCISE SELECTION =================

async def show_exercise_selection(
    update,
    context,
    exercises,
):
    context.user_data["pending_exercises"] = exercises

    keyboard = []

    for index, exercise in enumerate(
        exercises,
        start=1,
    ):
        keyboard.append([
            InlineKeyboardButton(
                (
                    f"{index}. {exercise['title']} "
                    f"({len(exercise['questions'])})"
                ),
                callback_data=f"exercise:{index}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            f"{len(exercises) + 1}. All Exercises",
            callback_data="exercise:all",
        )
    ])

    await update.message.reply_text(
        "📚 PDF mein multiple exercises mili hain.\n\n"
        "Kaunsi exercise ke polls chahiye?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def exercise_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if OWNER_ID and query.from_user.id != OWNER_ID:
        await query.answer(
            "You are not authorized.",
            show_alert=True,
        )
        return

    await query.answer()

    exercises = context.user_data.get(
        "pending_exercises"
    )

    if not exercises:
        await query.edit_message_text(
            "❌ Selection expire ho gaya. PDF dobara bhejein."
        )
        return

    selection = query.data.split(":", 1)[1]

    if selection == "all":
        selected = exercises
    else:
        try:
            index = int(selection) - 1
            selected = [exercises[index]]

        except (ValueError, IndexError):
            await query.edit_message_text(
                "❌ Invalid selection."
            )
            return

    context.user_data.pop(
        "pending_exercises",
        None,
    )

    await query.edit_message_text(
        "✅ Selection received. Quiz start ho raha hai..."
    )

    for exercise in selected:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                f"📖 {exercise['title']}\n"
                f"Questions: {len(exercise['questions'])}"
            ),
        )

        await send_quiz(
            query.message.chat_id,
            exercise["questions"],
            context,
        )


# ================= PDF HANDLER =================

async def handle_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not is_authorized(update):
        return

    document = update.message.document

    if not document:
        return

    filename = document.file_name or "document.pdf"

    if not filename.lower().endswith(".pdf"):
        await update.message.reply_text(
            "❌ Please send a PDF file."
        )
        return

    file_size = document.file_size or 0

    if file_size > MAX_PDF_MB * 1024 * 1024:
        await update.message.reply_text(
            f"❌ PDF size limit {MAX_PDF_MB} MB hai."
        )
        return

    status = await update.message.reply_text(
        "⏳ PDF download aur analysis ho raha hai..."
    )

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name

        telegram_file = await context.bot.get_file(
            document.file_id
        )

        await telegram_file.download_to_drive(
            custom_path=temp_path
        )

        pages = extract_pdf_pages(temp_path)

        if not pages:
            await status.edit_text(
                "❌ PDF se text nahi mil paya."
            )
            return

        full_text = "\n".join(pages)

        headings = find_exercise_headings(
            full_text
        )

        if headings:
            exercises = build_new_exercises(
                pages
            )

            exercises = [
                exercise
                for exercise in exercises
                if exercise["questions"]
            ]

            if not exercises:
                await status.edit_text(
                    "❌ Exercises detect hui, lekin questions "
                    "aur answer keys match nahi ho paayi.\n\n"
                    "Heroku logs check karein."
                )
                return

            await status.delete()

            await show_exercise_selection(
                update,
                context,
                exercises,
            )

            return

        # Old PDF format fallback.
        quiz = build_old_quiz(pages)

        if not quiz:
            await status.edit_text(
                "❌ Questions aur answer key match nahi ho paaye."
            )
            return

        await status.edit_text(
            "✅ Old-format PDF detected.\n"
            f"Total matched questions: {len(quiz)}\n\n"
            "Quiz start ho raha hai..."
        )

        await send_quiz(
            update.effective_chat.id,
            quiz,
            context,
        )

    except Exception:
        logger.exception(
            "PDF processing failed"
        )

        try:
            await status.edit_text(
                "❌ PDF process karte waqt error aa gaya.\n"
                "Heroku logs check karein."
            )
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
        raise RuntimeError(
            "BOT_TOKEN config var missing!"
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("cancel", cancel)
    )

    application.add_handler(
        CallbackQueryHandler(
            exercise_callback,
            pattern=r"^exercise:",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.PDF,
            handle_pdf,
        )
    )

    logger.info(
        "PDF Quiz Bot Started"
    )

    application.run_polling(
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
