"""CLI parameter-layer tests (no server contact).

Covers target-syntax parsing, global log level, ssh-pipe argument forms
(classic rsync ``-l`` vs openrsync ``user@host``), config error paths, and
command behavior with the network boundary (load_config / JMSSession /
assets) mocked out. Real-server end-to-end coverage lives outside this file.
"""

import logging
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from jms import __version__
from jms.cli import cli, main, parse_target
from jms.config import AppConfig, ServerConfig, load_config
from jms.exceptions import AuthError, ConfigError
from jms.log import logger
from jms.io.transfer import FileTask, TaskResult
from jms.io.verify import FileVerifyResult


def _null_cm():
    return nullcontext(MagicMock())


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_log_level() -> Iterator[None]:
    """Keep the global logger at INFO so -l DEBUG never leaks across tests."""
    yield
    logger.setLevel(logging.INFO)


# ──── target parsing ────────────────────────────────────────────


@pytest.mark.parametrize("spec,asset,server", [
    ("10.0.0.1@prod", "10.0.0.1", "prod"),
    ("my-host@local", "my-host", "local"),
    ("my-host", "my-host", None),
    ("a-b_c.example.com", "a-b_c.example.com", None),
    ("asset@", "asset@", None),    # trailing @ → no server, whole spec is the asset
    ("@server", "@server", None),  # leading @ → same
])
def test_parse_target(spec: str, asset: str, server: str | None) -> None:
    t = parse_target(spec)
    assert (t.asset, t.server) == (asset, server)


# ──── ssh-pipe command (rsync/scp -e bridge) ────────────────────


def _spy_bridge(calls: dict, code: int = 0):
    def spy(asset: str, server: str, cmd: str, config_path: str | None) -> int:
        calls.update(asset=asset, server=server, cmd=cmd, config_path=config_path)
        return code
    return spy


@pytest.mark.parametrize("argv", [
    ["ssh-pipe", "-l", "web-01", "prod", "rsync", "--server", "."],  # classic
    ["ssh-pipe", "web-01@prod", "rsync", "--server", "."],           # openrsync
])
def test_ssh_pipe_arg_forms(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, argv: list[str],
) -> None:
    calls: dict = {}
    monkeypatch.setattr("jms.io.ssh_pipe.run_bridge", _spy_bridge(calls, code=42))
    result = runner.invoke(cli, argv)
    assert result.exit_code == 42  # bridge return value becomes the exit code
    assert calls["asset"] == "web-01"
    assert calls["server"] == "prod"
    assert calls["cmd"] == "rsync --server ."


@pytest.mark.parametrize("argv", [
    ["ssh-pipe", "foo"],                 # neither form
    ["ssh-pipe", "-l", "web-01"],        # classic form missing server
    ["ssh-pipe", "web-01@prod"],         # no remote command
])
def test_ssh_pipe_bad_args(runner: CliRunner, argv: list[str]) -> None:
    result = runner.invoke(cli, argv)
    assert result.exit_code != 0


def test_ssh_pipe_fatal_never_tracebacks(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner,
) -> None:
    def boom(asset: str, server: str, cmd: str, config_path: str | None) -> int:
        raise RuntimeError("kaput")

    monkeypatch.setattr("jms.io.ssh_pipe.run_bridge", boom)
    result = runner.invoke(cli, ["ssh-pipe", "web-01@prod", "true"])
    assert result.exit_code == 1
    assert "jms ssh-pipe: fatal: kaput" in result.output
    assert "Traceback" not in result.output


def test_main_dispatches_to_click(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(sys, "argv", ["jms", "--version"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


# ──── global options ────────────────────────────────────────────


def test_version_option(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_invalid_log_level(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["-l", "BOGUS", "ls"])
    assert result.exit_code == 2
    assert "Invalid value" in result.output


@pytest.mark.parametrize("args", [
    ["--help"],
    ["ls", "--help"],
    ["exec", "--help"],
    ["login", "--help"],
    ["sftp", "--help"],
    ["config", "add", "--help"],
])
def test_help_runs(runner: CliRunner, args: list[str]) -> None:
    assert runner.invoke(cli, args).exit_code == 0


def test_exec_propagates_remote_exit_code(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner,
) -> None:
    """cmd_exec raises TerminalError(exit_code) → Click Exit with that code."""
    from contextlib import contextmanager

    from jms.exceptions import TerminalError

    srv = ServerConfig(
        name="prod", host="jump.example.com", username="alice", password="pw",
    )
    monkeypatch.setattr(
        "jms.cli._get_server", lambda config_path, server: srv,
    )

    @contextmanager
    def _fail_terminal(server, asset_name, account=None, protocol=None, backend="auto"):
        term = MagicMock()
        term.execute.side_effect = TerminalError(
            "status 42", exit_code=42, output="deploy failed!\nboom",
        )
        yield term

    monkeypatch.setattr("jms.cli._open_terminal", _fail_terminal)

    result = runner.invoke(cli, ["exec", "web@prod", "false"])
    assert result.exit_code == 42
    assert "deploy failed!" in result.stderr  # captured output relayed to stderr


def test_exec_timeout_prints_error_to_stderr(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner,
) -> None:
    """Timeout (no exit_code) → message echoed to stderr, exit 1."""
    from contextlib import contextmanager

    from jms.exceptions import TerminalError

    srv = ServerConfig(
        name="prod", host="jump.example.com", username="alice", password="pw",
    )
    monkeypatch.setattr(
        "jms.cli._get_server", lambda config_path, server: srv,
    )

    @contextmanager
    def _term(server, asset_name, account=None, protocol=None, backend="auto"):
        term = MagicMock()
        term.execute.side_effect = TerminalError("Command timed out after 1s")
        yield term

    monkeypatch.setattr("jms.cli._open_terminal", _term)

    result = runner.invoke(cli, ["exec", "web@prod", "sleep", "60"])
    assert result.exit_code == 1
    assert "timed out" in result.stderr


def test_exec_zero_exit_succeeds(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner,
) -> None:
    """cmd_exec with a successful remote command exits 0 and prints output."""
    from contextlib import contextmanager

    srv = ServerConfig(
        name="prod", host="jump.example.com", username="alice", password="pw",
    )
    monkeypatch.setattr(
        "jms.cli._get_server", lambda config_path, server: srv,
    )

    @contextmanager
    def _ok_terminal(server, asset_name, account=None, protocol=None, backend="auto"):
        term = MagicMock()
        term.execute.return_value = "hello"
        yield term

    monkeypatch.setattr("jms.cli._open_terminal", _ok_terminal)

    result = runner.invoke(cli, ["exec", "web@prod", "echo", "hello"])
    assert result.exit_code == 0
    assert "hello" in result.output


# ──── error paths (no config on disk) ───────────────────────────


def test_config_error_is_clean_by_default(runner: CliRunner) -> None:
    """JMSError renders as a one-line error + exit 1, never a traceback."""
    result = runner.invoke(cli, ["ls", "--config", "/nonexistent/config.yaml"])
    assert result.exit_code == 1
    assert "Error:" in result.output
    assert "Config not found" in result.output
    assert "Traceback" not in result.output


def test_debug_log_level_reraises(runner: CliRunner) -> None:
    """-l DEBUG lets the exception propagate for troubleshooting."""
    result = runner.invoke(
        cli, ["-l", "DEBUG", "ls", "--config", "/nonexistent/config.yaml"],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, ConfigError)


def test_sftp_rejects_two_local_paths(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["sftp", "./a.txt", "./b.txt"])
    assert result.exit_code == 1
    assert "remote path" in result.output


def test_exec_requires_command(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["exec", "my-host"])
    assert result.exit_code == 2


# ──── boundary-mocked behavior ──────────────────────────────────


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


class _FakeSession:
    """JMSSession stand-in: records the server, login() is a no-op."""

    def __init__(self, server: ServerConfig, otp_prompt: object = None) -> None:
        self.server = server

    def login(self, force: bool = False) -> None:
        pass


def test_ls_lists_assets(monkeypatch: pytest.MonkeyPatch, runner: CliRunner) -> None:
    monkeypatch.setattr("jms.cli.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr("jms.core.auth.JMSSession", _FakeSession)
    monkeypatch.setattr(
        "jms.core.resources.list_assets",
        lambda session, limit=50: [{
            "name": "web-01", "address": "10.0.0.1",
            "platform": {"name": "Linux"}, "type": {"label": "Host"},
        }],
    )
    result = runner.invoke(cli, ["ls"])
    assert result.exit_code == 0
    assert "web-01" in result.output
    assert "10.0.0.1" in result.output
    assert "Total: 1" in result.output


def test_ls_unknown_server_alias(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner,
) -> None:
    monkeypatch.setattr("jms.cli.load_config", lambda path=None: _fake_config())
    result = runner.invoke(cli, ["ls", "bogus"])
    assert result.exit_code == 1
    assert "Server 'bogus' not found" in result.output


def test_ls_no_assets(monkeypatch: pytest.MonkeyPatch, runner: CliRunner) -> None:
    monkeypatch.setattr("jms.cli.load_config", lambda path=None: _fake_config())
    monkeypatch.setattr("jms.core.auth.JMSSession", _FakeSession)
    monkeypatch.setattr("jms.core.resources.list_assets", lambda session, limit=50: [])
    result = runner.invoke(cli, ["ls"])
    assert result.exit_code == 0
    assert "No assets found." in result.output


# ──── config add ────────────────────────────────────────────────


def test_config_add_validates_then_saves(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, tmp_path: Path,
) -> None:
    monkeypatch.setattr("jms.core.auth.JMSSession", _FakeSession)
    cfg_path = tmp_path / "config.yaml"
    result = runner.invoke(
        cli,
        ["config", "add", "prod", "--config", str(cfg_path)],
        input="jms.example.com\nalice\npw\npw\n\n",
    )
    assert result.exit_code == 0, result.output
    assert "Credentials valid" in result.output
    cfg = load_config(str(cfg_path))
    assert "prod" in cfg.servers
    assert cfg.default == "prod"  # first server becomes the default
    assert cfg.servers["prod"].password == "pw"


def test_config_add_password_mismatch(runner: CliRunner) -> None:
    result = runner.invoke(
        cli,
        ["config", "add", "prod"],
        input="jms.example.com\nalice\npw1\npw2\n",
    )
    assert result.exit_code != 0
    assert "do not match" in result.output


def test_config_add_rejects_bad_credentials(
    monkeypatch: pytest.MonkeyPatch, runner: CliRunner, tmp_path: Path,
) -> None:
    class _BadSession(_FakeSession):
        def login(self, force: bool = False) -> None:
            raise AuthError("bad credentials")

    monkeypatch.setattr("jms.core.auth.JMSSession", _BadSession)
    cfg_path = tmp_path / "config.yaml"
    result = runner.invoke(
        cli,
        ["config", "add", "prod", "--config", str(cfg_path)],
        input="jms.example.com\nalice\npw\npw\n\n",
    )
    assert result.exit_code == 1
    assert "bad credentials" in result.output
    assert "Traceback" not in result.output
    assert not cfg_path.exists()  # nothing saved when validation fails


# ──── config list / remove / set-default ────────────────────────


def _seed_two_servers(cfg_path: Path) -> None:
    from jms.config import add_server

    add_server("prod", "p.example.com", "alice", "pw", config_path=str(cfg_path))
    add_server("dev", "d.example.com", "bob", "pw", config_path=str(cfg_path))


def test_config_list_marks_default(runner: CliRunner, tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    _seed_two_servers(cfg_path)
    result = runner.invoke(cli, ["config", "list", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    prod_row = next(r for r in result.output.splitlines() if r.startswith("prod"))
    dev_row = next(r for r in result.output.splitlines() if r.startswith("dev"))
    assert prod_row.endswith("*") and not dev_row.endswith("*")
    assert "alice" in prod_row and "p.example.com" in prod_row


def test_config_remove(runner: CliRunner, tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    _seed_two_servers(cfg_path)
    result = runner.invoke(
        cli, ["config", "remove", "dev", "-y", "--config", str(cfg_path)],
    )
    assert result.exit_code == 0, result.output
    assert "dev" not in load_config(str(cfg_path)).servers


def test_config_set_default(runner: CliRunner, tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yaml"
    _seed_two_servers(cfg_path)
    result = runner.invoke(
        cli, ["config", "set-default", "dev", "--config", str(cfg_path)],
    )
    assert result.exit_code == 0, result.output
    assert load_config(str(cfg_path)).default == "dev"


# ──── transfer verify/retry orchestration ────────────────────────


def _mk_file_task(src: str = "/s/f", dst: str = "/d/f") -> FileTask:
    return FileTask(
        src_path=src, dst_path=dst, start=0, end=10, total_size=10,
        chunk_index=0, total_chunks=1,
    )


def _mk_result(task: FileTask, md5: str) -> TaskResult:
    return TaskResult(task=task, bytes_done=10, md5=md5, verified=True)


def _ok_result(src: str, dst: str) -> FileVerifyResult:
    return FileVerifyResult(
        src_path=src, dst_path=dst, src_md5="a" * 32, dst_md5="a" * 32,
        ok=True, bad_tasks=(),
    )


def test_run_transfer_retries_bad_chunks_then_succeeds(
    monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    """md5 mismatch round 1 → bad chunk re-transmitted → round 2 ok."""
    from jms.io.service import run_transfer
    from jms.io.transfer import ChunkPolicy, ChunkSplitPolicy, FileInfo
    from jms.io.verify import FileVerifyResult

    task = _mk_file_task()
    calls: dict[str, int] = {"n": 0}

    def fake_execute(current_tasks, *_, **__):
        calls["n"] += 1
        return [_mk_result(t, "bad" if calls["n"] == 1 else "a" * 32) for t in current_tasks]

    def fake_verify(results, src_hasher, dst_hasher):
        if calls["n"] == 1:
            return [FileVerifyResult(
                src_path="/s/f", dst_path="/d/f", src_md5="a" * 32,
                dst_md5="b" * 32, ok=False, bad_tasks=(task,),
            )]
        return [_ok_result("/s/f", "/d/f")]

    monkeypatch.setattr("jms.io.transfer.execute_transfer", fake_execute)
    monkeypatch.setattr("jms.io.verify.verify_files", fake_verify)

    files = [FileInfo(src_path="/s/f", dst_path="/d/f", size=10)]
    run_transfer(
        files, src_factory=MagicMock(), dst_factory=MagicMock(), direction="upload",
        n_workers=1, policy=ChunkPolicy.FULL,
        split_policy=ChunkSplitPolicy.SEEK,
        src_hasher_factory=lambda: _null_cm(), dst_hasher_factory=lambda: _null_cm(),
        max_retries=2,
    )

    assert calls["n"] == 2  # initial transfer + retry round


def test_run_transfer_gives_up_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent mismatch → TransferError after max_retries."""
    from jms.exceptions import TransferError
    from jms.io.service import run_transfer
    from jms.io.transfer import ChunkPolicy, ChunkSplitPolicy, FileInfo
    from jms.io.verify import FileVerifyResult

    task = _mk_file_task()

    def fake_execute(current_tasks, *_, **__):
        return [_mk_result(t, "bad") for t in current_tasks]

    def fake_verify(results, src_hasher, dst_hasher):
        return [FileVerifyResult(
            src_path="/s/f", dst_path="/d/f", src_md5="a" * 32,
            dst_md5="b" * 32, ok=False, bad_tasks=(task,),
        )]

    monkeypatch.setattr("jms.io.transfer.execute_transfer", fake_execute)
    monkeypatch.setattr("jms.io.verify.verify_files", fake_verify)

    files = [FileInfo(src_path="/s/f", dst_path="/d/f", size=10)]
    with pytest.raises(TransferError, match="giving up"):
        run_transfer(
            files, src_factory=MagicMock(), dst_factory=MagicMock(), direction="upload",
            n_workers=1, policy=ChunkPolicy.FULL,
            split_policy=ChunkSplitPolicy.SEEK,
            src_hasher_factory=lambda: _null_cm(), dst_hasher_factory=lambda: _null_cm(),
            max_retries=1,
        )
