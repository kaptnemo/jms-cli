# Credential crypto details

## Algorithm and parameters (matches `jms/config/crypto.py`)

| Item | Value |
|---|---|
| Cipher | AES-256-GCM (`cryptography.hazmat.primitives.ciphers.aead.AESGCM`) |
| Key derivation | PBKDF2-HMAC-SHA256, 200,000 iterations, 32-byte output |
| PBKDF2 password | `f"{host}|{username}"` (UTF-8) |
| PBKDF2 salt | `b"jms-credential-v1:" + f"{host}:{username}".encode()` |
| IV | `os.urandom(12)` (96-bit nonce) |
| Ciphertext format | `enc:v1:` + base64(IV + ciphertext+tag) |
| Prefix constant | `ENC_PREFIX = "enc:v1:"` |

## Detection and read/write behavior

- `is_encrypted(value)`: True when the value starts with `enc:v1:`.
- `encrypt(plaintext, host, username)`: empty input returns empty string;
  otherwise returns the prefixed base64 string.
- `decrypt(value, host, username)`: values without the prefix pass through
  unchanged; ciphertext that fails to decrypt raises `ValueError` (surfaced as
  `ConfigError: credential decryption failed` by `config.py`).
- `save_config()`: creates via `os.open(..., 0o600)` then `os.fchmod(0o600)` as
  a fallback; an empty `otp_secret` is stored as an empty string.

## Key derivation is deterministic

The same (host, username) derives the same key on any machine, so a config.yaml
can be migrated between machines without changing the ciphertext. Note the
threat model: host/username are stored in plaintext next to the ciphertext, so
anyone who can read the file can derive the key offline — encryption is
obfuscation at rest; real confidentiality relies on the 0600 file mode.

## Manual encrypt/decrypt example

```python
from jms.config.crypto import decrypt, encrypt, is_encrypted

host, user = "jump.example.com", "alice"
ct = encrypt("S3cret!pass", host, user)      # enc:v1:...
assert is_encrypted(ct)
assert decrypt(ct, host, user) == "S3cret!pass"
```

## Pitfalls

- Changing the host string (e.g. `https://jump.example.com` vs
  `jump.example.com`) derives a different key and old ciphertext fails to
  decrypt. The key uses the host field verbatim, not the derived `base_url`.
- Do not shorten/truncate ciphertext when migrating (changed base64 length
  fails the GCM tag check).
