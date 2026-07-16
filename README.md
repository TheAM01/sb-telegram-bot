# Ledger Bots — multitenant Telegram bot platform

A multitenant platform around a Telegram group ledger bot. Anyone can submit
their own bot token on the public panel (no sign-in), along with a list of
Telegram user IDs allowed to use it; an administrator approves it on the
admin panel; every approved bot then runs the **same shared bot code**
(`bot.py`) as its own isolated process.

## What each bot does

- Reads messages that are exactly **5 or 6 lines** long as an entry: the
  **last** line is the price and the earlier lines are ignored. The price must
  be a bare number — a last line carrying any text (e.g. `10800 CP`, which is
  COD points) means the message isn't an entry and is ignored. Messages of any
  other length are ignored.
- The sign on the amount decides the direction: `500` and `+500` both **add**
  500 to the running total, `-500` **subtracts** 500.
- Only replies to Telegram user IDs on the bot's **allowed-users list** (set
  at submission time on the public panel). Leave it blank to allow everyone.
- Stores payment details (Binance ID, USDT address, UPI, etc.) per chat.

| Command | Description |
| --- | --- |
| `<5 or 6 line message>` | Last line is read as the price and applied to the running total. Must be a bare number; `10800 CP` is not a price. |
| `<number>` | A message that is just a number: `500`/`+500` adds, `-500` subtracts. |
| `0` | Show the current remaining amount. |
| `/paid` | Mark the running total as paid and reset it to 0. |
| `/setpayment <method> <details>` | Save/update a payment method. |
| `/payment` (or `/payments`) | Show all saved payment details for this chat. |
| `/delpayment <method>` | Remove a saved payment method. |

## Setup

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

`panel.py` uses only the Python standard library; the venv is only needed by
the bots themselves (`python-telegram-bot`, `sympy`).

## Running on a VPS (background process, no systemd)

Start it detached from your SSH session with `nohup` so it keeps running
after you log out:

```bash
cd /path/to/ledger-bots
PANEL_PASSWORD="choose-a-strong-password" \
  nohup venv/bin/python panel.py --host 0.0.0.0 --port 8080 \
  > panel.out 2>&1 &
disown
```

- `nohup ... &` runs it in the background and ignores the hangup signal sent
  when your SSH session closes.
- `disown` detaches it from the current shell job table too, so it survives
  even a `kill $(jobs -p)`-style cleanup.
- Output (including the printed admin password, if you didn't set
  `PANEL_PASSWORD`) goes to `panel.out` in the working directory.

Check it's running:

```bash
ps aux | grep panel.py
tail -f panel.out
```

Stop it:

```bash
pkill -f "python.*panel.py"
```

Restart it after a reboot (add this exact command to a personal script or
your shell history — there's no systemd unit managing this):

```bash
cd /path/to/ledger-bots && PANEL_PASSWORD="..." nohup venv/bin/python panel.py --host 0.0.0.0 --port 8080 > panel.out 2>&1 & disown
```

Note: the panel itself doesn't survive a VPS reboot on its own (no
systemd/cron tie-in) — you'll need to re-run the start command manually or
via your own cron `@reboot` entry if you want that automated:

```bash
crontab -e
# add:
@reboot cd /path/to/ledger-bots && PANEL_PASSWORD="..." nohup venv/bin/python panel.py --host 0.0.0.0 --port 8080 > panel.out 2>&1 &
```

(Approved bot subprocesses themselves *are* restarted automatically by
`panel.py`'s monitor thread whenever the panel process is up — see below.)

## Panels

- **Public panel — `http://<server>:8080/`** (no sign-in)
  Users paste a bot token from `@BotFather` and, optionally, a list of
  Telegram user IDs to restrict who the bot replies to (comma or
  whitespace-separated; blank = everyone allowed). The token is validated
  against Telegram's `getMe`, then queued as **pending**. Submitting the same
  token again later updates the allowed-users list — if the bot is already
  live, it's restarted automatically to pick up the change. The same token
  can also be pasted into "Check status" any time to see status and the
  current allowed-users list.

- **Admin panel — `http://<server>:8080/admin`**
  Protected by HTTP Basic Auth (username `admin`, password from
  `--password` / `PANEL_PASSWORD`, else a random one printed at startup).
  Admins see all submissions and can **Approve**, **Reject**, **Pause**,
  **Resume**, **Delete**, view each bot's recent log, and override a bot's
  allowed-users list directly (restarts the bot if it's live).

## How the multitenancy works

- Approved bots each run as their own `bot.py` subprocess with `BOT_TOKEN`,
  `BOT_DATA_DIR`, and `BOT_AUTHORIZED_USERS` set in the environment — all
  bots share one codebase, but each has an isolated data directory
  (`data/bots/<id>/`) for its `payments.json` and `bot.log`, and its own
  allowed-users list.
- The registry lives in `data/panel.db` (SQLite), including tokens and
  allowed-users lists, so approved bots can be restarted after a reboot —
  **keep `data/` private** (it is git-ignored).
- A monitor thread restarts approved bots that crash. Five rapid crashes in a
  row mark the bot **Failed** (an admin can Retry after fixing the cause,
  e.g. a revoked token).
- Re-submitting a regenerated token for the same bot username replaces the
  stale token, refreshes the allowed-users list, and keeps the bot's status.

Running `bot.py` directly still works for a single bot:
`BOT_TOKEN=... BOT_AUTHORIZED_USERS=111,222 venv/bin/python bot.py` (data is
stored next to the script unless `BOT_DATA_DIR` is set; leave
`BOT_AUTHORIZED_USERS` unset to allow everyone).

## Security notes

- Plain HTTP, no TLS: logins, tokens, and allowed-user lists travel in
  cleartext. Expose the port only on a trusted network, put a TLS reverse
  proxy in front, or tunnel: `ssh -L 8080:localhost:8080 user@server`.
- Public submissions are rate-limited per IP and validated against Telegram
  before being stored.