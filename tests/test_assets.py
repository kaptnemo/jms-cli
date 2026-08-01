# -*- coding: utf-8 -*-
"""Tests for jms.assets — JMSSession is mocked, no real server."""

from unittest.mock import MagicMock

import pytest

from jms.assets import (
    AssetInfo,
    get_asset_detail,
    list_assets,
    resolve_asset,
    search_assets,
    select_account,
    select_protocol,
)
from jms.exceptions import AssetError
from jms.http import RESTClient

ASSET_SRV1 = {
    "id": "uuid-1",
    "name": "home_server",
    "address": "192.168.31.78",
    "platform": {"name": "Linux"},
    "org_id": "org-1",
}
ASSET_SRV2 = {
    "id": "uuid-2",
    "name": "db_server",
    "address": "10.0.0.2",
    "platform": "Linux",
    "org_id": "org-1",
}
DETAIL = {
    "permed_accounts": [
        {"alias": "@USER", "username": "dynamic", "name": "Dynamic user"},
        {"alias": "gcszhn", "username": "gcszhn", "name": "gcszhn"},
    ],
    "permed_protocols": [
        {"name": "ssh", "port": 22},
        {"name": "sftp", "port": 22},
    ],
}


def _session(responses: dict[str, dict]) -> MagicMock:
    """Mock JMSSession: path → api_get 返回的 payload。"""
    sess = MagicMock()
    sess.api_get.side_effect = lambda path, params=None: responses[path]

    def _get_all(path: str, params: dict | None = None, page_size: int = 100):
        data = responses[path]
        return iter(data.get("results", []) if isinstance(data, dict) else data)

    sess.api_get_all.side_effect = _get_all
    return sess


def test_search_assets_parses_results() -> None:
    sess = MagicMock()
    sess.api_get_all.return_value = iter([ASSET_SRV1, ASSET_SRV2])
    out = search_assets(sess, "server")
    assert out == [ASSET_SRV1, ASSET_SRV2]
    _, kwargs = sess.api_get_all.call_args
    assert kwargs["params"] == {"search": "server"}


def test_search_assets_empty() -> None:
    sess = MagicMock()
    sess.api_get_all.return_value = iter([])
    assert search_assets(sess, "nope") == []


def test_list_assets() -> None:
    sess = MagicMock()
    sess.api_get_all.return_value = iter([ASSET_SRV1, ASSET_SRV2])
    assert list_assets(sess, limit=1) == [ASSET_SRV1]


def test_get_asset_detail() -> None:
    sess = _session({"/api/v1/perms/users/self/assets/uuid-1/": DETAIL})
    assert get_asset_detail(sess, "uuid-1") == DETAIL


def test_select_account_prefers_user_alias() -> None:
    assert select_account(DETAIL["permed_accounts"]) == "@USER"


def test_select_account_named_when_no_user_alias() -> None:
    accounts = [{"alias": "gcszhn", "username": "gcszhn"}]
    assert select_account(accounts) == "gcszhn"


def test_select_account_empty_falls_back_to_input() -> None:
    assert select_account([]) == "@INPUT"


def test_select_account_falls_back_to_username_when_alias_missing() -> None:
    assert select_account([{"alias": "", "username": "bob"}]) == "bob"


def test_select_account_falls_back_to_first_entry() -> None:
    accounts = [{"alias": "@ANON", "username": ""}]
    assert select_account(accounts) == "@ANON"


def test_select_protocol_prefers_ssh() -> None:
    protos = [{"name": "rdp"}, {"name": "SSH"}]
    assert select_protocol(protos) == "ssh"
    assert select_protocol([{"name": "rdp"}]) == "rdp"
    assert select_protocol([]) == "ssh"


def test_resolve_asset_exact_match() -> None:
    sess = _session({
        "/api/v1/perms/users/self/assets/": {
            "results": [ASSET_SRV2, ASSET_SRV1],
        },
        "/api/v1/perms/users/self/assets/uuid-1/": DETAIL,
    })
    info = resolve_asset(sess, "home_server")

    assert isinstance(info, AssetInfo)
    assert info.id == "uuid-1"
    assert info.name == "home_server"
    assert info.address == "192.168.31.78"
    assert info.account == "@USER"
    assert info.protocol == "ssh"
    assert info.platform == "Linux"
    assert info.org_id == "org-1"


def test_resolve_asset_first_result_when_no_exact_match() -> None:
    sess = _session({
        "/api/v1/perms/users/self/assets/": {"results": [ASSET_SRV2]},
        "/api/v1/perms/users/self/assets/uuid-2/": DETAIL,
    })
    info = resolve_asset(sess, "db")
    assert info.id == "uuid-2"


def test_resolve_asset_overrides() -> None:
    sess = _session({
        "/api/v1/perms/users/self/assets/": {"results": [ASSET_SRV1]},
        "/api/v1/perms/users/self/assets/uuid-1/": DETAIL,
    })
    info = resolve_asset(sess, "home_server", account="gcszhn", protocol="sftp")
    assert info.account == "gcszhn"
    assert info.protocol == "sftp"


def test_resolve_asset_not_found() -> None:
    sess = _session({
        "/api/v1/perms/users/self/assets/": {"results": []},
    })
    with pytest.raises(AssetError, match="No asset found"):
        resolve_asset(sess, "ghost")


def _paged_rest_client(pages: dict[int, dict]) -> RESTClient:
    """Real RESTClient whose transport serves one search page per offset."""
    client = RESTClient("https://jump.example.com")
    client.session = MagicMock()

    def _request(method: str, url: str, **kw) -> MagicMock:
        r = MagicMock()
        r.status_code = 200
        if url.endswith("/uuid-1/"):
            r.json.return_value = DETAIL
        else:
            r.json.return_value = pages[kw["params"]["offset"]]
        r.text = str(r.json.return_value)
        return r

    client.session.request.side_effect = _request
    return client


def test_search_assets_spans_all_pages() -> None:
    """Regression: results beyond the first page must not be dropped."""
    page1 = [dict(ASSET_SRV2, id=f"uuid-x{i}", name=f"server-{i}") for i in range(2)]
    client = _paged_rest_client({
        0: {"count": 3, "results": page1},
        2: {"count": 3, "results": [ASSET_SRV1]},
    })
    out = search_assets(client, "server")  # type: ignore[arg-type]
    assert ASSET_SRV1 in out
    assert len(out) == 3


def test_resolve_asset_exact_match_on_later_page() -> None:
    """Regression: exact match past page 1 previously lost to assets[0]."""
    filler = dict(ASSET_SRV2, id="uuid-2", name="server-aa")
    client = _paged_rest_client({
        0: {"count": 2, "results": [filler]},
        1: {"count": 2, "results": [ASSET_SRV1]},
    })
    # page_size=1 forces the exact match onto the second page
    infos = list(client.api_get_all(
        "/api/v1/perms/users/self/assets/",
        params={"search": "home_server"},
        page_size=1,
    ))
    assert infos == [filler, ASSET_SRV1]

    info = resolve_asset(client, "home_server")  # type: ignore[arg-type]
    assert info.id == "uuid-1"
    assert info.name == "home_server"
