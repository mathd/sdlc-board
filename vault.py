#!/usr/bin/env python3
"""Vault CLI for the SDLC board.

Subcommands:
  migrate    Read ticket JSON from the sdlc-state worktree, write one note per
             ticket into the FNS vault.
  selfcheck  Round-trip every ticket through note_from_ticket/ticket_from_note
             and report any field that does not survive.

Config comes from the environment:
  FNS_API     base URL, e.g. http://10.99.0.31:9000
  FNS_TOKEN   bearer token
  FNS_VAULT   vault name, e.g. sdlc
  FNS_CLIENT  x-client header value (must match the token's client restriction)
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- config

API = os.environ.get("FNS_API", "http://10.99.0.31:9000").rstrip("/")
TOKEN = os.environ.get("FNS_TOKEN", "")
VAULT = os.environ.get("FNS_VAULT", "sdlc")
CLIENT = os.environ.get("FNS_CLIENT", "sdlcBoard")

# Frontmatter carries only flat scalars and string lists, so Obsidian's
# properties panel can edit them (plan §4). Everything else round-trips
# through the JSON block in the body.
SCALAR_FIELDS = ("key", "status", "type", "parent", "classification", "assignee", "pr")
LIST_FIELDS = ("labels",)

# Rich structures kept verbatim in a fenced JSON block.
BLOCK_FIELDS = ("readiness", "links", "comments", "context", "comment")

DATA_FENCE = "```json sdlc-data"

# Fixed, unambiguous boundary between the description and whatever follows.
DESC_MARKER = "<!-- /description -->"
DESC_SEPARATOR = "\n" + DESC_MARKER
# A description may legitimately CONTAIN the marker -- a ticket about this very
# migration would. Escape it on the way out and restore it on the way in, so the
# boundary stays unambiguous without truncating real content.
#
# The escape must be INJECTIVE: escaping only the marker maps two distinct
# inputs (the raw marker, and text that already contains the escaped spelling)
# onto the same rendering, so the already-escaped one comes back altered. So
# escape the escape character first, and unescape in the mirror-image order.
DESC_MARKER_ESCAPED = "<!-- \\/description -->"
_ESC_SENTINEL = "<!-- \\\\"
_RAW_SENTINEL = "<!-- \\"


def _escape_desc(text: str) -> str:
    return text.replace(_RAW_SENTINEL, _ESC_SENTINEL).replace(
        DESC_MARKER, DESC_MARKER_ESCAPED
    )


def _unescape_desc(text: str) -> str:
    return text.replace(DESC_MARKER_ESCAPED, DESC_MARKER).replace(
        _ESC_SENTINEL, _RAW_SENTINEL
    )


# ---------------------------------------------------------------- hashing


def encode_hash32(content: str) -> str:
    """Port of the server's EncodeHash32 (pkg/util/hash.go:24-37).

    Iterates UTF-16 code units, not Python code points: a ticket containing an
    emoji hashes differently otherwise and every sync comparison for it fails.
    Verified against the live server in Phase 0.
    """
    raw = content.encode("utf-16-le")
    units = struct.unpack(f"<{len(raw) // 2}H", raw)
    h = 0
    for u in units:
        h = (h * 31 + u) & 0xFFFFFFFF
    if h >= 0x80000000:
        h -= 0x100000000
    return str(h)


# ---------------------------------------------------------------- yaml-lite


def _yaml_scalar(v) -> str:
    """Emit a YAML scalar. Strings are always JSON-quoted.

    JSON string syntax is a subset of YAML's double-quoted style, so this is
    correct for colons, backticks, quotes, newlines and emoji alike. Emitting
    raw scalars instead is the mutation §10 requires selfcheck to catch.
    """
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return json.dumps(str(v), ensure_ascii=False)


def _parse_yaml_scalar(s: str):
    s = s.strip()
    if s in ("null", "~", ""):
        return None
    if s == "true":
        return True
    if s == "false":
        return False
    if s.startswith('"'):
        return json.loads(s)
    try:
        return int(s)
    except ValueError:
        return s


# ---------------------------------------------------------------- mapping


def flatten_pr(pr):
    """The source has five pr shapes; the board only ever reads the URL.

    Returns the URL string (or None). The full object is preserved in the data
    block, so number/state/mergeCommit/branch are not lost.
    """
    if pr is None:
        return None
    if isinstance(pr, str):
        return pr
    if isinstance(pr, dict):
        return pr.get("url")
    return None


def note_from_ticket(t: dict) -> str:
    """Render a ticket dict as note text: frontmatter + body."""
    lines = ["---"]
    for f in SCALAR_FIELDS:
        v = flatten_pr(t.get("pr")) if f == "pr" else t.get(f)
        lines.append(f"{f}: {_yaml_scalar(v)}")
    for f in LIST_FIELDS:
        vals = t.get(f) or []
        rendered = ", ".join(_yaml_scalar(x) for x in vals)
        lines.append(f"{f}: [{rendered}]")
    lines.append("---")
    lines.append("")

    # One ticket in the corpus uses `title` instead of `summary`. The heading
    # renders from either, and parsing back derives `summary` from the heading,
    # so such a ticket gains a `summary` equal to its `title`. That is
    # deliberate normalisation, not loss: the board reads `summary`, and the
    # original `title` is preserved verbatim in the data block.
    title = t.get("summary") or t.get("title") or ""
    lines.append(f"# {t.get('key', '')}: {title}".rstrip())
    lines.append("")

    for link in t.get("links") or []:
        lines.append(f"> {link.get('type')} [[{link.get('key')}]]")
    if t.get("links"):
        lines.append("")

    desc = t.get("description")
    if desc:
        # The description is emitted VERBATIM followed by a fixed two-newline
        # separator. Because the separator is a constant, the parser can remove
        # exactly it and recover a description whether or not it ends in a
        # newline of its own -- descriptions in the corpus do both.
        lines.append(_escape_desc(desc) + DESC_SEPARATOR)

    # Everything the frontmatter cannot hold, verbatim and recoverable.
    # `pr` rides along whenever it is not a bare URL string, so the object's
    # other keys survive the flattening above.
    extra = {f: t[f] for f in BLOCK_FIELDS if f in t}
    for f in ("summary", "title", "description", "branch", "risk"):
        if f in t:
            extra[f] = t[f]
    pr = t.get("pr")
    if isinstance(pr, dict):
        extra["pr"] = pr
    if extra:
        lines.append(DATA_FENCE)
        lines.append(json.dumps(extra, indent=2, ensure_ascii=False, sort_keys=True))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def ticket_from_note(text: str) -> dict:
    """Inverse of note_from_ticket. Used by selfcheck and the read path."""
    t: dict = {}
    if not text.startswith("---\n"):
        msg = "note has no frontmatter"
        raise ValueError(msg)
    end = text.index("\n---", 4)
    fm = text[4:end]
    body = text[end + len("\n---") :].lstrip("\n")

    for line in fm.split("\n"):
        if not line.strip() or ":" not in line:
            continue
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            t[k] = json.loads(f"[{inner}]") if inner else []
        else:
            t[k] = _parse_yaml_scalar(v)

    prose = body
    if DATA_FENCE in body:
        start = body.index(DATA_FENCE) + len(DATA_FENCE)
        blob = body[start:]
        blob = blob[: blob.index("\n```")]
        t.update(json.loads(blob))
        prose = body[: body.index(DATA_FENCE)]

    # The RENDERED body wins over the data block for the two fields a human can
    # reasonably retype in Obsidian. Deriving them from the block alone makes
    # the note display-only: a title or description edited in the editor is
    # silently erased on the next board write, which is a lost update the CAS
    # cannot see because the guarded field never changed.
    summary, description = _split_prose(prose, t.get("key", ""))
    if summary is not None:
        t["summary"] = summary
    if description is not None:
        t["description"] = description
    elif "description" in t and not (t.get("description") or "").strip():
        t.pop("description")
    return t


def _split_prose(prose: str, key: str):
    """Recover (summary, description) from a rendered note body.

    Returns (None, None) for anything that does not look like our own render,
    so a hand-written note cannot lose data by being misread.
    """
    lines = prose.split("\n")
    summary = None
    idx = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            heading = line[2:].strip()
            prefix = f"{key}:"
            summary = (
                heading[len(prefix) :].strip()
                if key and heading.startswith(prefix)
                else heading
            )
            idx = i + 1
            break
    else:
        return None, None

    rest = lines[idx:]
    while rest and (not rest[0].strip() or rest[0].startswith("> ")):
        rest.pop(0)  # blank line after the heading, and the wikilink block
    text = "\n".join(rest)
    if DESC_MARKER in text:
        # Cut at the explicit marker: unambiguous regardless of how the
        # description itself ends.
        description = text[: text.index(DESC_MARKER)]
        if description.endswith("\n"):
            description = description[:-1]
    else:
        description = text.strip("\n") or None
    if description:
        description = _unescape_desc(description)
    return summary, (description or None)


# ---------------------------------------------------------------- transport


def _request(method: str, path: str, payload=None, query: str = ""):
    url = f"{API}{path}{query}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("x-client", CLIENT)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def put_note(path: str, content: str) -> dict:
    now_ms = int(time.time() * 1000)
    return _request(
        "POST",
        "/api/note",
        {
            "vault": VAULT,
            "path": path,
            "content": content,
            "contentHash": encode_hash32(content),
            "mtime": now_ms,
            "ctime": now_ms,
        },
    )


def get_note(path: str) -> dict:
    q = f"?vault={VAULT}&path={urllib.parse.quote(path)}"
    return _request("GET", "/api/note", query=q)


# ---------------------------------------------------------------- commands


def load_tickets(src: Path) -> list[dict]:
    out = []
    for p in sorted(src.glob("*.json")):
        out.append(json.loads(p.read_text(encoding="utf-8")))
    return out


def cmd_selfcheck(args) -> int:
    """Round-trip every ticket in memory and report lossy fields.

    No network. This is the §10 "round trip is lossless" test; the mutation it
    must catch is emitting raw YAML scalars instead of JSON-encoded ones.
    """
    tickets = load_tickets(Path(args.source))
    bad = 0
    for t in tickets:
        got = ticket_from_note(note_from_ticket(t))
        for k, want in t.items():
            if k == "pr":
                # pr is deliberately flattened in frontmatter; the object is
                # restored from the data block when it was an object.
                if got.get("pr") != (want if isinstance(want, dict) else flatten_pr(want)):
                    print(f"  {t['key']}: pr differs")
                    bad += 1
                continue
            if got.get(k) != want:
                print(f"  {t['key']}: field {k!r} differs")
                print(f"      want {json.dumps(want, ensure_ascii=False)[:160]}")
                print(f"      got  {json.dumps(got.get(k), ensure_ascii=False)[:160]}")
                bad += 1
    print(f"selfcheck: {len(tickets)} tickets, {bad} lossy field(s)")
    return 1 if bad else 0


def cmd_migrate(args) -> int:
    if not TOKEN:
        print("FNS_TOKEN is not set", file=sys.stderr)
        return 2
    tickets = load_tickets(Path(args.source))
    if args.limit:
        tickets = tickets[: args.limit]
    ok = fail = 0
    for t in tickets:
        key = t.get("key")
        path = f"tickets/{key}.md"
        content = note_from_ticket(t)
        if args.dry_run:
            print(f"  would write {path} ({len(content)} chars)")
            ok += 1
            continue
        try:
            r = put_note(path, content)
            if r.get("code") == 1:
                ok += 1
            else:
                print(f"  {key}: {r.get('message')}", file=sys.stderr)
                fail += 1
        except urllib.error.HTTPError as e:
            print(f"  {key}: HTTP {e.code} {e.read()[:200]!r}", file=sys.stderr)
            fail += 1
        if ok % 25 == 0 and not args.dry_run:
            print(f"  ... {ok} written")
    print(f"migrate: {ok} written, {fail} failed")
    return 1 if fail else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    default_src = (
        Path.home() / "sources" / "ticketing_system.sdlc-state" / ".sdlc" / "tickets"
    )

    m = sub.add_parser("migrate", help="write ticket JSON into the vault as notes")
    m.add_argument("--source", default=str(default_src))
    m.add_argument("--limit", type=int, default=0)
    m.add_argument("--dry-run", action="store_true")
    m.set_defaults(func=cmd_migrate)

    s = sub.add_parser("selfcheck", help="round-trip tickets, report lossy fields")
    s.add_argument("--source", default=str(default_src))
    s.set_defaults(func=cmd_selfcheck)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  (used by get_note)

    sys.exit(main())
