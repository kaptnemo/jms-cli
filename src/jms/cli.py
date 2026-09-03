"""Command-line interface for jms-cli.

SSH-style target syntax: ``<asset>[@<server>]`` — omit ``@<server>`` to use
the default server from the config.

Commands:
    jms config add <alias>
    jms ls [@server] [-q keyword]
    jms exec <asset>[@server] <cmd...>
    jms login <asset>[@server]
    jms sftp <src> <dst>          (one or both sides <asset>[@server]:<path>)
    jms ssh-pipe ...              (rsync/scp -e bridge; passes remote cmd through)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

import click

from jms import __version__
from jms.config import (
    ServerConfig,
    add_server,
    load_config,
    remove_server,
    set_default_server,
)
from jms.exceptions import AssetError, JMSError, TerminalError
from jms.log import logger, setup_logging

if TYPE_CHECKING:
    from jms.core.resources import AssetInfo
    from jms.core.auth import JMSSession
    from jms.transport import AbstractTerminal
    from jms.io.transfer import FileTask, OpenerFactory, TaskResult, TransferSpec


# ──── Target parsing ────────────────────────────────────────────


@dataclass(frozen=True)
class Target:
    """Parsed ``asset[@server]`` command-line target.

    Attributes:
        asset: Asset name or address.
        server: Server alias, or None for the default server.
    """

    asset: str
    server: str | None


def parse_target(spec: str) -> Target:
    """Parse ``asset[@server]`` into a Target.

    The last ``@`` is the separator; a leading or trailing ``@`` is
    treated as part of the asset name (no server given).

    Examples:
        >>> parse_target("10.0.0.1@prod")
        Target(asset='10.0.0.1', server='prod')
        >>> parse_target("my-host")
        Target(asset='my-host', server=None)
    """
    at = spec.rfind("@")
    if at == -1 or at == 0 or at == len(spec) - 1:
        return Target(asset=spec, server=None)
    return Target(asset=spec[:at], server=spec[at + 1:])


def default_otp_prompt() -> str:
    """Prompt interactively for an MFA verification code (CLI default callback).

    Wired into every ``JMSSession(otp_prompt=...)`` created by this module;
    sessions without a configured ``otp_secret`` use it when MFA triggers.

    Returns:
        The verification code entered by the user.
    """
    return click.prompt("MFA verification code", type=str, err=True)


# ──── Error rendering ───────────────────────────────────────────


class _JMSGroup(click.Group):
    """Click group that renders JMSError as a one-line stderr message.

    Library exceptions must never surface as bare tracebacks; ``-l DEBUG``
    re-enables them for troubleshooting.
    """

    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except JMSError as e:
            if logger.isEnabledFor(logging.DEBUG):
                raise
            click.echo(f"Error: {e}", err=True)
            ctx.exit(1)


# ──── Shared helpers ────────────────────────────────────────────


def _get_server(config_path: str | None, server: str | None) -> ServerConfig:
    """Load config and resolve the target server (default when ``server`` is None)."""
    cfg = load_config(config_path)
    if server:
        return cfg.get_server(server)
    return cfg.default_server


def _make_session(server: ServerConfig, force_login: bool = False) -> JMSSession:
    """Open an authenticated session with the interactive MFA prompt wired in."""
    from jms.core.auth import JMSSession

    session = JMSSession(server, otp_prompt=default_otp_prompt)
    session.login(force=force_login)
    return session


def _resolve(
    server: ServerConfig,
    asset_name: str,
    account: str | None = None,
    protocol: str | None = None,
) -> tuple[JMSSession, AssetInfo]:
    """Authenticate and resolve an asset. Returns ``(session, asset)``."""
    from jms.core.resources import resolve_asset

    session = _make_session(server)
    logger.info("Resolving asset '%s' ...", asset_name)
    try:
        asset = resolve_asset(session, asset_name, account=account, protocol=protocol)
    except AssetError:
        raise click.ClickException(
            f"Asset '{asset_name}' not found on {server.base_url}.\n"
            f"Use 'jms ls {server.name}' to list available assets."
        )
    logger.info(
        "Asset: %s (%s) account=%s protocol=%s",
        asset.name, asset.address, asset.account, asset.protocol,
    )
    return session, asset


@contextmanager
def _open_terminal(
    server: ServerConfig,
    asset_name: str,
    account: str | None = None,
    protocol: str | None = None,
    backend: str = "auto",
) -> Iterator[AbstractTerminal]:
    """Resolve an asset and yield a connected terminal.

    Args:
        server: Target server config.
        asset_name: Asset name or IP.
        account: Optional account override.
        protocol: Optional protocol override.
        backend: Backend selection: "ssh", "ws", or "auto" (default).
    """
    from jms.transport import BackendType, connect

    backend_type = {
        "ssh": BackendType.SSH,
        "ws": BackendType.WEBSOCKET,
    }.get(backend.lower(), BackendType.AUTO)

    session, asset = _resolve(server, asset_name, account, protocol)
    logger.info("Connecting (backend=%s) ...", backend_type.value)
    try:
        with connect(session, asset, backend=backend_type) as terminal:
            logger.info("Shell ready (backend=%s).", terminal.backend_name)
            yield terminal
    except TerminalError as e:
        raise click.ClickException(
            f"Failed to connect to '{asset_name}' on {server.base_url}: {e}"
        )


# ──── CLI definition ────────────────────────────────────────────


@click.group(cls=_JMSGroup)
@click.version_option(__version__, prog_name="jms")
@click.option(
    "--log-level", "-l",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"], case_sensitive=False),
    default="INFO",
    envvar="JMS_LOG_LEVEL",
    help="Log level (also via JMS_LOG_LEVEL env var).",
)
def cli(log_level: str) -> None:
    """jms — SSH-style access to JumpServer v4 bastion assets.

    \b
    Target syntax:
        <asset>@<server>   asset on a named server
        <asset>            asset on the default server

    \b
    Examples:
        jms exec 10.0.0.1@prod whoami
        jms login my-host
        jms sftp my-host@prod:/tmp/data.csv ./data.csv
        jms sftp ./report.pdf my-host:/reports/report.pdf
    """
    setup_logging(log_level)


# ──── config subcommands ────────────────────────────────────────


@cli.group("config")
def config_group() -> None:
    """Manage server configurations."""


@config_group.command("add")
@click.argument("alias")
@click.option("--set-default", is_flag=True, help="Set as the default server.")
@click.option("--config", "config_path", default=None, hidden=True)
def config_add(alias: str, set_default: bool, config_path: str | None) -> None:
    """Add or update a JumpServer configuration.

    Prompts for host, username, password (hidden) and an optional OTP
    secret, then validates the credentials against the server — full
    dual auth, including MFA — before saving anything. The first server
    added becomes the default automatically.
    """
    host = click.prompt("Host (e.g. jump.example.com or https://...)")
    username = click.prompt("Username")
    password = click.prompt("Password", hide_input=True, confirmation_prompt=True)
    otp_secret = click.prompt(
        "OTP secret (base32, empty to enter the code each time)",
        hide_input=True, default="", show_default=False,
    )

    probe = ServerConfig(
        name=alias, host=host, username=username,
        password=password, otp_secret=otp_secret,
    )
    logger.info("Validating credentials against %s ...", probe.base_url)
    _make_session(probe, force_login=True)  # raises AuthError on failure
    click.echo(f"[OK] Credentials valid for {probe.base_url}")

    path = add_server(
        name=alias, host=host, username=username,
        password=password, otp_secret=otp_secret,
        set_default=set_default, config_path=config_path,
    )
    click.echo(f"[OK] Server '{alias}' saved to {path}")
    if set_default:
        click.echo(f"[OK] '{alias}' set as default server.")


@config_group.command("list")
@click.option("--config", "config_path", default=None, hidden=True)
def config_list(config_path: str | None) -> None:
    """List configured JumpServer servers (default marked with *)."""
    cfg = load_config(config_path)
    click.echo(f"{'Alias':<20}{'Host':<36}{'Username':<20}Default")
    click.echo("-" * 78)
    for name, srv in cfg.servers.items():
        marker = "*" if name == cfg.default else ""
        click.echo(f"{name:<20}{srv.host:<36}{srv.username:<20}{marker}")
    click.echo(f"\nTotal: {len(cfg.servers)} server(s)")


@config_group.command("remove")
@click.argument("alias")
@click.option("--yes", "-y", is_flag=True, help="Do not ask for confirmation.")
@click.option("--config", "config_path", default=None, hidden=True)
def config_remove(alias: str, yes: bool, config_path: str | None) -> None:
    """Remove a configured server."""
    if not yes:
        click.confirm(f"Remove server '{alias}'?", abort=True)
    path = remove_server(alias, config_path)
    click.echo(f"[OK] Server '{alias}' removed ({path})")


@config_group.command("set-default")
@click.argument("alias")
@click.option("--config", "config_path", default=None, hidden=True)
def config_set_default(alias: str, config_path: str | None) -> None:
    """Set the default server used when @server is omitted."""
    set_default_server(alias, config_path)
    click.echo(f"[OK] '{alias}' set as default server.")


# ──── operational commands ──────────────────────────────────────


@cli.command("ls")
@click.argument("server", default=None, required=False)
@click.option("--config", "config_path", default=None, hidden=True)
@click.option("--search", "-q", default=None, help="Search keyword.")
@click.option("--limit", "-n", default=50, help="Max results.")
def cmd_ls(
    server: str | None, config_path: str | None, search: str | None, limit: int,
) -> None:
    """List available assets on a JumpServer.

    \b
    Examples:
        jms ls                   # default server
        jms ls prod              # specific server
        jms ls -q web            # search on default
        jms ls prod -q web       # search on 'prod'
    """
    from jms.core.resources import list_assets, search_assets

    srv = _get_server(config_path, server)
    session = _make_session(srv)
    assets = search_assets(session, search) if search else list_assets(session, limit=limit)

    if not assets:
        click.echo("No assets found.")
        return

    click.echo(f"\n{'Name':<35} {'Address':<20} {'Platform':<15} {'Type':<10}")
    click.echo("-" * 80)
    for a in assets:
        platform = a.get("platform", {})
        pname = platform.get("name", "") if isinstance(platform, dict) else str(platform)
        atype = a.get("type", "?")
        atype = atype.get("label", str(atype)) if isinstance(atype, dict) else str(atype)
        click.echo(
            f"{str(a.get('name', '?')):<35} {str(a.get('address', '?')):<20} "
            f"{pname:<15} {atype:<10}"
        )
    click.echo(f"\nTotal: {len(assets)} asset(s)")


@cli.command("exec", context_settings={"ignore_unknown_options": True})
@click.argument("target")
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@click.option("--config", "config_path", default=None, hidden=True)
@click.option("--account", default=None, help="Account override.")
@click.option("--protocol", default=None, help="Protocol override.")
@click.option("--timeout", "-t", default=30, help="Timeout seconds.")
@click.option(
    "--backend", "-b",
    type=click.Choice(["ssh", "ws", "auto"], case_sensitive=False),
    default="auto",
    help="Terminal backend: ssh, ws (WebSocket), or auto (default).",
)
def cmd_exec(
    target: str, command: tuple[str, ...], config_path: str | None,
    account: str | None, protocol: str | None, timeout: int, backend: str,
) -> None:
    """Execute a command on a remote asset.

    \b
    Examples:
        jms exec 10.0.0.1@prod whoami
        jms exec my-host ls -la /tmp
        jms exec my-host@prod 'echo hello world'
        jms exec -b ssh my-host whoami
    """
    t = parse_target(target)
    srv = _get_server(config_path, t.server)
    with _open_terminal(srv, t.asset, account, protocol, backend) as terminal:
        try:
            click.echo(terminal.execute(
                " ".join(command), timeout=timeout, check=True,
            ))
        except TerminalError as e:
            if e.output:
                click.echo(e.output, err=True)
            if e.exit_code is None:
                click.echo(f"Error: {e}", err=True)
            raise click.exceptions.Exit(e.exit_code or 1)


@cli.command("login")
@click.argument("target")
@click.option("--config", "config_path", default=None, hidden=True)
@click.option("--account", default=None, help="Account override.")
@click.option("--protocol", default=None, help="Protocol override.")
@click.option(
    "--backend", "-b",
    type=click.Choice(["ssh", "ws", "auto"], case_sensitive=False),
    default="auto",
    help="Terminal backend: ssh, ws (WebSocket), or auto (default).",
)
def cmd_login(
    target: str, config_path: str | None,
    account: str | None, protocol: str | None, backend: str,
) -> None:
    """Open an interactive terminal session (Ctrl+] to exit).

    \b
    Examples:
        jms login 10.0.0.1@prod
        jms login my-host
        jms login -b ssh my-host
    """
    t = parse_target(target)
    srv = _get_server(config_path, t.server)
    with _open_terminal(srv, t.asset, account, protocol, backend) as terminal:
        logger.info("Press Ctrl+] to exit.")
        click.echo()
        terminal.interactive()
    click.echo()
    logger.info("Session closed.")


# ──── sftp ──────────────────────────────────────────────────────


@cli.command("sftp")
@click.argument("src")
@click.argument("dst")
@click.option("--config", "config_path", default=None, hidden=True)
@click.option("--account", default=None, help="Account override.")
@click.option(
    "--parallel", "-j",
    default=4, type=int,
    help="Number of parallel workers (default: 4).",
)
@click.option(
    "--policy", "-p",
    type=click.Choice(["full", "files_only"], case_sensitive=False),
    default=None,
    help="Chunk policy: full (split large files) or files_only (no splitting).",
)
@click.option(
    "--split-policy",
    type=click.Choice(["seek", "split-files"], case_sensitive=False),
    default="seek",
    help=(
        "How chunks are written to a remote dst: 'seek' (default; workers "
        "share one dst handle in r+b and seek to their offset) or "
        "'split-files' (each chunk writes <dst>.partNN, merged via SSH "
        "'cat' at the end). Ignored for download (local pwrite is safe)."
    ),
)
@click.option(
    "--chroot",
    default="./",
    help=(
        "SFTP chroot location in SSH-exec terms. Some JumpServer / KoKo "
        "deployments chroot SFTP to HOME (use './' — the default) or to "
        "'/tmp'; SSH-exec sees the real filesystem so md5sum / cat "
        "commands need the translated path. Use '/' to disable "
        "translation. Applies to both sides of a relay transfer."
    ),
)
@click.option(
    "--no-verify",
    is_flag=True, default=False,
    help=(
        "Skip post-transfer md5 verification. Faster but bypasses the "
        "chunk-level retry safety net — only use when you verify the "
        "file out-of-band, or when the verify path itself is broken "
        "(md5sum missing on the remote, chroot mis-set, etc.)."
    ),
)
@click.option(
    "--recursive", "-R",
    is_flag=True, default=False,
    help="Recurse into directories.",
)
@click.option(
    "--skip-hidden",
    is_flag=True, default=False,
    help="Skip hidden files and directories (names starting with '.').",
)
@click.option(
    "--backend", "-b",
    type=click.Choice(["ssh", "ws", "http"], case_sensitive=False),
    default=None,
    envvar="JMS_TRANSFER_BACKEND",
    help=(
        "Transfer backend: ssh (default, native SFTP over KoKo:2222), "
        "ws (HTTP file transfer over /koko/ws/sftp/), "
        "or http (alias for ws). Also settable via JMS_TRANSFER_BACKEND."
    ),
)
def cmd_sftp(
    src: str, dst: str, config_path: str | None, account: str | None,
    parallel: int, policy: str | None, split_policy: str, chroot: str,
    no_verify: bool, recursive: bool, skip_hidden: bool, backend: str | None,
) -> None:
    """Transfer files between local and remote via SFTP or HTTP (ws).

    One or both of SRC/DST must be a remote spec (asset[@server]:path).
    Transfer direction is detected automatically.

    \b
    Download (remote → local):
        jms sftp my-host@prod:/tmp/data.csv ./data.csv

    \b
    Upload (local → remote):
        jms sftp ./report.pdf my-host:/reports/report.pdf

    \b
    Remote-to-remote (streaming relay, no local disk):
        jms sftp src-host@s1:/file dst-host@s2:/file

    \b
    Recursive directory transfer (skip hidden files):
        jms sftp -R --skip-hidden ./project/ host:/tmp/project/
    """
    from jms.core.auth import JMSSession
    from jms.io.service import relay_transfer, sftp_transfer
    from jms.io.transfer import RelaySpec, parse_transfer_spec

    spec = parse_transfer_spec(src, dst)
    backend_name = (backend or os.environ.get("JMS_TRANSFER_BACKEND") or "ssh").lower()
    kwargs = dict(
        account=account,
        n_workers=parallel,
        policy_name=policy,
        split_policy_name=split_policy,
        chroot=chroot,
        verify=not no_verify,
        recursive=recursive,
        skip_hidden=skip_hidden,
        backend=backend,
        session_factory=lambda srv: JMSSession(srv, otp_prompt=default_otp_prompt),
        on_status=click.echo,
    )
    if isinstance(spec, RelaySpec):
        relay_transfer(spec, config_path, **kwargs)
        return
    srv = _get_server(config_path, spec.server)
    if backend_name in ("ws", "http"):
        _run_ws_sftp(srv, spec, kwargs)
    else:
        kwargs["execute_hook"] = _run_transfer_with_progress
        sftp_transfer(srv, spec, **kwargs)


def _run_ws_sftp(
    srv: "ServerConfig",
    spec: "TransferSpec",
    kwargs: dict,
) -> None:
    """Run a ws-backend transfer under a rich progress bar (byte-increment)."""
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TransferSpeedColumn,
    )

    from jms.io.service import sftp_transfer

    direction = "upload" if spec.is_upload else "download"
    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
    ) as progress:
        task_id = progress.add_task(direction.capitalize(), total=None)
        kwargs = dict(kwargs, on_progress=lambda d: progress.update(task_id, advance=d))
        sftp_transfer(srv, spec, **kwargs)


def _run_transfer_with_progress(
    tasks: list[FileTask],
    src_factory: OpenerFactory,
    dst_factory: OpenerFactory,
    n_workers: int,
    direction: str,
) -> list[TaskResult]:
    """Execute transfer tasks under a rich progress bar (CLI execute_hook).

    Injected into ``jms.io.service.run_transfer``; rendering is a pure CLI
    concern, so it lives here rather than in the service layer.
    """
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    from jms.io.transfer import execute_transfer

    progress = Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )
    with progress:
        file_tasks: dict[str, int] = {}
        file_progress: dict[str, int] = {}
        lock = threading.Lock()

        for task in tasks:
            if task.src_path not in file_tasks:
                file_tasks[task.src_path] = progress.add_task(
                    f"{direction.capitalize()} {task.filename}",
                    total=task.total_size,
                )

        def cb(task: FileTask, bytes_done: int) -> None:
            key = task.src_path
            with lock:
                # bytes_done may be negative on spot-check rewind
                chunk_key = f"{key}:{task.chunk_index}"
                prev = file_progress.get(chunk_key, 0)
                file_progress[chunk_key] = max(
                    0, prev + bytes_done if bytes_done < 0 else bytes_done,
                )
                total_done = sum(
                    v for k, v in file_progress.items()
                    if k.startswith(f"{key}:")
                )
                progress.update(file_tasks[key], completed=total_done)

        return execute_transfer(
            tasks, src_factory, dst_factory,
            n_workers=n_workers, callback=cb,
        )


# ──── ssh-pipe (rsync/scp -e bridge) ─────────────────────────────


@cli.command(
    "ssh-pipe",
    context_settings={
        # the remote command carries its own flags (rsync --server ...)
        "ignore_unknown_options": True,
        "allow_interspersed_args": False,
    },
)
@click.option("-l", "user", default=None, hidden=True,
              help="Asset name (classic rsync passes it as -l <user>).")
@click.option("--config", "config_path", default=None, hidden=True)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def ssh_pipe_cmd(
    ctx: click.Context, user: str | None, config_path: str | None, args: tuple[str, ...],
) -> None:
    """Stdio bridge used as rsync/scp's -e transport.

    rsync invokes it in one of two forms (target = <asset>@<server>,
    where asset is a JumpServer asset and server a config.yaml alias):

    \b
        classic rsync:  jms ssh-pipe -l <asset> <server> <remote_cmd...>
        openrsync:      jms ssh-pipe <asset>@<server> <remote_cmd...>

    \b
    Example:
        rsync -avz -e 'jms ssh-pipe' ./local/ my-asset@my-server:/data/
    """
    from jms.io.ssh_pipe import run_bridge

    if user is not None:  # classic rsync: -l asset server cmd...
        asset_name, rest = user, list(args)
        if not rest:
            raise click.UsageError("missing <server> after -l <asset>")
        server_alias, cmd = rest[0], rest[1:]
    elif args and "@" in args[0]:  # openrsync: asset@server cmd...
        asset_name, server_alias = args[0].rsplit("@", 1)
        cmd = list(args[1:])
    else:
        raise click.UsageError(
            "expected '-l <asset> <server>' or '<asset>@<server>' from rsync"
        )
    if not cmd:
        raise click.UsageError("no remote command in args")

    try:
        code = run_bridge(asset_name, server_alias, " ".join(cmd), config_path)
    except Exception as e:  # never leak a traceback into rsync's stdio
        sys.stderr.write(f"jms ssh-pipe: fatal: {e}\n")
        code = 1
    ctx.exit(code or 0)


# ──── mcp (local stdio MCP server) ──────────────────────────────


@cli.command("mcp")
@click.option("--config", "config_path", default=None, hidden=True)
def cmd_mcp(config_path: str | None) -> None:
    """Start a local MCP stdio server exposing jms tools to AI assistants."""
    from jms.mcp.server import main as mcp_main

    mcp_main(config_path)


# ──── entry point ───────────────────────────────────────────────


def main() -> None:
    """Entry point: everything goes through Click, ssh-pipe included."""
    cli()


if __name__ == "__main__":
    main()
