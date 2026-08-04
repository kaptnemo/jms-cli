"""Create an SFTP connection token for a resolved asset."""

from jms.core.auth import JMSSession
from jms.core.resources import AssetInfo
from jms.transport import create_connection_token


def create_sftp_token(session: JMSSession, asset: AssetInfo) -> dict:
    """Return an SFTP connection token (protocol sftp / web_sftp)."""
    return create_connection_token(
        session,
        asset,
        protocol="sftp",
        connect_method="web_sftp",
    )
