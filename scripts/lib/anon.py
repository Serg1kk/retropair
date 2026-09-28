"""Обезличивание данных отчёта (`--anon`) и гейт чужих имён для личного файла.

- `scrub_contacts(text)` — e-mail, ссылки на github, `@хэндлы` и домашние пути → плейсхолдеры.
- `Anonymizer(people, extra_names, lang)` — id → `p01…` (по сортировке id; если токены ростера
  сами вида `p01`, буква кода другая — `code_prefix`, см. `CODE_PREFIXES`), имя → вымышленное
  (детерминированно по sha256 от id, без пересечений с ростером); `.text(s)` и `.deep(v)`
  чистят строки: сначала id (длинные первыми), потом основы слов имён, потом контакты.
  Слово имени, общее у нескольких людей, и общая часть id → нейтральный плейсхолдер.
  `.payload(p)` — копия через `deep`, но `people[].id/name` явно по id → `id_map/name_map`.
- `personal_gate_patterns(target, others, extra_names)` + `personal_gate(text, patterns)` —
  поиск токенов других людей (id, части id, основы слов имени, `sanitize.names`) в тексте
  личного файла; токены адресата исключаются, тёзки ловятся только целым словом.
- `roster_gate_patterns(people, extra_names)` — все токены ростера без исключений
  (проверка обезличенных group/reviewer).
- `gate_text(payload, title, skip_workspace_title)` — все строковые значения payload (без
  ключей dict и без значений-перечислений, HTML-сущности раскрыты) и заголовок, склеенные
  через `\\n`; кириллица остаётся как есть; значение заголовка workspace вырезается только при
  `skip_workspace_title` (личный гейт без `--anon`).
- `title_name_owners(title, people)` — id людей, чьи токены есть в заголовке workspace
  (предупреждение `report`).
- В `Anonymizer` и гейте ростера `_` — граница слова и для имён (`alice__repo`, `max_tokens`).

Слова имён склоняются, поэтому имя ловится по основе с окончанием из короткого списка
(«Анна» → «Анной», «Ольховская» → «Ольховской»), а id и его части — только целиком между
не-буквенно-цифровыми символами (id `lev` не задевает `level`).
"""

from __future__ import annotations

import copy
import hashlib
import html
import re
from typing import Iterable

LANGS = ("ru", "en")
# Значения под этими ключами — коды из словарей плагина, а не текст: их не чистят и не
# проверяют гейтом (иначе id или имя, похожее на код, ломало бы схему).
ENUM_KEYS = frozenset({"axis", "status", "mode", "lang", "ie", "transitions", "verdict", "reason"})
MIN_TOKEN = 3
# Префиксы кодов людей по порядку: `p` (p01…), а если токены ростера сами вида `p01` — первый
# свободный из остальных (см. `_code_prefix`).
_CODE_LETTERS = ("p", "x", "q", "z", "k")
CODE_PREFIXES = _CODE_LETTERS + tuple(a + b for a in _CODE_LETTERS for b in _CODE_LETTERS)
EXTRA_PLACEHOLDER = {"ru": "[имя]", "en": "[name]"}

# Вымышленные имена. Не пересекаются с именами синтетической фикстуры тестов (проверяется
# тестом) и подобраны без коротких основ, похожих на обычные слова.
FIRST_RU_M = ["Арсений", "Богдан", "Всеволод", "Демид", "Елисей", "Захар", "Кирилл", "Макар",
              "Матвей", "Мирон", "Назар", "Платон", "Родион", "Савелий", "Тимофей", "Ярослав",
              "Святослав", "Прохор", "Вадим", "Эльдар", "Филипп", "Данила", "Остап", "Лука"]
FIRST_RU_F = ["Алина", "Валерия", "Дарья", "Елизавета", "Злата", "Марина", "Милана", "Полина",
              "Регина", "Софья", "Таисия", "Агата", "Есения", "Лидия", "Оксана", "Василиса",
              "Ангелина", "Виолетта", "Варвара", "Ксения", "Серафима", "Эвелина", "Арина",
              "Стефания"]
LAST_RU = ["Воронов", "Гладков", "Ежов", "Зимин", "Крылов", "Лебедев", "Носов", "Панов",
           "Рябов", "Сомов", "Титов", "Ушаков", "Фомин", "Царёв", "Чижов", "Шилов", "Щукин",
           "Юдин", "Яшин", "Гусев", "Дроздов", "Кудрин", "Селезнёв", "Громов"]
FIRST_EN = ["Ansel", "Brisa", "Corwin", "Delphine", "Everett", "Fiona", "Gideon", "Harriet",
            "Imogen", "Jasper", "Keziah", "Leopold", "Marisol", "Nolan", "Ophelia", "Percival",
            "Quentin", "Rosalind", "Silas", "Thaddeus", "Ursula", "Wendell", "Xavier", "Yvette"]
LAST_EN = ["Ashdown", "Blackwood", "Calloway", "Dunmore", "Everly", "Fairbanks", "Gallagher",
           "Hollister", "Ingram", "Jessop", "Kingsley", "Lockhart", "Merriweather", "Northcott",
           "Oakes", "Pemberton", "Quimby", "Radcliffe", "Sterling", "Thornbury", "Underhill",
           "Vance", "Whitlock", "Yardley"]

_CYR_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_WORD_SPLIT_RE = re.compile(r"[\s\-_]+")
# Окончания падежей и родов для слов имени (кириллица): основа + одно из них, целым словом.
_RU_ENDINGS = sorted({
    "а", "я", "о", "е", "ё", "и", "ы", "у", "ю", "й", "ь",
    "ой", "ей", "ою", "ею", "ом", "ем", "ём", "ам", "ям", "ах", "ях", "ым", "им",
    "ая", "яя", "ую", "юю", "ое", "ее", "ые", "ие", "ых", "их", "ий", "ый",
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
}, key=len, reverse=True)
_RU_ENDINGS_RE = "(?:" + "|".join(_RU_ENDINGS) + ")?"
_RU_ADJ_TAILS = ("ая", "яя", "ий", "ый", "ой")
_RU_VOWEL_TAIL = "аяоеёиыуюйь"
_FLEETING_RE = re.compile(r"^(.{3,})[еоё]([лвкнр])$", re.IGNORECASE)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_GITHUB_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?github\.com/[\w.-]+(?:/[^\s)\]>»\"',;]*)?")
_HANDLE_RE = re.compile(r"(?<![\w/.])@[\w-]+")
_HOME_PREFIX_RE = re.compile(r"/(?:Users|home)/[^/\s)\]>»\"',;:]+(/?)")


def scrub_contacts(text: str) -> str:
    """E-mail → `[email]`, ссылка на github → `[ссылка]`, `@хэндл` → `[хэндл]`,
    домашний префикс пути (каталог Users или home плюс имя) → `~/`, а без хвоста
    пути (конец строки, пробел, скобка) → `~`."""
    text = _EMAIL_RE.sub("[email]", text)
    text = _GITHUB_URL_RE.sub("[ссылка]", text)
    text = _HANDLE_RE.sub("[хэндл]", text)
    return _HOME_PREFIX_RE.sub(lambda m: "~/" if m.group(1) else "~", text)


def _hash_int(text: str, offset: int = 0) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[offset:offset + 8], 16)


def _yo(escaped: str) -> str:
    """«е» и «ё» в основе взаимозаменяемы (Семён / Семен)."""
    return re.sub("[её]", "[её]", escaped)


# Границы слова: обычная (`_` — часть слова) и с `_` как разделителем (`alice__repo`, `x_alice_y`).
_WORD_BOUNDS = (r"(?<!\w)", r"(?!\w)")
_UNDERSCORE_BOUNDS = (r"(?<![^\W_])", r"(?![^\W_])")


def _name_pattern(token: str, underscore: bool = False) -> re.Pattern:
    """Слово имени в любом падеже: основа + окончание из списка, целым словом; при `underscore`
    `_` тоже граница слова."""
    if _CYR_RE.search(token):
        low = token.lower()
        if len(token) - 2 >= MIN_TOKEN and low.endswith(_RU_ADJ_TAILS):
            stems = [token[:-2]]
            if low.endswith("ий"):  # Леонтий → Леонтия, Леонтием: основа до «й»
                stems.append(token[:-1])
        elif low[-1] in _RU_VOWEL_TAIL and len(token) >= MIN_TOKEN:
            stems = [token[:-1]]
        else:
            stems = [token]
        m = _FLEETING_RE.match(token)
        if m:  # беглая гласная: Павел → Павла
            stems.append(m.group(1) + m.group(2))
        body = "(?:" + "|".join(_yo(re.escape(s)) for s in stems) + ")" + _RU_ENDINGS_RE
    elif len(token) > 4:
        body = re.escape(token[:-1]) + r"[a-z]{0,3}"
    else:
        body = re.escape(token) + r"(?:'s|s)?"
    left, right = _UNDERSCORE_BOUNDS if underscore else _WORD_BOUNDS
    return re.compile(left + body + right, re.IGNORECASE)


def _id_pattern(token: str) -> re.Pattern:
    """id и его части не склоняются, но живут в путях и run_id: `x_anna_P1`, `REC-anna-001`."""
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def _whole_word(token: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + _yo(re.escape(token)) + r"(?!\w)", re.IGNORECASE)


def _name_words(name: str) -> list[str]:
    return [w for w in _WORD_SPLIT_RE.split(name or "") if len(w) >= MIN_TOKEN]


def _id_tokens(pid: str) -> list[str]:
    out = [pid] if len(pid) >= MIN_TOKEN else []
    for part in pid.split("-"):
        if len(part) >= MIN_TOKEN and part not in out:
            out.append(part)
    return out


def _person_tokens(person: dict) -> list[tuple[str, str]]:
    """[(токен, 'id'|'name')] человека: id, части id, слова имени (все ≥ 3 символов)."""
    pid = person.get("id")
    if not isinstance(pid, str) or not pid:
        raise ValueError(f"у карточки человека нет id: {person!r}")
    toks = [(t, "id") for t in _id_tokens(pid)]
    seen = {t.casefold() for t, _ in toks}
    for w in _name_words(person.get("name") or ""):
        if w.casefold() not in seen:
            seen.add(w.casefold())
            toks.append((w, "name"))
    return toks


def _pattern(token: str, kind: str, underscore: bool = False) -> re.Pattern:
    return _id_pattern(token) if kind == "id" else _name_pattern(token, underscore)


def _code_prefix(people: list[dict], extra_words: list[str]) -> str:
    """Первый префикс кодов из `CODE_PREFIXES`, с которого (плюс цифра) не начинается ни один
    токен ростера: id целиком (любой длины), части id, слова имён, `sanitize.names`. Тогда код
    одного человека не равен id или части id другого, замены не каскадируют, а гейт ростера не
    принимает код за настоящий id. Все префиксы заняты → ValueError."""
    tokens = list(extra_words)
    for p in people:
        tokens += [t for t, _kind in _person_tokens(p)] + [p["id"]]
    for prefix in CODE_PREFIXES:
        taken = re.compile(re.escape(prefix) + r"\d", re.IGNORECASE)
        if not any(taken.match(t) for t in tokens):
            return prefix
    raise ValueError(
        f"не удалось выбрать префикс кодов обезличивания: все {len(CODE_PREFIXES)} вариантов "
        f"({', '.join(CODE_PREFIXES)}) заняты токенами ростера вида <префикс><цифры>"
    )


class Anonymizer:
    """Детерминированные коды и вымышленные имена; вычистка ростера из текстов."""

    def __init__(self, people: list[dict], extra_names: Iterable[str] = (), lang: str = "ru"):
        if lang not in LANGS:
            raise ValueError(f"неизвестный язык обезличивания {lang!r}, ожидается один из {LANGS}")
        self.lang = lang
        ids = [p.get("id") for p in people]
        if len(set(ids)) != len(ids):
            raise ValueError(f"повторяющиеся id людей: {sorted(i for i in set(ids) if ids.count(i) > 1)}")
        extra_words = [w for name in extra_names for w in _name_words(name)]
        roster: list[tuple[str, re.Pattern]] = []
        for p in people:
            roster += [(t, _pattern(t, kind)) for t, kind in _person_tokens(p)]
        roster += [(w, _name_pattern(w)) for w in extra_words]

        self.code_prefix = _code_prefix(people, extra_words)
        self.id_map: dict[str, str] = {
            pid: f"{self.code_prefix}{i:02d}" for i, pid in enumerate(sorted(ids), start=1)
        }
        self.name_map: dict[str, str] = {}
        for pid in sorted(ids):
            self.name_map[pid] = self._pick_name(pid, roster)

        placeholder = EXTRA_PLACEHOLDER[lang]
        # Кому принадлежит токен: id и части id — по casefold, слова имени — по совпадению
        # паттернов (Каменев / Каменева). Общий у нескольких людей токен в свободном тексте
        # нельзя приписать одному из них, он заменяется нейтральным плейсхолдером.
        id_owners: dict[str, dict] = {}
        name_owners: list[tuple[str, re.Pattern, set[str], str]] = []
        for p in people:
            pid = p["id"]
            first, last = self.name_map[pid].split(" ", 1)
            words = [w.casefold() for w in _name_words(p.get("name") or "")]
            for tok, kind in _person_tokens(p):
                if kind == "id":
                    entry = id_owners.setdefault(tok.casefold(), {"tok": tok, "pids": set(),
                                                                  "full": None})
                    entry["pids"].add(pid)
                    if tok == pid:
                        entry["full"] = pid
                    continue
                # `_` — граница, как в гейте ростера: `alice__repo`, `max_tokens` заменяются
                # (лишняя замена в обезличенной копии безопаснее отказа гейта).
                pat = _name_pattern(tok, underscore=True)
                repl = first if words.index(tok.casefold()) == 0 else last
                owners = {pid}
                for other_tok, other_pat, other_owners, _r in name_owners:
                    if pat.fullmatch(other_tok) or other_pat.fullmatch(tok):
                        owners |= other_owners
                        other_owners.add(pid)
                name_owners.append((tok, pat, owners, repl))

        id_subs: list[tuple[str, re.Pattern, str]] = []
        for entry in id_owners.values():
            if entry["full"] is not None:
                repl = self.id_map[entry["full"]]
            elif len(entry["pids"]) == 1:
                repl = self.id_map[next(iter(entry["pids"]))]
            else:
                repl = placeholder
            id_subs.append((entry["tok"], _id_pattern(entry["tok"]), repl))
        name_subs = [(tok, pat, repl if len(owners) == 1 else placeholder)
                     for tok, pat, owners, repl in name_owners]
        name_subs += [(w, _name_pattern(w, underscore=True), placeholder) for w in extra_words]
        # Длинные первыми: `anna-k` раньше `anna`, «Александра» раньше «Александр».
        self._id_subs = sorted(id_subs, key=lambda s: -len(s[0]))
        self._name_subs = sorted(name_subs, key=lambda s: -len(s[0]))

    def _pick_name(self, pid: str, roster: list[tuple[str, re.Pattern]]) -> str:
        if self.lang == "ru":
            female = _hash_int(pid, 8) % 2 == 1
            firsts, lasts = (FIRST_RU_F if female else FIRST_RU_M), LAST_RU
            suffix = "а" if female else ""
        else:
            firsts, lasts, suffix = FIRST_EN, LAST_EN, ""
        h1, h2 = _hash_int(pid), _hash_int(pid, 16)
        used = set(self.name_map.values())
        used_words = {w for n in used for w in n.split()}
        total = len(firsts) * len(lasts)
        for strict in (True, False):
            for bump in range(total):
                first = firsts[(h1 + bump) % len(firsts)]
                last = lasts[(h2 + bump // len(firsts)) % len(lasts)] + suffix
                full = f"{first} {last}"
                if full in used or self._clashes(first, roster) or self._clashes(last, roster):
                    continue
                if strict and (first in used_words or last in used_words):
                    continue
                return full
        raise ValueError(
            f"не удалось подобрать вымышленное имя для {pid!r}: все {total} вариантов "
            f"({self.lang}) заняты или совпадают с ростером"
        )

    @staticmethod
    def _clashes(word: str, roster: list[tuple[str, re.Pattern]]) -> bool:
        own = _name_pattern(word)
        return any(pat.search(word) or own.search(tok) for tok, pat in roster)

    def text(self, value):
        """Строка без id, слов имён (в любом падеже) и контактов; не-строки — как есть."""
        if not isinstance(value, str) or not value:
            return value
        if value in self.id_map:
            return self.id_map[value]
        out = value
        for _tok, pat, repl in self._id_subs:
            out = pat.sub(repl, out)
        for _tok, pat, repl in self._name_subs:
            out = pat.sub(repl, out)
        return scrub_contacts(out)

    def deep(self, value):
        """Рекурсивно по значениям; ключи dict и значения-перечисления не трогаются."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        if isinstance(value, dict):
            return {k: (v if k in ENUM_KEYS else self.deep(v)) for k, v in value.items()}
        return value

    def payload(self, payload: dict) -> dict:
        """Копия payload через `deep`, а `people[].id` и `people[].name` — явно по id
        (`id_map`, `name_map`): имя человека не собирается пословно из чужих замен.

        id человека не из ростера → ValueError с этим id.
        """
        people = payload.get("people") or []
        real_ids = [p.get("id") for p in people]
        unknown = [pid for pid in real_ids if pid not in self.id_map]
        if unknown:
            raise ValueError(f"в payload люди не из ростера обезличивания: {unknown}")
        out = self.deep(copy.deepcopy(payload))
        for person, pid in zip(out.get("people") or [], real_ids):
            person["id"] = self.id_map[pid]
            person["name"] = self.name_map[pid]
        return out


def personal_gate_patterns(target: dict, others: list[dict],
                           extra_names: Iterable[str] = ()) -> list[tuple[str, re.Pattern]]:
    """[(подпись «чей: токен», паттерн)] токенов других людей и `sanitize.names`.

    Токен, совпадающий с токеном адресата (две Анны), пропускается; токен, чей паттерн
    накрывает токен адресата (Александра / Александр), ищется только целым словом.
    """
    own = [t for t, _ in _person_tokens(target)]
    own_low = {t.casefold() for t in own}
    candidates = [(o["id"], tok, kind) for o in others for tok, kind in _person_tokens(o)]
    candidates += [("sanitize.names", w, "name") for n in extra_names for w in _name_words(n)]
    pats: list[tuple[str, re.Pattern]] = []
    seen: set[str] = set()
    for who, tok, kind in candidates:
        label = f"{who}: {tok}"
        if tok.casefold() in own_low or label in seen:
            continue
        seen.add(label)
        pat = _pattern(tok, kind)
        if any(pat.fullmatch(t) for t in own):
            pat = _whole_word(tok)
        pats.append((label, pat))
    return pats


def roster_gate_patterns(people: list[dict],
                         extra_names: Iterable[str] = ()) -> list[tuple[str, re.Pattern]]:
    """[(подпись, паттерн)] всех токенов ростера и `sanitize.names` без исключений —
    проверка обезличенного файла, где настоящих токенов не должно быть ни одного.
    `_` здесь — граница слова и для имён: хэндл в `alice__repo` тоже настоящий токен."""
    pats: list[tuple[str, re.Pattern]] = []
    seen: set[str] = set()
    for p in people:
        for tok, kind in _person_tokens(p):
            label = f"{p['id']}: {tok}"
            if label not in seen:
                seen.add(label)
                pats.append((label, _pattern(tok, kind, underscore=True)))
    for name in extra_names:
        for w in _name_words(name):
            label = f"sanitize.names: {w}"
            if label not in seen:
                seen.add(label)
                pats.append((label, _name_pattern(w, underscore=True)))
    return pats


def personal_gate(text: str, patterns: list[tuple[str, re.Pattern]]) -> list[str]:
    """Нарушения «чей токен → что нашлось»; пустой список = гейт зелёный."""
    hits = []
    for label, pat in patterns:
        m = pat.search(text)
        if m:
            hits.append(f"{label} → «{m.group(0)}»")
    return hits


def _strings(value, out: list[str]) -> None:
    if isinstance(value, str):
        # `*_html` экранирован (`O'Brien` → `O&#x27;Brien`): гейт смотрит на текст, не на разметку
        out.append(html.unescape(value))
    elif isinstance(value, list):
        for v in value:
            _strings(v, out)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in ENUM_KEYS:
                _strings(v, out)


def gate_text(payload: dict, title: str, skip_workspace_title: bool = False) -> str:
    """Заголовок и все строковые значения payload через `\\n` (без ключей и перечислений).

    По умолчанию заголовок workspace (`meta.workspace_title`) — обычный текст (гейт обезличенного
    файла). `skip_workspace_title` — только личный гейт без `--anon`: значение заголовка не текст
    о человеке, его нет в тексте гейта, и из `title` и `meta.title` оно вырезается (слово «Demo»
    в «Demo team» — не имя «Alex Demo»); то же слово в остальном тексте гейт по-прежнему видит.
    Имена в самом заголовке `report` показывает предупреждением (`title_name_owners`)."""
    meta = payload.get("meta") if isinstance(payload, dict) else None
    ws = meta.get("workspace_title") if isinstance(meta, dict) else None
    if not skip_workspace_title:
        ws = None

    def drop_ws(text):
        return text.replace(ws, " ") if isinstance(ws, str) and ws.strip() and isinstance(text, str) else text

    out = [drop_ws(title)]
    if isinstance(meta, dict) and ws is not None:
        meta = {k: drop_ws(v) if k == "title" else v for k, v in meta.items() if k != "workspace_title"}
        payload = {**payload, "meta": meta}
    _strings(payload, out)
    return "\n".join(out)


def title_name_owners(title: str, people: list[dict]) -> list[str]:
    """Отсортированные id людей, чьи токены (id, части id, слова имени) есть в заголовке
    workspace. Та же граница слова, что у личного гейта."""
    if not isinstance(title, str) or not title.strip():
        return []
    owners = set()
    for p in people:
        if any(_pattern(tok, kind).search(title) for tok, kind in _person_tokens(p)):
            owners.add(p["id"])
    return sorted(owners)
