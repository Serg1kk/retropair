"""Проектные агенты: болванки плагина `templates/agents/*.md` → `<проект>/.claude/agents/`.

В болванке подставляются `{{model}}` и `{{effort}}` из `config.models.<роль>`. Файл, который
отличается от болванки только строками `model:`/`effort:` во frontmatter, переписывается (смена
модели); файл с правленым телом не трогается без `force` — вызывающий решает, ошибка это
(`agents write`, код 1) или совет (`setup-apply`).

Запись плагина: `<проект>/.retropair/agents.json` — `{роль: {sha256, plugin_version}}`, хэш тела
без строк `model:`/`effort:`. Тело совпадает с записанным хэшем, но не с болванкой — файл никто не
правил, вышла новая версия плагина (`outdated`): его можно обновить. Не совпадает ни с тем, ни с
другим (или записи нет) — правка человека (`body_edited`).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from lib.version import __version__, plugin_root
from lib.workspace import SrError, read_json, write_json_atomic, write_text_atomic, ws_paths

ROLES = {"collector": "log-collector", "judge": "log-judge", "critic": "feedback-critic"}
PREFIX = "retropair-"
_TUNED = re.compile(r"^(model|effort):")


def _name(role: str) -> str:
    if role not in ROLES:
        raise SrError(f"agents: неизвестная роль {role!r} (допустимы: {', '.join(ROLES)})", 2)
    return ROLES[role]


def stub_path(role: str) -> Path:
    """Болванка роли в плагине: `templates/agents/<имя>.md`."""
    return plugin_root() / "templates" / "agents" / f"{_name(role)}.md"


def target(root: Path, role: str) -> Path:
    """Файл агента в проекте: `<root>/.claude/agents/retropair-<имя>.md`."""
    folder = Path(root) / ".claude" / "agents"
    new = folder / f"{PREFIX}{_name(role)}.md"
    old = folder / f"sessionretro-{_name(role)}.md"  # customized legacy agents remain authoritative
    if old.exists() and new.exists():
        raise SrError(f"agents: конфликт старого и нового агента: {old}, {new}; "
                      "выберите один файл вручную, автоматической перезаписи нет", 1)
    return old if old.is_file() else new


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise SrError(f"agents: файл не найден ({path})", 1) from err
    except UnicodeDecodeError as err:
        raise SrError(f"agents: файл не в UTF-8: {err.reason} ({path})", 1) from err


def render(role: str, model: str, effort: str) -> str:
    """Болванка роли с подставленными model/effort; остаток `{{` — `SrError(1)` с путём болванки."""
    path = stub_path(role)
    text = _read(path).replace("{{model}}", str(model)).replace("{{effort}}", str(effort))
    if "{{" in text:
        raise SrError(f"agents: в болванке остались неподставленные поля ({path})", 1)
    return text


def _frontmatter_end(lines: list[str]) -> int | None:
    """Индекс закрывающей `---` frontmatter или None, если frontmatter нет."""
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            return i
    return None


def _without_tuning(text: str) -> str:
    """Текст без строк `model:`/`effort:` внутри frontmatter."""
    lines = text.splitlines(keepends=True)
    end = _frontmatter_end(lines)
    if end is None:
        return text
    head = [line for line in lines[1:end] if not _TUNED.match(line)]
    return "".join([lines[0], *head, *lines[end:]])


def _field(text: str, key: str) -> str | None:
    lines = text.splitlines()
    end = _frontmatter_end(lines)
    if end is None:
        return None
    for line in lines[1:end]:
        if line.startswith(f"{key}:"):
            return line[len(key) + 1:].strip()
    return None


def record_path(root: Path) -> Path:
    """Запись плагина о телах агентов: `<root>/.retropair/agents.json`."""
    return ws_paths(root)["state_dir"] / "agents.json"


def _load_record(root: Path) -> dict:
    path = record_path(root)
    if not path.is_file():
        return {}
    data = read_json(path)  # битый JSON — SrError(1) с путём
    if not isinstance(data, dict):
        raise SrError(f"agents: agents.json не объект: получен {type(data).__name__} ({path})", 1)
    return data


def _sha(text: str) -> str:
    return hashlib.sha256(_without_tuning(text).encode("utf-8")).hexdigest()


def _written_by_plugin(record: dict, role: str, text: str) -> bool:
    """Тело файла совпадает с тем, что плагин записал в прошлый раз."""
    entry = record.get(role)
    return isinstance(entry, dict) and entry.get("sha256") == _sha(text)


def _role_models(cfg: dict, role: str) -> tuple[str, str]:
    try:
        spec = cfg["models"][role]
        return spec["model"], spec["effort"]
    except (KeyError, TypeError) as err:
        raise SrError(f"agents: в конфиге нет models.{role}.model/effort", 1) from err


def status(root: Path, cfg: dict) -> list[dict]:
    """По роли: есть ли файл, совпадают ли model/effort с конфигом, правлено ли тело человеком
    (`body_edited`) или просто отстало от новой болванки плагина (`outdated`)."""
    record = _load_record(root)
    out = []
    for role in ROLES:
        model, effort = _role_models(cfg, role)
        path = target(root, role)
        item = {"role": role, "path": str(path), "exists": path.is_file(), "model_ok": False,
                "effort_ok": False, "body_edited": False, "outdated": False}
        if item["exists"]:
            text = _read(path)
            item["model_ok"] = _field(text, "model") == model
            item["effort_ok"] = _field(text, "effort") == effort
            if _without_tuning(text) != _without_tuning(render(role, model, effort)):
                plugin = _written_by_plugin(record, role, text)
                item["outdated"], item["body_edited"] = plugin, not plugin
        out.append(item)
    return out


def write(root: Path, cfg: dict, force: bool = False) -> dict:
    """Записать агентов ролей; `{"created", "updated", "unchanged", "skipped_edited"}` — роли.

    Правленый вручную файл без `force` попадает в `skipped_edited` и не трогается; файл, который
    плагин записал и никто не правил, обновляется до текущей болванки (`updated`). Симлинк остаётся
    симлинком: пишется его цель.
    """
    res = {"created": [], "updated": [], "unchanged": [], "skipped_edited": []}
    old_record = _load_record(root)
    record = dict(old_record)
    for role in ROLES:
        text = render(role, *_role_models(cfg, role))
        path = target(root, role)
        if not path.is_file():
            write_text_atomic(path.resolve(), text)
            res["created"].append(role)
        else:
            have = _read(path)
            if have == text:
                res["unchanged"].append(role)
            elif (_without_tuning(have) != _without_tuning(text) and not force
                  and not _written_by_plugin(old_record, role, have)):
                res["skipped_edited"].append(role)
                continue
            else:
                write_text_atomic(path.resolve(), text)
                res["updated"].append(role)
        entry = {"sha256": _sha(text), "plugin_version": __version__}
        prev = record.get(role)
        if not (isinstance(prev, dict) and prev.get("sha256") == entry["sha256"]):
            record[role] = entry
    if record != old_record:
        write_json_atomic(record_path(root).resolve(), record)
    return res


def sync(root: Path, cfg: dict) -> dict:
    """После обновления плагина: обновить только «устаревшие» файлы (тело = записанный отпечаток,
    болванка новее) до текущей болванки с model/effort из конфига.

    Отчёт `{"updated", "unchanged", "edited", "missing", "record"}`: роли по спискам, `record` —
    есть ли `agents.json`. Правленые (и неразличимые без записи) файлы и отсутствующие не трогает,
    новых файлов не создаёт: нет файла — ретро берёт агента плагина.
    """
    res = {"updated": [], "unchanged": [], "edited": [], "missing": [],
           "record": record_path(root).is_file()}
    record = _load_record(root)
    changed = False
    for item in status(root, cfg):
        role = item["role"]
        if not item["exists"]:
            res["missing"].append(role)
        elif item["body_edited"]:
            res["edited"].append(role)
        elif item["outdated"]:
            text = render(role, *_role_models(cfg, role))
            write_text_atomic(target(root, role).resolve(), text)
            record[role] = {"sha256": _sha(text), "plugin_version": __version__}
            res["updated"].append(role)
            changed = True
        else:
            res["unchanged"].append(role)
    if changed:
        write_json_atomic(record_path(root).resolve(), record)
    return res
