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
# COMMON FUNCTIONS
# ---------------------------------------------------------

def clean_text(text):
    text = (text or "").replace("\u00a0", " ")
    text = text.replace("\uf0b7", " ").replace("\u200b", "").replace("\xad", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def is_owner(update):
    return not OWNER_ID or update.effective_user.id == OWNER_ID


def extract_page_columns(page):
    words = page.get_text("words", sort=False)
    lines = defaultdict(list)

    for word in words:
        x0, y0, x1, y1, word_text, block_no, line_no, word_no = word[:8]
        lines[(block_no, line_no)].append({
            "x": x0,
            "y": y0,
            "text": word_text
        })

    left, right = [], []
    middle = page.rect.width / 2

    for line_words in lines.values():
        line_words.sort(key=lambda item: item["x"])
        line_text = clean_text(" ".join(x["text"] for x in line_words))

        if not line_text:
            continue

        x = min(item["x"] for item in line_words)
        y = min(item["y"] for item in line_words)
        item = (y, x, line_text)

        (left if x < middle else right).append(item)

    left.sort(key=lambda x: (x[0], x[1]))
    right.sort(key=lambda x: (x[0], x[1]))

    output = [x[2] for x in left]

    if left and right:
        output.append("")

    output.extend(x[2] for x in right)
    return "\n".join(output)


def extract_pdf_pages(path):
    with fitz.open(path) as doc:
        return [extract_page_columns(page) for page in doc]


# ---------------------------------------------------------
# PDF ANSWER KEY PARSER
# ---------------------------------------------------------

EXERCISE_HEADING_PATTERN = re.compile(
    r"(?im)^[ \t]*EXERCISE\s*[-–]?\s*[IVX]+\b[^\n]*"
)

ANSWER_HEADING_PATTERN = re.compile(
    r"(?im)^[ \t]*(?:"
    r"EXERCISE\s*[-–]?\s*[IVX]+\b[^\n]*?\bANSWER\s*KEY[^\n]*"
    r"|ANSWER\s*KEY[^\n]*"
    r")[ \t]*$"
)

EXERCISE_LINE_PATTERN = re.compile(
    r"(?i)^[ \t]*EXERCISE\s*[-–]?\s*[IVX]+\b"
)


def parse_answer_values(answer_text):
    values = []

    for token in re.findall(r"[A-D]|\d+", answer_text or "", re.I):
        token = token.upper()

        if token in ("A", "B", "C", "D"):
            values.append(token)

        elif token.isdigit():
            if len(token) > 1 and all(c in "1234" for c in token):
                values.extend(list(token))
            elif token in ("1", "2", "3", "4"):
                values.append(token)

    return [
        "ABCD"[int(x) - 1] if x in "1234" else x
        for x in values
    ]


def parse_answer_key(text):
    answers = {}
    lines = [clean_text(x) for x in (text or "").splitlines()]

    def numeric_tokens(value):
        return [int(x) for x in re.findall(r"\d{1,3}", value or "")]

    i = 0

    while i < len(lines):
        line = lines[i]

        if not re.match(r"^\s*Que\.?\s*", line, re.I):
            i += 1
            continue

        question_parts = [
            re.sub(r"^\s*Que\.?\s*", "", line, flags=re.I)
        ]

        j = i + 1

        while j < len(lines) and not re.match(
            r"^\s*Ans\.?\s*", lines[j], re.I
        ):
            if re.match(r"^\s*Que\.?\s*", lines[j], re.I):
                break

            if EXERCISE_LINE_PATTERN.match(lines[j]):
                break

            if re.search(r"ANSWER\s*KEY|EXPLANATION|SOLUTION", lines[j], re.I):
                break

            question_parts.append(lines[j])
            j += 1

        if j >= len(lines) or not re.match(
            r"^\s*Ans\.?\s*", lines[j], re.I
        ):
            i += 1
            continue

        question_numbers = numeric_tokens(" ".join(question_parts))

        answer_parts = [
            re.sub(r"^\s*Ans\.?\s*", "", lines[j], flags=re.I)
        ]

        k = j + 1

        while k < len(lines):
            current = lines[k]

            if re.match(r"^\s*(?:Que|Ans)\.?\s*", current, re.I):
                break

            if EXERCISE_LINE_PATTERN.match(current):
                break

            if re.search(r"ANSWER\s*KEY|EXPLANATION|SOLUTION", current, re.I):
                break

            if not re.fullmatch(
                r"[\s,;|/()\[\]{}A-Da-d1-4.-]*", current
            ):
                break

            if current.strip():
                answer_parts.append(current)

            k += 1

        values = parse_answer_values(" ".join(answer_parts))

        for number, answer in zip(question_numbers, values):
            if 1 <= number <= 300:
                answers[number] = answer

        i = max(j + 1, k)

    legacy_pattern = re.compile(
        r"(?i)\bQ\s*(\d{1,3})\s*[.):\-]?\s*([A-D])\b"
    )

    for match in legacy_pattern.finditer(text or ""):
        number = int(match.group(1))

        if 1 <= number <= 300:
            answers[number] = match.group(2).upper()

    return answers


# ---------------------------------------------------------
# PDF QUESTION PARSER
# ---------------------------------------------------------

def normalize_option_label(label):
    label = label.upper()
    return "ABCD"[int(label) - 1] if label in "1234" else label


def find_option_group(content):
    pattern = re.compile(r"\(\s*([1-4A-D])\s*\)", re.I)
    matches = list(pattern.finditer(content))
    valid_groups = []

    for start in range(max(0, len(matches) - 3)):
        group = matches[start:start + 4]

        if len(group) != 4:
            continue

        labels = [
            normalize_option_label(m.group(1))
            for m in group
        ]

        if labels == list("ABCD"):
            valid_groups.append(group)

    return valid_groups[-1] if valid_groups else None


def parse_questions(text):
    questions = []

    question_pattern = re.compile(
        r"(?im)^[ \t]*(?:Q\s*[-.]?\s*(\d{1,3})\s*\.?\s*|(\d{1,3})\.\s+)"
    )

    matches = list(question_pattern.finditer(text or ""))

    for index, match in enumerate(matches):
        number = int(match.group(1) or match.group(2))
        start = match.end()

        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        content = (text[start:end] or "").strip()

        content = re.sub(r"\b[A-Z]{2,5}\d{3,}\b", "", content)

        content = re.sub(
            r"Android App\s*\|.*|iOS App|PW Website",
            "",
            content,
            flags=re.I
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

            options[label] = clean_text(
                content[option_start:option_end]
            )

        if not question_text or not all(
            label in options for label in "ABCD"
        ):
            continue

        final_options = [options[x] for x in "ABCD"]

        if len(question_text) > 300:
            question_text = question_text[:297].rsplit(" ", 1)[0] + "..."

        if any(not x or len(x) > 100 for x in final_options):
            log.warning(
                "Skipping PDF Q%s: option empty or exceeds 100 characters",
                number
            )
            continue

        questions.append({
            "number": number,
            "question": question_text,
            "options": final_options,
        })

    return questions


# ---------------------------------------------------------
# DIRECT TEXT MCQ PARSER
# ---------------------------------------------------------

def parse_text_questions(text):
    text = clean_text(text or "")

    question_pattern = re.compile(
        r"(?m)^[ \t]*(\d{1,3})[.)][ \t]+"
    )

    matches = list(question_pattern.finditer(text))
    quizzes = []

    option_pattern = re.compile(
        r"(?m)^[ \t]*([A-D])[).][ \t]*(.*)$",
        re.I
    )

    for index, match in enumerate(matches):
        number = int(match.group(1))

        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(text)
        )

        block = text[match.end():end].strip()
        option_matches = list(option_pattern.finditer(block))

        if len(option_matches) != 4:
            log.warning(
                "Text Q%s skipped: found %s options",
                number,
                len(option_matches)
            )
            continue

        labels = [m.group(1).upper() for m in option_matches]

        if labels != list("ABCD"):
            continue

        question_text = clean_text(
            block[:option_matches[0].start()]
        )

        options = []
        correct_indexes = []

        for option_index, option_match in enumerate(option_matches):
            option_end = (
                option_matches[option_index + 1].start()
                if option_index + 1 < len(option_matches)
                else len(block)
            )

            option_text = clean_text(
                block[option_match.start(2):option_end]
            )

            if "✅" in option_text:
                correct_indexes.append(option_index)

            option_text = clean_text(
                option_text.replace("✅", "")
            )

            options.append(option_text)

        if not question_text:
            continue

        if len(question_text) > 300:
            question_text = (
                question_text[:297].rsplit(" ", 1)[0] + "..."
            )

        if any(not x or len(x) > 100 for x in options):
            log.warning("Text Q%s skipped: invalid option length", number)
            continue

        if len(correct_indexes) != 1:
            log.warning(
                "Text Q%s skipped: exactly one ✅ required",
                number
            )
            continue

        quizzes.append({
            "number": number,
            "question": question_text,
            "options": options,
            "correct": correct_indexes[0],
        })

    return quizzes, len(matches)


# ---------------------------------------------------------
# SEND TELEGRAM POLLS
# ---------------------------------------------------------

async def send_quiz_polls(
    update,
    context,
    quizzes,
    status,
    source_label="Text"
):
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
                question["number"]
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
            f"⚠️ Process cancelled.\n\n"
            f"Polls sent: {sent}\nFailed: {failed}"
        )
    else:
        await status.edit_text(
            f"🎉 {source_label} conversion completed!\n\n"
            f"Questions: {len(quizzes)}\n"
            f"Polls sent: {sent}\n"
            f"Failed: {failed}"
        )


# ---------------------------------------------------------
# DIRECT TEXT HANDLER
# ---------------------------------------------------------

async def handle_text(update, context):
    message = update.message

    if not message or not message.text:
        return

    if not is_owner(update):
        await message.reply_text(
            "This bot is restricted to its owner."
        )
        return

    if context.user_data.get("processing"):
        await message.reply_text(
            "⏳ Aapka ek quiz pehle se process ho raha hai."
        )
        return

    quizzes, detected = parse_text_questions(message.text)

    if not quizzes:
        await message.reply_text(
            "❌ Koi valid MCQ nahi mila.\n\n"
            "Format:\n"
            "1. Question\n"
            "A) Option\n"
            "B) Option\n"
            "C) Option ✅\n"
            "D) Option\n\n"
            "Har question mein exactly ek option par ✅ lagayein."
        )
        return

    context.user_data["processing"] = True
    context.user_data["cancelled"] = False

    status = await message.reply_text(
        f"✅ {len(quizzes)} questions ready.\n"
        f"Detected blocks: {detected}\n"
        "📤 Polls bheje ja rahe hain..."
    )

    try:
        await send_quiz_polls(
            update,
            context,
            quizzes,
            status,
            "Text"
        )

        skipped = detected - len(quizzes)

        if skipped:
            await message.reply_text(
                f"⚠️ {skipped} questions skip hue.\n"
                "Exactly 4 options aur ek ✅ correct option hona chahiye."
            )

    finally:
        context.user_data["processing"] = False
# ---------------------------------------------------------
# PDF HANDLER
# ---------------------------------------------------------

async def handle_pdf(update, context):
    message = update.message

    if not message or not message.document:
        return

    if not is_owner(update):
        await message.reply_text(
            "This bot is restricted to its owner."
        )
        return

    if context.user_data.get("processing"):
        await message.reply_text(
            "⏳ Aapka ek quiz pehle se process ho raha hai."
        )
        return

    document = message.document

    if not document.file_name.lower().endswith(".pdf"):
        await message.reply_text("❌ Please send a PDF file.")
        return

    max_size = MAX_PDF_MB * 1024 * 1024

    if document.file_size and document.file_size > max_size:
        await message.reply_text(
            f"❌ PDF size {MAX_PDF_MB} MB se kam honi chahiye."
        )
        return

    context.user_data["processing"] = True
    context.user_data["cancelled"] = False

    status = await message.reply_text(
        "📥 PDF download ho rahi hai..."
    )

    temp_path = None

    try:
        telegram_file = await context.bot.get_file(document.file_id)

        with tempfile.NamedTemporaryFile(
            suffix=".pdf",
            delete=False
        ) as temp:
            temp_path = temp.name

        await telegram_file.download_to_drive(temp_path)

        if os.path.getsize(temp_path) > max_size:
            await status.edit_text("❌ PDF size limit exceed.")
            return

        await status.edit_text(
            "📖 PDF read ho rahi hai..."
        )

        pages = extract_pdf_pages(temp_path)
        full_text = "\n".join(pages)

        questions = parse_questions(full_text)
        answers = parse_answer_key(full_text)

        quizzes = []

        for question in questions:
            answer = answers.get(question["number"])

            if not answer or answer not in "ABCD":
                continue

            quizzes.append({
                "number": question["number"],
                "question": question["question"],
                "options": question["options"],
                "correct": "ABCD".index(answer),
            })

        if not quizzes:
            await status.edit_text(
                "❌ PDF se valid questions aur answers match nahi hue.\n\n"
                f"Questions extracted: {len(questions)}\n"
                f"Answers extracted: {len(answers)}\n\n"
                "Heroku logs check karein."
            )
            return

        await status.edit_text(
            f"✅ PDF parsed successfully!\n\n"
            f"Questions: {len(questions)}\n"
            f"Answers: {len(answers)}\n"
            f"Matched: {len(quizzes)}\n\n"
            "📤 Polls bheje ja rahe hain..."
        )

        await send_quiz_polls(
            update,
            context,
            quizzes,
            status,
            "PDF"
        )

    except Exception as error:
        log.exception("PDF processing error")
        try:
            await status.edit_text(
                f"❌ PDF processing error:\n{str(error)[:2500]}"
            )
        except Exception:
            pass

    finally:
        context.user_data["processing"] = False

        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


# ---------------------------------------------------------
# COMMANDS
# ---------------------------------------------------------

async def start(update, context):
    if not is_owner(update):
        await update.message.reply_text(
            "This bot is restricted to its owner."
        )
        return

    await update.message.reply_text(
        "🚀 PDF TO QUIZ POLL MAKER\n\n"
        "Bot PDF aur direct text dono se Quiz Poll bana sakta hai.\n\n"
        "📄 PDF: Apni MCQ PDF bhejein.\n"
        "📝 TEXT: MCQ text bhejein, correct option par ✅ lagayein.\n\n"
        "/help - Help\n"
        "/cancel - Stop current process\n\n"
        "Powered by @SumitTripathi"
    )


async def help_command(update, context):
    await update.message.reply_text(
        "📚 HOW TO USE\n\n"
        "1. PDF bhejein: Bot answer key se correct answers match karega.\n\n"
        "2. Direct Text bhejein:\n\n"
        "1. Question?\n"
        "A) Option 1\n"
        "B) Option 2\n"
        "C) Option 3 ✅\n"
        "D) Option 4\n\n"
        "Har question mein exactly ek ✅ hona chahiye.\n\n"
        "Commands:\n"
        "/start - Start bot\n"
        "/help - Help\n"
        "/cancel - Cancel process"
    )


async def cancel(update, context):
    if not is_owner(update):
        return

    if context.user_data.get("processing"):
        context.user_data["cancelled"] = True
        await update.message.reply_text(
            "⚠️ Cancellation request received."
        )
    else:
        await update.message.reply_text(
            "Koi process currently running nahi hai."
        )


# ---------------------------------------------------------
# ERROR HANDLER
# ---------------------------------------------------------

async def error_handler(update, context):
    log.error(
        "Telegram update error",
        exc_info=context.error
    )


# ---------------------------------------------------------
# MAIN
# ---------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel))

    application.add_handler(
        MessageHandler(
            filters.Document.PDF,
            handle_pdf
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_text
        )
    )

    application.add_error_handler(error_handler)

    log.info("Bot starting...")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
