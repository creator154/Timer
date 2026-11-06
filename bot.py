import os
import re
import logging
import tempfile
from pathlib import Path

import fitz
from telegram import Update
from telegram.constants import PollType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
MAX_PDF_MB = int(os.getenv("MAX_PDF_MB", "20"))
POLL_DELAY = float(os.getenv("POLL_DELAY", "1.2"))
OCR_ENABLED = os.getenv("OCR_ENABLED", "false").lower() == "true"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
log = logging.getLogger("pdf-quiz-poll-maker")

# Supports common answer-key styles: 1-A, 1. A, 1) (A), Q1: A, etc.
ANSWER_PATTERNS = [
    re.compile(r"(?im)(?:^|[\s,;])(?:Q\s*)?(\d{1,3})\s*[\.\):\-]\s*[\(\[]?([A-D])[\)\]]?(?=\s|[,;]|$)"),
    re.compile(r"(?im)(?:^|[\s,;])(?:Q\s*)?(\d{1,3})\s+[\(\[]?([A-D])[\)\]]?(?=\s|[,;]|$)"),
]

QUESTION_START = re.compile(r"(?im)^\s*(?:Q(?:uestion)?\s*)?(\d{1,3})\s*[\.\)]\s*(.*)$")
OPTION_START = re.compile(r"(?im)^\s*(?:\(([A-D])\)|([A-D])[\.\)])\s*(.*)$")


def clean_text(value: str) -> str:
    value = value.replace("\u00a0", " ").replace("\uf0b7", " ")
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def extract_pdf_pages(path: str):
    doc = fitz.open(path)
    pages = [clean_text(page.get_text("text")) for page in doc]
    if OCR_ENABLED and sum(len(p) for p in pages) < 100:
        try:
            import pytesseract
            from PIL import Image
            import io
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                image = Image.open(io.BytesIO(pix.tobytes("png")))
                pages[i] = clean_text(pytesseract.image_to_string(image))
        except Exception:
            log.exception("OCR failed; continuing with embedded PDF text.")
    doc.close()
    return pages


def parse_answer_key(text: str):
    answers = {}
    for pattern in ANSWER_PATTERNS:
        for match in pattern.finditer(text):
            qno = int(match.group(1))
            letter = match.group(2).upper()
            if 1 <= qno <= 300:
                answers[qno] = letter
    return answers


def parse_questions(text: str):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    questions = []
    current = None
    current_option = None

    def finish():
        nonlocal current, current_option
        if not current:
            return
        if current_option:
            current["options"].append(current_option)
        if len(current["options"]) == 4 and current["question"].strip():
            questions.append(current)
        current = None
        current_option = None

    for line in lines:
        qmatch = QUESTION_START.match(line)
        omatch = OPTION_START.match(line)

        if qmatch:
            finish()
            current = {
                "number": int(qmatch.group(1)),
                "question": qmatch.group(2).strip(),
                "options": []
            }
            continue

        if current and omatch:
            if current_option:
                current["options"].append(current_option)
            letter = (omatch.group(1) or omatch.group(2)).upper()
            current_option = {"letter": letter, "text": omatch.group(3).strip()}
            continue

        if current:
            if current_option:
                current_option["text"] += " " + line
            else:
                current["question"] += " " + line

    finish()

    # Normalize option order and remove malformed/duplicate option labels.
    valid = []
    for q in questions:
        by_letter = {o["letter"]: clean_text(o["text"]) for o in q["options"]}
        if all(letter in by_letter and by_letter[letter] for letter in "ABCD"):
            q["question"] = clean_text(q["question"])
            q["options"] = [by_letter[x] for x in "ABCD"]
            if len(q["question"]) <= 300 and all(len(x) <= 100 for x in q["options"]):
                valid.append(q)
    return valid


def build_quiz(pages):
    # The answer key is expected at the end. Search the last 8 pages first.
    key_text = "\n".join(pages[-8:])
    answers = parse_answer_key(key_text)

    # Some PDFs put the key just before the final pages; widen if too few found.
    if not answers:
        answers = parse_answer_key("\n".join(pages[-15:]))

    # Parse questions from the document, excluding the final answer-key pages
    # when possible, so numbered answer rows are not mistaken for questions.
    body_pages = pages[:-1] if len(pages) > 1 else pages
    body_text = "\n".join(body_pages)
    questions = parse_questions(body_text)

    matched = []
    for q in questions:
        letter = answers.get(q["number"])
        if letter and letter in "ABCD":
            q["correct"] = "ABCD".index(letter)
            matched.append(q)
    return matched, len(questions), len(answers)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 **PDF Quiz Poll Maker**\n\n"
        "NEET/JEE MCQ PDF bhejiye. PDF ke last mein answer key honi chahiye.\n"
        "Main questions ko Telegram Quiz Polls mein convert karunga.\n\n"
        "/help — Help\n/cancel — Current process cancel",
        parse_mode="Markdown"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "PDF format:\n"
        "• Questions numbered 1., 2. ...\n"
        "• Options labelled A), B), C), D) or (A), (B), (C), (D)\n"
        "• Answer key PDF ke last pages mein: 1-A, 2-C ...\n\n"
        "Text-based PDFs best work karte hain. Scanned PDF ke liye OCR_ENABLED=true set karein.\n"
        "Note: Different PDF layouts may need parser adjustment."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cancelled"] = True
    await update.message.reply_text("Process cancel request noted.")


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    document = message.document
    if not document or not (document.file_name or "").lower().endswith(".pdf"):
        await message.reply_text("Please PDF file send karein.")
        return

    if document.file_size and document.file_size > MAX_PDF_MB * 1024 * 1024:
        await message.reply_text(f"PDF {MAX_PDF_MB} MB se chhoti honi chahiye.")
        return

    if OWNER_ID and update.effective_user.id != OWNER_ID:
        await message.reply_text("This bot is currently restricted to its owner.")
        return

    context.user_data["cancelled"] = False
    status = await message.reply_text("⏳ PDF download aur analysis ho raha hai...")

    suffix = Path(document.file_name or "input.pdf").suffix or ".pdf"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            temp_path = tmp.name

        tg_file = await context.bot.get_file(document.file_id)
        await tg_file.download_to_drive(custom_path=temp_path)

        pages = extract_pdf_pages(temp_path)
        if context.user_data.get("cancelled"):
            await status.edit_text("❌ Process cancelled.")
            return

        quizzes, total_found, key_count = build_quiz(pages)
        if not quizzes:
            await status.edit_text(
                "❌ Questions aur answer key match nahi ho paaye.\n\n"
                "Expected format:\n"
                "Questions: 1. Question text / A) ... / B) ... / C) ... / D) ...\n"
                "Answer key: 1-A, 2-C ...\n\n"
                "Aapka PDF layout alag ho sakta hai; sample PDF se parser adjust karna padega."
            )
            return

        await status.edit_text(
            f"✅ {len(quizzes)} questions answer key se match hue.\n"
            f"Ab quiz polls bhej raha hoon..."
        )

        sent = 0
        failed = 0
        for q in quizzes:
            if context.user_data.get("cancelled"):
                break
            try:
                await context.bot.send_poll(
                    chat_id=update.effective_chat.id,
                    question=q["question"],
                    options=q["options"],
                    type=PollType.QUIZ,
                    correct_option_id=q["correct"],
                    is_anonymous=True
                )
                sent += 1
            except Exception:
                failed += 1
                log.exception("Failed sending question %s", q["number"])

            if sent % 10 == 0 and sent:
                try:
                    await status.edit_text(
                        f"📤 Polls sent: {sent}/{len(quizzes)}\n"
                        f"Failed: {failed}"
                    )
                except Exception:
                    pass
            import asyncio
            await asyncio.sleep(POLL_DELAY)

        await status.edit_text(
            f"🎉 Completed!\n\n"
            f"Questions detected: {total_found}\n"
            f"Answer key entries detected: {key_count}\n"
            f"Quiz polls sent: {sent}\n"
            f"Failed: {failed}"
        )

    except Exception as exc:
        log.exception("PDF processing error")
        await status.edit_text(f"❌ PDF process nahi ho paya: {str(exc)[:300]}")
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Document.PDF, handle_pdf))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
    log.info("PDF Quiz Poll Maker started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
