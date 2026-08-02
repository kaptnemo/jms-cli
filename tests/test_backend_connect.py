# -*- coding: utf-8 -*-
"""Tests for jms.transport.connect 工厂 — 两个后端实现全部 mock。"""

from unittest.mock import MagicMock

import pytest

from jms.core.resources import AssetInfo
from jms.transport import BackendType, connect, list_backends, open_backend
from jms.transport.base import TerminalCapability
from jms.transport.registry import (
    _BACKENDS,
    backend_capabilities,
    register_backend,
)
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
        "jms.transport.open_ssh_terminal", lambda s, a: _term("ssh"),
    )
    with connect(MagicMock(), ASSET, backend=BackendType.SSH) as term:
        assert term.backend_name == "ssh"
    term.close.assert_called_once()


def test_connect_ws_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jms.transport.open_ws_terminal", lambda s, a: _term("ws"),
    )
    with connect(MagicMock(), ASSET, backend=BackendType.WEBSOCKET) as term:
        assert term.backend_name == "ws"
    term.close.assert_called_once()


def test_connect_auto_prefers_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    ws_called = []
    monkeypatch.setattr(
        "jms.transport.open_ssh_terminal", lambda s, a: _term("ssh"),
    )
    monkeypatch.setattr(
        "jms.transport.open_ws_terminal",
        lambda s, a: ws_called.append(1) or _term("ws"),
    )
    with connect(MagicMock(), ASSET, backend=BackendType.AUTO) as term:
        assert term.backend_name == "ssh"
    assert not ws_called


def test_connect_auto_falls_back_to_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUTO：SSH 连接失败回退 WebSocket。"""

    def _fail(s, a):
        raise TerminalError("connect failed")

    monkeypatch.setattr("jms.transport.open_ssh_terminal", _fail)
    monkeypatch.setattr(
        "jms.transport.open_ws_terminal", lambda s, a: _term("ws"),
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
        "jms.transport.open_ssh_terminal", lambda s, a: _term("ssh"),
    )
    monkeypatch.setattr(
        "jms.transport.open_ws_terminal",
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


def test_register_and_open() -> None:
    """注册自定义后端后可按名打开，list_backends() 包含它和内置后端。"""

    def _dummy(s, a):
        return _term("dummy")

    register_backend("dummy", _dummy)
    try:
        term = open_backend("dummy", MagicMock(), ASSET)
        assert term.backend_name == "dummy"
        names = list_backends()
        assert "dummy" in names
        assert "ssh" in names and "ws" in names
    finally:
        _BACKENDS.pop("dummy", None)


def test_unknown_backend_raises() -> None:
    with pytest.raises(TerminalError, match="Unknown backend"):
        open_backend("nope", MagicMock(), ASSET)


def test_connect_accepts_string_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect 接受原始字符串后端名，经 registry 解析。"""
    monkeypatch.setattr(
        "jms.transport.open_ws_terminal", lambda s, a: _term("ws"),
    )
    with connect(MagicMock(), ASSET, backend="ws") as term:
        assert term.backend_name == "ws"
    term.close.assert_called_once()


def test_connect_auto_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog,
) -> None:
    """AUTO：首个后端抛 TerminalError，换下一个成功，且打印日志。"""

    def _fail(s, a):
        raise TerminalError("connect failed")

    monkeypatch.setattr("jms.transport.open_ssh_terminal", _fail)
    monkeypatch.setattr(
        "jms.transport.open_ws_terminal", lambda s, a: _term("ws"),
    )
    with caplog.at_level("INFO", logger="jms"):
        with connect(MagicMock(), ASSET, backend=BackendType.AUTO) as term:
            assert term.backend_name == "ws"
    assert "trying next" in caplog.text
    term.close.assert_called_once()


def test_duplicate_register_raises() -> None:
    with pytest.raises(ValueError, match="already registered"):
        register_backend("ssh", lambda s, a: _term("ssh"))


def test_capabilities_metadata() -> None:
    assert backend_capabilities("ssh") == frozenset({
        TerminalCapability.EXEC, TerminalCapability.INTERACTIVE,
    })
    assert backend_capabilities("ws") == frozenset({
        TerminalCapability.EXEC, TerminalCapability.INTERACTIVE,
    })
    assert backend_capabilities("nope") == frozenset()
