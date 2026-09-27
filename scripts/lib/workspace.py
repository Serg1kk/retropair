"""Workspace проекта: поиск `.sessionretro/`, пути состояния, JSON-ввод/вывод, SrError."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path

STATE_DIR = ".sessionretro"


class SrError(Exception):
    """Ожидаемая ошибка sessionretro: `sr.py` печатает `message` в stderr и выходит с `code`.

    Коды: 1 — данные не сошлись / гейт / валидация; 2 — ошибка вызова.
    """

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _global_state_dir() -> Path:
    return (Path.home() / STATE_DIR).resolve()


def _is_workspace_root(root: Path) -> bool:
    state = root / STATE_DIR
    return state.resolve() != _global_state_dir() and (state / "config.json").is_file()


def find_workspace(start: Path | None = None, explicit: str | None = None) -> Path:
    """Корень проекта — папка, где лежит `.sessionretro/config.json`.

    `explicit` — корень проекта или сама папка `.sessionretro`; иначе поиск вверх от
    `start` (по умолчанию cwd). Глобальная `~/.sessionretro` workspace не считается.
    """
    if explicit is not None:
        given = Path(explicit).expanduser().resolve()
        root = given.parent if given.name == STATE_DIR else given
        if not _is_workspace_root(root):
            raise SrError(
                f"workspace не найден: в указанной папке нет {STATE_DIR}/config.json "
                f"(--workspace: {explicit})",
                2,
            )
        return root

    origin = Path(start) if start is not None else Path.cwd()
    current = origin.resolve()
    for candidate in (current, *current.parents):
        if _is_workspace_root(candidate):
            return candidate
    raise SrError(
        f"workspace не найден: нет {STATE_DIR}/config.json ни в папке, ни выше "
        f"(старт: {origin}); создайте его командой init или укажите --workspace",
        2,
    )


def ws_paths(root: Path) -> dict[str, Path]:
    """Пути состояния workspace (раскладка `.sessionretro/`)."""
    state = Path(root) / STATE_DIR
    return {
        "state_dir": state,
        "config": state / "config.json",
        "people": state / "people",
        "logs": state / "logs",
        "logs_index": state / "logs-index.json",
        "runs": state / "runs",
        "pending": state / "runs" / "pending",
        "reviews": state / "reviews",
        "reports": state / "reports",
        "templates": state / "templates",
        "improvements": state / "improvements",
        "state": state / "plugin-state.json",
    }


def _current_umask() -> int:
    # Прочитать umask можно только установив его; сразу возвращаем прежнее значение.
    mask = os.umask(0)
    os.umask(mask)
    return mask


def write_json_atomic(path: Path, data) -> None:
    """Пишет JSON через временный файл в той же папке и `os.replace` (см. `write_text_atomic`)."""
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_text_atomic(path: Path, text: str) -> None:
    """Пишет текст через временный файл в той же папке и `os.replace`; переводы строк как есть.

    Права: как у существующего файла, для нового — `0o666 & ~umask` (как у обычного
    `open`), а не 0600 от `mkstemp`.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o666 & ~_current_umask()
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path):
    """Читает JSON; битый файл — `SrError(1)` с путём и текстом ошибки парсера."""
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise SrError(f"битый JSON: {path}: {err}", 1) from err


# ── переносимые пути (без логина) ────────────────────────────────────────────


def _bases(root: Path | None) -> list[tuple[str, str, str]]:
    """(база, замена для «база/», замена для голой базы): корень проекта → «», «.»; HOME → «~/», «~».

    Каждая база — как дана и после `resolve()`; длинные раньше коротких (проект обычно внутри HOME)."""
    out: dict[str, tuple[str, str]] = {}
    home = Path.home()
    for base in {str(home), str(home.resolve())}:
        out[base] = ("~/", "~")
    if root is not None:
        for base in {str(Path(root)), str(Path(root).resolve())}:
            out[base] = ("", ".")
    return sorted(((b, *r) for b, r in out.items() if b not in ("", "/")),
                  key=lambda t: len(t[0]), reverse=True)


def _unmunge_home(text: str) -> str:
    """`-Users-<логин>-…` (имя папки сессий Claude Code: не-буквы cwd → `-`) → `-~-…`."""
    home = Path.home()
    for base in sorted({str(home), str(home.resolve())}, key=len, reverse=True):
        munged = re.sub(r"[^A-Za-z0-9]", "-", base)
        if munged.strip("-"):
            text = re.sub(re.escape(munged) + r"(?=-|/|$|[\s)\],;:'\"])", "-~", text)
    return text


def portable_path(path: Path | str, root: Path | None = None) -> str:
    """Путь для файлов состояния: внутри проекта — относительный от корня, внутри HOME — `~/…`,
    иначе как есть; munged-домашний путь в имени папки сессий → `-~`. Логин из домашнего пути так в
    `.meta.json` и `logs-index.json` не попадает."""
    text = str(path)
    for base, sub, bare in _bases(root):
        if text == base:
            return bare
        if text.startswith(base + "/"):
            return _unmunge_home(sub + text[len(base) + 1:])
    return _unmunge_home(text)


def portable_text(text: str, root: Path | None = None) -> str:
    """То же для путей внутри строки (предупреждения): `<root>/x` → `x`, `<HOME>/x` → `~/x`.

    База заменяется, только если за ней `/` или конец имени (пробел, скобка, кавычка, конец строки)."""
    for base, sub, bare in _bases(root):
        pattern = re.compile(re.escape(base) + r"(?:(/)|(?=[\s)\],;:'\"]|$))")
        text = pattern.sub(lambda m: sub if m.group(1) else bare, text)
    return _unmunge_home(text)
