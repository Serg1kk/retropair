"""Команда `people`: карточки людей workspace — add, show, list, context."""

from __future__ import annotations

import json
from pathlib import Path

from lib import people
from lib.config import load_config
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
