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
python3 -m venv venv
venv/bin/pip install -r requirements.txt
export BOT_TOKEN="<your-telegram-bot-token>"
venv/bin/python bot.py
```

The bot token is read from the `BOT_TOKEN` (or `TELEGRAM_BOT_TOKEN`)
environment variable — it is never hard-coded.

## Web control panel (enter the token in a browser)

Instead of setting `BOT_TOKEN` yourself, run the control panel and paste the
token into a web page. It validates the token against Telegram, then launches
`bot.py` for you (Start / Stop / status + recent log).

```bash
# deps for the bot must be installed in ./venv (see Setup above)
PANEL_PASSWORD="choose-a-strong-password" python3 control.py --host 0.0.0.0 --port 8080
```

- `control.py` uses only the Python standard library — no extra deps.
- Open `http://<server-ip>:8080`, log in (username `admin`, the password you
  set), paste the token, click **Start bot**.
- The token is passed to `bot.py` via its environment and is **never written to
  disk**.
- Protected by HTTP Basic Auth. If `--password`/`PANEL_PASSWORD` is omitted, a
  random password is printed at startup.

**Security:** this is plain HTTP with no TLS, so the login and token travel in
cleartext. Only expose port 8080 on a trusted network, or keep it on localhost
and reach it through an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 user@server   # then open http://localhost:8080
```
