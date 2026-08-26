#!/usr/bin/env python3
"""Per-vault board configuration, read from `_board/config.md`.

The board renders its columns from this file and the project's `sdlc-ticket`
skill reads the same one, so the skill's rules and the board's columns cannot
drift.

Unlike a ticket's frontmatter this MAY nest (`wip` is a mapping): only the board
and the skill parse it, and nobody edits it through Obsidian's properties panel.
That is what lets tickets stay flat while this does not.
"""

from __future__ import annotations

import json

CONFIG_PATH = "_board/config.md"

# The board understands this version and the one before it. A higher number
# means a config written by a newer board, whose meaning we do not know.
SCHEMA = 1
# The poison/skew line has a BOTTOM end (ADR-017 §5b'): `schema <= 0` is a
# broken envelope, not the future. Treat it as the current schema rather than
# refusing the vault over it.
SCHEMA_MIN = 1

DEFAULTS = {
    "schema": SCHEMA,
    "project": "",
    "statuses": ["Backlog", "Ready", "Planning", "Building", "PO Review", "Done"],
    "transversal": ["BLOCKED"],
    "wip": {},
}


class UnsupportedSchema(Exception):
    def __init__(self, found, supported):
        super().__init__(
            f"board config schema {found} is newer than this board supports "
            f"(max {supported}) — update the board rather than guessing"
        )
        self.found = found
        self.supported = supported


def _scalar(raw: str):
    raw = raw.strip()
    if not raw:
        return None
    # JSON covers every shape this file uses: quoted strings, numbers, lists
    # and mappings. Falling back to the raw text keeps an unquoted word working.
    try:
        return json.loads(raw)
    except ValueError:
        if raw in ("true", "false"):
            return raw == "true"
        if raw in ("null", "~"):
            return None
        return raw


def parse(text: str) -> dict:
    """Parse `_board/config.md` into a config dict, filling defaults.

    Raises UnsupportedSchema if the file was written by a newer board.
    """
    cfg = dict(DEFAULTS)
    cfg["wip"] = {}
    if not text or not text.startswith("---\n"):
        return cfg
    try:
        end = text.index("\n---", 4)
    except ValueError:
        return cfg

    # Both inline JSON (`statuses: ["A","B"]`) and YAML block style are
    # accepted. Skipping indented lines instead -- as this parser first did --
    # makes a block-style list silently fall back to DEFAULTS, so a vault
    # renders the WRONG workflow with no error at all.
    lines = text[4:end].split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1] in (" ", "\t", "-"):
            continue  # a stray child line with no parent; the block reader eats its own
        key, sep, raw = line.partition(":")
        if not sep:
            continue
        key = key.strip()

        if raw.strip():
            value = _scalar(raw)
            if value is not None:
                cfg[key] = value
            continue

        # Empty value: read the indented block that follows.
        block, seq, mapping = [], [], {}
        while i < len(lines):
            nxt = lines[i]
            if not nxt.strip():
                i += 1
                continue
            if nxt[:1] not in (" ", "\t"):
                break
            i += 1
            item = nxt.strip()
            if item.startswith("- "):
                seq.append(_scalar(item[2:]))
            elif ":" in item:
                k2, _, v2 = item.partition(":")
                mapping[k2.strip()] = _scalar(v2)
            block.append(item)
        if seq:
            cfg[key] = seq
        elif mapping:
            cfg[key] = mapping

    found = cfg.get("schema", SCHEMA)
    if not isinstance(found, int) or isinstance(found, bool) or found < SCHEMA_MIN:
        # A broken envelope, not the future. Treat it as the current schema
        # rather than refusing the whole vault.
        cfg["schema"] = SCHEMA
    elif found > SCHEMA:
        raise UnsupportedSchema(found, SCHEMA)

    if not isinstance(cfg.get("statuses"), list) or not cfg["statuses"]:
        cfg["statuses"] = list(DEFAULTS["statuses"])
    if not isinstance(cfg.get("transversal"), list):
        cfg["transversal"] = list(DEFAULTS["transversal"])
    if not isinstance(cfg.get("wip"), dict):
        cfg["wip"] = {}
    return cfg
