"""Карточки людей `.retropair/people/<id>.yaml`: чтение, список, добавление, срез для судьи.

Эффективная карточка = карточка + `feedback` из `people/_default.yaml` (поля карточки
перекрывают дефолт, пустое значение в карточке дефолт не трогает), пустые списки — `[]`;
результат проходит схему `references/schemas/person.json`.
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from lib import yamlmini
from lib.config import load_config, save_config
from lib.schema import validate
from lib.version import plugin_root
from lib.workspace import SrError, ws_paths

PERSON_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
DEFAULT_CARD = "_default"
LIST_FIELDS = ("skip_basics", "boundaries", "avoid")
OBJECT_FIELDS = ("profile", "consent")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def template_path() -> Path:
    return plugin_root() / "templates" / "person.template.yaml"


def check_id(pid: str) -> None:
    """Неверный формат id — `SrError(2)`."""
    # fullmatch: у `match` с `$` проходил бы id с завершающим переводом строки
    if not isinstance(pid, str) or not PERSON_ID_RE.fullmatch(pid):
        raise SrError(
            f"неверный id человека: нужен формат {PERSON_ID_RE.pattern} (id: {pid!r})", 2
        )


def _read_yaml(path: Path) -> dict:
    try:
        data = yamlmini.load(path.read_text(encoding="utf-8"))
    except yamlmini.YamlError as err:
        raise SrError(f"карточка не разбирается: {err} ({path})", 1) from err
    if not isinstance(data, dict):
        raise SrError(f"карточка не отображение: получен {type(data).__name__} ({path})", 1)
    return data


def _default_feedback(root: Path) -> dict:
    path = ws_paths(root)["people"] / f"{DEFAULT_CARD}.yaml"
    if not path.is_file():
        raise SrError(f"нет файла дефолтов карточек ({path})", 1)
    feedback = _read_yaml(path).get("feedback")
    if not isinstance(feedback, dict):
        raise SrError(f"в файле дефолтов нет блока feedback ({path})", 1)
    return feedback


def _effective(card: dict, default_fb: dict, path: Path) -> dict:
    """Слияние карточки с `_default.feedback`, нормализация пустых значений, схема."""
    card = dict(card)
    own = card.get("feedback")
    if own is None:
        own = {}
    if not isinstance(own, dict):
        raise SrError(f"feedback карточки не отображение: получен {type(own).__name__} ({path})", 1)
    feedback = dict(default_fb)
    feedback.update({key: value for key, value in own.items() if value is not None})
    for key in LIST_FIELDS:
        if feedback.get(key) is None:
            feedback[key] = []
    card["feedback"] = feedback
    if "sources" in card and card["sources"] is None:
        card["sources"] = []
    for key in OBJECT_FIELDS:
        if key in card and card[key] is None:
            card[key] = {}
    problems = validate(card, "person")
    if problems:
        raise SrError(f"карточка не проходит схему: {'; '.join(problems)} ({path})", 1)
    return card


def card_path(root: Path, pid: str) -> Path:
    check_id(pid)
    return ws_paths(root)["people"] / f"{pid}.yaml"


def load_person(root: Path, pid: str) -> dict:
    """Эффективная карточка; нет файла или неверный id — код 2, битая карточка — код 1."""
    path = card_path(root, pid)
    if not path.is_file():
        raise SrError(f"человек не найден: нет карточки (id: {pid}, файл: {path})", 2)
    card = _read_yaml(path)
    raw_id = card.get("id")
    if raw_id is not None and not isinstance(raw_id, str):
        raise SrError(
            f"id в карточке не строка (в файле: {raw_id!r}, тип {type(raw_id).__name__}): "
            f'возьмите id в кавычки — id: "{pid}" ({path})',
            1,
        )
    if raw_id != pid:
        raise SrError(
            f"id в карточке не совпадает с именем файла (в файле: {card.get('id')!r}, "
            f"ожидался: {pid!r}; {path})",
            1,
        )
    return _effective(card, _default_feedback(root), path)


def list_people(root: Path) -> list[dict]:
    """Эффективные карточки всех людей workspace (кроме `_default`), по id."""
    folder = ws_paths(root)["people"]
    if not folder.is_dir():
        return []
    cards = []
    for path in sorted(folder.glob("*.yaml")):
        if path.stem == DEFAULT_CARD or path.name.startswith("."):
            continue  # скрытые файлы — lock-файлы и копии редакторов (`.#alex.yaml`)
        if not PERSON_ID_RE.fullmatch(path.stem):
            raise SrError(
                f"имя файла карточки не подходит под id {PERSON_ID_RE.pattern} ({path})", 1
            )
        cards.append(load_person(root, path.stem))
    return cards


def quote_name(value: str) -> str:
    """Строка для YAML-скаляра: `"…"`, а если внутри `"` — `'…'`.

    yamlmini не разворачивает экранирование, поэтому обе кавычки сразу, перевод строки и
    управляющие символы — `SrError(2)`.
    """
    if _CONTROL_RE.search(value):
        raise SrError(
            f"значение с переводом строки или управляющим символом не записать в карточку "
            f"({value!r})",
            2,
        )
    if '"' in value and "'" in value:
        raise SrError(f"значение с обеими кавычками не записать в карточку ({value!r})", 2)
    if '"' in value:
        return f"'{value}'"
    return f'"{value}"'


def add_person(root: Path, pid: str, name: str, group: str) -> tuple[Path, bool]:
    """Пишет карточку из `templates/person.template.yaml`; возвращает (путь, группа добавлена).

    Неверный id, пустое имя или группа, недопустимые символы — код 2; карточка уже есть — код 1;
    группы нет в `workspace.groups` — добавляется в config.json (после записи карточки:
    упавшая запись карточки не оставляет группу в конфиге). Подстановка — один проход,
    поэтому `{{…}}` в имени сохраняется литералом.
    """
    path = card_path(root, pid)
    if name == "":
        raise SrError(f"пустое имя человека (id: {pid})", 2)
    if group == "":
        raise SrError(f"пустая группа (id: {pid})", 2)
    values = {
        "id": pid,
        "name": quote_name(name),
        "group": quote_name(group),
        "date": _dt.date.today().isoformat(),
    }
    if path.exists():
        raise SrError(f"карточка уже есть (id: {pid}, файл: {path})", 1)

    template = template_path()
    text = template.read_text(encoding="utf-8")
    unknown = sorted(set(_PLACEHOLDER_RE.findall(text)) - set(values))
    if unknown:
        raise SrError(
            f"в шаблоне карточки неизвестные поля: {', '.join(unknown)} ({template})", 1
        )
    text = _PLACEHOLDER_RE.sub(lambda m: values[m[1]], text)
    try:
        card = yamlmini.load(text)
    except yamlmini.YamlError as err:
        raise SrError(f"шаблон карточки не разбирается после подстановки: {err} ({template})", 1) from err
    if not isinstance(card, dict) or card.get("name") != name or card.get("group") != group:
        raise SrError(
            f"карточка не читается обратно без изменений (name: {name!r}, group: {group!r}; "
            f"{template})",
            1,
        )
    _effective(card, _default_feedback(root), path)

    cfg = load_config(root)
    groups = cfg["workspace"]["groups"]
    added = group not in groups
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as fh:
        fh.write(text)
    if added:
        groups.append(group)
        save_config(root, cfg)
    return path, added


def _yes(value) -> str:
    return "да" if value else "нет"


def _years(value) -> str:
    n = abs(value) % 100
    if 11 <= n <= 14:
        word = "лет"
    elif n % 10 == 1:
        word = "год"
    elif 2 <= n % 10 <= 4:
        word = "года"
    else:
        word = "лет"
    return f"{value} {word}"


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def feedback_language(card: dict, cfg: dict) -> str:
    """Язык текста участнику: profile.preferred_language → config.lang.

    Старые карточки без предпочтения используют язык workspace; историю не меняет.
    """
    preferred = (card.get("profile") or {}).get("preferred_language")
    lang = preferred if preferred is not None else cfg["lang"]
    if lang not in ("ru", "en"):
        raise SrError(f"неподдерживаемый язык фидбэка {lang!r}: нужен ru или en", 1)
    return lang


def person_context(card: dict) -> list[str]:
    """Срез карточки для судьи: 5–9 строк (`channel`, `template`, `prefix` не подаются).

    Всегда есть строки профиля («не указан», если пусто), обращения, флагов, границ
    («нет», если пусто) и ценности; ограничения и «не объяснять» — только если заполнены.
    """
    profile = card.get("profile") or {}
    fb = card["feedback"]
    head = [_text(profile.get("role")), _text(profile.get("grade"))]
    if profile.get("years") is not None:
        head.append(_years(profile["years"]))
    head = [part for part in head if part]
    first = [f"профиль: {', '.join(head) if head else 'не указан'}"]
    if _text(profile.get("stack")):
        first.append(f"стек: {_text(profile.get('stack'))}")
    if _text(profile.get("tools")):
        first.append(f"инструменты: {_text(profile.get('tools'))}")
    lines = ["; ".join(first)]
    if profile.get("preferred_language") is not None:
        lines.append(f"preferred_language: {profile['preferred_language']}")
    if _text(profile.get("constraints")):
        lines.append(f"ограничения: {_text(profile.get('constraints'))}")
    lines.append(f"обращение: {fb['address']}; длина: {fb['length']}; тон: {fb['tone']}")
    lines.append(
        f"открывать обобщением: {_yes(fb['opening_summary'])}; "
        f"подписывать разделы: {_yes(fb['mark_sections'])}; "
        f"приёмы исправления: {_yes(fb['practices'])}"
    )
    if fb["skip_basics"]:
        lines.append(f"не объяснять: {'; '.join(fb['skip_basics'])}")
    limits = [f"границы: {', '.join(fb['boundaries']) if fb['boundaries'] else 'нет'}"]
    if fb["avoid"]:
        limits.append(f"избегать: {', '.join(fb['avoid'])}")
    lines.append("; ".join(limits))
    lines.append(f"ценность: {fb['value_focus']}; динамика: {_yes(fb['show_dynamics'])}")
    return lines
