import os
import re
import asyncio
import logging
import tempfile
from collections import defaultdict

import fitz
from telegram import Update
from telegram.constants import PollType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
MAX_PDF_MB = int(os.getenv("MAX_PDF_MB", "20"))
POLL_DELAY = float(os.getenv("POLL_DELAY", "1.2"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

log = logging.getLogger("pdf-quiz-poll-maker")


# ---------------------------------------------------------
# TEXT EXTRACTION
# ---------------------------------------------------------

def clean_text(text):
    text = text.replace("\u00a0", " ")
    text = text.replace("\uf0b7", " ")
    text = text.replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_page_columns(page):
    """
    Extract left column first, then right column.
    This works for common two-column Allen PDFs where each
    PDF text line belongs to one column.
    """
    words = page.get_text("words", sort=False)
    lines = defaultdict(list)

    for word in words:
        x0, y0, x1, y1, word_text, block_no, line_no, word_no = word[:8]
        lines[(block_no, line_no)].append({
            "x": x0,
            "y": y0,
            "text": word_text,
        })

    left = []
    right = []
    middle = page.rect.width / 2

    for line_words in lines.values():
        line_words.sort(key=lambda item: item["x"])
        line_text = clean_text(" ".join(item["text"] for item in line_words))

        if not line_text:
            continue

        x = min(item["x"] for item in line_words)
        y = min(item["y"] for item in line_words)
        item = (y, x, line_text)

        if x < middle:
            left.append(item)
        else:
            right.append(item)

    left.sort(key=lambda item: (item[0], item[1]))
    right.sort(key=lambda item: (item[0], item[1]))

    output = [item[2] for item in left]

    if left and right:
        output.append("")

    output.extend(item[2] for item in right)
    return "\n".join(output)


def extract_pdf_pages(path):
    pages = []

    with fitz.open(path) as doc:
        for page in doc:
            pages.append(extract_page_columns(page))

    return pages


# ---------------------------------------------------------
# ANSWER KEY PARSER
# ---------------------------------------------------------

def parse_answer_values(answer_text):
    """
    Parse answer values such as:
    4 3 2 1 3
    1 2 3 4
    2322  -> 2, 3, 2, 2 (merged PDF extraction)
    A B C D
    """
    values = []

    for token in re.findall(r"[A-D]|\d+", answer_text, re.I):
        token = token.upper()

        if token in ("A", "B", "C", "D"):
            values.append(token)
        elif token.isdigit():
            if len(token) > 1 and all(char in "1234" for char in token):
                values.extend(list(token))
            else:
                values.append(token)

    converted = []

    for value in values:
        if value in ("1", "2", "3", "4"):
            converted.append("ABCD"[int(value) - 1])
        elif value in ("A", "B", "C", "D"):
            converted.append(value)

    return converted


def parse_answer_key(text):
    """
    Supports Allen table format:

    Que. 1 2 3 4 5
    Ans. 4 3 2 1 3

    Also supports legacy format:
    Q1 B
    Q2 D
    """
    answers = {}
    lines = [clean_text(line) for line in text.splitlines()]

    i = 0
    while i < len(lines):
        line = lines[i]

        if not re.match(r"^Que\.?\s*", line, re.I):
            i += 1
            continue

        question_part = re.sub(
            r"^Que\.?\s*", "", line, flags=re.I
        )
        question_numbers = [
            int(value) for value in re.findall(r"\d+", question_part)
        ]

        answer_part = None
        answer_line_index = None

        for j in range(i + 1, min(i + 5, len(lines))):
            if re.match(r"^Ans\.?\s*", lines[j], re.I):
                answer_part = re.sub(
                    r"^Ans\.?\s*", "", lines[j], flags=re.I
                )
                answer_line_index = j
                break

            # A new Que. row means this Que. row had no answer row.
            if re.match(r"^Que\.?\s*", lines[j], re.I):
                break

        if answer_part is not None:
            # Include wrapped answer values until next Que. row
            # or another exercise heading.
            for j in range(answer_line_index + 1, len(lines)):
                if re.match(r"^Que\.?\s*", lines[j], re.I):
                    break
                if re.match(r"^EXERCISE\s*[-–]\s*[IVX]+", lines[j], re.I):
                    break
                answer_part += " " + lines[j]

            answer_values = parse_answer_values(answer_part)

            for number, answer in zip(question_numbers, answer_values):
                if 1 <= number <= 300:
                    answers[number] = answer

        i += 1

    # Legacy answer format: Q1 B, Q2 D
    legacy_pattern = re.compile(
        r"\bQ\s*(\d{1,3})\s*[\.\):\-]?\s*([A-D])\b",
        re.I,
    )

    for match in legacy_pattern.finditer(text):
        number = int(match.group(1))
        if 1 <= number <= 300:
            answers[number] = match.group(2).upper()

    return answers


# ---------------------------------------------------------
# QUESTION PARSER
# ---------------------------------------------------------

def normalize_option_label(label):
    label = label.upper()

    if label in "1234":
        return "ABCD"[int(label) - 1]

    return label


def find_option_group(content):
    """
    Find four consecutive option markers in ABCD order.
    Supports both (1)(2)(3)(4) and (A)(B)(C)(D).
    Choosing the last matching group avoids mistaking
    numbered references inside a question for its options.
    """
    pattern = re.compile(r"\(\s*([1-4A-D])\s*\)", re.I)
    matches = list(pattern.finditer(content))

    valid_groups = []

    for start in range(max(0, len(matches) - 3)):
        group = matches[start:start + 4]

        if len(group) != 4:
            continue

        labels = [
            normalize_option_label(match.group(1))
            for match in group
        ]

        if labels == list("ABCD"):
            valid_groups.append(group)

    return valid_groups[-1] if valid_groups else None


def parse_questions(text):
    questions = []

    # Supports:
    # 1. Question text
    # Q1. Question text
    # Q1 Question text
    # Old Q1-style numbering and Allen 1. style numbering.
    question_pattern = re.compile(
        r"(?im)^\s*(?:Q\s*[-.]?\s*(\d{1,3})\s*\.?\s*|(\d{1,3})\.\s+)"
    )

    matches = list(question_pattern.finditer(text))

    for index, match in enumerate(matches):
        number = int(match.group(1) or match.group(2))
        start = match.end()

        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        content = text[start:end].strip()

        # Remove common PDF IDs and footer text.
        content = re.sub(
            r"\b[A-Z]{2,5}\d{3,}\b",
            "",
            content,
        )
        content = re.sub(
            r"Android App\s*\|.*|iOS App|PW Website",
            "",
            content,
            flags=re.I,
        )

        option_matches = find_option_group(content)

        if not option_matches:
            continue

        question_text = clean_text(
            content[:option_matches[0].start()]
        )

        options = {}

        for option_index, option_match in enumerate(option_matches):
            label = normalize_option_label(option_match.group(1))
            option_start = option_match.end()

            option_end = (
                option_matches[option_index + 1].start()
                if option_index + 1 < len(option_matches)
                else len(content)
            )

            option_text = clean_text(content[option_start:option_end])
            options[label] = option_text

        if not question_text:
            continue

        if not all(label in options for label in "ABCD"):
            continue

        final_options = [options[label] for label in "ABCD"]

        # Telegram limits: question <= 300, each option <= 100 chars.
        if len(question_text) > 300:
            log.warning(
                "Skipping Q%s: question exceeds Telegram's 300-char limit",
                number,
            )
            continue

        if any(not option or len(option) > 100 for option in final_options):
            log.warning(
                "Skipping Q%s: option is empty or exceeds 100 chars",
                number,
            )
            continue

        questions.append({
            "number": number,
            "question": question_text,
            "options": final_options,
        })

    return questions


# ---------------------------------------------------------
# EXERCISE-WISE QUIZ BUILDER
# ---------------------------------------------------------

ANSWER_HEADING_PATTERN = re.compile(
    r"(?im)^\s*EXERCISE\s*[-–]\s*[IVX]+\b"
    r"[^\n]*ANSWER\s*KEY[^\n]*"
)

EXERCISE_HEADING_PATTERN = re.compile(
    r"(?im)^\s*EXERCISE\s*[-–]\s*[IVX]+\b[^\n]*"
)


def build_quiz(pages):
    full_text = "\n".join(pages)

    key_matches = list(ANSWER_HEADING_PATTERN.finditer(full_text))

    if not key_matches:
        log.warning("No Exercise Answer Key heading found.")
        return [], 0, 0

    all_quizzes = []
    total_questions = 0
    total_answers = 0
    previous_section_start = 0

    for index, key_match in enumerate(key_matches):
        # Questions for this exercise are between the previous
        # exercise heading and this exercise's answer-key heading.
        question_text = full_text[
            previous_section_start:key_match.start()
        ]

        # End this answer-key section at the next exercise heading.
        next_exercise = EXERCISE_HEADING_PATTERN.search(
            full_text, key_match.end()
        )

        if next_exercise:
            answer_section_end = next_exercise.start()
            next_section_start = next_exercise.start()
        else:
            answer_section_end = len(full_text)
            next_section_start = len(full_text)

        answer_text = full_text[
            key_match.end():answer_section_end
        ]

        questions = parse_questions(question_text)
        answers = parse_answer_key(answer_text)

        total_questions += len(questions)
        total_answers += len(answers)

        heading = clean_text(key_match.group(0))

        log.info(
            "%s | questions=%s | answers=%s",
            heading,
            len(questions),
            len(answers),
        )

        matched_in_section = 0

        for question in questions:
            answer = answers.get(question["number"])

            if not answer:
                continue

            question["correct"] = "ABCD".index(answer)
            all_quizzes.append(question)
            matched_in_section += 1

        log.info(
            "%s | matched=%s",
            heading,
            matched_in_section,
        )

        previous_section_start = next_section_start

    # Preserve exercise order; do not globally sort because
    # question numbering restarts from 1 in each exercise.
    return all_quizzes, total_questions, total_answers


# ---------------------------------------------------------
# TELEGRAM COMMANDS
# ---------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 PDF QUIZ POLL MAKER\n\n"
        "NEET/JEE MCQ PDF bhejiye.\n\n"
        "Bot PDF se questions aur Answer Key read karke "
        "Telegram Quiz Polls banayega.\n\n"
        "/help - Help\n"
        "/cancel - Process cancel"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Supported PDF Formats\n\n"
        "• Questions: Q1, Q2... or 1., 2., 3...\n"
        "• Options: (A)-(D) or (1)-(4)\n"
        "• Answer Key: Q1 B or Que. / Ans. table\n\n"
        "✅ Exercise-wise Answer Key matching\n"
        "✅ Allen-style answer key tables\n"
        "✅ Hindi and English text PDFs\n"
        "✅ Common two-column PDFs\n\n"
        "/cancel - Cancel process"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cancelled"] = True
    await update.message.reply_text("❌ Cancellation requested.")


# ---------------------------------------------------------
# PDF HANDLER
# ---------------------------------------------------------

async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    document = message.document

    if not document:
        return

    if not (document.file_name or "").lower().endswith(".pdf"):
        await message.reply_text("Please PDF file send karein.")
        return

    if document.file_size and document.file_size > MAX_PDF_MB * 1024 * 1024:
        await message.reply_text(
            f"PDF {MAX_PDF_MB} MB se chhoti honi chahiye."
        )
        return

    if OWNER_ID and update.effective_user.id != OWNER_ID:
        await message.reply_text(
            "This bot is restricted to its owner."
        )
        return

    # Prevent two simultaneous PDF jobs from sharing the same user state.
    if context.user_data.get("processing"):
        await message.reply_text(
            "⏳ Aapki ek PDF pehle se process ho rahi hai."
        )
        return

    context.user_data["processing"] = True
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
        await tg_file.download_to_drive(custom_path=temp_path)

        if context.user_data.get("cancelled"):
            await status.edit_text("❌ Process cancelled.")
            return

        pages = extract_pdf_pages(temp_path)

        if context.user_data.get("cancelled"):
            await status.edit_text("❌ Process cancelled.")
            return

        quizzes, total_found, key_count = build_quiz(pages)

        if not quizzes:
            await status.edit_text(
                "❌ Questions aur Answer Key match nahi hue.\n\n"
                f"Questions detected: {total_found}\n"
                f"Answer Key entries: {key_count}\n\n"
                "PDF format check karein ya logs share karein."
            )
            return

        await status.edit_text(
            f"✅ {len(quizzes)} questions match hue.\n"
            "📤 Quiz polls bheje ja rahe hain..."
        )

        sent = 0
        failed = 0

        for question in quizzes:
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

            except Exception:
                failed += 1
                log.exception(
                    "Poll failed for Q%s",
                    question["number"],
                )

            if sent and sent % 5 == 0:
                try:
                    await status.edit_text(
                        f"📤 Polls sent: {sent}/{len(quizzes)}\n"
                        f"❌ Failed: {failed}"
                    )
                except Exception:
                    pass

            await asyncio.sleep(POLL_DELAY)

        if context.user_data.get("cancelled"):
            await status.edit_text(
                "⚠️ Process cancelled.\n\n"
                f"Polls sent: {sent}\n"
                f"Failed: {failed}"
            )
        else:
            await status.edit_text(
                "🎉 Quiz conversion completed!\n\n"
                f"Questions detected: {total_found}\n"
                f"Answer Key entries: {key_count}\n"
                f"Matched questions: {len(quizzes)}\n"
                f"Polls sent: {sent}\n"
                f"Failed: {failed}"
            )

    except Exception as error:
        log.exception("PDF processing error")

        try:
            await status.edit_text(
                f"❌ Error: {str(error)[:250]}"
            )
        except Exception:
            pass

    finally:
        context.user_data["processing"] = False

        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                log.warning("Could not remove temporary PDF: %s", temp_path)


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing"
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(
        MessageHandler(
            filters.Document.PDF,
            handle_pdf,
        )
    )

    log.info("PDF Quiz Poll Maker started")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
