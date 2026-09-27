"""Конфиг workspace `.sessionretro/config.json`: загрузка, дефолт из шаблона, сохранение.

Схема — `references/schemas/config.json`; запись только после валидации.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib.schema import validate
from lib.version import plugin_root
from lib.workspace import SrError, read_json, write_json_atomic, ws_paths


def template_path() -> Path:
    return plugin_root() / "templates" / "config.template.json"


def check_config(cfg, path: Path) -> None:
    """Объект и схема; проблема — `SrError(1)` со списком путей полей."""
    if not isinstance(cfg, dict):
        raise SrError(f"config.json не объект: получен {type(cfg).__name__} ({path})", 1)
    problems = validate(cfg, "config")
    if problems:
        raise SrError(f"config.json не проходит схему: {'; '.join(problems)} ({path})", 1)


def load_config(root: Path) -> dict:
    """Читает и валидирует config.json; нет файла — код 2, битый JSON или схема — код 1."""
    path = ws_paths(root)["config"]
    if not path.is_file():
        raise SrError(f"config.json не найден: нет файла ({path})", 2)
    cfg = read_json(path)
    check_config(cfg, path)
    return cfg


def default_config(*, ws_id: str, title: str, mode: str, lang: str) -> dict:
    """Конфиг из `templates/config.template.json` с подставленными полями."""
    path = template_path()
    cfg = read_json(path)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("workspace"), dict):
        raise SrError(f"шаблон конфига повреждён: нет объекта workspace ({path})", 1)
    cfg["workspace"]["id"] = ws_id
    cfg["workspace"]["title"] = title
    cfg["mode"] = mode
    cfg["lang"] = lang
    if "{{" in json.dumps(cfg, ensure_ascii=False):
        raise SrError(f"в шаблоне конфига остались неподставленные поля ({path})", 1)
    return cfg


def save_config(root: Path, cfg: dict) -> None:
    """Валидирует и атомарно пишет config.json; невалидный конфиг не пишется (код 1)."""
    path = ws_paths(root)["config"]
    check_config(cfg, path)
    write_json_atomic(path, cfg)
