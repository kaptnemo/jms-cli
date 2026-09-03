"""Tests for jms.config.session — the persisted-session cache."""

import os
import stat

from requests.cookies import RequestsCookieJar

from jms.config import ServerConfig
from jms.config import session as session_mod
from jms.config.session import (
    clear_session,
    deserialize_cookies,
    load_session,
    save_session,
    serialize_cookies,
)


def _server(host: str = "jump.example.com", username: str = "alice") -> ServerConfig:
    return ServerConfig(
        name="prod", host=host, username=username, password="pw",
    )


def _jar() -> RequestsCookieJar:
    jar = RequestsCookieJar()
    jar.set("jms_sessionid", "sid123", domain="jump.example.com", path="/")
    jar.set("jms_csrftoken", "csrf123", domain="jump.example.com", path="/")
    return jar


def test_save_load_roundtrip() -> None:
    save_session(_server(), _jar(), "tok-abc", "csrf-x")

    data = load_session(_server())
    assert data is not None
    assert data["bearer_token"] == "tok-abc"
    assert data["csrf_token"] == "csrf-x"

    jar = deserialize_cookies(data["cookies"])
    assert jar.get("jms_sessionid", domain="jump.example.com") == "sid123"
    assert jar.get("jms_csrftoken", domain="jump.example.com") == "csrf123"


def test_cookie_domain_path_preserved() -> None:
    jar = RequestsCookieJar()
    jar.set("k", "v", domain="jump.example.com", path="/core")
    items = serialize_cookies(jar)
    restored = deserialize_cookies(items)
    cookie = list(restored)[0]
    assert cookie.domain == "jump.example.com"
    assert cookie.path == "/core"


def test_load_missing_file_returns_none() -> None:
    assert load_session(_server()) is None


def test_load_host_mismatch_returns_none() -> None:
    save_session(_server(), _jar(), "tok", "csrf")
    assert load_session(_server(host="other.example.com")) is None


def test_load_username_mismatch_returns_none() -> None:
    save_session(_server(), _jar(), "tok", "csrf")
    assert load_session(_server(username="bob")) is None


def test_clear_session_removes_entry() -> None:
    save_session(_server(), _jar(), "tok", "csrf")
    assert load_session(_server()) is not None
    clear_session(_server())
    assert load_session(_server()) is None


def test_clear_session_missing_is_noop() -> None:
    clear_session(_server())  # must not raise


def test_session_file_written_0600() -> None:
    save_session(_server(), _jar(), "tok", "csrf")
    mode = stat.S_IMODE(os.stat(session_mod.session_file_path()).st_mode)
    assert mode == 0o600


def test_payload_is_encrypted() -> None:
    save_session(_server(), _jar(), "tok-abc", "csrf-x")
    content = session_mod.session_file_path().read_text(encoding="utf-8")
    assert "sid123" not in content
    assert "tok-abc" not in content
    assert "enc:v1:" in content
