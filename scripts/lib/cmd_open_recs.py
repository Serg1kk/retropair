"""Команда `open-recs`: открытые рекомендации человека, ждущие одобрения черновика, и следующий номер REC.

В режимах team и course при `reviewer_gate: true` рекомендации неодобренного прогона человек
не получал: они идут в `pending_approval`, а судье подаются только `open`.

У каждой открытой рекомендации — момент выдачи `issued_at` (время прогона; при гейте ревьюера —
время approve) и `evaluable`: есть ли среди сессий периода, которые пойдут в пачки (как у
`batches` с той же `--period-label`), хоть одна, законченная после выдачи. `evaluable: false` →
судить рано: вердикт `not_applicable_yet`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lib import people, tracker
from lib.config import load_config
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("open-recs", help="открытые рекомендации человека и следующий номер")
    p.add_argument("person", metavar="PERSON", help="id человека (карточка people/<id>.yaml)")
    p.add_argument("--json", action="store_true",
                   help="вывод JSON {open, pending_approval, next_number, warnings}")
    p.add_argument("--period-label", metavar="L",
                   help="метка периода, как у batches: evaluable считается по сессиям этого периода")
    p.set_defaults(func=handle)


def _line(rec: dict) -> str:
    return (f"{rec['id']}  [{rec['axis']}]  {rec['text']}  (период {rec['period'] or '—'}, {rec['run_id']}, "
            f"выдана {rec.get('issued_at') or '—'})")


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    people.load_person(root, args.person)  # нет карточки → код 2, а не пустой список
    gated = tracker.reviewer_gated(load_config(root))
    sessions, index_warnings = tracker.period_session_entries(root, args.person, args.period_label)
    result = tracker.open_recs(tracker.load(root), args.person, gated=gated, sessions=sessions)
    result["warnings"] = index_warnings + result["warnings"]
    for line in result["warnings"]:
        print(f"sessionretro: предупреждение: {line}", file=sys.stderr)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    for rec in result["open"]:
        mark = "" if rec["evaluable"] else "  — рано судить: логи периода закончились до выдачи"
        print(_line(rec) + mark)
    if not result["open"]:
        print(f"открытых рекомендаций у {args.person} нет")
    if result["pending_approval"]:
        print("ждут одобрения черновика, вердикт по ним отложен до approve:")
        for rec in result["pending_approval"]:
            print(f"  {_line(rec)}")
    print(f"следующий номер: REC-{args.person}-{result['next_number']:03d}")
    return 0
