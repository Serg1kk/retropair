"""Команда `people`: карточки людей workspace — add, show, list, context; предложения правок
карточки — suggestions, apply, reject (логика в `lib/card_suggestions.py`)."""

from __future__ import annotations

import json
from pathlib import Path

from lib import card_suggestions, people
from lib.config import load_config
from lib.i18n import t
from lib.workspace import find_workspace


def register(subparsers) -> None:
    p = subparsers.add_parser("people", help="карточки людей workspace")
    sub = p.add_subparsers(dest="people_command", metavar="<действие>", required=True)

    add = sub.add_parser("add", help="создать карточку из шаблона")
    add.add_argument("id", help="id человека: латиница в нижнем регистре, цифры, дефис")
    add.add_argument("--name", help="имя (по умолчанию — id)")
    add.add_argument("--group", help="группа (по умолчанию — первая группа workspace)")
    add.set_defaults(func=handle_add)

    show = sub.add_parser("show", help="показать карточку")
    show.add_argument("id")
    show.add_argument("--json", action="store_true",
                      help="эффективная карточка после слияния с _default.yaml")
    show.set_defaults(func=handle_show)

    lst = sub.add_parser("list", help="список людей")
    lst.add_argument("--json", action="store_true", help="эффективные карточки массивом")
    lst.set_defaults(func=handle_list)

    ctx = sub.add_parser("context", help="срез карточки для судьи (5–8 строк)")
    ctx.add_argument("id")
    ctx.add_argument("--json", action="store_true", help="те же строки JSON-массивом")
    ctx.set_defaults(func=handle_context)

    sug = sub.add_parser("suggestions", help="предложения правок карточки (pending)")
    sug.add_argument("id", nargs="?", help="id человека (по умолчанию — все)")
    sug.add_argument("--refresh", action="store_true",
                     help="пересчитать по одобренным прогонам, добавить только новые")
    sug.add_argument("--all", action="store_true", help="все статусы, не только pending")
    sug.add_argument("--json", action="store_true", help="{new, items, warnings} одним объектом")
    sug.set_defaults(func=handle_suggestions)

    app = sub.add_parser("apply", help="применить предложение к карточке")
    app.add_argument("id")
    app.add_argument("suggestion", metavar="S-id")
    app.set_defaults(func=handle_apply)

    rej = sub.add_parser("reject", help="отклонить предложение")
    rej.add_argument("id")
    rej.add_argument("suggestion", metavar="S-id")
    rej.add_argument("--reason", help="почему отклонено")
    rej.set_defaults(func=handle_reject)


def _root(args) -> Path:
    return find_workspace(Path.cwd(), args.workspace)


def _dump(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def handle_add(args) -> int:
    root = _root(args)
    name = args.name if args.name is not None else args.id
    group = args.group
    if group is None:
        people.check_id(args.id)
        group = load_config(root)["workspace"]["groups"][0]
    path, added = people.add_person(root, args.id, name, group)
    print(f"карточка создана: {path}")
    if added:
        print(f"группа {group} добавлена в workspace.groups")
    return 0


def handle_show(args) -> int:
    root = _root(args)
    card = people.load_person(root, args.id)
    if args.json:
        _dump(card)
    else:
        print(people.card_path(root, args.id).read_text(encoding="utf-8"), end="")
    return 0


def handle_list(args) -> int:
    cards = people.list_people(_root(args))
    if args.json:
        _dump(cards)
    else:
        for card in cards:
            print(f"{card['id']} · {card['name']} · {card['group']}")
    return 0


def handle_context(args) -> int:
    lines = people.person_context(people.load_person(_root(args), args.id))
    if args.json:
        _dump(lines)
    else:
        print("\n".join(lines))
    return 0


I18N = ("cli", "setup")


def _lang(root: Path) -> str:
    return load_config(root)["lang"]


def _shown(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def suggestion_lines(item: dict, lang: str) -> list[str]:
    """Строка предложения и, у pending, команды apply/reject."""
    pid, sid = item["person_id"], item["id"]
    if item.get("op") == "append":
        proposed = item.get("proposed")
        values = proposed if isinstance(proposed, list) else [proposed]
        change = "+ " + ", ".join(_shown(v) for v in values)
    else:
        change = f"{_shown(item.get('current'))} → {_shown(item.get('proposed'))}"
    line = f"{sid} {item.get('field')}: {change} ({item.get('source')}, run {item.get('run_id')})"
    if item.get("status") != "pending":
        return [f"{line} [{item.get('status')}]"]
    return [
        line,
        f"  {item.get('reason')}",
        f"  {t(I18N, 'card_apply', lang)}: sr.py people apply {pid} {sid}",
        f'  {t(I18N, "card_reject", lang)}: sr.py people reject {pid} {sid} --reason "…"',
    ]


def handle_suggestions(args) -> int:
    root = _root(args)
    if args.id is not None:
        people.load_person(root, args.id)
    lang = _lang(root)
    warnings: list[str] = []
    new = card_suggestions.refresh(root, args.id, warn=warnings.append) if args.refresh else []
    items = card_suggestions.listing(root, args.id, include_all=args.all)
    if args.json:
        _dump({"new": [i["id"] for i in new], "items": items, "warnings": warnings})
        return 0
    for message in warnings:
        print(f"{t(I18N, 'card_warning', lang)}: {message}")
    if args.refresh:
        print(f"{t(I18N, 'card_new', lang)}: {len(new)}")
    if not items:
        print(t(I18N, "card_none", lang))
    for item in items:
        print("\n".join(suggestion_lines(item, lang)))
    return 0


def handle_apply(args) -> int:
    root = _root(args)
    item = card_suggestions.apply(root, args.id, args.suggestion)
    print(f"{t(I18N, 'card_applied', _lang(root))}: {item['id']} {item['field']} = {_shown(item['proposed'])}")
    return 0


def handle_reject(args) -> int:
    root = _root(args)
    item = card_suggestions.reject(root, args.id, args.suggestion, args.reason)
    print(f"{t(I18N, 'card_rejected', _lang(root))}: {item['id']} {item['field']}")
    return 0


def doctor_lines(root: Path | None) -> list[tuple[str, object]]:
    """`card_suggestions_pending` — pending-предложения к карточкам, без пересчёта."""
    return card_suggestions.doctor_lines(root)
