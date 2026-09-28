"""Команда `workspaces`: записи глобального реестра `~/.sessionretro/workspaces.json`.

`list` показывает все записи и отмечает те, у которых на диске нет
`<path>/.sessionretro/config.json`; `remove <id>` и `remove --missing` убирают записи из
реестра. Файлы проектов и их `.sessionretro/` команда не трогает. Реестр глобальный,
поэтому `--workspace` здесь не используется.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import registry
from lib.workspace import STATE_DIR, SrError

MISSING_MARK = "папки нет"
INDEX_HINT = "страница workspaces обновится при следующем `report --index`"


def register(subparsers) -> None:
    p = subparsers.add_parser("workspaces", help="записи глобального реестра workspaces")
    sub = p.add_subparsers(dest="workspaces_command", metavar="<действие>", required=True)

    lst = sub.add_parser("list", help="все записи реестра")
    lst.add_argument("--json", action="store_true", help="записи JSON-массивом")
    lst.set_defaults(func=handle_list)

    rm = sub.add_parser("remove", help="убрать запись из реестра (файлы проекта не трогаются)")
    target = rm.add_mutually_exclusive_group(required=True)
    target.add_argument("id", nargs="?", help="id записи (см. `workspaces list`)")
    target.add_argument("--missing", action="store_true",
                        help=f"убрать все записи, у которых нет {STATE_DIR}/config.json")
    rm.set_defaults(func=handle_remove)


def is_missing(entry: dict) -> bool:
    """Нет `<path>/.sessionretro/config.json` (или у записи нет пути)."""
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        return True
    return not (Path(path) / STATE_DIR / "config.json").is_file()


def _row(entry: dict) -> dict:
    return {
        "id": entry.get("id"),
        "path": entry.get("path"),
        "title": entry.get("title"),
        "mode": entry.get("mode"),
        "missing": is_missing(entry),
    }


def handle_list(args) -> int:
    rows = [_row(e) for e in registry.load()]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(f"реестр пуст ({registry.workspaces_path()})")
        return 0
    for row in rows:
        line = f"{row['id']} · {row['path'] or '—'} · {row['title']} · {row['mode']}"
        if row["missing"]:
            line += f" · {MISSING_MARK}"
        print(line)
    return 0


def _remove_one(ws_id: str) -> None:
    try:
        registry.remove(ws_id)
    except SrError as err:
        known = [str(e.get("id")) for e in registry.load()]
        raise SrError(
            f"{err.message}; известные id: {', '.join(known) if known else 'реестр пуст'}",
            err.code,
        ) from err


def handle_remove(args) -> int:
    if args.missing:
        gone = [e for e in registry.load() if is_missing(e)]
        if not gone:
            print("нечего убирать: у всех записей реестра папка на месте")
            return 0
        for entry in gone:
            _remove_one(entry["id"])
        print(f"убрано из реестра записей без {STATE_DIR}/config.json: {len(gone)}")
        for entry in gone:
            print(f"- {entry['id']} · {entry.get('path') or '—'}")
        print(INDEX_HINT)
        return 0
    entries = {e.get("id"): e for e in registry.load()}
    _remove_one(args.id)
    path = entries[args.id].get("path") or "—"
    print(f"убрано из реестра: {args.id} · {path} (файлы проекта не тронуты)")
    print(INDEX_HINT)
    return 0
