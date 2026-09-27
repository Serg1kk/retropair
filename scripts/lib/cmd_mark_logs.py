"""Команда `mark-logs`: пометить в индексе логи уже записанного прогона (догнать покрытие)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lib import tracker
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("mark-logs", help="пометить в индексе логи записанного прогона")
    p.add_argument("run_id", metavar="RUN", help="id прогона")
    p.add_argument("--sessions", nargs="+", default=[], metavar="REF",
                   help="ещё оценённые сессии: session_id, его префикс от 8 знаков, ключ или путь")
    p.add_argument("--excluded", nargs="+", default=[], metavar="REF[=reason]",
                   help="сессии, осознанно не оценённые; причина после = (по умолчанию not_batched)")
    p.add_argument("--exclude-note", help="пояснение к исключённым сессиям, попадёт в индекс")
    p.add_argument("--force", action="store_true", help="перепометить уже помеченный прогон")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    result = tracker.mark_logs(root, args.run_id, sessions=args.sessions, excluded=args.excluded,
                               force=args.force, exclude_note=args.exclude_note)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    cov = result["coverage"]
    if result["missing"]:
        print("sessionretro: в индексе логов не нашлись сессии прогона: " + ", ".join(result["missing"]),
              file=sys.stderr)
    if cov["marked"] < cov["expected"]:
        print(f"sessionretro: покрытие неполное: помечено {cov['marked']} из {cov['expected']}", file=sys.stderr)
    return 1 if result["missing"] or cov["marked"] < cov["expected"] else 0
