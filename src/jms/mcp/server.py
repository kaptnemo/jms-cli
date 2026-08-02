"""Local stdio MCP server exposing jms-cli capabilities as MCP tools.

Runs on the local machine and serves AI assistants / MCP clients over
stdio. Every tool creates its own authenticated session per invocation —
MCP tools are stateless, so no shared session state is kept between calls.
Only tools are exposed; no resources, prompts, or structured output.
Tools are split by capability into ``jms.mcp.tools`` submodules, each
registering its own tools on the server.

Usage::

    from jms.mcp.server import build_server, main

    build_server("path/to/config.yaml").run(transport="stdio")
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import server as mcpserver

from jms.mcp.tools import (
    assets,
    config as config_tools,
    exec as exec_tools,
    transfer as transfer_tools,
)


def build_server(config_path: str | None = None) -> mcpserver.MCPServer:
    """Create an MCPServer exposing jms-cli capabilities as tools.

    Args:
        config_path: Explicit config path used when a tool is not given
            its own ``config_path`` argument (e.g. from ``JMS_CONFIG``).

    Returns:
        A configured MCPServer ready for ``run(transport="stdio")``.
    """
    mcp_server = mcpserver.MCPServer(name="jms")
    config_tools.register_tools(mcp_server, config_path)
    assets.register_tools(mcp_server, config_path)
    exec_tools.register_tools(mcp_server, config_path)
    transfer_tools.register_tools(mcp_server, config_path)
    return mcp_server


def main(config_path: str | None = None) -> None:
    """Start the MCP stdio server (blocking).

    Args:
        config_path: Explicit config path, or None to fall back to the
            ``JMS_CONFIG`` env var and then the platform default.
    """
    if config_path is None:
        config_path = os.environ.get("JMS_CONFIG")
    build_server(config_path).run(transport="stdio")
