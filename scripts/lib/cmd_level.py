"""Команда `level`: посчитать уровень прогона формулой и записать `level_computed`."""

from __future__ import annotations

import json
from pathlib import Path

from lib import tracker
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("level", help="посчитать уровень прогона и записать level_computed")
    p.add_argument("run_id", metavar="RUN", help="id прогона")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    print(json.dumps(tracker.set_level(root, args.run_id), ensure_ascii=False, indent=2))
    return 0
