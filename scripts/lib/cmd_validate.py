"""Команда `validate`: трекер целиком по схеме и инвариантам каждого живого прогона."""

from __future__ import annotations

from pathlib import Path

from lib import tracker
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("validate", help="проверить трекер прогонов: схема и инварианты")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    problems = tracker.validate_file(root)
    for line in problems:
        print(line)
    if problems:
        print(f"проблем: {len(problems)} ({tracker.tracker_path(root)})")
        return 1
    print(f"трекер в порядке ({tracker.tracker_path(root)})")
    return 0
