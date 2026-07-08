import json
import logging
import os
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)
from sympy import sympify

logging.basicConfig(format="%(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)

# Running total for each group
group_totals = {}

# Payment details are stored per-chat in a JSON file next to this script so
# they survive bot restarts.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PAYMENTS_FILE = os.path.join(BASE_DIR, "payments.json")


def load_payments():
    """Load {chat_id: {method: details}} from disk, tolerating a missing/bad file."""
    try:
        with open(PAYMENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_payments(data):
    """Write payment details atomically so a crash mid-write can't corrupt the file."""
    tmp = PAYMENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PAYMENTS_FILE)


# Loaded once at startup; JSON keys are strings, so chat ids are stored as str.
payment_details = load_payments()


async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Read text or caption
    if update.message.text:
        content = update.message.text
    elif update.message.caption:
        content = update.message.caption
    else:
        return

    before = group_totals.get(chat_id, 0)

    # If user sends only 0, show remaining amount
    if content.strip() == "0":
        await update.message.reply_text(
            f"💰 Remaining Amount: {before}"
        )
        return

    now = 0

    # Read every line separately
    for line in content.splitlines():
        line = line.strip()

        if not line:
            continue

        try:
            value = float(sympify(line))
            now += value
        except Exception:
            # Ignore text lines
            continue

    if now == 0:
        return

    total = before + now
    group_totals[chat_id] = total

    await update.message.reply_text(
        f"before: {before}\n"
        f"now: {now}\n"
        f"total: {total}"
    )


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    previous = group_totals.get(chat_id, 0)
    group_totals[chat_id] = 0

    await update.message.reply_text(
        f"✅ Paid: {previous}\n"
        f"💰 Remaining Amount: 0"
    )


async def set_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add or update a payment method for this chat, e.g. /setpayment binance 123456789"""
    chat_id = str(update.effective_chat.id)

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /setpayment <method> <details>\n\n"
            "Examples:\n"
            "  /setpayment binance 123456789\n"
            "  /setpayment usdt-trc20 TXxxxxxxxxxxxxxxxxxxxx\n"
            "  /setpayment upi name@bank"
        )
        return

    method = context.args[0].strip().lower()
    details = " ".join(context.args[1:]).strip()

    chat_methods = payment_details.setdefault(chat_id, {})
    chat_methods[method] = details
    save_payments(payment_details)

    await update.message.reply_text(f"✅ Saved {method}: {details}")


async def show_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show all payment methods stored for this chat."""
    chat_id = str(update.effective_chat.id)
    chat_methods = payment_details.get(chat_id, {})

    if not chat_methods:
        await update.message.reply_text(
            "No payment details saved yet.\n"
            "Add one with: /setpayment <method> <details>"
        )
        return

    lines = ["💳 Payment Details:"]
    for method, details in chat_methods.items():
        lines.append(f"• {method}: {details}")

    await update.message.reply_text("\n".join(lines))


async def del_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove a stored payment method, e.g. /delpayment binance"""
    chat_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text("Usage: /delpayment <method>")
        return

    method = context.args[0].strip().lower()
    chat_methods = payment_details.get(chat_id, {})

    if method in chat_methods:
        del chat_methods[method]
        save_payments(payment_details)
        await update.message.reply_text(f"🗑️ Removed {method}")
    else:
        await update.message.reply_text(f"No payment entry for '{method}'.")


def main():
    token = os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "Set the BOT_TOKEN (or TELEGRAM_BOT_TOKEN) environment variable "
            "with your Telegram bot token."
        )

    app = ApplicationBuilder().token(token).build()

    app.add_handler(CommandHandler("setpayment", set_payment))
    app.add_handler(CommandHandler("payment", show_payment))
    app.add_handler(CommandHandler("payments", show_payment))
    app.add_handler(CommandHandler("delpayment", del_payment))
    app.add_handler(CommandHandler("paid", paid))
    app.add_handler(
        MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, calculate)
    )

    app.run_polling()


if __name__ == "__main__":
    main()
