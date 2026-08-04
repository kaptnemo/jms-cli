# rdp backend minimal implementation checklist (jms-terminal skill)

## Abstract class and interfaces

- Inherit `jms.transport.base.AbstractTerminal`; declare capabilities as a
  class attribute (e.g. `frozenset({TerminalCapability.EXEC})`, metadata
  only).
- Four interfaces to implement:
  1. `execute(command: str, timeout: int = 30, check: bool = False) -> str`
  2. `interactive() -> None`
  3. `close() -> None`
  4. `backend_name -> str` (returns `"rdp"`)

## Self-registration

```python
from jms.transport.base import AbstractTerminal, TerminalCapability
from jms.transport.registry import register_backend

class RDPTerminal(AbstractTerminal):
    capabilities = frozenset({TerminalCapability.EXEC})
    ...

def open_rdp_terminal(session, asset) -> RDPTerminal:
    ...

register_backend("rdp", open_rdp_terminal, RDPTerminal.capabilities)
```

## AUTO sequence

`BackendType.AUTO` does not include the new backend by default:
`_AUTO_SEQUENCE = ("ssh", "ws")` (transport/registry.py). After registration
the backend is only reachable via explicit `open_backend("rdp", ...)`;
joining the AUTO fallback chain requires editing the registry tuple.
