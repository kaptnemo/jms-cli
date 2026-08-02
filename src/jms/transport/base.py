"""Abstract base class for terminal backends.

All terminal backends (SSH, WebSocket) implement the same interface,
so upper layers (transfer/cli) can switch backends transparently.
"""

import re
import shutil
from abc import ABC, abstractmethod
from enum import Enum

# ANSI escape sequences (CSI / OSC / backspace overstrike)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x08.")


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from terminal output."""
    return ANSI_RE.sub("", text)


def local_tty_size() -> tuple[int, int]:
    """Return the local terminal size as ``(cols, rows)``, 80x24 on failure."""
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines


class TerminalCapability(Enum):
    """Capabilities a terminal backend may offer.

    Metadata for the backend registry / UI only — backends still implement
    the abstract ``execute()`` / ``interactive()`` regardless of flags.
    ``DISPLAY`` is reserved for future RDP/VNC-style backends.
    """

    EXEC = "exec"
    INTERACTIVE = "interactive"
    DISPLAY = "display"


class AbstractTerminal(ABC):
    """Abstract terminal session to a JumpServer asset.

    Subclasses must implement ``execute()`` (headless command execution),
    ``interactive()`` (interactive PTY relay) and ``close()`` (resource
    cleanup).
    """

    capabilities: frozenset[TerminalCapability] = frozenset()

    @abstractmethod
    def execute(self, command: str, timeout: int = 30, check: bool = False) -> str:
        """Execute a command and return its stdout output.

        Args:
            command: Shell command to execute.
            timeout: Maximum seconds to wait for output.
            check: When true, raise ``TerminalError`` (carrying the remote
                exit code) if the command exits non-zero or times out.

        Returns:
            Command output string (stripped).
        """

    @abstractmethod
    def interactive(self) -> None:
        """Start an interactive PTY relay.

        Enters raw terminal mode and relays I/O bidirectionally between
        local stdin/stdout and the remote terminal; returns when the
        user disconnects (Ctrl+]).
        """

    @abstractmethod
    def close(self) -> None:
        """Close the terminal connection and release resources."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Backend name (e.g. 'ssh' / 'websocket')."""

    def __enter__(self) -> "AbstractTerminal":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
