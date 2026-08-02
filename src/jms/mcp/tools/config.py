"""MCP tools: server configuration introspection (``jms_config_list``)."""

from __future__ import annotations

from mcp.server.mcpserver import server as mcpserver

from jms.mcp.tools import _cfg


def register_tools(server: mcpserver.MCPServer, config_path: str | None = None) -> None:
    """Register the ``jms_config_list`` tool on the given MCP server.

    Args:
        server: The MCP server to attach tools to.
        config_path: Default config path used when a tool call omits
            its own ``config_path`` argument.
    """
    default_config = config_path

    @server.tool(
        name="jms_config_list",
        description="List configured JumpServer servers, marking the default with '*'.",
    )
    def jms_config_list(config_path: str | None = None) -> str:
        """List configured servers (default marked with '*')."""
        from jms.config import load_config

        try:
            cfg = load_config(_cfg(config_path, default_config))
            lines = [
                f"{name:<16} {srv.host:<32} {srv.username:<16} "
                f"{'*' if name == cfg.default else ''}"
                for name, srv in cfg.servers.items()
            ]
            return "\n".join(lines) if lines else "No servers configured."
        except Exception as e:
            return f"ERROR: {e}"
