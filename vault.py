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
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
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
    # DENYLIST, not an allowlist: everything that is not a flat frontmatter
    # scalar rides in the data block. An allowlist silently DELETED any field it
    # did not know about -- a schema extension or hand-added metadata would
    # vanish on the next board write, from the only live copy.
    extra = {
        k: v
        for k, v in t.items()
        if k not in SCALAR_FIELDS and k not in LIST_FIELDS
    }
    pr = t.get("pr")
    if isinstance(pr, dict):
        extra["pr"] = pr
    if extra:
        lines.append(DATA_FENCE)
        lines.append(json.dumps(extra, indent=2, ensure_ascii=False, sort_keys=True))
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


# The order the source JSON uses. A pull writes ticket files back for the git
# board, and §8's cutover check is a daily `git diff`: if the key order changes,
# every file shows as modified and the diff stops meaning "the boards disagree".
FIELD_ORDER = (
    "key", "type", "parent", "classification", "status", "labels", "assignee",
    "pr", "summary", "title", "description", "links", "readiness", "context",
    "comments", "comment", "branch", "risk",
)


# Nested objects are rendered with sort_keys=True in the note (that keeps note
# content deterministic, which the content-hash comparison relies on), so their
# key order must be restored here instead.
NESTED_ORDER = {
    "pr": ("number", "url", "state", "mergeCommit", "merge_commit", "branch"),
    "comment": ("stage", "kind", "author", "body"),
}
COMMENT_ORDER = ("stage", "kind", "author", "body")
LINK_ORDER = ("type", "key")
READINESS_ORDER = ("state", "owner", "note")


def _reorder(value, order):
    if not isinstance(value, dict):
        return value
    out = {k: value[k] for k in order if k in value}
    out.update({k: v for k, v in value.items() if k not in out})
    return out


def _detect_indent(text: str):
    """The indent width a JSON file already uses, from its second line."""
    for line in text.split("\n")[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return None


def reorder_like(value, template):
    """Recursively restore `template`'s key order onto `value`.

    The note render uses sort_keys=True (it keeps note content deterministic,
    which the content-hash comparison depends on), so every nested object comes
    back alphabetised. Rather than hand-maintain an ordering table for each
    nested shape, take the order from the file we are about to overwrite: the
    cutover diff should show DRIFT, not formatting.
    """
    if isinstance(value, dict) and isinstance(template, dict):
        out = {}
        for k in template:
            if k in value:
                out[k] = reorder_like(value[k], template[k])
        for k, v in value.items():
            if k not in out:
                out[k] = v
        return out
    if isinstance(value, list) and isinstance(template, list):
        return [
            reorder_like(v, template[i]) if i < len(template) else v
            for i, v in enumerate(value)
        ]
    return value


# Frontmatter always emits these, even when the source JSON omitted them, so a
# pull would otherwise add `"parent": null` to every ticket that never had one
# and make the cutover diff noisy with changes that carry no information.
DROP_IF_NULL = ("parent", "classification", "assignee", "pr")


def ordered_ticket(t: dict, keep_nulls=()) -> dict:
    """Re-key a ticket into the source's field order, extras last."""
    t = {
        k: v
        for k, v in t.items()
        if not (k in DROP_IF_NULL and v is None and k not in keep_nulls)
    }
    out = {k: t[k] for k in FIELD_ORDER if k in t}
    out.update({k: v for k, v in t.items() if k not in out})
    for field, order in NESTED_ORDER.items():
        if field in out:
            out[field] = _reorder(out[field], order)
    if isinstance(out.get("comments"), list):
        out["comments"] = [_reorder(c, COMMENT_ORDER) for c in out["comments"]]
    if isinstance(out.get("links"), list):
        out["links"] = [_reorder(x, LINK_ORDER) for x in out["links"]]
    if isinstance(out.get("readiness"), dict):
        out["readiness"] = {
            k: _reorder(v, READINESS_ORDER) for k, v in out["readiness"].items()
        }
    return out


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
    # Strip ONLY the wikilink block this renderer emits ("> type [[KEY]]"), and
    # the blank line after the heading. Dropping every leading "> " line eats a
    # description that legitimately opens with a Markdown blockquote.
    link_re = re.compile(r"^> \S+ \[\[[^\]]+\]\]$")
    while rest and (not rest[0].strip() or link_re.match(rest[0])):
        rest.pop(0)
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


def put_note(path: str, content: str, vault: str | None = None) -> dict:
    now_ms = int(time.time() * 1000)
    return _request(
        "POST",
        "/api/note",
        {
            "vault": vault or VAULT,
            "path": path,
            "content": content,
            "contentHash": encode_hash32(content),
            "mtime": now_ms,
            "ctime": now_ms,
        },
    )


def _list_note_paths(prefix="", page_size=100, vault=None):
    """Every note path in the vault, paginated.

    THE SERVER CAPS pageSize AT 100 and does not say so: asking for 1000 returns
    100 rows while `pager.totalRows` reports the real count. A single request
    therefore truncates silently, which is how a pull quietly wrote back only
    99 of 272 tickets. Always page until the rows run out.
    """
    # The ONLY reliable stop condition is an empty page. `len(rows) < page_size`
    # is wrong precisely because the server caps pageSize without saying so:
    # ask for 1000, get 100, and a short-page test ends the loop after one page
    # having silently seen a third of the vault.
    out, page = [], 1
    seen = 0
    while True:
        q = f"?vault={vault or VAULT}&page={page}&pageSize={page_size}"
        data = (_request("GET", "/api/notes", query=q) or {}).get("data") or {}
        rows = data.get("list") or []
        if not rows:
            break
        seen += len(rows)
        for r in rows:
            path = r.get("path", "")
            if path.startswith(prefix):
                out.append(path)
        total = (data.get("pager") or {}).get("totalRows")
        if total is not None and seen >= total:
            break
        page += 1
    return sorted(set(out))


def move_note(old_path: str, new_path: str, vault: str | None = None) -> dict:
    """Move a note within a vault.

    The route is `POST /api/note/rename` and it takes `oldPath` (source) and
    `path` (destination) -- NOT the `path`/`destination` pair of
    `NoteMoveRequest`, which is a DTO with no route bound to it. Getting this
    wrong returns `305 Invalid Params`.

    Verified to be a true move: the source leaves the note listing, the
    destination appears, and the total count is unchanged.
    """
    return _request(
        "POST",
        "/api/note/rename",
        {"vault": vault or VAULT, "oldPath": old_path, "path": new_path},
    )


def get_note(path: str, vault: str | None = None) -> dict:
    q = f"?vault={vault or VAULT}&path={urllib.parse.quote(path)}"
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


def cmd_pull(args) -> int:
    """Write vault state back into the sdlc-state worktree as ticket JSON.

    This is what makes the cutover rollback real (plan §8): the git board keeps
    working, read-only, fed by this. Phase 2 stripped git from `server.py`, not
    from here.

    It is one-way by design -- vault to git, never back. Bidirectional
    reconciliation is explicitly out of scope, and a git-to-vault path would
    give the archive a way to overwrite live state.
    """
    if not TOKEN:
        print("FNS_TOKEN is not set", file=sys.stderr)
        return 2

    tickets_dir = Path(args.into)
    if not tickets_dir.exists():
        print(f"no such directory: {tickets_dir}", file=sys.stderr)
        return 2

    paths = _list_note_paths(prefix="tickets/", page_size=args.page_size)

    written = skipped = 0
    seen = set()
    failed_paths = []
    for path in sorted(paths):
        try:
            note = (get_note(path).get("data") or {}).get("content")
        except Exception as e:  # noqa: BLE001 - a transient error must not crash a cron job
            print(f"  {path}: {type(e).__name__}: {e}", file=sys.stderr)
            skipped += 1
            failed_paths.append(path)
            continue
        if not note:
            skipped += 1
            continue
        try:
            ticket = ticket_from_note(note)
        except (ValueError, KeyError) as e:
            print(f"  {path}: unparseable ({e})", file=sys.stderr)
            skipped += 1
            continue
        key = ticket.get("key")
        if not key:
            skipped += 1
            continue
        seen.add(key)
        target = tickets_dir / f"{key}.json"
        shaped = ordered_ticket(ticket)
        if target.exists():
            try:
                template = json.loads(target.read_text(encoding="utf-8"))
            except ValueError:
                template = None
            if isinstance(template, dict):
                # Frontmatter always emits parent/classification/assignee/pr.
                # Keep a null the existing file HAD and drop one it did not, so
                # the cutover diff shows drift rather than formatting.
                keep = tuple(f for f in DROP_IF_NULL if f in template)
                for field in keep:
                    shaped.setdefault(field, None)
                # The existing file's own key order wins: FIELD_ORDER is only a
                # fallback for a ticket the worktree has never seen. Some source
                # files order their top-level keys differently, and imposing one
                # order on them turns the cutover diff into noise.
                shaped = reorder_like(
                    ordered_ticket(shaped, keep_nulls=keep), template
                )
        # Match the existing file's formatting exactly -- indent width and
        # trailing newline both vary across the corpus (a handful of tickets use
        # a 1-space indent, a few end without a newline). The cutover check in
        # §8 is a daily `git diff`, so any formatting we impose shows up as
        # drift that is not drift and makes the signal useless.
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        indent, newline = 2, True
        if existing:
            indent = _detect_indent(existing) or 2
            newline = existing.endswith("\n")
        body = json.dumps(shaped, indent=indent, ensure_ascii=False)
        if newline:
            body += "\n"
        if existing == body:
            continue
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(target)
        written += 1

    # A ticket archived in the vault should leave the git board too, or the two
    # drift and the daily diff never comes back clean.
    #
    # But prune ONLY on positive evidence that the vault no longer has the note.
    # A ticket is "not seen" both when it was archived and when its note failed
    # to fetch, was empty, or would not parse -- and deleting on the second
    # meaning destroys the git archive, which is the rollback copy, on a
    # transient error. Verified: an HTTP 500, an empty note and an unparseable
    # note each deleted a live ticket's JSON and still exited 0.
    removed = 0
    if args.prune:
        if skipped:
            print(
                f"prune SKIPPED: {skipped} note(s) could not be read, so an "
                f"absent ticket cannot be told from an unreadable one. "
                f"Re-run when the vault is healthy.",
                file=sys.stderr,
            )
        else:
            vault_keys = {
                Path(p).stem for p in paths
            }  # every ticket note the vault listed
            for existing in sorted(tickets_dir.glob("*.json")):
                if existing.stem not in vault_keys:
                    existing.unlink()
                    removed += 1

    print(
        f"pull: {written} written, {skipped} skipped, {removed} pruned "
        f"({len(seen)} tickets in the vault)"
    )
    # A skipped ticket means the git archive is now incomplete. Say so in the
    # exit code, or a cron job reports success while the rollback copy rots.
    return 1 if skipped else 0


def cmd_log(args) -> int:
    """Manually append one transition line.

    A repair tool for a line `server.py` failed to write (it prints
    "TRANSITION LOG GAP" when that happens). Agents must NOT use this: they set
    role/model/effort on the POST /ticket body and the server logs them, so
    appending here too would double-log every agent transition.
    """
    import translog

    if not TOKEN:
        print("FNS_TOKEN is not set", file=sys.stderr)
        return 2

    def read(path):
        try:
            return (get_note(path).get("data") or {}).get("content")
        except urllib.error.HTTPError:
            return None

    def write(path, content):
        r = put_note(path, content)
        if r.get("code") != 1:
            msg = f"{r.get('code')} {r.get('message')}"
            raise RuntimeError(msg)

    actor = {"role": args.role, "model": args.model, "effort": args.effort}
    line = translog.append_transition(
        read, write, args.key, args.get_from, args.to, actor
    )
    print(line)
    return 0


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

    default_state = (
        Path.home() / "sources" / "ticketing_system.sdlc-state" / ".sdlc" / "tickets"
    )
    pl = sub.add_parser("pull", help="write vault state back to the sdlc-state worktree")
    pl.add_argument("--into", default=str(default_state))
    pl.add_argument("--page-size", type=int, default=1000)
    pl.add_argument(
        "--prune",
        action="store_true",
        help="delete ticket JSON whose note is no longer in the vault (archived)",
    )
    pl.set_defaults(func=cmd_pull)

    lg = sub.add_parser("log", help="manually append one transition (repair tool)")
    lg.add_argument("key")
    lg.add_argument("get_from", metavar="FROM")
    lg.add_argument("to", metavar="TO")
    lg.add_argument("--role", default="human")
    lg.add_argument("--model", default="")
    lg.add_argument("--effort", default="")
    lg.set_defaults(func=cmd_log)

    s = sub.add_parser("selfcheck", help="round-trip tickets, report lossy fields")
    s.add_argument("--source", default=str(default_src))
    s.set_defaults(func=cmd_selfcheck)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    import urllib.parse  # noqa: F401  (used by get_note)

    sys.exit(main())
