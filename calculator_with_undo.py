import logging
import os
import psycopg2

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
from sympy import sympify

logging.basicConfig(
    format="%(message)s",
    level=logging.INFO
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

DATABASE_URL = os.getenv("DATABASE_URL")

ALLOWED_USERS = [
    1573531032,
]


def get_connection():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS group_totals(
        chat_id BIGINT PRIMARY KEY,
        total DOUBLE PRECISION DEFAULT 0
    )
    """)
    cur.execute("""
CREATE TABLE IF NOT EXISTS history(
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

    conn.commit()
    cur.close()
    conn.close()


def get_total(chat_id):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        "SELECT total FROM group_totals WHERE chat_id=%s",
        (chat_id,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    return row[0] if row else 0
def save_history(chat_id, value):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO history(chat_id, value)
    VALUES(%s, %s)
    """, (chat_id, value))

    conn.commit()

    cur.close()
    conn.close()

def save_total(chat_id, total):
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO group_totals(chat_id,total)
    VALUES(%s,%s)
    ON CONFLICT(chat_id)
    DO UPDATE SET total=EXCLUDED.total
    """, (chat_id, total))

    conn.commit()

    cur.close()
    conn.close()



def get_last_history(chat_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, value
    FROM history
    WHERE chat_id=%s
    ORDER BY id DESC
    LIMIT 1
    """, (chat_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row

def delete_history(history_id):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM history WHERE id=%s", (history_id,))
    conn.commit()
    cur.close()
    conn.close()

async def undo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ALLOWED_USERS:
        return

    chat_id = update.effective_chat.id
    last = get_last_history(chat_id)

    if not last:
        await update.message.reply_text("❌ Nothing to undo.")
        return

    history_id, value = last
    current = get_total(chat_id)
    new_total = current - value

    save_total(chat_id, new_total)
    delete_history(history_id)

    await update.message.reply_text(
        f"↩️ Last Entry Removed\n\n"
        f"Removed: {value}\n"
        f"Before: {current}\n"
        f"Now: {new_total}"
    )


async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id not in ALLOWED_USERS:
        return

    chat_id = update.effective_chat.id

    if update.message.text:
        content = update.message.text

    elif update.message.caption:
        content = update.message.caption

    else:
        return

    before = get_total(chat_id)

    if content.strip() == "0":
        await update.message.reply_text(
            f"💰 Remaining Amount: {before}"
        )
        return

    lines = content.splitlines()

    if len(lines) == 0:
        return

    first_line = lines[0].strip()

    try:
        now = float(sympify(first_line))
    except Exception:
        return

    if now == 0:
        return

    total = before + now

    save_history(chat_id, now)

    save_total(chat_id, total)

    await update.message.reply_text(
        f"Before: {before}\n"
        f"Now: {now}\n"
        f"Total: {total}"
    )

async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id not in ALLOWED_USERS:
        return

    chat_id = update.effective_chat.id

    previous = get_total(chat_id)

    save_total(chat_id, 0)

    await update.message.reply_text(
        f"✅ Payment Received\n\n"
        f"Paid Amount: {previous}\n"
        f"Remaining Amount: 0"
    )


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        f"👤 User ID: {update.effective_user.id}\n"
        f"💬 Chat ID: {update.effective_chat.id}"
    )


def main():

    if not DATABASE_URL:
        raise Exception("DATABASE_URL not found!")

    init_db()

    TOKEN = os.getenv("TOKEN")

    if not TOKEN:
        raise Exception("TOKEN not found!")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("paid", paid))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("undo", undo))

    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.Document.ALL)
            & ~filters.COMMAND,
            calculate,
        )
    )

    print("Bot is running...")

    app.run_polling()


if __name__ == "__main__":
    main()
