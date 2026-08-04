#!/usr/bin/env python3
"""Validate a jms-cli config.yaml against the v1.0 schema.

Usage:
    uv run python3 skills/jms-config/scripts/validate_config.py <path-to-config.yaml>

Exit code 0 = schema-valid, 1 = invalid, 2 = usage/import error.
Mirrors the checks in jms.config.parse_config: YAML parses as a mapping,
'servers' is a non-empty mapping, and every server has non-empty string
host/username/password (otp_secret optional). Run after hand-editing or
generating a config file -- never "eyeball" schema conformance.
"""

import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: run via 'uv run python3 ...' from the repo root")
    sys.exit(2)

ENC_PREFIX = "enc:v1:"


def validate(path: str) -> tuple[list[str], list[str]]:
    """Return (errors, notes); non-empty errors means the file is invalid."""
    errors: list[str] = []
    notes: list[str] = []
    p = Path(path)
    if not p.exists():
        return [f"config file not found: {p}"], notes
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"YAML parse failed: {exc}"], notes
    if raw is None:
        return ["config is empty (no content)"], notes
    if not isinstance(raw, dict):
        return [f"config must be a YAML mapping, got {type(raw).__name__}"], notes

    servers = raw.get("servers")
    if not servers or not isinstance(servers, dict):
        return ["'servers' must be a non-empty mapping"], notes

    for name, srv in servers.items():
        if not isinstance(srv, dict):
            errors.append(
                f"server '{name}' must be a mapping, got {type(srv).__name__}"
            )
            continue
        for field in ("host", "username", "password"):
            value = srv.get(field, "")
            if not isinstance(value, str) or not value:
                errors.append(
                    f"server '{name}' missing required string field '{field}'"
                )
        password = srv.get("password", "")
        if password and not password.startswith(ENC_PREFIX):
            notes.append(
                f"server '{name}': password is plaintext (accepted on read, "
                "but save_config() will encrypt it)"
            )
        if "otp_secret" in srv and srv["otp_secret"] is None:
            errors.append(f"server '{name}' otp_secret must be a string, got null")
    return errors, notes


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    errors, notes = validate(sys.argv[1])
    for note in notes:
        print(f"NOTE: {note}")
    if errors:
        for err in errors:
            print(f"ERROR: {err}")
        sys.exit(1)
    print(f"OK: {sys.argv[1]} matches the jms config v1.0 schema")
    sys.exit(0)
