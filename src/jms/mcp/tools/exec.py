"""MCP tools: remote command execution (``jms_exec``)."""

from __future__ import annotations

from mcp.server.mcpserver import server as mcpserver

from jms.mcp.tools import _cfg, _get_server, _new_session
from jms.transport import BackendType


def register_tools(server: mcpserver.MCPServer, config_path: str | None = None) -> None:
    """Register the ``jms_exec`` tool on the given MCP server.

    Args:
        server: The MCP server to attach tools to.
        config_path: Default config path used when a tool call omits
            its own ``config_path`` argument.
    """
    default_config = config_path

    @server.tool(
        name="jms_exec",
        description="Execute a command on an asset and return its output.",
    )
    def jms_exec(
        asset: str,
        command: str,
        server: str | None = None,
        config_path: str | None = None,
        timeout: int = 30,
    ) -> str:
        """Run ``command`` on ``asset`` and return its output."""
        from jms.core.resources import resolve_asset
        from jms.transport import connect

        try:
            srv = _get_server(_cfg(config_path, default_config), server)
            session = _new_session(srv)
            try:
                info = resolve_asset(session, asset)
                with connect(session, info, BackendType.AUTO) as terminal:
                    return terminal.execute(command, timeout=timeout)
            finally:
                session.session.close()
        except Exception as e:
            return f"ERROR: {e}"
