#!/usr/bin/env python3
"""Append-only transition log, and the history/metrics derived from it.

The log replaces git commit timestamps as the timing source. One line per
transition, in `_log/transitions.md`:

    - 2026-08-26T20:45:47Z TKT-272 Backlog -> Ready (role=implement model=claude-opus-5 effort=high)

Trailing fields are `key=value` so a dimension can be added without a format
migration and an older parser keeps working.

APPENDS MUST BE SERIALISED. `POST /api/note/append` is a read-modify-write in Go
with no BaseHash and no transaction (note_service.go:1106-1141): 50 concurrent
appends to one note kept **1 line** and destroyed the pre-existing content,
while 50 serial appends kept all 50 (measured in Phase 0). The lock below is
what makes the log safe, and it is sufficient only because server.py is the
sole writer.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = "_log/transitions.md"

# The single writer's lock. See the module docstring: without this the log
# silently loses lines under concurrency.
_APPEND_LOCK = threading.Lock()

# ...but a threading lock only binds ONE process. `vault.py log` is a second
# writer by design (it repairs a line the server failed to write), and it can
# run while the server is appending -- two read-modify-writes with no shared
# lock, which is exactly the race that costs lines. A lock file makes the two
# processes serialise against each other.
_LOCK_FILE = Path(
    os.environ.get("SDLC_LOG_LOCK", Path(tempfile.gettempdir()) / "sdlc-translog.lock")
)


@contextlib.contextmanager
def _cross_process_lock(timeout=30.0):
    """flock the lock file, so a second process cannot append concurrently.

    Falls back to the in-process lock alone where flock is unavailable rather
    than refusing to write: a lost line is bad, a board that cannot log at all
    is worse.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    with open(_LOCK_FILE, "a+") as fh:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() > deadline:
                    msg = f"could not acquire {_LOCK_FILE} within {timeout}s"
                    raise TimeoutError(msg) from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

LINE_RE = re.compile(
    r"^- (?P<ts>\S+) (?P<key>[A-Z][A-Z0-9]*-\d+) "
    r"(?P<frm>.+?) -> (?P<to>.+?)(?: \((?P<fields>.*)\))?$"
)


def format_line(key: str, frm: str, to: str, actor: dict, when=None) -> str:
    ts = datetime.fromtimestamp(when or time.time(), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    fields = " ".join(
        f"{k}={v}" for k, v in sorted(actor.items()) if v not in (None, "")
    )
    suffix = f" ({fields})" if fields else ""
    return f"- {ts} {key} {frm} -> {to}{suffix}"


def parse_line(line: str):
    m = LINE_RE.match(line.strip())
    if not m:
        return None
    fields = {}
    for part in (m.group("fields") or "").split():
        k, _, v = part.partition("=")
        if k:
            fields[k] = v
    try:
        ts = int(
            datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None
    return {
        "ts": ts,
        "key": m.group("key"),
        "from": m.group("frm"),
        "to": m.group("to"),
        "fields": fields,
    }


def append_transition(read_note, write_note, key, frm, to, actor, when=None) -> str:
    """Append one line under the serialising lock.

    `read_note(path) -> content|None` and `write_note(path, content)` are
    injected so this stays testable without a live vault.

    A read-modify-write under our own lock is equivalent to POST /api/note/append
    and lets the log be created on first use.
    """
    line = format_line(key, frm, to, actor, when)
    with _APPEND_LOCK, _cross_process_lock():
        existing = read_note(LOG_PATH)
        if existing is None:
            body = (
                "# Transition log\n\n"
                "Append-only. One line per ticket transition; `server.py` is the "
                "only writer.\n\n" + line + "\n"
            )
        else:
            body = existing if existing.endswith("\n") else existing + "\n"
            body += line + "\n"
        write_note(LOG_PATH, body)
    return line


def read_events(log_text: str):
    """{key: [event, ...]}, oldest first, ordered by TIMESTAMP not file position.

    File order is not reliably chronological. Two transitions racing through
    `POST /ticket` are appended in whichever order wins the append lock, which
    is not necessarily the order the writes happened; and `vault.py log` repairs
    a missing line by appending it at the end, long after the transition it
    records. Several readers take `events[-1]` as "the latest", so trusting file
    order puts a stale status in `_since`, in `/metrics`, and in the date a
    dated archive uses.

    The sort is stable, so two lines sharing a second keep their file order.
    """
    events = {}
    for raw in (log_text or "").split("\n"):
        parsed = parse_line(raw)
        if parsed:
            events.setdefault(parsed["key"], []).append(parsed)
    for evs in events.values():
        evs.sort(key=lambda e: e["ts"])
    return events


def history(log_text: str, key: str):
    """Events for one ticket, shaped like the old git-derived /history."""
    out = []
    for e in read_events(log_text).get(key, []):
        actor = e["fields"]
        who = actor.get("role") or "?"
        if actor.get("model"):
            who += f"/{actor['model']}"
        out.append(
            {
                "ts": e["ts"],
                "status": e["to"],
                "labels": [],
                "subject": f"{key}: {e['from']} → {e['to']} ({who})",
            }
        )
    return out


def analyze(key, now, events, current_status=None):
    """Fold one ticket's transitions into durations.

    Mirrors the git-derived shape the board renders: per-status totals plus
    agent/human splits, keyed on the role that OWNED each interval -- the role
    recorded on the transition INTO a status is who held it until the next one.
    """
    if not events:
        return None
    per_status = {}
    agent = human = 0

    # The first event's `from` is the status the ticket occupied before the log
    # begins; we cannot know for how long, so timing starts at the first
    # transition.
    for cur, nxt in zip(events, [*events[1:], {"ts": now}]):
        dur = max(0, nxt["ts"] - cur["ts"])
        per_status[cur["to"]] = per_status.get(cur["to"], 0) + dur
        role = (cur["fields"].get("role") or "").lower()
        if role == "human":
            human += dur
        elif role:
            agent += dur

    last = events[-1]
    return {
        "key": key,
        "status": current_status or last["to"],
        "since": last["ts"],
        "created": events[0]["ts"],
        "perStatus": per_status,
        "agent": agent,
        "human": human,
        "total": now - events[0]["ts"],
    }


def all_analysis(log_text: str, now: int, statuses=None):
    statuses = statuses or {}
    out = {}
    for key, events in read_events(log_text).items():
        a = analyze(key, now, events, statuses.get(key))
        if a:
            out[key] = a
    return out
