# sdlc-board

Board UI, local server and vault CLI for the SDLC board, backed by a Fast Note Sync (FNS) vault.

Plan: `ticketing_system/.sdlc/vault-migration-plan.md`.

## Protocol facts established in Phase 0

Executed against 10.99.0.31:9000 (FNS v3.6.1) on 2026-08-26. Everything downstream depends on
these; they were measured, not read off the docs.

### Timestamps are MILLISECONDS

Wrote a note with client `mtime=ctime=1787780659429`; the server echoed both unchanged and assigned
`lastTime=1787780659943`.

- `docs/REST_API.md` §Timestamp Format ("all milliseconds") is **correct**.
- `docs/ws_api.md`'s closing note ("seconds except lastTime") is **wrong**.
- The `1700000000` DTO examples (`note_dto.go:31-32`) are **wrong** — illustrative only.

Source agrees: every write path uses `UnixMilli()`. `lastTime` maps to `UpdatedTimestamp` and is
server-assigned; key the sync watermark on it, never on client-supplied `mtime`.

### Content hash: UTF-16, not FNV-1a

`REST_API.md` describes "a 32-bit hash algorithm (e.g. FNV-1a)". The implementation is Java's
`String.hashCode` over **UTF-16 code units** (`pkg/util/hash.go:24-37`). `vault.py:encode_hash32`
is a verified port: for `'seed émoji 😀 tail'` the server computed `82064936` and so does the port.

Iterating Python code points instead breaks only tickets containing non-BMP characters — 24 of the
272 tickets do. A test corpus without an emoji passes a broken port.

### Concurrent appends LOSE DATA — the transition log needs a single writer

50 concurrent `POST /api/note/append` to one note:

| | lines landed | seed line |
|---|---|---|
| 50 concurrent | **1 / 50** | destroyed |
| 50 serial | 50 / 50 | intact |

All 50 writes were *accepted* (`version=50`, no errors); the final content was literally
`line-42\n`. `AppendContent` (`note_service.go:1106-1141`) is a plain read-modify-write in Go with
**no `BaseHash`** and no transaction, so the last writer overwrites the whole note.

Consequence for Phase 4: `server.py` must serialise appends to `_log/transitions.md` behind a lock.
Sound because it is the sole writer. The plan's §10 row "remove the serialising lock, *if Phase 0
showed one is needed*" is now **unconditionally required**.

### Auth

Token scope is three dimensions, `p:` protocol / `c:` client / `f:` function
(`pkg/app/permission.go`). Client restriction is **exact-match**, case-insensitive, wildcard only
via a trailing `*`; ours is `sdlc*`.

**Every REST call and the WS handshake must send an `x-client` header.** Omitting it, or sending a
non-matching value, returns `315 Auth token Scope restricted` — indistinguishable from a genuine
scope problem, and it cost a long misdiagnosis. `/api/health` is the only path exempt from the
scope check.

WS handshake framing is `Authorization|<raw-token>` — the token is **not** JSON-quoted
(`pkg/app/websocket.go:1166`). A malformed token yields `308 Session expired`, which looks exactly
like a revoked token; if WS says 308 while REST works, suspect the client first.

### `contentHash` on a note is not necessarily server-derived

`POST /api/note` stores a client-supplied `contentHash` **verbatim without validating it** (probes
stored `"x"` and `"placeholder"` successfully). Only append/prepend/replace recompute it. Phase 2's
reconciliation compares on `contentHash`, so the mirror should recompute locally rather than trust
a stored value.

## Environment

```sh
export FNS_API=http://10.99.0.31:9000
export FNS_TOKEN=<bearer token>
export FNS_VAULT=sdlc
export FNS_CLIENT=sdlcBoard      # must match the token's client restriction
```

## vault.py

```sh
python3 vault.py selfcheck            # round-trip every ticket in memory, report lossy fields
python3 vault.py migrate --dry-run    # show what would be written
python3 vault.py migrate              # write one note per ticket into the vault
```

### Note format

Frontmatter holds flat scalars and string lists only, so Obsidian's properties panel can edit them:
`key`, `status`, `type`, `parent`, `classification`, `assignee`, `pr`, `labels`. Strings are always
JSON-encoded, which is valid YAML double-quoted style and survives colons, backticks, quotes,
newlines and emoji.

The body holds the title, wikilinks for `links`, the description, and a fenced ` ```json sdlc-data `
block carrying everything frontmatter cannot: `readiness`, `links`, `comments`, `context`, plus
`summary`/`description` and the full `pr` object. That block is what makes the round trip lossless —
the source corpus has 1,915 comment bodies and ~44 distinct ad-hoc `context` keys, and `pr` appears
in five different shapes.

## Status

- **Phase 0 complete.** Facts above.
- **Phase 1 complete.** All 272 tickets migrated; all 272 read back from the vault byte-identical
  to their source render, all 272 server hashes match, zero lossy fields.
- Phase 2 (WebSocket read path) not started. `server.py` is still the git-backed original and is
  **not** wired to the vault yet.
