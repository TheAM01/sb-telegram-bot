#!/usr/bin/env python3
"""Multitenant panel for the Telegram ledger bot.

Public panel (no sign-in):  anyone submits a bot token; it is validated
against Telegram's getMe and queued as "pending".

Admin panel (HTTP Basic Auth, username 'admin'):  approve, reject, pause or
delete submitted bots. Every approved bot runs the shared bot.py code as its
own subprocess with an isolated data directory, and is restarted
automatically if it crashes (with a cap on rapid crash loops).

Run:  python3 panel.py [--host H] [--port P] [--password PW]

Storage: data/panel.db (SQLite) holds the registry — including tokens, which
are required to restart bots after a reboot. Keep the data/ directory private.
"""
import argparse
import atexit
import base64
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(BASE_DIR, "bot.py")
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
BOTS_DIR = os.path.join(DATA_DIR, "bots")
DB_FILE = os.path.join(DATA_DIR, "panel.db")

USERNAME = "admin"
PASSWORD = None  # set in main()

TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")

# A bot moves through: pending -> approved | rejected.  Approved bots can be
# paused by an admin, and are marked failed after repeated rapid crashes.
STATUSES = ("pending", "approved", "rejected", "paused", "failed")


# --------------------------------------------------------------------------
# Database (stdlib sqlite3; one short-lived connection per call)
# --------------------------------------------------------------------------
_db_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def db_init():
    os.makedirs(BOTS_DIR, exist_ok=True)
    with _db_lock, _connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bots (
                 id           INTEGER PRIMARY KEY AUTOINCREMENT,
                 token        TEXT NOT NULL UNIQUE,
                 username     TEXT NOT NULL,
                 status       TEXT NOT NULL DEFAULT 'pending',
                 submitted_at TEXT NOT NULL,
                 decided_at   TEXT
               )"""
        )


def q(sql, args=()):
    with _db_lock, _connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def ex(sql, args=()):
    with _db_lock, _connect() as conn:
        cur = conn.execute(sql, args)
        return cur.lastrowid


def get_bot(bot_id):
    rows = q("SELECT * FROM bots WHERE id = ?", (bot_id,))
    return rows[0] if rows else None


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Telegram
# --------------------------------------------------------------------------
def telegram_get_me(token):
    """Validate a token. Returns (ok: bool, username_or_error: str)."""
    url = "https://api.telegram.org/bot%s/getMe" % token
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
        if data.get("ok"):
            return True, data["result"].get("username", "unknown")
        return False, data.get("description", "Telegram rejected the token")
    except urllib.error.HTTPError as e:
        try:
            return False, json.load(e).get("description", "HTTP %s" % e.code)
        except Exception:
            return False, "Invalid token (HTTP %s)" % e.code
    except Exception as e:
        return False, "Could not reach Telegram: %s" % e


# --------------------------------------------------------------------------
# Bot process manager — one bot.py subprocess per approved bot
# --------------------------------------------------------------------------
def bot_python():
    venv_py = os.path.join(BASE_DIR, "venv", "bin", "python")
    return venv_py if os.path.exists(venv_py) else sys.executable


def bot_dir(bot_id):
    return os.path.join(BOTS_DIR, str(bot_id))


def bot_log_path(bot_id):
    return os.path.join(bot_dir(bot_id), "bot.log")


def read_log_tail(bot_id, n=80):
    try:
        with open(bot_log_path(bot_id), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return ""


class Manager:
    MAX_QUICK_CRASHES = 5   # rapid deaths in a row before marking 'failed'
    QUICK_CRASH_SECS = 60

    def __init__(self):
        self.lock = threading.Lock()
        self.procs = {}   # bot_id -> {"proc", "started", "logf"}
        self.fails = {}   # bot_id -> consecutive quick-crash count

    def is_running(self, bot_id):
        info = self.procs.get(bot_id)
        return info is not None and info["proc"].poll() is None

    def uptime(self, bot_id):
        info = self.procs.get(bot_id)
        if info and info["proc"].poll() is None:
            return int(time.time() - info["started"])
        return None

    def start(self, bot):
        bot_id = bot["id"]
        with self.lock:
            if self.is_running(bot_id):
                return
            d = bot_dir(bot_id)
            os.makedirs(d, exist_ok=True)
            logf = open(bot_log_path(bot_id), "ab", buffering=0)
            logf.write(("\n--- starting @%s at %s ---\n"
                        % (bot["username"], time.ctime())).encode())
            env = dict(os.environ)
            env["BOT_TOKEN"] = bot["token"]
            env["BOT_DATA_DIR"] = d
            proc = subprocess.Popen(
                [bot_python(), BOT_SCRIPT],
                env=env, stdout=logf, stderr=subprocess.STDOUT, cwd=BASE_DIR,
            )
            self.procs[bot_id] = {"proc": proc, "started": time.time(), "logf": logf}

    def stop(self, bot_id):
        with self.lock:
            info = self.procs.pop(bot_id, None)
            self.fails.pop(bot_id, None)
        if not info:
            return
        proc = info["proc"]
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            info["logf"].close()
        except Exception:
            pass

    def stop_all(self):
        for bot_id in list(self.procs):
            self.stop(bot_id)

    def sync(self):
        """Reconcile running processes with the registry: start approved bots,
        stop anything no longer approved, restart crashes (capped)."""
        approved = q("SELECT * FROM bots WHERE status = 'approved'")
        approved_ids = {b["id"] for b in approved}

        for bot_id in list(self.procs):
            if bot_id not in approved_ids:
                self.stop(bot_id)

        for bot in approved:
            bot_id = bot["id"]
            info = self.procs.get(bot_id)
            if info and info["proc"].poll() is None:
                continue
            if info:  # process died
                quick = time.time() - info["started"] < self.QUICK_CRASH_SECS
                self.fails[bot_id] = self.fails.get(bot_id, 0) + 1 if quick else 1
                with self.lock:
                    self.procs.pop(bot_id, None)
                try:
                    info["logf"].close()
                except Exception:
                    pass
                if self.fails[bot_id] >= self.MAX_QUICK_CRASHES:
                    ex("UPDATE bots SET status = 'failed' WHERE id = ?", (bot_id,))
                    continue
            self.start(bot)


manager = Manager()


def monitor_loop():
    while True:
        try:
            manager.sync()
        except Exception as e:
            print("monitor error: %s" % e, file=sys.stderr)
        time.sleep(15)


# --------------------------------------------------------------------------
# Simple per-IP rate limit for public submissions
# --------------------------------------------------------------------------
_rl_lock = threading.Lock()
_rl = {}  # ip -> [timestamps]


def rate_limited(ip, limit=8, window=600):
    cutoff = time.time() - window
    with _rl_lock:
        hits = [t for t in _rl.get(ip, []) if t > cutoff]
        if len(hits) >= limit:
            _rl[ip] = hits
            return True
        hits.append(time.time())
        _rl[ip] = hits
        return False


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
def mask_token(token):
    return token[:6] + "…" + token[-4:] if len(token) > 12 else "…"


def public_view(bot):
    return {
        "username": bot["username"],
        "status": bot["status"],
        "running": manager.is_running(bot["id"]),
        "submitted_at": bot["submitted_at"],
    }


def admin_view(bot):
    return {
        "id": bot["id"],
        "username": bot["username"],
        "token_masked": mask_token(bot["token"]),
        "status": bot["status"],
        "running": manager.is_running(bot["id"]),
        "uptime": manager.uptime(bot["id"]),
        "submitted_at": bot["submitted_at"],
        "decided_at": bot["decided_at"],
    }


ADMIN_ACTION_RE = re.compile(r"^/api/admin/bots/(\d+)/(approve|reject|pause|delete)$")
ADMIN_LOG_RE = re.compile(r"^/api/admin/bots/(\d+)/log$")


class Handler(BaseHTTPRequestHandler):
    server_version = "BotPanel/2.0"

    def log_message(self, *args):
        pass

    # --- helpers -----------------------------------------------------------
    def _send(self, body, code=200, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, name, ctype):
        try:
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                self._send(f.read(), ctype=ctype)
        except FileNotFoundError:
            self._send("%s not found" % name, 500, "text/plain")

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    def _authed(self):
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode("utf-8").partition(":")
        except Exception:
            return False
        return hmac.compare_digest(user, USERNAME) and hmac.compare_digest(pw, PASSWORD)

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Bot Panel Admin"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required")

    # --- routes ------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path

        # public
        if path in ("/", "/index.html"):
            return self._send_file("public.html", "text/html; charset=utf-8")
        if path == "/static/style.css":
            return self._send_file("style.css", "text/css; charset=utf-8")
        if path == "/api/status":
            token = (parse_qs(url.query).get("token") or [""])[0].strip()
            if not token:
                return self._send({"ok": False, "error": "No token provided"}, 400)
            rows = q("SELECT * FROM bots WHERE token = ?", (token,))
            if not rows:
                return self._send({"ok": False, "error": "No submission found for this token"}, 404)
            return self._send({"ok": True, "bot": public_view(rows[0])})

        # admin
        if path == "/admin":
            if not self._authed():
                return self._deny()
            return self._send_file("admin.html", "text/html; charset=utf-8")
        if path == "/api/admin/bots":
            if not self._authed():
                return self._deny()
            bots = q("SELECT * FROM bots ORDER BY id DESC")
            return self._send({"ok": True, "bots": [admin_view(b) for b in bots]})
        m = ADMIN_LOG_RE.match(path)
        if m:
            if not self._authed():
                return self._deny()
            return self._send({"ok": True, "log": read_log_tail(int(m.group(1)))})

        self._send({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/submit":
            return self._handle_submit()

        m = ADMIN_ACTION_RE.match(path)
        if m:
            if not self._authed():
                return self._deny()
            return self._handle_admin_action(int(m.group(1)), m.group(2))

        self._send({"error": "not found"}, 404)

    # --- public: submit a token ---------------------------------------------
    def _handle_submit(self):
        ip = self.client_address[0]
        if rate_limited(ip):
            return self._send(
                {"ok": False, "error": "Too many submissions — try again later."}, 429)

        token = str(self._read_json().get("token", "")).strip()
        if not TOKEN_RE.match(token):
            return self._send(
                {"ok": False,
                 "error": "That doesn't look like a bot token. Get one from @BotFather "
                          "(format: 123456789:AA…)."}, 400)

        existing = q("SELECT * FROM bots WHERE token = ?", (token,))
        if existing:
            return self._send({"ok": True, "already": True,
                               "bot": public_view(existing[0])})

        ok, info = telegram_get_me(token)
        if not ok:
            return self._send({"ok": False, "error": info}, 400)
        username = info

        # If the same bot was re-submitted with a regenerated token, replace
        # the stale token but keep the bot's status.
        same_bot = q("SELECT * FROM bots WHERE username = ?", (username,))
        if same_bot:
            bot = same_bot[0]
            manager.stop(bot["id"])
            ex("UPDATE bots SET token = ? WHERE id = ?", (token, bot["id"]))
            bot = get_bot(bot["id"])
            if bot["status"] == "approved":
                manager.start(bot)
            return self._send({"ok": True, "already": True, "updated": True,
                               "bot": public_view(bot)})

        try:
            ex("INSERT INTO bots (token, username, status, submitted_at) "
               "VALUES (?, ?, 'pending', ?)", (token, username, now()))
        except sqlite3.IntegrityError:
            pass  # concurrent duplicate submit
        bot = q("SELECT * FROM bots WHERE token = ?", (token,))[0]
        return self._send({"ok": True, "bot": public_view(bot)})

    # --- admin: lifecycle actions -------------------------------------------
    def _handle_admin_action(self, bot_id, action):
        bot = get_bot(bot_id)
        if not bot:
            return self._send({"ok": False, "error": "Bot not found"}, 404)

        if action == "approve":
            ok, info = telegram_get_me(bot["token"])
            if not ok:
                ex("UPDATE bots SET status = 'failed', decided_at = ? WHERE id = ?",
                   (now(), bot_id))
                return self._send(
                    {"ok": False, "error": "Token is no longer valid: %s" % info}, 400)
            manager.fails.pop(bot_id, None)
            ex("UPDATE bots SET status = 'approved', username = ?, decided_at = ? "
               "WHERE id = ?", (info, now(), bot_id))
            manager.start(get_bot(bot_id))
            return self._send({"ok": True, "bot": admin_view(get_bot(bot_id))})

        if action == "reject":
            manager.stop(bot_id)
            ex("UPDATE bots SET status = 'rejected', decided_at = ? WHERE id = ?",
               (now(), bot_id))
            return self._send({"ok": True, "bot": admin_view(get_bot(bot_id))})

        if action == "pause":
            manager.stop(bot_id)
            ex("UPDATE bots SET status = 'paused', decided_at = ? WHERE id = ?",
               (now(), bot_id))
            return self._send({"ok": True, "bot": admin_view(get_bot(bot_id))})

        if action == "delete":
            manager.stop(bot_id)
            ex("DELETE FROM bots WHERE id = ?", (bot_id,))
            shutil.rmtree(bot_dir(bot_id), ignore_errors=True)
            return self._send({"ok": True})

        return self._send({"ok": False, "error": "Unknown action"}, 400)


# --------------------------------------------------------------------------
def main():
    global PASSWORD
    ap = argparse.ArgumentParser(description="Multitenant Telegram bot panel")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--password", default=os.environ.get("PANEL_PASSWORD"),
                    help="admin password (default: env PANEL_PASSWORD, else random)")
    args = ap.parse_args()

    generated = not args.password
    PASSWORD = args.password or secrets.token_urlsafe(9)

    db_init()
    manager.sync()  # start bots that were approved before a restart
    threading.Thread(target=monitor_loop, daemon=True).start()
    atexit.register(manager.stop_all)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 60)
    print(" Multitenant Telegram bot panel")
    print("   Public:   http://%s:%d/" % (args.host, args.port))
    print("   Admin:    http://%s:%d/admin" % (args.host, args.port))
    print("   Login:    username 'admin'  /  password '%s'" % PASSWORD)
    if generated:
        print("   (random password; set --password or PANEL_PASSWORD to fix it)")
    print("=" * 60)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop_all()


if __name__ == "__main__":
    main()
