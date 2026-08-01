"""Terminal backend abstraction: one terminal interface over SSH / WebSocket.

Usage::

    from jms.backend import BackendType, connect

    with connect(session, asset, backend=BackendType.AUTO) as term:
        output = term.execute("whoami")

AUTO selection strategy: try SSH first (lower latency, no Nginx hop,
native PTY); fall back to WebSocket on failure.
"""

from contextlib import contextmanager
from enum import Enum
from typing import Iterator

from jms.assets import AssetInfo
from jms.auth import JMSSession
from jms.backend.base import AbstractTerminal, local_tty_size, strip_ansi
from jms.backend.ssh import (
    SSHTerminal,
    connect_ssh,
    open_koko_transport,
    open_ssh_terminal,
)
from jms.backend.token import KOKO_SSH_PORT, create_connection_token
from jms.backend.ws import WSTerminal, connect_ws, open_ws_terminal
from jms.exceptions import TerminalError
from jms.log import logger

__all__ = [
    "AbstractTerminal",
    "BackendType",
    "KOKO_SSH_PORT",
    "SSHTerminal",
    "WSTerminal",
    "connect",
    "connect_ssh",
    "connect_ws",
    "create_connection_token",
    "local_tty_size",
    "open_koko_transport",
    "open_ssh_terminal",
    "open_ws_terminal",
    "strip_ansi",
]


class BackendType(Enum):
    """Terminal backend selection."""

    AUTO = "auto"
    SSH = "ssh"
    WEBSOCKET = "ws"


@contextmanager
def connect(
    session: JMSSession,
    asset: AssetInfo,
    backend: BackendType = BackendType.AUTO,
) -> Iterator[AbstractTerminal]:
    """Connect to an asset and yield a ready terminal (factory context manager).

    Args:
        session: Authenticated session.
        asset: Resolved asset.
        backend: Backend selection (AUTO / SSH / WEBSOCKET).

    Yields:
        An ``AbstractTerminal`` instance (``SSHTerminal`` or ``WSTerminal``).

    Raises:
        TerminalError: All attempted backends failed to connect, or the
            backend type is unknown.
    """
    # The try must only cover connection setup, never the yield — otherwise a
    # TerminalError raised inside the with-body would be misread as a connect
    # failure and trigger fallback (contextlib would also raise RuntimeError,
    # swallowing the real error)
    term: AbstractTerminal
    if backend == BackendType.SSH:
        term = open_ssh_terminal(session, asset)
    elif backend == BackendType.WEBSOCKET:
        term = open_ws_terminal(session, asset)
    elif backend == BackendType.AUTO:
        try:
            term = open_ssh_terminal(session, asset)
        except TerminalError as ssh_err:
            logger.info(
                "SSH backend failed (%s), falling back to WebSocket ...",
                ssh_err,
            )
            term = open_ws_terminal(session, asset)
    else:
        raise TerminalError(f"Unknown backend: {backend}")

    try:
        yield term
    finally:
        term.close()
