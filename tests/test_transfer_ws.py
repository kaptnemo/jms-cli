"""Tests for jms.io.transfer.ws — the /koko/ws/sftp/ HTTP file-transfer backend.

``websocket.create_connection`` and ``create_connection_token`` are mocked, and
an in-memory ``FakeWS`` replays the KoKo SFTP message protocol (JSON text
frames with base64 ``raw``). Tests assert observable protocol behavior (URL,
cookie, command sequence, chunk offsets) — not mock call counts.
"""

import base64
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest
import websocket

from jms.core.resources import AssetInfo
from jms.exceptions import TransferError
from jms.io.transfer.ws import CHUNK_SIZE, WSFileClient, connect_ws_sftp
from jms.io.service import relay_transfer, resolve_backend, sftp_transfer
from jms.io.transfer import RelaySpec, TransferSpec

ASSET = AssetInfo(
    id="asset-uuid-1", name="web1", address="10.0.0.1",
    account="@USER", protocol="ssh",
)


def _session(base_url: str = "https://jump.example.com") -> MagicMock:
    session = MagicMock()
    session.base_url = base_url
    session.session_id = "sid-abc"
    return session


class FakeWS:
    """In-memory WebSocket: records sends and replays queued text frames."""

    def __init__(self, frames: list | None = None) -> None:
        self.sent: list[str] = []
        self.frames: list = list(frames or [])
        self.closed = False
        self.sock = None

    def send(self, data: str) -> None:
        self.sent.append(data)

    def settimeout(self, _t: float) -> None:
        pass

    def recv_data(self):
        if self.frames:
            return self.frames.pop(0)
        raise websocket.WebSocketTimeoutException("timeout")

    def close(self) -> None:
        self.closed = True


def _connect_frame(ws_id: str = "ws-uuid-1") -> tuple:
    return (1, json.dumps({"id": ws_id, "type": "CONNECT"}).encode())


def _text(payload: dict) -> tuple:
    return (1, json.dumps(payload).encode())


# ──── connect_ws_sftp ────────────────────────────────────────────


def test_connect_ws_sftp_url_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = FakeWS([_connect_frame()])
    factory = MagicMock(return_value=ws)
    monkeypatch.setattr("jms.io.transfer.ws.websocket.create_connection", factory)
    token = MagicMock(return_value={"id": "tok-id", "value": "tok-val"})
    monkeypatch.setattr("jms.io.transfer.ws.create_connection_token", token)

    client = connect_ws_sftp(_session("https://jump.example.com"), ASSET)

    assert isinstance(client, WSFileClient)
    assert client._ws_id == "ws-uuid-1"

    # token is created with the same shape as the SSH SFTP path
    assert token.call_args.kwargs == {"protocol": "ssh", "connect_method": "web_sftp"}

    url = factory.call_args.args[0]
    assert url.startswith("wss://jump.example.com/koko/ws/sftp/?token=tok-id")
    kwargs = factory.call_args.kwargs
    assert any("jms_sessionid=sid-abc" in h for h in kwargs["header"])
    assert kwargs["origin"] == "https://jump.example.com"
    assert kwargs["subprotocols"] == ["JMS-KOKO"]

    client.close()
    assert ws.closed


def test_connect_ws_sftp_ws_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWS([_connect_frame()])
    factory = MagicMock(return_value=ws)
    monkeypatch.setattr("jms.io.transfer.ws.websocket.create_connection", factory)
    monkeypatch.setattr(
        "jms.io.transfer.ws.create_connection_token",
        MagicMock(return_value={"id": "tok-id", "value": "tok-val"}),
    )

    connect_ws_sftp(_session("http://jump.internal"), ASSET)

    assert factory.call_args.args[0].startswith("ws://jump.internal/koko/ws/sftp/")


def test_connect_ws_sftp_retries_once(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = FakeWS([_connect_frame()])
    factory = MagicMock(side_effect=[Exception("handshake boom"), ws])
    monkeypatch.setattr("jms.io.transfer.ws.websocket.create_connection", factory)
    token = MagicMock(return_value={"id": "tok-id", "value": "tok-val"})
    monkeypatch.setattr("jms.io.transfer.ws.create_connection_token", token)

    connect_ws_sftp(_session(), ASSET)

    assert factory.call_count == 2
    assert token.call_count == 2


def test_connect_ws_sftp_retry_fails_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jms.io.transfer.ws.websocket.create_connection",
        MagicMock(side_effect=Exception("boom")),
    )
    monkeypatch.setattr(
        "jms.io.transfer.ws.create_connection_token",
        MagicMock(return_value={"id": "tok-id", "value": "tok-val"}),
    )

    with pytest.raises(TransferError, match="WebSocket SFTP connection"):
        connect_ws_sftp(_session(), ASSET)


# ──── protocol helpers ───────────────────────────────────────────


class ScriptedWS(FakeWS):
    """Routes each sent command to a handler that enqueues a response."""

    def __init__(self, handler) -> None:
        super().__init__()
        self._handler = handler

    def send(self, data: str) -> None:
        super().send(data)
        msg = json.loads(data)
        seq = msg["id"]
        cmd = msg["cmd"]
        fields = json.loads(msg.get("data") or "{}")
        raw = base64.b64decode(msg["raw"]) if msg.get("raw") else None
        data, raw_resp = self._handler(cmd, fields, raw)
        frame: dict = {"id": seq, "type": "SFTP_DATA", "cmd": cmd, "data": data}
        if raw_resp is not None:
            frame["raw"] = base64.b64encode(raw_resp).decode("ascii")
        self.frames.append(_text(frame))


def _client(handler) -> WSFileClient:
    return WSFileClient(ScriptedWS(handler), "ws-uuid-1")


def test_ls_parses_size_and_dir() -> None:
    def handler(cmd, fields, raw):
        assert cmd == "list"
        return json.dumps([
            {"name": "a.txt", "size": "42", "is_dir": False},
            {"name": "sub", "size": "0", "is_dir": True},
        ]), None

    client = _client(handler)
    entries = client.ls("/data")
    assert entries == [
        {"name": "a.txt", "size": 42, "is_dir": False},
        {"name": "sub", "size": 0, "is_dir": True},
    ]


def test_stat_matches_entry_in_parent() -> None:
    def handler(cmd, fields, raw):
        assert cmd == "list"
        assert fields["path"] == "/data"
        return json.dumps([
            {"name": "f.bin", "size": "100", "is_dir": False},
        ]), None

    client = _client(handler)
    assert client.stat("/data/f.bin") == {"size": 100, "is_dir": False}


def test_stat_root_is_dir() -> None:
    client = WSFileClient(FakeWS(), "ws-uuid-1")
    assert client.stat("/") == {"size": 0, "is_dir": True}


def test_server_err_raises() -> None:
    class _ErrWS(ScriptedWS):
        def send(self, data: str) -> None:
            msg = json.loads(data)
            self.frames.append(_text({
                "id": msg["id"], "type": "SFTP_DATA", "err": "permission denied",
            }))

    client = WSFileClient(_ErrWS(lambda c, f, r: ("", None)), "ws-uuid-1")
    with pytest.raises(TransferError, match="permission denied"):
        client.ls("/x")


def test_upload_file_chunk_then_merge() -> None:
    content = b"x" * (CHUNK_SIZE + 17)  # spans two chunks
    records = []

    def handler(cmd, fields, raw):
        records.append((cmd, fields, raw))
        if cmd == "upload":
            return "ok", None
        raise AssertionError(f"unexpected cmd {cmd}")

    fd, path = tempfile.mkstemp()
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)

        progress = []
        client = _client(handler)
        client.upload_file("/dst/f.bin", path, len(content), on_progress=progress.append)

        assert [r[0] for r in records] == ["upload", "upload", "upload"]
        first, second, third = records
        assert first[1] == {"path": "/dst/f.bin", "chunk": True, "offset": 0}
        assert second[1] == {"path": "/dst/f.bin", "chunk": True, "offset": CHUNK_SIZE}
        assert third[1] == {"path": "/dst/f.bin", "merge": True}
        assert first[2] == content[:CHUNK_SIZE]
        assert second[2] == content[CHUNK_SIZE:]
        assert third[2] is None
        assert sum(progress) == len(content)
        # all chunks share one integer id (the server-side handle key)
        ids = {json.loads(s)["id"] for s in client._ws.sent}
        assert ids == {"1"}
    finally:
        os.unlink(path)


def test_upload_empty_file_single_non_chunk() -> None:
    records = []

    def handler(cmd, fields, raw):
        records.append((cmd, fields, raw))
        return "ok", None

    fd, path = tempfile.mkstemp()
    try:
        os.close(fd)
        client = _client(handler)
        client.upload_file("/dst/e.bin", path, 0)
        assert records == [("upload", {"path": "/dst/e.bin", "size": 0}, None)]
    finally:
        os.unlink(path)


class DownloadWS(FakeWS):
    """Replays a ``download`` stream: N SFTP_BINARY frames + 1 SFTP_DATA."""

    def __init__(self, chunks: list[bytes], filename: str = "f.bin", err: str = "") -> None:
        super().__init__()
        self._chunks = chunks
        self._filename = filename
        self._err = err

    def send(self, data: str) -> None:
        super().send(data)
        msg = json.loads(data)
        seq = msg["id"]
        if self._err:
            self.frames.append(_text({
                "id": seq, "type": "SFTP_DATA", "err": self._err,
            }))
            return
        for chunk in self._chunks:
            self.frames.append(_text({
                "id": seq, "type": "SFTP_BINARY",
                "raw": base64.b64encode(chunk).decode("ascii"),
            }))
        self.frames.append(_text({
            "id": seq, "type": "SFTP_DATA", "data": self._filename,
        }))


def test_download_file_streams_frames(tmp_path) -> None:
    content = b"hello download"
    dst = tmp_path / "out" / "f.bin"
    ws = DownloadWS([b"hello ", b"download"])
    client = WSFileClient(ws, "ws-uuid-1")

    progress = []
    client.download_file("/f.bin", str(dst), len(content), on_progress=progress.append)

    assert dst.read_bytes() == content
    assert sum(progress) == len(content)


def test_download_error_raises(tmp_path) -> None:
    dst = tmp_path / "f.bin"
    ws = DownloadWS([b"partial"], err="no such file")
    client = WSFileClient(ws, "ws-uuid-1")

    with pytest.raises(TransferError, match="no such file"):
        client.download_file("/f.bin", str(dst), 7)


# ──── backend dispatch ───────────────────────────────────────────


def test_resolve_backend_default_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JMS_TRANSFER_BACKEND", raising=False)
    assert resolve_backend() == "ssh"
    assert resolve_backend("WS") == "ws"
    assert resolve_backend("http") == "ws"
    monkeypatch.setenv("JMS_TRANSFER_BACKEND", "ws")
    assert resolve_backend() == "ws"
    with pytest.raises(TransferError, match="Unknown transfer backend"):
        resolve_backend("ftp")


def test_sftp_transfer_dispatches_to_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    called = {}

    def fake_ws_transfer(server, spec, account, **kwargs):
        called["server"] = server
        called["spec"] = spec
        called["kwargs"] = kwargs

    monkeypatch.setattr("jms.io.service.ws_transfer", fake_ws_transfer)
    server = MagicMock()
    spec = TransferSpec(
        asset="web1", server=None, remote_path="/tmp/x",
        local_path="./x", is_upload=True,
    )
    sftp_transfer(server, spec, backend="ws")

    assert called["spec"] is spec
    assert called["kwargs"]["n_workers"] == 4


def test_relay_ws_backend_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = RelaySpec("a", "s1", "/x", "b", "s2", "/y")
    with pytest.raises(TransferError, match="not supported by the ws backend"):
        relay_transfer(spec, backend="ws")
