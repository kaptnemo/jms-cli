# -*- coding: utf-8 -*-
"""真实 JumpServer 集成测试（不打 mock，连用户部署的服务器）。

凭据从环境变量读取（不入库）：

    JMS_TEST_HOST       JumpServer 地址（如 192.168.31.78）
    JMS_TEST_USERNAME   登录账号
    JMS_TEST_PASSWORD   登录密码
    JMS_TEST_OTP        TOTP secret（MFA 强制时必填）
    JMS_TEST_ASSET      授权资产名（默认 home_server）

环境变量缺失或服务器不可达时自动 skip。
"""

import hashlib
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlparse

import pytest
from click.testing import CliRunner

from jms.core.resources import resolve_asset, search_assets
from jms.core.auth import JMSSession
from jms.transport import BackendType, connect, create_connection_token
from jms.cli import cli
from jms.config import ServerConfig, add_server, config_file_path
from jms.exceptions import ConnectionTokenError, TransferError
from jms.io.transfer import connect_sftp

_ENV_VARS = ["JMS_TEST_HOST", "JMS_TEST_USERNAME", "JMS_TEST_PASSWORD"]

pytestmark = pytest.mark.skipif(
    not all(os.environ.get(v) for v in _ENV_VARS),
    reason=f"需要环境变量: {', '.join(_ENV_VARS)}",
)


@pytest.fixture(scope="module")
def server() -> ServerConfig:
    return ServerConfig(
        name="test",
        host=os.environ["JMS_TEST_HOST"],
        username=os.environ["JMS_TEST_USERNAME"],
        password=os.environ["JMS_TEST_PASSWORD"],
        otp_secret=os.environ.get("JMS_TEST_OTP", ""),
    )


@pytest.fixture(scope="module")
def session(server: ServerConfig) -> JMSSession:
    u = urlparse(server.base_url)
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        socket.create_connection((u.hostname, port), timeout=3).close()
    except OSError:
        pytest.skip(f"JumpServer 不可达: {u.hostname}:{port}")
    sess = JMSSession(server)
    sess.login()
    return sess


@pytest.fixture(scope="module")
def asset_name() -> str:
    return os.environ.get("JMS_TEST_ASSET", "home_server")


def test_login(session: JMSSession) -> None:
    """双认证：Bearer token + jms_sessionid cookie 都拿到。"""
    assert session.bearer_token
    assert session.session_id
    assert session.is_authenticated


def test_search_assets(session: JMSSession, asset_name: str) -> None:
    results = search_assets(session, asset_name)
    assert any(a.get("name") == asset_name for a in results)


def test_resolve_asset(session: JMSSession, asset_name: str) -> None:
    asset = resolve_asset(session, asset_name)
    assert asset.id
    assert asset.account  # alias（如 @USER 或 gcszhn）
    assert asset.protocol == "ssh"


@pytest.mark.parametrize("backend", [BackendType.SSH, BackendType.WEBSOCKET])
def test_execute(session: JMSSession, asset_name: str,
                 backend: BackendType) -> None:
    """两种后端各跑一条真命令。"""
    asset = resolve_asset(session, asset_name)
    with connect(session, asset, backend=backend) as term:
        out = term.execute("echo jms-$((6*7))", timeout=30)
    assert "jms-42" in out


class _ITConfig(NamedTuple):
    """Throwaway config.yaml inside a fake HOME for CLI / ssh-pipe e2e."""

    path: Path
    home: Path


@pytest.fixture(scope="module")
def it_config(server: ServerConfig, tmp_path_factory: pytest.TempPathFactory) -> _ITConfig:
    """Write a real (encrypted-on-save) config.yaml under a fake HOME.

    ``--config <path>`` covers the Click commands; the ssh-pipe bridge only
    reads the platformdirs default location, so tests redirect it via HOME.
    """
    home = tmp_path_factory.mktemp("it-home")
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(home))
    # platformdirs honors XDG_CONFIG_HOME even on macOS; pin it so the
    # path computed here matches the ssh-pipe subprocess env exactly
    mp.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    try:
        path = config_file_path()
    finally:
        mp.undo()
    add_server(
        name="it", host=server.host, username=server.username,
        password=server.password, otp_secret=server.otp_secret,
        set_default=True, config_path=str(path),
    )
    return _ITConfig(path=path, home=home)


def test_sftp_roundtrip(
    session: JMSSession, asset_name: str, tmp_path: Path,
) -> None:
    """Upload a 1MB random file over SFTP, download it back, md5 must match.

    Skips when the asset permission lacks upload/download actions — KoKo
    answers "please select one of the assets" (no connect-level asset) or
    EACCES (actions not granted). That is a server-side grant issue, not
    a product bug. The remote path is relative: KoKo chroots the session
    to the account's sftp home, so absolute paths like /tmp may not exist.
    """
    asset = resolve_asset(session, asset_name)
    payload = os.urandom(1024 * 1024)
    local_up = tmp_path / "up.bin"
    local_up.write_bytes(payload)
    remote = f"jms-it-{os.getpid()}.bin"
    local_dn = tmp_path / "dn.bin"
    try:
        with connect_sftp(session, asset) as client:
            ch = client.new_channel()
            try:
                ch.put(str(local_up), remote)
                ch.get(remote, str(local_dn))
            finally:
                try:
                    ch.remove(remote)
                except OSError:
                    pass
                ch.close()
    except (OSError, TransferError) as e:
        if "please select one of the assets" in str(e) or isinstance(e, PermissionError):
            pytest.skip("asset permission lacks upload/download actions")
        raise
    assert hashlib.md5(local_dn.read_bytes()).hexdigest() == \
        hashlib.md5(payload).hexdigest()


def test_connection_token_contract(session: JMSSession, asset_name: str) -> None:
    """Protocol contract of this server version: ssh+web_sftp token is
    accepted, protocol="sftp" is rejected (perm_account_invalid)."""
    asset = resolve_asset(session, asset_name)
    token = create_connection_token(
        session, asset, protocol="ssh", connect_method="web_sftp",
    )
    assert token.get("id")
    assert token.get("value")
    with pytest.raises(ConnectionTokenError):
        create_connection_token(
            session, asset, protocol="sftp", connect_method="web_sftp",
        )


def test_cli_ls(session: JMSSession, asset_name: str, it_config: _ITConfig) -> None:
    """CLI e2e: `jms ls` lists the authorized asset (session fixture gates
    reachability; the command performs its own login via the tmp config)."""
    result = CliRunner().invoke(cli, ["ls", "--config", str(it_config.path)])
    assert result.exit_code == 0, result.output
    assert asset_name in result.output


def test_cli_exec(session: JMSSession, asset_name: str, it_config: _ITConfig) -> None:
    """CLI e2e: `jms exec` runs a remote command (non-interactive stdin is
    fine for exec; login needs a real PTY and is not covered)."""
    result = CliRunner().invoke(cli, [
        "exec", asset_name, "echo", "jms-e2e-$((6*7))",
        "--config", str(it_config.path),
    ])
    assert result.exit_code == 0, result.output
    assert "jms-e2e-42" in result.output


@pytest.mark.parametrize("target_args", [
    ["-l", None, "it"],   # classic rsync: -l <asset> <server>
    [None],               # openrsync: <asset>@<server> (macOS default)
])
def test_ssh_pipe_bridge(
    session: JMSSession, asset_name: str, it_config: _ITConfig,
    target_args: list,
) -> None:
    """ssh-pipe e2e: subprocess bridge relays stdio to a remote `cat` —
    stdin comes back on stdout, remote exit code 0 propagates.

    Both rsync invocation forms are covered (classic ``-l`` and openrsync
    ``user@host``). The bridge loads config from the platformdirs default
    location only, so the subprocess runs with HOME pointed at the
    fixture's fake home (XDG_CONFIG_HOME pinned too, where it would win
    over HOME).
    """
    env = {
        **os.environ,
        "HOME": str(it_config.home),
        "XDG_CONFIG_HOME": str(it_config.home / ".config"),
    }
    argv = ["-l", asset_name, "it"] if target_args[0] == "-l" \
        else [f"{asset_name}@it"]
    proc = subprocess.Popen(
        [sys.executable, "-m", "jms.cli", "ssh-pipe", *argv, "cat"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env,
    )
    try:
        out, err = proc.communicate(b"jms-pipe-echo\n", timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err.decode(errors="replace")
    assert b"jms-pipe-echo" in out
