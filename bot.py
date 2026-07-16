import ast
import json
import logging
import math
import operator
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

# A plain amount on its own line, e.g. "500", "+500", "-200" or "1500.50".
# The sign decides the direction: bare and "+" add, "-" subtracts.
NUMBER_RE = re.compile(r"[+-]?\d+(?:\.\d+)?")

# An amount line may also be a calculation: "5+2", "5*7", "(100-40)/2".
# Only digits, the four operators, parentheses, a decimal point and spaces
# are allowed. This is what rejects a COD-points line like "10800 CP": the
# letters aren't in the set, so it never reaches the evaluator.
EXPRESSION_RE = re.compile(r"[0-9+\-*/(). ]+")

# Typed on phone keyboards in place of / and *.
SYMBOL_ALIASES = {"÷": "/", "×": "*", "−": "-"}

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node):
    """Evaluate one node of a parsed expression, allowing only numeric
    literals and + - * / (binary and unary). Anything else — a name, a call,
    ** , a comparison — raises, so it is never evaluated. Deliberately not
    eval()/sympify(): these lines come from untrusted group messages."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        # bool is an int subclass; "True" isn't an amount.
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("not a number")
        return float(node.value)
    if isinstance(node, ast.BinOp):
        op = _BINARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError("operator not allowed")
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError("operator not allowed")
        return op(_eval_node(node.operand))
    raise ValueError("not an arithmetic expression")


def parse_amount(line):
    """Return the signed amount on `line`, or None if the line isn't an amount.

    The line is either a bare number ("500"/"+500" are +500, "-500" is -500)
    or a calculation to work out first ("5+2" is 7, "5*7" is 35). A line
    carrying any text — most importantly a COD-points line like "10800 CP" —
    is not an amount and yields None."""
    text = line.strip()
    for symbol, plain in SYMBOL_ALIASES.items():
        text = text.replace(symbol, plain)

    if not text or not EXPRESSION_RE.fullmatch(text):
        return None

    try:
        value = _eval_node(ast.parse(text, mode="eval"))
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None

    if not math.isfinite(value):
        return None
    # Amounts are money; keep them at 2dp so 0.1+0.2 and 5/3 don't drag float
    # noise into the running total.
    return round(value, 2)


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

    # Trailing blank lines shouldn't change the shape of the message.
    lines = content.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return

    # Any message can be an entry, whatever its length: the price is the last
    # line, either a bare number or a calculation to work out. Everything
    # above it is ignored. If the last line carries any text it isn't a price
    # (e.g. "10800 CP" is COD points), so the message is ignored.
    price_line = lines[-1].strip()
    now = parse_amount(price_line)
    if now is None or now == 0:
        return

    total = round(before + now, 2)
    group_totals[chat_id] = total
    save_totals(group_totals)

    reply = ""
    # When the price was worked out rather than written down, show the sum so
    # the group can check it.
    if not NUMBER_RE.fullmatch(price_line):
        reply += f"🧮 {price_line} = {now}\n"

    await update.message.reply_text(
        reply +
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