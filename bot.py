
import os
import re
import asyncio
import logging
import tempfile
from pathlib import Path
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
    level=logging.INFO
)

log = logging.getLogger("pdf-quiz-poll-maker")


def clean_text(text):
    text = text.replace("\u00a0", " ")
    text = text.replace("\uf0b7", " ")
    text = text.replace("\u200b", "")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def extract_page_columns(page):
    """
    Extract PDF text column-wise.
    Left column first, then right column.
    """

    words = page.get_text("words", sort=False)

    lines = defaultdict(list)

    for word in words:
        x0, y0, x1, y1, text, block_no, line_no, word_no = word[:8]

        key = (block_no, line_no)

        lines[key].append({
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

    pages = []

    for page in doc:
        pages.append(extract_page_columns(page))

    doc.close()

    return pages


def parse_answer_key(text):

    answers = {}

    pattern = re.compile(
        r"\bQ\s*(\d{1,3})\s*[\.\):\-]?\s*([A-D])\b",
        re.IGNORECASE
    )

    for match in pattern.finditer(text):

        number = int(match.group(1))
        answer = match.group(2).upper()

        if 1 <= number <= 300:
            answers[number] = answer

    return answers


def parse_questions(text):

    questions = []

    question_pattern = re.compile(
        r"(?im)^\s*Q\s*(\d{1,3})\s*"
    )

    question_matches = list(
        question_pattern.finditer(text)
    )

    option_pattern = re.compile(
        r"\(([A-D])\)\s*",
        re.IGNORECASE
    )

    for index, match in enumerate(question_matches):

        number = int(match.group(1))

        start = match.end()

        if index + 1 < len(question_matches):
            end = question_matches[index + 1].start()
        else:
            end = len(text)

        content = text[start:end].strip()

        # Remove PDF footer text
        content = re.sub(
            r"Android App\s*\|.*",
            "",
            content,
            flags=re.IGNORECASE
        )

        content = re.sub(
            r"iOS App|PW Website",
            "",
            content,
            flags=re.IGNORECASE
        )

        option_matches = list(
            option_pattern.finditer(content)
        )

        if len(option_matches) < 4:
            continue

        question_text = content[:option_matches[0].start()]

        options = {}

        for i, option_match in enumerate(option_matches):

            letter = option_match.group(1).upper()

            option_start = option_match.end()

            if i + 1 < len(option_matches):
                option_end = option_matches[i + 1].start()
            else:
                option_end = len(content)

            option_text = content[option_start:option_end]

            option_text = clean_text(option_text)

            # Avoid duplicate option labels
            if letter not in options:
                options[letter] = option_text

        question_text = clean_text(question_text)

        if not all(x in options for x in "ABCD"):
            continue

        final_options = [
            options["A"],
            options["B"],
            options["C"],
            options["D"],
        ]

        # Telegram poll limits
        if not question_text:
            continue

        if len(question_text) > 300:
            log.warning(
                "Question %s exceeds Telegram limit",
                number
            )
            continue

        if any(
            not option or len(option) > 100
            for option in final_options
        ):
            log.warning(
                "Question %s has invalid options",
                number
            )
            continue

        questions.append({
            "number": number,
            "question": question_text,
            "options": final_options,
        })

    return questions


def build_quiz(pages):

    answer_page_index = None

    for index, page_text in enumerate(pages):

        if re.search(
            r"Answer\s*Key",
            page_text,
            re.IGNORECASE
        ):
            answer_page_index = index
            break

    if answer_page_index is None:
        return [], 0, 0

    # Questions before Answer Key only
    question_pages = pages[:answer_page_index]

    # Answer Key page
    answer_text = pages[answer_page_index]

    questions_text = "\n".join(question_pages)

    questions = parse_questions(questions_text)

    answers = parse_answer_key(answer_text)

    matched = []

    for question in questions:

        number = question["number"]

        answer = answers.get(number)

        if not answer:
            continue

        if answer not in "ABCD":
            continue

        question["correct"] = "ABCD".index(answer)

        matched.append(question)

    matched.sort(key=lambda q: q["number"])

    return matched, len(questions), len(answers)


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
        "📚 Supported PDF Format\n\n"
        "• Questions: Q1, Q2, Q3...\n"
        "• Options: (A), (B), (C), (D)\n"
        "• Answer Key: Q1 B, Q2 D...\n\n"
        "Two-column PDFs supported.\n"
        "Answer Key page questions ke baad honi chahiye."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    context.user_data["cancelled"] = True

    await update.message.reply_text(
        "❌ Cancellation requested."
    )


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

    context.user_data["cancelled"] = False

    status = await message.reply_text(
        "⏳ PDF download aur analysis ho raha hai..."
    )

    temp_path = None

    try:

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf"
        ) as tmp:

            temp_path = tmp.name

        tg_file = await context.bot.get_file(
            document.file_id
        )

        await tg_file.download_to_drive(
            custom_path=temp_path
        )

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
                "PDF format check karein."
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

        await status.edit_text(
            f"❌ Error: {str(error)[:250]}"
        )

    finally:

        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


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
            handle_pdf
        )
    )

    log.info("PDF Quiz Poll Maker started")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
