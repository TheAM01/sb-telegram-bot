#!/usr/bin/env python3
"""Web control panel for the Telegram bot.

Serves a small page where you paste the bot token in the browser. The token is
validated against Telegram's getMe, then bot.py is launched as a subprocess with
BOT_TOKEN set in its environment -- so bot.py itself needs no changes and the
token is never written to disk.

Run:  python3 control.py [--host H] [--port P] [--password PW]
The panel is protected by HTTP Basic Auth (username: admin).
"""
import argparse
import atexit
import base64
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_SCRIPT = os.path.join(BASE_DIR, "bot.py")
INDEX_FILE = os.path.join(BASE_DIR, "index.html")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")

USERNAME = "admin"
PASSWORD = None  # set in main()

_lock = threading.Lock()
_state = {"proc": None, "username": None, "last_error": None}


def bot_python():
    """Use the project venv's python if present, so the bot's deps are available."""
    venv_py = os.path.join(BASE_DIR, "venv", "bin", "python")
    return venv_py if os.path.exists(venv_py) else sys.executable


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


def is_running():
    p = _state["proc"]
    return p is not None and p.poll() is None


def read_log_tail(n=50):
    try:
        with open(LOG_FILE, "rb") as f:
            lines = f.read().decode("utf-8", "replace").splitlines()
        return "\n".join(lines[-n:])
    except FileNotFoundError:
        return ""


def start_bot(token):
    with _lock:
        if is_running():
            return False, "Already running as @%s" % _state["username"]
        ok, info = telegram_get_me(token)
        if not ok:
            _state["last_error"] = info
            return False, info
        username = info
        env = dict(os.environ)
        env["BOT_TOKEN"] = token
        logf = open(LOG_FILE, "ab", buffering=0)
        logf.write(("\n--- starting @%s at %s ---\n" % (username, time.ctime())).encode())
        proc = subprocess.Popen(
            [bot_python(), BOT_SCRIPT],
            env=env, stdout=logf, stderr=subprocess.STDOUT, cwd=BASE_DIR,
        )
        _state.update(proc=proc, username=username, last_error=None)

    # If the process dies immediately (bad deps, etc.), surface the reason.
    time.sleep(2.0)
    if not is_running():
        _state["username"] = None
        tail = read_log_tail(20)
        _state["last_error"] = "Bot exited right after start"
        return False, "Bot exited right after start:\n" + (tail or "(no log output)")
    return True, _state["username"]


def stop_bot():
    with _lock:
        p = _state["proc"]
        if p is None or p.poll() is not None:
            _state.update(proc=None, username=None)
            return False, "Not running"
    p.terminate()
    try:
        p.wait(timeout=8)
    except subprocess.TimeoutExpired:
        p.kill()
    uname = _state["username"]
    _state.update(proc=None, username=None)
    return True, uname or ""


class Handler(BaseHTTPRequestHandler):
    server_version = "BotPanel/1.0"

    def log_message(self, *args):
        pass  # keep the console clean

    # --- auth ---
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
        self.send_header("WWW-Authenticate", 'Basic realm="Bot Control Panel"')
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Authentication required")

    # --- helpers ---
    def _send(self, body, code=200, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except Exception:
            return {}

    # --- routes ---
    def do_GET(self):
        if not self._authed():
            return self._deny()
        if self.path in ("/", "/index.html"):
            try:
                with open(INDEX_FILE, "rb") as f:
                    self._send(f.read(), ctype="text/html; charset=utf-8")
            except FileNotFoundError:
                self._send("index.html not found", 500, "text/plain")
        elif self.path.startswith("/status"):
            self._send({
                "running": is_running(),
                "username": _state["username"] if is_running() else None,
                "last_error": _state["last_error"],
                "log": read_log_tail(40),
            })
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._authed():
            return self._deny()
        if self.path == "/start":
            token = str(self._read_json().get("token", "")).strip()
            if not token:
                return self._send({"ok": False, "error": "No token provided"}, 400)
            ok, info = start_bot(token)
            self._send({"ok": ok, "username": info} if ok
                       else {"ok": False, "error": info}, 200 if ok else 400)
        elif self.path == "/stop":
            ok, info = stop_bot()
            self._send({"ok": ok, "username": info})
        else:
            self._send({"error": "not found"}, 404)


def main():
    global PASSWORD
    ap = argparse.ArgumentParser(description="Telegram bot control panel")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--password", default=os.environ.get("PANEL_PASSWORD"),
                    help="panel password (default: env PANEL_PASSWORD, else random)")
    args = ap.parse_args()

    generated = not args.password
    PASSWORD = args.password or secrets.token_urlsafe(9)

    def _cleanup():
        if is_running():
            _state["proc"].terminate()
    atexit.register(_cleanup)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("=" * 56)
    print(" Telegram bot control panel")
    print("   URL:      http://%s:%d" % (args.host, args.port))
    print("   Login:    username 'admin'  /  password '%s'" % PASSWORD)
    if generated:
        print("   (random password; set --password or PANEL_PASSWORD to fix it)")
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
