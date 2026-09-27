"""Команда `improve`: предложения движку — add, list, pack, sent, sync.

Ничего не отправляет и не запускает внешних программ: `pack` собирает пакет, печатает его целиком и
строку `gh issue create …`, которую пользователь запускает сам. `doctor_lines` добавляет в `doctor`
число `pending`-предложений.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import improve
from lib.config import load_config
from lib.i18n import t
from lib.version import __version__, plugin_root
from lib.workspace import find_workspace

I18N = ("cli", "setup")


def register(subparsers) -> None:
    p = subparsers.add_parser("improve", help="предложения по улучшению плагина (без отправки)")
    sub = p.add_subparsers(dest="improve_command", metavar="<действие>", required=True)

    add = sub.add_parser("add", help="записать предложение (pending), текст обезличивается")
    add.add_argument("--component", required=True, help="скилл или часть движка")
    add.add_argument("--symptom", required=True, help="что не вышло и в какой ситуации")
    add.add_argument("--workaround", required=True, help="как обошли в этот раз")
    add.add_argument("--proposal", required=True,
                     help="причина — …; подход — …; критерий успеха — …")
    add.add_argument("--slug", help="слаг ID (по умолчанию — из компонента)")
    add.set_defaults(func=handle_add)

    lst = sub.add_parser("list", help="предложения вне архива: ID, статус, компонент")
    lst.add_argument("--json", action="store_true", help="массив объектов")
    lst.set_defaults(func=handle_list)

    pck = sub.add_parser("pack", help="собрать pending в один файл и напечатать команду gh")
    pck.add_argument("--out", help="файл пакета (по умолчанию improvements/pack-ГГГГММДД-<hex4>.md)")
    pck.set_defaults(func=handle_pack)

    snt = sub.add_parser("sent", help="отметить отправленные: pending → sent")
    snt.add_argument("ids", nargs="+", metavar="ID")
    snt.set_defaults(func=handle_sent)

    syn = sub.add_parser("sync", help="внедрённые по CHANGELOG → implemented и в archive/")
    syn.add_argument("--changelog", help="файл CHANGELOG (по умолчанию CHANGELOG.md плагина)")
    syn.set_defaults(func=handle_sync)


def _root(args) -> Path:
    return find_workspace(Path.cwd(), args.workspace)


def _lang(root: Path) -> str:
    return load_config(root)["lang"]


def handle_add(args) -> int:
    root = _root(args)
    path = improve.add(root, args.component, args.symptom, args.workaround, args.proposal,
                       slug=args.slug)
    print(f"{t(I18N, 'improve_added', _lang(root))}: {path}")
    return 0


def handle_list(args) -> int:
    root = _root(args)
    items = improve.list_items(root)
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    elif not items:
        print(t(I18N, "improve_none", _lang(root)))
    else:
        for item in items:
            print(f"{item['id']} · {item['status']} · {item['component']}")
    return 0


def handle_pack(args) -> int:
    root = _root(args)
    # repository проверяется до записи пакета: иначе код 1 оставил бы файл-сироту
    repository = improve._manifest_repository()
    improve.repo_slug(repository)
    path, text = improve.pack(root, Path(args.out).expanduser().resolve() if args.out else None)
    count = sum(1 for item in improve.list_items(root) if item["status"] == improve.PENDING)
    command = improve.gh_command(path, count, __version__, repository=repository)
    lang = _lang(root)
    print(text, end="" if text.endswith("\n") else "\n")
    print()
    print(f"{t(I18N, 'improve_pack_file', lang)}: {path}")
    print(t(I18N, "improve_pack_next", lang))
    print(command)
    return 0


def handle_sent(args) -> int:
    root = _root(args)
    changed = improve.mark_sent(root, args.ids)
    lang = _lang(root)
    for ident in args.ids:
        key = "improve_sent" if ident in changed else "improve_unchanged"
        print(f"{t(I18N, key, lang)}: {ident}")
    return 0


def handle_sync(args) -> int:
    root = _root(args)
    changelog = (Path(args.changelog).expanduser() if args.changelog
                 else plugin_root() / "CHANGELOG.md")
    moved = improve.sync(root, changelog)
    lang = _lang(root)
    if not moved:
        print(t(I18N, "improve_sync_none", lang))
    for ident in moved:
        print(f"{t(I18N, 'improve_implemented', lang)}: {ident}")
    return 0


def doctor_lines(root: Path | None) -> list[tuple[str, object]]:
    """`improvements_pending`; вне workspace — ничего."""
    return improve.doctor_lines(root)
