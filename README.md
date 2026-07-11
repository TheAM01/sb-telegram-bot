# Ledger Bots — multitenant Telegram bot platform

A multitenant platform around a Telegram group ledger bot. Anyone can submit
their own bot token on the public panel (no sign-in); an administrator
approves it on the admin panel; every approved bot then runs the **same
shared bot code** (`bot.py`) as its own isolated process.

## What each bot does

- Adds up numbers/expressions posted in a chat and keeps a running total per group.
- Stores payment details (Binance ID, USDT address, UPI, etc.) per chat.

| Command | Description |
| --- | --- |
| `<numbers / expressions>` | Any message with numbers or math (one per line) is added to the running total. |
| `0` | Show the current remaining amount. |
| `/paid` | Mark the running total as paid and reset it to 0. |
| `/setpayment <method> <details>` | Save/update a payment method. |
| `/payment` (or `/payments`) | Show all saved payment details for this chat. |
| `/delpayment <method>` | Remove a saved payment method. |

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
PANEL_PASSWORD="choose-a-strong-password" python3 panel.py --host 0.0.0.0 --port 8080
```

`panel.py` uses only the Python standard library; the venv is only needed by
the bots themselves (`python-telegram-bot`, `sympy`).

## Panels

- **Public panel — `http://<server>:8080/`** (no sign-in)
  Users paste a bot token from `@BotFather`. The token is validated against
  Telegram's `getMe`, then queued as **pending**. The same token can be pasted
  again any time to check status (pending / live / rejected / paused).

- **Admin panel — `http://<server>:8080/admin`**
  Protected by HTTP Basic Auth (username `admin`, password from
  `--password` / `PANEL_PASSWORD`, else a random one printed at startup).
  Admins see all submissions and can **Approve**, **Reject**, **Pause**,
  **Resume**, **Delete**, and view each bot's recent log.

## How the multitenancy works

- Approved bots each run as their own `bot.py` subprocess with `BOT_TOKEN`
  and `BOT_DATA_DIR` set in the environment — all bots share one codebase,
  but each has an isolated data directory (`data/bots/<id>/`) for its
  `payments.json` and `bot.log`.
- The registry lives in `data/panel.db` (SQLite). Tokens are stored there so
  approved bots can be restarted after a reboot — **keep `data/` private**
  (it is git-ignored).
- A monitor thread restarts approved bots that crash. Five rapid crashes in a
  row mark the bot **Failed** (an admin can Retry after fixing the cause,
  e.g. a revoked token).
- Re-submitting a regenerated token for the same bot username replaces the
  stale token and keeps the bot's status.

Running `bot.py` directly still works for a single bot:
`BOT_TOKEN=... venv/bin/python bot.py` (data is stored next to the script
unless `BOT_DATA_DIR` is set).

## Security notes

- Plain HTTP, no TLS: logins and tokens travel in cleartext. Expose the port
  only on a trusted network, put a TLS reverse proxy in front, or tunnel:
  `ssh -L 8080:localhost:8080 user@server`.
- Public submissions are rate-limited per IP and validated against Telegram
  before being stored.
