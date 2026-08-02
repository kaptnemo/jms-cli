"""SFTP access to an asset over a KoKo connection-token transport."""

from __future__ import annotations

import stat

import paramiko

from jms.core.resources import AssetInfo
from jms.core.auth import JMSSession
from jms.exceptions import TerminalError, TransferError
from jms.log import logger
from jms.transport import open_koko_transport

# Timeout for each SFTP channel (seconds)
SFTP_CHANNEL_TIMEOUT: int = 3600


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
