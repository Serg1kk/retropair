"""Предложения правок карточки человека `people/<id>.suggestions.json`: генерация, apply, reject.

Карточка не переписывается молча: предложения копятся в файле рядом с карточкой со статусом
`pending`, правка — только командой `people apply`. Баллы и оценка от предложений не меняются.

Источники — живые прогоны человека с `review_status: approved`; период — метка прогона (нет метки —
`run_id`), у периода берётся ревизия с самым поздним `(reviewed_at, revision)`, порядок периодов —
порядок первого появления в трекере:

- `script:agent_stack` — элементы `agent_stack` последнего одобренного периода без хвостовых
  номеров версий сборки (`Claude Code 2.1.261` → `Claude Code`; `Opus 5` остаётся), которых нет в
  `profile.tools` (без регистра, `-`/`_` как пробел, по вхождению, версии отрезаны и там) →
  `set profile.tools`;
- `script:stable_axis` — оси с баллом ≥ 3 (с правкой ревьюера; нет данных — не считается) в двух
  последних одобренных периодах → одно предложение на прогон: `append feedback.skip_basics` =
  список имён осей на языке workspace, которых нет ни в карточке, ни в прежних предложениях;
- `judge` — поле `card_suggestions` записи прогона последнего одобренного периода (из выхода судьи,
  форма схемы `judge`: `field` — ключ блока `profile`, `value` — строка, у `years` целое строкой) →
  `set profile.<field>`; форма проверяется, чужое и кривое пропускается с предупреждением.

Значение, которое не записать в карточку так, чтобы оно читалось обратно (перевод строки, неудачная
смесь кавычек), пропускается с предупреждением — предложение не создаётся.

Устаревшие pending закрываются статусом `stale` (`decided_by: "refresh"` или `"apply"`): у `set`
значение поля в карточке уже не то, от которого предложение считалось (правили руками или применено
другое), у `append` все пункты уже в карточке; новое предложение судьи на поле закрывает прежние
pending судьи на то же поле из других прогонов.

Дедуп: `(field, op, proposed)` уже есть в статусе кроме `stale` → не создаётся (отклонённое не
возвращается, устаревшее — может вернуться);
у `append` `proposed` — список строк (старые файлы — одна строка, читаются как список из неё), ключ —
отсортированный кортеж, а элементы, уже бывшие в прежних append-предложениях поля, из нового выпадают.
`apply` правит текст YAML построчно (комментарии, порядок и переводы строк сохраняются; блок,
закомментированный как в шаблоне `people add`, раскомментируется на месте), затем читает карточку
обратно (`people.load_person` и сверка значения); не сошлось — откат файла, `SrError(1)`; любое
другое исключение после записи — тоже откат.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

from lib import i18n, level, marks, people, tracker, yamlmini
from lib.config import load_config
from lib.lock import file_lock
from lib.workspace import SrError, read_json, write_json_atomic, write_text_atomic, ws_paths

FILE_VERSION = 1
SUFFIX = ".suggestions.json"
STATUSES = ("pending", "applied", "rejected", "stale")
PROFILE_FIELDS = ("role", "grade", "years", "stack", "tools", "constraints")
LIST_FIELDS = ("skip_basics", "boundaries", "avoid")
ALLOWED = tuple(f"profile.{k}" for k in PROFILE_FIELDS) + tuple(f"feedback.{k}" for k in LIST_FIELDS)
JUDGE_KEYS = {"field", "value", "reason", "evidence"}
EVIDENCE_KEYS = {"session_id", "turn_index", "quote"}
JUDGE_MAX = 3
STABLE_SCORE = 3
STABLE_PERIODS = 2
I18N = ("cli", "setup")
_KEY_RE = r"^( *){key}:(?=\s|$)(.*)$"
# хвост «версия сборки»: `2.1.261`, `v1.2.3`, `0.86.1-dev`, `@0.46.0` (одиночное число — не версия)
_VERSION_TAIL = re.compile(r"(?:\s+|\s*@\s*)v?\d+(?:\.\d+)+\S*$", re.I)


# --- файл предложений -----------------------------------------------------------------------


def suggestions_path(root: Path, pid: str) -> Path:
    if pid == people.DEFAULT_CARD:
        raise SrError(f"карточка {people.DEFAULT_CARD} не правится предложениями", 2)
    people.check_id(pid)
    return ws_paths(root)["people"] / f"{pid}{SUFFIX}"


def _load(path: Path, pid: str) -> dict:
    if not path.is_file():
        return {"version": FILE_VERSION, "person_id": pid, "items": []}
    data = read_json(path)
    if (not isinstance(data, dict) or data.get("version") != FILE_VERSION
            or data.get("person_id") != pid or not isinstance(data.get("items"), list)
            or not all(isinstance(i, dict) and isinstance(i.get("id"), str) for i in data["items"])):
        raise SrError(
            f"файл предложений не той формы: нужен объект version {FILE_VERSION}, person_id {pid!r}, "
            f"items — список объектов с id ({path})",
            1,
        )
    return data


def load_items(root: Path, pid: str) -> list[dict]:
    """Все предложения человека (без пересчёта); нет файла — пусто, битый — `SrError(1)`."""
    return _load(suggestions_path(root, pid), pid)["items"]


def _people_with_files(root: Path) -> list[str]:
    folder = ws_paths(root)["people"]
    if not folder.is_dir():
        return []
    return sorted(p.name[: -len(SUFFIX)] for p in folder.glob(f"*{SUFFIX}") if not p.name.startswith("."))


def listing(root: Path, pid: str | None = None, *, include_all: bool = False) -> list[dict]:
    """Предложения (pending, с `include_all` — все) одного человека или всех, с полем `person_id`."""
    ids = [pid] if pid is not None else _people_with_files(root)
    out = []
    for one in ids:
        for item in load_items(root, one):
            if include_all or item.get("status") == "pending":
                out.append(dict(item, person_id=one))
    return out


def pending_count(root: Path) -> int:
    """Число pending-предложений по всем файлам workspace, без пересчёта."""
    return len(listing(root))


def doctor_lines(root: Path | None) -> list[tuple[str, object]]:
    if root is None:
        return []
    return [("card_suggestions_pending", pending_count(root))]


# --- генерация ------------------------------------------------------------------------------


def _approved_periods(tr: dict, pid: str) -> list[dict]:
    """Одобренные живые прогоны человека: по прогону на период, периоды в порядке трекера."""
    chosen: dict[str, dict] = {}
    order: list[str] = []
    for run in tracker.live_runs(tr, pid):
        if run.get("review_status") != tracker.APPROVED_STATUS:
            continue
        key = (run.get("period") or {}).get("label") or run.get("run_id")
        rank = (run.get("reviewed_at") or "", run.get("revision") or 0)
        prev = chosen.get(key)
        if prev is None:
            order.append(key)
        if prev is None or rank >= (prev.get("reviewed_at") or "", prev.get("revision") or 0):
            chosen[key] = run
    return [chosen[k] for k in order]


def _norm(text: str) -> str:
    return re.sub(r"[-_\s]+", " ", str(text)).strip().lower()


def _strip_version(text: str) -> str:
    """Элемент стека без хвостового номера версии сборки."""
    return _VERSION_TAIL.sub("", str(text).strip()).strip()


def _as_list(proposed) -> list:
    """`proposed` append-предложения списком: старые файлы хранят одну строку."""
    return list(proposed) if isinstance(proposed, list) else [proposed]


def _evidence(run: dict, session_id=None, turn_index=None, quote=None) -> list[dict]:
    return [{"run_id": run.get("run_id"), "session_id": session_id, "turn_index": turn_index,
             "quote": quote}]


def _current(card: dict, field: str):
    block, key = field.split(".", 1)
    if block == "feedback":
        return list(card["feedback"].get(key) or [])
    return (card.get("profile") or {}).get(key)


def _from_stack(run: dict, card: dict, lang: str) -> list[dict]:
    tools = (card.get("profile") or {}).get("tools")
    tools = "" if tools is None else str(tools).strip()
    have = ", ".join(_norm(_strip_version(t)) for t in tools.split(","))
    new: list[str] = []
    for item in run.get("agent_stack") or []:
        if not isinstance(item, str) or not item.strip():
            continue
        item = _strip_version(item)
        if not item or _norm(item) in have or any(_norm(item) == _norm(n) for n in new):
            continue
        new.append(item)
    if not new:
        return []
    proposed = ", ".join(([tools] if tools else []) + new)
    return [{"field": "profile.tools", "op": "set", "proposed": proposed,
             "reason": i18n.t(I18N, "card_reason_stack", lang).format(items=", ".join(new)),
             "evidence": _evidence(run), "source": "script:agent_stack", "run_id": run.get("run_id")}]


def _from_axes(periods: list[dict], card: dict, lang: str) -> list[dict]:
    if len(periods) < STABLE_PERIODS:
        return []
    last = periods[-STABLE_PERIODS:]
    scores = [level.effective_scores(run) for run in last]
    have = {_norm(s) for s in card["feedback"].get("skip_basics") or []}
    names = []
    for axis in i18n.keys("axes"):
        if not all(isinstance(s.get(axis), int) and s[axis] >= STABLE_SCORE for s in scores):
            continue
        name = i18n.t("axes", axis, lang)
        if _norm(name) not in have:
            names.append(name)
    if not names:
        return []
    return [{"field": "feedback.skip_basics", "op": "append", "proposed": names,
             "reason": i18n.t(I18N, "card_reason_axis", lang).format(axes=", ".join(names)),
             "evidence": [e for run in last for e in _evidence(run)],
             "source": "script:stable_axis", "run_id": last[-1].get("run_id")}]


def _judge_problem(item) -> str | None:
    """Что не так с элементом `card_suggestions` судьи; `None` — годится."""
    if not isinstance(item, dict):
        return f"не объект ({type(item).__name__})"
    extra = sorted(set(item) - JUDGE_KEYS)
    if extra:
        return f"лишние поля {', '.join(extra)} (допустимы {', '.join(sorted(JUDGE_KEYS))})"
    key = item.get("field")
    if key not in PROFILE_FIELDS:
        return (f"поле {key!r}: судья предлагает только ключи profile ({', '.join(PROFILE_FIELDS)}); "
                f"прочее правится руками")
    field = f"profile.{key}"
    value = item.get("value")
    if key == "years":
        if not isinstance(value, str) or not value.strip().isdigit():
            return f"поле {field}: value {value!r} — нужно целое ≥ 0 строкой"
    elif not isinstance(value, str) or not value.strip():
        return f"поле {field}: value {value!r} — нужна непустая строка"
    if not isinstance(item.get("reason"), str) or not item["reason"].strip():
        return f"поле {field}: нет reason"
    ev = item.get("evidence")
    if ev is not None:
        if not isinstance(ev, dict) or set(ev) - EVIDENCE_KEYS:
            return f"поле {field}: evidence — объект с полями {', '.join(sorted(EVIDENCE_KEYS))} или null"
        turn = ev.get("turn_index")
        if (not isinstance(ev.get("session_id"), (str, type(None)))
                or not isinstance(ev.get("quote"), (str, type(None)))
                or not (turn is None or (isinstance(turn, int) and not isinstance(turn, bool)))):
            return f"поле {field}: evidence.session_id/quote — строка или null, turn_index — целое или null"
    return None


def _from_judge(run: dict, card: dict, warn: Callable[[str], None]) -> list[dict]:
    if "card_suggestions" not in run or run["card_suggestions"] is None:
        return []
    where = f"{run.get('run_id')}: card_suggestions"
    items = run["card_suggestions"]
    if not isinstance(items, list):
        warn(f"{where} не список ({type(items).__name__}): пропущено")
        return []
    if len(items) > JUDGE_MAX:
        warn(f"{where}: элементов {len(items)}, берутся первые {JUDGE_MAX}")
    out = []
    for i, item in enumerate(items[:JUDGE_MAX]):
        problem = _judge_problem(item)
        if problem is not None:
            warn(f"{where}[{i}]: {problem}: пропущено")
            continue
        field = f"profile.{item['field']}"
        value = item["value"].strip()
        if item["field"] == "years":
            value = int(value)
        ev = item.get("evidence") or {}
        out.append({"field": field, "op": "set",
                    "proposed": value, "reason": item["reason"].strip(),
                    "evidence": _evidence(run, ev.get("session_id"), ev.get("turn_index"), ev.get("quote")),
                    "source": "judge", "run_id": run.get("run_id")})
    return out


def _noop(cand: dict, card: dict) -> bool:
    current = _current(card, cand["field"])
    if cand["op"] == "append":
        have = {_norm(x) for x in current}
        return all(_norm(v) in have for v in _as_list(cand["proposed"]))
    return current == cand["proposed"]


def _candidates(tr: dict, card: dict, lang: str, warn: Callable[[str], None]) -> list[dict]:
    periods = _approved_periods(tr, card["id"])
    if not periods:
        return []
    found = (_from_stack(periods[-1], card, lang) + _from_axes(periods, card, lang)
             + _from_judge(periods[-1], card, warn))
    out = []
    for cand in found:
        if _noop(cand, card):
            continue
        try:
            for value in _as_list(cand["proposed"]) if cand["op"] == "append" else [cand["proposed"]]:
                render_scalar(value)
        except SrError as err:
            warn(f"{cand['run_id']}: {cand['field']}: {err.message}: пропущено")
            continue
        out.append(cand)
    return out


def _is_stale(item: dict, card: dict) -> bool:
    """Pending уже не применить: у `set` поле в карточке не то, от которого оно считалось; у
    `append` все пункты уже в карточке."""
    field = item.get("field")
    if field not in ALLOWED:
        return False
    current = _current(card, field)
    if item.get("op") == "append":
        have = {_norm(x) for x in current}
        return all(_norm(v) in have for v in _as_list(item.get("proposed")))
    return current != item.get("current")


def _close_stale(items: list[dict], card: dict, stamp: str, by: str) -> int:
    """Пометить устаревшие pending статусом `stale`; вернуть, сколько закрыто."""
    closed = 0
    for item in items:
        if item.get("status") == "pending" and _is_stale(item, card):
            item.update(status="stale", decided_at=stamp, decided_by=by)
            closed += 1
    return closed


def _stderr_warn(message: str) -> None:
    print(f"sessionretro: предупреждение: {message}", file=sys.stderr)


def _next_id(items: list[dict], pid: str) -> str:
    top = 0
    for item in items:
        m = re.fullmatch(rf"S-{re.escape(pid)}-(\d{{3,}})", str(item.get("id")))
        if m:
            top = max(top, int(m.group(1)))
    return f"S-{pid}-{top + 1:03d}"


def _dedup_key(item: dict):
    proposed = item.get("proposed")
    if item.get("op") == "append":
        return (item.get("field"), "append", repr(tuple(sorted(_norm(v) for v in _as_list(proposed)))))
    return (item.get("field"), item.get("op"), repr(proposed))


def _without_proposed_before(cand: dict, items: list[dict]) -> dict | None:
    """Append-кандидат без элементов, уже бывших в append-предложениях поля (любой статус);
    пусто — `None`. Прочие кандидаты — как есть."""
    if cand["op"] != "append":
        return cand
    seen = {_norm(v) for i in items if i.get("field") == cand["field"] and i.get("op") == "append"
            for v in _as_list(i.get("proposed"))}
    rest = [v for v in _as_list(cand["proposed"]) if _norm(v) not in seen]
    return dict(cand, proposed=rest) if rest else None


def refresh(root: Path, person_id: str | None = None, *, warn: Callable[[str], None] | None = None,
            now: datetime | None = None) -> list[dict]:
    """Пересчитать предложения одного человека или всех; вернуть только новые (записанные)."""
    root = Path(root)
    warn = warn or _stderr_warn
    cards = [people.load_person(root, person_id)] if person_id is not None else people.list_people(root)
    tr = tracker.load(root)
    lang = load_config(root)["lang"]
    stamp = marks.iso_utc(now)
    created: list[dict] = []
    for card in cards:
        pid = card["id"]
        cands = _candidates(tr, card, lang, warn)
        path = suggestions_path(root, pid)
        if not cands and not path.is_file():
            continue
        with file_lock(path):
            data = _load(path, pid)
            closed = _close_stale(data["items"], card, stamp, "refresh")
            seen = {_dedup_key(i) for i in data["items"] if i.get("status") != "stale"}
            added = []
            for cand in cands:
                cand = _without_proposed_before(cand, data["items"])
                if cand is None or _dedup_key(cand) in seen:
                    continue
                seen.add(_dedup_key(cand))
                if cand["source"] == "judge":
                    for old in data["items"]:
                        if (old.get("status") == "pending" and old.get("source") == "judge"
                                and old.get("field") == cand["field"] and old.get("run_id") != cand["run_id"]):
                            old.update(status="stale", decided_at=stamp, decided_by="refresh")
                            closed += 1
                item = {"id": _next_id(data["items"], pid), "field": cand["field"], "op": cand["op"],
                        "current": _current(card, cand["field"]), "proposed": cand["proposed"],
                        "reason": cand["reason"], "evidence": cand["evidence"], "source": cand["source"],
                        "run_id": cand["run_id"], "created_at": stamp, "status": "pending",
                        "decided_at": None, "decided_by": None, "reject_reason": None}
                data["items"].append(item)
                added.append(item)
            if added or closed:
                write_json_atomic(path, data)
                created += added
    return created


# --- правка текста карточки -----------------------------------------------------------------


def render_scalar(value) -> str:
    """Значение для YAML-строки карточки; строка — через `people.quote_name`."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _quote(value)
    raise SrError(f"значение {value!r} ({type(value).__name__}) не записать в карточку", 2)


def _quote(value: str) -> str:
    """Строка в кавычках, которую yamlmini читает обратно как есть, и с комментарием после.

    yamlmini не разворачивает экранирование, поэтому при обеих кавычках подбирается обёртка, с
    которой строка читается обратно; не подошла ни одна, перевод строки, управляющий символ —
    `SrError(2)`."""
    if '"' not in value or "'" not in value:
        return people.quote_name(value)
    people.quote_name(value.replace('"', ""))  # перевод строки и управляющие символы → SrError(2)
    for mark in ('"', "'"):
        text = f"{mark}{value}{mark}"
        try:
            if yamlmini.load(f"k: {text}  # c\n") == {"k": value}:
                return text
        except yamlmini.YamlError:
            pass
    raise SrError(f"значение с обеими кавычками не записать в карточку так, чтобы оно читалось "
                  f"обратно ({value!r}): поправьте карточку руками", 2)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _split_comment(rest: str) -> tuple[str, str]:
    """`rest` после `key:` → (значение с пробелами, комментарий `# …` или '')."""
    quote = None
    prev = " "
    for idx, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == "#" and prev in (" ", "\t"):
            return rest[:idx], rest[idx:]
        prev = ch
    return rest, ""


def _with_comment(head: str, old_head_len: int, comment: str) -> str:
    if not comment:
        return head
    return head + " " * max(1, old_head_len - len(head)) + comment


def _is_comment(line: str) -> bool:
    return line.strip().startswith("#")


def _uncomment_block(lines: list[str], block: str, key: str) -> list[str] | None:
    """Блок, закомментированный как в шаблоне `people add` (`# feedback:` на колонке 0 и строки
    `#   …` под ним): раскомментировать заголовок и строку ключа `key`, если она среди этих строк
    (её комментарий остаётся); прочие строки остаются комментариями, поэтому раскомментировать их
    потом можно без второго заголовка. Нет такого заголовка — `None`."""
    head_re = re.compile(rf"^#\s*{re.escape(block)}:(?=\s|$)(.*)$")
    key_re = re.compile(rf"^#\s*{re.escape(key)}:(?=\s|$)(.*)$")
    for i, line in enumerate(lines):
        m = head_re.match(line.rstrip("\n"))
        if not m:
            continue
        out = list(lines)
        out[i] = f"{block}:{m.group(1)}\n"
        for k in range(i + 1, len(out)):
            if not out[k].startswith("#"):
                break
            km = key_re.match(out[k].rstrip("\n"))
            if km:
                out[k] = f"  {key}:{km.group(1)}\n"
                break
        return out
    return None


def edit_card_text(text: str, field: str, op: str, value, base: list | None = None) -> str:
    """Текст карточки после правки `field` (`set` — значение, `append` — элемент или список).

    `base` — элементы, которые пишутся перед новым, если у ключа в карточке своего списка нет
    (иначе ключ карточки перекрыл бы список из `_default.yaml`). Остальные строки не меняются.
    """
    block, key = field.split(".", 1)
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    items = _as_list(value) if op == "append" else None
    new_items = list(base or []) + items if op == "append" else None

    def key_lines(indent: int) -> list[str]:
        if op == "set":
            return [f"{' ' * indent}{key}: {render_scalar(value)}\n"]
        return [f"{' ' * indent}{key}:\n"] + [f"{' ' * (indent + 2)}- {render_scalar(v)}\n" for v in new_items]

    start = None
    for i, line in enumerate(lines):
        if _indent(line) == 0 and re.match(rf"^{re.escape(block)}:(?=\s|$)", line):
            start = i
            break
    if start is None:
        uncommented = _uncomment_block(lines, block, key)
        if uncommented is not None:
            return edit_card_text("".join(uncommented), field, op, value, base=base)
        return "".join(lines) + f"{block}:\n" + "".join(key_lines(2))
    head_rest = _split_comment(lines[start].rstrip("\n")[len(block) + 1:])[0].strip()
    if head_rest:
        raise SrError(f"блок {block}: записан одной строкой ({head_rest!r}), правка {field} — вручную", 1)

    last = start
    j = start + 1
    while j < len(lines):
        line = lines[j]
        if line.strip() == "" or _is_comment(line):
            j += 1
            continue
        if _indent(line) == 0:
            break
        last = j
        j += 1
    child = next((_indent(lines[k]) for k in range(start + 1, last + 1)
                  if lines[k].strip() and not _is_comment(lines[k])), 2)

    key_re = re.compile(_KEY_RE.format(key=re.escape(key)))
    at = None
    for k in range(start + 1, last + 1):
        m = key_re.match(lines[k].rstrip("\n"))
        if m and len(m.group(1)) == child:
            at = k
            break
    if at is None:
        return "".join(lines[: last + 1] + key_lines(child) + lines[last + 1:])

    m = key_re.match(lines[at].rstrip("\n"))
    prefix = f"{m.group(1)}{key}:"
    raw_value, comment = _split_comment(m.group(2))
    stripped = raw_value.strip()
    comment_col = len(prefix) + len(raw_value)
    if op == "set":
        lines[at] = _with_comment(f"{prefix} {render_scalar(value)}", comment_col, comment) + "\n"
        return "".join(lines)

    if stripped:
        raise SrError(f"{field}: в карточке не список ({stripped!r}), правка — вручную", 1)

    # блочный список: элементы — строки `- …` на отступе ключа или глубже
    item_indent = None
    last_item = at
    k = at + 1
    while k < len(lines):
        line = lines[k]
        if line.strip() == "" or _is_comment(line):
            k += 1
            continue
        ind = _indent(line)
        content = line.strip()
        if item_indent is None:
            if content.startswith("- ") or content == "-":
                if ind < child:
                    break
                item_indent = ind
                last_item = k
            else:
                break
        elif ind == item_indent and (content.startswith("- ") or content == "-"):
            last_item = k
        elif ind > item_indent:
            last_item = k
        else:
            break
        k += 1
    if item_indent is None:
        add = [f"{' ' * (child + 2)}- {render_scalar(v)}\n" for v in new_items]
        return "".join(lines[: at + 1] + add + lines[at + 1:])
    add = [f"{' ' * item_indent}- {render_scalar(v)}\n" for v in items]
    return "".join(lines[: last_item + 1] + add + lines[last_item + 1:])


# --- apply / reject -------------------------------------------------------------------------


def _find(data: dict, sid: str, path: Path) -> dict:
    for item in data["items"]:
        if item.get("id") == sid:
            return item
    raise SrError(f"предложение {sid} не найдено ({path})", 1)


def _pending(item: dict, sid: str) -> None:
    if item.get("status") != "pending":
        raise SrError(f"предложение {sid} уже {item.get('status')}: повторно не применяется", 1)


def apply(root: Path, pid: str, sid: str, *, now: datetime | None = None) -> dict:
    """Применить pending-предложение к карточке; вернуть обновлённое предложение.

    Нет предложения, не pending, карточка изменилась после предложения, правка не прошла схему
    или обратное чтение не совпало — `SrError(1)`, карточка байт в байт прежняя.
    """
    root = Path(root)
    spath = suggestions_path(root, pid)
    cpath = people.card_path(root, pid)
    if not cpath.is_file():
        raise SrError(f"человек не найден: нет карточки (id: {pid}, файл: {cpath})", 2)
    with file_lock(spath):
        data = _load(spath, pid)
        item = _find(data, sid, spath)
        _pending(item, sid)
        field, op, proposed = item.get("field"), item.get("op"), item.get("proposed")
        if field not in ALLOWED or op not in ("set", "append") or (op == "append") != field.startswith("feedback."):
            raise SrError(f"предложение {sid}: поле {field!r} с операцией {op!r} не применяется", 1)
        card = people.load_person(root, pid)
        current = _current(card, field)
        close = (f"предложение устарело — закройте его: people suggestions {pid} --refresh "
                 f"(пометит stale) или people reject {pid} {sid}")
        if op == "set" and current != item.get("current"):
            raise SrError(
                f"предложение {sid}: {field} в карточке уже {current!r}, а предложение считалось от "
                f"{item.get('current')!r}; {close}", 1)
        if op == "append":
            have = {_norm(x) for x in current}
            values = [v for v in _as_list(proposed) if _norm(v) not in have]
            if not values:
                raise SrError(f"предложение {sid}: {proposed!r} уже есть в {field}; {close}", 1)

        original = cpath.read_bytes().decode("utf-8")  # как есть: откат байт в байт, `\r\n` на месте
        eol = "\r\n" if "\r\n" in original else "\n"
        text = original.replace("\r\n", "\n")
        block, key = field.split(".", 1)
        try:
            own = ((yamlmini.load(text) or {}).get(block) or {}).get(key)
        except yamlmini.YamlError as err:
            raise SrError(f"карточка не разбирается: {err} ({cpath})", 1) from err
        base = None if isinstance(own, list) else current
        value = proposed if op == "set" else values
        new_text = edit_card_text(text, field, op, value, base=base).replace("\n", eol)
        expected = proposed if op == "set" else list(current) + values
        target = cpath.resolve()  # карточка-симлинк остаётся симлинком
        stamp = marks.iso_utc(now)
        write_text_atomic(target, new_text)
        try:
            card = people.load_person(root, pid)
            got = _current(card, field)
            if got != expected:
                raise SrError(f"после записи {field} читается как {got!r}, ожидалось {expected!r}", 1)
            item.update(status="applied", decided_at=stamp, decided_by="cli")
            _close_stale(data["items"], card, stamp, "apply")
            write_json_atomic(spath, data)
        except BaseException as err:
            write_text_atomic(target, original)
            if isinstance(err, SrError):
                raise SrError(f"предложение {sid} ({field}) не применено, карточка возвращена: "
                              f"{err.message}", 1) from err
            raise
        return item


def reject(root: Path, pid: str, sid: str, reason: str | None = None, *,
           now: datetime | None = None) -> dict:
    """Отклонить pending-предложение (карточка не трогается); не pending — `SrError(1)`."""
    spath = suggestions_path(Path(root), pid)
    people.load_person(Path(root), pid)
    with file_lock(spath):
        data = _load(spath, pid)
        item = _find(data, sid, spath)
        _pending(item, sid)
        item.update(status="rejected", decided_at=marks.iso_utc(now), decided_by="cli",
                    reject_reason=reason)
        write_json_atomic(spath, data)
        return item
