# rdp backend implementation checklist

- Inherit `AbstractTerminal` (`jms/transport/base.py`).
- Implement execute / interactive / close / backend_name.
- Call `register_backend("rdp", open_rdp_terminal, ...)` at module bottom.
- Note: after registration the backend can be used via
  `connect(..., backend="rdp")`.
