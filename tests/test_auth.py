# -*- coding: utf-8 -*-
"""Tests for jms.auth — all HTTP traffic is mocked, no real server."""

from unittest.mock import MagicMock

import pyotp
import pytest

from jms.core.auth import JMSSession
from jms.config import ServerConfig
from jms.exceptions import APIError, AuthError, MFARequired

BASE = "https://jump.example.com"
AUTH_URL = f"{BASE}/api/v1/authentication/auth/"
MFA_URL = f"{BASE}/api/v1/authentication/mfa/challenge/"
LOGIN_URL = f"{BASE}/core/auth/login/"


def _server(otp_secret: str = "") -> ServerConfig:
    return ServerConfig(
        name="prod", host="jump.example.com",
        username="alice", password="pw", otp_secret=otp_secret,
    )


def _resp(status: int = 200, payload: dict | None = None) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.text = str(payload)
    return r


def _mock_session() -> MagicMock:
    """A MagicMock standing in for requests.Session (cookies as dict)."""
    sess = MagicMock()
    sess.cookies = {}
    return sess


def test_login_success_password_only() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess

    post_calls: list = []

    def _post(url: str, **kw) -> MagicMock:
        post_calls.append(url)
        if url == AUTH_URL:
            return _resp(201, {"token": "tok-abc"})
        # form login POST
        sess.cookies["jms_sessionid"] = "sid123"
        return _resp()

    def _get(url: str, **kw) -> MagicMock:
        sess.cookies.setdefault("jms_csrftoken", "csrf123")
        return _resp()

    sess.post.side_effect = _post
    sess.get.side_effect = _get

    jms.login()

    assert jms.bearer_token == "tok-abc"
    assert jms.session_id == "sid123"
    assert jms.is_authenticated
    assert jms.csrf_token == "csrf123"
    assert AUTH_URL in post_calls and LOGIN_URL in post_calls


def test_login_mfa_with_totp_secret() -> None:
    secret = pyotp.random_base32()
    jms = JMSSession(_server(otp_secret=secret))
    sess = _mock_session()
    jms.session = sess

    auth_calls: list = []
    mfa_payloads: list = []

    def _post(url: str, **kw) -> MagicMock:
        if url == AUTH_URL:
            auth_calls.append(1)
            if len(auth_calls) == 1:
                return _resp(200, {"code": "mfa_required"})
            return _resp(201, {"token": "tok-mfa"})
        if url == MFA_URL:
            mfa_payloads.append(kw["json"])
            return _resp(200, {})
        sess.cookies["jms_sessionid"] = "sid123"
        return _resp()

    sess.post.side_effect = _post
    sess.get.side_effect = lambda url, **kw: _resp()

    jms.login()

    assert jms.bearer_token == "tok-mfa"
    assert len(auth_calls) == 2  # initial + retry after MFA
    assert len(mfa_payloads) == 1
    assert mfa_payloads[0]["type"] == "otp"
    expected = pyotp.TOTP(secret).now()
    assert mfa_payloads[0]["code"] == expected


def test_login_mfa_interactive_prompt() -> None:
    jms = JMSSession(_server(), otp_prompt=lambda: "654321")
    sess = _mock_session()
    jms.session = sess

    auth_calls: list = []
    mfa_payloads: list = []

    def _post(url: str, **kw) -> MagicMock:
        if url == AUTH_URL:
            auth_calls.append(1)
            if len(auth_calls) == 1:
                # 另一版本字段：error 而不是 code
                return _resp(200, {"error": "mfa_required"})
            return _resp(201, {"token": "tok-prompt"})
        if url == MFA_URL:
            mfa_payloads.append(kw["json"])
            return _resp(200, {})
        sess.cookies["jms_sessionid"] = "sid123"
        return _resp()

    sess.post.side_effect = _post
    sess.get.side_effect = lambda url, **kw: _resp()

    jms.login()

    assert jms.bearer_token == "tok-prompt"
    assert mfa_payloads[0]["code"] == "654321"


def test_login_mfa_empty_prompt_raises() -> None:
    jms = JMSSession(_server(), otp_prompt=lambda: "")
    sess = _mock_session()
    jms.session = sess
    sess.post.side_effect = lambda url, **kw: _resp(200, {"code": "mfa_required"})

    with pytest.raises(MFARequired):
        jms.login()


def test_login_wrong_password() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.post.side_effect = lambda url, **kw: _resp(
        400, {"msg": "username or password error"}
    )

    with pytest.raises(AuthError, match="API login failed"):
        jms.login()
    assert not jms.is_authenticated


def test_login_mfa_challenge_rejected() -> None:
    jms = JMSSession(_server(otp_secret=pyotp.random_base32()))
    sess = _mock_session()
    jms.session = sess

    def _post(url: str, **kw) -> MagicMock:
        if url == AUTH_URL:
            return _resp(200, {"code": "mfa_required"})
        if url == MFA_URL:
            return _resp(400, {"msg": "otp code invalid"})
        return _resp()

    sess.post.side_effect = _post

    with pytest.raises(AuthError, match="MFA failed"):
        jms.login()


def test_form_login_no_cookie_raises() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    # API 登录成功，但表单登录不种 session cookie
    sess.post.side_effect = lambda url, **kw: (
        _resp(201, {"token": "tok"}) if url == AUTH_URL else _resp()
    )
    sess.get.side_effect = lambda url, **kw: _resp()

    with pytest.raises(AuthError, match="no session cookie"):
        jms.login()


def test_api_get_sets_bearer_and_csrf_headers() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    jms.bearer_token = "tok-x"
    jms.csrf_token = "csrf-x"
    sess.request.return_value = _resp(200, {"ok": True})

    out = jms.api_get("/api/v1/ping/", params={"a": 1})

    assert out == {"ok": True}
    headers = sess.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer tok-x"
    assert headers["X-CSRFToken"] == "csrf-x"


def test_api_post_error_raises_api_error() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.request.return_value = _resp(500, {"detail": "boom"})

    with pytest.raises(APIError, match="HTTP 500") as exc_info:
        jms.api_post("/api/v1/x/", {"k": "v"})
    assert exc_info.value.status_code == 500


def _html_resp(status: int) -> MagicMock:
    """502 之类的非 JSON 响应（nginx 错误页）。"""
    r = MagicMock()
    r.status_code = status
    r.json.side_effect = ValueError("Expecting value")
    r.text = "<html>Bad Gateway</html>"
    return r


def test_login_non_json_response_raises_auth_error() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.post.return_value = _html_resp(502)

    with pytest.raises(AuthError, match="invalid JSON"):
        jms.login()


def test_api_get_non_json_raises_api_error() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.request.return_value = _html_resp(200)

    with pytest.raises(APIError, match="invalid JSON"):
        jms.api_get("/api/v1/ping/")


def test_api_get_network_error_raises_api_error() -> None:
    import requests

    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.request.side_effect = requests.ConnectionError("refused")

    with pytest.raises(APIError, match="API GET") as exc_info:
        jms.api_get("/api/v1/ping/")
    assert exc_info.value.status_code == 0


def test_api_get_403_raises_api_error() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.request.return_value = _resp(403, {"detail": "forbidden"})

    with pytest.raises(APIError, match="HTTP 403"):
        jms.api_get("/api/v1/ping/")


def test_api_get_401_raises_auth_error() -> None:
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.request.return_value = _resp(401, {"detail": "token expired"})

    with pytest.raises(AuthError, match="HTTP 401"):
        jms.api_get("/api/v1/ping/")


def test_login_mfa_without_secret_or_prompt_raises() -> None:
    """Library default: no stdin hijack, MFARequired surfaces instead."""
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.post.side_effect = lambda url, **kw: _resp(200, {"code": "mfa_required"})

    with pytest.raises(MFARequired):
        jms.login()


def test_login_reuses_cached_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid cached session skips the full login (no POST/GET)."""
    monkeypatch.setattr(
        "jms.config.session.load_session",
        lambda server: {
            "cookies": [{
                "name": "jms_sessionid", "value": "sid-cached",
                "domain": "jump.example.com", "path": "/",
                "expires": None, "secure": False,
            }],
            "bearer_token": "tok-cached",
            "csrf_token": "csrf-cached",
        },
    )
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.request.return_value = _resp(200, {"count": 0, "results": []})

    jms.login()

    assert jms.is_authenticated
    assert jms.bearer_token == "tok-cached"
    assert sess.post.called is False  # full login skipped
    assert sess.get.called is False


def test_login_expired_cache_relogs_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """An expired cached session is cleared and a full login runs."""
    monkeypatch.setattr(
        "jms.config.session.load_session",
        lambda server: {"cookies": [], "bearer_token": "tok-stale", "csrf_token": ""},
    )
    cleared: list = []
    monkeypatch.setattr(
        "jms.config.session.clear_session",
        lambda server: cleared.append(server.name),
    )
    jms = JMSSession(_server())
    sess = _mock_session()
    jms.session = sess
    sess.request.return_value = _resp(401, {"detail": "token expired"})

    def _post(url: str, **kw) -> MagicMock:
        if url == AUTH_URL:
            return _resp(201, {"token": "tok-new"})
        sess.cookies["jms_sessionid"] = "sid-new"
        return _resp()

    sess.post.side_effect = _post
    sess.get.side_effect = lambda url, **kw: _resp()

    jms.login()

    assert jms.bearer_token == "tok-new"
    assert cleared == ["prod"]
    assert jms.is_authenticated
