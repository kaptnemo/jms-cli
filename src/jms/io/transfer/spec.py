"""Transfer-spec direction detection and remote spec parsing."""

from __future__ import annotations

from jms.exceptions import TransferError
from jms.io.transfer.models import RelaySpec, TransferSpec


def _parse_remote_spec(spec: str) -> tuple[str, str | None, str]:
    """Parse ``asset[@server]:path`` into ``(asset, server, path)``.

    The first colon splits the host part from the path, so ``@`` or
    further colons inside the path are preserved; the last ``@``
    within the host part separates the server alias.

    Returns:
        ``(asset, server_or_None, remote_path)``.

    Raises:
        TransferError: If the spec has no colon, an empty asset,
            or an empty path.
    """
    colon = spec.find(":")
    if colon == -1:
        raise TransferError(
            f"Invalid remote spec '{spec}': expected <asset>[@<server>]:<path>"
        )
    host, path = spec[:colon], spec[colon + 1:]
    at = host.rfind("@")
    if at > 0:
        asset, server = host[:at], host[at + 1:] or None
    else:
        asset, server = host, None
    if not asset:
        raise TransferError(f"Invalid remote spec '{spec}': asset name is empty")
    if not path:
        raise TransferError(f"Invalid remote spec '{spec}': remote path is empty")
    return asset, server, path


def parse_transfer_spec(src: str, dst: str) -> TransferSpec | RelaySpec:
    """Determine transfer direction from ``sftp <src> <dst>`` arguments.

    A side counts as remote when it contains a colon; local paths on
    POSIX never do (Windows drive letters are out of scope).

    - One remote + one local -> ``TransferSpec`` (upload or download)
    - Both remote -> ``RelaySpec`` (stream relay)
    - Neither remote -> ``TransferError``

    Args:
        src: First positional argument.
        dst: Second positional argument.

    Returns:
        Parsed ``TransferSpec`` or ``RelaySpec``.

    Raises:
        TransferError: If neither argument is remote.
    """
    src_remote = ":" in src
    dst_remote = ":" in dst

    if not src_remote and not dst_remote:
        raise TransferError(
            "Neither argument looks like a remote path. "
            "Use <asset>[@server]:<path> for the remote side."
        )

    if src_remote and dst_remote:
        s_asset, s_server, s_path = _parse_remote_spec(src)
        d_asset, d_server, d_path = _parse_remote_spec(dst)
        return RelaySpec(
            src_asset=s_asset, src_server=s_server, src_path=s_path,
            dst_asset=d_asset, dst_server=d_server, dst_path=d_path,
        )

    if dst_remote:
        asset, server, remote_path = _parse_remote_spec(dst)
        return TransferSpec(
            asset=asset, server=server,
            remote_path=remote_path, local_path=src, is_upload=True,
        )

    asset, server, remote_path = _parse_remote_spec(src)
    return TransferSpec(
        asset=asset, server=server,
        remote_path=remote_path, local_path=dst, is_upload=False,
    )
