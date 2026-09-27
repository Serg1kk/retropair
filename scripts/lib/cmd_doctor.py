"""Команда `doctor`: состояние workspace, окружения и реестра.

Не падает на отсутствии чего-либо; битый JSON (config, реестр, `~/.claude/settings.json`) —
код 1. Другие файлы команд добавляют строки через `doctor_lines(root)` (см. `registry_cmds`).
"""

from __future__ import annotations

import json
import platform
import re
import subprocess
from pathlib import Path

from lib import registry, registry_cmds
from lib.people import list_people
from lib.version import __version__
from lib.workspace import SrError, find_workspace, read_json, ws_paths

NOT_FOUND = "не найден"
NOT_SET = "не задан"
NO_WORKSPACE = "нет workspace"
NOT_REGISTERED = "не в реестре"
SPECSTORY_TIMEOUT = 5


def register(subparsers) -> None:
    p = subparsers.add_parser("doctor", help="проверить workspace, окружение и реестр")
    p.add_argument("--json", action="store_true", help="вывод одним JSON-объектом")
    p.set_defaults(func=handle)


def _specstory_version() -> str:
    try:
        res = subprocess.run(
            ["specstory", "--version"], capture_output=True, text=True, timeout=SPECSTORY_TIMEOUT
        )
    except FileNotFoundError:
        return NOT_FOUND
    except subprocess.TimeoutExpired:
        return f"нет ответа за {SPECSTORY_TIMEOUT} с"
    if res.returncode != 0:
        return f"ошибка (код {res.returncode})"
    return specstory_version_line(res.stdout or res.stderr)


def specstory_version_line(out: str) -> str:
    """Строка с версией из вывода `specstory --version` (при первом запуске выше бывает баннер)."""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if not lines:
        return "пустой ответ"
    for line in lines:
        if re.match(r"^v?\d+\.\d+", line):
            return line
    return f"версия не распознана: {lines[0]}"


def _cleanup_period_days():
    path = Path.home() / ".claude" / "settings.json"
    if not path.is_file():
        return NOT_SET
    data = read_json(path)
    if not isinstance(data, dict):
        raise SrError(
            f"doctor: settings.json не объект: получен {type(data).__name__} ({path})", 1
        )
    return data.get("cleanupPeriodDays", NOT_SET)


def _load_config(root: Path) -> dict:
    path = ws_paths(root)["config"]
    cfg = read_json(path)
    if not isinstance(cfg, dict):
        raise SrError(f"doctor: config.json не объект: получен {type(cfg).__name__} ({path})", 1)
    return cfg


def _workspace_lines(root: Path | None, cfg: dict | None) -> list[tuple[str, object]]:
    if root is None or cfg is None:
        return [("workspace", NOT_FOUND), ("mode", NO_WORKSPACE), ("people", NO_WORKSPACE)]
    people = len(list_people(root))  # битая карточка — SrError(1), doctor выходит с кодом 1
    return [("workspace", str(root)), ("mode", cfg.get("mode", NOT_SET)), ("people", people)]


def _hook_enabled(cfg: dict | None):
    if cfg is None:
        return NO_WORKSPACE
    hook = cfg.get("hook")
    if not isinstance(hook, dict) or "enabled" not in hook:
        return NOT_SET
    return hook["enabled"]


def base_lines(root: Path | None) -> list[tuple[str, object]]:
    """Собственные строки doctor в фиксированном порядке."""
    cfg = _load_config(root) if root is not None else None
    lines = _workspace_lines(root, cfg)
    lines += [
        ("python", platform.python_version()),
        ("specstory", _specstory_version()),
        ("hook_enabled", _hook_enabled(cfg)),
        ("cleanup_period_days", _cleanup_period_days()),
        ("registry_count", len(registry.load())),
    ]
    if root is None:
        lines += [("last_run_at", NO_WORKSPACE), ("last_collect_at", NO_WORKSPACE)]
    else:
        entry = registry.find_by_path(root)
        if entry is None:
            lines += [("last_run_at", NOT_REGISTERED), ("last_collect_at", NOT_REGISTERED)]
        else:
            lines += [("last_run_at", entry.get("last_run_at")),
                      ("last_collect_at", entry.get("last_collect_at"))]
    lines.append(("plugin_version", __version__))
    return lines


def collect_doctor_lines(root: Path | None, modules=None) -> tuple[list[tuple[str, object]], int]:
    """Строки doctor и код выхода: свои строки + `doctor_lines(root)` из файлов команд.

    Исключение в чужом `doctor_lines`, неверная форма ответа или занятый ключ — строка
    `<файл>: ошибка …` и код 1.
    """
    if modules is None:
        modules = registry_cmds.discover()
    lines = base_lines(root)
    taken = {key for key, _ in lines}
    rc = 0
    for module in modules:
        extra = getattr(module, "doctor_lines", None)
        if extra is None:
            continue
        name = registry_cmds.module_file(module)
        try:
            produced = list(extra(root))
            for item in produced:
                if not (isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str)):
                    raise ValueError(f"ожидалась пара (str, значение), получено {item!r}")
                if item[0] in taken:
                    raise ValueError(f"ключ '{item[0]}' уже занят")
        except Exception as err:  # noqa: BLE001 — контракт: ошибка чужих строк видна в выводе
            lines.append((name, f"ошибка {type(err).__name__}: {err}"))
            taken.add(name)
            rc = 1
            continue
        for key, value in produced:
            lines.append((key, value))
            taken.add(key)
    return lines, rc


def _fmt(value) -> str:
    if value is None:
        return "нет"
    if isinstance(value, bool):
        return "да" if value else "нет"
    return str(value)


def format_text(lines: list[tuple[str, object]]) -> str:
    return "\n".join(f"{key}: {_fmt(value)}" for key, value in lines)


def handle(args) -> int:
    try:
        root = find_workspace(Path.cwd(), args.workspace)
    except SrError as err:
        if err.code != 2:
            raise
        root = None
    lines, rc = collect_doctor_lines(root)
    if args.json:
        print(json.dumps(dict(lines), ensure_ascii=False, indent=2))
    else:
        print(format_text(lines))
    return rc
