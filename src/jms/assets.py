"""JumpServer asset lookup and connection-parameter resolution."""

from dataclasses import dataclass
from itertools import islice
from typing import Optional

from jms.auth import JMSSession
from jms.exceptions import AssetError


@dataclass(frozen=True)
class AssetInfo:
    """A resolved asset with its connection parameters.

    Attributes:
        id: Asset UUID.
        name: Asset display name.
        address: IP or hostname.
        account: Selected connection account alias.
        protocol: Selected protocol name.
        platform: Platform name (e.g. 'Linux').
        org_id: Organization UUID.
    """

    id: str
    name: str
    address: str
    account: str
    protocol: str
    platform: str = ""
    org_id: str = ""


def search_assets(session: JMSSession, keyword: str) -> list[dict]:
    """Search authorized assets by keyword, across all result pages.

    Args:
        session: Authenticated session.
        keyword: Search keyword (matches name/address/comment).

    Returns:
        Asset dicts returned by the API; empty list when nothing matches.
    """
    return list(session.api_get_all(
        "/api/v1/perms/users/self/assets/",
        params={"search": keyword},
    ))


def list_assets(session: JMSSession, limit: int = 50) -> list[dict]:
    """List authorized assets, capped at ``limit`` entries.

    Pages are fetched lazily, so no page beyond the cap is requested.

    Args:
        session: Authenticated session.
        limit: Maximum number of assets to return.

    Returns:
        Asset dicts, at most ``limit`` entries.
    """
    return list(islice(
        session.api_get_all("/api/v1/perms/users/self/assets/"),
        limit,
    ))


def get_asset_detail(session: JMSSession, asset_id: str) -> dict:
    """Fetch asset detail, including permed accounts and protocols.

    Args:
        session: Authenticated session.
        asset_id: Asset UUID.

    Returns:
        Asset detail dict containing permed_accounts and permed_protocols.
    """
    return session.api_get(
        f"/api/v1/perms/users/self/assets/{asset_id}/"
    )


def select_account(permed_accounts: list[dict]) -> str:
    """Select the best account alias: @USER > named account > @INPUT.

    The connection-token API requires the account **alias** (e.g.
    '@USER'), not the display name (e.g. 'Dynamic user').

    Args:
        permed_accounts: Permed account dicts as returned by the API.

    Returns:
        Account alias used to create the connection token.
    """
    if not permed_accounts:
        return "@INPUT"
    for acc in permed_accounts:
        if acc.get("alias", "") == "@USER":
            return "@USER"
    for acc in permed_accounts:
        alias = acc.get("alias", "")
        username = acc.get("username", "")
        if alias and not alias.startswith("@"):
            return alias
        if username and not username.startswith("@"):
            return username
    first = permed_accounts[0]
    return first.get("alias", first.get("username", "@INPUT"))


def select_protocol(permed_protocols: list[dict]) -> str:
    """Select the best protocol, preferring SSH.

    Args:
        permed_protocols: Permed protocol dicts as returned by the API.

    Returns:
        Protocol name.
    """
    if not permed_protocols:
        return "ssh"
    for proto in permed_protocols:
        if proto.get("name", "").lower() == "ssh":
            return "ssh"
    return permed_protocols[0].get("name", "ssh")


def resolve_asset(
    session: JMSSession,
    asset_name: str,
    account: Optional[str] = None,
    protocol: Optional[str] = None,
) -> AssetInfo:
    """Search an asset by name and resolve its connection parameters.

    An exact name match wins; otherwise the first search result is used.

    Args:
        session: Authenticated session.
        asset_name: Asset name to search for.
        account: Explicit account alias; auto-selected when None.
        protocol: Explicit protocol; auto-selected when None.

    Returns:
        A connectable AssetInfo.

    Raises:
        AssetError: No matching asset found.
    """
    assets = search_assets(session, asset_name)
    if not assets:
        raise AssetError(f"No asset found matching '{asset_name}'.")

    # Exact match wins
    target = None
    for a in assets:
        if a.get("name") == asset_name:
            target = a
            break
    if target is None:
        target = assets[0]

    asset_id = target["id"]
    detail = get_asset_detail(session, asset_id)

    permed_accounts = detail.get("permed_accounts", [])
    permed_protocols = detail.get("permed_protocols", [])

    sel_account = account or select_account(permed_accounts)
    sel_protocol = protocol or select_protocol(permed_protocols)

    platform_info = target.get("platform", {})
    platform_name = (
        platform_info.get("name", "") if isinstance(platform_info, dict)
        else str(platform_info)
    )

    return AssetInfo(
        id=asset_id,
        name=target.get("name", asset_name),
        address=target.get("address", ""),
        account=sel_account,
        protocol=sel_protocol,
        platform=platform_name,
        org_id=target.get("org_id", ""),
    )
