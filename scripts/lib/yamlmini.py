"""Внутренний мини-парсер подмножества YAML для карточек `people/<id>.yaml` и
`_default.yaml`. Только чтение, stdlib, без зависимостей.

Подмножество, достаточное для people/<id>.yaml —
плоские схемы, формат которых мы контролируем сами:

- комментарии ``#`` (до конца строки, но не внутри кавычек) и пустые строки;
- ``key: scalar`` со скалярами null/~/bool/int/float/строка (в кавычках или без);
- вложенные блоки по отступам: блок-списки скаляров, блок-списки отображений,
  вложенные отображения на любую глубину.

НЕ поддерживаем (YAGNI): flow-стиль ``{a: 1}``/``[1, 2]``, якоря, многострочные
``|``/``>``, мульти-документ ``---``. Только пробелы для отступов (табы → ошибка).
"""


class YamlError(Exception):
    """Некорректный YAML для нашего подмножества (табы, битая структура)."""


def load(text):
    """Разобрать YAML-текст нашего подмножества в Python-структуру (dict/list)."""
    text = text.lstrip("﻿")  # срезать BOM в начале файла
    lines = _lex(text)
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, lines[0][0])
    return value


# --- лексер: строки → (indent, content, lineno), без комментов и пустых ---

def _lex(text):
    out = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.lstrip(" \t")
        lead = raw[: len(raw) - len(stripped)]
        content = _strip_inline_comment(stripped).rstrip()
        if not content:
            continue  # пустая или чисто комментная строка — табы в ней не важны
        if "\t" in lead:
            raise YamlError(
                f"табы запрещены в отступах, используйте пробелы (строка {lineno})"
            )
        out.append((len(lead), content, lineno))
    return out


def _strip_inline_comment(s):
    """Отбросить инлайн-коммент ``#`` вне кавычек (``#`` считается началом
    комментария только в начале или после пробела — как в YAML)."""
    quote = None
    prev = " "
    result = []
    for ch in s:
        if quote:
            result.append(ch)
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
            result.append(ch)
        elif ch == "#" and prev == " ":
            break
        else:
            result.append(ch)
        prev = ch
    return "".join(result)


# --- разбор по отступам (вложенные блоки) ---

def _parse_block(lines, i, indent):
    content = lines[i][1]
    if content == "-" or content.startswith("- "):
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _is_list_item(content):
    return content == "-" or content.startswith("- ")


def _child_block(lines, i, indent):
    """Разобрать блок-значение под строкой ``key:`` (lines[i]). Возвращает
    (value, next_i). Поддерживает блок-список на ТОМ ЖЕ отступе, что и ключ
    (мейнстримный YAML-стиль ``goals:\\n- a``); вложенный блок с бОльшим
    отступом; иначе значения нет (None)."""
    j = i + 1
    if j < len(lines):
        nxt_indent, nxt_content = lines[j][0], lines[j][1]
        if nxt_indent > indent:
            return _parse_block(lines, j, nxt_indent)
        if nxt_indent == indent and _is_list_item(nxt_content):
            return _parse_list(lines, j, indent)
    return None, i + 1


# Индикаторы block-скаляров YAML (folded/literal) — наше подмножество их НЕ поддерживает.
# Без явной ошибки `goals: >-` падал бы «неожиданным отступом» на СЛЕДУЮЩЕЙ строке —
# самая частая нога на живых профилях.
_BLOCK_SCALARS = {"|", "|-", "|+", ">", ">-", ">+"}


def _check_block_scalar(raw, lineno):
    if raw in _BLOCK_SCALARS:
        raise YamlError(
            f"folded/literal-скаляры ('{raw}') не поддерживаются, "
            f"используй quoted string (строка {lineno})"
        )


def _parse_map(lines, i, indent):
    result = {}
    while i < len(lines):
        cur_indent, content, lineno = lines[i]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise YamlError(f"неожиданный отступ (строка {lineno})")
        colon = _find_colon(content)
        if colon < 0:
            raise YamlError(f"ожидался 'key: value' (строка {lineno})")
        key = content[:colon].strip()
        rest = content[colon + 1:].strip()
        if rest:
            _check_block_scalar(rest, lineno)
            result[key] = _scalar(rest)
            i += 1
        else:
            result[key], i = _child_block(lines, i, indent)
    return result, i


def _parse_list(lines, i, indent):
    result = []
    while i < len(lines):
        cur_indent, content, lineno = lines[i]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise YamlError(f"неожиданный отступ (строка {lineno})")
        if not (content == "-" or content.startswith("- ")):
            break
        tail = content[1:]
        item_col = cur_indent + 1 + (len(tail) - len(tail.lstrip(" ")))
        after = tail.strip()
        if after == "":
            if i + 1 < len(lines) and lines[i + 1][0] > indent:
                value, i = _parse_block(lines, i + 1, lines[i + 1][0])
            else:
                value, i = None, i + 1
            result.append(value)
            continue
        if _find_colon(after) < 0:
            _check_block_scalar(after, lineno)
            result.append(_scalar(after))
            i += 1
        else:
            # элемент-отображение: первая пара на строке дефиса + продолжения
            # с отступом item_col → парсим как map, начиная с этой строки.
            lines[i] = (item_col, after, lineno)
            value, i = _parse_map(lines, i, item_col)
            result.append(value)
    return result, i


def _find_colon(s):
    """Индекс разделителя ключ/значение ``:`` верхнего уровня (вне кавычек),
    за которым идёт пробел или конец строки. -1 — разделителя нет (чистый скаляр)."""
    quote = None
    for idx, ch in enumerate(s):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == ":" and (idx + 1 == len(s) or s[idx + 1] == " "):
            return idx
    return -1


def _scalar(s):
    s = s.strip()
    if s == "" or s == "~" or s.lower() == "null":
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s
