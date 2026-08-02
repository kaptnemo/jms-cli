"""Configuration domain: config-file management and credential crypto.

The public config API lives here so ``from jms.config import ...`` keeps
working regardless of the internal module layout.
"""

from jms.config.config import (
    AppConfig,
    ServerConfig,
    add_server,
    config_dir,
    config_file_path,
    load_config,
    parse_config,
    remove_server,
    save_config,
    set_default_server,
)

__all__ = [
    "AppConfig",
    "ServerConfig",
    "add_server",
    "config_dir",
    "config_file_path",
    "load_config",
    "parse_config",
    "remove_server",
    "save_config",
    "set_default_server",
]
