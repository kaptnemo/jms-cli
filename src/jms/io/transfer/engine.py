"""Transfer execution engine: parallel worker, spot check, dst resolution.

The unified entry point for upload / download / relay transfers. Runs
``FileTask`` items on a ``ThreadPoolExecutor``; all workers on the same
side share one ``paramiko.Transport`` (a single JumpServer connection
token) while each worker opens its own SFTP channel from that transport.
"""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import BinaryIO

from jms.exceptions import TransferError
from jms.log import logger
from jms.io.transfer.openers import OpenerFactory
from jms.io.transfer.models import (
    FileInfo,
    FileTask,
    ProgressCallback,
    TaskResult,
)
from jms.io.transfer.sftp import SFTPClient

# SFTP read/write buffer size (64KB — paramiko default is 8KB)
SFTP_BUFFER_SIZE: int = 64 * 1024

# Per-chunk spot-check probe size (start, middle, end)
SPOT_CHECK_SIZE: int = 32

# How many times to retry a single chunk if spot check fails
SPOT_CHECK_MAX_ATTEMPTS: int = 3


# ──── Unified worker ────────────────────────────────────────────


def _capture_sample(
    data: bytes, abs_pos: int, sample_offset: int,
) -> bytes | None:
    """If ``sample_offset`` falls inside the buffer ``data`` placed
    at absolute position ``abs_pos``, return SPOT_CHECK_SIZE bytes
    starting there (clamped to what's available). Otherwise None.
    """
    rel = sample_offset - abs_pos
    if rel < 0 or rel >= len(data):
        return None
    return bytes(data[rel:rel + SPOT_CHECK_SIZE])


def _spot_check_after_write(
    dst_f: BinaryIO,
    write_offset: int,
    chunk_size: int,
    bytes_done: int,
    sample_start: bytes,
    sample_mid: bytes,
    sample_end: bytes,
) -> str | None:
    """Read back start / middle / end samples from the dst handle
    and compare against the captured bytes. Returns None on match,
    or a human-readable diff string on mismatch.

    Assumes ``dst_f`` is opened with read access (``r+b``); does
    nothing for write-only handles (returns None).
    """
    if bytes_done <= 0:
        return None
    mid_off = chunk_size // 2
    end_off = max(0, bytes_done - SPOT_CHECK_SIZE)
    probes = [
        ("start", write_offset, sample_start),
        ("middle", write_offset + mid_off, sample_mid),
        ("end", write_offset + end_off, sample_end),
    ]
    for label, abs_off, expected in probes:
        if not expected:
            continue
        try:
            dst_f.seek(abs_off)
            actual = dst_f.read(len(expected))
        except Exception as e:
            return f"{label}@{abs_off}: read failed ({e})"
        if actual != expected:
            return (
                f"{label}@{abs_off}: "
                f"want {expected[:8].hex()}.. got {actual[:8].hex()}.. "
                f"(read {len(actual)}B)"
            )
    return None


def _write_chunk_once(
    task: FileTask,
    src_factory: OpenerFactory,
    dst_factory: OpenerFactory,
    callback: ProgressCallback | None,
) -> tuple[int, str, str | None, bytes, bytes, bytes]:
    """Single write attempt for one chunk. Returns
    (bytes_done, md5_hex, spot_check_err, sample_start, sample_mid, sample_end).

    ``spot_check_err`` is None on success, a diagnostic string on
    mismatch. Captures three 32-byte samples during the write so
    the caller can re-verify on retry. Uses fresh openers from the
    factories (= new SFTP channel per attempt, so a retry truly
    reconnects).
    """
    src_opener = src_factory.create()
    dst_opener = dst_factory.create()
    h = hashlib.md5()
    bytes_done = 0
    sample_start = b""
    sample_mid = b""
    sample_end = b""

    chunk_size = task.chunk_size
    mid_off = chunk_size // 2
    end_off = max(0, chunk_size - SPOT_CHECK_SIZE)

    # Choose dst mode:
    #   - chunked SEEK    -> "r+b" (file pre-allocated, seek then write)
    #   - chunked part    -> "wb"  (worker owns the part file)
    #   - non-chunked     -> "wb"  (worker owns the whole file)
    if task.is_chunked and not task.is_part_file:
        dst_mode = "r+b"
    else:
        dst_opener.mkdir_p(task.dst_path)
        dst_mode = "wb"

    try:
        with src_opener.open(task.src_path, "rb") as src_f:
            if task.start > 0:
                src_f.seek(task.start)
            with dst_opener.open(task.dst_path, dst_mode) as dst_f:
                if dst_mode == "r+b" and task.write_offset > 0:
                    dst_f.seek(task.write_offset)
                remaining = chunk_size
                pos = 0  # position within the chunk
                while remaining > 0:
                    read_size = min(SFTP_BUFFER_SIZE, remaining)
                    data = src_f.read(read_size)
                    if not data:
                        break
                    h.update(data)
                    dst_f.write(data)
                    # capture samples while bytes are in hand
                    if not sample_start:
                        s = _capture_sample(data, pos, 0)
                        if s:
                            sample_start = s
                    if not sample_mid:
                        s = _capture_sample(data, pos, mid_off)
                        if s:
                            sample_mid = s
                    if not sample_end:
                        s = _capture_sample(data, pos, end_off)
                        if s:
                            sample_end = s
                    bytes_done += len(data)
                    pos += len(data)
                    remaining -= len(data)
                    if callback:
                        callback(task, bytes_done)

                # Flush exactly once after the write loop so paramiko
                # drains its async write queue before we read back.
                try:
                    dst_f.flush()
                except Exception as e:
                    return (
                        bytes_done, h.hexdigest(),
                        f"flush failed: {e}",
                        sample_start, sample_mid, sample_end,
                    )

                # Spot check (only meaningful when handle is readable).
                err = None
                if dst_mode == "r+b":
                    err = _spot_check_after_write(
                        dst_f, task.write_offset, chunk_size,
                        bytes_done, sample_start, sample_mid, sample_end,
                    )
                return (
                    bytes_done, h.hexdigest() if bytes_done > 0 else "",
                    err, sample_start, sample_mid, sample_end,
                )
    finally:
        src_factory.close(src_opener)
        dst_factory.close(dst_opener)


def _spot_check_part_file(
    task: FileTask,
    dst_factory: OpenerFactory,
    sample_start: bytes,
    sample_mid: bytes,
    sample_end: bytes,
) -> str | None:
    """Re-open a freshly-written part file in ``rb`` and run spot
    check. Used when the write happened in ``wb`` mode (which is
    write-only on SFTP) so we couldn't verify in the same handle.
    """
    if task.chunk_size <= 0:
        return None
    dst_opener = dst_factory.create()
    try:
        with dst_opener.open(task.dst_path, "rb") as f:
            return _spot_check_after_write(
                f, 0, task.chunk_size, task.chunk_size,
                sample_start, sample_mid, sample_end,
            )
    except Exception as e:
        return f"reopen for spot check failed: {e}"
    finally:
        dst_factory.close(dst_opener)


def _worker(
    task: FileTask,
    src_factory: OpenerFactory,
    dst_factory: OpenerFactory,
    callback: ProgressCallback | None = None,
) -> TaskResult:
    """Transfer a single chunk with inline 3-point spot check.

    Each attempt writes the chunk and reads back 32 bytes at
    start / middle / end of the written range; on mismatch the
    progress is rewound (``callback(task, -bytes_done)``) and a
    fresh SFTP channel is opened for the retry. Up to
    ``SPOT_CHECK_MAX_ATTEMPTS`` attempts total.

    The returned ``TaskResult`` carries ``verified=False`` if all
    attempts failed spot check — the caller's md5-verify pass is
    then expected to catch and retry it at a higher level.
    """
    last_err: str | None = None
    last_md5 = ""
    last_bytes = 0
    for attempt in range(1, SPOT_CHECK_MAX_ATTEMPTS + 1):
        bytes_done, md5_hex, err, s_start, s_mid, s_end = _write_chunk_once(
            task, src_factory, dst_factory, callback,
        )
        last_bytes, last_md5, last_err = bytes_done, md5_hex, err

        # If the spot check couldn't run in-handle (wb mode for part
        # files), do an out-of-band re-read now.
        if err is None and task.is_part_file and bytes_done > 0:
            err = _spot_check_part_file(
                task, dst_factory, s_start, s_mid, s_end,
            )
            last_err = err

        if err is None:
            return TaskResult(
                task=task, bytes_done=bytes_done, md5=md5_hex,
                verified=True, attempts=attempt,
            )

        logger.warning(
            "chunk %d of %s: spot check failed on attempt %d/%d: %s",
            task.chunk_index, task.dst_path,
            attempt, SPOT_CHECK_MAX_ATTEMPTS, err,
        )
        # Rewind progress so the retry doesn't double-count.
        if callback and bytes_done > 0:
            callback(task, -bytes_done)

    logger.error(
        "chunk %d of %s: giving up after %d spot-check failures (last: %s)",
        task.chunk_index, task.dst_path,
        SPOT_CHECK_MAX_ATTEMPTS, last_err,
    )
    return TaskResult(
        task=task, bytes_done=last_bytes,
        md5=last_md5 if last_bytes > 0 else "",
        verified=False, attempts=SPOT_CHECK_MAX_ATTEMPTS,
    )


# ──── Pre-allocation ────────────────────────────────────────────


def _pre_allocate_chunked(
    tasks: list[FileTask],
    dst_factory: OpenerFactory,
) -> None:
    """Pre-allocate destination files for chunked transfers.

    Only applies to SEEK-policy chunks (where workers seek-write
    into one shared file). SPLIT_FILES part files do not need
    pre-allocation because each worker owns its own part file
    and writes it sequentially from offset 0.
    """
    chunked_files: dict[str, int] = {}
    for task in tasks:
        if task.is_chunked and not task.is_part_file:
            chunked_files[task.dst_path] = task.total_size

    if not chunked_files:
        return

    dst_opener = dst_factory.create()
    try:
        for dst_path, size in chunked_files.items():
            dst_opener.mkdir_p(dst_path)
            dst_opener.pre_allocate(dst_path, size)
    finally:
        dst_factory.close(dst_opener)


# ──── Main execution ────────────────────────────────────────────


def execute_transfer(
    tasks: list[FileTask],
    src_factory: OpenerFactory,
    dst_factory: OpenerFactory,
    n_workers: int = 4,
    callback: ProgressCallback | None = None,
) -> list[TaskResult]:
    """Execute transfer tasks in parallel using a ThreadPoolExecutor.

    Unified entry point for upload, download, and relay.

    Args:
        tasks: List of ``FileTask`` from ``plan_transfer()``.
        src_factory: Factory for source file openers.
        dst_factory: Factory for destination file openers.
        n_workers: Max parallel workers.
        callback: Optional ``callback(task, bytes_done)`` for progress.
            A negative ``bytes_done`` rewinds progress after a failed
            spot-check attempt.

    Returns:
        List of ``TaskResult`` — one per input task, in the same
        order. Each carries the bytes transferred and the MD5
        of the bytes streamed through that worker.
    """
    if not tasks:
        return []

    _pre_allocate_chunked(tasks, dst_factory)

    if n_workers <= 1 or len(tasks) == 1:
        results: list[TaskResult] = []
        for task in tasks:
            results.append(_worker(task, src_factory, dst_factory, callback))
        return results

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = [
            pool.submit(_worker, task, src_factory, dst_factory, callback)
            for task in tasks
        ]
        results = [f.result() for f in futures]

    return results


def group_parts_by_merge_target(
    results: list[TaskResult],
) -> dict[str, list[TaskResult]]:
    """Group ``TaskResult`` whose tasks are part files by their
    ``merge_to`` target, sorted by ``chunk_index``.

    Used by the caller to know which sets of ``.partNN`` files need
    a remote ``cat`` step to assemble the final destination.
    Returns an empty dict if no part files are present.
    """
    by_target: dict[str, list[TaskResult]] = {}
    for r in results:
        if r.task.is_part_file:
            assert r.task.merge_to is not None  # guaranteed by is_part_file
            by_target.setdefault(r.task.merge_to, []).append(r)
    for target in by_target:
        by_target[target].sort(key=lambda r: r.task.chunk_index)
    return by_target


# ──── Destination resolution (cp semantics) ──────────────────────


def resolve_remote_dst(sftp: SFTPClient, dst_path: str, src_basename: str) -> str:
    """Apply cp-like semantics to a remote destination path.

    If ``dst_path`` is an existing directory on the remote, place
    the file inside it as ``<dst_path>/<src_basename>``. Otherwise
    use ``dst_path`` verbatim (overwriting if it already exists as
    a file). Mirrors ``cp src dst`` behavior.

    Args:
        sftp: Connected SFTPClient for the remote.
        dst_path: Destination path the user provided.
        src_basename: Basename of the source file.

    Returns:
        The final destination file path on the remote.
    """
    try:
        if sftp.stat(dst_path)["is_dir"]:
            return f"{dst_path.rstrip('/')}/{src_basename}"
    except Exception:
        # dst doesn't exist — treat as file path
        pass
    return dst_path


def resolve_local_dst(dst_path: str, src_basename: str) -> str:
    """Apply cp-like semantics to a local destination path."""
    p = Path(dst_path)
    if p.is_dir():
        return str(p / src_basename)
    return dst_path


# ──── File listing helpers ──────────────────────────────────────


def list_remote_files(
    sftp: SFTPClient,
    path: str,
    recursive: bool = False,
    skip_hidden: bool = False,
) -> list[FileInfo]:
    """List files on a remote SFTP server.

    Args:
        sftp: Connected SFTP client.
        path: Remote path (file or directory).
        recursive: If True, recurse into subdirectories.
        skip_hidden: If True, skip files/dirs starting with ``.``.

    Returns:
        List of FileInfo with src_path set to remote paths and
        dst_path left empty.

    Raises:
        TransferError: If the remote path does not exist.
    """
    try:
        info = sftp.stat(path)
    except Exception:
        raise TransferError(f"Remote path not found: {path}")

    if not info["is_dir"]:
        return [FileInfo(src_path=path, dst_path="", size=info["size"])]

    files: list[FileInfo] = []
    entries = sftp.ls(path)
    for entry in entries:
        name = entry["name"]
        if skip_hidden and name.startswith("."):
            continue
        full_path = f"{path.rstrip('/')}/{name}"
        if entry["is_dir"]:
            if recursive:
                files.extend(list_remote_files(
                    sftp, full_path,
                    recursive=True, skip_hidden=skip_hidden,
                ))
        else:
            files.append(FileInfo(
                src_path=full_path, dst_path="",
                size=entry["size"],
            ))

    return files


def list_local_files(
    path: str,
    recursive: bool = False,
    skip_hidden: bool = False,
) -> list[FileInfo]:
    """List files on the local filesystem.

    Args:
        path: Local path (file or directory).
        recursive: If True, recurse into subdirectories.
        skip_hidden: If True, skip files/dirs starting with ``.``.

    Returns:
        List of FileInfo with src_path set to local paths and
        dst_path left empty.

    Raises:
        TransferError: If the local path does not exist.
    """
    p = Path(path)
    if not p.exists():
        raise TransferError(f"Local path not found: {path}")

    if p.is_file():
        return [FileInfo(src_path=str(p), dst_path="", size=p.stat().st_size)]

    files: list[FileInfo] = []
    if recursive:
        for child in p.rglob("*"):
            if child.is_file():
                # Check all path components for hidden
                if skip_hidden and any(
                    part.startswith(".") for part in child.relative_to(p).parts
                ):
                    continue
                files.append(FileInfo(
                    src_path=str(child), dst_path="",
                    size=child.stat().st_size,
                ))
    else:
        for child in p.iterdir():
            if child.is_file():
                if skip_hidden and child.name.startswith("."):
                    continue
                files.append(FileInfo(
                    src_path=str(child), dst_path="",
                    size=child.stat().st_size,
                ))

    return files
