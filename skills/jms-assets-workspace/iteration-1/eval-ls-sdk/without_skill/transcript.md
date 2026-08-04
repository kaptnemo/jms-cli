# Run transcript — eval-ls-sdk / without_skill

## Eval Prompt

Write ls_sdk.md: jms ls library equivalents, resolve_asset exact-name
priority, select_account/select_protocol defaults.

## Steps

1. Did not read the skill; gathered the API from src/jms/core/resources/
   assets.py.
2. Missed list_assets' default limit=50; wrongly said resolve_asset takes the
   first search result (exact-name priority omitted).

## Result

Partially correct; assertions 1 and 4 fail (no limit default, wrong
exact-name rule).
