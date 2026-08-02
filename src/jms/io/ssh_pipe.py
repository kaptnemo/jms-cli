"""SSH pipe bridge for rsync / scp over JumpServer.

Used as rsync's ``-e`` program::

    rsync -avz -e 'jms ssh-pipe' \\
        ./local/  <asset>@<server>:<remote_path>/

The bridge opens an authenticated KoKo SSH transport (via
``open_koko_transport``), then uses paramiko's ``exec_command`` to run the
remote rsync command while relaying stdin / stdout / stderr through thread
loops.

This enables standard rsync features (delta transfer, compression,
recursive, delete-sync) over the JumpServer bastion — no OpenSSH,
sshpass, or pexpect required.

Examples::

    # Upload a directory with compression
    rsync -avz -e 'jms ssh-pipe' ./project/ web-01@prod:/home/user/project/

    # Download with delete-sync
    rsync -avz --delete -e 'jms ssh-pipe' web-01@prod:/data/output/ ./output/

Note: this is a stdio bridge — all diagnostics must go to stderr only;
stdout carries rsync protocol bytes.
"""

import os
import signal
import sys
import threading
from urllib.parse import urlparse

from jms.core.resources import resolve_asset
from jms.core.auth import JMSSession
from jms.transport import open_koko_transport
from jms.config import load_config
from jms.log import logger


def run_bridge(
    asset_name: str,
    server_alias: str,
    remote_cmd: str,
    config_path: str | None = None,
) -> int:
    """Relay stdio to a remote command over JumpServer — returns its exit code.

    Args:
        asset_name: JumpServer asset name (user field of the rsync URL).
        server_alias: Server alias in config.yaml (host field of the URL).
        remote_cmd: Remote command line (e.g. ``rsync --server ...``).
        config_path: Explicit config path, or None for the platformdirs
            default (the only form usable from rsync).

    Returns:
        Remote command exit status (0 on success).

    Raises:
        ConfigError: Config missing or unknown server alias.
        AssetError: Asset cannot be resolved.
        TerminalError: Transport cannot be established.
    """
    logger.debug("ssh-pipe: asset=%s server=%s cmd=%r", asset_name, server_alias, remote_cmd)

    cfg = load_config(config_path)
    server = cfg.get_server(server_alias)
    session = JMSSession(server)
    session.login()
    koko_host = urlparse(session.base_url).hostname or ""
    if not koko_host:
        sys.stderr.write("jms ssh-pipe: cannot derive host from config\n")
        return 1

    asset = resolve_asset(session, asset_name, protocol="ssh")

    transport = open_koko_transport(session, asset)

    chan = transport.open_session()
    chan.exec_command(remote_cmd)

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    stderr_fd = sys.stderr.fileno()

    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    # Use threads for stdin→channel and channel→stdout so that a
    # blocked read on one direction doesn't stall the other.
    def relay_in() -> None:
        try:
            while True:
                data = os.read(stdin_fd, 65536)
                if not data:
                    break
                chan.sendall(data)
        except Exception:
            pass
        finally:
            try:
                chan.shutdown_write()
            except Exception:
                pass

    def relay_out() -> None:
        try:
            while True:
                data = chan.recv(65536)
                if not data:
                    break
                os.write(stdout_fd, data)
        except Exception:
            pass

    def relay_err() -> None:
        try:
            while True:
                data = chan.recv_stderr(65536)
                if not data:
                    break
                os.write(stderr_fd, data)
        except Exception:
            pass

    tin = threading.Thread(target=relay_in, daemon=True)
    tout = threading.Thread(target=relay_out, daemon=True)
    terr = threading.Thread(target=relay_err, daemon=True)
    tin.start()
    tout.start()
    terr.start()

    tout.join()
    tin.join(timeout=2.0)
    terr.join(timeout=2.0)

    code = chan.recv_exit_status()
    chan.close()
    transport.close()
    return code
