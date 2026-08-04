# `jms ls` library equivalents (jms-assets skill)

## CLI → library mapping

| CLI | Equivalent library call |
|---|---|
| `jms ls` | `list_assets(session)` (default `limit=50`, lazy pagination) |
| `jms ls -q mysql` | `search_assets(session, "mysql")` (all pages) |
| `jms ls -n 10` | `list_assets(session, limit=10)` |
| `jms ls prod` | `cfg.get_server("prod")`, then log in and list (no `@` prefix) |

## resolve_asset exact-name priority

`resolve_asset(session, name)` first runs `search_assets`; among the results,
the asset whose `name` **exactly matches** wins; only when no exact match
exists does it take the first search hit. It then fetches detail
(`permed_accounts`/`permed_protocols`) to build the `AssetInfo`.

## Default selection behavior

- `select_account`: `@USER` first → first non-`@` alias (fallback to username
  when alias empty) → first account → `@INPUT`.
- `select_protocol`: **ssh first**, otherwise the first granted protocol.
