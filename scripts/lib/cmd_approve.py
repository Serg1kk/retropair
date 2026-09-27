"""Команда `approve`: перевести текст ревью из черновика в одобренный (после чтения ревьюером)."""

from __future__ import annotations

from pathlib import Path

from lib import render_review
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("approve", help="одобрить текст ревью: status draft → approved")
    p.add_argument("person", metavar="PERSON", help="id человека")
    p.add_argument("run_id", nargs="?", metavar="RUN", help="id прогона (по умолчанию последний живой)")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    result = render_review.approve(root, args.person, args.run_id)
    if result["changed"]:
        print(f"ревью {result['run_id']} одобрено (status: approved)")
    else:
        print(f"ревью {result['run_id']} уже одобрено (status: approved), ничего не менялось")
    return 0
