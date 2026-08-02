"""JumpServer v4 bastion asset access: exec, login, SFTP transfer, rsync over ssh-pipe.

The orchestration API (high-level transfer entry points) lives in ``jms.io.service``.
"""

from jms.core.resources import AssetInfo, list_assets, resolve_asset, search_assets
from jms.core.auth import JMSSession
from jms.transport import BackendType, connect
from jms.config import AppConfig, ServerConfig, add_server, load_config
from jms.exceptions import (
    APIError,
    AssetError,
    AuthError,
    ConfigError,
    ConnectionTokenError,
    JMSError,
    MFARequired,
    TerminalError,
    TransferError,
)
from jms.io.service import (
    merge_parts_via_ssh,
    relay_transfer,
    run_transfer,
    sftp_transfer,
)
from jms.io.transfer import FileInfo, FileTask, RelaySpec, TaskResult, TransferSpec
from jms.io.verify import verify_files

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "APIError",
    "AppConfig",
    "AssetError",
    "AssetInfo",
    "AuthError",
    "BackendType",
    "ConfigError",
    "ConnectionTokenError",
    "FileInfo",
    "FileTask",
    "JMSError",
    "JMSSession",
    "MFARequired",
    "RelaySpec",
    "ServerConfig",
    "TaskResult",
    "TerminalError",
    "TransferError",
    "TransferSpec",
    "add_server",
    "connect",
    "list_assets",
    "load_config",
    "merge_parts_via_ssh",
    "relay_transfer",
    "resolve_asset",
    "run_transfer",
    "search_assets",
    "sftp_transfer",
    "verify_files",
]
