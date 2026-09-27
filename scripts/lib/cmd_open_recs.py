"""Команда `open-recs`: открытые рекомендации человека и следующий номер REC."""

from __future__ import annotations

import json
from pathlib import Path

from lib import people, tracker
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("open-recs", help="открытые рекомендации человека и следующий номер")
    p.add_argument("person", metavar="PERSON", help="id человека (карточка people/<id>.yaml)")
    p.add_argument("--json", action="store_true", help="вывод JSON {open, next_number}")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    people.load_person(root, args.person)  # нет карточки → код 2, а не пустой список
    result = tracker.open_recs(tracker.load(root), args.person)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    for rec in result["open"]:
        print(f"{rec['id']}  [{rec['axis']}]  {rec['text']}  (период {rec['period'] or '—'}, {rec['run_id']})")
    if not result["open"]:
        print(f"открытых рекомендаций у {args.person} нет")
    print(f"следующий номер: REC-{args.person}-{result['next_number']:03d}")
    return 0
