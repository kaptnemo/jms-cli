"""JumpServer REST connection layer: transport, auth, and asset resolution.

Public names re-exported here keep ``from jms.core import ...`` stable
regardless of the internal module layout.
"""

from jms.core.auth import JMSSession
from jms.core.http import HTTP_TIMEOUT, RESTClient
from jms.core.resources import (
    AssetInfo,
    get_asset_detail,
    list_assets,
    resolve_asset,
    search_assets,
    select_account,
    select_protocol,
)

__all__ = [
    "AssetInfo",
    "HTTP_TIMEOUT",
    "JMSSession",
    "RESTClient",
    "get_asset_detail",
    "list_assets",
    "resolve_asset",
    "search_assets",
    "select_account",
    "select_protocol",
]
