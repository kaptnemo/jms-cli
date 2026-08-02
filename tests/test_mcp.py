"""MCP server unit tests (no server startup, no network).

Exercises ``jms.mcp.server.build_server``: tool registration and tool
behavior with JMSSession / assets / transport / config all mocked out.
Tools are invoked directly via ``server._tool_manager`` so no stdio MCP
protocol round-trip is needed.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

from jms.core.resources import AssetInfo
from jms.core.auth import JMSSession
from jms.config import AppConfig, ServerConfig
from jms.exceptions import TerminalError
from jms.mcp.server import build_server


def _fake_config() -> AppConfig:
    return AppConfig(
        default="prod",
        servers={
            "prod": ServerConfig(
                name="prod", host="jms.example.com",
                username="alice", password="secret",
            ),
        },
    )


def _tool_fn(name: str):
    """Fetch a registered tool's underlying callable from the server registry."""
    return build_server()._tool_manager.get_tool(name).fn


def _fake_asset() -> AssetInfo:
    return AssetInfo(
        id="a1", name="web-01", address="10.0.0.1",
        account="@USER", protocol="ssh",
    )


# ──── registration ──────────────────────────────────────────────


def test_build_server_registers_tools() -> None:
    server = build_server()
    names = [t.name for t in server._tool_manager.list_tools()]
    assert names == [
        "jms_config_list",
        "jms_ls",
        "jms_resolve_asset",
        "jms_exec",
        "jms_sftp_upload",
        "jms_sftp_download",
        "jms_sftp_relay",
    ]


# ──── jms_config_list ───────────────────────────────────────────


def test_config_list_tool(monkeypatch) -> None:
    monkeypatch.setattr("jms.config.load_config", lambda path=None: _fake_config())
    result = _tool_fn("jms_config_list")()
    assert "prod" in result
    assert "jms.example.com" in result
    assert "*" in result  # default marker


def test_config_list_tool_error(monkeypatch) -> None:
    def _boom(path=None):
        raise FileNotFoundError("no config")

    monkeypatch.setattr("jms.config.load_config", _boom)
    result = _tool_fn("jms_config_list")()
    assert result.startswith("ERROR:")


# ──── jms_ls ────────────────────────────────────────────────────


def test_jms_ls_tool(monkeypatch) -> None:
    monkeypatch.setattr("jms.config.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr(JMSSession, "login", lambda self: None)
    monkeypatch.setattr(
        "jms.core.resources.search_assets",
        lambda session, keyword: [
            {"name": "web-01", "address": "10.0.0.1", "platform": {"name": "Linux"}},
            {"name": "db-01", "address": "10.0.0.2", "platform": "Linux"},
        ],
    )
    result = _tool_fn("jms_ls")(keyword="web")
    assert "web-01" in result
    assert "10.0.0.1" in result
    assert "db-01" in result


def test_jms_ls_tool_empty(monkeypatch) -> None:
    monkeypatch.setattr("jms.config.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr(JMSSession, "login", lambda self: None)
    monkeypatch.setattr("jms.core.resources.search_assets", lambda session, keyword: [])
    result = _tool_fn("jms_ls")(keyword="nope")
    assert "No assets found." in result


# ──── jms_resolve_asset ─────────────────────────────────────────


def test_jms_resolve_asset_tool(monkeypatch) -> None:
    monkeypatch.setattr("jms.config.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr(JMSSession, "login", lambda self: None)
    monkeypatch.setattr(
        "jms.core.resources.resolve_asset",
        lambda session, asset, account=None, protocol=None: _fake_asset(),
    )
    result = _tool_fn("jms_resolve_asset")(asset="web-01")
    assert "web-01" in result
    assert "10.0.0.1" in result
    assert "@USER" in result
    assert "ssh" in result


# ──── jms_exec ──────────────────────────────────────────────────


@contextmanager
def _ok_connect(session, asset, backend=None):
    term = MagicMock()
    term.execute.return_value = "hello\n"
    yield term


def test_jms_exec_tool_success(monkeypatch) -> None:
    monkeypatch.setattr("jms.config.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr(JMSSession, "login", lambda self: None)
    monkeypatch.setattr(
        "jms.core.resources.resolve_asset",
        lambda session, asset, account=None, protocol=None: _fake_asset(),
    )
    monkeypatch.setattr("jms.transport.connect", _ok_connect)
    result = _tool_fn("jms_exec")(asset="web-01", command="whoami")
    assert result == "hello\n"


def test_jms_exec_tool_error(monkeypatch) -> None:
    monkeypatch.setattr("jms.config.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr(JMSSession, "login", lambda self: None)
    monkeypatch.setattr(
        "jms.core.resources.resolve_asset",
        lambda session, asset, account=None, protocol=None: _fake_asset(),
    )

    @contextmanager
    def _fail_connect(session, asset, backend=None):
        raise TerminalError("connection refused")

    monkeypatch.setattr("jms.transport.connect", _fail_connect)
    result = _tool_fn("jms_exec")(asset="web-01", command="whoami")
    assert result.startswith("ERROR:")
    assert "connection refused" in result


# ──── sftp tools (error path only — transfer engine is mocked away) ──


def test_jms_sftp_upload_success(monkeypatch) -> None:
    monkeypatch.setattr("jms.config.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr("jms.io.service.sftp_transfer", lambda *a, **k: None)
    result = _tool_fn("jms_sftp_upload")(
        src="./local.txt", asset="web-01", dst="/tmp/local.txt",
    )
    assert result.startswith("OK:")
    assert "local.txt" in result


def test_jms_sftp_download_success(monkeypatch) -> None:
    monkeypatch.setattr("jms.config.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr("jms.io.service.sftp_transfer", lambda *a, **k: None)
    result = _tool_fn("jms_sftp_download")(
        asset="web-01", src="/tmp/data.csv", dst="./data.csv",
    )
    assert result.startswith("OK:")
    assert "data.csv" in result


def test_jms_sftp_relay_success(monkeypatch) -> None:
    monkeypatch.setattr("jms.io.service.relay_transfer", lambda *a, **k: None)
    result = _tool_fn("jms_sftp_relay")(
        src_spec="host-a@prod:/tmp/a.txt", dst_spec="host-b@prod:/tmp/b.txt",
    )
    assert result.startswith("OK:")


def test_jms_sftp_relay_rejects_local_spec() -> None:
    result = _tool_fn("jms_sftp_relay")(
        src_spec="./a.txt", dst_spec="host-b@prod:/tmp/b.txt",
    )
    assert result.startswith("ERROR:")
