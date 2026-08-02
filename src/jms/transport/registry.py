"""Backend registry: connect() resolves backends by name so new protocols
can be added without touching the dispatch logic.

Backends self-register (see the bottom of ``jms.transport.ssh`` and
``jms.transport.ws``); future RDP/VNC backends register here too.
"""

import sys
from typing import Callable, cast

from jms.core.auth import JMSSession
from jms.core.resources import AssetInfo
from jms.exceptions import TerminalError
from jms.transport.base import AbstractTerminal, TerminalCapability

Factory = Callable[[JMSSession, AssetInfo], AbstractTerminal]

# Backend name -> (factory, capabilities). A backend factory opens a
# terminal for a resolved asset.
_BACKENDS: dict[str, tuple[Factory, frozenset[TerminalCapability]]] = {}

# Backends tried in order by BackendType.AUTO. Register-only backends
# (e.g. a future display-only RDP/VNC) are intentionally not listed here.
_AUTO_SEQUENCE: tuple[str, ...] = ("ssh", "ws")


def register_backend(
    name: str,
    factory: Callable[[JMSSession, AssetInfo], AbstractTerminal],
    capabilities: frozenset[TerminalCapability] = frozenset(),
) -> None:
    """Register a terminal backend factory under ``name``.

    Args:
        name: Backend name (e.g. ``"ssh"``, ``"ws"``).
        factory: Opens and returns a ready terminal.
        capabilities: Backend capability flags (metadata only).

    Raises:
        ValueError: A backend with this name is already registered.
    """
    if name in _BACKENDS:
        raise ValueError(f"Backend already registered: {name}")
    _BACKENDS[name] = (factory, capabilities)


def open_backend(name: str, session: JMSSession, asset: AssetInfo) -> AbstractTerminal:
    """Open a terminal via the named backend.

    Args:
        name: Registered backend name.
        session: Authenticated JumpServer session.
        asset: Resolved asset.

    Returns:
        A ready terminal (caller owns it; must ``close()``).

    Raises:
        TerminalError: No backend registered under ``name``.
    """
    entry = _BACKENDS.get(name)
    if entry is None:
        raise TerminalError(f"Unknown backend: {name}")
    factory, _ = entry
    # Re-resolve through the jms.transport namespace so tests that patch
    # jms.transport.open_ssh_terminal / open_ws_terminal still intercept.
    transport_mod = sys.modules.get("jms.transport")
    candidate = getattr(transport_mod, getattr(factory, "__name__", ""), None)
    if not callable(candidate):
        candidate = factory
    return cast(
        Callable[[JMSSession, AssetInfo], AbstractTerminal], candidate,
    )(session, asset)


def list_backends() -> list[str]:
    """Return registered backend names, sorted."""
    return sorted(_BACKENDS)


def backend_capabilities(name: str) -> frozenset[TerminalCapability]:
    """Return a backend's capability flags (empty for unknown backends)."""
    entry = _BACKENDS.get(name)
    if entry is None:
        return frozenset()
    return entry[1]


def auto_sequence() -> list[str]:
    """Return the backends tried in order by ``BackendType.AUTO``."""
    return list(_AUTO_SEQUENCE)
