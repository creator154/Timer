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
POLL_DELAY = max(0.0, float(os.getenv("POLL_DELAY", "1.2")))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("pdf-quiz-poll-maker")


# ---------------------------------------------------------
# TEXT EXTRACTION
# ---------------------------------------------------------

def clean_text(text):
    text = (text or "").replace("\u00a0", " ")
    text = text.replace("\uf0b7", " ").replace("\u200b", "").replace("\xad", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_page_columns(page):
    """Extract left column first, then right column."""
    words = page.get_text("words", sort=False)
    lines = defaultdict(list)

    for word in words:
        x0, y0, x1, y1, word_text, block_no, line_no, word_no = word[:8]
        lines[(block_no, line_no)].append({
            "x": x0, "y": y0, "text": word_text
        })

    left, right = [], []
    middle = page.rect.width / 2

    for line_words in lines.values():
        line_words.sort(key=lambda item: item["x"])
        line_text = clean_text(" ".join(item["text"] for item in line_words))
        if not line_text:
            continue

        x = min(item["x"] for item in line_words)
        y = min(item["y"] for item in line_words)
        item = (y, x, line_text)
        (left if x < middle else right).append(item)

    left.sort(key=lambda item: (item[0], item[1]))
    right.sort(key=lambda item: (item[0], item[1]))

    output = [item[2] for item in left]
    if left and right:
        output.append("")
    output.extend(item[2] for item in right)
    return "\n".join(output)


def extract_pdf_pages(path):
    with fitz.open(path) as doc:
        return [extract_page_columns(page) for page in doc]


# ---------------------------------------------------------
# HEADING PATTERNS
# ---------------------------------------------------------

# Hyphen optional: EXERCISE I, EXERCISE-II, EXERCISE-III(A)
EXERCISE_HEADING_PATTERN = re.compile(
    r"(?im)^\s*EXERCISE\s*[-–]?\s*[IVX]+\b[^\n]*"
)

# Allen PDFs often print the exercise title and ANSWER KEY on separate lines:
#   EXERCISE-III
#   ANSWER KEY
# Also supports a single-line heading such as "EXERCISE I ANSWER KEY".
ANSWER_HEADING_PATTERN = re.compile(
    r"(?im)^\s*(?:EXERCISE\s*[-–]?\s*[IVX]+\b[^\n]*"
    r"(?:\n[ \t]*\n?)*"
    r")?ANSWER\s*KEY[^\n]*"
)

EXERCISE_LINE_PATTERN = re.compile(
    r"(?i)^\s*EXERCISE\s*[-–]?\s*[IVX]+\b"
)


# ---------------------------------------------------------
# ANSWER KEY PARSER
# ---------------------------------------------------------

def parse_answer_values(answer_text):
    """Parse A-D and 1-4 values, including merged values such as 2322."""
    values = []
    for token in re.findall(r"[A-D]|\d+", answer_text or "", re.I):
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
    """Parse Allen Que./Ans. tables even when values wrap onto next lines."""
    answers = {}
    lines = [clean_text(line) for line in (text or "").splitlines()]

    def numeric_tokens(value):
        # Ignore labels/punctuation; retain only plausible question numbers.
        return [int(x) for x in re.findall(r"\d{1,3}", value or "")]

    def answer_tokens(value):
        return parse_answer_values(value)

    i = 0
    while i < len(lines):
        line = lines[i]
        if not re.match(r"^\s*Que\.?(?:\s|$)", line, re.I):
            i += 1
            continue

        question_parts = [re.sub(r"^\s*Que\.?\s*", "", line, flags=re.I)]
        j = i + 1
        # The question-number row may be split over several lines before Ans.
        while j < len(lines) and not re.match(r"^\s*Ans\.?\s*", lines[j], re.I):
            if re.match(r"^\s*Que\.?(?:\s|$)", lines[j], re.I):
                break
            if EXERCISE_LINE_PATTERN.match(lines[j]):
                break
            if re.search(r"ANSWER\s*KEY|EXPLANATION|SOLUTION", lines[j], re.I):
                break
            question_parts.append(lines[j])
            j += 1

        if j >= len(lines) or not re.match(r"^\s*Ans\.?\s*", lines[j], re.I):
            i += 1
            continue

        question_numbers = numeric_tokens(" ".join(question_parts))
        answer_parts = [re.sub(r"^\s*Ans\.?\s*", "", lines[j], flags=re.I)]
        k = j + 1
        # Some PDFs wrap the answer row; only accept lines made of answer-like tokens.
        while k < len(lines):
            current = lines[k]
            if re.match(r"^\s*(?:Que|Ans)\.?\s*", current, re.I):
                break
            if EXERCISE_LINE_PATTERN.match(current) or re.search(
                r"ANSWER\s*KEY|EXPLANATION|SOLUTION", current, re.I
            ):
                break
            if not re.fullmatch(r"[\s,;|/()\[\]{}A-Da-d1-4.-]*", current):
                break
            if current.strip():
                answer_parts.append(current)
            k += 1

        values = answer_tokens(" ".join(answer_parts))
        for number, answer in zip(question_numbers, values):
            if 1 <= number <= 300:
                answers[number] = answer
        i = max(j + 1, k)

    # Legacy answer-key format: Q1 B / Q2 D.
    legacy_pattern = re.compile(
        r"(?im)^\s*Q\s*(\d{1,3})\s*[.):\-]?\s*([A-D])\s*$"
    )
    for match in legacy_pattern.finditer(text or ""):
        number = int(match.group(1))
        if 1 <= number <= 300:
            answers[number] = match.group(2).upper()

    return answers


# ---------------------------------------------------------
# QUESTION PARSER
# ---------------------------------------------------------

def normalize_option_label(label):
    label = label.upper()
    return "ABCD"[int(label) - 1] if label in "1234" else label


def find_option_group(content):
    """Find the last consecutive (1)-(4) or (A)-(D) marker group."""
    pattern = re.compile(r"\(\s*([1-4A-D])\s*\)", re.I)
    matches = list(pattern.finditer(content))
    valid_groups = []

    for start in range(max(0, len(matches) - 3)):
        group = matches[start:start + 4]
        if len(group) != 4:
            continue
        labels = [normalize_option_label(m.group(1)) for m in group]
        if labels == list("ABCD"):
            valid_groups.append(group)

    return valid_groups[-1] if valid_groups else None


def parse_questions(text):
    questions = []
    question_pattern = re.compile(
        r"(?im)^\s*(?:Q\s*[-.]?\s*(\d{1,3})\s*\.?\s*|(\d{1,3})\.\s+)"
    )
    matches = list(question_pattern.finditer(text or ""))

    for index, match in enumerate(matches):
        number = int(match.group(1) or match.group(2))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = (text[start:end] or "").strip()

        content = re.sub(r"\b[A-Z]{2,5}\d{3,}\b", "", content)
        content = re.sub(
            r"Android App\s*\|.*|iOS App|PW Website",
            "", content, flags=re.I
        )

        option_matches = find_option_group(content)
        if not option_matches:
            continue

        question_text = clean_text(content[:option_matches[0].start()])
        options = {}

        for option_index, option_match in enumerate(option_matches):
            label = normalize_option_label(option_match.group(1))
            option_start = option_match.end()
            option_end = (
                option_matches[option_index + 1].start()
                if option_index + 1 < len(option_matches)
                else len(content)
            )
            options[label] = clean_text(content[option_start:option_end])

        if not question_text or not all(label in options for label in "ABCD"):
            continue

        final_options = [options[label] for label in "ABCD"]
        if len(question_text) > 300:
            log.warning(
                "Q%s question was %s characters; shortening to Telegram's 300-character limit",
                number, len(question_text)
            )
            question_text = question_text[:297].rsplit(" ", 1)[0].rstrip() + "..."
        if any(not option or len(option) > 100 for option in final_options):
            log.warning("Skipping Q%s: option empty or exceeds 100 characters", number)
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

def build_quiz(pages):
    full_text = "\n".join(pages)
    key_matches = list(ANSWER_HEADING_PATTERN.finditer(full_text))

    if not key_matches:
        log.warning("No Exercise Answer Key heading found.")
        for line in full_text.splitlines():
            if "EXERCISE" in line.upper() or "ANSWER KEY" in line.upper():
                log.info("PDF heading candidate: %s", clean_text(line)[:180])
        return [], 0, 0

    all_quizzes = []
    total_questions = 0
    total_answers = 0
    previous_section_start = 0

    for key_match in key_matches:
        question_text = full_text[previous_section_start:key_match.start()]
        next_exercise = EXERCISE_HEADING_PATTERN.search(
            full_text, key_match.end()
        )

        if next_exercise:
            answer_section_end = next_exercise.start()
            next_section_start = next_exercise.start()
        else:
            answer_section_end = len(full_text)
            next_section_start = len(full_text)

        answer_text = full_text[key_match.end():answer_section_end]
        questions = parse_questions(question_text)
        answers = parse_answer_key(answer_text)

        total_questions += len(questions)
        total_answers += len(answers)

        heading = clean_text(key_match.group(0))
        log.info(
            "%s | questions=%s | answers=%s",
            heading, len(questions), len(answers)
        )

        matched = 0
        for question in questions:
            answer = answers.get(question["number"])
            if not answer:
                continue
            question["correct"] = "ABCD".index(answer)
            all_quizzes.append(question)
            matched += 1

        log.info("%s | matched=%s", heading, matched)
        previous_section_start = next_section_start

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
        await message.reply_text(f"PDF {MAX_PDF_MB} MB se chhoti honi chahiye.")
        return

    if OWNER_ID and update.effective_user.id != OWNER_ID:
        await message.reply_text("This bot is restricted to its owner.")
        return

    if context.user_data.get("processing"):
        await message.reply_text("⏳ Aapki ek PDF pehle se process ho rahi hai.")
        return

    context.user_data["processing"] = True
    context.user_data["cancelled"] = False
    status = await message.reply_text("⏳ PDF download aur analysis ho raha hai...")
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
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
                "Agar PDF scanned/image-based hai to OCR ki zaroorat ho sakti hai. "
                "Logs share karein."
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
                log.exception("Poll failed for Q%s", question["number"])

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
                f"Polls sent: {sent}\nFailed: {failed}"
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
            await status.edit_text(f"❌ Error: {str(error)[:250]}")
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
        raise RuntimeError("BOT_TOKEN environment variable missing")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))

    log.info("PDF Quiz Poll Maker started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
