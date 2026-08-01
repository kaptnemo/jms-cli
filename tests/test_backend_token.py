# -*- coding: utf-8 -*-
"""Tests for jms.backend.token — api_post 全部 mock，不连真实服务器。"""

from unittest.mock import MagicMock

import pytest

from jms.assets import AssetInfo
from jms.backend.token import TOKEN_API_PATH, create_connection_token
from jms.exceptions import ConnectionTokenError

ASSET = AssetInfo(
    id="asset-uuid-1", name="web1", address="10.0.0.1",
    account="@USER", protocol="ssh",
)


def _session(token: dict | None = None) -> MagicMock:
    session = MagicMock()
    session.api_post.return_value = token or {"id": "tok-id", "value": "tok-val"}
    return session


def test_token_payload_fields() -> None:
    session = _session()

    token = create_connection_token(session, ASSET)

    assert token == {"id": "tok-id", "value": "tok-val"}
    path, payload = session.api_post.call_args.args
    assert path == TOKEN_API_PATH
    # protocol 固定 ssh、connect_method 固定 web_cli、account 用 alias
    assert payload == {
        "asset": "asset-uuid-1",
        "account": "@USER",
        "protocol": "ssh",
        "connect_method": "web_cli",
    }


def test_token_api_failure_raises() -> None:
    session = _session()
    session.api_post.side_effect = Exception("HTTP 403")

    with pytest.raises(ConnectionTokenError, match="connection token"):
        create_connection_token(session, ASSET)
