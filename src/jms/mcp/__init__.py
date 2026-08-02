"""MCP adapter for jms-cli (stdio server + per-capability tools).

Re-exports the public entry points from ``jms.mcp.server``.
"""

from jms.mcp.server import build_server, main

__all__ = ["build_server", "main"]
