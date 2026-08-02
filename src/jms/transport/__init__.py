"""Terminal backend abstraction: one terminal interface over SSH / WebSocket.

Usage::

    from jms.transport import BackendType, connect

    with connect(session, asset, backend=BackendType.AUTO) as term:
        output = term.execute("whoami")

AUTO selection strategy: try SSH first (lower latency, no Nginx hop,
native PTY); fall back to WebSocket on failure. Backends self-register
via ``register_backend`` and are dispatched by name, so new protocols
(rdp / vnc / ...) never require editing ``connect()`` or ``BackendType``.
"""

from contextlib import contextmanager
from enum import Enum
from typing import Iterator

from jms.core.resources import AssetInfo
from jms.core.auth import JMSSession
from jms.transport.base import (
    AbstractTerminal,
    TerminalCapability,
    local_tty_size,
    strip_ansi,
)
from jms.transport.registry import (
    auto_sequence,
    backend_capabilities,
    list_backends,
    open_backend,
    register_backend,
)
from jms.transport.ssh import (
    SSHTerminal,
    connect_ssh,
    open_koko_transport,
    open_ssh_terminal,
)
from jms.transport.token import KOKO_SSH_PORT, create_connection_token
from jms.transport.ws import WSTerminal, connect_ws, open_ws_terminal
from jms.exceptions import TerminalError
from jms.log import logger

__all__ = [
    "AbstractTerminal",
    "BackendType",
    "KOKO_SSH_PORT",
    "SSHTerminal",
    "TerminalCapability",
    "WSTerminal",
    "auto_sequence",
    "backend_capabilities",
    "connect",
    "connect_ssh",
    "connect_ws",
    "create_connection_token",
    "list_backends",
    "local_tty_size",
    "open_backend",
    "open_koko_transport",
    "open_ssh_terminal",
    "open_ws_terminal",
    "register_backend",
    "strip_ansi",
]


class BackendType(Enum):
    """Terminal backend selection."""

    AUTO = "auto"
    SSH = "ssh"
    WEBSOCKET = "ws"


def _resolve_name(backend: BackendType | str) -> str:
    """Map a BackendType or raw backend name to a registry name."""
    if isinstance(backend, BackendType):
        return backend.value
    if isinstance(backend, str):
        return backend
    raise TerminalError(f"Unknown backend: {backend}")


@contextmanager
def connect(
    session: JMSSession,
    asset: AssetInfo,
    backend: BackendType | str = BackendType.AUTO,
) -> Iterator[AbstractTerminal]:
    """Connect to an asset and yield a ready terminal (factory context manager).

    Args:
        session: Authenticated session.
        asset: Resolved asset.
        backend: Backend selection — a ``BackendType`` (AUTO / SSH /
            WEBSOCKET) or a raw backend name (e.g. ``"ws"``).

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
    if backend == BackendType.AUTO:
        term = _open_auto(session, asset)
    else:
        term = open_backend(_resolve_name(backend), session, asset)

    try:
        yield term
    finally:
        term.close()


def _open_auto(session: JMSSession, asset: AssetInfo) -> AbstractTerminal:
    """Try each backend in AUTO order; on failure log and try the next."""
    last_err: TerminalError | None = None
    for name in auto_sequence():
        try:
            return open_backend(name, session, asset)
        except TerminalError as err:
            last_err = err
            logger.info("Backend %s failed (%s), trying next ...", name, err)
    if last_err is None:
        raise TerminalError("No backends available")
    raise last_err
