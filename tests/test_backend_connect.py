# -*- coding: utf-8 -*-
"""Tests for jms.backend.connect 工厂 — 两个后端实现全部 mock。"""

from unittest.mock import MagicMock

import pytest

from jms.assets import AssetInfo
from jms.backend import BackendType, connect
from jms.exceptions import TerminalError

ASSET = AssetInfo(
    id="asset-uuid-1", name="web1", address="10.0.0.1",
    account="@USER", protocol="ssh",
)


def _term(name: str) -> MagicMock:
    t = MagicMock()
    t.backend_name = name
    return t


def test_connect_ssh_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jms.backend.open_ssh_terminal", lambda s, a: _term("ssh"),
    )
    with connect(MagicMock(), ASSET, backend=BackendType.SSH) as term:
        assert term.backend_name == "ssh"
    term.close.assert_called_once()


def test_connect_ws_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jms.backend.open_ws_terminal", lambda s, a: _term("ws"),
    )
    with connect(MagicMock(), ASSET, backend=BackendType.WEBSOCKET) as term:
        assert term.backend_name == "ws"
    term.close.assert_called_once()


def test_connect_auto_prefers_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    ws_called = []
    monkeypatch.setattr(
        "jms.backend.open_ssh_terminal", lambda s, a: _term("ssh"),
    )
    monkeypatch.setattr(
        "jms.backend.open_ws_terminal",
        lambda s, a: ws_called.append(1) or _term("ws"),
    )
    with connect(MagicMock(), ASSET, backend=BackendType.AUTO) as term:
        assert term.backend_name == "ssh"
    assert not ws_called


def test_connect_auto_falls_back_to_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTO：SSH 连接失败回退 WebSocket。"""

    def _fail(s, a):
        raise TerminalError("connect failed")

    monkeypatch.setattr("jms.backend.open_ssh_terminal", _fail)
    monkeypatch.setattr(
        "jms.backend.open_ws_terminal", lambda s, a: _term("ws"),
    )
    with connect(MagicMock(), ASSET, backend=BackendType.AUTO) as term:
        assert term.backend_name == "ws"
    term.close.assert_called_once()


def test_connect_auto_body_error_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTO：with 体内抛 TerminalError 不得触发回退，原异常原样冒出。"""
    ws_called = []
    monkeypatch.setattr(
        "jms.backend.open_ssh_terminal", lambda s, a: _term("ssh"),
    )
    monkeypatch.setattr(
        "jms.backend.open_ws_terminal",
        lambda s, a: ws_called.append(1) or _term("ws"),
    )
    with pytest.raises(TerminalError, match="boom"):
        with connect(MagicMock(), ASSET, backend=BackendType.AUTO) as term:
            raise TerminalError("boom")
    assert not ws_called  # 没有误开 WS 连接
    term.close.assert_called_once()


def test_connect_unknown_backend() -> None:
    with pytest.raises(TerminalError, match="Unknown backend"):
        with connect(MagicMock(), ASSET, backend="telnet"):  # type: ignore[arg-type]
            pass
