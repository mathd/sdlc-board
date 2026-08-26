#!/usr/bin/env python3
"""Local SDLC board server, backed by a Fast Note Sync vault.

Ticket state lives in an FNS vault, not in git. A read-only WebSocket client
(fnsclient.py) mirrors the vault in memory and on disk; this server renders the
board from that mirror.

  GET  /board          -> config + tickets, from the in-memory mirror
  GET  /history?key=K  -> empty until Phase 4 rebuilds it from _log/transitions.md
  GET  /metrics        -> empty until Phase 4 rebuilds it from _log/transitions.md
  POST /ticket         -> compare-and-swap one ticket's status into the vault
  POST /archive        -> 503 until Phase 6 moves it onto POST /api/note/move

The write is a SERVER-SIDE compare-and-swap, because the vault does not offer
one. POST /api/note declares a `baseHash` field and ignores it: a write carrying
a deliberately wrong baseHash returns code 1 and overwrites the note (verified
against the live server). The only server-enforced CAS is on the WebSocket path
under `manualMerge`, which would mean giving the mirror write access -- exactly
what Phase 2 removed on purpose.

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
import threading
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fnsclient import FNSReadClient, VaultMirror
from vault import get_note as vault_get_note
from vault import note_from_ticket, ticket_from_note
from vault import put_note as vault_put_note

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

import vault as _vault

_vault.API, _vault.TOKEN, _vault.VAULT, _vault.CLIENT = API.rstrip("/"), TOKEN, VAULT, CLIENT

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


class ConflictError(Exception):
    """The guarded field no longer holds the value the client moved from."""

    def __init__(self, expected, actual):
        super().__init__(f"expected status {expected!r}, vault has {actual!r}")
        self.expected = expected
        self.actual = actual


class VaultWriteError(Exception):
    pass


# The only fields a board drag may change. Everything else on a ticket --
# summary, description, readiness, comments, context, links -- is authored in
# Obsidian or by an agent, and a board write must never carry a stale copy of it
# back over a fresher one.
BOARD_WRITABLE_FIELDS = ("status", "labels", "assignee", "pr", "classification",
                         "parent", "type")

MAX_CAS_ATTEMPTS = 3  # bound the retry so a hot note cannot spin

# One writer at a time from this process. Two browser tabs on the same board
# would otherwise interleave read-modify-write on one note, which is exactly the
# race the CAS exists to catch -- and a local lock closes it without a round
# trip. It does NOT protect against another machine's server.py; that is what
# the status check below is for.
WRITE_LOCK = threading.Lock()


def write_ticket(key, ticket, moving_from, actor):
    """Read, verify the status field, write. Returns {status, attempts}.

    On a lost race the vault's status has already moved, so the write is
    refused and both values go back to the browser (ConflictError).

    A body-only change -- someone edited the description in Obsidian while the
    card sat still -- is NOT a conflict: the guarded field still reads as
    expected, so the status change is re-applied onto the fresh note and
    retried. Rejecting those would train people to click through conflict
    dialogs until they click through a real one.
    """
    path = f"{TICKET_PREFIX}{key}.md"
    new_status = ticket["status"]

    with WRITE_LOCK:
        for attempt in range(1, MAX_CAS_ATTEMPTS + 1):
            try:
                envelope = vault_get_note(path)
            except Exception as e:  # noqa: BLE001 - surfaced to the browser
                raise VaultWriteError(f"cannot read {path}: {e}") from e
            current = envelope.get("data")
            if not current:
                raise VaultWriteError(
                    f"cannot read {path}: {envelope.get('message') or envelope}"
                )

            content = current["content"]
            try:
                existing = ticket_from_note(content)
            except (ValueError, KeyError) as e:
                raise VaultWriteError(f"{path} is not a parseable ticket: {e}") from e

            vault_status = existing.get("status")
            # The guard. `moving_from` is None for a create or a non-move edit.
            if moving_from is not None and vault_status != moving_from:
                if vault_status == new_status:
                    # Already where we wanted it -- someone applied the same
                    # move first. Idempotent, not a conflict.
                    return {"status": new_status, "attempts": attempt, "noop": True}
                raise ConflictError(moving_from, vault_status)

            # Apply ONLY the fields the board is allowed to change, onto the
            # note as it exists in the vault right now. Merging the client's
            # whole ticket over the vault's would let a field the client read
            # seconds ago overwrite a fresher one -- which silently destroyed a
            # description edited in Obsidian during testing. The client cannot
            # influence a field that is not in this set, rather than being
            # trusted not to send one.
            merged = dict(existing)
            for field in BOARD_WRITABLE_FIELDS:
                if field in ticket:
                    merged[field] = ticket[field]
            new_content = note_from_ticket(merged)
            if new_content == content:
                return {"status": new_status, "attempts": attempt, "noop": True}

            try:
                r = vault_put_note(path, new_content)
            except Exception as e:  # noqa: BLE001
                raise VaultWriteError(str(e)) from e
            if r.get("code") == 1:
                return {"status": new_status, "attempts": attempt}
            last_error = f"{r.get('code')} {r.get('message')}"

        raise VaultWriteError(f"gave up after {MAX_CAS_ATTEMPTS} attempts: {last_error}")


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
            # Writes need the vault: offline the board is readable but not
            # writable, and says so with the age of the data.
            board["readOnly"] = not MIRROR.connected
            if not MIRROR.connected:
                board["readOnlyReason"] = (
                    "vault unreachable — showing the last mirrored state"
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
            self._write_ticket()
            return
        if route == "/archive":
            self.send_error(
                503,
                "archive moves to POST /api/note/move in Phase 6",
            )
            return
        self.send_error(404)

    def _write_ticket(self):
        """Compare-and-swap one ticket's status into the vault.

        `_rev` from board.html is no longer a git blob hash: it is the status
        the card is moving FROM (plan §3). The swap refuses when the vault no
        longer holds that value, which is what makes two racing drags produce
        one winner and one visible conflict rather than a silent lost update.

        The CAS is server-side because the vault does not offer one. POST
        /api/note declares a `baseHash` field and IGNORES it -- verified: a
        write carrying a deliberately wrong baseHash returns code 1 and
        overwrites the note. The only server-enforced compare-and-swap is on
        the WebSocket path under `manualMerge`, and taking it would mean giving
        the mirror the ability to write, which Phase 2 deliberately removed.
        """
        try:
            n = int(self.headers.get("Content-Length", 0))
            ticket = json.loads(self.rfile.read(n))
        except (ValueError, TypeError) as e:
            self.send_error(400, f"invalid ticket: {e}")
            return

        key = ticket.get("key", "")
        if not KEY_RE.match(key):
            self.send_error(400, f"bad ticket key: {key!r}")
            return
        if not isinstance(ticket.get("status"), str):
            self.send_error(400, "status must be a string")
            return

        moving_from = ticket.pop("_rev", None)
        ticket.pop("_since", None)  # server-computed, never stored
        # The actor is supplied by the caller; the server cannot infer it.
        actor = {
            "role": ticket.pop("role", None) or "human",
            "model": ticket.pop("model", None) or "",
            "effort": ticket.pop("effort", None) or "",
        }

        try:
            result = write_ticket(key, ticket, moving_from, actor)
        except ConflictError as e:
            self._json(
                409,
                {
                    "error": "conflict",
                    "key": key,
                    "expected": e.expected,
                    "actual": e.actual,
                    "message": (
                        f"{key} is now {e.actual!r} in the vault, not {e.expected!r} "
                        "— someone else moved it"
                    ),
                },
            )
            return
        except VaultWriteError as e:
            self._json(502, {"error": "vault write failed", "key": key, "detail": str(e)})
            return

        self._json(200, {"ok": True, "key": key, **result})

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
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        if CLIENT_THREAD:
            CLIENT_THREAD.stop()


if __name__ == "__main__":
    main()
