#!/usr/bin/env python3
"""Local SDLC board server, backed by a Fast Note Sync vault.

Ticket state lives in an FNS vault, not in git. A read-only WebSocket client
(fnsclient.py) mirrors the vault in memory and on disk; this server renders the
board from that mirror.

  GET  /board          -> config + tickets, from the in-memory mirror
  GET  /history?key=K  -> one ticket's transitions, from _log/transitions.md
  GET  /metrics        -> stage durations, from _log/transitions.md
  GET  /vaults         -> the vaults this board can serve
  POST /ticket         -> compare-and-swap one ticket's status into the vault
  POST /archive        -> move Done tickets into archive/ (?before=YYYY-MM-DD)

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
import urllib.error
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import boardconfig
import translog
from fnsclient import FNSReadClient, VaultMirror
from vault import get_note as vault_get_note
from vault import note_from_ticket, ticket_from_note
from vault import move_note as vault_move_note
from vault import put_note as vault_put_note

DIR = Path(__file__).resolve().parent
CONFIG = DIR / "config.json"
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")

API = os.environ.get("FNS_API", "http://10.99.0.31:9000")
TOKEN = os.environ.get("FNS_TOKEN", "")
VAULT = os.environ.get("FNS_VAULT", "sdlc")  # the default/selected vault
CLIENT = os.environ.get("FNS_CLIENT", "sdlcBoard")
MIRROR_DIR = Path(os.environ.get("SDLC_MIRROR", Path.home() / ".cache" / "sdlc-board"))

# A note is a ticket only if it lives here. _board/ and _log/ are not tickets.
TICKET_PREFIX = "tickets/"

# The only files the static handler may serve.
STATIC_ALLOWED = frozenset({"/board.html", "/favicon.ico"})

import vault as _vault

_vault.API, _vault.TOKEN, _vault.VAULT, _vault.CLIENT = API.rstrip("/"), TOKEN, VAULT, CLIENT

def discover_vaults():
    """Every vault this token may reach, from GET /api/vault.

    The switcher is populated from this. One WebSocket connection serves every
    vault (broadcasts are per user, tagged with the vault), so switching is a
    filter over messages already arriving -- no reconnect.
    """
    try:
        r = _vault._request("GET", "/api/vault")
    except Exception as e:  # noqa: BLE001 - offline start
        print(f"[board] cannot list vaults ({e}); using {VAULT} only", file=sys.stderr)
        return [VAULT]
    rows = r.get("data") or []
    names = [row.get("vault") for row in rows if row.get("vault")]
    if VAULT not in names:
        names.append(VAULT)
    return names or [VAULT]


VAULTS = discover_vaults() if TOKEN else [VAULT]
MIRROR = VaultMirror(MIRROR_DIR, VAULTS)
CLIENT_THREAD = None


def sort_key(t):
    prefix, _, num = t["key"].rpartition("-")
    return (prefix, int(num))


def _log_text(vault=None):
    """The transition log, served from the MIRROR -- no vault round trip.

    The log is a note like any other, so the WebSocket mirror already holds a
    current copy and it stays readable when the vault is unreachable.
    """
    return MIRROR.snapshot(vault or VAULT).get(translog.LOG_PATH, "")


def board_config(vault=None):
    """This vault's `_board/config.md`, from the mirror."""
    text = MIRROR.snapshot(vault or VAULT).get(boardconfig.CONFIG_PATH, "")
    return boardconfig.parse(text)


def known_vaults():
    """Vaults the board can serve: those discovered at startup plus any the
    mirror has since seen on the shared socket. Reading the frozen startup list
    alone leaves a newly authorised vault permanently unreachable."""
    with MIRROR.lock:
        seen = list(MIRROR.vaults)
    return sorted({*VAULTS, *seen})


def _selected_vault(query):
    """The vault named by ?vault=, restricted to what the token may reach."""
    want = parse_qs(query).get("vault", [""])[0]
    return want if want in known_vaults() else VAULT


def tickets_from_mirror(vault=None):
    """Parse every ticket note in the mirror into a ticket dict.

    Notes that fail to parse are skipped rather than taking the board down: a
    hand-edit in Obsidian should not blank the board.
    """
    out = []
    for path, content in MIRROR.snapshot(vault or VAULT).items():
        if not path.startswith(TICKET_PREFIX):
            continue
        try:
            t = ticket_from_note(content)
        except (ValueError, KeyError) as e:
            print(f"[board] skipping unparseable note {path}: {e}", file=sys.stderr)
            continue
        # Validate the key on the READ path too. It is interpolated into
        # inline event handlers in board.html, so a hand-edited key like
        # `X');fetch('/archive',{method:'POST'});//-1` executes as JavaScript
        # with same-origin access to the write routes (executed). It also has
        # to be a string that sort_key can split, or one malformed note takes
        # the whole board down with an AttributeError.
        key = t.get("key")
        if not isinstance(key, str) or not KEY_RE.match(key):
            print(f"[board] skipping note with invalid key {key!r}: {path}",
                  file=sys.stderr)
            continue
        if not isinstance(t.get("status"), str):
            print(f"[board] skipping {key}: status is not a string", file=sys.stderr)
            continue
        if not isinstance(t.get("labels"), list):
            t["labels"] = []
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
# The only fields a board action may change.
BOARD_WRITABLE_FIELDS = ("status", "labels", "assignee", "pr", "classification",
                         "parent", "type")

# `comments` is NOT in that set, because a drag sends the browser's whole stale
# array and replacing the vault's with it would drop any comment an agent added
# meanwhile. But the gate buttons legitimately append one, so comments are
# APPENDED, never replaced: whatever the client sends beyond what the vault
# already holds is added to the end, and nothing is ever removed by a board
# write.

def _log_read(path, vault=None):
    """Read the transition log.

    Returns its content, `translog.MISSING` when the vault positively says the
    note does not exist, or None when it could not be read at all. The caller
    MUST NOT treat the last case as absence -- doing so overwrites the log.
    """
    try:
        envelope = vault_get_note(path, vault)
    except urllib.error.HTTPError as e:
        # 404/430 is a real "not found"; anything else is a failure to read.
        return translog.MISSING if e.code in (404, 430) else None
    except Exception:  # noqa: BLE001 - unreachable vault, timeout, bad payload
        return None
    code = envelope.get("code")
    data = envelope.get("data")
    if data and data.get("content") is not None:
        return data["content"]
    if code in (430,) or (code != 1 and data is None):
        # The service answered and said there is no such note.
        return translog.MISSING if code == 430 else None
    return translog.MISSING


def _log_write(path, content, vault=None):
    r = vault_put_note(path, content, vault)
    if r.get("code") != 1:
        raise VaultWriteError(f"{r.get('code')} {r.get('message')}")


# Transition timestamps are whole seconds, so two writes in the same second
# would sort ambiguously. Handing out a strictly increasing value under the
# write lock keeps the log's order equal to the order the writes committed.
_LAST_TS = [0]


def _next_transition_time():
    """A non-decreasing transition timestamp, unique per write. Call under WRITE_LOCK."""
    now = int(time.time())
    if now <= _LAST_TS[0]:
        now = _LAST_TS[0] + 1
    _LAST_TS[0] = now
    return now


def _comment_id(comment):
    """Identity of a comment, for append-only merging.

    Content-based on purpose: the board and the agents do not assign comment
    ids, so identity has to come from the comment itself. Re-sending an
    identical comment is therefore a no-op rather than a duplicate, which is
    the behaviour a retry needs.
    """
    if not isinstance(comment, dict):
        return json.dumps(comment, sort_keys=True, ensure_ascii=False)
    return json.dumps(
        {k: comment.get(k) for k in ("stage", "kind", "author", "body")},
        sort_keys=True,
        ensure_ascii=False,
    )


MAX_CAS_ATTEMPTS = 3  # bound the retry so a hot note cannot spin

# One writer at a time from this process. Two browser tabs on the same board
# would otherwise interleave read-modify-write on one note, which is exactly the
# race the CAS exists to catch -- and a local lock closes it without a round
# trip. It does NOT protect against another machine's server.py; that is what
# the status check below is for.
WRITE_LOCK = threading.Lock()


def write_ticket(key, ticket, moving_from, actor, vault=None):
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
                envelope = vault_get_note(path, vault)
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

            # The note must be the ticket we were asked to write. A mismatched
            # key means we would apply this status change to a different
            # ticket while reporting success for this one.
            if existing.get("key") != key:
                raise VaultWriteError(
                    f"{path} holds key {existing.get('key')!r}, not {key!r}"
                )

            vault_status = existing.get("status")
            # The guard.
            if vault_status != moving_from:
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

            # Append-only comments (see BOARD_WRITABLE_FIELDS above), merged by
            # IDENTITY rather than by position.
            #
            # Counting -- "take incoming[len(current):]" -- assumes the vault's
            # comments are an unchanged prefix of the client's, and nothing
            # checks that. Two ways it lost data: a gate comment added while an
            # agent appended one concurrently gives equal lengths and the gate
            # comment is dropped; and a reordered longer payload re-appends
            # comments the vault already has. Both executed.
            incoming = ticket.get("comments")
            if isinstance(incoming, list):
                current = existing.get("comments") or []
                have = {_comment_id(c) for c in current}
                added = [c for c in incoming if _comment_id(c) not in have]
                if added:
                    merged["comments"] = current + added
                else:
                    merged["comments"] = current
            new_content = note_from_ticket(merged)
            if new_content == content:
                return {"status": new_status, "attempts": attempt, "noop": True}

            try:
                r = vault_put_note(path, new_content, vault)
            except Exception as e:  # noqa: BLE001
                raise VaultWriteError(str(e)) from e
            if r.get("code") == 1:
                return {
                    "status": new_status,
                    "attempts": attempt,
                    "moved_from": vault_status,
                    # Stamped INSIDE WRITE_LOCK, where the order of two racing
                    # writes is already decided. Stamping later -- when the
                    # append lock is acquired, which is a separate lock taken
                    # after this one is released -- lets two transitions be
                    # recorded in the opposite order to the one they happened
                    # in, and no amount of sorting afterwards can repair a
                    # timestamp that was wrong when written.
                    "at": _next_transition_time(),
                }
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

        if url.path == "/vaults":
            self._json(200, {"vaults": known_vaults(), "selected": VAULT})
            return

        if url.path == "/board":
            vault = _selected_vault(url.query)
            board = json.loads(CONFIG.read_text(encoding="utf-8"))
            try:
                cfg = board_config(vault)
            except boardconfig.UnsupportedSchema as e:
                # Refuse to render rather than guess at a config we do not
                # understand (plan §4).
                self._json(
                    200,
                    {
                        "vault": vault,
                        "vaults": VAULTS,
                        "tickets": [],
                        "now": int(time.time()),
                        "unsupportedSchema": {"found": e.found, "supported": e.supported},
                        "readOnly": True,
                        "readOnlyReason": str(e),
                    },
                )
                return
            board["statuses"] = cfg["statuses"]
            board["blocked"] = (cfg["transversal"] or ["BLOCKED"])[0]
            board["transversal"] = cfg["transversal"]
            board["wip"] = cfg["wip"]
            if cfg.get("project"):
                board["project"] = cfg["project"]
            board["vault"] = vault
            board["vaults"] = known_vaults()
            tickets = tickets_from_mirror(vault)
            # _since drives the age chip on each card: when the ticket last
            # changed status, from the transition log.
            events = translog.read_events(_log_text(vault))
            for t in tickets:
                ev = events.get(t["key"])
                if ev:
                    t["_since"] = ev[-1]["ts"]
            board["tickets"] = tickets
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
            vault = _selected_vault(url.query)
            self._json(
                200,
                {"key": key, "events": translog.history(_log_text(vault), key)},
            )
            return

        if url.path == "/metrics":
            vault = _selected_vault(url.query)
            now = int(time.time())
            statuses = {t["key"]: t.get("status") for t in tickets_from_mirror(vault)}
            rows = translog.all_analysis(_log_text(vault), now, statuses)
            self._json(
                200,
                {"now": now, "tickets": sorted(rows.values(), key=sort_key)},
            )
            return

        # Serve ONLY the board's own assets. SimpleHTTPRequestHandler's document
        # root is this repository, so the default behaviour served vault.py,
        # .git/config and directory listings -- and would serve a token.env
        # dropped beside server.py, which is exactly the filename .gitignore
        # anticipates (executed). Parent traversal was already handled; the
        # problem was the root itself.
        if url.path in ("/", "/board.html"):
            self.path = "/board.html"
        elif url.path not in STATIC_ALLOWED:
            self.send_error(404)
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
            self._archive(urlparse(self.path).query)
            return
        self.send_error(404)

    def _archive(self, query=""):
        """Move Done tickets into `archive/`.

        `?before=YYYY-MM-DD` archives only tickets that entered Done before that
        day; no parameter archives every Done ticket. The cutoff comes from the
        transition log, which is the only timing source now -- a ticket with no
        logged transition has no known Done date, so a dated archive skips it
        rather than guessing.
        """
        params = parse_qs(query)
        vault = _selected_vault(query)
        before = params.get("before", [""])[0]
        cutoff = None
        if before:
            try:
                cutoff = time.mktime(time.strptime(before, "%Y-%m-%d"))
            except ValueError:
                self.send_error(400, "bad before date (want YYYY-MM-DD)")
                return

        events = translog.read_events(_log_text(vault))
        moved, failed, skipped = [], [], []
        for t in tickets_from_mirror(vault):
            if t.get("status") != "Done":
                continue
            key = t["key"]
            if cutoff is not None:
                # The date must come from the transition that entered Done, not
                # from whatever happened last. A Done ticket whose Done
                # transition was never logged has NO known Done date, and using
                # an unrelated transition's date archives it on a guess.
                ev = events.get(key) or []
                done_at = next(
                    (e["ts"] for e in reversed(ev) if e["to"] == "Done"), None
                )
                if done_at is None:
                    skipped.append(key)
                    continue
                if done_at >= cutoff:
                    continue
            src = f"{TICKET_PREFIX}{key}.md"
            # Re-read the LIVE note before moving it. Archive eligibility came
            # from the mirror, which is stale whenever the WebSocket is behind
            # or down -- and moving a ticket that is no longer Done makes an
            # active ticket vanish from the board.
            try:
                fresh = ticket_from_note(
                    (vault_get_note(src, vault).get("data") or {})["content"]
                )
            except Exception as e:  # noqa: BLE001
                failed.append({"key": key, "error": f"could not re-read: {e}"[:120]})
                continue
            if fresh.get("status") != "Done":
                skipped.append(key)
                continue
            try:
                r = vault_move_note(src, f"archive/{key}.md", vault)
            except Exception as e:  # noqa: BLE001
                failed.append({"key": key, "error": str(e)[:120]})
                continue
            if r.get("code") == 1:
                moved.append(key)
            else:
                failed.append({"key": key, "error": f"{r.get('code')} {r.get('message')}"})

        self._json(
            200,
            {
                "ok": not failed,
                "vault": vault,
                "archived": sorted(moved, key=lambda k: sort_key({"key": k})),
                "failed": failed,
                "skippedNoLoggedDate": sorted(
                    skipped, key=lambda k: sort_key({"key": k})
                ),
            },
        )

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
        # The status must be one the selected vault actually has. Checking only
        # that it is a string let a typo ("Buidling") persist into a state no
        # column renders, and let any local REST caller bypass every workflow
        # and DoR gate in the UI.
        vault_for_cfg = _selected_vault(urlparse(self.path).query)
        try:
            cfg = board_config(vault_for_cfg)
        except boardconfig.UnsupportedSchema:
            cfg = boardconfig.DEFAULTS
        allowed = list(cfg["statuses"]) + list(cfg["transversal"])
        if ticket["status"] not in allowed:
            self._json(
                400,
                {
                    "error": "unknown status",
                    "key": ticket.get("key"),
                    "status": ticket["status"],
                    "allowed": allowed,
                    "message": (
                        f"{ticket['status']!r} is not a status of vault "
                        f"{vault_for_cfg!r}"
                    ),
                },
            )
            return

        moving_from = ticket.pop("_rev", None)
        # The guard is NOT optional. Without `_rev` there is nothing to compare
        # against, so the write would overwrite whatever the vault holds -- a
        # silent lost update, verified: a POST omitting _rev moved a ticket
        # Backlog -> Done straight through the CAS. Refuse instead.
        if not isinstance(moving_from, str) or not moving_from:
            self._json(
                400,
                {
                    "error": "missing _rev",
                    "key": key,
                    "message": (
                        "_rev is required: it names the status this write expects "
                        "the vault to hold, and without it the write cannot be checked"
                    ),
                },
            )
            return
        ticket.pop("_since", None)  # server-computed, never stored
        # The actor is supplied by the caller; the server cannot infer it.
        actor = {
            "role": ticket.pop("role", None) or "human",
            "model": ticket.pop("model", None) or "",
            "effort": ticket.pop("effort", None) or "",
        }

        vault = _selected_vault(urlparse(self.path).query)
        try:
            result = write_ticket(key, ticket, moving_from, actor, vault)
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

        # Append AFTER the write is confirmed. The two have no transaction
        # between them, so pick the failure you prefer: appending first records
        # transitions that never happened and corrupts /metrics with no way to
        # tell, while appending second loses a line when the append fails --
        # leaving /metrics incomplete but never wrong. Take the second, and log
        # the failure locally so the gap is visible.
        moved_from = result.pop("moved_from", None)
        happened_at = result.pop("at", None)
        if moved_from is not None and moved_from != result["status"]:
            try:
                translog.append_transition(
                    lambda p: _log_read(p, vault),
                    lambda p, c: _log_write(p, c, vault),
                    key,
                    moved_from,
                    result["status"],
                    actor,
                    when=happened_at,
                )
            except Exception as e:  # noqa: BLE001 - never fail the write on this
                print(
                    f"[board] TRANSITION LOG GAP: {key} {moved_from} -> "
                    f"{result['status']} was not logged: {e}",
                    file=sys.stderr,
                )
                result["logged"] = False

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
        CLIENT_THREAD = FNSReadClient(API, TOKEN, VAULTS, MIRROR, client_type=CLIENT)
        CLIENT_THREAD.start()

    print(f"sdlc board on http://localhost:{port}/board.html")
    print(f"vaults: {', '.join(VAULTS)} at {API} (selected: {VAULT})")
    print(f"mirror: {MIRROR_DIR} ({len(MIRROR.snapshot(VAULT))} notes on disk)")
    try:
        ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
    except KeyboardInterrupt:
        if CLIENT_THREAD:
            CLIENT_THREAD.stop()


if __name__ == "__main__":
    main()
