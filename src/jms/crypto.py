"""Credential encryption using server-derived keys.

Derives an AES-256 key from the server host + username using
PBKDF2-HMAC-SHA256 (no external deps — uses stdlib hashlib).
Encrypts/decrypts sensitive fields (password, otp_secret) with
AES-GCM via the ``cryptography`` library.

The key derivation is deterministic: same (host, username) always
produces the same key, so credentials can be decrypted on any
machine with the same config context.

Threat model: host and username are stored in plaintext alongside the
ciphertext in the same config file, so anyone who can read the file can
derive the key offline. This mechanism is obfuscation at rest only —
real confidentiality relies on the config file's 0600 permissions.
"""

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Salt prefix — combined with host+username for uniqueness
_SALT_PREFIX: bytes = b"jms-credential-v1:"

# PBKDF2 iterations
_KDF_ITERATIONS: int = 200_000

# Encrypted value prefix marker
ENC_PREFIX: str = "enc:v1:"


def _derive_key(host: str, username: str) -> bytes:
    """Derive a 32-byte AES key from server host and username.

    Args:
        host: Server host string (used as part of salt).
        username: Username string for key derivation.

    Returns:
        32-byte derived key.
    """
    salt = _SALT_PREFIX + f"{host}:{username}".encode("utf-8")
    return hashlib.pbkdf2_hmac(
        "sha256",
        password=f"{host}|{username}".encode("utf-8"),
        salt=salt,
        iterations=_KDF_ITERATIONS,
        dklen=32,
    )


def encrypt(plaintext: str, host: str, username: str) -> str:
    """Encrypt a plaintext string.

    Returns a string prefixed with ``enc:v1:`` containing the
    base64-encoded IV+ciphertext+tag.

    Args:
        plaintext: String to encrypt.
        host: Server host for key derivation.
        username: Username for key derivation.

    Returns:
        Encrypted string with prefix, or empty string for empty input.
    """
    if not plaintext:
        return ""

    key = _derive_key(host, username)
    iv = os.urandom(12)  # 96-bit nonce for AES-GCM

    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(iv, plaintext.encode("utf-8"), None)

    # iv (12) + ciphertext+tag
    payload = base64.b64encode(iv + ct).decode("ascii")
    return f"{ENC_PREFIX}{payload}"


def decrypt(encrypted: str, host: str, username: str) -> str:
    """Decrypt an encrypted string.

    Args:
        encrypted: String with ``enc:v1:`` prefix (or plaintext).
        host: Server host for key derivation.
        username: Username for key derivation.

    Returns:
        Decrypted plaintext (or input unchanged if not encrypted).

    Raises:
        ValueError: If decryption fails.
    """
    if not encrypted or not encrypted.startswith(ENC_PREFIX):
        return encrypted  # Not encrypted, return as-is

    payload = encrypted[len(ENC_PREFIX):]
    raw = base64.b64decode(payload)
    iv = raw[:12]
    ct = raw[12:]

    key = _derive_key(host, username)

    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(iv, ct, None)
    except Exception as e:
        raise ValueError(f"Decryption failed: {e}") from e

    return plaintext.decode("utf-8")


def is_encrypted(value: str) -> bool:
    """Check if a value is encrypted."""
    return value.startswith(ENC_PREFIX)
