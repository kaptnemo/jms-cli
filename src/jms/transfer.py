"""Parallel SFTP file transfer with large-file chunking and relay support.

Unified transfer engine for three scenarios:

- **upload** (local -> remote)
- **download** (remote -> local)
- **relay** (remote -> remote, streamed through memory, never touching
  local disk)

Parallelism uses a ``ThreadPoolExecutor``. All workers on the same side share
one ``paramiko.Transport`` (a single JumpServer connection token) while
each worker opens its own ``SFTPClient`` channel from that transport;
the transport is thread-safe, individual SFTP channels are not.

Files larger than ``DEFAULT_CHUNK_THRESHOLD`` are split into chunks
(``ChunkPolicy.FULL``). Each chunk seek-writes to its offset of a
pre-allocated destination file (``ChunkSplitPolicy.SEEK``) or to a
standalone ``.partNN`` file that a later remote ``cat`` step assembles
(``ChunkSplitPolicy.SPLIT_FILES``).

Progress reporting is callback-based (see ``execute_transfer``); the
CLI layer renders a rich progress bar on top of it, library users may
simply omit the callback.
"""

from __future__ import annotations

import hashlib
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable

import paramiko

from jms.assets import AssetInfo
from jms.auth import JMSSession
from jms.backend import open_koko_transport
from jms.exceptions import TerminalError, TransferError
from jms.log import logger

# SFTP read/write buffer size (64KB — paramiko default is 8KB)
SFTP_BUFFER_SIZE: int = 64 * 1024

# Minimum chunk size — don't split below this (16MB)
MIN_CHUNK_SIZE: int = 16 * 1024 * 1024

# Default threshold: files larger than this get chunked (256MB)
DEFAULT_CHUNK_THRESHOLD: int = 256 * 1024 * 1024

# Per-chunk spot-check probe size (start, middle, end)
SPOT_CHECK_SIZE: int = 32

# How many times to retry a single chunk if spot check fails
SPOT_CHECK_MAX_ATTEMPTS: int = 3

# Timeout for each SFTP channel (seconds)
SFTP_CHANNEL_TIMEOUT: int = 3600

# Progress callback signature: (task, bytes_done_so_far_in_this_attempt).
# A negative value rewinds progress after a failed spot-check attempt.
ProgressCallback = Callable[["FileTask", int], None]


# ──── Enums ─────────────────────────────────────────────────────


class ChunkPolicy(Enum):
    """Controls whether large files are split into chunks.

    Attributes:
        FULL: Multi-file concurrency + large-file chunking.
        FILES_ONLY: Multi-file concurrency only, no splitting.
    """

    FULL = "full"
    FILES_ONLY = "files_only"


class ChunkSplitPolicy(Enum):
    """Controls how chunks are physically written to the destination.

    Attributes:
        SEEK: Each worker opens the same dst in ``r+b`` mode and
            seeks to its offset. Relies on the server honoring
            SFTP pwrite semantics for concurrent handles on one
            file.
        SPLIT_FILES: Each worker writes its chunk to a separate
            ``<dst>.partNN`` file from offset 0; a merge step
            (``cat`` via SSH exec) assembles the final dst. Avoids
            any concurrent-write hazard on the SFTP server.
            Applies only to remote-dst writes (upload / relay
            write-side); local-dst (download) is unaffected since
            POSIX pwrite on a local fd is always safe.
    """

    SEEK = "seek"
    SPLIT_FILES = "split-files"


# ──── Data structures ───────────────────────────────────────────


@dataclass(frozen=True)
class FileTask:
    """A single transfer unit: a file or a chunk of a file.

    Attributes:
        src_path: Source path (local for upload, remote for download).
        dst_path: Destination path that this worker writes to.
            For ``SPLIT_FILES`` policy this is the ``.partNN`` path,
            not the final logical destination (see ``merge_to``).
        start: Byte offset in the SOURCE where this chunk begins.
        end: Byte offset in the SOURCE where this chunk ends.
        total_size: Total size of the original source file.
        chunk_index: Index of this chunk (0-based).
        total_chunks: Total number of chunks for this file (1 = no split).
        merge_to: If set, dst_path is a part file to be concatenated
            into ``merge_to`` after all sibling parts complete. None
            for SEEK policy and for non-chunked tasks.
        write_offset: Offset at which to seek the destination handle
            before writing. For SEEK policy this equals ``start``; for
            SPLIT_FILES it is 0 (parts are independent files).
    """

    src_path: str
    dst_path: str
    start: int
    end: int
    total_size: int
    chunk_index: int
    total_chunks: int
    merge_to: str | None = None
    write_offset: int = 0

    @property
    def chunk_size(self) -> int:
        """Number of bytes this task is responsible for."""
        return self.end - self.start

    @property
    def filename(self) -> str:
        """Base filename for display purposes."""
        return Path(self.merge_to or self.src_path).name

    @property
    def is_chunked(self) -> bool:
        """Whether this task is part of a multi-chunk transfer."""
        return self.total_chunks > 1

    @property
    def is_part_file(self) -> bool:
        """Whether this task writes a standalone part file."""
        return self.merge_to is not None


@dataclass(frozen=True)
class FileInfo:
    """Metadata for a file to be transferred.

    Attributes:
        src_path: Source path.
        dst_path: Destination path (filled in by the caller, e.g. via
            ``dataclasses.replace`` after dst resolution).
        size: File size in bytes.
    """

    src_path: str
    dst_path: str
    size: int


@dataclass(frozen=True)
class TaskResult:
    """Outcome of one ``FileTask`` execution.

    Attributes:
        task: The executed task.
        bytes_done: Bytes successfully read+written.
        md5: Hex MD5 of the bytes streamed through this worker
             (i.e. read from src and written to dst). Empty
             string if the read produced 0 bytes.
        verified: Whether inline 3-point spot check passed after
             the final write. False means the chunk failed all
             inline retry attempts and is suspected corrupted.
        attempts: How many write attempts were made (1 = first try
             succeeded; >1 = inline retry was triggered).
    """

    task: FileTask
    bytes_done: int
    md5: str
    verified: bool = True
    attempts: int = 1


# ──── Direction detection ───────────────────────────────────────


@dataclass(frozen=True)
class TransferSpec:
    """Parsed local <-> remote transfer specification.

    Attributes:
        asset: Asset name or address.
        server: Server alias, or None for the default server.
        remote_path: Path on the remote asset.
        local_path: Path on the local machine.
        is_upload: True for local -> remote, False for remote -> local.
    """

    asset: str
    server: str | None
    remote_path: str
    local_path: str
    is_upload: bool


@dataclass(frozen=True)
class RelaySpec:
    """Parsed remote -> remote transfer specification (stream relay).

    Both sides are remote assets, possibly on different servers.

    Attributes:
        src_asset: Source asset name or address.
        src_server: Source server alias, or None for default.
        src_path: Path on the source asset.
        dst_asset: Destination asset name or address.
        dst_server: Destination server alias, or None for default.
        dst_path: Path on the destination asset.
    """

    src_asset: str
    src_server: str | None
    src_path: str
    dst_asset: str
    dst_server: str | None
    dst_path: str


def _parse_remote_spec(spec: str) -> tuple[str, str | None, str]:
    """Parse ``asset[@server]:path`` into ``(asset, server, path)``.

    The first colon splits the host part from the path, so ``@`` or
    further colons inside the path are preserved; the last ``@``
    within the host part separates the server alias.

    Returns:
        ``(asset, server_or_None, remote_path)``.

    Raises:
        TransferError: If the spec has no colon, an empty asset,
            or an empty path.
    """
    colon = spec.find(":")
    if colon == -1:
        raise TransferError(
            f"Invalid remote spec '{spec}': expected <asset>[@<server>]:<path>"
        )
    host, path = spec[:colon], spec[colon + 1:]
    at = host.rfind("@")
    if at > 0:
        asset, server = host[:at], host[at + 1:] or None
    else:
        asset, server = host, None
    if not asset:
        raise TransferError(f"Invalid remote spec '{spec}': asset name is empty")
    if not path:
        raise TransferError(f"Invalid remote spec '{spec}': remote path is empty")
    return asset, server, path


def parse_transfer_spec(src: str, dst: str) -> TransferSpec | RelaySpec:
    """Determine transfer direction from ``sftp <src> <dst>`` arguments.

    A side counts as remote when it contains a colon; local paths on
    POSIX never do (Windows drive letters are out of scope).

    - One remote + one local -> ``TransferSpec`` (upload or download)
    - Both remote -> ``RelaySpec`` (stream relay)
    - Neither remote -> ``TransferError``

    Args:
        src: First positional argument.
        dst: Second positional argument.

    Returns:
        Parsed ``TransferSpec`` or ``RelaySpec``.

    Raises:
        TransferError: If neither argument is remote.
    """
    src_remote = ":" in src
    dst_remote = ":" in dst

    if not src_remote and not dst_remote:
        raise TransferError(
            "Neither argument looks like a remote path. "
            "Use <asset>[@server]:<path> for the remote side."
        )

    if src_remote and dst_remote:
        s_asset, s_server, s_path = _parse_remote_spec(src)
        d_asset, d_server, d_path = _parse_remote_spec(dst)
        return RelaySpec(
            src_asset=s_asset, src_server=s_server, src_path=s_path,
            dst_asset=d_asset, dst_server=d_server, dst_path=d_path,
        )

    if dst_remote:
        asset, server, remote_path = _parse_remote_spec(dst)
        return TransferSpec(
            asset=asset, server=server,
            remote_path=remote_path, local_path=src, is_upload=True,
        )

    asset, server, remote_path = _parse_remote_spec(src)
    return TransferSpec(
        asset=asset, server=server,
        remote_path=remote_path, local_path=dst, is_upload=False,
    )


# ──── SFTP client ────────────────────────────────────────────────


class SFTPClient:
    """SFTP access to an asset over a KoKo connection-token transport.

    One ``paramiko.Transport`` (one token) can serve multiple SFTP
    channels via ``new_channel()``; token auth bypasses MFA entirely.

    Args:
        transport: Authenticated KoKo transport (owned, closed by us).
        sftp: SFTP channel opened from that transport.
    """

    def __init__(
        self, transport: paramiko.Transport, sftp: paramiko.SFTPClient,
    ) -> None:
        self._transport: paramiko.Transport = transport
        self._sftp: paramiko.SFTPClient = sftp
        self._closed: bool = False

    @property
    def transport(self) -> paramiko.Transport:
        """The underlying paramiko Transport (thread-safe, shareable)."""
        return self._transport

    def new_channel(self) -> paramiko.SFTPClient:
        """Open a new SFTP channel on the shared transport.

        Each channel is independent and should be used by a single
        thread. The transport itself is thread-safe.

        Returns:
            A fresh ``paramiko.SFTPClient`` on the same transport.
        """
        ch = paramiko.SFTPClient.from_transport(self.transport)
        if ch is None:
            raise TransferError("SFTP channel failed: transport not active")
        chan = ch.get_channel()
        if chan is not None:
            chan.settimeout(SFTP_CHANNEL_TIMEOUT)
        return ch

    def ls(self, path: str = ".") -> list[dict]:
        """List directory entries as dicts with name/size/is_dir keys."""
        return [
            {
                "name": attr.filename,
                "size": attr.st_size or 0,
                "is_dir": stat.S_ISDIR(attr.st_mode) if attr.st_mode else False,
            }
            for attr in self._sftp.listdir_attr(path)
        ]

    def stat(self, path: str) -> dict:
        """Return ``{"size": int, "is_dir": bool}`` for a remote path."""
        attr = self._sftp.stat(path)
        return {
            "size": attr.st_size or 0,
            "is_dir": stat.S_ISDIR(attr.st_mode) if attr.st_mode else False,
        }

    def close(self) -> None:
        """Close the SFTP channel and the underlying transport."""
        if self._closed:
            return
        self._closed = True
        try:
            self._sftp.close()
        except Exception:
            pass
        self._transport.close()
        logger.debug("SFTP connection closed")

    def __enter__(self) -> "SFTPClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def connect_sftp(session: JMSSession, asset: AssetInfo) -> SFTPClient:
    """Open SFTP access to an asset via KoKo connection-token auth.

    Requires a ``protocol="ssh"``/``connect_method="web_sftp"`` token:
    SFTP rides on the asset's ssh protocol (permed_protocols only lists
    ssh, with ``sftp_enabled`` in its settings); ``protocol="sftp"``
    tokens are rejected (perm_account_invalid).

    Note: the asset permission must grant upload/download actions —
    with connect-only perms KoKo reports "please select one of the
    assets" for every SFTP path.

    Args:
        session: Authenticated JMS session.
        asset: Resolved asset info.

    Returns:
        Connected SFTPClient.

    Raises:
        TransferError: If the SSH handshake or SFTP channel fails.
    """
    try:
        transport = open_koko_transport(
            session, asset, protocol="ssh", connect_method="web_sftp",
        )
    except TerminalError as e:
        raise TransferError(f"SFTP connection failed: {e}") from e

    try:
        sftp = paramiko.SFTPClient.from_transport(transport)
    except Exception as e:
        transport.close()
        raise TransferError(f"SFTP channel failed: {e}") from e
    if sftp is None:
        transport.close()
        raise TransferError("SFTP channel failed: transport not active")

    chan = sftp.get_channel()
    if chan is not None:
        chan.settimeout(SFTP_CHANNEL_TIMEOUT)

    logger.debug("SFTP channel open")
    return SFTPClient(transport, sftp)


# ──── I/O opener abstraction ─────────────────────────────────────


class IOOpener:
    """Abstract interface for opening source/destination files.

    Concrete subclasses handle the local filesystem or a remote SFTP
    channel. A relay transfer pairs a remote source opener with a
    remote destination opener and streams through memory.
    """

    def open(self, path: str, mode: str) -> BinaryIO:
        """Open a file. Returns a context manager with read/write/seek."""
        raise NotImplementedError

    def pre_allocate(self, path: str, size: int) -> None:
        """Pre-allocate a file to a given size (for chunked writes)."""
        raise NotImplementedError

    def mkdir_p(self, path: str) -> None:
        """Ensure parent directories exist for the given path."""


class LocalOpener(IOOpener):
    """Opens local files."""

    def open(self, path: str, mode: str) -> BinaryIO:
        return open(path, mode)

    def pre_allocate(self, path: str, size: int) -> None:
        # Idempotent: skip if file already exists at the target size
        try:
            if Path(path).stat().st_size == size:
                return
        except OSError:
            pass
        with open(path, "wb") as f:
            if size > 0:
                f.seek(size - 1)
                f.write(b"\0")

    def mkdir_p(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)


class SFTPChannelOpener(IOOpener):
    """Opens remote files via a dedicated SFTP channel.

    Each instance holds its own ``paramiko.SFTPClient`` channel
    created from a shared ``Transport``. Thread-safe as long as
    each thread uses its own ``SFTPChannelOpener``.

    Args:
        sftp_channel: An independent ``paramiko.SFTPClient`` channel.
    """

    def __init__(self, sftp_channel: paramiko.SFTPClient) -> None:
        self._ch: paramiko.SFTPClient = sftp_channel

    def open(self, path: str, mode: str) -> BinaryIO:
        return self._ch.open(path, mode)

    def pre_allocate(self, path: str, size: int) -> None:
        # Idempotent: if the file already exists at the right size,
        # leave it alone. Critical for chunk-level retries — otherwise
        # opening with "wb" would truncate previously-written chunks
        # back to a sparse hole.
        try:
            st = self._ch.stat(path)
            if (st.st_size or 0) == size:
                logger.debug(
                    "pre_allocate skipped: remote %s already has size %d",
                    path, size,
                )
                return
        except Exception:
            pass
        with self._ch.open(path, "wb") as f:
            if size > 0:
                f.seek(size - 1)
                f.write(b"\0")
        logger.debug("Pre-allocated remote %s (%d bytes)", path, size)

    def mkdir_p(self, path: str) -> None:
        parts = path.rsplit("/", 1)
        if len(parts) == 2 and parts[0]:
            self._ensure_dirs(parts[0])

    def close(self) -> None:
        """Close this SFTP channel."""
        try:
            self._ch.close()
        except Exception:
            pass

    def _ensure_dirs(self, path: str) -> None:
        """Recursively create remote directories."""
        try:
            self._ch.stat(path)
            return
        except Exception:
            pass
        parent = path.rsplit("/", 1)
        if len(parent) == 2 and parent[0]:
            self._ensure_dirs(parent[0])
        try:
            self._ch.mkdir(path)
        except Exception:
            pass


# ──── Opener factories ────────────────────────────────────────────


class OpenerFactory:
    """Creates IOOpener instances for worker threads."""

    def create(self) -> IOOpener:
        """Create a new opener for a worker thread."""
        raise NotImplementedError

    def close(self, opener: IOOpener) -> None:
        """Release resources held by the opener."""


class LocalOpenerFactory(OpenerFactory):
    """Factory for local file openers."""

    def create(self) -> IOOpener:
        return LocalOpener()


class SFTPOpenerFactory(OpenerFactory):
    """Factory that creates per-thread SFTP channel openers.

    All channels share the same ``Transport`` (single token).
    Each worker gets an independent ``SFTPClient`` channel.

    Args:
        sftp_client: A connected ``SFTPClient`` whose transport
                     will be shared across all worker channels.
    """

    def __init__(self, sftp_client: SFTPClient) -> None:
        self._client: SFTPClient = sftp_client

    def create(self) -> IOOpener:
        ch = self._client.new_channel()
        return SFTPChannelOpener(ch)

    def close(self, opener: IOOpener) -> None:
        if isinstance(opener, SFTPChannelOpener):
            opener.close()


# ──── Transfer planning ─────────────────────────────────────────


def plan_transfer(
    files: list[FileInfo],
    n_workers: int = 4,
    chunk_threshold: int = DEFAULT_CHUNK_THRESHOLD,
    policy: ChunkPolicy = ChunkPolicy.FULL,
    split_policy: ChunkSplitPolicy = ChunkSplitPolicy.SEEK,
    min_chunk_size: int = MIN_CHUNK_SIZE,
) -> list[FileTask]:
    """Plan transfer tasks for a list of files.

    Small files get a single ``FileTask``. Large files (above
    ``chunk_threshold``) are split into multiple chunks when
    ``policy`` is ``FULL`` and ``n_workers`` > 1.

    Args:
        files: List of files to transfer.
        n_workers: Number of parallel workers.
        chunk_threshold: Split files larger than this (bytes).
        policy: Chunking policy.
        split_policy: How chunks are physically written
            (``SEEK`` shares one dst file via pwrite, ``SPLIT_FILES``
            gives each chunk its own ``.partNN`` file for later merge).
        min_chunk_size: Never split below this chunk size.

    Returns:
        List of FileTask instances ready for execution.
    """
    tasks: list[FileTask] = []
    do_chunk = (policy == ChunkPolicy.FULL and n_workers > 1)

    for f in files:
        if do_chunk and f.size > chunk_threshold:
            chunk_size = max(f.size // n_workers, min_chunk_size)
            n_chunks = (f.size + chunk_size - 1) // chunk_size

            for i in range(n_chunks):
                start = i * chunk_size
                end = min(start + chunk_size, f.size)
                if split_policy == ChunkSplitPolicy.SPLIT_FILES:
                    part_path = f"{f.dst_path}.part{i:04d}"
                    tasks.append(FileTask(
                        src_path=f.src_path, dst_path=part_path,
                        start=start, end=end, total_size=f.size,
                        chunk_index=i, total_chunks=n_chunks,
                        merge_to=f.dst_path, write_offset=0,
                    ))
                else:
                    tasks.append(FileTask(
                        src_path=f.src_path, dst_path=f.dst_path,
                        start=start, end=end, total_size=f.size,
                        chunk_index=i, total_chunks=n_chunks,
                        merge_to=None, write_offset=start,
                    ))
        else:
            tasks.append(FileTask(
                src_path=f.src_path, dst_path=f.dst_path,
                start=0, end=f.size, total_size=f.size,
                chunk_index=0, total_chunks=1,
                merge_to=None, write_offset=0,
            ))

    return tasks


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
