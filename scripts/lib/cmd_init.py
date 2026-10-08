"""Команда `init`: создаёт workspace `.retropair/` в папке проекта и регистрирует его."""

from __future__ import annotations

import shutil
from pathlib import Path

from lib import registry
from lib.config import check_config, default_config, load_config, save_config
from lib.people import add_person
from lib.version import __version__, plugin_root
from lib.workspace import SrError, ws_paths, write_json_atomic

MODES = ("self", "team", "course")
LANGS = ("ru", "en")
SELF_ID = "me"
SELF_NAME = {"ru": "Я", "en": "Me"}
DEFAULT_GROUP = "default"


def register(subparsers) -> None:
    p = subparsers.add_parser("init", help="создать workspace в папке проекта")
    p.add_argument("--mode", required=True, choices=MODES, help="self | team | course")
    p.add_argument("--title", help="заголовок workspace (по умолчанию — имя папки)")
    p.add_argument("--lang", choices=LANGS, default="ru", help="язык текстов (по умолчанию ru)")
    p.add_argument("--path", metavar="DIR", help="папка проекта (по умолчанию cwd)")
    p.set_defaults(func=handle)


def _templates() -> Path:
    return plugin_root() / "templates"


def handle(args) -> int:
    root = Path(args.path).expanduser() if args.path else Path.cwd()
    if not root.is_dir():
        raise SrError(f"init: папка проекта не найдена (--path: {root})", 2)
    root = root.resolve()
    paths = ws_paths(root)
    if paths["state_dir"].resolve() == registry.sr_home().resolve():
        raise SrError(
            f"init: домашняя папка не может быть workspace — там глобальная "
            f"{paths['state_dir'].name} плагина; укажите папку проекта (путь: {root})",
            2,
        )
    if paths["config"].exists():
        raise SrError(f"workspace уже создан: {paths['state_dir']}", 1)

    title = args.title if args.title is not None else root.name
    cfg = default_config(ws_id=registry.preview_id(root, title), title=title, mode=args.mode,
                         lang=args.lang)
    check_config(cfg, paths["config"])

    # Сначала файлы workspace, регистрация в реестре — последней: упавшая запись не оставляет
    # в реестре записи без workspace.
    paths["people"].mkdir(parents=True, exist_ok=True)
    save_config(root, cfg)
    shutil.copyfile(_templates() / "_default.yaml", paths["people"] / "_default.yaml")
    if args.mode == "self":
        add_person(root, SELF_ID, SELF_NAME[args.lang], DEFAULT_GROUP)
    write_json_atomic(paths["state"], {
        "formatVersion": 1,
        "pluginVersion": __version__,
        "storeKind": "workspace",
        "migrations": [],
        "lastSync": None,
    })
    entry = registry.register(root, title, args.mode, cfg["workspace"]["groups"])
    if entry["id"] != cfg["workspace"]["id"]:
        # id заняли между предпросмотром и регистрацией — источник правды реестр
        cfg = load_config(root)
        cfg["workspace"]["id"] = entry["id"]
        save_config(root, cfg)
    print(f"workspace создан: {paths['state_dir']}")
    print(f"id: {entry['id']} · режим: {args.mode} · язык: {args.lang}")
    return 0
