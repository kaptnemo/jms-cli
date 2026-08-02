"""MCP tools: SFTP file transfer (``jms_sftp_upload``, ``jms_sftp_download``,
``jms_sftp_relay``).
"""

from __future__ import annotations

from mcp.server.mcpserver import server as mcpserver

from jms.mcp.tools import _cfg, _get_server


def register_tools(server: mcpserver.MCPServer, config_path: str | None = None) -> None:
    """Register the SFTP transfer tools on the given MCP server.

    Args:
        server: The MCP server to attach tools to.
        config_path: Default config path used when a tool call omits
            its own ``config_path`` argument.
    """
    default_config = config_path

    def _sftp(
        asset: str,
        src: str,
        dst: str,
        is_upload: bool,
        server: str | None,
        config_path: str | None,
    ) -> str:
        """Upload or download one file via ``jms.io.service.sftp_transfer``."""
        from jms.io.service import sftp_transfer
        from jms.io.transfer import TransferSpec

        try:
            srv = _get_server(_cfg(config_path, default_config), server)
            spec = TransferSpec(
                asset=asset, server=server,
                remote_path=dst if is_upload else src,
                local_path=src if is_upload else dst,
                is_upload=is_upload,
            )
            sftp_transfer(srv, spec, on_status=None)
            verb = "uploaded" if is_upload else "downloaded"
            if is_upload:
                return f"OK: {verb} {src} -> {asset}:{dst}"
            return f"OK: {verb} {asset}:{src} -> {dst}"
        except Exception as e:
            return f"ERROR: {e}"

    @server.tool(
        name="jms_sftp_upload",
        description="Upload a local file to an asset via SFTP.",
    )
    def jms_sftp_upload(
        src: str,
        asset: str,
        dst: str,
        server: str | None = None,
        config_path: str | None = None,
    ) -> str:
        """Upload local ``src`` to ``dst`` on ``asset`` via SFTP."""
        return _sftp(asset, src, dst, True, server, config_path)

    @server.tool(
        name="jms_sftp_download",
        description="Download a file from an asset to the local machine via SFTP.",
    )
    def jms_sftp_download(
        asset: str,
        src: str,
        dst: str,
        server: str | None = None,
        config_path: str | None = None,
    ) -> str:
        """Download ``src`` from ``asset`` to local ``dst`` via SFTP."""
        return _sftp(asset, src, dst, False, server, config_path)

    @server.tool(
        name="jms_sftp_relay",
        description="Relay a file between two assets (streamed, no local disk).",
    )
    def jms_sftp_relay(
        src_spec: str,
        dst_spec: str,
        config_path: str | None = None,
    ) -> str:
        """Transfer between two remote specs like ``asset[@server]:path``."""
        from jms.io.service import relay_transfer
        from jms.io.transfer import RelaySpec, parse_transfer_spec

        try:
            spec = parse_transfer_spec(src_spec, dst_spec)
            if not isinstance(spec, RelaySpec):
                return (
                    "ERROR: both src_spec and dst_spec must be "
                    "remote <asset>[@server]:<path>"
                )
            relay_transfer(spec, _cfg(config_path, default_config), on_status=None)
            return f"OK: relayed {src_spec} -> {dst_spec}"
        except Exception as e:
            return f"ERROR: {e}"
