"""Transfer planning: chunking policies and task generation."""

from __future__ import annotations

from enum import Enum

from jms.io.transfer.models import FileInfo, FileTask

# Minimum chunk size — don't split below this (16MB)
MIN_CHUNK_SIZE: int = 16 * 1024 * 1024

# Default threshold: files larger than this get chunked (256MB)
DEFAULT_CHUNK_THRESHOLD: int = 256 * 1024 * 1024


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
