"""Команда `render-review`: собрать текст ревью прогона по шаблону и записать файл ревью."""

from __future__ import annotations

from pathlib import Path

from lib import render_review
from lib.workspace import find_workspace, ws_paths


def register(subparsers) -> None:
    p = subparsers.add_parser("render-review", help="собрать текст ревью по шаблону и проверить запреты")
    p.add_argument("judge", metavar="JUDGE", help="файл выхода судьи, записанный командой append")
    p.add_argument("--person", required=True, metavar="ID", help="id человека")
    p.add_argument("--run", metavar="RUN", help="id прогона (по умолчанию последний живой прогон человека)")
    p.add_argument("--template", metavar="FILE", help="файл шаблона вместо шаблона карточки или плагина")
    p.set_defaults(func=handle)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    path = render_review.write_review(root, args.judge, args.person, run_id=args.run, template=args.template)
    status = path.read_text(encoding="utf-8").split("status: ", 1)[1].split("\n", 1)[0]
    try:
        shown = path.relative_to(ws_paths(root)["state_dir"].parent).as_posix()
    except ValueError:
        shown = str(path)
    print(f"текст ревью записан: {shown} (status: {status})")
    return 0
