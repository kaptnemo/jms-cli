"""Connection-token creation, shared by the SSH and WebSocket terminals.

Both KoKo's SSH service (port 2222) and the WebSocket terminal
authenticate with a connection token: SSH uses ``JMS-{token_id}`` as
the username and ``token_value`` as the password; the WebSocket
terminal URL carries ``token_id`` directly. Token authentication
bypasses MFA.
"""

from jms.core.resources import AssetInfo
from jms.core.auth import JMSSession
from jms.exceptions import ConnectionTokenError

# KoKo SSH port (shared by terminal and SFTP)
KOKO_SSH_PORT: int = 2222

TOKEN_API_PATH: str = "/api/v1/authentication/connection-token/"


def create_connection_token(
    session: JMSSession,
    asset: AssetInfo,
    protocol: str = "ssh",
    connect_method: str = "web_cli",
) -> dict:
    """Create a connection token for a resolved asset.

    Defaults to ``protocol="ssh"``, ``connect_method="web_cli"``
    (exec / interactive shell); SFTP must pass ``protocol="sftp"``,
    ``connect_method="web_sftp"`` — an ssh/web_cli token's SFTP
    subsystem lands on the KoKo virtual root (failing with
    "please select one of the assets"), while a web_sftp token roots
    directly at the asset. ``account`` must be the account **alias**
    (e.g. '@USER'), not the display name.

    Args:
        session: Authenticated session.
        asset: Resolved asset.
        protocol: Token protocol (ssh/sftp).
        connect_method: Connect method (web_cli/web_sftp).

    Returns:
        Token dict with ``id`` and ``value`` fields.

    Raises:
        ConnectionTokenError: Token creation failed.
    """
    try:
        return session.api_post(
            TOKEN_API_PATH,
            {
                "asset": asset.id,
                "account": asset.account,
                "protocol": protocol,
                "connect_method": connect_method,
            },
        )
    except Exception as e:
        raise ConnectionTokenError(
            f"Failed to create connection token: {e}"
        ) from e
