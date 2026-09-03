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

This package is split into focused modules (``models``, ``spec``,
``sftp``, ``openers``, ``plan``, ``engine``); this module re-exports the
whole public API so ``from jms.io.transfer import X`` keeps working.
"""

from jms.io.transfer.engine import (
    SPOT_CHECK_MAX_ATTEMPTS,
    SPOT_CHECK_SIZE,
    SFTP_BUFFER_SIZE,
    _capture_sample,
    _pre_allocate_chunked,
    _spot_check_after_write,
    _spot_check_part_file,
    _worker,
    _write_chunk_once,
    execute_transfer,
    group_parts_by_merge_target,
    list_local_files,
    list_remote_files,
    resolve_local_dst,
    resolve_remote_dst,
)
from jms.io.transfer.openers import (
    IOOpener,
    LocalOpener,
    LocalOpenerFactory,
    OpenerFactory,
    SFTPChannelOpener,
    SFTPOpenerFactory,
)
from jms.io.transfer.models import (
    FileInfo,
    FileTask,
    ProgressCallback,
    RelaySpec,
    TaskResult,
    TransferSpec,
)
from jms.io.transfer.plan import (
    ChunkPolicy,
    ChunkSplitPolicy,
    DEFAULT_CHUNK_THRESHOLD,
    MIN_CHUNK_SIZE,
    plan_transfer,
)
from jms.io.transfer.sftp import SFTP_CHANNEL_TIMEOUT, SFTPClient, connect_sftp
from jms.io.transfer.ws import CHUNK_SIZE, WSFileClient, connect_ws_sftp
from jms.io.transfer.spec import _parse_remote_spec, parse_transfer_spec

__all__ = [
    "CHUNK_SIZE",
    "ChunkPolicy",
    "ChunkSplitPolicy",
    "DEFAULT_CHUNK_THRESHOLD",
    "FileInfo",
    "FileTask",
    "IOOpener",
    "LocalOpener",
    "LocalOpenerFactory",
    "MIN_CHUNK_SIZE",
    "OpenerFactory",
    "ProgressCallback",
    "RelaySpec",
    "SFTP_BUFFER_SIZE",
    "SFTP_CHANNEL_TIMEOUT",
    "SFTPChannelOpener",
    "SFTPClient",
    "SFTPOpenerFactory",
    "SPOT_CHECK_MAX_ATTEMPTS",
    "SPOT_CHECK_SIZE",
    "TaskResult",
    "TransferSpec",
    "WSFileClient",
    "_capture_sample",
    "_parse_remote_spec",
    "_pre_allocate_chunked",
    "_spot_check_after_write",
    "_spot_check_part_file",
    "_worker",
    "_write_chunk_once",
    "connect_sftp",
    "connect_ws_sftp",
    "execute_transfer",
    "group_parts_by_merge_target",
    "list_local_files",
    "list_remote_files",
    "parse_transfer_spec",
    "plan_transfer",
    "resolve_local_dst",
    "resolve_remote_dst",
]
