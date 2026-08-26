#!/usr/bin/env python3
"""Local SDLC board server, backed by a Fast Note Sync vault.

Ticket state lives in an FNS vault, not in git. A read-only WebSocket client
(fnsclient.py) mirrors the vault in memory and on disk; this server renders the
board from that mirror.

  GET  /board          -> config + tickets, from the in-memory mirror
  GET  /history?key=K  -> 503 until Phase 4 rebuilds it from _log/transitions.md
  GET  /metrics        -> 503 until Phase 4 rebuilds it from _log/transitions.md
  POST /ticket         -> 503 until Phase 3 adds the REST compare-and-swap
  POST /archive        -> 503 until Phase 6 moves it onto POST /api/note/move

PHASE 2 IS READ-ONLY. Removing the git write path strands POST /ticket, which
is not rewritten until Phase 3, so it returns 503 rather than pointing at a
store that no longer exists.

There is no git code here at all. vault.py keeps its own, because the cutover
archive still needs it.

Offline: when the socket is down the board still renders from the on-disk
mirror, marked read-only with the age of the data.

Localhost-only, and unauthenticated BECAUSE it binds 127.0.0.1. That assumption
is load-bearing; do not change the bind address without adding authentication.
"""

import json
import os
import re
import sys
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fnsclient import FNSReadClient, VaultMirror
from vault import ticket_from_note

DIR = Path(__file__).resolve().parent
CONFIG = DIR / "config.json"
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

API = os.environ.get("FNS_API", "http://10.99.0.31:9000")
TOKEN = os.environ.get("FNS_TOKEN", "")
VAULT = os.environ.get("FNS_VAULT", "sdlc")
CLIENT = os.environ.get("FNS_CLIENT", "sdlcBoard")
MIRROR_DIR = Path(os.environ.get("SDLC_MIRROR", Path.home() / ".cache" / "sdlc-board"))

# A note is a ticket only if it lives here. _board/ and _log/ are not tickets.
TICKET_PREFIX = "tickets/"

MIRROR = VaultMirror(MIRROR_DIR, [VAULT])
CLIENT_THREAD = None


def sort_key(t):
    prefix, _, num = t["key"].rpartition("-")
    return (prefix, int(num))


def tickets_from_mirror(vault=VAULT):
    """Parse every ticket note in the mirror into a ticket dict.

    Notes that fail to parse are skipped rather than taking the board down: a
    hand-edit in Obsidian should not blank the board.
    """
    out = []
    for path, content in MIRROR.snapshot(vault).items():
        if not path.startswith(TICKET_PREFIX):
            continue
        try:
            t = ticket_from_note(content)
        except (ValueError, KeyError) as e:
            print(f"[board] skipping unparseable note {path}: {e}", file=sys.stderr)
            continue
        if not t.get("key"):
            continue
        out.append(t)
    return sorted(out, key=sort_key)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(DIR), **k)

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected (reload/poll abort) before the response landed

    def do_GET(self):
        url = urlparse(self.path)

        if url.path == "/board":
            board = json.loads(CONFIG.read_text(encoding="utf-8"))
            board["tickets"] = tickets_from_mirror()
            board["now"] = int(time.time())
            # Phase 2: the board is read-only and says so, with the age of the
            # data when the socket is down.
            board["readOnly"] = True
            board["readOnlyReason"] = (
                "read-only during the vault migration; writes return in Phase 3"
            )
            board["connected"] = MIRROR.connected
            age = MIRROR.age_seconds()
            board["dataAgeSeconds"] = None if age == float("inf") else int(age)
            self._json(200, board)
            return

        if url.path == "/history":
            key = parse_qs(url.query).get("key", [""])[0]
            if not KEY_RE.match(key):
                self.send_error(400, "bad key")
                return
            # Deliberately empty, not git-derived: the git timeline stops at the
            # migration, and reporting it as current would be a lie. Phase 4
            # rebuilds this from _log/transitions.md.
            self._json(
                200,
                {
                    "key": key,
                    "events": [],
                    "unavailable": "history is rebuilt from the transition log in Phase 4",
                },
            )
            return

        if url.path == "/metrics":
            self._json(
                200,
                {
                    "now": int(time.time()),
                    "tickets": [],
                    "unavailable": "metrics are rebuilt from the transition log in Phase 4",
                },
            )
            return

        try:
            super().do_GET()
        except BrokenPipeError:
            pass  # client disconnected before the static file finished sending

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/ticket":
            self.send_error(
                503,
                "board is read-only: the vault write path lands in Phase 3",
            )
            return
        if route == "/archive":
            self.send_error(
                503,
                "archive moves to POST /api/note/move in Phase 6",
            )
            return
        self.send_error(404)

    def log_message(self, *a):
        pass


def main():
    global CLIENT_THREAD
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    if not TOKEN:
        print(
            "FNS_TOKEN is not set: starting from the on-disk mirror only "
            "(the board will be stale and read-only)",
            file=sys.stderr,
        )
    else:
        CLIENT_THREAD = FNSReadClient(API, TOKEN, [VAULT], MIRROR, client_type=CLIENT)
        CLIENT_THREAD.start()

    print(f"sdlc board on http://localhost:{port}/board.html")
    print(f"vault: {VAULT} at {API}")
    print(f"mirror: {MIRROR_DIR} ({len(MIRROR.snapshot(VAULT))} notes on disk)")
    print("board is READ-ONLY (Phase 2)")
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        if CLIENT_THREAD:
            CLIENT_THREAD.stop()


if __name__ == "__main__":
    main()
