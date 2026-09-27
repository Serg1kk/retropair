"""Команда `normalize`: лог сессии → JSON-документ с ходами и механическими метриками.

Workspace не нужен. Коды: 0 — ок; 1 — ходов не найдено; 2 — нет файла, отрицательный срез,
неизвестная роль. Фильтры `--from-turn/--to-turn/--roles/--drop-synthetic` меняют только
печать: метрики и `turns_total` всегда по всей сессии.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import normalize
from lib.workspace import SrError

ROLES = {"user", "assistant", "system"}


def register(subparsers) -> None:
    p = subparsers.add_parser("normalize", help="разобрать лог сессии в JSON с ходами и метриками")
    p.add_argument("path", help="файл лога")
    p.add_argument("--stats", action="store_true", help="только сводка, без текста ходов")
    p.add_argument("--no-tool-output", action="store_true", help="не тащить вывод инструментов")
    p.add_argument("--max-turn-chars", type=int, default=0,
                   help="обрезать текст хода (0 — не резать)")
    p.add_argument("--from-turn", type=int, default=None,
                   help="печатать ходы начиная с этого индекса (включительно)")
    p.add_argument("--to-turn", type=int, default=None,
                   help="печатать ходы до этого индекса (не включая)")
    p.add_argument("--roles", default=None,
                   help="какие роли печатать: user,assistant,system через запятую или all")
    p.add_argument("--drop-synthetic", action="store_true",
                   help="не печатать вставки харнесса (скиллы, компакт, слэш-команды)")
    p.add_argument("--tools-map", action="store_true",
                   help="карта вызовов инструментов по ходам без прозы и вывода")
    p.set_defaults(func=handle)


def parse_roles(value: str | None) -> set[str] | None:
    """`--roles` → множество ролей; None — все роли. Неизвестная роль → SrError(2)."""
    if not value:
        return None
    roles = {r.strip().lower() for r in value.split(",") if r.strip()}
    unknown = roles - ROLES - {"all"}
    if unknown:
        raise SrError(
            f"normalize: неизвестные роли в --roles: {', '.join(sorted(unknown))} "
            f"(допустимо: {', '.join(sorted(ROLES))}, all)", 2)
    return None if "all" in roles else roles


def tools_map(turns: list[dict], lo: int, hi: int) -> list[dict]:
    """Вызовы инструментов в ходах [lo, hi): чтения выброшены, свёрнутые вызовы без команды
    схлопнуты в строку со счётчиком, пустые поля опущены."""
    rows = []
    for t in turns:
        if not (lo <= t["index"] < hi):
            continue
        collapsed_counts: dict = {}
        for tool in t.get("tools") or []:
            if tool.get("collapsed") and not tool.get("command") and tool.get("type") != "read":
                key = (tool.get("name"), tool.get("type"))
                collapsed_counts[key] = collapsed_counts.get(key, 0) + 1
        for (name, ttype), n in collapsed_counts.items():
            rows.append({"turn_index": t["index"], "name": name, "type": ttype,
                         "count": n, "commands_hidden": True})
        for tool in t.get("tools") or []:
            if (tool.get("collapsed") and not tool.get("command")) or tool.get("type") == "read":
                continue
            row = {"turn_index": t["index"], "name": tool.get("name"),
                   "output_chars": tool.get("output_chars")}
            if tool.get("command"):
                row["command"] = tool["command"][:160]
            if tool.get("type"):
                row["type"] = tool["type"]
            if t.get("interrupted"):
                row["interrupted"] = True
            rows.append(row)
    return rows


def handle(args) -> int:
    path = Path(args.path)
    for flag, value in (("--from-turn", args.from_turn), ("--to-turn", args.to_turn)):
        if value is not None and value < 0:
            raise SrError(f"normalize: {flag} не может быть отрицательным (получено {value})", 2)
    roles = parse_roles(args.roles)
    doc = normalize.parse_file(path, keep_tool_output=not args.no_tool_output,
                               max_turn_chars=args.max_turn_chars)
    if not doc["turns_total"]:
        raise SrError(
            f"normalize: ходов не найдено — файл не похож ни на один поддерживаемый формат "
            f"({', '.join(normalize.format_names())}) ({path})", 1)

    lo = args.from_turn or 0
    hi = args.to_turn if args.to_turn is not None else doc["turns_total"]
    if args.tools_map:
        rows = tools_map(doc["turns"], lo, hi)
        doc["tools_map"] = rows
        doc["tools_map_scope"] = {"from_turn": lo, "to_turn": hi, "calls": len(rows)}
        doc.pop("turns", None)
    elif args.stats:
        doc.pop("turns", None)
    elif args.from_turn is not None or args.to_turn is not None or roles or args.drop_synthetic:
        sel = [t for t in doc["turns"] if lo <= t["index"] < hi]
        if roles:
            sel = [t for t in sel if t["role"] in roles]
        if args.drop_synthetic:
            sel = [t for t in sel if not t["synthetic"]]
        doc["turns"] = sel
        doc["turns_slice"] = {
            "from_turn": lo, "to_turn": hi,
            "roles": sorted(roles) if roles else "all",
            "synthetic_dropped": bool(args.drop_synthetic),
            "returned": len(sel), "of_total": doc["turns_total"],
        }
    print(json.dumps(doc, ensure_ascii=False, indent=2))
    return 0
