#!/usr/bin/env python3
"""Local SDLC tracker server.

Ticket state is metadata about the work, not the work — it lives on a dedicated git branch
(default: sdlc-state), never on feature branches, so feature PRs stay state-free and the state
history is a serialized, conflict-free line (one file per ticket; every transition = one commit).

On startup the server self-bootstraps a worktree of that branch next to the repo
(<repo>.<branch>, plain `git worktree` — no extra tooling needed) and serves/writes tickets there:

  GET  /board          -> config + tickets (each with _since: epoch of last status change)
  GET  /history?key=K  -> per-ticket audit trail from the state-branch git log
  GET  /metrics        -> per-ticket stage durations computed from the git timeline
  POST /ticket         -> upsert one ticket file in the state worktree, auto-committed

Metrics are exact, not hand-tracked: every mutation is a commit, so the timeline is the
git history of the ticket's file. Computations are cached per state-branch HEAD.

Localhost-only. Run from any checkout: python3 .sdlc/server.py [port]
"""

import hashlib
import json
import re
import subprocess  # noqa: S404 — fixed git commands, validated args only
import sys
import time
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DIR = Path(__file__).resolve().parent  # .sdlc/ in the developer's normal checkout
REPO = DIR.parent
CONFIG = DIR / "config.json"
KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def git(*args, cwd=REPO):
    return subprocess.run(  # noqa: S603 — args are literals + KEY_RE-validated keys
        ["git", "-C", str(cwd), *args],  # noqa: S607 — git resolved from PATH by design
        capture_output=True,
        text=True,
        check=False,
    )


def bootstrap_state_worktree():
    """Ensure the state branch + its worktree exist.

    Returns:
        (worktree_path, tickets_dir) inside the state worktree.
    """
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    branch = cfg.get("state", {}).get("branch", "sdlc-state")
    wt_path = REPO.parent / f"{REPO.name}.{branch}"
    if not (wt_path / ".git").exists():
        if git("rev-parse", "--verify", branch).returncode != 0:
            src = (
                branch
                if git("rev-parse", "--verify", f"origin/{branch}").returncode == 0
                else "HEAD"
            )
            git("branch", branch, src if src != branch else f"origin/{branch}")
        r = git("worktree", "add", str(wt_path), branch)
        if r.returncode != 0:
            sys.exit(
                f"cannot create state worktree at {wt_path}:\n{r.stderr}"
                f"(is '{branch}' already checked out somewhere else?)"
            )
        print(f"state worktree created: {wt_path} [{branch}]")
    tickets = wt_path / ".sdlc" / "tickets"
    tickets.mkdir(parents=True, exist_ok=True)
    return wt_path, tickets


STATE_WT, TICKETS = bootstrap_state_worktree()


def sort_key(t):
    prefix, _, num = t["key"].rpartition("-")
    return (prefix, int(num))


def validate(t):
    key = t.get("key", "")
    if not KEY_RE.match(key):
        msg = f"bad ticket key: {key!r}"
        raise ValueError(msg)
    if not isinstance(t.get("status"), str):
        msg = "status must be a string"
        raise ValueError(msg)
    if not isinstance(t.get("labels"), list):
        msg = "labels must be a list"
        raise ValueError(msg)
    readiness = t.get("readiness")
    if readiness is not None:
        if not isinstance(readiness, dict):
            msg = "readiness must be an object"
            raise ValueError(msg)
        for item, v in readiness.items():
            if not isinstance(v, dict) or v.get("state") not in {
                "met",
                "open",
                "deferred",
            }:
                msg = f"readiness.{item}.state must be met|open|deferred"
                raise ValueError(msg)
    return key


def file_rev(path):
    """Content revision of a ticket file, for optimistic concurrency.

    Returns:
        First 12 hex chars of the file's sha256.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def commit_state(key, message):
    rel = f".sdlc/tickets/{key}.json"
    git("add", rel, cwd=STATE_WT)
    # chore(<KEY>) keeps commitizen-style hooks happy while staying greppable per ticket;
    # [skip ci] keeps state pushes from triggering CI in repos with unfiltered `on: push`
    r = git("commit", "-m", f"chore({key}): {message} [skip ci]", cwd=STATE_WT)
    if r.returncode != 0 and "nothing to commit" not in r.stdout + r.stderr:
        print(f"state commit failed for {key}:\n{r.stdout}{r.stderr}", file=sys.stderr)


# --- git-derived timelines & metrics (cached per state-branch HEAD) ---------------------

_CACHE = {"head": None, "analysis": {}}


def all_events():
    """Snapshot every ticket at each commit of its file, in two git calls.

    One `log --raw` maps commits -> (ticket, blob); one `cat-file --batch` reads
    all blobs. Per-commit `git show` spawns made cold /board take ~30s at ~100 tickets.

    Returns:
        {key: chronological list of {ts, status, labels, subject}}.
    """
    log = git(
        "log", "--reverse", "--format=C\t%H\t%at\t%s", "--raw", "--no-renames",
        "--no-abbrev", "--", ".sdlc/tickets", cwd=STATE_WT,
    )
    commits = []  # (ts, subject, [(blob, key)])
    for line in log.stdout.splitlines():
        if line.startswith("C\t"):
            _, _sha, ts, subject = line.split("\t", 3)
            commits.append((int(ts), subject, []))
        elif line.startswith(":"):
            meta, _, path = line.partition("\t")
            blob = meta.split()[3]  # post-image blob; all-zeros on deletion
            if path.endswith(".json") and blob.strip("0"):
                commits[-1][2].append((blob, Path(path).stem))
    blobs = {b for _, _, files in commits for b, _ in files}
    raw = subprocess.run(  # noqa: S603,S607 — fixed argv, blob shas from git itself
        ["git", "-C", str(STATE_WT), "cat-file", "--batch"],
        input="\n".join(blobs).encode(), capture_output=True, check=False,
    )
    content, out, i = {}, raw.stdout, 0
    while i < len(out):
        nl = out.index(b"\n", i)
        header = out[i:nl].split()
        i = nl + 1
        if len(header) < 3 or header[1] != b"blob":
            continue  # "<sha> missing" — no payload follows
        size = int(header[2])
        content[header[0].decode()] = out[i : i + size]
        i += size + 1  # skip payload + trailing newline
    events = {}
    for ts, subject, files in commits:
        for blob, key in files:
            try:
                snap = json.loads(content.get(blob, b""))
            except ValueError:
                continue
            events.setdefault(key, []).append(
                {
                    "ts": ts,
                    "status": snap.get("status"),
                    "labels": snap.get("labels", []),
                    "subject": subject.replace(" [skip ci]", ""),
                }
            )
    return events


def analyze(key, now, ev):
    """Fold a ticket's event timeline into durations.

    Returns:
        {key, status, since, created, perStatus, agent, human, total, events} or None.
    """
    if not ev:
        return None
    per_status = {}
    agent = human = 0
    for cur, nxt in zip(ev, [*ev[1:], {"ts": now}]):
        dur = max(0, nxt["ts"] - cur["ts"])
        per_status[cur["status"]] = per_status.get(cur["status"], 0) + dur
        if any(str(label).startswith("agent:") for label in cur["labels"]):
            agent += dur
        if "needs:human" in cur["labels"]:
            human += dur
    since = ev[0]["ts"]
    for prev, cur in zip(ev, ev[1:]):
        if cur["status"] != prev["status"]:
            since = cur["ts"]
    return {
        "key": key,
        "status": ev[-1]["status"],
        "since": since,
        "created": ev[0]["ts"],
        "perStatus": per_status,
        "agent": agent,
        "human": human,
        "total": now - ev[0]["ts"],
        "events": ev,
    }


def all_analysis():
    """Analysis for every ticket, recomputed only when the state branch advances.

    Returns:
        {key: analyze(key)} for all ticket files.
    """
    head = git("rev-parse", "HEAD", cwd=STATE_WT).stdout.strip()
    if _CACHE["head"] != head:
        now = int(time.time())
        events = all_events()
        _CACHE["analysis"] = {
            p.stem: analyze(p.stem, now, events.get(p.stem))
            for p in TICKETS.glob("*.json")
        }
        _CACHE["head"] = head
    return _CACHE["analysis"]


# -----------------------------------------------------------------------------------------


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
            analysis = all_analysis()
            tickets = []
            for p in TICKETS.glob("*.json"):
                t = json.loads(p.read_text(encoding="utf-8"))
                a = analysis.get(t["key"])
                if a:
                    t["_since"] = a["since"]
                t["_rev"] = file_rev(p)
                tickets.append(t)
            board["tickets"] = sorted(tickets, key=sort_key)
            board["now"] = int(time.time())
            self._json(200, board)
            return
        if url.path == "/history":
            key = parse_qs(url.query).get("key", [""])[0]
            if not KEY_RE.match(key):
                self.send_error(400, "bad key")
                return
            a = all_analysis().get(key)
            self._json(200, {"key": key, "events": a["events"] if a else []})
            return
        if url.path == "/metrics":
            rows = [a for a in all_analysis().values() if a]
            for row in rows:
                row.pop("events", None)  # slim payload; /history has the detail
            self._json(
                200,
                {"now": int(time.time()), "tickets": sorted(rows, key=sort_key)},
            )
            return
        try:
            super().do_GET()
        except BrokenPipeError:
            pass  # client disconnected before the static file finished sending

    def do_POST(self):
        url = urlparse(self.path)
        route = url.path
        if route == "/archive":
            self._archive(url.query)
            return
        if route != "/ticket":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            t = json.loads(self.rfile.read(n))
            key = validate(t)
        except (ValueError, TypeError) as e:  # bad body -> never touch the files
            self.send_error(400, f"invalid ticket: {e}")
            return
        t.pop("_since", None)  # server-computed, never stored
        rev = t.pop("_rev", None)  # optimistic concurrency: reject stale writes
        path = TICKETS / f"{key}.json"
        if rev is not None and path.exists() and rev != file_rev(path):
            self.send_error(
                409, "stale write: ticket changed since read - re-fetch /board"
            )
            return
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        tmp = path.with_suffix(".json.tmp")  # atomic replace
        tmp.write_text(
            json.dumps(t, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(path)
        if old is None:
            msg = f"created ({t['status']})"
        elif old.get("status") != t["status"]:
            msg = f"{old.get('status')} → {t['status']}"
        else:
            msg = "update"
        commit_state(key, msg)
        self._json(200, {"ok": True, "key": key, "commit": msg})

    def _archive(self, query=""):
        """Move Done tickets to .sdlc/tickets/archive/ (one commit).

        ?before=YYYY-MM-DD archives only tickets that entered Done before that day;
        no param archives all Done tickets.
        """
        before = parse_qs(query).get("before", [""])[0]
        cutoff = None
        if before:
            try:
                cutoff = time.mktime(time.strptime(before, "%Y-%m-%d"))
            except ValueError:
                self.send_error(400, "bad before date (want YYYY-MM-DD)")
                return
        analysis = all_analysis()
        arch = TICKETS / "archive"
        arch.mkdir(exist_ok=True)
        moved = []
        for p in TICKETS.glob("*.json"):
            t = json.loads(p.read_text(encoding="utf-8"))
            if t.get("status") != "Done":
                continue
            a = analysis.get(t["key"])
            done_since = a["since"] if a else 0  # last status change = Done entry
            if cutoff is not None and done_since >= cutoff:
                continue
            p.rename(arch / p.name)
            moved.append(t["key"])
        if moved:
            git("add", "-A", cwd=STATE_WT)
            git(
                "commit",
                "-m",
                f"chore(sdlc): archive {len(moved)} done ticket(s) [skip ci]",
                cwd=STATE_WT,
            )
        self._json(200, {"ok": True, "archived": sorted(moved)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    print(f"sdlc board on http://localhost:{port}/board.html")
    print(
        f"state: {STATE_WT} (tickets committed per change; push when you want to share)"
    )
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
