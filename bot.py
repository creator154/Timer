
import os
import re
import asyncio
import logging
import tempfile
from collections import defaultdict

import fitz
from telegram import Update
from telegram.constants import PollType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
MAX_PDF_MB = int(os.getenv("MAX_PDF_MB", "20"))
POLL_DELAY = float(os.getenv("POLL_DELAY", "1.2"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("pdf-quiz-poll-maker")


# ================= TEXT CLEANING =================

def clean_text(text):
    text = text.replace("\u00a0", " ")
    text = text.replace("\uf0b7", " ")
    text = text.replace("\u200b", "")
    text = text.replace("\x00", " ")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\bCC\d{3,}\b", "", text)
    text = re.sub(r"\bAndroid App\s*\|.*", "", text, flags=re.I)
    text = re.sub(r"\biOS App\b|\bPW Website\b", "", text, flags=re.I)

    return text.strip()


# ================= PDF EXTRACTION =================

def extract_page_columns(page):
    """
    Original two-column extraction method.
    Left column first, then right column.
    """

    words = page.get_text("words", sort=False)
    lines = defaultdict(list)

    for word in words:
        x0, y0, x1, y1, text, block_no, line_no, word_no = word[:8]

        lines[(block_no, line_no)].append({
            "x": x0,
            "y": y0,
            "text": text,
        })

    left = []
    right = []

    middle = page.rect.width / 2

    for line_words in lines.values():
        line_words.sort(key=lambda x: x["x"])

        line_text = " ".join(
            item["text"] for item in line_words
        )

        line_text = clean_text(line_text)

        if not line_text:
            continue

        x = min(item["x"] for item in line_words)
        y = min(item["y"] for item in line_words)

        item = (y, x, line_text)

        if x < middle:
            left.append(item)
        else:
            right.append(item)

    left.sort(key=lambda x: (x[0], x[1]))
    right.sort(key=lambda x: (x[0], x[1]))

    output = []

    for _, _, text in left:
        output.append(text)

    output.append("")

    for _, _, text in right:
        output.append(text)

    return "\n".join(output)


def extract_pdf_pages(path):
    doc = fitz.open(path)

    try:
        return [
            extract_page_columns(page)
            for page in doc
        ]
    finally:
        doc.close()


# ================= ANSWER KEY PARSER =================

def parse_answer_key(text):
    answers = {}

    # New format:
    # Que. 1 2 3 4 5
    # Ans. 3 2 1 4 2

    row_pattern = re.compile(
        r"\bQue\.?\s*(.*?)\s*\bAns\.?\s*(.*?)"
        r"(?=\bQue\.?\s*|$)",
        re.I | re.S,
    )

    for match in row_pattern.finditer(text):
        question_part = match.group(1)
        answer_part = match.group(2)

        question_numbers = re.findall(
            r"\d{1,3}",
            question_part,
        )

        answer_values = re.findall(
            r"(?<![A-Za-z0-9])([1-4]|[A-D])(?![A-Za-z0-9])",
            answer_part,
            re.I,
        )

        for number, answer in zip(
            question_numbers,
            answer_values,
        ):
            number = int(number)

            if 1 <= number <= 300:
                answers[number] = answer.upper()

    # Old format:
    # Q1 B
    # Q2 D
    # Q3 A

    old_pattern = re.compile(
        r"\bQ\s*(\d{1,3})\s*[\.\):\-]?\s*([A-D])\b",
        re.I,
    )

    for match in old_pattern.finditer(text):
        number = int(match.group(1))
        answer = match.group(2).upper()

        if 1 <= number <= 300:
            answers[number] = answer

    # Alternative old answer format:
    # 1. B
    # 2. C

    if not answers:
        alternative_pattern = re.compile(
            r"(?m)^\s*(\d{1,3})\s*[.)\-:]\s*"
            r"\(?([A-D])\)?\s*$",
            re.I,
        )

        for match in alternative_pattern.finditer(text):
            answers[int(match.group(1))] = match.group(2).upper()

    return answers


def answer_to_index(answer):
    if not answer:
        return None

    answer = str(answer).strip().upper()

    if answer in "ABCD" and len(answer) == 1:
        return ord(answer) - ord("A")

    if answer in ("1", "2", "3", "4"):
        return int(answer) - 1

    return None


# ================= QUESTION PARSER =================

def parse_questions(text):
    questions = []

    # Old format: Q1 Question...
    # New format: 1. Question...

    question_pattern = re.compile(
        r"(?im)^\s*(?:"
        r"Q\s*(\d{1,3})\s*"
        r"|"
        r"(\d{1,3})\s*[.)]\s+"
        r")"
    )

    question_matches = list(
        question_pattern.finditer(text)
    )

    # Supports:
    # (A), (B), (C), (D)
    # (1), (2), (3), (4)
    # A), B), C), D)

    option_pattern = re.compile(
        r"\(([A-D1-4])\)\s*"
        r"|(?<![A-Za-z0-9])([A-D])\)\s*",
        re.I,
    )

    for index, match in enumerate(question_matches):
        number = int(match.group(1) or match.group(2))

        start = match.end()

        if index + 1 < len(question_matches):
            end = question_matches[index + 1].start()
        else:
            end = len(text)

        content = text[start:end].strip()

        content = re.sub(
            r"Android App\s*\|.*",
            "",
            content,
            flags=re.I,
        )

        content = re.sub(
            r"iOS App|PW Website",
            "",
            content,
            flags=re.I,
        )

        # Remove answer-key content from question block.
        content = re.split(
            r"\bANSWER\s*KEY\b",
            content,
            maxsplit=1,
            flags=re.I,
        )[0]

        option_matches = list(
            option_pattern.finditer(content)
        )

        # Find the first complete A-D or 1-4 sequence.
        selected = []
        seen = set()

        for option_match in option_matches:
            raw = (
                option_match.group(1)
                or option_match.group(2)
            ).upper()

            if raw in "1234":
                label = "ABCD"[int(raw) - 1]
            else:
                label = raw

            if label in seen:
                continue

            if label not in "ABCD":
                continue

            # Keep option order as it appears in the PDF.
            selected.append((label, option_match))
            seen.add(label)

            if len(selected) == 4:
                break

        if len(selected) != 4:
            continue

        if set(label for label, _ in selected) != set("ABCD"):
            continue

        question_text = clean_text(
            content[:selected[0][1].start()]
        )

        options = {}

        for i, (label, option_match) in enumerate(selected):
            option_start = option_match.end()

            if i + 1 < len(selected):
                option_end = selected[i + 1][1].start()
            else:
                option_end = len(content)

            option_text = clean_text(
                content[option_start:option_end]
            )

            options[label] = option_text

        if not question_text:
            continue

        final_options = [
            options.get("A", ""),
            options.get("B", ""),
            options.get("C", ""),
            options.get("D", ""),
        ]

        # Telegram limits
        if len(question_text) > 300:
            log.warning("Question %s exceeds 300 characters", number)
            continue

        if any(
            not option or len(option) > 100
            for option in final_options
        ):
            log.warning("Invalid options in question %s", number)
            continue

        questions.append({
            "number": number,
            "question": question_text,
            "options": final_options,
        })

    return questions


# ================= EXERCISE DETECTION =================

EXERCISE_PATTERN = re.compile(
    r"(?im)^\s*EXERCISE\s*[-–—]?\s*"
    r"([IVXLCDM]+|\d+)\b[^\n]*"
)


def get_answer_key_position(text):
    return re.search(
        r"\bANSWER\s*KEY\b",
        text,
        re.I,
    )


def build_single_quiz(text):
    """
    Original PDF support.
    Questions before Answer Key, answers after Answer Key.
    """

    key_match = get_answer_key_position(text)

    if not key_match:
        return []

    question_text = text[:key_match.start()]
    answer_text = text[key_match.start():]

    questions = parse_questions(question_text)
    answers = parse_answer_key(answer_text)

    matched = []

    for question in questions:
        answer = answers.get(question["number"])
        correct = answer_to_index(answer)

        if correct is None:
            continue

        question["correct"] = correct
        matched.append(question)

    matched.sort(key=lambda q: q["number"])

    return matched


def build_exercises(pages):
    """
    Supports both:
    1. Old PDFs without exercise headings.
    2. New PDFs with separate EXERCISE-I, II, III sections.
    """

    full_text = "\n".join(pages)

    exercise_matches = list(
        EXERCISE_PATTERN.finditer(full_text)
    )

    exercises = []

    # Old PDF format: no exercise headings.
    if not exercise_matches:
        questions = build_single_quiz(full_text)

        if questions:
            exercises.append({
                "name": "All Questions",
                "questions": questions,
            })

        return exercises

    # New PDF format: split by exercise headings.
    for index, match in enumerate(exercise_matches):
        exercise_number = match.group(1).upper()

        heading = match.group(0).strip()

        start = match.end()

        if index + 1 < len(exercise_matches):
            end = exercise_matches[index + 1].start()
        else:
            end = len(full_text)

        section = full_text[start:end]

        key_match = get_answer_key_position(section)

        if not key_match:
            log.warning(
                "Answer Key not found for Exercise %s",
                exercise_number,
            )
            continue

        question_text = section[:key_match.start()]
        answer_text = section[key_match.start():]

        questions = parse_questions(question_text)
        answers = parse_answer_key(answer_text)

        matched = []

        for question in questions:
            answer = answers.get(question["number"])
            correct = answer_to_index(answer)

            if correct is None:
                continue

            question["correct"] = correct
            matched.append(question)

        matched.sort(key=lambda q: q["number"])

        if matched:
            exercises.append({
                "name": heading,
                "questions": matched,
            })

        log.info(
            "Exercise %s: questions=%s, answers=%s, matched=%s",
            exercise_number,
            len(questions),
            len(answers),
            len(matched),
        )

    return exercises


# ================= ACCESS CONTROL =================

def is_allowed(user_id):
    return OWNER_ID == 0 or user_id == OWNER_ID


# ================= COMMANDS =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    await update.message.reply_text(
        "📄 PDF QUIZ POLL MAKER\n\n"
        "NEET/JEE MCQ PDF bhejiye.\n\n"
        "Bot questions aur Answer Key read karke "
        "Telegram Quiz Polls banayega.\n\n"
        "/help - Help\n"
        "/cancel - Process cancel"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    await update.message.reply_text(
        "📚 PDF QUIZ BOT HELP\n\n"
        "Supported formats:\n\n"
        "Old format:\n"
        "• Q1, Q2, Q3...\n"
        "• (A), (B), (C), (D)\n"
        "• Q1 B, Q2 D...\n\n"
        "New format:\n"
        "• 1., 2., 3...\n"
        "• (1), (2), (3), (4)\n"
        "• Que. ... Ans. ...\n\n"
        "Exercise-wise selection supported.\n"
        "/cancel - Cancel process"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    context.user_data["cancelled"] = True
    context.user_data.pop("exercises", None)
    context.user_data.pop("waiting_selection", None)

    await update.message.reply_text(
        "❌ Cancellation requested."
    )


# ================= PDF HANDLER =================

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    document = message.document

    if not document:
        return

    if not is_allowed(update.effective_user.id):
        await message.reply_text(
            "This bot is restricted to its owner."
        )
        return

    if not (document.file_name or "").lower().endswith(".pdf"):
        await message.reply_text(
            "Please PDF file send karein."
        )
        return

    if document.file_size and document.file_size > MAX_PDF_MB * 1024 * 1024:
        await message.reply_text(
            f"PDF {MAX_PDF_MB} MB se chhoti honi chahiye."
        )
        return

    context.user_data["cancelled"] = False

    status = await message.reply_text(
        "⏳ PDF download aur analysis ho raha hai..."
    )

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf",
        ) as tmp:
            temp_path = tmp.name

        tg_file = await context.bot.get_file(document.file_id)

        await tg_file.download_to_drive(
            custom_path=temp_path
        )

        pages = extract_pdf_pages(temp_path)

        if context.user_data.get("cancelled"):
            await status.edit_text("❌ Process cancelled.")
            return

        exercises = build_exercises(pages)

        if not exercises:
            await status.edit_text(
                "❌ Questions aur Answer Key match nahi hue.\n\n"
                "Possible reasons:\n"
                "• Question format detect nahi hua.\n"
                "• Answer Key format detect nahi hua.\n"
                "• Questions aur answers match nahi hue."
            )
            return

        context.user_data["exercises"] = exercises
        context.user_data["waiting_selection"] = True

        total_questions = sum(
            len(ex["questions"])
            for ex in exercises
        )

        lines = ["📚 PDF Exercises Found\n"]

        for index, exercise in enumerate(exercises, start=1):
            lines.append(
                f"{index}. {exercise['name']} — "
                f"{len(exercise['questions'])} Questions"
            )

        lines.append("")
        lines.append(f"Total Exercises: {len(exercises)}")
        lines.append(f"Total Matched Questions: {total_questions}")
        lines.append("")
        lines.append("Exercise select karo:")

        for index in range(1, len(exercises) + 1):
            lines.append(str(index))

        all_option = len(exercises) + 1

        lines.append(
            f"{all_option}. All Exercises"
        )

        lines.append("")
        lines.append("Reply mein sirf number likho.")

        await status.edit_text("\n".join(lines))

    except Exception as error:
        log.exception("PDF processing error")

        await status.edit_text(
            f"❌ Error: {str(error)[:250]}"
        )

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


# ================= QUIZ SENDER =================

async def send_exercise_polls(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    selected_exercises,
):
    total = sum(
        len(ex["questions"])
        for ex in selected_exercises
    )

    progress = await update.message.reply_text(
        "🚀 Quiz banana start ho gaya!\n"
        f"Total Questions: {total}"
    )

    sent = 0
    failed = 0

    for exercise in selected_exercises:
        if context.user_data.get("cancelled"):
            break

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=(
                f"📚 {exercise['name']}\n"
                f"Questions: {len(exercise['questions'])}"
            ),
        )

        for question in exercise["questions"]:
            if context.user_data.get("cancelled"):
                break

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

            except TelegramError:
                failed += 1

                log.exception(
                    "Poll failed for Q%s",
                    question["number"],
                )

            if sent and sent % 5 == 0:
                try:
                    await progress.edit_text(
                        f"📤 Polls sent: {sent}/{total}\n"
                        f"❌ Failed: {failed}"
                    )
                except TelegramError:
                    pass

            await asyncio.sleep(POLL_DELAY)

    if context.user_data.get("cancelled"):
        await progress.edit_text(
            "⚠️ Process cancelled.\n\n"
            f"Polls sent: {sent}\n"
            f"Failed: {failed}"
        )
    else:
        await progress.edit_text(
            "🎉 Quiz conversion completed!\n\n"
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

    value = (update.message.text or "").strip()

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

    await send_exercise_polls(
        update,
        context,
        selected,
    )


# ================= MAIN =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing"
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("help", help_command)
    )

    app.add_handler(
        CommandHandler("cancel", cancel)
    )

    app.add_handler(
        MessageHandler(
            filters.Document.PDF,
            handle_pdf,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            selection_handler,
        )
    )

    log.info("PDF Quiz Poll Maker started")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
