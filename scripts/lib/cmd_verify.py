"""Команда `verify`: сквозной аудит прогонов — покрытие пометками, тексты ревью, уровень, сироты."""

from __future__ import annotations

from pathlib import Path

from lib import tracker
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("verify", help="проверить, что каждый прогон закрыт до конца")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    problems = tracker.verify(root)
    for line in problems:
        print(line)
    if problems:
        print(f"проблем: {len(problems)}")
        return 1
    print("все прогоны закрыты: пометки, тексты ревью и уровни на месте, сирот нет")
    return 0
