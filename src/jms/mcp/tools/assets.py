"""MCP tools: asset listing and resolution (``jms_ls``, ``jms_resolve_asset``)."""

from __future__ import annotations

from mcp.server.mcpserver import server as mcpserver

from jms.mcp.tools import _cfg, _get_server, _new_session


def register_tools(server: mcpserver.MCPServer, config_path: str | None = None) -> None:
    """Register the ``jms_ls`` and ``jms_resolve_asset`` tools on the server.

    Args:
        server: The MCP server to attach tools to.
        config_path: Default config path used when a tool call omits
            its own ``config_path`` argument.
    """
    default_config = config_path

    @server.tool(
        name="jms_ls",
        description="List or search authorized assets on a JumpServer.",
    )
    def jms_ls(
        server: str | None = None,
        keyword: str | None = None,
        config_path: str | None = None,
    ) -> str:
        """List assets; with ``keyword``, search instead."""
        from jms.core.resources import list_assets, search_assets

        try:
            srv = _get_server(_cfg(config_path, default_config), server)
            session = _new_session(srv)
            try:
                assets = search_assets(session, keyword) if keyword else list_assets(session)
                if not assets:
                    return "No assets found."
                lines = []
                for a in assets:
                    platform = a.get("platform", {})
                    pname = (
                        platform.get("name", "")
                        if isinstance(platform, dict) else str(platform)
                    )
                    lines.append(f"{a.get('name', '?')}  {a.get('address', '?')}  {pname}")
                return "\n".join(lines)
            finally:
                session.session.close()
        except Exception as e:
            return f"ERROR: {e}"

    @server.tool(
        name="jms_resolve_asset",
        description="Resolve an asset to its connection info (address, account, protocol).",
    )
    def jms_resolve_asset(
        asset: str,
        server: str | None = None,
        config_path: str | None = None,
    ) -> str:
        """Resolve an asset to its connection parameters."""
        from jms.core.resources import resolve_asset

        try:
            srv = _get_server(_cfg(config_path, default_config), server)
            session = _new_session(srv)
            try:
                info = resolve_asset(session, asset)
                return (
                    f"name: {info.name}\n"
                    f"address: {info.address}\n"
                    f"account: {info.account}\n"
                    f"protocol: {info.protocol}"
                )
            finally:
                session.session.close()
        except Exception as e:
            return f"ERROR: {e}"
