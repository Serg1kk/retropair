"""Команда `compare`: расхождение двух прогонов одного человека по осям, уровню, гейту и антипаттернам."""

from __future__ import annotations

import json
from pathlib import Path

from lib import tracker
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("compare", help="сравнить два прогона одного человека")
    p.add_argument("a", metavar="A", help="id первого прогона")
    p.add_argument("b", metavar="B", help="id второго прогона")
    p.add_argument("--json", action="store_true", help="вывод JSON")
    p.set_defaults(func=handle)


def _show(value) -> str:
    return "нет данных" if value is None else str(value)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    result = tracker.compare(tracker.load(root), args.a, args.b)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"{result['person_id']}: {args.a} → {args.b}")
    for axis, row in result["axes"].items():
        delta = "" if row["delta"] is None else f"  Δ {row['delta']:+d}"
        shift = "  (сдвиг данных)" if row["evidence_shift"] else ""
        print(f"  {axis}: {_show(row['a'])} → {_show(row['b'])}{delta}{shift}")
    print(f"наибольший сдвиг: {_show(result['max_abs_delta'])}; оси со сдвигом ≥ 2: "
          f"{', '.join(result['shifted_ge2']) or 'нет'}")
    print(f"уровень: {result['level']['a']} → {result['level']['b']}; "
          f"гейт совпал: {'да' if result['gate_same'] else 'нет'}; "
          f"антипаттерны совпали: {'да' if result['antipatterns_same'] else 'нет'}")
    return 0
