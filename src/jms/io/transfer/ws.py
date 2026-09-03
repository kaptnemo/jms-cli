"""HTTP file-transfer backend over KoKo's ``/koko/ws/sftp/`` WebSocket.

JumpServer's web SFTP editor talks to KoKo through a JSON-message WebSocket
protocol (NOT a raw SFTP-over-WebSocket binary stream). Every frame is a JSON
text message of the form::

    {"id": "...", "cmd": "...", "data": "<json string>", "raw": "<base64>"}

Binary payloads ride in the base64 ``raw`` field (Go's ``encoding/json``
marshals ``[]byte`` as base64). The endpoint exposes elFinder-style commands —
``list`` / ``download`` / ``upload`` / ``rm`` / ``rename`` / ``mkdir`` — that
are present across KoKo versions, so this backend deliberately sticks to them
rather than the newer (and not yet universally deployed) ``transfer_*``
checksummed chunk protocol:

- ``list``     — directory listing (name / size / is_dir)
- ``download`` — stream the file as ``SFTP_BINARY`` frames + a final
                 ``SFTP_DATA`` frame carrying the filename
- ``upload``   — ``chunk=true`` + ``offset`` writes one chunk keyed by an
                 integer ``id``; ``merge=true`` closes the server-side handle

Each chunk of a single file must reuse the same integer ``id`` (the server
keys its open file handle on it), so a file is uploaded sequentially. Distinct
files are still transferred across worker threads, each holding its own
WebSocket connection.

The client mirrors the ``SFTPClient`` surface used by ``list_remote_files`` /
``resolve_remote_dst`` (``ls`` / ``stat`` / ``close``), so the existing
transfer orchestration reuses those helpers unchanged.
"""

from __future__ import annotations

import base64
import json
import posixpath
import time
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

# Default per-response timeout (seconds).
OP_TIMEOUT: int = 120

# Chunk size (bytes) for the ``upload`` chunked command. Kept aligned with the
# server's own 2MB download framing so a single message never grows unwieldy.
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

    def _send(self, cmd: str, fields: dict, raw: Optional[bytes], id: Optional[str]) -> str:
        """Send one command; return the request id used to correlate the reply."""
        seq = id if id is not None else str(self._next_seq())
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
        id: Optional[str] = None,
    ) -> dict:
        """Send a command and return its normalized response.

        The returned dict normalizes ``data`` (JSON-decoded when possible)
        and ``raw`` (base64-decoded bytes). A non-empty server ``err`` raises
        :class:`TransferError`.
        """
        seq = self._send(cmd, fields or {}, raw, id)
        msg = self._recv_for(seq, timeout)
        if msg.get("err"):
            raise TransferError(msg["err"])
        return self._normalize(msg)

    @staticmethod
    def _normalize(msg: dict) -> dict:
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

    # ──── transfer ───────────────────────────────────────────────

    def upload_file(
        self,
        dst_path: str,
        src_path: str,
        size: int,
        chunk_size: int = CHUNK_SIZE,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Upload a local file via the chunked ``upload`` command.

        Empty files use a single non-chunk ``upload`` (which creates the
        zero-byte target); otherwise chunks are sent with ``chunk=true`` +
        ``offset`` under one integer id, closed by a final ``merge=true``.

        Args:
            dst_path: Remote destination path.
            src_path: Local source path.
            size: File size in bytes.
            chunk_size: Bytes per ``upload`` chunk.
            on_progress: Optional callback receiving bytes just written.

        Raises:
            TransferError: The server rejected an upload/merge step.
        """
        if size == 0:
            self._request("upload", {"path": dst_path, "size": 0})
            return

        cid = str(self._next_seq())
        offset = 0
        with open(src_path, "rb") as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                self._request(
                    "upload",
                    {"path": dst_path, "chunk": True, "offset": offset},
                    raw=data,
                    id=cid,
                )
                offset += len(data)
                if on_progress:
                    on_progress(len(data))
        self._request("upload", {"path": dst_path, "merge": True}, id=cid)

    def download_file(
        self,
        src_path: str,
        dst_path: str,
        size: int,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Download a remote file to a local path via the ``download`` command.

        The server streams ``SFTP_BINARY`` frames followed by one ``SFTP_DATA``
        frame; the payload is written to ``dst_path`` verbatim.

        Args:
            src_path: Remote source path.
            dst_path: Local destination path.
            size: Remote file size in bytes (informational; the stream is
                authoritative).
            on_progress: Optional callback receiving bytes just written.
        """
        seq = self._send("download", {"path": src_path, "is_dir": False}, None, None)
        chunks: list[bytes] = []
        while True:
            msg = self._recv_for(seq, OP_TIMEOUT)
            if msg.get("err"):
                raise TransferError(msg["err"])
            if msg.get("type") == "SFTP_DATA":
                break  # terminal frame (carries the filename)
            raw_b64 = msg.get("raw")
            if raw_b64:
                data = base64.b64decode(raw_b64)
                chunks.append(data)
                if on_progress:
                    on_progress(len(data))

        parent = Path(dst_path).parent
        if str(parent):
            parent.mkdir(parents=True, exist_ok=True)
        with open(dst_path, "wb") as f:
            for data in chunks:
                f.write(data)

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
