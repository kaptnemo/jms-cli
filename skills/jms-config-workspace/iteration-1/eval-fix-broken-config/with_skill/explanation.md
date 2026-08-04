# Why a plaintext password also passes on read

`jms.config.parse_config` checks `is_encrypted()` on the password field first:
only values starting with `enc:v1:` go through `decrypt()`, everything else is
used as plaintext. Therefore `password: fix-me` is valid — the skill's
`validate_config.py` flags plaintext only as a note ("accepted on read,
encrypted on save").
