"""Create an SFTP connection token for a resolved asset (jms-auth skill).

Real-server contract (validated against the live JumpServer, see
tests/test_integration.py::test_connection_token_contract):
SFTP rides on the asset's ssh protocol with ``sftp_enabled``, so the token
must use protocol="ssh" + connect_method="web_sftp". Passing
protocol="sftp" is REJECTED by this JumpServer version with
perm_account_invalid. The ssh/web_cli token is for exec/interactive
terminals only — pointing its SFTP subsystem at KoKo fails with
"please select one of the assets".
"""

from jms.core.auth import JMSSession
from jms.core.resources import AssetInfo
from jms.transport import create_connection_token


def create_sftp_token(session: JMSSession, asset: AssetInfo) -> dict:
    """Return a connection token usable for SFTP.

    Args:
        session: Authenticated JumpServer session.
        asset: Resolved asset (account must be the alias, e.g. '@USER').

    Returns:
        Token dict with ``id`` and ``value``. SSH auth on KoKo port 2222
        uses username ``JMS-{id}`` and password ``value``; SFTP shares the
        same token mechanism via the asset's ssh protocol.
    """
    return create_connection_token(
        session,
        asset,
        protocol="ssh",      # NOT "sftp": this version rejects it
        connect_method="web_sftp",  # enables SFTP on the ssh protocol
    )
