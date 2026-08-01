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
import shlex
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ContextManager, Iterator, Union

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
    from jms.assets import AssetInfo
    from jms.auth import JMSSession
    from jms.backend import AbstractTerminal
    from jms.transfer import (
        ChunkPolicy,
        ChunkSplitPolicy,
        FileInfo,
        FileTask,
        OpenerFactory,
        RelaySpec,
        TaskResult,
        TransferSpec,
    )
    from jms.verify import LocalHasher, RemoteHasher

    # Zero-arg callable returning a context manager that yields a hasher.
    HasherFactory = Callable[[], ContextManager[Union[RemoteHasher, LocalHasher]]]


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


def _make_session(server: ServerConfig) -> JMSSession:
    """Open an authenticated session with the interactive MFA prompt wired in."""
    from jms.auth import JMSSession

    session = JMSSession(server, otp_prompt=default_otp_prompt)
    session.login()
    return session


def _resolve(
    server: ServerConfig,
    asset_name: str,
    account: str | None = None,
    protocol: str | None = None,
) -> tuple[JMSSession, AssetInfo]:
    """Authenticate and resolve an asset. Returns ``(session, asset)``."""
    from jms.assets import resolve_asset

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


def _resolve_asset(
    session: JMSSession, asset_name: str, account: str | None, side: str,
) -> AssetInfo:
    """Resolve an asset on an existing session, with a click-friendly error."""
    from jms.assets import resolve_asset

    logger.info("Resolving %s asset '%s' ...", side.lower(), asset_name)
    try:
        return resolve_asset(session, asset_name, account=account)
    except AssetError:
        raise click.ClickException(f"{side} asset '{asset_name}' not found.")


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
    from jms.backend import BackendType, connect

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
    _make_session(probe)  # raises AuthError on failure — nothing is saved
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
    from jms.assets import list_assets, search_assets

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
def cmd_sftp(
    src: str, dst: str, config_path: str | None, account: str | None,
    parallel: int, policy: str | None, split_policy: str, chroot: str,
    no_verify: bool, recursive: bool, skip_hidden: bool,
) -> None:
    """Transfer files between local and remote via SFTP.

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
    from jms.transfer import RelaySpec, parse_transfer_spec

    spec = parse_transfer_spec(src, dst)
    if isinstance(spec, RelaySpec):
        _do_sftp_r2r(
            spec, config_path, account, parallel, policy,
            split_policy, chroot, no_verify, recursive, skip_hidden,
        )
    else:
        srv = _get_server(config_path, spec.server)
        _do_sftp_transfer(
            srv, spec, account, parallel, policy,
            split_policy, chroot, no_verify, recursive, skip_hidden,
        )


def _do_sftp_transfer(
    server: ServerConfig,
    spec: TransferSpec,
    account: str | None,
    n_workers: int,
    policy_name: str | None,
    split_policy_name: str,
    chroot: str,
    no_verify: bool,
    recursive: bool,
    skip_hidden: bool,
) -> None:
    """Execute an SFTP upload or download (exactly one remote side)."""
    from jms.transfer import (
        ChunkPolicy,
        ChunkSplitPolicy,
        LocalOpenerFactory,
        SFTPOpenerFactory,
        connect_sftp,
        list_local_files,
        list_remote_files,
        resolve_local_dst,
        resolve_remote_dst,
    )

    policy = ChunkPolicy(policy_name.lower()) if policy_name else ChunkPolicy.FULL
    split_policy = ChunkSplitPolicy(split_policy_name.lower())
    session, asset = _resolve(server, spec.asset, account)

    if spec.is_upload:
        src_path = Path(spec.local_path)
        if src_path.is_dir() and not recursive:
            raise click.ClickException(
                f"'{spec.local_path}' is a directory. Use -R to transfer recursively."
            )
        files = list_local_files(
            spec.local_path, recursive=recursive, skip_hidden=skip_hidden,
        )
        with connect_sftp(session, asset) as sftp:
            if src_path.is_dir():
                base = str(src_path)
                files = [
                    replace(
                        f,
                        dst_path=(
                            f"{spec.remote_path.rstrip('/')}/"
                            f"{os.path.relpath(f.src_path, base)}"
                        ),
                    )
                    for f in files
                ]
            else:
                # cp semantics: existing remote dir gets the basename inside
                dst_file = resolve_remote_dst(sftp, spec.remote_path, src_path.name)
                files = [replace(f, dst_path=dst_file) for f in files]
            _run_transfer(
                files, LocalOpenerFactory(), SFTPOpenerFactory(sftp),
                "upload", n_workers, policy, split_policy,
                src_hasher_factory=None if no_verify else _local_hasher_cm,
                dst_hasher_factory=(
                    None if no_verify
                    else lambda: _ssh_hasher_cm(session, asset, chroot=chroot)
                ),
                merge_session=session,
                merge_asset=asset,
                merge_chroot=chroot,
            )
    else:
        with connect_sftp(session, asset) as sftp:
            try:
                src_info = sftp.stat(spec.remote_path)
            except Exception:
                raise click.ClickException(
                    f"Remote path not found: {spec.remote_path}"
                )
            if src_info["is_dir"] and not recursive:
                raise click.ClickException(
                    f"'{spec.remote_path}' is a directory. "
                    f"Use -R to transfer recursively."
                )
            files = list_remote_files(
                sftp, spec.remote_path, recursive=recursive, skip_hidden=skip_hidden,
            )
            if src_info["is_dir"]:
                base = spec.remote_path.rstrip("/")
                files = [
                    replace(
                        f,
                        dst_path=str(
                            Path(spec.local_path) / _relative(base, f.src_path)
                        ),
                    )
                    for f in files
                ]
            else:
                files = [
                    replace(
                        f,
                        dst_path=resolve_local_dst(
                            spec.local_path, Path(f.src_path).name,
                        ),
                    )
                    for f in files
                ]
            # Local dst: POSIX pwrite on a local fd is always safe, so the
            # split-files policy is meaningless on download — force SEEK.
            _run_transfer(
                files, SFTPOpenerFactory(sftp), LocalOpenerFactory(),
                "download", n_workers, policy, ChunkSplitPolicy.SEEK,
                src_hasher_factory=(
                    None if no_verify
                    else lambda: _ssh_hasher_cm(session, asset, chroot=chroot)
                ),
                dst_hasher_factory=None if no_verify else _local_hasher_cm,
            )


def _do_sftp_r2r(
    spec: RelaySpec,
    config_path: str | None,
    account: str | None,
    n_workers: int,
    policy_name: str | None,
    split_policy_name: str,
    chroot: str,
    no_verify: bool,
    recursive: bool,
    skip_hidden: bool,
) -> None:
    """Execute a remote-to-remote transfer (streamed relay, no local disk)."""
    from jms.transfer import (
        ChunkPolicy,
        ChunkSplitPolicy,
        SFTPOpenerFactory,
        connect_sftp,
        list_remote_files,
        resolve_remote_dst,
    )

    policy = ChunkPolicy(policy_name.lower()) if policy_name else ChunkPolicy.FULL
    split_policy = ChunkSplitPolicy(split_policy_name.lower())

    src_session = _make_session(_get_server(config_path, spec.src_server))
    src_asset = _resolve_asset(src_session, spec.src_asset, account, "Source")
    dst_session = _make_session(_get_server(config_path, spec.dst_server))
    dst_asset = _resolve_asset(dst_session, spec.dst_asset, account, "Destination")

    with connect_sftp(src_session, src_asset) as src_sftp:
        try:
            src_info = src_sftp.stat(spec.src_path)
        except Exception:
            raise click.ClickException(f"Source path not found: {spec.src_path}")
        if src_info["is_dir"] and not recursive:
            raise click.ClickException(
                f"'{spec.src_path}' is a directory. Use -R to transfer recursively."
            )
        files = list_remote_files(
            src_sftp, spec.src_path, recursive=recursive, skip_hidden=skip_hidden,
        )
        with connect_sftp(dst_session, dst_asset) as dst_sftp:
            if src_info["is_dir"]:
                base = spec.src_path.rstrip("/")
                files = [
                    replace(
                        f,
                        dst_path=(
                            f"{spec.dst_path.rstrip('/')}/"
                            f"{_relative(base, f.src_path)}"
                        ),
                    )
                    for f in files
                ]
            else:
                # cp semantics: existing dst dir gets the basename inside
                dst_file = resolve_remote_dst(
                    dst_sftp, spec.dst_path, Path(spec.src_path).name,
                )
                files = [replace(f, dst_path=dst_file) for f in files]
            _run_transfer(
                files,
                SFTPOpenerFactory(src_sftp), SFTPOpenerFactory(dst_sftp),
                "relay", n_workers, policy, split_policy,
                src_hasher_factory=(
                    None if no_verify
                    else lambda: _ssh_hasher_cm(src_session, src_asset, chroot=chroot)
                ),
                dst_hasher_factory=(
                    None if no_verify
                    else lambda: _ssh_hasher_cm(dst_session, dst_asset, chroot=chroot)
                ),
                merge_session=dst_session,
                merge_asset=dst_asset,
                merge_chroot=chroot,
            )


def _relative(base: str, path: str) -> str:
    """Strip the ``base/`` prefix from a remote path (basename as fallback)."""
    if path.startswith(base + "/"):
        return path[len(base) + 1:]
    return Path(path).name


@contextmanager
def _ssh_hasher_cm(
    session: JMSSession, asset: AssetInfo, chroot: str = "/",
) -> Iterator[RemoteHasher]:
    """Yield a RemoteHasher bound to a throwaway SSH-exec terminal."""
    from jms.backend import connect_ssh
    from jms.verify import RemoteHasher

    with connect_ssh(session, asset) as term:
        yield RemoteHasher(term, chroot=chroot)


@contextmanager
def _local_hasher_cm() -> Iterator[LocalHasher]:
    """Yield a LocalHasher (context-managed for symmetry with _ssh_hasher_cm)."""
    from jms.verify import LocalHasher

    yield LocalHasher()


def _merge_parts_via_ssh(
    session: JMSSession,
    asset: AssetInfo,
    parts_by_target: dict[str, list[TaskResult]],
    cleanup: bool = False,
    chroot: str = "/",
) -> None:
    """Assemble ``.partNN`` files into their merge target via SSH ``cat``.

    Success is verified by a sentinel echo plus a byte-size check: the
    merged file's size must equal the sum of the part sizes.

    Args:
        session: Authenticated session for the dst host.
        asset: Resolved asset on the dst host.
        parts_by_target: Output of ``group_parts_by_merge_target``.
        cleanup: Remove the part files after a successful merge.
        chroot: SFTP chroot in SSH-exec terms (see
            ``verify.translate_remote_path``). Part files and the merge
            target are SFTP-side paths and must be translated before the
            ``cat`` / ``rm`` shell command runs.

    Raises:
        click.ClickException: If the remote merge fails for any target.
    """
    if not parts_by_target:
        return
    from jms.backend import connect_ssh
    from jms.verify import translate_remote_path

    with connect_ssh(session, asset) as term:
        for target, parts in parts_by_target.items():
            part_paths = [translate_remote_path(chroot, r.task.dst_path) for r in parts]
            quoted_parts = " ".join(shlex.quote(p) for p in part_paths)
            quoted_target = shlex.quote(translate_remote_path(chroot, target))
            expected_size = sum(r.task.chunk_size for r in parts)
            cmd = f"cat {quoted_parts} > {quoted_target} && stat -c %s {quoted_target}"
            if cleanup:
                cmd += f" && rm -f {quoted_parts}"
            cmd += " && echo __MERGE_OK__"
            logger.info(
                "merging %d part(s) into %s%s ...",
                len(parts), target, " (+ cleanup)" if cleanup else "",
            )
            try:
                out = term.execute(cmd, timeout=3600)
            except Exception as e:
                raise click.ClickException(f"merge SSH exec failed for {target}: {e}")
            if "__MERGE_OK__" not in out:
                raise click.ClickException(f"merge failed for {target}: {out[:500]}")
            lines = [ln for ln in out.splitlines() if ln.strip()]
            try:
                actual_size = int(lines[-2])
            except (IndexError, ValueError):
                raise click.ClickException(
                    f"merge size check failed for {target}: "
                    f"could not parse size from {out[:500]}"
                )
            if actual_size != expected_size:
                raise click.ClickException(
                    f"merge size mismatch for {target}: "
                    f"expected {expected_size}, got {actual_size}"
                )
            logger.info(
                "merged %s: %d bytes from %d part(s)",
                target, actual_size, len(parts),
            )


def _report_spot_check_failures(results: list[TaskResult]) -> None:
    """Warn about chunks whose inline spot check exhausted all retries.

    Not fatal by itself — the md5 verify pass catches and retries any
    real corruption — but the user deserves to see that the guard tripped.
    """
    for r in results:
        if not r.verified:
            click.echo(
                f"[WARNING] chunk {r.task.chunk_index} of "
                f"{r.task.merge_to or r.task.dst_path}: inline spot check "
                f"failed all {r.attempts} attempt(s); relying on md5 "
                f"verify pass to catch / retry"
            )


def _run_transfer(
    files: list[FileInfo],
    src_factory: OpenerFactory,
    dst_factory: OpenerFactory,
    direction: str,
    n_workers: int,
    policy: ChunkPolicy,
    split_policy: ChunkSplitPolicy,
    src_hasher_factory: HasherFactory | None = None,
    dst_hasher_factory: HasherFactory | None = None,
    merge_session: JMSSession | None = None,
    merge_asset: AssetInfo | None = None,
    merge_chroot: str = "/",
    max_retries: int = 3,
) -> None:
    """Run a transfer with rich progress, md5 verification, and chunk retry.

    The hasher factories are invoked once per verification round (after
    each transfer attempt); both None means verification is skipped. For
    ``split_policy=SPLIT_FILES`` on a remote dst, ``merge_session`` +
    ``merge_asset`` must be supplied so part files can be assembled via
    SSH ``cat`` after each transfer round.
    """
    from jms.transfer import (
        ChunkSplitPolicy,
        execute_transfer,
        group_parts_by_merge_target,
        plan_transfer,
    )

    if not files:
        click.echo("No files to transfer.")
        return

    tasks = plan_transfer(
        files, n_workers=n_workers, policy=policy, split_policy=split_policy,
    )
    total_bytes = sum(f.size for f in files)
    total_files = len(files)

    logger.info(
        "%s: %d file(s), %d task(s), %d worker(s), %d bytes, policy=%s, split=%s",
        direction, total_files, len(tasks), n_workers, total_bytes,
        policy.value, split_policy.value,
    )

    def _execute(current_tasks: list[FileTask]) -> list[TaskResult]:
        from rich.progress import (
            BarColumn,
            DownloadColumn,
            Progress,
            TextColumn,
            TimeRemainingColumn,
            TransferSpeedColumn,
        )

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

            for task in current_tasks:
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
                current_tasks, src_factory, dst_factory,
                n_workers=n_workers, callback=cb,
            )

    def _maybe_merge(current_results: list[TaskResult], cleanup: bool = False) -> None:
        if split_policy != ChunkSplitPolicy.SPLIT_FILES:
            return
        parts = group_parts_by_merge_target(current_results)
        if not parts:
            return
        if merge_session is None or merge_asset is None:
            raise click.ClickException(
                "split-files policy requires a remote dst with SSH exec "
                "access; merge_session/asset not supplied"
            )
        _merge_parts_via_ssh(
            merge_session, merge_asset, parts, cleanup=cleanup, chroot=merge_chroot,
        )

    results = _execute(tasks)
    _report_spot_check_failures(results)
    _maybe_merge(results, cleanup=False)
    total = sum(r.bytes_done for r in results)

    if src_hasher_factory is not None and dst_hasher_factory is not None:
        from jms.verify import verify_files

        for attempt in range(1, max_retries + 2):
            click.echo(f"[verify] round {attempt}: computing md5sum on both sides ...")
            with src_hasher_factory() as src_hasher, \
                    dst_hasher_factory() as dst_hasher:
                file_results = verify_files(results, src_hasher, dst_hasher)

            all_ok = all(fr.ok for fr in file_results)
            for fr in file_results:
                if fr.ok:
                    click.echo(f"[OK] md5 match: {fr.dst_path} ({fr.src_md5})")
                else:
                    click.echo(
                        f"[FAIL] md5 mismatch: {fr.dst_path} "
                        f"(src={fr.src_md5 or '?'}, dst={fr.dst_md5 or '?'}, "
                        f"bad_chunks={len(fr.bad_tasks)})"
                    )
            if all_ok:
                # Final cleanup of part files on successful verify.
                _maybe_merge(results, cleanup=True)
                break

            bad_tasks = [t for fr in file_results for t in fr.bad_tasks]
            if not bad_tasks:
                raise click.ClickException(
                    "md5 mismatch but no chunk identified as corrupt; the "
                    "streamed bytes may not match the real source file. "
                    "Refusing to retry blindly."
                )
            if attempt > max_retries:
                raise click.ClickException(
                    f"md5 mismatch after {max_retries} retries; giving up. "
                    f"{len(bad_tasks)} chunk(s) still bad."
                )

            click.echo(f"[retry] re-transmitting {len(bad_tasks)} corrupt chunk(s) ...")
            new_results = _execute(bad_tasks)
            _report_spot_check_failures(new_results)
            # Replace old results for bad tasks with new ones.
            new_by_id = {id(r.task): r for r in new_results}
            results = [new_by_id.get(id(r.task), r) for r in results]
            # Re-merge after retry (idempotent overwrite).
            _maybe_merge(results, cleanup=False)
            total = sum(r.bytes_done for r in results)

    click.echo(
        f"[OK] {direction.capitalize()} complete: {total} bytes "
        f"({total_files} file(s), {n_workers} workers)."
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
    from jms.ssh_pipe import run_bridge

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


# ──── entry point ───────────────────────────────────────────────


def main() -> None:
    """Entry point: everything goes through Click, ssh-pipe included."""
    cli()


if __name__ == "__main__":
    main()
