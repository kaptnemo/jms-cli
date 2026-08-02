"""MCP tool modules for the jms server, grouped by capability.

Each submodule defines a ``register_tools(server, config_path)`` that
attaches its tools via the ``@server.tool(...)`` decorator. The shared
helpers below keep per-invocation session handling uniform across tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jms.config import ServerConfig
    from jms.core.auth import JMSSession


def _get_server(config_path: str | None, server: str | None) -> ServerConfig:
    """Load config and resolve the target server (default when ``server`` is None)."""
    from jms.config import load_config

    cfg = load_config(config_path)
    if server:
        return cfg.get_server(server)
    return cfg.default_server


def _new_session(server: ServerConfig) -> JMSSession:
    """Create and log in a fresh JMSSession (one per tool invocation)."""
    from jms.core.auth import JMSSession

    session = JMSSession(server)
    session.login()
    return session


def _cfg(tool_path: str | None, default: str | None) -> str | None:
    """A tool's explicit config path, falling back to the server default."""
    return tool_path if tool_path is not None else default


__all__ = ["_cfg", "_get_server", "_new_session"]
