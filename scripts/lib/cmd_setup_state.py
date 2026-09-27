"""Команда `setup-state`: текущие ответы онбординга и находки окружения (вне workspace — код 0)."""

from __future__ import annotations

import json
from pathlib import Path

from lib import setup
from lib.cmd_setup_apply import workspace_dir


def register(subparsers) -> None:
    p = subparsers.add_parser("setup-state", help="текущие значения онбординга и находки окружения")
    p.add_argument("--json", action="store_true",
                   help="JSON: workspace в формате ответов setup-apply (или null) и found")
    p.set_defaults(func=handle)


def _text(value) -> str:
    if value is None or value is False:
        return "нет"
    if value is True:
        return "да"
    return str(value)


def handle(args) -> int:
    path = workspace_dir(args.workspace) if args.workspace is not None else None
    data = setup.state(Path.cwd(), Path.home(), path)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    ws = data["workspace"]
    print(f"workspace: {ws['path'] if ws else 'нет'}")
    if ws:
        print(f"mode: {ws['mode']} · lang: {ws['lang']} · people: {len(ws['people'])}")
    for key, value in data["found"].items():
        print(f"{key}: {_text(value)}")
    return 0
