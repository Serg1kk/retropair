"""Команда `retract`: отозвать прогон (он остаётся в истории) и откатить пометки его логов."""

from __future__ import annotations

import json
from pathlib import Path

from lib import tracker
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("retract", help="отозвать прогон, не удаляя его, и снять пометки логов")
    p.add_argument("run_id", metavar="RUN", help="id прогона")
    p.add_argument("--why", help="причина отзыва (обязательна)")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    result = tracker.retract(root, args.run_id, args.why or "")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
