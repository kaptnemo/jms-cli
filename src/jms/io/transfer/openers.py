"""I/O opener abstraction: local filesystem or per-thread SFTP channels."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

import paramiko

from jms.log import logger
from jms.io.transfer.sftp import SFTPClient


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
