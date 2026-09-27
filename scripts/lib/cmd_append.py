"""Команда `append`: записать прогон оценки из выхода судьи в трекер и пометить сессии в индексе логов."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lib import tracker
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("append", help="записать прогон оценки из выхода судьи")
    p.add_argument("judge", metavar="JUDGE", help="файл выхода судьи (judge.json)")
    p.add_argument("--person", required=True, help="id человека (карточка people/<id>.yaml)")
    p.add_argument("--period-label",
                   help="метка периода: латиница, цифры, . _ - (до 64 знаков); без неё в режиме run "
                        "метку run-ГГГГММДД[-N] выдаёт движок, итог — period_label в выводе")
    p.add_argument("--period-confidence", choices=("inferred", "explicit"), default="inferred",
                   help="метка периода выведена (inferred) или названа явно (explicit)")
    p.add_argument("--replace", metavar="RUN", help="переписать прогон RUN (id и правка ревьюера сохраняются)")
    p.add_argument("--skipped", nargs="+", default=[], metavar="SID",
                   help="сессии, известные и осознанно пропущенные этим прогоном")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    result = tracker.append(
        root, Path(args.judge), args.person,
        period_label=args.period_label, period_confidence=args.period_confidence,
        replace=args.replace, skipped=args.skipped,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    for line in result["warnings"]:
        print(f"sessionretro: предупреждение: {line}", file=sys.stderr)
    if result["warnings_other_runs"]:
        print("sessionretro: в трекере есть проблемные прогоны, запись они не блокируют:\n  "
              + "\n  ".join(result["warnings_other_runs"]), file=sys.stderr)
    return 0
