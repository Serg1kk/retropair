"""Команда `check-collector`: проверить выходы коллектора до передачи судье.

Кроме схемы и сходимостей сверяет каждую цитату с логом её сессии (`sessions[].path`): цитата
стоит в названном ходе как реплика человека либо как команда или вывод инструмента. Относительные
пути логов берутся от `.sessionretro/` workspace: `--workspace` или поиск вверх от файла пачки.
"""

from __future__ import annotations

import json

from lib import collector_check


def register(subparsers) -> None:
    p = subparsers.add_parser("check-collector", help="проверить JSON-выходы коллектора (пачки) до судьи")
    p.add_argument("paths", nargs="+", metavar="FILE", help="файлы пачек")
    p.set_defaults(func=handle)


def handle(args) -> int:
    code, results = collector_check.check_files(args.paths, getattr(args, "workspace", None))
    report = {
        "checked": len(results),
        "with_problems": sum(1 for r in results if r["problems"]),
        "missing_files": sum(1 for r in results if r["missing"]),
        "batches": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code
