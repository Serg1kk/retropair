"""Глобальный реестр workspaces: `~/.retropair/workspaces.json`."""

from __future__ import annotations

import re
from pathlib import Path

from lib.lock import file_lock
from lib.version import __version__
from lib.workspace import STATE_DIR, require_current_state, SrError, read_json, write_json_atomic

UPDATABLE = ("path", "title", "mode", "groups", "last_run_at", "last_collect_at")

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya", "і": "i", "ї": "yi", "є": "ye", "ў": "u", "ґ": "g",
}


def sr_home() -> Path:
    require_current_state(Path.home())
    return Path.home() / STATE_DIR


def workspaces_path() -> Path:
    return sr_home() / "workspaces.json"


def slugify(text: str) -> str:
    """Латиница, цифры и дефис; кириллица транслитерируется; пусто — `workspace`."""
    lowered = "".join(_TRANSLIT.get(ch, ch) for ch in text.lower())
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "workspace"


def _load_unlocked() -> list[dict]:
    path = workspaces_path()
    if not path.exists():
        return []
    data = read_json(path)
    if not isinstance(data, list):
        raise SrError(
            f"реестр workspaces повреждён: ожидался JSON-массив, получен "
            f"{type(data).__name__} (файл: {path})",
            1,
        )
    return data


def load() -> list[dict]:
    return _load_unlocked()


def _ensure_plugin_state() -> None:
    state = sr_home() / "plugin-state.json"
    if state.exists():
        return
    write_json_atomic(state, {
        "formatVersion": 1,
        "pluginVersion": __version__,
        "storeKind": "global",
        "migrations": [],
        "lastSync": None,
    })


def _norm(root: Path) -> str:
    return str(Path(root).expanduser().resolve())


def _free_id(entries: list[dict], title: str) -> str:
    taken = {e.get("id") for e in entries}
    base = slugify(title)
    ws_id, n = base, 1
    while ws_id in taken:
        n += 1
        ws_id = f"{base}-{n}"
    return ws_id


def preview_id(root: Path, title: str) -> str:
    """id, который `register` выдал бы сейчас, — без записи и без создания `~/.retropair`.

    Между предпросмотром и регистрацией id может занять другой процесс: вызывающий
    сверяет результат `register` и при расхождении берёт id из реестра.
    """
    path_str = _norm(root)
    entries = _load_unlocked()
    for entry in entries:
        if entry.get("path") == path_str:
            return entry["id"]
    return _free_id(entries, title)


def register(root: Path, title: str, mode: str, groups: list[str]) -> dict:
    """Регистрирует workspace; по пути идемпотентно (запись обновляется, id сохраняется)."""
    path_str = _norm(root)
    with file_lock(workspaces_path()):
        entries = _load_unlocked()
        for entry in entries:
            if entry.get("path") == path_str:
                entry.update(title=title, mode=mode, groups=list(groups))
                result = entry
                break
        else:
            ws_id = _free_id(entries, title)
            result = {
                "id": ws_id,
                "path": path_str,
                "title": title,
                "mode": mode,
                "groups": list(groups),
                "last_run_at": None,
                "last_collect_at": None,
            }
            entries.append(result)
        write_json_atomic(workspaces_path(), entries)
        _ensure_plugin_state()
    return result


def update(ws_id: str, **fields) -> dict:
    """Меняет известные поля записи; неизвестный ключ или id — `SrError(1)`."""
    unknown = sorted(set(fields) - set(UPDATABLE))
    if unknown:
        raise SrError(
            f"реестр workspaces: неизвестные поля записи: {', '.join(unknown)} "
            f"(id: {ws_id}; допустимы: {', '.join(UPDATABLE)})",
            1,
        )
    if "path" in fields:
        fields["path"] = _norm(fields["path"])
    with file_lock(workspaces_path()):
        entries = _load_unlocked()
        for entry in entries:
            if entry.get("id") == ws_id:
                entry.update(fields)
                write_json_atomic(workspaces_path(), entries)
                return entry
    raise SrError(f"реестр workspaces: нет записи с таким id (id: {ws_id}, файл: {workspaces_path()})", 1)


def remove(ws_id: str) -> None:
    """Удаляет запись; неизвестный id — `SrError(1)`."""
    with file_lock(workspaces_path()):
        entries = _load_unlocked()
        kept = [e for e in entries if e.get("id") != ws_id]
        if len(kept) == len(entries):
            raise SrError(
                f"реестр workspaces: нет записи с таким id (id: {ws_id}, файл: {workspaces_path()})", 1
            )
        write_json_atomic(workspaces_path(), kept)


def find_by_path(root: Path) -> dict | None:
    path_str = _norm(root)
    for entry in load():
        if entry.get("path") == path_str:
            return entry
    return None
