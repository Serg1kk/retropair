"""Хук SessionEnd: запускает фоновый `sr.py collect --quiet` для workspace текущего проекта.

Контракт (stdout хука попадает в контекст сессии):
- stdout всегда пуст, код выхода всегда 0;
- холостой путь (нет `.sessionretro/config.json` вверх от `cwd`) — только stdlib, без
  импорта `lib/`, быстрее 50 мс; глобальная `~/.sessionretro` workspace не считается;
- `hook.enabled != true` — выход без запуска;
- workspace не из реестра пользователя `~/.sessionretro/workspaces.json` (путь записи совпадает
  с корнем проекта) — collect не запускается: чужой клон с закоммиченным конфигом не должен
  собирать логи пользователя. Одна строка в stderr, в проект ничего не пишется; битый реестр —
  тоже строка в stderr;
- иначе строка `<ISO> hook session=<id>` в `.sessionretro/collect.log` и отвязанный процесс
  collect с выводом в тот же лог; лог больше 1 МБ перед записью обрезается до последних 200 КБ;
- любая ошибка (битый конфиг, нет прав) — строка в `collect.log`; если и лог не пишется — в stderr.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = ".sessionretro"
REGISTRY_NAME = "workspaces.json"
LOG_NAME = "collect.log"
LOG_MAX = 1024 * 1024
LOG_KEEP = 200 * 1024
SCRIPTS = Path(__file__).resolve().parent


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _find_root(start: Path) -> Path | None:
    """Папка с `.sessionretro/config.json` вверх от `start`, кроме глобальной `~/.sessionretro`."""
    try:
        glob = (Path.home() / STATE_DIR).resolve()
        current = start.resolve()
    except (OSError, RuntimeError):
        return None
    for candidate in (current, *current.parents):
        state = candidate / STATE_DIR
        if (state / "config.json").is_file() and state.resolve() != glob:
            return candidate
    return None


def _trim(log: Path) -> None:
    try:
        size = log.stat().st_size
    except FileNotFoundError:
        return
    if size <= LOG_MAX:
        return
    with open(log, "rb") as fh:
        fh.seek(size - LOG_KEEP)
        tail = fh.read()
    with open(log, "wb") as fh:
        fh.write(tail)


def _log_line(log: Path, line: str) -> None:
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        _trim(log)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as err:
        print(f"sessionretro hook: {line} (лог не пишется: {log}: {err})", file=sys.stderr)


def _enabled(config: Path) -> bool:
    """`hook.enabled is True`; битый JSON или не объект — исключение с понятным текстом."""
    try:
        cfg = json.loads(config.read_text(encoding="utf-8"))
    except ValueError as err:
        raise ValueError(f"config.json не разбирается: {err} ({config})") from err
    if not isinstance(cfg, dict):
        raise ValueError(f"config.json не объект: {type(cfg).__name__} ({config})")
    hook = cfg.get("hook")
    return isinstance(hook, dict) and hook.get("enabled") is True


class RegistryError(Exception):
    """Реестр workspaces не читается или не массив."""


def _registered(root: Path) -> bool:
    """Корень проекта есть в `~/.sessionretro/workspaces.json` (его пишет setup пользователя)."""
    path = Path.home() / STATE_DIR / REGISTRY_NAME
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise RegistryError(f"реестр не читается: {err} ({path})") from err
    if not isinstance(data, list):
        raise RegistryError(f"реестр не JSON-массив: {type(data).__name__} ({path})")
    want = str(root)
    return any(isinstance(e, dict) and e.get("path") == want for e in data)


def _launch(root: Path, log: Path, head: str) -> None:
    import subprocess  # только на пути запуска: холостой путь не платит за импорт

    log.parent.mkdir(parents=True, exist_ok=True)
    _trim(log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(head + "\n")
        fh.flush()
        subprocess.Popen(
            [sys.executable, str(SCRIPTS / "sr.py"), "collect", "--quiet", "--workspace", str(root)],
            stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, cwd=str(root),
        )


def main() -> int:
    payload = _read_payload()
    session = payload.get("session_id") or "?"
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        try:
            cwd = os.getcwd()
        except OSError:
            return 0
    root = _find_root(Path(cwd))
    if root is None:
        return 0
    log = root / STATE_DIR / LOG_NAME
    head = f"{_now()} hook session={session}"
    try:
        if not _enabled(root / STATE_DIR / "config.json"):
            return 0
    except Exception as err:  # noqa: BLE001 — хук не роняет сессию, ошибка видна в логе
        _log_line(log, f"{head}: ошибка {type(err).__name__}: {err}")
        return 0
    try:
        registered = _registered(root)
    except RegistryError as err:
        print(f"sessionretro hook: {err}; сбор не запущен ({root})", file=sys.stderr)
        return 0
    if not registered:
        print(f"sessionretro hook: workspace не зарегистрирован в ~/{STATE_DIR}/{REGISTRY_NAME} "
              f"({root}), сбор не запущен; запустите /sessionretro:setup", file=sys.stderr)
        return 0
    try:
        _launch(root, log, head)
    except Exception as err:  # noqa: BLE001 — хук не роняет сессию, ошибка видна в логе
        _log_line(log, f"{head}: ошибка {type(err).__name__}: {err}")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as err:  # noqa: BLE001 — последний рубеж: stdout пуст, код 0
        print(f"sessionretro hook: ошибка {type(err).__name__}: {err}", file=sys.stderr)
        code = 0
    sys.exit(code)
