#!/usr/bin/env python3
"""Read-only Fast Note Sync WebSocket client for the board mirror.

Structurally read-only (plan §Phase 2, trap #6): the only client-to-server
actions implemented are Authorization, ClientInfo and NoteSync. NoteModify,
NoteDelete and NoteRename are deliberately absent, so a mirror bug cannot
corrupt the shared vault -- the code to do so does not exist.

Two related safeguards:
  * NoteSync is sent with an empty `delNotes`. The server DELETES anything
    listed there (ws_note.go:842-870); a mirror must never populate it.
  * NoteSyncNeedPush is logged and ignored (trap #7). The server sends it when
    it believes we hold a newer copy; answering it would mean uploading.

Timestamps are milliseconds (Phase 0, measured). `lastTime` is server-assigned
and is the watermark; `mtime` is client-supplied and must never be used as one.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
from urllib.parse import urlparse

from vault import encode_hash32

# The server-to-client actions that make up a sync page's details.
PAGE_DETAIL_ACTIONS = frozenset(
    {"NoteSyncModify", "NoteSyncDelete", "NoteSyncRename", "NoteSyncMtime"}
)

WS_TEXT = 0x1
WS_BINARY = 0x2
WS_CLOSE = 0x8
WS_PING = 0x9
WS_PONG = 0xA


class WSError(Exception):
    pass


class _Socket:
    """Minimal RFC 6455 client. Text frames only, plus ping/pong."""

    def __init__(self, url: str, headers: dict[str, str], timeout: float = 30.0):
        u = urlparse(url)
        self.host = u.hostname
        self.port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        if u.scheme == "wss":
            msg = "wss is not supported; the board talks to a LAN server over ws"
            raise WSError(msg)

        self.sock = socket.create_connection((self.host, self.port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {self.host}:{self.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{k}: {v}" for k, v in headers.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                msg = "connection closed during handshake"
                raise WSError(msg)
            buf += chunk
        status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            raise WSError(f"handshake failed: {status}")
        self._rest = buf.split(b"\r\n\r\n", 1)[1]
        self._lock = threading.Lock()

    # -- framing -------------------------------------------------------

    def _recv_exact(self, n: int) -> bytes:
        out = self._rest[:n]
        self._rest = self._rest[n:]
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                msg = "connection closed"
                raise WSError(msg)
            out += chunk
        return out

    def send_text(self, payload: str) -> None:
        body = payload.encode()
        mask = os.urandom(4)
        header = bytearray([0x80 | WS_TEXT])
        n = len(body)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
        with self._lock:
            self.sock.sendall(bytes(header) + masked)

    def recv_message(self) -> tuple[int, bytes] | None:
        """Returns (opcode, payload), reassembling continuation frames."""
        opcode = None
        data = b""
        while True:
            h = self._recv_exact(2)
            fin = h[0] & 0x80
            op = h[0] & 0x0F
            length = h[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length) if length else b""
            if op in (WS_PING, WS_PONG, WS_CLOSE):
                # RFC 6455 allows control frames BETWEEN fragments. Returning
                # here mid-message would discard everything accumulated so far
                # and truncate the note -- and a 142KB ticket is exactly the
                # payload that fragments. Handle the control frame in place and
                # keep reassembling.
                if op == WS_PING:
                    self.send_pong(payload)
                    continue
                if op == WS_PONG:
                    continue
                if data or opcode is not None:
                    msg = "connection closed mid-message"
                    raise WSError(msg)
                return op, payload
            if op != 0:
                opcode = op
            data += payload
            if fin:
                return opcode or WS_TEXT, data

    def send_pong(self, payload: bytes) -> None:
        mask = os.urandom(4)
        header = bytearray([0x80 | WS_PONG, 0x80 | len(payload)])
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._lock:
            self.sock.sendall(bytes(header) + masked)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class VaultMirror:
    """Keeps an in-memory + on-disk mirror of one or more vaults.

    The mirror is the board's read source. It survives restarts on disk, so a
    board started with the server unreachable still renders yesterday's state.
    """

    def __init__(self, mirror_dir, vaults):
        self.dir = mirror_dir
        self.vaults = list(vaults)
        self.notes: dict[str, dict[str, str]] = {v: {} for v in self.vaults}
        # Per-note server timestamps. The reconciliation skip condition requires
        # BOTH contentHash AND mtime to match (ws_note.go:1020); reporting mtime=0
        # makes every note fall through to a resend or NoteSyncNeedPush on every
        # reconnect, so the mirror must remember what the server told it.
        self.meta: dict[str, dict[str, dict]] = {v: {} for v in self.vaults}
        # Watermarks are PER VAULT (trap #4). One shared lastTime leaves one
        # project permanently behind while another looks fine.
        self.last_time: dict[str, int] = {v: 0 for v in self.vaults}
        self.lock = threading.RLock()
        self.connected = False
        self.last_sync_at = 0.0
        self._load()

    # -- disk ----------------------------------------------------------

    def _vault_dir(self, vault: str):
        return self.dir / vault

    def _safe_target(self, vault: str, path: str):
        """Resolve a note path inside the vault dir, or refuse it.

        Paths come from the server. A path containing `../` otherwise writes or
        deletes files outside the mirror -- verified: it created a file in /tmp.
        """
        base = self._vault_dir(vault).resolve()
        target = (base / path).resolve()
        if target != base and base not in target.parents:
            msg = f"note path escapes the mirror: {path!r}"
            raise ValueError(msg)
        return target

    def _meta_path(self, vault: str):
        return self._vault_dir(vault) / "_mirror.json"

    def _load(self) -> None:
        for v in self.vaults:
            vd = self._vault_dir(v)
            if not vd.exists():
                continue
            meta_path = self._meta_path(v)
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    self.last_time[v] = int(meta.get("lastTime", 0))
                    notes_meta = meta.get("notes")
                    self.meta[v] = notes_meta if isinstance(notes_meta, dict) else {}
                    # Age of the data must survive a restart, or an offline
                    # board cannot say how stale it is.
                    self.last_sync_at = max(
                        self.last_sync_at, float(meta.get("syncedAt") or 0)
                    )
                except (ValueError, OSError):
                    self.last_time[v] = 0
                    self.meta[v] = {}
            for p in vd.rglob("*.md"):
                rel = p.relative_to(vd).as_posix()
                try:
                    self.notes[v][rel] = p.read_text(encoding="utf-8")
                except OSError:
                    continue

    def _persist_note(self, vault: str, path: str, content: str) -> None:
        target = self._safe_target(vault, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(target)

    def _remove_note(self, vault: str, path: str) -> None:
        target = self._safe_target(vault, path)
        try:
            target.unlink()
        except FileNotFoundError:
            pass

    def _persist_meta(self, vault: str) -> None:
        vd = self._vault_dir(vault)
        vd.mkdir(parents=True, exist_ok=True)
        self._meta_path(vault).write_text(
            json.dumps(
                {
                    "lastTime": self.last_time[vault],
                    "notes": self.meta.get(vault, {}),
                    "syncedAt": self.last_sync_at,
                }
            ),
            encoding="utf-8",
        )

    # -- mutations (local only; never sent upstream) --------------------

    def _ensure_vault(self, vault: str) -> None:
        """Accept a vault we did not know about at startup.

        VAULTS is discovered once at import, but the shared socket carries
        broadcasts for EVERY vault the user can reach -- including one added or
        newly authorised after the process started. Without this the first such
        broadcast raises KeyError inside the sole mirror thread, which dies
        while `connected` stays true and every board silently freezes.
        """
        if vault not in self.notes:
            self.notes[vault] = {}
            self.meta[vault] = {}
            self.last_time.setdefault(vault, 0)
            if vault not in self.vaults:
                self.vaults.append(vault)
            print(f"[fns] mirroring newly seen vault: {vault}")

    def apply_modify(self, vault: str, path: str, content: str,
                     mtime: int = 0, ctime: int = 0) -> None:
        with self.lock:
            self._ensure_vault(vault)
            self.notes.setdefault(vault, {})[path] = content
            if mtime or ctime:
                self.meta.setdefault(vault, {})[path] = {
                    "mtime": mtime,
                    "ctime": ctime,
                }
            self._persist_note(vault, path, content)
            self._persist_meta(vault)

    def apply_delete(self, vault: str, path: str) -> None:
        with self.lock:
            self._ensure_vault(vault)
            self.notes.setdefault(vault, {}).pop(path, None)
            self.meta.setdefault(vault, {}).pop(path, None)
            self._remove_note(vault, path)

    def apply_rename(self, vault: str, old: str, new: str) -> None:
        with self.lock:
            self._ensure_vault(vault)
            # Validate BOTH paths before touching anything. Mutating first and
            # validating inside _persist_note loses the note entirely when the
            # destination is rejected: the source is already gone from memory,
            # _load only re-reads at construction, and a retry then deletes the
            # surviving file from disk too.
            self._safe_target(vault, old)
            self._safe_target(vault, new)

            notes = self.notes.setdefault(vault, {})
            content = notes.pop(old, None)
            meta = self.meta.setdefault(vault, {})
            note_meta = meta.pop(old, None)
            # Write the destination BEFORE removing the source, so a crash
            # between the two leaves a duplicate (self-healing on next sync)
            # rather than nothing at all.
            if content is not None:
                notes[new] = content
                if note_meta is not None:
                    meta[new] = note_meta
                self._persist_note(vault, new, content)
            self._remove_note(vault, old)

    def set_watermark(self, vault: str, last_time: int) -> None:
        with self.lock:
            self._ensure_vault(vault)
            if last_time > self.last_time.get(vault, 0):
                self.last_time[vault] = last_time
                self._persist_meta(vault)

    def inventory(self, vault: str) -> list[dict]:
        """The client's own note list, for the server to compute a real delta.

        contentHash is recomputed locally rather than carried from the server:
        POST /api/note stores a client-supplied hash verbatim without
        validating it (Phase 0), so a stored hash is not trustworthy.
        """
        with self.lock:
            out = []
            meta = self.meta.get(vault, {})
            for path, content in self.notes.get(vault, {}).items():
                m = meta.get(path) or {}
                out.append(
                    {
                        "path": path,
                        "pathHash": encode_hash32(path),
                        "contentHash": encode_hash32(content),
                        "mtime": int(m.get("mtime") or 0),
                        "ctime": int(m.get("ctime") or 0),
                    }
                )
            return out

    def snapshot(self, vault: str) -> dict[str, str]:
        with self.lock:
            return dict(self.notes.get(vault, {}))

    def age_seconds(self) -> float:
        return time.time() - self.last_sync_at if self.last_sync_at else float("inf")


class FNSReadClient(threading.Thread):
    """Connects, authenticates, reconciles per vault, applies broadcasts.

    Reconnect is the repair mechanism, not an error path (trap #4): a dropped
    connection means missed broadcasts, and re-running NoteSync on reconnect is
    the only thing that repairs it.
    """

    daemon = True

    def __init__(self, api, token, vaults, mirror, client_type="sdlcBoard",
                 client_name="sdlc-board", on_change=None):
        super().__init__(name="fns-read-client")
        self.api = api.rstrip("/")
        self.token = token
        self.mirror = mirror
        for v in vaults:
            mirror._ensure_vault(v)
        self.client_type = client_type
        self.client_name = client_name
        self.on_change = on_change
        self.stop_flag = threading.Event()
        self._ws = None
        self._syncing = False  # True while a bulk NoteSync is in flight

    @property
    def vaults(self):
        """Whatever the mirror currently knows about -- NOT a list frozen at
        construction. A vault first seen in a broadcast after startup must also
        be reconciled on the next reconnect, or it silently never catches up.
        """
        with self.mirror.lock:
            return list(self.mirror.vaults)

    # -- protocol helpers ----------------------------------------------

    def _ws_url(self) -> str:
        u = urlparse(self.api)
        return f"ws://{u.hostname}:{u.port or 80}/api/user/sync"

    def _send(self, action: str, payload) -> None:
        # Authorization carries the RAW token, not JSON (websocket.go:1166).
        body = payload if isinstance(payload, str) else json.dumps(payload)
        self._ws.send_text(f"{action}|{body}")

    def _expect(self, timeout=30.0):
        self._ws.sock.settimeout(timeout)
        op, raw = self._ws.recv_message()
        if op == WS_PING:
            self._ws.send_pong(raw)
            return self._expect(timeout)
        if op == WS_CLOSE:
            msg = "server closed the connection"
            raise WSError(msg)
        text = raw.decode("utf-8", errors="replace")
        action, _, payload = text.partition("|")
        try:
            env = json.loads(payload) if payload else {}
        except ValueError:
            env = {}
        return action, env

    # -- handshake ------------------------------------------------------

    def _connect(self) -> None:
        self._ws = _Socket(
            self._ws_url(),
            {
                "x-client": self.client_type,
                "x-client-name": self.client_name,
                "x-client-version": "1.0.0",
            },
        )
        self._send("Authorization", self.token)
        action, env = self._expect()
        if env.get("code") != 1:
            raise WSError(f"auth failed: {env.get('code')} {env.get('message')}")

        self._send(
            "ClientInfo",
            {
                "name": self.client_name,
                "version": "1.0.0",
                "type": self.client_type,
                "offlineSyncStrategy": "newTimeMerge",
            },
        )
        self._expect()

    def _sync_vault(self, vault: str) -> None:
        """One NoteSync per vault, with that vault's inventory and watermark.

        Broadcasts arrive for every vault on one connection, but reconciliation
        does not: NoteSync names a single vault and lastTime is per vault.

        The bulk download is PULL-based, not push. The server queues the pages
        and waits ("默认不自动发送，等待客户端拉取", ws_note.go:1209): nothing
        arrives until the client sends NoteSyncPageAck. Page indexes on the
        wire are 1-based while the ack is 0-based, and the client never acks
        the last page (ws_sync_download_cache.go:120-124, 140-156).
        """
        context = f"sync-{vault}-{int(time.time() * 1000)}"
        self._syncing = True
        try:
            self._sync_vault_inner(vault, context)
        finally:
            self._syncing = False

    def _sync_vault_inner(self, vault: str, context: str) -> None:
        self._send(
            "NoteSync",
            {
                "vault": vault,
                "context": context,
                "lastTime": self.mirror.last_time.get(vault, 0),
                "notes": self.mirror.inventory(vault),
                "delNotes": [],  # never populate: the server DELETES these
                "missingNotes": [],
            },
        )
        pending_watermark = None
        completed = False
        draining_last = False
        last_page_expected = 0
        last_page_seen = 0
        while not self.stop_flag.is_set():
            action, env = self._expect()
            data = env.get("data") or {}

            if action == "NoteSyncEnd":
                for queued in data.get("messages") or []:
                    self._dispatch(
                        queued.get("action", ""),
                        {"data": queued.get("data"), "vault": vault},
                    )
                pending_watermark = data.get("lastTime") or pending_watermark
                if not (data.get("needModifyCount") or data.get("needDeleteCount")):
                    completed = True  # nothing to pull: the sync IS complete
                    break
                # Pages are queued but unsent; -1 starts the pull.
                self._send(
                    "NoteSyncPageAck",
                    {"context": context, "vault": vault, "pageIndex": -1},
                )
                continue

            if action == "NoteSyncPage":
                # The page metadata carries TotalCount -- how many detail
                # messages follow -- and arrives BEFORE them
                # (ws_sync_download_cache.go:228-239). Counting them is
                # deterministic; waiting for silence is not.
                expected = int(data.get("totalCount") or 0)
                if data.get("isLast"):
                    last_page_expected = expected
                    last_page_seen = 0
                    draining_last = True
                    if expected == 0:
                        completed = True
                        break
                    continue
                self._send(
                    "NoteSyncPageAck",
                    {
                        "context": context,
                        "vault": vault,
                        "pageIndex": data.get("pageIndex", 0),
                    },
                )
                continue

            self._dispatch(action, env, default_vault=vault)
            if draining_last and action in PAGE_DETAIL_ACTIONS:
                # Count ONLY the actions a page is made of. Counting every
                # dispatched frame lets an unrelated live broadcast -- which
                # arrives on the same socket, for any vault -- inflate the tally
                # and end the sync early, advancing the watermark past a note
                # that was never applied.
                last_page_seen += 1
                if last_page_seen >= last_page_expected:
                    completed = True
                    break

        # ONLY advance the watermark on a sync we know finished. NoteSync
        # returns changes strictly after lastTime, so advancing it past notes
        # that were never applied skips them PERMANENTLY -- the mirror goes
        # silently and unrecoverably stale. Leaving it behind costs a re-sync of
        # notes we already hold, which is cheap and self-correcting.
        if pending_watermark and completed:
            self.mirror.set_watermark(vault, int(pending_watermark))
        elif pending_watermark:
            print(
                f"[fns] {vault}: sync did not complete; keeping watermark at "
                f"{self.mirror.last_time.get(vault, 0)} so the next NoteSync re-fetches"
            )

    # -- message handling -----------------------------------------------

    def _dispatch(self, action: str, env: dict, default_vault: str | None = None) -> None:
        data = env.get("data") or {}
        vault = env.get("vault") or default_vault
        if not vault:
            return

        if action == "NoteSyncModify":
            path = data.get("path")
            if path is not None and data.get("content") is not None:
                self.mirror.apply_modify(
                    vault,
                    path,
                    data["content"],
                    mtime=int(data.get("mtime") or 0),
                    ctime=int(data.get("ctime") or 0),
                )
                self._changed()
        elif action == "NoteSyncDelete":
            path = data.get("path")
            if path:
                self.mirror.apply_delete(vault, path)
                self._changed()
        elif action == "NoteSyncRename":
            old = data.get("oldPath") or data.get("old_path")
            new = data.get("path")
            if old and new:
                self.mirror.apply_rename(vault, old, new)
                self._changed()
        elif action == "NoteSyncMtime":
            pass  # timestamp only, no content change
        elif action == "NoteSyncNeedPush":
            # Trap #7: the server thinks we hold a newer copy. We are read-only;
            # log and ignore. Implementing the upload it asks for would make the
            # mirror a writer.
            print(f"[fns] NoteSyncNeedPush ignored (read-only): {data.get('path')}")

        # Advance the watermark per message ONLY for live broadcasts, where
        # each message stands alone. During a bulk NoteSync the messages arrive
        # in pages, so a drop partway would leave the watermark past notes that
        # were never applied -- and NoteSync only returns changes AFTER
        # lastTime, so those notes are skipped permanently. _sync_vault sets the
        # watermark once, at the end, and only on a complete sync.
        if data.get("lastTime") and not self._syncing:
            self.mirror.set_watermark(vault, int(data["lastTime"]))

    def _changed(self) -> None:
        self.mirror.last_sync_at = time.time()
        if self.on_change:
            self.on_change()

    # -- run loop --------------------------------------------------------

    def run(self) -> None:
        backoff = 1.0
        while not self.stop_flag.is_set():
            try:
                self._connect()
                for v in self.vaults:
                    self._sync_vault(v)
                self.mirror.connected = True
                self.mirror.last_sync_at = time.time()
                backoff = 1.0
                print(f"[fns] connected; mirroring {', '.join(self.vaults)}")
                while not self.stop_flag.is_set():
                    action, env = self._expect(timeout=90.0)
                    self._dispatch(action, env)
            except (WSError, OSError, ValueError) as e:
                self.mirror.connected = False
                if self.stop_flag.is_set():
                    break
                print(f"[fns] disconnected ({e}); retrying in {backoff:.0f}s")
                self.stop_flag.wait(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if self._ws:
                    self._ws.close()
                    self._ws = None
        self.mirror.connected = False

    def stop(self) -> None:
        self.stop_flag.set()
        if self._ws:
            self._ws.close()
