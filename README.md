# PDF Quiz Poll Maker Bot

A Heroku-ready Telegram bot that reads MCQ questions from a PDF, reads the answer key near the end of the PDF, matches answers, and sends Telegram Quiz Polls.

## Features

- PDF upload through Telegram
- Extracts text from selectable-text PDFs
- Optional OCR for scanned PDFs
- Reads answer keys from the final pages
- Matches question numbers with A/B/C/D answers
- Sends each matched question as a Telegram Quiz Poll
- Owner-only mode using `OWNER_ID`
- Heroku worker deployment

## Expected PDF layout

The first version supports common layouts such as:

```text
1. What is the SI unit of force?
A) Joule
B) Newton
C) Watt
D) Pascal

2. ...
A) ...
B) ...
C) ...
D) ...

ANSWER KEY
1-B
2-D
3-A
```

Question numbering should be `1.` or `1)`. Options should be `A)` / `A.` / `(A)` and so on. Answer key entries can be `1-B`, `1. B`, `1) B`, or `1 B`.

## Deploy on Heroku

1. Create a new GitHub repository and upload all files from this project.
2. Create a Heroku app and connect the GitHub repository.
3. In **Settings → Config Vars**, add:

| Key | Value |
|---|---|
| `BOT_TOKEN` | Your Telegram bot token from BotFather |
| `OWNER_ID` | Your numeric Telegram user ID |
| `MAX_PDF_MB` | `20` |
| `POLL_DELAY` | `1.2` |
| `OCR_ENABLED` | `false` |

4. Deploy the branch.
5. In **Resources**, enable the `worker` dyno and disable `web` if one exists.
6. Open the bot and send `/start`, then upload a PDF.

For scanned PDFs, set `OCR_ENABLED=true` and redeploy. OCR depends on Tesseract installed through `Aptfile`; the first version is configured for English OCR.

## Important limitations

- PDF layouts vary. This parser is a starter and may need adjustment for a particular coaching PDF.
- Questions must have four options labelled A-D.
- Answer key should be in the last pages and use a supported format.
- It only sends questions for which it finds a matching answer key entry.
- Review a few extracted polls before using a large PDF. Complex columns, tables, mathematical notation, and image-based questions may not extract perfectly.
- Telegram limits poll question text to 300 characters and each option to 100 characters.

## Local run

```bash
pip install -r requirements.txt
export BOT_TOKEN="YOUR_BOT_TOKEN"
export OWNER_ID="YOUR_TELEGRAM_USER_ID"
python bot.py
```
