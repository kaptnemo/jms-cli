"""Logging setup for jms-cli.

Provides a module-level logger configured via ``setup_logging()``.
All modules should use ``from jms.log import logger``.
"""

import logging
import sys

# Named logger — all modules share this
logger: logging.Logger = logging.getLogger("jms")

# Prevent duplicate handlers if setup_logging is called multiple times
_configured: bool = False


def setup_logging(level: str = "INFO") -> None:
    """Configure the jms logger.

    Args:
        level: Log level string (DEBUG, INFO, WARNING, ERROR).

    Raises:
        ValueError: If level is not a valid logging level name.
    """
    global _configured
    numeric = getattr(logging, level.upper(), None)
    if not isinstance(numeric, int):
        raise ValueError(f"Unknown log level: {level}")
    logger.setLevel(numeric)
    if _configured:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(logging.DEBUG)
    fmt = logging.Formatter("[%(levelname)s] %(message)s")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    _configured = True
