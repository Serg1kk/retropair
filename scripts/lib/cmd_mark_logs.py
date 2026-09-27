"""Команда `mark-logs`: пометить в индексе логи уже записанного прогона (догнать покрытие).

`mark-logs --no-run --person <id> --excluded REF[=reason] …` — закрыть сессии без прогона: за
период у человека только короткие сессии, оценки нет, а сессии не должны оставаться `new`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lib import tracker
from lib.workspace import SrError, find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("mark-logs", help="пометить в индексе логи записанного прогона")
    p.add_argument("run_id", metavar="RUN", nargs="?", help="id прогона (не задаётся с --no-run)")
    p.add_argument("--sessions", nargs="+", default=[], metavar="REF",
                   help="ещё оценённые сессии: session_id, его префикс от 8 знаков, ключ или путь")
    p.add_argument("--excluded", nargs="+", default=[], metavar="REF[=reason]",
                   help="сессии, осознанно не оценённые; причина после = (по умолчанию not_batched, "
                        "с --no-run transcript_too_short)")
    p.add_argument("--exclude-note", help="пояснение к исключённым сессиям, попадёт в индекс")
    p.add_argument("--force", action="store_true", help="перепометить уже помеченный прогон")
    p.add_argument("--no-run", action="store_true",
                   help="закрыть сессии без прогона (все короткие): только --person и --excluded, причины "
                        + ", ".join(tracker.NO_RUN_REASONS))
    p.add_argument("--person", metavar="PERSON", help="id человека; только вместе с --no-run")
    p.set_defaults(func=handle)


def _check_no_run(args) -> None:
    if args.run_id:
        raise SrError(f"--no-run и id прогона {args.run_id!r} вместе не задаются", 2)
    if args.sessions:
        raise SrError("--no-run не ставит оценённых сессий: --sessions только с id прогона", 2)
    if args.force:
        raise SrError("--no-run не перепомечает: --force только с id прогона", 2)
    if not args.person:
        raise SrError("--no-run без --person: чьи сессии закрывать", 2)
    if not args.excluded:
        raise SrError(f"--no-run у {args.person}: нет сессий в --excluded", 2)


def _no_run(args) -> int:
    _check_no_run(args)
    root = find_workspace(Path.cwd(), args.workspace)
    result = tracker.mark_logs_no_run(root, args.person, args.excluded, exclude_note=args.exclude_note)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["missing"]:
        print(f"sessionretro: у {result['person_id']} в индексе логов не нашлись сессии: "
              + ", ".join(result["missing"]) + "; ничего не помечено", file=sys.stderr)
        return 1
    return 0


def handle(args) -> int:
    if args.no_run:
        return _no_run(args)
    if args.person:
        raise SrError("--person только вместе с --no-run; у прогона человек свой", 2)
    if not args.run_id:
        raise SrError("mark-logs без id прогона: назовите RUN или закройте сессии без прогона "
                      "(--no-run --person <id> --excluded …)", 2)
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
