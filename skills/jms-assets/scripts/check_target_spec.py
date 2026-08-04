#!/usr/bin/env python3
"""Validate ``asset[@server]`` target specs against ``jms.cli.parse_target``.

Usage: python3 check_target_spec.py '<spec>' [<spec> ...]

Prints one line per spec: ``<spec> -> asset=<asset>, server=<server|default>``.
Exit code 0 when all specs parse; 1 on usage error.
"""

from __future__ import annotations

import sys

from jms.cli import parse_target


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    for spec in argv:
        t = parse_target(spec)
        print(f"{spec} -> asset={t.asset}, server={t.server or '<default>'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
