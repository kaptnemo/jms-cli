"""HTTP file-transfer backend over KoKo's ``/koko/ws/sftp/`` WebSocket.

JumpServer's web SFTP editor talks to KoKo through a JSON-message WebSocket
protocol (NOT a raw SFTP-over-WebSocket binary stream). Every frame is a JSON
text message of the form::

    {"id": "...", "cmd": "...", "data": "<json string>", "raw": "<base64>"}

Binary payloads ride in the base64 ``raw`` field (Go's ``encoding/json``
marshals ``[]byte`` as base64). The endpoint exposes elFinder-style commands
(``list`` / ``download`` / ``upload`` / ``rm`` / ``rename`` / ``mkdir``) plus a
resumable, checksummed chunk protocol built for large files:

- ``transfer_prepare``  — create the staging file, resolve conflicts
- ``transfer_write``    — append a chunk at ``offset``, SHA256-verified
- ``transfer_read``     — read ``length`` bytes at ``offset``, SHA256 + EOF
- ``transfer_commit``   — verify the full-file SHA256 and rename into place
- ``transfer_status`` / ``transfer_cancel`` — resumable transfer control

Each ``transfer_write`` chunk is verified against its SHA256 on arrival and
the final ``transfer_commit`` re-hashes the whole staged file, so integrity is
enforced server-side (no extra SSH-exec ``md5sum`` pass is needed). The
protocol requires chunks of a single file to be written *sequentially*
(``offset <= committed_bytes``), so this backend uploads each file serially
while still multiplexing distinct files across worker threads.

The client mirrors the ``SFTPClient`` surface used by ``list_remote_files`` /
``resolve_remote_dst`` (``ls`` / ``stat`` / ``close``), so the existing
transfer orchestration reuses those helpers unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import posixpath
import time
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import websocket

from jms.core.auth import JMSSession
from jms.core.resources import AssetInfo
from jms.exceptions import TransferError
from jms.log import logger
from jms.transport import create_connection_token

# WebSocket endpoint used by KoKo's web SFTP editor.
WS_SFTP_PATH: str = "/koko/ws/sftp/"

# WebSocket connect timeout (seconds).
WS_CONNECT_TIMEOUT: int = 15

# Default per-operation response timeout (seconds).
OP_TIMEOUT: int = 120

# transfer_commit re-hashes the whole staged file server-side; allow a long
# window for multi-GB files over slow links.
COMMIT_TIMEOUT: int = 3600

# Chunk size (bytes). Matches KoKo's ``transferChunkMaxSize`` (2MB) for reads;
# used for both read and write chunks so a message never grows unwieldy.
CHUNK_SIZE: int = 2 * 1024 * 1024


class WSFileClient:
    """File access to one asset over KoKo's ``/koko/ws/sftp/`` WebSocket.

    One client owns one WebSocket connection (and thus one connection token).
    It is not thread-safe: a worker thread must hold its own instance.

    Args:
        ws: Connected WebSocket (from ``connect_ws_sftp``).
        ws_id: Session UUID from the server's CONNECT message.
    """

    def __init__(self, ws: websocket.WebSocket, ws_id: str) -> None:
        self._ws: websocket.WebSocket = ws
        self._ws_id: str = ws_id
        self._seq: int = 0
        self._closed: bool = False

    # ──── low-level request/response ─────────────────────────────

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _send(self, cmd: str, fields: dict, raw: Optional[bytes]) -> str:
        """Send one command; return the request id used to correlate the reply."""
        seq = str(self._next_seq())
        payload: dict = {"id": seq, "cmd": cmd, "data": json.dumps(fields)}
        if raw is not None:
            payload["raw"] = base64.b64encode(raw).decode("ascii")
        self._ws.send(json.dumps(payload))
        return seq

    def _recv_for(self, seq: str, timeout: float) -> dict:
        """Read frames until the response matching ``seq`` arrives.

        Server-initiated PING frames are answered with an application-level
        PONG (the Nginx WS tunnel drops protocol-level pings); PONG / CONNECT
        frames are skipped.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransferError("timed out waiting for WebSocket SFTP response")
            self._ws.settimeout(remaining)
            try:
                opcode, data = self._ws.recv_data()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception as e:
                raise TransferError(f"WebSocket SFTP recv failed: {e}") from e
            if opcode != 1:
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            msg_type = msg.get("type")
            if msg_type == "PING":
                self._send_pong()
                continue
            if msg_type in ("PONG", "CONNECT"):
                continue
            if msg.get("id") == seq:
                return msg

    def _request(
        self,
        cmd: str,
        fields: Optional[dict] = None,
        raw: Optional[bytes] = None,
        timeout: float = OP_TIMEOUT,
    ) -> dict:
        """Send a command and return its normalized response.

        The returned dict normalizes ``data`` (JSON-decoded when possible)
        and ``raw`` (base64-decoded bytes). A non-empty server ``err`` raises
        :class:`TransferError`.
        """
        seq = self._send(cmd, fields or {}, raw)
        msg = self._recv_for(seq, timeout)
        if msg.get("err"):
            raise TransferError(msg["err"])

        data = msg.get("data")
        if isinstance(data, str) and data:
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                pass
        raw_b64 = msg.get("raw")
        return {
            "id": msg.get("id"),
            "type": msg.get("type"),
            "cmd": msg.get("cmd"),
            "data": data,
            "raw": base64.b64decode(raw_b64) if raw_b64 else None,
            "current_path": msg.get("current_path"),
        }

    def _send_pong(self) -> None:
        try:
            self._ws.send(json.dumps({"id": self._ws_id, "type": "PONG"}))
        except Exception as e:
            logger.debug("WebSocket SFTP PONG send failed: %s", e)

    # ──── directory / stat (mirrors SFTPClient surface) ──────────

    def ls(self, path: str) -> list[dict]:
        """List directory entries as dicts with name/size/is_dir keys."""
        resp = self._request("list", {"path": path})
        entries = resp.get("data") or []
        out = []
        for e in entries:
            try:
                size = int(e.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            out.append({
                "name": e.get("name", ""),
                "size": size,
                "is_dir": bool(e.get("is_dir")),
            })
        return out

    def stat(self, path: str) -> dict:
        """Return ``{"size": int, "is_dir": bool}`` for a remote path.

        The protocol has no dedicated stat command; the parent directory is
        listed and the entry matched by basename.
        """
        p = path.rstrip("/")
        if not p:
            return {"size": 0, "is_dir": True}
        parent = posixpath.dirname(p) or "/"
        name = posixpath.basename(p)
        for entry in self.ls(parent):
            if entry["name"] == name:
                return {"size": entry["size"], "is_dir": entry["is_dir"]}
        raise TransferError(f"Remote path not found: {path}")

    def mkdir(self, path: str) -> None:
        """Create a directory tree on the remote (KoKo's ``MkdirAll``)."""
        self._request("mkdir", {"path": path})

    # ──── chunked transfer primitives ────────────────────────────

    def _transfer_id(self) -> str:
        return uuid.uuid4().hex

    def read_chunk(self, path: str, offset: int, length: int) -> tuple[bytes, str, bool]:
        """Read ``length`` bytes at ``offset``. Returns ``(data, sha256, eof)``."""
        resp = self._request(
            "transfer_read",
            {
                "transfer_id": self._transfer_id(),
                "path": path,
                "offset": offset,
                "length": length
            },
        )
        meta = resp.get("data") or {}
        return (
            resp.get("raw") or b"",
            meta.get("sha256", ""),
            bool(meta.get("eof")),
        )

    def upload_file(
        self,
        dst_path: str,
        src_path: str,
        size: int,
        conflict_policy: str = "overwrite",
        chunk_size: int = CHUNK_SIZE,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Upload a local file via ``transfer_prepare`` / ``write`` / ``commit``.

        Args:
            dst_path: Remote destination path.
            src_path: Local source path.
            size: File size in bytes.
            conflict_policy: ``overwrite`` / ``skip`` / ``keep_both`` / ``ask``.
            chunk_size: Bytes per ``transfer_write`` chunk.
            on_progress: Optional callback receiving bytes just written.

        Raises:
            TransferError: The server rejected a prepare/write/commit step.
        """
        tid = self._transfer_id()
        resp = self._request(
            "transfer_prepare",
            {
                "transfer_id": tid,
                "path": dst_path,
                "size": size,
                "conflict_policy": conflict_policy
            },
        )
        state = resp.get("data", {}).get("state") if isinstance(resp.get("data"), dict) else None
        if state == "skipped":
            return
        if state == "conflict":
            raise TransferError(
                f"target exists and conflict policy 'ask' would block upload: {dst_path}"
            )

        full = hashlib.sha256()
        offset = 0
        with open(src_path, "rb") as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                full.update(data)
                self._request(
                    "transfer_write",
                    {
                        "transfer_id": tid,
                        "path": dst_path,
                        "size": size,
                        "offset": offset,
                        "sha256": hashlib.sha256(data).hexdigest()
                    },
                    raw=data,
                )
                offset += len(data)
                if on_progress:
                    on_progress(len(data))

        self._request(
            "transfer_commit",
            {
                "transfer_id": tid,
                "path": dst_path,
                "size": size,
                "sha256": full.hexdigest(),
                "conflict_policy": conflict_policy
            },
            timeout=COMMIT_TIMEOUT,
        )

    def download_file(
        self,
        src_path: str,
        dst_path: str,
        size: int,
        chunk_size: int = CHUNK_SIZE,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Download a remote file to a local path via ``transfer_read``.

        Each chunk's SHA256 is checked against the bytes received; a mismatch
        raises :class:`TransferError` (the SSH backend's md5-verify pass is not
        applicable here because the server already hashes on the way out).

        Args:
            src_path: Remote source path.
            dst_path: Local destination path.
            size: Remote file size in bytes.
            chunk_size: Bytes per ``transfer_read`` request.
            on_progress: Optional callback receiving bytes just written.
        """
        parent = Path(dst_path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)

        offset = 0
        with open(dst_path, "wb") as f:
            while offset < size:
                data, sha256_hex, eof = self.read_chunk(src_path, offset, chunk_size)
                if not data:
                    if eof or offset >= size:
                        break
                    raise TransferError(
                        f"empty chunk at offset {offset} while downloading {src_path}"
                    )
                if sha256_hex and hashlib.sha256(data).hexdigest() != sha256_hex:
                    raise TransferError(
                        f"checksum mismatch at offset {offset} while downloading {src_path}"
                    )
                f.write(data)
                offset += len(data)
                if on_progress:
                    on_progress(len(data))

    def close(self) -> None:
        """Close the WebSocket connection."""
        if self._closed:
            return
        self._closed = True
        try:
            self._ws.close()
        except Exception as e:
            logger.debug("WebSocket SFTP close error (expected): %s", e)

    def __enter__(self) -> "WSFileClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def connect_ws_sftp(session: JMSSession, asset: AssetInfo) -> WSFileClient:
    """Open a ``/koko/ws/sftp/`` session to an asset via connection-token auth.

    Reuses the same token shape as the SSH SFTP path (``protocol="ssh"``,
    ``connect_method="web_sftp"``): SFTP rides the asset's ssh protocol and a
    ``protocol="sftp"`` token is rejected by JumpServer. On handshake failure
    the flow is retried once with a fresh token.

    Args:
        session: Authenticated JMS session.
        asset: Resolved asset info.

    Returns:
        A connected :class:`WSFileClient`.

    Raises:
        TransferError: If the WebSocket handshake or CONNECT message fails.
    """
    hostname = urlparse(session.base_url).hostname or ""
    ws_scheme = "wss" if session.base_url.startswith("https") else "ws"

    def _create_and_connect() -> websocket.WebSocket:
        token = create_connection_token(
            session, asset, protocol="ssh", connect_method="web_sftp",
        )
        ts = int(time.time() * 1000)
        ws_url = (
            f"{ws_scheme}://{hostname}{WS_SFTP_PATH}"
            f"?token={token['id']}&_={ts}"
        )
        # Token id is in the URL — log hygiene: never log the full URL.
        logger.debug(
            "WebSocket SFTP URL: %s://%s%s", ws_scheme, hostname, WS_SFTP_PATH,
        )
        return websocket.create_connection(
            ws_url,
            header=[
                f"Cookie: jms_sessionid={session.session_id}; "
                f"SESSION_COOKIE_NAME_PREFIX=jms_",
            ],
            origin=session.base_url,
            host=hostname,
            subprotocols=["JMS-KOKO"],
            timeout=WS_CONNECT_TIMEOUT,
        )

    try:
        ws = _create_and_connect()
    except Exception as first_err:
        logger.debug(
            "WebSocket SFTP connect failed: %s. Retrying with a fresh token ...",
            first_err,
        )
        try:
            ws = _create_and_connect()
        except Exception as second_err:
            raise TransferError(
                f"WebSocket SFTP connection to {hostname} failed: {second_err}"
            ) from second_err

    try:
        opcode, data = ws.recv_data()
        connect_msg = json.loads(data.decode("utf-8"))
        ws_id = connect_msg.get("id", "")
        logger.debug("WebSocket SFTP CONNECT received, session_id=%s", ws_id)
        return WSFileClient(ws, ws_id)
    except Exception as e:
        ws.close()
        raise TransferError(f"Failed to initialise WebSocket SFTP: {e}") from e
