"""Запреты текста для человека (условие 9 «Проверок скриптом» в `references/output-contract.md`).

Один набор правил на два места: валидатор трекера проверяет им каждое поле `feedback_parts`,
рендер — собранный отправляемый блок. Проверяется только проза: содержимое кавычек «…», “…”,
„…“, "…" и `…` — дословная цитата, в ней законны тире, id, цифры, `>` и чужое обращение.
"""

from __future__ import annotations

import re

from lib import i18n

QUOTES_RE = re.compile(r"«[^»]*»|“[^”]*”|„[^“”]*[“”]|\"[^\"\n]*\"|`[^`\n]*`")
SCORE_RE = re.compile(
    r"(?i)\bбалл\w*|\bscore\w*|\bpoints?\s+out\s+of\b|\b\d+\s*(?:из|out\s+of)\s*(?:4|5|10|100)\b"
    r"|\b\d+\s*/\s*(?:4|5|10|100)\b|\bуровень\s+[0-4]\b|\blevel\s+[0-4]\b"
)
SNAKE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
REC_RE = re.compile(r"\bREC-[\w-]*")
HEX_RE = re.compile(r"(?i)\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b")
# en-слова уровней, которые в обычной речи законны («while developing the API»): запрещены
# только рядом со словом level (или «уровень»).
LEVEL_WORDS_WITH_LEVEL = {"competent", "naive", "developing", "insufficient"}
# Ключи уровней, чьи ru-имена тоже обычная речь («в логах мало данных», «навык развивается»;
# «мало данных» к тому же термин судьи для нехватки данных): запрещены только рядом со словом
# «уровень» (или level).
RU_LEVEL_KEYS_WITH_LEVEL = {"developing", "insufficient"}
LEVEL_WORD = r"(?:level|уровень|уровн\w*)"
# Закрытый список сравнений с другими людьми (ru/en), ищется в прозе без учёта регистра.
COMPARISONS = (
    "лучше, чем другие", "хуже, чем другие", "лучше других", "хуже других", "чем у других",
    "чем остальные", "у других", "по сравнению с другими", "в отличие от других", "как у всех",
    "в группе", "выше среднего", "ниже среднего", "среди коллег",
    "than others", "than the others", "than other people", "than your peers", "than your colleagues",
    "compared to others", "compared with others", "unlike others", "above average", "below average",
    "in the group", "among your peers",
)
# Маркеры старой формы текста: приёмы и рекомендация отдельными секциями после критериев.
OLD_FORM_MARKERS = ("Приёмы под то, что стоит подтянуть", "Рекомендация (попробовать)")
TY_FORMS = ("ты", "тебя", "тебе", "тобой", "тобою", "твой", "твоя", "твоё", "твое", "твои", "твоего",
            "твоей", "твоему", "твоим", "твоими", "твоих", "твою")
VY_FORMS = ("вы", "вас", "вам", "вами", "ваш", "ваша", "ваше", "ваши", "вашего", "вашей", "вашему",
            "вашим", "вашими", "ваших", "вашу")
WRONG_ADDRESS = {"ты": VY_FORMS, "вы": TY_FORMS}


def prose_outside_quotes(text) -> str:
    """Текст без содержимого кавычек: цитата дословна и живёт по своим правилам."""
    return QUOTES_RE.sub(" ", text) if isinstance(text, str) else ""


def _level_patterns() -> list[tuple[re.Pattern, str]]:
    found = []
    for key in i18n.keys("levels"):
        en, ru = i18n.t("levels", key, "en"), i18n.t("levels", key, "ru")
        names = {key, key.replace("_", " "), en, ru}
        for name in names:
            word = re.escape(name)
            if (name.lower() in LEVEL_WORDS_WITH_LEVEL or (name == en and key in LEVEL_WORDS_WITH_LEVEL)
                    or (name == ru and key in RU_LEVEL_KEYS_WITH_LEVEL)):
                pattern = (rf"(?i)(?<!\w){LEVEL_WORD}(?!\w)\W+(?:\w+\W+)?{word}(?!\w)"
                           rf"|(?<!\w){word}\W+(?:\w+\W+)?{LEVEL_WORD}(?!\w)")
            else:
                pattern = rf"(?i)(?<!\w){word}(?!\w)"
            found.append((re.compile(pattern), name))
    return found


def _fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def prose_problems(prose: str, address: str | None = None) -> list[str]:
    """Нарушения запретов в прозе `prose` (кавычки уже вырезаны); пусто — чисто.

    Баллы и шкала, имена и ключи уровней, `snake_case`, `REC-`, id сессий, длинное тире,
    сравнения с другими, обращение против `address` карточки (`ты`/`вы`), маркеры старой
    формы, `>` вне цитат. Каждая строка начинается с названия запрета.
    """
    problems: list[str] = []

    def hit(rule: str, pattern: re.Pattern) -> None:
        for m in pattern.finditer(prose):
            problems.append(f"{rule}: {m.group(0)!r}")

    hit("балльная лексика", SCORE_RE)
    for pattern, name in _level_patterns():
        for m in pattern.finditer(prose):
            problems.append(f"имя уровня ({name}): {m.group(0)!r}")
    hit("внутренний идентификатор snake_case", SNAKE_RE)
    hit("id рекомендации", REC_RE)
    hit("id сессии", HEX_RE)
    if "—" in prose:
        problems.append("длинное тире вне цитаты: «—»")
    low = prose.lower()
    for phrase in COMPARISONS:
        if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", low):
            problems.append(f"сравнение с другими людьми: {phrase!r}")
    for form in WRONG_ADDRESS.get(address, ()):
        if re.search(rf"(?i)(?<!\w){form}(?!\w)", prose):
            problems.append(f"обращение {form!r} при address: {address}")
    folded = _fold(prose)
    for marker in OLD_FORM_MARKERS:
        if _fold(marker) in folded:
            problems.append(f"маркер старой формы текста: {marker!r} (приёмы и рекомендация живут в пунктах действий)")
    if ">" in prose:
        problems.append("символ «>» вне цитаты: цитатные блоки и стрелки в тексте не ставятся")
    return problems
