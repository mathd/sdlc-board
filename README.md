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

### `baseHash` is declared but NOT enforced on REST

The plan's Phase 3 rests on `POST /api/note` honouring `baseHash` as a concurrency token. It does
not. `NoteModifyOrCreateRequest` declares the field (`note_dto.go:27`) and the REST handler never
reads it: it passes `params` straight to `ModifyOrCreate`, which has no `BaseHash` logic at all.
Grepping the repo, `BaseHash` is read **only** in `ws_note.go`.

Executed against the live server: a write carrying `baseHash: "definitely-not-the-current-hash"`
returned `code: 1` and destroyed the note's content. **REST is last-writer-wins.**

The one server-enforced compare-and-swap is on the WebSocket path, and only under
`offlineSyncStrategy: manualMerge` — it returns **530** (not the 441 the plan expects; 441 is
"Destination note already exists", a rename collision) and refuses the write. Verified: content
stayed `ORIGINAL`. Taking it would mean giving the mirror write access, which is exactly what
Phase 2 removed on purpose, so the board does the CAS server-side instead.

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

## Running the board

```sh
export FNS_API=... FNS_TOKEN=... FNS_VAULT=sdlc FNS_CLIENT=sdlcBoard
python3 server.py 8787          # http://localhost:8787/board.html
```

`SDLC_MIRROR` sets the on-disk mirror location (default `~/.cache/sdlc-board`).

`server.py` binds 127.0.0.1 and is unauthenticated **because** of that. The assumption is
load-bearing: do not change the bind address without adding authentication.

### The bulk download is PULL-based

The single most surprising part of the protocol. `NoteSync` does not push the notes: the server
queues the pages and waits ("默认不自动发送，等待客户端拉取", `ws_note.go:1209`). Nothing arrives
until the client sends `NoteSyncPageAck`, and that ack **requires a `vault` field** even though the
context already identifies the sync. A client that only sends `NoteSync` connects, authenticates,
receives `NoteSyncEnd` with `needModifyCount: 272`, and then waits forever having received nothing.

Page indexes on the wire are 1-based while the ack is 0-based, and the client never acks the last
page (`ws_sync_download_cache.go:120-124, 140-156`).

### The mirror must remember the server's mtime

The reconciliation skip condition requires **both** `contentHash` and `mtime` to match
(`ws_note.go:1020`). An inventory reporting `mtime: 0` matches nothing, so every note falls through
to a resend or a `NoteSyncNeedPush` on every single reconnect — 270 of them here before the fix,
zero after. The mirror persists the server's `mtime`/`ctime` per note alongside the content.

## Status

- **Phase 0 complete.** Facts above.
- **Phase 1 complete.** All 272 tickets migrated; all 272 read back from the vault byte-identical
  to their source render, all 272 server hashes match, zero lossy fields.
- **Phase 2 complete.** `server.py` has no git code at all; `/board` renders from the mirror.
  Verified: a vault edit reaches the board in **~1s**; killing the socket and writing while
  disconnected **converges in ~2s with no restart**; starting with FNS unreachable renders 272
  tickets read-only from disk. Mutation-checked — replacing `_sync_vault` with a no-op leaves the
  gap unrepaired.
  - The board is **read-only** for the whole of Phase 2: `POST /ticket` and `POST /archive` return
    503, and `/history` and `/metrics` return empty with an `unavailable` note rather than serving
    stale git-derived data.
  - The WS client is **structurally** read-only (trap #6): the only actions it can send are
    `Authorization`, `ClientInfo`, `NoteSync` and `NoteSyncPageAck`. `NoteModify`/`NoteDelete`/
    `NoteRename` do not exist in the file, and `delNotes` is a hardcoded `[]` because the server
    deletes whatever is listed there (`ws_note.go:842-870`).
  - `NoteSyncNeedPush` is logged and ignored (trap #7).
- **Phase 3 complete.** `POST /ticket` is a server-side compare-and-swap on the status field.
  Verified: a drag propagates to the vault and back to the board; a body edit in Obsidian followed
  by a move succeeds **and keeps the edit**; two racing drags from one status produce exactly one
  winner and one 409 naming both values. Mutation-checked — removing the guard makes both racing
  drags return 200 and silently lose one.
  - **Browser-verified**, per AGENTS.md: a real Chrome drag of TKT-209 Ready→Planning fired the
    board's own handlers, and re-reading the vault confirmed `status: "Planning"` landed. The
    board's DoR and workflow gates refused two earlier drags, which is them working.
  - `_rev` is no longer a git blob hash: it is the status the card is moving **from**.
  - Only `status`, `labels`, `assignee`, `pr`, `classification`, `parent` and `type` are writable
    from the board. A drag sends the whole ticket, and merging all of it over the vault's note
    silently destroyed a description edited in Obsidian during testing — the client cannot
    influence a field outside that set rather than being trusted not to send one.
- **Note format changed in Phase 3 (vault re-migrated).** The rendered body is now the source of
  truth for `summary` and `description`, and a `<!-- /description -->` marker bounds the
  description. Before this, the body was *derived* from the data block and re-rendered from it, so
  anything a human typed in Obsidian outside that block was erased on the next board write — a
  lost update the CAS cannot see, because the guarded field never changed. Descriptions in the
  corpus both do and do not end in a newline, so the boundary has to be explicit rather than
  inferred; removing the marker costs 40 descriptions on `selfcheck`.
- **Code review fixes (Codex, cross-model).** Eight findings validated by execution and fixed; two
  rejected. Notable:
  - **`_rev` was optional, so the CAS was bypassable.** A `POST /ticket` omitting it moved a ticket
    Backlog→Done straight through the guard. `_rev` is now required and a write without it is
    refused with 400. This is the "a precondition that cannot fail" shape: the guard was present,
    correct, and skippable.
  - **The watermark advanced on an incomplete sync.** Worse than reported: `_dispatch` advanced it
    per *message*, so an interrupted bulk sync ratcheted it past notes never applied — and since
    `NoteSync` only returns changes after `lastTime`, those notes were skipped **permanently**. Now
    advanced once, at the end, only on a complete sync; per-message advance is limited to live
    broadcasts, where each message stands alone. Verified: an interrupted sync holds the watermark
    at its old value.
  - **A note whose frontmatter key mismatched its path** was read and rewritten while reporting
    success for the requested key — a status change applied to the wrong ticket. Now refused.
  - Path traversal (a server-supplied `../` path wrote outside the mirror — executed, it landed in
    `/tmp`), control frames truncating fragmented messages, rename losing the note on a crash,
    a malformed `_mirror.json` breaking `inventory()`, and a description containing the literal
    `<!-- /description -->` being truncated (now escaped; 0 real collisions in the corpus today).
  - **Rejected:** the claim that `urllib.parse` is unbound in `vault.get_note` (`import
    urllib.request` binds it — verified by reaching the network layer), and the cross-process race
    on the CAS, which is the accepted residual of the server-side approach FNS forces (see
    `baseHash` above), not a defect.
- **Second review pass (fixes' interaction).** Three findings, all validated by execution:
  - **Two correct fixes composed into a new defect.** Path validation (fix 5) and the reordered
    rename (fix 6) are each right alone: `apply_rename` mutated the in-memory mirror *before*
    `_persist_note` validated the destination, so a rejected path made the note vanish from memory
    — and since `_load` only re-reads at construction, a retry then deleted the surviving file from
    disk. Executed both steps. Both paths are now validated before anything is touched, and the
    per-note `mtime` metadata moves with the rename (it was being orphaned, which would have
    re-created the `NeedPush` storm for renamed notes).
  - **Sync completion was inferred from silence.** The first fix treated a 3s socket timeout as
    proof the last page had fully arrived. It is not. The page metadata carries `totalCount` and
    arrives *before* its details (`ws_sync_download_cache.go:228-239`), so the client now **counts**
    them. Deterministic, and it also cut a complete sync from ~3s to ~1s. Verified: interrupting at
    4 different points (up to 194/272 notes) holds the watermark; mutation-checked by removing the
    completion gate, which advances it and skips 225 notes permanently.
  - **The description escaping was not injective.** Escaping only the marker maps two distinct
    inputs onto one rendering, so a description already containing `<!-- \/description -->` came
    back altered. Now escapes the escape character first and unescapes in mirror-image order;
    14 adversarial cases (nested, repeated, sentinel-only) round-trip exactly.
- **Phase 4 complete.** `_log/transitions.md` is the timing source; `/history`, `/metrics` and each
  card's `_since` are derived from it, and no git shape survives.
  - **`server.py` is the only writer, and appends are serialised behind a lock.** This is not
    optional: Phase 0 measured 50 concurrent appends keeping **1 line** (and destroying the
    pre-existing content) against 50/50 serial. Mutation-checked in isolation — replacing the lock
    with a null context drops 50 appends to 4.
  - **The append happens only after the ticket write is confirmed.** The two have no transaction
    between them. Appending first would record transitions that never happened and corrupt
    `/metrics` with no way to tell; appending second loses a line when the append fails, leaving
    `/metrics` incomplete but never wrong. Verified: a failing append still returns 200 for the
    write, reports `logged: false`, and prints `TRANSITION LOG GAP` for repair. Neither a no-op
    move nor a *refused* one is logged.
  - `vault.py log KEY FROM TO` is the manual repair tool for such a gap. Agents must not use it:
    they set `role`/`model`/`effort` on the `POST /ticket` body and the server logs them, so
    appending separately would double-log every agent transition.
  - Metrics were verified against values **derived from the requirement** (a status is held from
    the transition into it until the next one), not read off a run — an assertion written by
    observing what the code did pins the behaviour, not the rule. §10's mutation (remove one log
    line) changes the output.
  - The log is read from the **mirror**, not the vault, so `/metrics` costs no round trip and
    survives the vault being unreachable.
- **Phase 5 partially complete — the switcher is built, the two-vault criterion is NOT verified.**
  - Columns, project name, transversal statuses and WIP limits now render from the selected
    vault's `_board/config.md`, not from `config.json`. Proven by mutation: rewriting the vault
    config to `["Icebox", "Doing", "Shipped"]` changed the rendered columns, and a `schema: 99`
    config makes the board **refuse to render** and serve zero tickets rather than guess.
  - `schema` gating follows ADR-017's rule that the poison line has a bottom end: `schema <= 0`,
    a string, or a boolean is a broken envelope and renders as the current schema; only a
    genuinely higher number refuses.
  - `/vaults`, `/board`, `/history`, `/metrics` and `POST /ticket` are all vault-scoped via
    `?vault=`; an unknown vault falls back to the default rather than erroring. One WebSocket
    connection mirrors every vault, so switching is a filter over data already arriving.
  - The switcher hides itself when only one vault exists. Browser-verified: with a stubbed
    two-vault payload it shows both options, follows the selection, retitles the page per project
    and persists the choice to `localStorage`; a real browser write through the vault-scoped path
    reached the vault and was recorded in the transition log.
  - **NOT verified, and this is the phase's own "Done when":** *two vaults with deliberately
    different status lists both render correctly*, and *switching between them needs no reconnect*.
    The token's allowlist covers only `sdlc` (`Vault access restricted: sdlc-test2`), so the test
    cannot be run and the multi-vault path has never executed against a second real vault. The UI
    mechanism was exercised with a stub, which is not the same claim. **Widen the token allowlist
    and run it before relying on Phase 5.**
  - The `sdlc-ticket` skill is deliberately **not** repointed yet: its `references/local-tracker.md`
    documents the git board, which §8 requires to keep working until cutover. Repoint it there.
- **Not verified:** the two-vault case (§10 "each vault catches up after a reconnect"). Watermarks
  are per vault and reload independently, but the token's allowlist covers only `sdlc`, so the
  convergence test itself has not been run. Do it before relying on Phase 5.
- Phase 3 (REST write path) not started.
