import json
import logging
import os
import re
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

# Running total for each group, persisted to disk so it survives restarts.
group_totals = {}

# Payment details and running totals are stored per-chat in JSON files so
# they survive bot restarts. When the multitenant panel launches this script
# it sets BOT_DATA_DIR to a per-bot directory so tenants never share data.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("BOT_DATA_DIR") or BASE_DIR
os.makedirs(DATA_DIR, exist_ok=True)
PAYMENTS_FILE = os.path.join(DATA_DIR, "payments.json")
TOTALS_FILE = os.path.join(DATA_DIR, "totals.json")


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


def load_totals():
    """Load {chat_id: total} from disk, tolerating a missing/bad file.
    JSON keys are strings, so chat ids are converted back to int here."""
    try:
        with open(TOTALS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {int(k): v for k, v in data.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return {}


def save_totals(data):
    """Write running totals atomically so a crash mid-write can't corrupt the file."""
    tmp = TOTALS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in data.items()}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TOTALS_FILE)


# Loaded once at startup; JSON keys are strings, so chat ids are stored as str.
payment_details = load_payments()
group_totals = load_totals()

# A plain amount on its own line, e.g. "500", "-200" or "1500.50".
NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")


# --------------------------------------------------------------------------
# Authorized users — set per-bot by the admin panel via BOT_AUTHORIZED_USERS
# (comma-separated Telegram user ids). Empty/unset = no restriction.
# --------------------------------------------------------------------------
def _parse_authorized_users():
    raw = os.environ.get("BOT_AUTHORIZED_USERS", "").strip()
    if not raw:
        return set()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part and (part.lstrip("-").isdigit()):
            ids.add(int(part))
    return ids


AUTHORIZED_USERS = _parse_authorized_users()


def is_authorized(user_id):
    # No list configured for this bot -> no restriction.
    return not AUTHORIZED_USERS or user_id in AUTHORIZED_USERS


async def calculate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

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

    lines = content.splitlines()

    # A message is an entry if it matches the expected 5-or-6-line schema,
    # or if it is a single line that is entirely a number. Anything else
    # is ignored.
    single_number = len(lines) == 1 and NUMBER_RE.fullmatch(lines[0].strip())
    if not single_number and len(lines) not in (5, 6):
        return

    first_line = lines[0].strip()

    try:
        now = float(sympify(first_line))
    except Exception:
        return

    if now == 0:
        return

    total = before + now
    group_totals[chat_id] = total
    save_totals(group_totals)

    await update.message.reply_text(
        f"before: {before}\n"
        f"now: {now}\n"
        f"total: {total}"
    )


async def paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    previous = group_totals.get(chat_id, 0)
    group_totals[chat_id] = 0
    save_totals(group_totals)

    await update.message.reply_text(
        f"✅ Paid: {previous}\n"
        f"💰 Remaining Amount: 0"
    )


async def set_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Add or update a payment method for this chat, e.g. /setpayment binance 123456789"""
    if not is_authorized(update.effective_user.id):
        return

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
    if not is_authorized(update.effective_user.id):
        return

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
    if not is_authorized(update.effective_user.id):
        return

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


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with the sender's Telegram user ID. Deliberately not gated by
    is_authorized: it exists so people can find the ID an owner must add to
    the bot's allowed-users list."""
    await update.message.reply_text(
        f"🪪 Your Telegram user ID: {update.effective_user.id}"
    )


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
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(
        MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.CAPTION, calculate)
    )

    app.run_polling()


if __name__ == "__main__":
    main()