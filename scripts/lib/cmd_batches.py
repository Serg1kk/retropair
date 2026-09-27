"""Команда `batches`: нарезать сессии человека на пачки коллектора."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lib import batches
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("batches", help="нарезать сессии человека на пачки коллектора")
    p.add_argument("person", metavar="PERSON", help="id человека")
    p.add_argument("--json", action="store_true", help="вывод JSON")
    p.add_argument("--measure", choices=sorted(batches.MEASURES), default="human",
                   help="размер сессии: human — человеческий слой через normalize (по умолчанию), "
                        "index — размер файла из индекса")
    p.add_argument("--period-label", metavar="L", help="метка периода: сессии другого периода уходят в skipped")
    p.set_defaults(func=handle)


def _text(result: dict) -> str:
    lines = [f"{result['person_id']}: пачек {len(result['batches'])}, замер {result['measure']}"]
    for b in result["batches"]:
        mark = ", batch_truncated" if b["batch_truncated"] else ""
        lines.append(f"пачка {b['index']}/{b['total']}: сессий {len(b['sessions'])}, байт {b['bytes']}, "
                     f"реплик {b['user_turns']}, {b['date_from']} → {b['date_to']}{mark}")
        for s in b["sessions"]:
            tail = f" (с хода {s['from_turn']})" if s["from_turn"] is not None else ""
            lines.append(f"  {s['session_id']} {s['path']}{tail}")
    for s in result["short"]:
        lines.append(f"короткая: {s['session_id']} {s['path']} (реплик {s['user_turns']})")
    for s in result["skipped"]:
        lines.append(f"пропущена ({s['reason']}): {s['session_id']} {s['path']} (период {s['period_guess']})")
    return "\n".join(lines)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    result = batches.for_person(root, args.person, measure=args.measure, period_label=args.period_label)
    if result["measure"] == "file_bytes":
        print("sessionretro: замер по размеру файла (measure: file_bytes) — трейсы инструментов "
              "раздувают размер, пачки грубее, чем по человеческому слою", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else _text(result))
    return 0
