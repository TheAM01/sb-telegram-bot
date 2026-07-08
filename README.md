# sb-telegram-bot

A simple Telegram group bot that:

- Adds up numbers/expressions posted in a chat and keeps a running total per group.
- Stores payment details (Binance ID, USDT address, UPI, etc.) per chat.

## Commands

| Command | Description |
| --- | --- |
| `<numbers / expressions>` | Any message with numbers or math (one per line) is added to the running total. |
| `0` | Show the current remaining amount. |
| `/paid` | Mark the running total as paid and reset it to 0. |
| `/setpayment <method> <details>` | Save/update a payment method. |
| `/payment` (or `/payments`) | Show all saved payment details for this chat. |
| `/delpayment <method>` | Remove a saved payment method. |

### Payment examples

```
/setpayment binance 123456789
/setpayment usdt-trc20 TXxxxxxxxxxxxxxxxxxxxx
/setpayment upi name@bank
/payment
```

Payment details are stored per chat in `payments.json` (created next to the
script, git-ignored) so they survive restarts.

## Setup

```bash
pip install -r requirements.txt          # add --break-system-packages, or use a venv
export BOT_TOKEN="<your-telegram-bot-token>"
python3 bot.py
```

The bot token is read from the `BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN`)
environment variable — it is never hard-coded.
