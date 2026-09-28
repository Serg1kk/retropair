"""Команда `inventory`: пересчёт `logs-index.json` по снапшотам `.sessionretro/logs/`.

Источники не читаются и снапшоты не пишутся (это делает `collect`); только скан снапшотов,
слияние с прежним индексом и атомарная запись под блокировкой `.sessionretro/collect.lock`.
Идёт сбор (например, хук SessionEnd перечищает снапшоты) — ждёт его до `lock.COLLECT_WAIT_SECONDS`
с одной строкой в stderr; не дождался — код 1 с путём lock-файла.
Записи людей с `consent.logs: false` исключаются так же, как в `collect`.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import inventory, lock, sources
from lib.config import load_config
from lib.people import list_people
from lib.workspace import find_workspace, ws_paths

def register(subparsers) -> None:
    p = subparsers.add_parser("inventory", help="пересчитать инвентарь логов по снапшотам")
    p.add_argument("--json", action="store_true", help="вывести logs-index.json целиком")
    p.set_defaults(func=handle)


def format_text(index: dict, path: Path) -> str:
    counts = inventory.summary(index)
    lines = [f"инвентарь: {len(index['logs'])} записей ({path})",
             " · ".join(f"{label}: {counts[state]}" for state, label in inventory.STATE_LABELS)]
    if index.get("warnings"):
        lines.append("предупреждения:")
        lines += [f"- {w}" for w in index["warnings"]]
    return "\n".join(lines)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    cfg = load_config(root)
    denied = sources.no_consent(list_people(root))
    paths = ws_paths(root)
    with lock.collect_lock(paths["state_dir"]):
        index = inventory.rebuild(root, cfg, no_consent=denied)
    if args.json:
        print(json.dumps(index, ensure_ascii=False, indent=2))
    else:
        print(format_text(index, paths["logs_index"]))
    return 0
