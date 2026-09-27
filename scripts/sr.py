#!/usr/bin/env python3
"""sessionretro CLI: тонкий диспетчер. Команды живут в `lib/cmd_*.py` (см. `lib/registry_cmds.py`)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib import registry_cmds  # noqa: E402
from lib.workspace import SrError  # noqa: E402


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    pre = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre.add_argument("--workspace")
    known, rest = pre.parse_known_args(argv)

    parser = registry_cmds.build_parser()
    args = parser.parse_args(rest)
    args.workspace = known.workspace
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    try:
        return registry_cmds.dispatch(args)
    except SrError as err:
        print(f"sessionretro: {err.message}", file=sys.stderr)
        return err.code


if __name__ == "__main__":
    sys.exit(main())
