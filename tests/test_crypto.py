# -*- coding: utf-8 -*-
"""Tests for jms.crypto."""

import pytest

from jms.config.crypto import ENC_PREFIX, decrypt, encrypt, is_encrypted

HOST = "jump.example.com"
USER = "alice"


def test_round_trip() -> None:
    ct = encrypt("s3cret-pw", HOST, USER)
    assert ct.startswith(ENC_PREFIX)
    assert "s3cret-pw" not in ct
    assert decrypt(ct, HOST, USER) == "s3cret-pw"


def test_empty_plaintext() -> None:
    assert encrypt("", HOST, USER) == ""
    assert decrypt("", HOST, USER) == ""


def test_decrypt_plaintext_passthrough() -> None:
    assert decrypt("plain-pw", HOST, USER) == "plain-pw"


def test_wrong_key_fails() -> None:
    ct = encrypt("s3cret-pw", HOST, USER)
    with pytest.raises(ValueError, match="Decryption failed"):
        decrypt(ct, HOST, "bob")


def test_tampered_ciphertext_fails() -> None:
    ct = encrypt("s3cret-pw", HOST, USER)
    tampered = ct[:-4] + "AAAA"
    with pytest.raises(ValueError):
        decrypt(tampered, HOST, USER)


def test_is_encrypted() -> None:
    assert is_encrypted(encrypt("x", HOST, USER))
    assert not is_encrypted("plain")
    assert not is_encrypted("")


def test_deterministic_key() -> None:
    """Same (host, username) decrypts across calls (different IVs)."""
    a = encrypt("pw", HOST, USER)
    b = encrypt("pw", HOST, USER)
    assert a != b  # random IV
    assert decrypt(a, HOST, USER) == decrypt(b, HOST, USER) == "pw"
