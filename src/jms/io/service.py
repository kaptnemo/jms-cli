"""Transfer orchestration layer: high-level workflows usable from CLI or as a library API.

Bridges the transfer engine (``jms.io.transfer``) and verification
(``jms.io.verify``) into end-to-end SFTP workflows — upload, download, and
remote-to-remote relay — with chunk-level retry and md5 verification.

This module has zero CLI concerns: no CLI framework, no rich, no sys,
no interactive input. Progress rendering and status output are injected
by the caller:

- ``execute_hook``: a wrapper around ``execute_transfer`` that renders
  progress (the CLI injects a rich-progress one); None runs the plain
  transfer with no progress bar.
- ``on_status``: a callback for one-line status messages; None logs them
  via the module logger.

Exceptions are jms exceptions only (``TransferError`` etc.); the CLI
layer is responsible for rendering them to the user.
"""

from __future__ import annotations

import os
import posixpath
import shlex
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, ContextManager, Iterator, Union

from jms.core.resources import AssetInfo
from jms.core.auth import JMSSession
from jms.config import ServerConfig, load_config
from jms.exceptions import AssetError, TransferError
from jms.log import logger
from jms.io.transfer import (
    ChunkPolicy,
    ChunkSplitPolicy,
    FileInfo,
    FileTask,
    LocalOpenerFactory,
    OpenerFactory,
    RelaySpec,
    SFTPOpenerFactory,
    TaskResult,
    TransferSpec,
    connect_sftp,
    connect_ws_sftp,
    list_local_files,
    list_remote_files,
    resolve_local_dst,
    resolve_remote_dst,
)
from jms.io.transfer.ws import WSFileClient
from jms.io.verify import LocalHasher, RemoteHasher

# Zero-arg callable returning a context manager that yields a hasher.
HasherFactory = Callable[[], ContextManager[Union[RemoteHasher, LocalHasher]]]

# Creates a session for a server; None means JMSSession(server) with the
# default (no interactive MFA prompt -> MFA raises MFARequired).
SessionFactory = Callable[[ServerConfig], JMSSession]

# CLI-injected progress wrapper around execute_transfer.
ExecuteHook = Callable[[list[FileTask], OpenerFactory, OpenerFactory, int, str], list[TaskResult]]

# CLI-injected one-line status sink (e.g. a terminal echo).
StatusHook = Callable[[str], None]

# CLI-injected progress sink for the WebSocket transfer backend: receives the
# number of bytes just transferred (increments).
WSProgressHook = Callable[[int], None]


def _emit(message: str, on_status: StatusHook | None) -> None:
    """Route a one-line status message to ``on_status`` or the logger."""
    if on_status is not None:
        on_status(message)
    else:
        logger.info(message)


# Backends accepted by the transfer layer. ``http`` is a legacy alias for
# ``ws``: the only viable API path is the WebSocket endpoint (the elFinder HTTP
# connector needs a companion ``/koko/ws/elfinder`` session and cannot run
# standalone). ``ssh`` is the default (native SFTP over KoKo's port 2222).
_TRANSFER_BACKENDS: frozenset[str] = frozenset({"ssh", "ws", "http"})


def resolve_backend(backend: str | None = None) -> str:
    """Resolve the transfer backend name from an explicit value, then the
    ``JMS_TRANSFER_BACKEND`` env var, then ``"ssh"``. ``http`` maps to ``ws``.

    Args:
        backend: Explicit backend name, or None to consult the environment.

    Returns:
        Canonical backend name (``"ssh"`` or ``"ws"``).

    Raises:
        TransferError: The resolved name is not a known backend.
    """
    name = (backend or os.environ.get("JMS_TRANSFER_BACKEND") or "ssh").strip().lower()
    if name == "http":
        name = "ws"
    if name not in _TRANSFER_BACKENDS:
        raise TransferError(
            f"Unknown transfer backend: {name!r} "
            f"(expected one of ssh, ws, http)"
        )
    return name


def _make_session(
    server: ServerConfig,
    session_factory: SessionFactory | None,
) -> JMSSession:
    """Create a session (via factory if given) and log in.

    Args:
        server: Target server config.
        session_factory: Injectable session creator; None uses
            ``JMSSession(server)`` with no interactive MFA prompt.

    Returns:
        An authenticated session.
    """
    session = session_factory(server) if session_factory else JMSSession(server)
    session.login()
    return session


def _resolve(
    server: ServerConfig,
    asset_name: str,
    account: str | None,
    session_factory: SessionFactory | None,
) -> tuple[JMSSession, AssetInfo]:
    """Authenticate and resolve an asset. Returns ``(session, asset)``.

    Args:
        server: Target server config.
        asset_name: Asset name or address.
        account: Optional account override.
        session_factory: Injectable session creator (see ``_make_session``).

    Returns:
        ``(session, asset)``.

    Raises:
        TransferError: If the asset cannot be found.
    """
    from jms.core.resources import resolve_asset

    session = _make_session(server, session_factory)
    logger.info("Resolving asset '%s' ...", asset_name)
    try:
        asset = resolve_asset(session, asset_name, account=account)
    except AssetError:
        raise TransferError(f"Asset '{asset_name}' not found on {server.base_url}")
    logger.info(
        "Asset: %s (%s) account=%s protocol=%s",
        asset.name, asset.address, asset.account, asset.protocol,
    )
    return session, asset


@contextmanager
def _ssh_hasher_cm(
    session: JMSSession, asset: AssetInfo, chroot: str = "/",
) -> Iterator[RemoteHasher]:
    """Yield a RemoteHasher bound to a throwaway SSH-exec terminal."""
    from jms.transport import connect_ssh

    with connect_ssh(session, asset) as term:
        yield RemoteHasher(term, chroot=chroot)


@contextmanager
def _local_hasher_cm() -> Iterator[LocalHasher]:
    """Yield a LocalHasher (context-managed for symmetry with _ssh_hasher_cm)."""
    yield LocalHasher()


def _relative(base: str, path: str) -> str:
    """Strip the ``base/`` prefix from a remote path (basename as fallback)."""
    if path.startswith(base + "/"):
        return path[len(base) + 1:]
    return Path(path).name


def merge_parts_via_ssh(
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
        TransferError: If the remote merge fails for any target.
    """
    if not parts_by_target:
        return
    from jms.transport import connect_ssh
    from jms.io.verify import translate_remote_path

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
                raise TransferError(f"merge SSH exec failed for {target}: {e}")
            if "__MERGE_OK__" not in out:
                raise TransferError(f"merge failed for {target}: {out[:500]}")
            lines = [ln for ln in out.splitlines() if ln.strip()]
            try:
                actual_size = int(lines[-2])
            except (IndexError, ValueError):
                raise TransferError(
                    f"merge size check failed for {target}: "
                    f"could not parse size from {out[:500]}"
                )
            if actual_size != expected_size:
                raise TransferError(
                    f"merge size mismatch for {target}: "
                    f"expected {expected_size}, got {actual_size}"
                )
            logger.info(
                "merged %s: %d bytes from %d part(s)",
                target, actual_size, len(parts),
            )


def report_spot_check_failures(results: list[TaskResult]) -> None:
    """Warn about chunks whose inline spot check exhausted all retries.

    Not fatal by itself — the md5 verify pass catches and retries any
    real corruption — but the user deserves to see that the guard tripped.

    Args:
        results: Output of ``execute_transfer()``.
    """
    for r in results:
        if not r.verified:
            logger.warning(
                "[WARNING] chunk %s of %s: inline spot check "
                "failed all %s attempt(s); relying on md5 "
                "verify pass to catch / retry",
                r.task.chunk_index,
                r.task.merge_to or r.task.dst_path,
                r.attempts,
            )


def run_transfer(
    files: list[FileInfo],
    src_factory: OpenerFactory,
    dst_factory: OpenerFactory,
    direction: str,
    *,
    n_workers: int = 4,
    policy: ChunkPolicy = ChunkPolicy.FULL,
    split_policy: ChunkSplitPolicy = ChunkSplitPolicy.SEEK,
    src_hasher_factory: HasherFactory | None = None,
    dst_hasher_factory: HasherFactory | None = None,
    merge_session: JMSSession | None = None,
    merge_asset: AssetInfo | None = None,
    merge_chroot: str = "/",
    max_retries: int = 3,
    execute_hook: ExecuteHook | None = None,
    on_status: StatusHook | None = None,
) -> None:
    """Run a transfer with md5 verification and chunk retry.

    The hasher factories are invoked once per verification round (after
    each transfer attempt); both None means verification is skipped. For
    ``split_policy=SPLIT_FILES`` on a remote dst, ``merge_session`` +
    ``merge_asset`` must be supplied so part files can be assembled via
    SSH ``cat`` after each transfer round.

    Args:
        files: Files to transfer.
        src_factory: Factory for source file openers.
        dst_factory: Factory for destination file openers.
        direction: Human-readable direction ("upload" / "download" /
            "relay").
        n_workers: Max parallel workers.
        policy: Chunking policy.
        split_policy: How chunk writes land on the dst.
        src_hasher_factory: Optional hasher for the src side; must be
            paired with ``dst_hasher_factory`` to enable verification.
        dst_hasher_factory: Optional hasher for the dst side.
        merge_session: Session for SSH ``cat`` merge (SPLIT_FILES only).
        merge_asset: Asset for SSH ``cat`` merge (SPLIT_FILES only).
        merge_chroot: SFTP chroot for the merge SSH exec.
        max_retries: Max re-transmit rounds after a failed verify.
        execute_hook: Injectable transfer runner (e.g. rich-progress
            wrapper); None calls ``execute_transfer`` directly.
        on_status: Injectable one-line status sink; None logs.

    Raises:
        TransferError: If verification cannot be satisfied.
    """
    from jms.io.transfer import (
        execute_transfer,
        group_parts_by_merge_target,
        plan_transfer,
    )

    if not files:
        _emit("No files to transfer.", on_status)
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
        if execute_hook is not None:
            return execute_hook(
                current_tasks, src_factory, dst_factory, n_workers, direction,
            )
        return execute_transfer(
            current_tasks, src_factory, dst_factory, n_workers=n_workers,
        )

    def _maybe_merge(current_results: list[TaskResult], cleanup: bool = False) -> None:
        if split_policy != ChunkSplitPolicy.SPLIT_FILES:
            return
        parts = group_parts_by_merge_target(current_results)
        if not parts:
            return
        if merge_session is None or merge_asset is None:
            raise TransferError(
                "split-files policy requires a remote dst with SSH exec "
                "access; merge_session/asset not supplied"
            )
        merge_parts_via_ssh(
            merge_session, merge_asset, parts, cleanup=cleanup, chroot=merge_chroot,
        )

    results = _execute(tasks)
    report_spot_check_failures(results)
    _maybe_merge(results, cleanup=False)
    total = sum(r.bytes_done for r in results)

    if src_hasher_factory is not None and dst_hasher_factory is not None:
        from jms.io.verify import verify_files

        for attempt in range(1, max_retries + 2):
            _emit(f"[verify] round {attempt}: computing md5sum on both sides ...", on_status)
            with src_hasher_factory() as src_hasher, \
                    dst_hasher_factory() as dst_hasher:
                file_results = verify_files(results, src_hasher, dst_hasher)

            all_ok = all(fr.ok for fr in file_results)
            for fr in file_results:
                if fr.ok:
                    _emit(f"[OK] md5 match: {fr.dst_path} ({fr.src_md5})", on_status)
                else:
                    _emit(
                        f"[FAIL] md5 mismatch: {fr.dst_path} "
                        f"(src={fr.src_md5 or '?'}, dst={fr.dst_md5 or '?'}, "
                        f"bad_chunks={len(fr.bad_tasks)})",
                        on_status,
                    )
            if all_ok:
                # Final cleanup of part files on successful verify.
                _maybe_merge(results, cleanup=True)
                break

            bad_tasks = [t for fr in file_results for t in fr.bad_tasks]
            if not bad_tasks:
                raise TransferError(
                    "md5 mismatch but no chunk identified as corrupt; the "
                    "streamed bytes may not match the real source file. "
                    "Refusing to retry blindly."
                )
            if attempt > max_retries:
                raise TransferError(
                    f"md5 mismatch after {max_retries} retries; giving up. "
                    f"{len(bad_tasks)} chunk(s) still bad."
                )

            _emit(f"[retry] re-transmitting {len(bad_tasks)} corrupt chunk(s) ...", on_status)
            new_results = _execute(bad_tasks)
            report_spot_check_failures(new_results)
            # Replace old results for bad tasks with new ones.
            new_by_id = {id(r.task): r for r in new_results}
            results = [new_by_id.get(id(r.task), r) for r in results]
            # Re-merge after retry (idempotent overwrite).
            _maybe_merge(results, cleanup=False)
            total = sum(r.bytes_done for r in results)

    _emit(
        f"[OK] {direction.capitalize()} complete: {total} bytes "
        f"({total_files} file(s), {n_workers} workers).",
        on_status,
    )


def ws_transfer(
    server: ServerConfig,
    spec: TransferSpec,
    account: str | None = None,
    *,
    n_workers: int = 4,
    recursive: bool = False,
    skip_hidden: bool = False,
    session_factory: SessionFactory | None = None,
    on_status: StatusHook | None = None,
    on_progress: WSProgressHook | None = None,
) -> None:
    """Upload or download via KoKo's ``/koko/ws/sftp/`` WebSocket.

    Uses the elFinder-style JSON commands (``list`` / ``download`` / ``upload``
    chunked / ``mkdir``) present across KoKo versions, so no SSH-exec md5
    verification pass is involved (the SSH backend owns that). All files ride
    a single WebSocket connection (one connection token): opening one
    connection per file is catastrophically expensive — KoKo creates a fresh
    SFTP session to the asset per connection and JumpServer issues a
    connection token each time — and rapidly exhausts the server. Files are
    therefore transferred sequentially; chunks of a single file are also
    sequential (the ``upload`` command keys one server-side handle per integer
    id).

    Args:
        server: Target server config.
        spec: Parsed ``TransferSpec`` (direction + paths).
        account: Optional account override.
        n_workers: Accepted for interface parity with the ssh backend; the ws
            backend transfers files sequentially.
        recursive: Recurse into directories.
        skip_hidden: Skip hidden files and directories.
        session_factory: Injectable session creator.
        on_status: Injectable one-line status sink.
        on_progress: Injectable byte-increment progress sink.

    Raises:
        TransferError: If the transfer cannot be set up or verified.
    """
    session, asset = _resolve(server, spec.asset, account, session_factory)

    with connect_ws_sftp(session, asset) as client:
        if spec.is_upload:
            src_path = Path(spec.local_path)
            if src_path.is_dir() and not recursive:
                raise TransferError(
                    f"'{spec.local_path}' is a directory. "
                    f"Use -R to transfer recursively."
                )
            files = list_local_files(
                spec.local_path, recursive=recursive, skip_hidden=skip_hidden,
            )
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
                dst_file = resolve_remote_dst(client, spec.remote_path, src_path.name)
                files = [replace(f, dst_path=dst_file) for f in files]
            _ws_upload(client, files, on_status, on_progress)
            return

        try:
            src_info = client.stat(spec.remote_path)
        except Exception:
            raise TransferError(f"Remote path not found: {spec.remote_path}")
        if src_info["is_dir"] and not recursive:
            raise TransferError(
                f"'{spec.remote_path}' is a directory. "
                f"Use -R to transfer recursively."
            )
        files = list_remote_files(
            client, spec.remote_path, recursive=recursive, skip_hidden=skip_hidden,
        )
        if src_info["is_dir"]:
            base = spec.remote_path.rstrip("/")
            files = [
                replace(
                    f,
                    dst_path=str(Path(spec.local_path) / _relative(base, f.src_path)),
                )
                for f in files
            ]
        else:
            files = [
                replace(
                    f,
                    dst_path=resolve_local_dst(spec.local_path, Path(f.src_path).name),
                )
                for f in files
            ]
        _ws_download(client, files, on_status, on_progress)


def _ws_upload(
    client: WSFileClient,
    files: list[FileInfo],
    on_status: StatusHook | None,
    on_progress: WSProgressHook | None,
) -> None:
    """Upload ``files`` (local -> remote) sequentially on one connection."""
    total = sum(f.size for f in files)
    _emit(
        f"[ws] uploading {len(files)} file(s), {total} bytes ...", on_status,
    )

    # Create each unique parent directory once (mkdir is MkdirAll, but
    # deduping avoids one round trip per file in a large tree).
    parents = {posixpath.dirname(f.dst_path) for f in files}
    for parent in sorted(parents):
        if parent:
            client.mkdir(parent)

    for f in files:
        client.upload_file(f.dst_path, f.src_path, f.size, on_progress=on_progress)

    _emit(
        f"[OK] Upload complete: {total} bytes ({len(files)} file(s)).",
        on_status,
    )


def _ws_download(
    client: WSFileClient,
    files: list[FileInfo],
    on_status: StatusHook | None,
    on_progress: WSProgressHook | None,
) -> None:
    """Download ``files`` (remote -> local) sequentially on one connection."""
    total = sum(f.size for f in files)
    _emit(
        f"[ws] downloading {len(files)} file(s), {total} bytes ...", on_status,
    )

    for f in files:
        client.download_file(f.src_path, f.dst_path, f.size, on_progress=on_progress)

    _emit(
        f"[OK] Download complete: {total} bytes ({len(files)} file(s)).",
        on_status,
    )


def sftp_transfer(
    server: ServerConfig,
    spec: TransferSpec,
    account: str | None = None,
    *,
    n_workers: int = 4,
    policy_name: str | None = None,
    split_policy_name: str = "seek",
    chroot: str = "/",
    verify: bool = True,
    recursive: bool = False,
    skip_hidden: bool = False,
    backend: str | None = None,
    session_factory: SessionFactory | None = None,
    execute_hook: ExecuteHook | None = None,
    on_status: StatusHook | None = None,
    on_progress: WSProgressHook | None = None,
) -> None:
    """Execute an upload or download (exactly one remote side).

    Args:
        server: Target server config.
        spec: Parsed ``TransferSpec`` (direction + paths).
        account: Optional account override.
        n_workers: Max parallel workers.
        policy_name: Chunk policy name ("full" / "files_only"), or None
            for the default (FULL). Ignored by the ``ws`` backend.
        split_policy_name: Chunk split policy name ("seek" /
            "split-files"). Ignored by the ``ws`` backend.
        chroot: SFTP chroot in SSH-exec terms (ssh backend only).
        verify: Enable post-transfer md5 verification (ssh backend only;
            the ws backend has no SSH-exec verification path).
        recursive: Recurse into directories.
        skip_hidden: Skip hidden files and directories.
        backend: Transfer backend ("ssh" / "ws" / "http"); None consults
            ``JMS_TRANSFER_BACKEND`` then defaults to "ssh".
        session_factory: Injectable session creator.
        execute_hook: Injectable transfer runner (progress rendering, ssh
            backend only).
        on_status: Injectable one-line status sink.
        on_progress: Injectable byte-increment progress sink (ws backend
            only).

    Raises:
        TransferError: If the transfer cannot be set up or verified.
    """
    backend_name = resolve_backend(backend)
    if backend_name == "ws":
        ws_transfer(
            server, spec, account,
            n_workers=n_workers, recursive=recursive, skip_hidden=skip_hidden,
            session_factory=session_factory, on_status=on_status,
            on_progress=on_progress,
        )
        return

    policy = ChunkPolicy(policy_name.lower()) if policy_name else ChunkPolicy.FULL
    split_policy = ChunkSplitPolicy(split_policy_name.lower())
    session, asset = _resolve(server, spec.asset, account, session_factory)

    if spec.is_upload:
        src_path = Path(spec.local_path)
        if src_path.is_dir() and not recursive:
            raise TransferError(
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
            run_transfer(
                files, LocalOpenerFactory(), SFTPOpenerFactory(sftp),
                "upload",
                n_workers=n_workers, policy=policy, split_policy=split_policy,
                src_hasher_factory=None if not verify else _local_hasher_cm,
                dst_hasher_factory=(
                    None if not verify
                    else lambda: _ssh_hasher_cm(session, asset, chroot=chroot)
                ),
                merge_session=session,
                merge_asset=asset,
                merge_chroot=chroot,
                execute_hook=execute_hook,
                on_status=on_status,
            )
    else:
        with connect_sftp(session, asset) as sftp:
            try:
                src_info = sftp.stat(spec.remote_path)
            except Exception:
                raise TransferError(
                    f"Remote path not found: {spec.remote_path}"
                )
            if src_info["is_dir"] and not recursive:
                raise TransferError(
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
            run_transfer(
                files, SFTPOpenerFactory(sftp), LocalOpenerFactory(),
                "download",
                n_workers=n_workers, policy=policy, split_policy=ChunkSplitPolicy.SEEK,
                src_hasher_factory=(
                    None if not verify
                    else lambda: _ssh_hasher_cm(session, asset, chroot=chroot)
                ),
                dst_hasher_factory=None if not verify else _local_hasher_cm,
                execute_hook=execute_hook,
                on_status=on_status,
            )


def relay_transfer(
    spec: RelaySpec,
    config_path: str | None = None,
    account: str | None = None,
    *,
    n_workers: int = 4,
    policy_name: str | None = None,
    split_policy_name: str = "seek",
    chroot: str = "/",
    verify: bool = True,
    recursive: bool = False,
    skip_hidden: bool = False,
    backend: str | None = None,
    session_factory: SessionFactory | None = None,
    execute_hook: ExecuteHook | None = None,
    on_status: StatusHook | None = None,
) -> None:
    """Execute a remote-to-remote transfer (streamed relay, no local disk).

    Args:
        spec: Parsed ``RelaySpec`` (both sides remote).
        config_path: Explicit config path, or None for the default.
        account: Optional account override.
        n_workers: Max parallel workers.
        policy_name: Chunk policy name ("full" / "files_only"), or None
            for the default (FULL).
        split_policy_name: Chunk split policy name ("seek" /
            "split-files").
        chroot: SFTP chroot in SSH-exec terms.
        verify: Enable post-transfer md5 verification.
        recursive: Recurse into directories.
        skip_hidden: Skip hidden files and directories.
        backend: Transfer backend ("ssh" / "ws" / "http"); None consults
            ``JMS_TRANSFER_BACKEND`` then defaults to "ssh". The ``ws``
            backend does not support relay yet.
        session_factory: Injectable session creator.
        execute_hook: Injectable transfer runner (progress rendering).
        on_status: Injectable one-line status sink.

    Raises:
        TransferError: If the transfer cannot be set up or verified.
    """
    backend_name = resolve_backend(backend)
    if backend_name == "ws":
        raise TransferError(
            "remote-to-remote relay is not supported by the ws backend; "
            "use the ssh backend (JMS_TRANSFER_BACKEND=ssh)"
        )
    policy = ChunkPolicy(policy_name.lower()) if policy_name else ChunkPolicy.FULL
    split_policy = ChunkSplitPolicy(split_policy_name.lower())

    cfg = load_config(config_path)
    src_server = (
        cfg.get_server(spec.src_server) if spec.src_server else cfg.default_server
    )
    dst_server = (
        cfg.get_server(spec.dst_server) if spec.dst_server else cfg.default_server
    )
    src_session, src_asset = _resolve(
        src_server, spec.src_asset, account, session_factory,
    )
    dst_session, dst_asset = _resolve(
        dst_server, spec.dst_asset, account, session_factory,
    )

    with connect_sftp(src_session, src_asset) as src_sftp:
        try:
            src_info = src_sftp.stat(spec.src_path)
        except Exception:
            raise TransferError(f"Source path not found: {spec.src_path}")
        if src_info["is_dir"] and not recursive:
            raise TransferError(
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
            run_transfer(
                files,
                SFTPOpenerFactory(src_sftp), SFTPOpenerFactory(dst_sftp),
                "relay",
                n_workers=n_workers, policy=policy, split_policy=split_policy,
                src_hasher_factory=(
                    None if not verify
                    else lambda: _ssh_hasher_cm(src_session, src_asset, chroot=chroot)
                ),
                dst_hasher_factory=(
                    None if not verify
                    else lambda: _ssh_hasher_cm(dst_session, dst_asset, chroot=chroot)
                ),
                merge_session=dst_session,
                merge_asset=dst_asset,
                merge_chroot=chroot,
                execute_hook=execute_hook,
                on_status=on_status,
            )
