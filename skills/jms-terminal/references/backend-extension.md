# New terminal backend template

Use `src/jms/transport/ssh.py` / `ws.py` as templates — both self-register on
import.

```python
"""Transport backend template: <proto> terminal (future RDP/VNC example)."""
from jms.core.resources import AssetInfo
from jms.core.auth import JMSSession
from jms.transport.base import AbstractTerminal, TerminalCapability
from jms.transport.registry import register_backend
from jms.exceptions import TerminalError


class ProtoTerminal(AbstractTerminal):
    capabilities = frozenset({TerminalCapability.EXEC})  # metadata only

    @property
    def backend_name(self) -> str:
        return "<proto>"

    def execute(self, command: str, timeout: int = 30, check: bool = False) -> str:
        raise NotImplementedError

    def interactive(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        ...


def open_proto_terminal(session: JMSSession, asset: AssetInfo) -> ProtoTerminal:
    """Create a connection token, connect, and return a ready terminal."""
    ...


register_backend("<proto>", open_proto_terminal, ProtoTerminal.capabilities)
```

## Wiring (no changes needed)

- `connect(session, asset, backend=BackendType.<X> | "name")` dispatches via
  `open_backend(name, ...)`.
- `TerminalCapability`: `EXEC` / `INTERACTIVE` / `DISPLAY` (reserved); flags
  are registry/UI metadata only — backends implement the same abstract
  interface regardless of flags.

## Explicit changes

- To join the AUTO fallback chain, edit the `_AUTO_SEQUENCE` tuple in
  `transport/registry.py` (default `("ssh", "ws")`); otherwise the backend is
  only callable explicitly.
- Add top-level exports to `jms/transport/__init__.py` `__all__` if needed.
- For interactive capabilities reuse `local_tty_size()` / `strip_ansi`.
