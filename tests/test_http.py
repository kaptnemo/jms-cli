# -*- coding: utf-8 -*-
"""Tests for jms.http — transport layer only; requests.Session is mocked."""

from unittest.mock import MagicMock

import pytest
import requests

from jms.exceptions import APIError, AuthError
from jms.core.http import RESTClient

BASE = "https://jump.example.com"


def _resp(status: int = 200, payload: object = None, text: str | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = text if text is not None else str(payload)
    return r


def _client() -> RESTClient:
    c = RESTClient(BASE)
    c.session = MagicMock()
    return c


def test_session_retries_transient_errors_for_all_methods() -> None:
    sess = RESTClient(BASE).session
    for scheme in ("https://", "http://"):
        retry = sess.adapters[scheme].max_retries
        assert retry.total == 3
        assert retry.backoff_factor == 0.5
        assert {502, 503, 504} <= set(retry.status_forcelist)
        assert retry.allowed_methods is None  # POST included
        assert retry.raise_on_status is False


def test_api_get_returns_parsed_json() -> None:
    c = _client()
    c.session.request.return_value = _resp(200, {"ok": True})

    assert c.api_get("/api/v1/ping/", params={"a": 1}) == {"ok": True}
    args, kwargs = c.session.request.call_args
    assert args[0] == "GET"
    assert args[1] == f"{BASE}/api/v1/ping/"
    assert kwargs["params"] == {"a": 1}


def test_api_verbs_map_to_http_methods() -> None:
    c = _client()
    c.session.request.return_value = _resp(200, {"ok": True})

    c.api_post("/api/v1/x/", {"k": "v"})
    c.api_patch("/api/v1/x/", {"k": "w"})
    c.api_delete("/api/v1/x/")

    methods = [call.args[0] for call in c.session.request.call_args_list]
    assert methods == ["POST", "PATCH", "DELETE"]
    assert c.session.request.call_args_list[0].kwargs["json"] == {"k": "v"}


def test_api_delete_204_returns_empty_dict() -> None:
    c = _client()
    c.session.request.return_value = _resp(204, text="")
    assert c.api_delete("/api/v1/x/") == {}


def test_401_raises_auth_error() -> None:
    c = _client()
    c.session.request.return_value = _resp(401, {"detail": "bad token"})

    with pytest.raises(AuthError, match="HTTP 401"):
        c.api_get("/api/v1/ping/")


@pytest.mark.parametrize("status", [403, 404, 500])
def test_non_2xx_raises_api_error_with_status(status: int) -> None:
    c = _client()
    c.session.request.return_value = _resp(status, {"detail": "boom"})

    with pytest.raises(APIError, match=f"HTTP {status}") as exc_info:
        c.api_get("/api/v1/ping/")
    assert exc_info.value.status_code == status


def test_network_error_raises_api_error_status_zero() -> None:
    c = _client()
    c.session.request.side_effect = requests.ConnectionError("refused")

    with pytest.raises(APIError) as exc_info:
        c.api_get("/api/v1/ping/")
    assert exc_info.value.status_code == 0


def test_non_json_response_raises_api_error() -> None:
    """A 200 with an HTML body (e.g. a captive portal) is not JSON."""
    c = _client()
    r = _resp(200, text="<html>not json</html>")
    r.json.side_effect = ValueError("Expecting value")
    c.session.request.return_value = r

    with pytest.raises(APIError, match="invalid JSON"):
        c.api_get("/api/v1/ping/")


def test_headers_carry_bearer_and_csrf_tokens() -> None:
    c = _client()
    c.bearer_token = "tok-x"
    c.csrf_token = "csrf-x"
    c.session.request.return_value = _resp(200, {})

    c.api_get("/api/v1/ping/")

    headers = c.session.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-x"
    assert headers["X-CSRFToken"] == "csrf-x"


def _paged_client(pages: dict[int, dict]) -> RESTClient:
    """Client whose transport serves one page per offset."""
    c = _client()
    c.session.request.side_effect = lambda method, url, **kw: _resp(
        200, pages[kw["params"]["offset"]]
    )
    return c


def test_get_all_walks_pages_until_count() -> None:
    c = _paged_client({
        0: {"count": 3, "results": [{"n": 1}, {"n": 2}]},
        2: {"count": 3, "results": [{"n": 3}]},
    })
    out = list(c.api_get_all("/api/v1/items/", page_size=2))

    assert out == [{"n": 1}, {"n": 2}, {"n": 3}]
    offsets = [
        call.kwargs["params"]["offset"]
        for call in c.session.request.call_args_list
    ]
    assert offsets == [0, 2]


def test_get_all_preserves_extra_params() -> None:
    c = _paged_client({0: {"count": 1, "results": [{"n": 1}]}})

    list(c.api_get_all("/api/v1/items/", params={"search": "web"}))

    params = c.session.request.call_args.kwargs["params"]
    assert params["search"] == "web"
    assert params["limit"] == 100


def test_get_all_stops_on_empty_page() -> None:
    c = _paged_client({0: {"count": 99, "results": []}})
    assert list(c.api_get_all("/api/v1/items/")) == []
    assert c.session.request.call_count == 1


def test_get_all_bare_list_response() -> None:
    """Pagination disabled server-side: the payload is a plain list."""
    c = _client()
    c.session.request.return_value = _resp(200, payload=[{"n": 1}, {"n": 2}])

    assert list(c.api_get_all("/api/v1/items/")) == [{"n": 1}, {"n": 2}]
    assert c.session.request.call_count == 1
