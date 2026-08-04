# `jms ls` library equivalents

- `jms ls` ≈ `list_assets(session)`; `-q` ≈ `search_assets(session, keyword)`;
  `-n` ≈ list_assets' limit param.
- `resolve_asset` uses the first search result.
- `select_account` prefers @USER; `select_protocol` prefers ssh.
