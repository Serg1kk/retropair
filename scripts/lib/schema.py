"""Валидатор на подмножестве JSON Schema и загрузка схем плагина.

Схемы лежат в `references/schemas/<name>.json`. Зависимостей нет (stdlib):
`jsonschema` не требуется. Поддержанные ключевые слова перечислены в
`SUPPORTED_KEYWORDS`; любое другое — проблема «не поддержано», чтобы схема не
недопроверялась молча.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from lib.version import plugin_root

SUPPORTED_KEYWORDS = frozenset({
    "type", "const", "enum", "maxLength", "minLength", "pattern", "minimum", "maximum",
    "required", "properties", "additionalProperties", "items", "minItems", "maxItems",
})
ANNOTATIONS = frozenset({"$schema", "$id", "title", "description", "default", "examples"})


def _level_unsupported(schema: dict, path: str) -> list[str]:
    return [
        f"{path}: ключевое слово {key!r} не поддержано"
        for key in schema
        if key not in SUPPORTED_KEYWORDS and key not in ANNOTATIONS
    ]


def unsupported_keywords(schema: dict, path: str = "$") -> list[str]:
    """Обойти схему целиком и вернуть все неподдержанные ключевые слова.

    Путь — по структуре схемы (`$.properties.x.items`), а не по данным.
    """
    problems = _level_unsupported(schema, path)
    for name, sub in (schema.get("properties") or {}).items():
        if isinstance(sub, dict):
            problems += unsupported_keywords(sub, f"{path}.properties.{name}")
    for key in ("items", "additionalProperties"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            problems += unsupported_keywords(sub, f"{path}.{key}")
    return problems


def _pattern_ok(pattern: str, value: str) -> bool:
    """`pattern` как в JSON Schema (поиск, не полное совпадение).

    В Python `$` совпадает и перед завершающим `\\n`; в JSON Schema — нет. Поэтому при
    шаблоне, который кончается на `$`, совпадение обязано доходить до конца строки.
    """
    match = re.search(pattern, value)
    if match is None:
        return False
    if pattern.endswith("$") and not pattern.endswith("\\$") and match.end() != len(value):
        return False
    return True


def mini_validate(node, schema: dict, path: str = "$") -> list[str]:
    """Валидатор на подмножестве JSON Schema, которым написаны схемы плагина.

    Поддержаны type, required, additionalProperties (false и как схема), properties,
    items, enum, const, minItems/maxItems, minimum/maximum, maxLength, minLength,
    pattern. Неизвестное ключевое слово на текущем уровне схемы — проблема
    «не поддержано» (путь — по данным).
    """
    problems: list[str] = _level_unsupported(schema, path)
    types = schema.get("type")
    if types:
        names = [types] if isinstance(types, str) else list(types)
        ok = False
        for name in names:
            if name == "object":
                ok = ok or isinstance(node, dict)
            elif name == "array":
                ok = ok or isinstance(node, list)
            elif name == "string":
                ok = ok or isinstance(node, str)
            elif name == "integer":
                ok = ok or (isinstance(node, int) and not isinstance(node, bool))
            elif name == "number":
                ok = ok or (isinstance(node, (int, float)) and not isinstance(node, bool))
            elif name == "boolean":
                ok = ok or isinstance(node, bool)
            elif name == "null":
                ok = ok or node is None
        if not ok:
            return problems + [f"{path}: тип {type(node).__name__}, а нужен {'/'.join(names)}"]
    def _same(a, b):
        # В Python True == 1, поэтому голое сравнение пропускало schema_version: true.
        if isinstance(a, bool) != isinstance(b, bool):
            return False
        return a == b

    if "const" in schema and not _same(node, schema["const"]):
        problems.append(f"{path}: должно быть {schema['const']!r}, а стоит {node!r}")
    if "enum" in schema and not any(_same(node, v) for v in schema["enum"]):
        problems.append(f"{path}: значение {node!r} вне списка допустимых")
    if isinstance(node, str) and "maxLength" in schema and len(node) > schema["maxLength"]:
        problems.append(f"{path}: строка длиннее {schema['maxLength']} знаков")
    if isinstance(node, str) and "minLength" in schema and len(node) < schema["minLength"]:
        problems.append(f"{path}: строка короче {schema['minLength']} знаков")
    if isinstance(node, str) and "pattern" in schema and not _pattern_ok(schema["pattern"], node):
        problems.append(f"{path}: {node!r} не подходит под шаблон {schema['pattern']}")
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        if "minimum" in schema and node < schema["minimum"]:
            problems.append(f"{path}: {node} меньше минимума {schema['minimum']}")
        if "maximum" in schema and node > schema["maximum"]:
            problems.append(f"{path}: {node} больше максимума {schema['maximum']}")
    if isinstance(node, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in node:
                problems.append(f"{path}: нет обязательного поля {name!r}")
        extra = schema.get("additionalProperties")
        if extra is False:
            for name in node:
                if name not in props:
                    problems.append(f"{path}: лишнее поле {name!r}, схема его не знает")
        for name, value in node.items():
            if name in props:
                problems += mini_validate(value, props[name], f"{path}.{name}")
            elif isinstance(extra, dict):
                # additionalProperties как СХЕМА: словарь с произвольными ключами и типом
                # значения. Без этой ветки такие поля не проверялись бы вовсе.
                problems += mini_validate(value, extra, f"{path}.{name}")
    if isinstance(node, list):
        if "minItems" in schema and len(node) < schema["minItems"]:
            problems.append(f"{path}: элементов {len(node)}, нужно минимум {schema['minItems']}")
        if "maxItems" in schema and len(node) > schema["maxItems"]:
            problems.append(f"{path}: элементов {len(node)}, нужно максимум {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(node):
                problems += mini_validate(item, item_schema, f"{path}[{i}]")
    return problems


def schema_path(name: str) -> Path:
    return plugin_root() / "references" / "schemas" / f"{name}.json"


def load_schema(name: str) -> dict:
    """Прочитать `references/schemas/<name>.json`.

    Нет файла → FileNotFoundError с путём; битый JSON → ValueError с путём.
    """
    path = schema_path(name)
    if not path.is_file():
        raise FileNotFoundError(f"retropair: схема не найдена: нет файла ({path})")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"retropair: схема не разбирается: {exc} ({path})") from exc


def validate(obj, name: str) -> list[str]:
    """Проверить `obj` по схеме `name`. Пустой список — всё сходится.

    Если в самой схеме есть неподдержанные ключевые слова, возвращаются они
    (данные по такой схеме честно не проверить).
    """
    schema = load_schema(name)
    broken = unsupported_keywords(schema)
    if broken:
        return broken
    return mini_validate(obj, schema)
