"""Session persistence: cache a logged-in session across CLI invocations.

JumpServer login is dual-auth (API Bearer token + Django form-login session
cookie), and may involve an interactive MFA prompt. Running it on every
command is slow and annoying, so after a successful login the session state
(cookie jar + bearer token + csrf token) is cached to disk keyed by server.

The cached payload is AES-256-GCM encrypted with the same server-derived key
used for passwords (``host`` + ``username`` via ``crypto``), and the file is
written 0600. On the next run ``JMSSession.login`` restores the cached state
and validates it with a cheap authenticated REST probe before reusing it.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
from pathlib import Path
from typing import Optional

import yaml

from jms.config.config import config_dir
from jms.config.crypto import decrypt, encrypt, is_encrypted

# Schema version for session.yaml.
SESSION_VERSION: float = 1.0


def session_file_path() -> Path:
    """Return the session cache file path (platformdirs-resolved)."""
    return config_dir() / "session.yaml"


def serialize_cookies(jar) -> list[dict]:
    """Convert a cookie jar into a list of attribute dicts (round-trippable)."""
    return [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "expires": c.expires,
            "secure": bool(c.secure),
        }
        for c in jar
    ]


def deserialize_cookies(items: list[dict]):
    """Rebuild a ``RequestsCookieJar`` from ``serialize_cookies`` output.

    A plain ``cookiejar_from_dict`` round-trip drops the domain/path scope,
    which silently stops requests from sending the cookie back. Reconstructing
    full ``Cookie`` objects preserves those attributes.
    """
    from requests.cookies import RequestsCookieJar

    jar = RequestsCookieJar()
    for d in items:
        jar.set_cookie(_cookie_from_dict(d))
    return jar


def _cookie_from_dict(d: dict) -> http.cookiejar.Cookie:
    domain = d.get("domain") or ""
    return http.cookiejar.Cookie(
        version=0,
        name=d.get("name", ""),
        value=d.get("value", ""),
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=d.get("path") or "/",
        path_specified=True,
        secure=bool(d.get("secure")),
        expires=d.get("expires"),
        discard=False,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def _read_servers() -> dict:
    path = session_file_path()
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    servers = raw.get("servers") or {}
    return servers if isinstance(servers, dict) else {}


def _write_servers(servers: dict) -> None:
    path = session_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_dump(
        {"version": SESSION_VERSION, "servers": servers},
        allow_unicode=True, sort_keys=False, default_flow_style=False,
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(data)


def save_session(server, jar, bearer_token: str, csrf_token: str) -> None:
    """Persist a session for ``server`` (encrypted, 0600).

    Args:
        server: ``ServerConfig`` (provides name/host/username).
        jar: Cookie jar carrying the ``jms_sessionid`` cookie.
        bearer_token: REST Bearer token.
        csrf_token: CSRF token for mutating API calls.
    """
    payload = json.dumps({
        "cookies": serialize_cookies(jar),
        "bearer_token": bearer_token,
        "csrf_token": csrf_token,
    })
    cipher = encrypt(payload, server.host, server.username)
    servers = _read_servers()
    servers[server.name] = {
        "host": server.host,
        "username": server.username,
        "session": cipher,
    }
    _write_servers(servers)


def load_session(server) -> Optional[dict]:
    """Load a cached session for ``server``, or None if missing/invalid.

    Only a cache entry whose ``host`` and ``username`` match the current
    ``server`` is returned (so re-adding a server under the same alias with
    different credentials never resurrects a stale session).

    Returns:
        ``{"cookies": [...], "bearer_token": str, "csrf_token": str}`` or
        None when absent, mismatched, or undecryptable.
    """
    servers = _read_servers()
    entry = servers.get(server.name)
    if not isinstance(entry, dict):
        return None
    if entry.get("host") != server.host or entry.get("username") != server.username:
        return None
    cipher = entry.get("session") or ""
    if not is_encrypted(cipher):
        return None
    try:
        payload = decrypt(cipher, server.host, server.username)
        data = json.loads(payload)
    except (ValueError, json.JSONDecodeError, TypeError):
        return None
    return {
        "cookies": data.get("cookies") or [],
        "bearer_token": data.get("bearer_token") or "",
        "csrf_token": data.get("csrf_token") or "",
    }


def clear_session(server) -> None:
    """Remove the cached session for ``server`` (no-op if absent)."""
    servers = _read_servers()
    if server.name not in servers:
        return
    del servers[server.name]
    _write_servers(servers)
