# Run transcript — eval-backend-extension / without_skill

## Eval Prompt

Output rdp_backend.md: abstract class, 4 interfaces, register_backend
self-registration, and whether AUTO includes the backend by default.

## Steps

1. Did not read the skill; gathered the interface and registration from
   transport/base.py and registry.py.
2. Missed the AUTO-sequence detail: no mention of
   _AUTO_SEQUENCE=("ssh", "ws") or that registration does not enter AUTO.

## Result

rdp_backend.md lacks the AUTO behavior (assertion 4 fails).
