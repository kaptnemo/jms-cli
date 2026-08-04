#!/usr/bin/env python3
"""Validate a `jms sftp <src> <dst>` argument pair's direction detection.

Usage:
    uv run python3 skills/jms-transfer/scripts/check_sftp_spec.py <src> <dst>

Calls jms.io.transfer.parse_transfer_spec (the exact function the CLI uses)
and prints upload / download / relay. Exit code 0 = valid pair, 1 = invalid,
2 = usage error. Run before constructing a transfer call to catch spec
mistakes deterministically.
"""

import sys

try:
    from jms.exceptions import TransferError
    from jms.io.transfer import (
        RelaySpec,
        TransferSpec,
        parse_transfer_spec,
    )
except ImportError as exc:
    print(f"jms package not importable: {exc}")
    print("Run from the repo root via: uv run python3 "
          "skills/jms-transfer/scripts/check_sftp_spec.py <src> <dst>")
    sys.exit(2)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    try:
        spec = parse_transfer_spec(src, dst)
    except TransferError as exc:
        print(f"ERROR: {exc}")
        return 1
    if isinstance(spec, RelaySpec):
        print(
            f"relay: {spec.src_asset}@{spec.src_server or '(default)'}:"
            f"{spec.src_path} -> {spec.dst_asset}@{spec.dst_server or '(default)'}:"
            f"{spec.dst_path}"
        )
    elif isinstance(spec, TransferSpec):
        direction = "upload" if spec.is_upload else "download"
        print(
            f"{direction}: asset={spec.asset} server={spec.server or '(default)'} "
            f"remote_path={spec.remote_path} local_path={spec.local_path}"
        )
    else:
        print(f"ERROR: unexpected spec type {type(spec).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
