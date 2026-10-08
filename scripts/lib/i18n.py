"""Строки интерфейса из `references/i18n.json`: секция → ключ → язык.

Секция задаётся строкой (`"axes"`) или кортежем пути (`("cli", "assess")`).
Нет секции, ключа или языка → `KeyError("i18n: нет <путь>")`: пустая строка в тексте
ревью хуже явной ошибки. Имена на `_` — служебные: ни секцией, ни ключом не считаются.
"""

from __future__ import annotations

import json
from functools import lru_cache

from lib.version import plugin_root


def _path():
    return plugin_root() / "references" / "i18n.json"


@lru_cache(maxsize=1)
def load() -> dict:
    """Прочитать `i18n.json` один раз за процесс (кеш)."""
    path = _path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"retropair: нет файла строк интерфейса ({path})") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"retropair: файл строк интерфейса не разбирается: {exc} ({path})") from exc


def _parts(section: str | tuple[str, ...]) -> tuple[str, ...]:
    return (section,) if isinstance(section, str) else tuple(section)


def _section(section: str | tuple[str, ...]) -> tuple[dict, tuple[str, ...]]:
    node = load()
    parts = _parts(section)
    for depth, name in enumerate(parts):
        if name.startswith("_") or not isinstance(node, dict) or not isinstance(node.get(name), dict):
            raise KeyError(f"i18n: нет {'.'.join(parts[:depth + 1])}")
        node = node[name]
    return node, parts


def t(section: str | tuple[str, ...], key: str, lang: str) -> str:
    """Строка `section.key.lang`; чего-то нет → `KeyError` с полным путём."""
    node, parts = _section(section)
    path = ".".join((*parts, key))
    entry = node.get(key)
    if key.startswith("_") or not isinstance(entry, dict):
        raise KeyError(f"i18n: нет {path}")
    value = entry.get(lang)
    if not isinstance(value, str) or not value:
        raise KeyError(f"i18n: нет {path}.{lang}")
    return value


def keys(section: str | tuple[str, ...]) -> list[str]:
    """Ключи секции в порядке файла, без служебных имён на `_`."""
    node, _ = _section(section)
    return [name for name in node if not name.startswith("_")]
