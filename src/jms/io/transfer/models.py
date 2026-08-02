"""Pure data types for the parallel SFTP transfer engine.

No imports from other ``jms`` modules — safe to import from anywhere
(e.g. ``jms.io.verify``) without creating an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

# Progress callback signature: (task, bytes_done_so_far_in_this_attempt).
# A negative value rewinds progress after a failed spot-check attempt.
ProgressCallback = Callable[["FileTask", int], None]


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
