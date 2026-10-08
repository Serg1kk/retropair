"""Команда `check-collector`: проверить выходы коллектора до передачи судье.

Кроме схемы и сходимостей сверяет каждую цитату с логом её сессии (`sessions[].path`): цитата
стоит в названном ходе как реплика человека либо как команда или вывод инструмента. Относительные
пути логов берутся от `.retropair/` workspace: `--workspace` или поиск вверх от файла пачки.

Номер хода и склейку кусков одной реплики без «…», которые лог восстанавливает однозначно,
команда чинит в файле сама; `--drop-invalid` (после исчерпания перезапусков коллектора) убирает
записи с неремонтируемыми цитатами. Массив кандидатов или сигналов, который коллектор опустил,
дописывается пустым (`filled`: пачка неполная, но перезапуска не нужно). Каждая правка — строка
в `metadata.warnings` пачки и в выводе (`filled`, `repaired`, `dropped` по пачке и их число сверху).
"""

from __future__ import annotations

import json

from lib import collector_check


def register(subparsers) -> None:
    p = subparsers.add_parser("check-collector", help="проверить JSON-выходы коллектора (пачки) до судьи")
    p.add_argument("paths", nargs="+", metavar="FILE", help="файлы пачек")
    p.add_argument("--drop-invalid", action="store_true",
                   help="убрать записи с неремонтируемыми цитатами (ось без наблюдений → axes_unobserved)")
    p.set_defaults(func=handle)


def handle(args) -> int:
    code, results = collector_check.check_files(args.paths, getattr(args, "workspace", None),
                                                drop_invalid=args.drop_invalid)
    report = {
        "checked": len(results),
        "with_problems": sum(1 for r in results if r["problems"]),
        "missing_files": sum(1 for r in results if r["missing"]),
        "filled": sum(len(r["filled"]) for r in results),
        "repaired": sum(len(r["repaired"]) for r in results),
        "dropped": sum(len(r["dropped"]) for r in results),
        "batches": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code
