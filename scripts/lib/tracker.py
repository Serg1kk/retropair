"""Трекер прогонов оценки v2 (`.retropair/runs/process-reviews.json`): загрузка, запись, инварианты.

Прогон — одна оценка одного человека за период: баллы осей под цитаты, гейт, рекомендации,
части текста ревью, пометки логов. Схема файла — `references/schemas/tracker.json`; условия,
которых схемы не выражают (список «Проверки скриптом» в `references/output-contract.md`,
условия 1–3 и 5–9), проверяет `validate_run`. Условие 7 в части «рычаги ↔ `best_practices`»
проверяется только при `append`: `best_practices` в трекер не уезжает.

Порядок записи: сначала все проверки, потом трекер, потом индекс логов. Замки — всегда
«трекер → индекс» (`lock.file_lock(<трекер>)`, затем `marks.index_lock`).

Статус рекомендации не хранится, а считается на лету по `prev_recommendations` живых прогонов:
`followed`/`ignored` закрывают, `partial` оставляет открытой, `not_observable` и
`not_applicable_yet` не меняют ничего (поведения не было видно или логи периода закончились до
выдачи рекомендации — спрашивать про него надо снова). Отозванный прогон (`retracted`) не
участвует ни в статусах, ни в проверках, но его номера рекомендаций заняты навсегда.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from lib import batches, i18n, level, marks, people, registry, schema, text_rules
from lib.config import load_config
from lib.lock import file_lock
from lib.workspace import SrError, read_json, write_json_atomic, ws_paths

TRACKER_VERSION = 2
TRACKER_FILE = "process-reviews.json"
MAX_SCHEMA_ERRORS = 25
MAX_LEVERS_TOTAL = 4
SHORT_MAX_ACTIONS = 2
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PLACE_VERDICTS = {"first_place_above": 1, "second_place_above": 2, "third_place_above": 3}
CLOSING_VERDICTS = ("followed", "ignored")
# Вердикты «по логам»: страж append заменяет их на TOO_EARLY_VERDICT, если логи старше выдачи.
EVALUATED_VERDICTS = ("followed", "partial", "ignored", "not_observable")
TOO_EARLY_VERDICT = "not_applicable_yet"
REVIEWED_STATUS = "reviewed"
APPROVED_STATUS = "approved"
GATED_MODES = ("team", "course")
MAX_LEVERS_PER_ACTION = 2
INSUFFICIENT_LEVEL = "insufficient"
# Ось, где под балл законно числовое доказательство из context_report без хода (схема judge).
NUMERIC_EVIDENCE_AXIS = "context_engineering"
LEADING_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


# --- хранилище ---------------------------------------------------------------


def tracker_path(root: Path) -> Path:
    return ws_paths(root)["runs"] / TRACKER_FILE


def _empty(root: Path) -> dict:
    return {"tracker_version": TRACKER_VERSION, "workspace": load_config(root)["workspace"]["id"],
            "updated_at": None, "runs": []}


def load(root: Path) -> dict:
    """Трекер workspace; нет файла → пустой v2 (`workspace` из конфига); битый или не v2 → `SrError(1)`."""
    path = tracker_path(root)
    if not path.is_file():
        return _empty(root)
    tr = read_json(path)
    if not isinstance(tr, dict) or not isinstance(tr.get("runs"), list):
        raise SrError(f"трекер не той формы: нужен объект со списком runs ({path})", 1)
    if tr.get("tracker_version") != TRACKER_VERSION:
        raise SrError(
            f"трекер версии {tr.get('tracker_version')!r}, а движок читает {TRACKER_VERSION} ({path})", 1
        )
    return tr


def _save(root: Path, tr: dict, now: datetime | None) -> None:
    tr["updated_at"] = marks.iso_utc(now)
    write_json_atomic(tracker_path(root), tr)


def schema_problems(obj, name: str) -> list[str]:
    """Расхождения `obj` со схемой `name`; нет схемы или она битая → `SrError(1)` с путём."""
    try:
        return schema.validate(obj, name)
    except FileNotFoundError as err:
        raise SrError(
            f"схема {name} не найдена: она ставится вместе с промптами оценки ({schema.schema_path(name)})", 1
        ) from err
    except ValueError as err:
        raise SrError(f"схема {name} не читается: {err} ({schema.schema_path(name)})", 1) from err


# --- прогоны -----------------------------------------------------------------


def make_run_id(ws_id: str, person_id: str, label: str | None, now: datetime) -> str:
    """`<workspace>_<person>_<метка|run>_<YYYYMMDDTHHMMSSZ>`; метка вне `LABEL_RE` → `SrError(2)`."""
    if label is not None and not LABEL_RE.fullmatch(label):
        raise SrError(
            f"метка периода {label!r} не подходит: нужен формат {LABEL_RE.pattern} "
            f"(метка входит в имя файла ревью)",
            2,
        )
    at = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    stamp = at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ws_id}_{person_id}_{label or 'run'}_{stamp}"


def run_mode_label(tr: dict, person_id: str, now: datetime) -> str:
    """Метка прогона режима `period.mode: run`: `run-<ГГГГММДД>` по `now` (UTC), занята → `-2`, `-3`, …

    Занятыми считаются метки всех прогонов человека, включая отозванные: метка не переиспользуется,
    и второй прогон за сутки становится своим периодом, а не ревизией первого.
    """
    at = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    base = f"run-{at.astimezone(timezone.utc):%Y%m%d}"
    taken = {(r.get("period") or {}).get("label") for r in tr.get("runs") or []
             if isinstance(r, dict) and r.get("person_id") == person_id}
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def live_run_with_label(tr: dict, person_id: str, label: str) -> dict | None:
    """Живой прогон человека с меткой периода `label` (первый в порядке файла) или `None`."""
    for run in live_runs(tr, person_id):
        if (run.get("period") or {}).get("label") == label:
            return run
    return None


def is_live(run: dict) -> bool:
    return isinstance(run, dict) and not run.get("retracted")


def live_runs(tr: dict, person_id: str | None = None) -> list:
    """Неотозванные прогоны в порядке файла; `person_id` — только этого человека."""
    return [r for r in tr.get("runs") or []
            if is_live(r) and (person_id is None or r.get("person_id") == person_id)]


def find_run(tr: dict, run_id: str) -> dict:
    """Прогон по id (живой или отозванный); нет → `SrError(1)`."""
    for run in tr.get("runs") or []:
        if isinstance(run, dict) and run.get("run_id") == run_id:
            return run
    raise SrError(f"прогон не найден в трекере (run_id: {run_id})", 1)


def effective_axes(run: dict) -> dict[str, int | None]:
    """Ось → балл с учётом правки ревьюера; ось без данных → `None`."""
    return level.effective_scores(run)


def _rec_number(rec_id, person_id: str) -> int | None:
    if not isinstance(rec_id, str):
        return None
    m = re.fullmatch(rf"REC-{re.escape(person_id)}-(\d{{3,}})", rec_id)
    return int(m.group(1)) if m else None


def _next_number(runs: list, person_id: str) -> int:
    top = 0
    for run in runs:
        if not isinstance(run, dict) or run.get("person_id") != person_id:
            continue
        for rec in run.get("recommendations") or []:
            number = _rec_number(rec.get("id") if isinstance(rec, dict) else None, person_id)
            if number is not None:
                top = max(top, number)
    return top + 1


def next_rec_number(tr: dict, person_id: str) -> int:
    """Следующий номер `REC-<person>-NNN`: по всем прогонам человека, включая отозванные.

    Номер отозванной рекомендации не переиспользуется: иначе два разных текста получат один id.
    """
    return _next_number(tr.get("runs") or [], person_id)


def issued_recs(runs: list, person_id: str) -> dict[str, dict]:
    """Выданные рекомендации человека со статусом на лету: `id → {rec, status, run_id}`.

    Считается по живым прогонам в порядке списка: `recommendations` выдают, `prev_recommendations`
    меняют статус (`followed`/`ignored` закрывают, `partial` → `open`, `not_observable` и
    `not_applicable_yet` — без изменений). Вердикт по неизвестному id пропускается (его ловит `validate_run`).
    """
    issued: dict[str, dict] = {}
    for run in runs:
        if not is_live(run) or run.get("person_id") != person_id:
            continue
        for rec in run.get("recommendations") or []:
            if isinstance(rec, dict) and isinstance(rec.get("id"), str):
                issued.setdefault(rec["id"], {"rec": rec, "status": "open", "run_id": run.get("run_id")})
        for verdict in run.get("prev_recommendations") or []:
            if not isinstance(verdict, dict):
                continue
            item = issued.get(verdict.get("id"))
            if item is None:
                continue
            if verdict.get("verdict") in CLOSING_VERDICTS:
                item["status"] = verdict["verdict"]
            elif verdict.get("verdict") == "partial":
                item["status"] = "open"
    return issued


# --- проверки ----------------------------------------------------------------


def _prose_fields(run: dict) -> list[tuple[str, str]]:
    """(имя поля, текст) прозы для человека: черновик и части текста, без полей `quote`.

    Из черновика вырезаются значения полей `quote` пунктов: цитата дословна (вложенные
    кавычки, тире и id в ней законны), так же её исключает рендер.
    """
    draft = run.get("feedback_draft")
    parts = run.get("feedback_parts")
    if isinstance(draft, str) and isinstance(parts, dict):
        for action in parts.get("actions") or []:
            quote = action.get("quote") if isinstance(action, dict) else None
            if isinstance(quote, str) and quote.strip():
                draft = draft.replace(quote, " ")
    fields = [("feedback_draft", draft)]
    if isinstance(parts, dict):
        fields += [("feedback_parts.opening", parts.get("opening")),
                   ("feedback_parts.closing", parts.get("closing"))]
        for i, action in enumerate(parts.get("actions") or []):
            if not isinstance(action, dict):
                continue
            for key in ("place", "cost", "try"):
                fields.append((f"feedback_parts.actions[{i}].{key}", action.get(key)))
            for j, lever in enumerate(action.get("levers") or []):
                fields.append((f"feedback_parts.actions[{i}].levers[{j}]", lever))
        for i, crit in enumerate(parts.get("criteria") or []):
            if isinstance(crit, dict):
                fields.append((f"feedback_parts.criteria[{i}].evidence", crit.get("evidence")))
    return [(name, text) for name, text in fields if isinstance(text, str) and text]


def _has_quote(evidence, axis_id=None) -> bool:
    """Есть цитата с адресом (`session_id` + `turn_index` + непустой `quote`); для
    `context_engineering` годится и числовое доказательство из context_report без хода."""
    for item in evidence or []:
        if not isinstance(item, dict) or not isinstance(item.get("quote"), str) or not item["quote"].strip():
            continue
        turn = item.get("turn_index")
        if (isinstance(item.get("session_id"), str) and item["session_id"]
                and isinstance(turn, int) and not isinstance(turn, bool)):
            return True
        if axis_id == NUMERIC_EVIDENCE_AXIS and turn is None:
            return True
    return False


def _check_axes(run: dict, where: str, axis_ids: list[str]) -> list[str]:
    problems = []
    axes = run.get("axes")
    if not isinstance(axes, list):
        return [f"{where}: axes не список"]
    ids = [a.get("id") if isinstance(a, dict) else None for a in axes]
    missing = [a for a in axis_ids if a not in ids]
    dups = sorted({a for a in ids if a is not None and ids.count(a) > 1})
    unknown = [a for a in ids if a not in axis_ids]
    if len(axes) != len(axis_ids) or missing or dups or unknown:
        problems.append(
            f"{where}: нужны ровно {len(axis_ids)} осей без повторов; осей {len(axes)}, "
            f"нет: {', '.join(missing) or '—'}; дубли: {', '.join(dups) or '—'}; "
            f"лишние: {', '.join(map(str, unknown)) or '—'}"
        )
    elif ids != axis_ids:
        problems.append(
            f"{where}: axes не в порядке axes.md (условие 3): нужно {', '.join(axis_ids)}; "
            f"стоит {', '.join(map(str, ids))}"
        )
    reasons = i18n.keys("insufficient_reasons")
    for axis in axes:
        if not isinstance(axis, dict):
            continue
        aid = axis.get("id")
        score, insuf = axis.get("score"), axis.get("insufficient_evidence")
        if insuf and score is not None:
            problems.append(f"{where}/{aid}: insufficient_evidence и балл {score} одновременно")
        if not insuf and score is None:
            problems.append(f"{where}/{aid}: балла нет и не помечено insufficient_evidence")
        if score is not None and (not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 4):
            problems.append(f"{where}/{aid}: балл {score!r} вне 0–4")
        reason = axis.get("insufficient_evidence_reason")
        if insuf and reason not in reasons:
            problems.append(
                f"{where}/{aid}: insufficient_evidence без причины: insufficient_evidence_reason "
                f"{reason!r}, нужна одна из {', '.join(reasons)} (условие 1)"
            )
        if not insuf and reason is not None:
            problems.append(
                f"{where}/{aid}: балл {score!r} и причина нехватки данных {reason!r} одновременно (условие 1)"
            )
        if not insuf and score is not None and not _has_quote(axis.get("evidence"), aid):
            problems.append(
                f"{where}/{aid}: балл без цитаты (нужна хотя бы одна с session_id, turn_index и quote)"
            )
    return problems


def _check_level_and_gate(run: dict, where: str) -> list[str]:
    problems = []
    lvl = run.get("level_judge")
    levels = i18n.keys("levels")
    if lvl not in levels:
        problems.append(f"{where}: уровень судьи {lvl!r} вне списка ({', '.join(levels)})")
    evidence = run.get("gating_evidence") or []
    if run.get("gating_triggered"):
        count = max(level.gate_triggers(run), 1)
        gate = min(count, 2)
        if lvl in levels and lvl != INSUFFICIENT_LEVEL and lvl not in level.GATE_CEILING[gate]:
            problems.append(
                f"{where}: гейт сработал (разных триггеров: {count}), уровень судьи {lvl} выше "
                f"потолка ({level.GATE_CAP[gate]})"
            )
    elif evidence:
        problems.append(f"{where}: gating_triggered false, а gating_evidence не пуст")
    return problems


def _check_override(run: dict, where: str, axis_ids: list[str]) -> list[str]:
    override = run.get("operator_override")
    if override is None:
        return []
    shape = "нужна форма {axes: {<ось>: 0–4}, why, by, at}"
    if not isinstance(override, dict):
        return [f"{where}: operator_override не объект ({shape})"]
    problems = []
    extra = sorted(set(override) - {"axes", "why", "by", "at"})
    if extra:
        problems.append(f"{where}: operator_override с лишними полями {', '.join(extra)} ({shape})")
    axes = override.get("axes")
    if not isinstance(axes, dict):
        problems.append(f"{where}: operator_override без блока axes ({shape})")
    else:
        for aid, score in axes.items():
            if aid not in axis_ids:
                problems.append(f"{where}: operator_override по неизвестной оси {aid!r}")
            elif not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 4:
                problems.append(f"{where}: operator_override {aid}={score!r} вне 0–4")
    for key in ("why", "by", "at"):
        value = override.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: operator_override без {key}: правка без причины и автора не читается")
    return problems


def _check_recs(run: dict, where: str, prior: list, all_runs: list, axis_ids: list[str]) -> list[str]:
    problems = []
    pid = run.get("person_id") or ""
    expected = _next_number(prior, pid)
    taken = {}
    for other in all_runs:
        if other is run or not isinstance(other, dict) or other.get("person_id") != pid:
            continue
        for rec in other.get("recommendations") or []:
            if isinstance(rec, dict):
                taken.setdefault(rec.get("id"), other.get("run_id"))
    seen: set = set()
    statuses = i18n.keys("rec_statuses")
    for rec in run.get("recommendations") or []:
        if not isinstance(rec, dict):
            problems.append(f"{where}: рекомендация не объект")
            continue
        rid = rec.get("id")
        number = _rec_number(rid, pid)
        if number is None:
            problems.append(f"{where}: id рекомендации {rid!r} не в форме REC-{pid}-NNN")
        elif rid in seen:
            problems.append(f"{where}: id рекомендации {rid} повторён в прогоне")
        elif rid in taken:
            problems.append(f"{where}: id рекомендации {rid} уже выдан в прогоне {taken[rid]}")
        elif number != expected:
            problems.append(
                f"{where}: id рекомендации {rid} не по порядку: следующий номер {expected:03d}"
            )
        if number is not None and number == expected:
            expected += 1
        seen.add(rid)
        if rec.get("axis") not in axis_ids:
            problems.append(f"{where}: рекомендация {rid} по неизвестной оси {rec.get('axis')!r}")
        if "status" in rec and rec["status"] not in statuses:
            problems.append(f"{where}: статус рекомендации {rid} {rec['status']!r} вне списка")
    issued = issued_recs(prior, pid)
    verdicts = i18n.keys("rec_verdicts")
    answered: set = set()
    for verdict in run.get("prev_recommendations") or []:
        if not isinstance(verdict, dict):
            problems.append(f"{where}: вердикт по прошлой рекомендации не объект")
            continue
        vid = verdict.get("id")
        if verdict.get("verdict") not in verdicts:
            problems.append(f"{where}: вердикт {verdict.get('verdict')!r} по {vid} вне списка ({', '.join(verdicts)})")
        if vid in answered:
            problems.append(f"{where}: по рекомендации {vid} два вердикта")
        answered.add(vid)
        item = issued.get(vid)
        if item is None:
            problems.append(f"{where}: вердикт по рекомендации {vid!r}, которой у {pid} не выдавали")
        elif item["status"] != "open":
            problems.append(f"{where}: вердикт по рекомендации {vid}, она уже закрыта ({item['status']})")
    return problems


def _action_axes(run: dict, n_actions: int) -> list:
    """Ось каждого пункта: `actions[i]` — это `recommendations[i]` (связь по индексу)."""
    recs = [r for r in run.get("recommendations") or [] if isinstance(r, dict)]
    return [recs[i].get("axis") if i < len(recs) else None for i in range(n_actions)]


def _check_parts(run: dict, where: str, axis_ids: list[str], length: str | None) -> list[str]:
    """Условия 3, 5, 6 и 7 (без `best_practices`) по `feedback_parts` прогона."""
    parts = run.get("feedback_parts")
    if not isinstance(parts, dict):
        return [f"{where}: нет feedback_parts: тексту ревью не из чего собираться"]
    problems = []
    actions = parts.get("actions")
    actions = actions if isinstance(actions, list) else []
    if not 1 <= len(actions) <= 3:
        problems.append(f"{where}: feedback_parts.actions: пунктов {len(actions)}, нужно 1–3")
    recs = run.get("recommendations") if isinstance(run.get("recommendations"), list) else []
    if len(actions) != len(recs):
        problems.append(
            f"{where}: feedback_parts.actions: пунктов {len(actions)}, а recommendations {len(recs)}: "
            f"i-й пункт это i-я рекомендация (условие 5)"
        )
    levers_total = 0
    for i, action in enumerate(actions):
        levers = action.get("levers") if isinstance(action, dict) else None
        levers = levers if isinstance(levers, list) else []
        if len(levers) > MAX_LEVERS_PER_ACTION:
            problems.append(
                f"{where}: feedback_parts.actions[{i}]: рычагов {len(levers)}, нужно не больше {MAX_LEVERS_PER_ACTION}"
            )
        levers_total += len(levers)
    if levers_total > MAX_LEVERS_TOTAL:
        problems.append(
            f"{where}: feedback_parts: рычагов на текст {levers_total}, нужно не больше {MAX_LEVERS_TOTAL}"
        )
    if length == "short":
        if len(actions) > SHORT_MAX_ACTIONS:
            problems.append(
                f"{where}: карточка просит length: short, а пунктов {len(actions)} (не больше {SHORT_MAX_ACTIONS})"
            )
        if levers_total:
            problems.append(f"{where}: карточка просит length: short, а рычагов {levers_total} (нужно 0)")
    criteria = parts.get("criteria")
    criteria = criteria if isinstance(criteria, list) else []
    order = [c.get("axis") if isinstance(c, dict) else None for c in criteria]
    if order != axis_ids:
        problems.append(
            f"{where}: feedback_parts.criteria: нужны ровно {len(axis_ids)} осей в порядке "
            f"{', '.join(axis_ids)}; стоит {', '.join(map(str, order)) or 'пусто'}"
        )
    allowed = i18n.keys("verdicts")
    action_axes = _action_axes(run, len(actions))
    pointed: dict[int, list[int]] = {}
    for i, crit in enumerate(criteria):
        verdict = crit.get("verdict") if isinstance(crit, dict) else None
        if verdict not in allowed:
            problems.append(f"{where}: feedback_parts.criteria[{i}].verdict {verdict!r} вне списка")
            continue
        place = PLACE_VERDICTS.get(verdict)
        if place is None:
            continue
        pointed.setdefault(place, []).append(i)
        if place > len(actions):
            problems.append(
                f"{where}: feedback_parts.criteria[{i}]: {verdict} ссылается на пункт {place}, "
                f"а пунктов {len(actions)}"
            )
        elif action_axes[place - 1] is not None and crit.get("axis") != action_axes[place - 1]:
            problems.append(
                f"{where}: feedback_parts.criteria[{i}] ({crit.get('axis')}): {verdict} ссылается на пункт "
                f"{place}, а он по оси recommendations[{place - 1}] = {action_axes[place - 1]} (условие 5)"
            )
    names = {n: v for v, n in PLACE_VERDICTS.items()}
    for place in range(1, len(actions) + 1):
        refs = pointed.get(place, [])
        if len(refs) != 1 and place in names:
            shown = ", ".join(f"criteria[{i}]" for i in refs) or "ни одного"
            problems.append(
                f"{where}: feedback_parts.actions[{place - 1}]: на пункт должен ссылаться ровно один "
                f"критерий (вердикт {names[place]}), ссылается {shown} (условие 5)"
            )
    return problems


def check_practices(run: dict, best_practices, where: str | None = None) -> list[str]:
    """Условие 7: число рычагов пунктов совпадает с числом приёмов `best_practices[]` по каждой оси.

    Ось пункта — ось `recommendations` с тем же индексом. `best_practices` нет (`None`) — приёмов ноль.
    """
    where = where or str(run.get("run_id") or "run")
    parts = run.get("feedback_parts") if isinstance(run.get("feedback_parts"), dict) else {}
    actions = [a for a in parts.get("actions") or [] if isinstance(a, dict)]
    levers: dict[str, int] = {}
    for axis, action in zip(_action_axes(run, len(actions)), actions):
        count = len(action.get("levers") or []) if isinstance(action.get("levers"), list) else 0
        if count:
            levers[str(axis)] = levers.get(str(axis), 0) + count
    practices: dict[str, int] = {}
    for item in best_practices or []:
        if isinstance(item, dict):
            count = len(item.get("practices") or []) if isinstance(item.get("practices"), list) else 0
            practices[str(item.get("axis"))] = practices.get(str(item.get("axis")), 0) + count
    problems = []
    for axis in sorted(set(levers) | set(practices)):
        if levers.get(axis, 0) != practices.get(axis, 0):
            problems.append(
                f"{where}: ось {axis}: рычагов в feedback_parts.actions[].levers {levers.get(axis, 0)}, "
                f"а приёмов в best_practices {practices.get(axis, 0)}: числа должны совпадать (условие 7)"
            )
    return problems


def _check_text(run: dict, where: str, address: str | None) -> list[str]:
    """Условие 9 по полям: запреты текста в каждом поле прозы (поля `quote` не проверяются)."""
    problems = []
    for name, text in _prose_fields(run):
        for problem in text_rules.prose_problems(text_rules.prose_outside_quotes(text), address):
            problems.append(f"{where}: {name}: {problem}")
    return problems


def _run_schema_problems(run: dict, where: str) -> list[str]:
    try:
        full = schema.load_schema("tracker")
    except (FileNotFoundError, ValueError) as err:
        raise SrError(f"схема трекера не читается: {err} ({schema.schema_path('tracker')})", 1) from err
    run_schema = ((full.get("properties") or {}).get("runs") or {}).get("items") or {}
    return [f"{where}: {p}" for p in schema.mini_validate(run, run_schema, "$")]


def validate_run(run: dict, tr: dict, *, length: str | None = None, address: str | None = None,
                 check_schema: bool = True) -> list[str]:
    """Проблемы одного прогона (пусто — всё сходится); отозванный прогон не проверяется.

    Предыдущие прогоны — те, что стоят в `tr["runs"]` раньше `run` (если `run` там нет — все):
    по ним считаются следующий номер рекомендации и открытые рекомендации. `length` и `address`
    — из карточки человека (`short` → не больше двух пунктов и без рычагов; `address` — одно
    обращение на весь текст).
    """
    if not isinstance(run, dict):
        return ["прогон не объект"]
    if not is_live(run):
        return []
    where = str(run.get("run_id") or "run")
    runs = [r for r in tr.get("runs") or [] if isinstance(r, dict)]
    pos = next((i for i, r in enumerate(runs) if r is run), len(runs))
    prior = runs[:pos]
    axis_ids = i18n.keys("axes")
    problems = _run_schema_problems(run, where) if check_schema else []
    if not run.get("person_id"):
        problems.append(f"{where}: нет person_id")
    problems += _check_axes(run, where, axis_ids)
    problems += _check_level_and_gate(run, where)
    problems += _check_override(run, where, axis_ids)
    problems += _check_recs(run, where, prior, runs, axis_ids)
    problems += _check_parts(run, where, axis_ids, length)
    problems += _check_text(run, where, address)
    return problems


def draft_verdict_problems(run: dict, runs: list, gated: bool) -> list[str]:
    """Вердикты `run` по рекомендациям неодобренных прогонов при гейте ревьюера (проверка `append`).

    Прошлые прогоны — те, что стоят в `runs` раньше `run`. Проверяется только новый прогон:
    вердикты, записанные раньше, `validate_file` не трогает, чтобы старые трекеры не краснели.
    """
    if not gated:
        return []
    pos = next((i for i, r in enumerate(runs) if r is run), len(runs))
    prior = [r for r in runs[:pos] if isinstance(r, dict)]
    by_id = {r.get("run_id"): r for r in prior}
    issued = issued_recs(prior, run.get("person_id") or "")
    where = str(run.get("run_id") or "run")
    problems = []
    for verdict in run.get("prev_recommendations") or []:
        item = issued.get(verdict.get("id")) if isinstance(verdict, dict) else None
        if item is None or not _awaits_approval(by_id.get(item["run_id"]) or {}, gated):
            continue
        problems.append(
            f"{where}: {verdict['id']} из неодобренного черновика {item['run_id']}: вердикт откладывается "
            f"до approve (человек этот текст не получал); уберите вердикт из prev_recommendations или "
            f"одобрите черновик командой approve {run.get('person_id')} {item['run_id']}"
        )
    return problems


def _card_feedback(root: Path, person_id: str) -> dict:
    return people.load_person(root, person_id).get("feedback") or {}


def _other_problems(root: Path, tr: dict, skip: dict | None) -> list[str]:
    problems = []
    seen: set = set()
    for i, run in enumerate(tr.get("runs") or []):
        where = run.get("run_id") if isinstance(run, dict) else None
        if where in seen:
            problems.append(f"{where}: дубликат run_id: прогоны неразличимы")
        seen.add(where)
        if run is skip or not is_live(run):
            continue
        try:
            feedback = _card_feedback(root, run.get("person_id") or "")
        except SrError as err:
            problems.append(f"{where or f'runs[{i}]'}: карточка человека: {err.message}")
            feedback = {}
        problems += validate_run(run, tr, length=feedback.get("length"), address=feedback.get("address"),
                                 check_schema=False)
    return problems


def validate_file(root: Path) -> list[str]:
    """Весь трекер: схема `tracker.json`, дубли `run_id` и `validate_run` каждого живого прогона."""
    tr = load(root)
    return schema_problems(tr, "tracker") + _other_problems(root, tr, None)


# --- append ------------------------------------------------------------------


def _read_payload(path: Path) -> dict:
    if not path.is_file():
        raise SrError(f"нет файла с выходом судьи ({path})", 2)
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise SrError(f"выход судьи не разбирается как JSON: {err} ({path})", 1) from err
    if isinstance(obj, dict) and "stub" in obj:
        raise SrError(f"стаб не заполнен: агент не записал выход ({path})", 1)
    problems = schema_problems(obj, "judge")
    if problems:
        shown = problems[:MAX_SCHEMA_ERRORS]
        tail = len(problems) - len(shown)
        raise SrError(
            f"прогон не записан: выход судьи не сошёлся со схемой judge ({path}), расхождений "
            f"{len(problems)}:\n  " + "\n  ".join(shown) + (f"\n  … и ещё {tail}" if tail else ""),
            1,
        )
    status = obj.get("status") or {}
    if not status.get("ok") or not isinstance(obj.get("data"), dict):
        raise SrError(
            f"прогон не записан: судья вернул status {status.get('code')!r} "
            f"({status.get('message') or 'без пояснения'}), блока data нет ({path})",
            1,
        )
    return obj


def _source(root: Path, path: Path) -> dict:
    resolved = path.resolve()
    try:
        shown = resolved.relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        shown = str(resolved)
    return {"path": shown, "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest()}


def _build_run(obj: dict, *, run_id: str, ws_id: str, person_id: str, label: str | None,
               confidence: str, skipped, now_iso: str, revision: int, override) -> dict:
    data = obj["data"]
    meta = obj.get("metadata") or {}
    scope = data.get("session_scope") or {}
    summary = data.get("summary") or {}
    axes = [
        {
            "id": a.get("id"),
            "score": a.get("score"),
            "insufficient_evidence": bool(a.get("insufficient_evidence")),
            "insufficient_evidence_reason": a.get("insufficient_evidence_reason"),
            "evidence": a.get("evidence") or [],
            "subcriteria": a.get("subcriteria") or [],
            "note": a.get("note"),
        }
        for a in data.get("axes") or []
    ]
    run = {
        "run_id": run_id,
        "workspace": ws_id,
        "period": {"label": label, "from": None, "to": None, "anchor": None},
        "period_confidence": confidence,
        "person_id": person_id,
        "reviewed_at": now_iso,
        "reviewer": f"log-judge@{meta.get('prompt_version') or 'unknown'}",
        "collector": meta.get("collector_version"),
        "batches": meta.get("batches"),
        "sessions_total": meta.get("sessions_total"),
        "tracker_version": TRACKER_VERSION,
        "sessions_reviewed": scope.get("sessions") or [],
        "sessions_skipped_known": list(skipped or []),
        "agent_stack": scope.get("agent_stack") or [],
        "metrics_available": meta.get("metrics_available"),
        "context_report_available": meta.get("context_report_available"),
        "warnings": meta.get("warnings") or [],
        "turns_total": meta.get("turns_analyzed"),
        "axes": axes,
        "antipatterns": [a for a in data.get("antipatterns") or [] if a.get("found")],
        "flags": data.get("flags") or [],
        "gating_triggered": bool(data.get("gating_triggered")),
        "gating_evidence": data.get("gating_evidence") or [],
        "task_solved": summary.get("task_solved"),
        "summary": summary.get("one_thing_to_change"),
        "recommendations": data.get("recommendations") or [],
        "prev_recommendations": data.get("prev_recommendations") or [],
        "feedback_draft": None,
        "feedback_parts": data.get("feedback_parts"),
        "revision": revision,
        "operator_override": override,
        "logs_index_marks": [],
        "level_judge": summary.get("level_judge"),
        "level_computed": None,
        "review_status": "draft",
        "artifact": None,
        "source": None,
    }
    # предложения правок карточки: как есть и только если судья их дал (нет → ключа нет)
    if "card_suggestions" in data:
        run["card_suggestions"] = data["card_suggestions"]
    return run


def _renumber_recs(run: dict, runs: list) -> list[str]:
    """Перенумеровать новые рекомендации `run` подряд со следующего свободного номера; вернуть предупреждения.

    Судья знает номера только из `open_recs`, а номера отозванных и закрытых рекомендаций заняты
    навсегда: взятый им номер может оказаться выданным. Первый номер — следующий после всех
    прогонов человека, стоящих в `runs` раньше `run` (при `--replace` номера заменяемого остаются
    замене, как в `validate_run`). Перенумеровываются только id формы `REC-<person>-NNN` без
    повторов внутри прогона: чужой person_id, другая форма или дубль остаются как есть, их
    отклоняет `validate_run`. Порядок рекомендаций не меняется (связь с пунктами — по индексу).
    Выход судьи на диске не трогается: меняется только прогон, который пишется в трекер.
    """
    pid = run.get("person_id") or ""
    recs = run.get("recommendations")
    if not isinstance(recs, list) or not recs:
        return []
    ids = [rec.get("id") if isinstance(rec, dict) else None for rec in recs]
    if any(_rec_number(rid, pid) is None for rid in ids) or len(set(ids)) != len(ids):
        return []
    pos = next((i for i, r in enumerate(runs) if r is run), len(runs))
    start = _next_number(runs[:pos], pid)
    new_ids = [f"REC-{pid}-{start + i:03d}" for i in range(len(recs))]
    if new_ids == ids:
        return []
    run["recommendations"] = [dict(rec, id=new) for rec, new in zip(recs, new_ids)]
    pairs = ", ".join(f"{old} → {new}" for old, new in zip(ids, new_ids) if old != new)
    return [
        f"рекомендации перенумерованы: {pairs} (судья взял занятый или не следующий номер, "
        f"следующий свободный REC-{pid}-{start:03d}; выход судьи на диске не изменён)"
    ]


def _lookup(index: dict, person_id: str, session: dict) -> dict | None:
    for ref in (session.get("session_id"), session.get("path")):
        if isinstance(ref, str) and ref.strip():
            entry = marks.find_entry(index, person_id, ref)
            if entry is not None:
                return entry
    return None


def _session_ref(session: dict) -> str:
    return str(session.get("session_id") or session.get("path") or "?")


def _newer_stamp(entry: dict, runs: list, run: dict) -> str | None:
    """Штамп на записи, если его поставил живой прогон того же человека, стоящий в `runs` позже `run`."""
    stamp = marks.stamped_by(entry)
    if stamp is None or stamp == run.get("run_id"):
        return None
    pos = next((i for i, r in enumerate(runs) if r is run), len(runs))
    for other in runs[pos + 1:]:
        if (is_live(other) and other.get("person_id") == run.get("person_id")
                and other.get("run_id") == stamp):
            return stamp
    return None


def _place_mark(entry: dict, run: dict, runs: list, status: str, note, now_iso: str,
                old_by_key: dict) -> tuple[dict, str | None]:
    """Пометить запись прогоном `run`; вернуть (пометка, id более нового прогона или `None`).

    Запись со штампом более нового живого прогона того же человека не трогается: иначе старый
    прогон перетёр бы новую оценку и порвал цепочку `prev`. Сессия остаётся в покрытии: берётся
    прежняя пометка этого прогона по ключу или `marks.kept_mark`.
    """
    newer = _newer_stamp(entry, runs, run)
    kind = REVIEWED_STATUS if status == REVIEWED_STATUS else "excluded"
    if newer is not None:
        reason = None if kind == REVIEWED_STATUS else status
        return old_by_key.get(entry.get("key")) or marks.kept_mark(entry, kind, newer, reason), newer
    if status == REVIEWED_STATUS:
        return marks.mark_reviewed(entry, run.get("run_id"), now_iso), None
    return marks.mark_excluded(entry, run.get("run_id"), status, note, now_iso), None


def _period_dates(run: dict, entries: list[dict]) -> tuple[str | None, str | None]:
    """min `log_started_at` / max `log_ended_at` записей индекса; нет → даты из путей сессий."""
    starts = [e["log_started_at"] for e in entries if isinstance(e.get("log_started_at"), str)]
    ends = [e["log_ended_at"] for e in entries if isinstance(e.get("log_ended_at"), str)]
    if starts or ends:
        return (min(starts) if starts else None, max(ends) if ends else None)
    dates = []
    for s in run.get("sessions_reviewed") or []:
        m = LEADING_DATE_RE.match(Path(str(s.get("path") or "")).name)
        if m:
            dates.append(m.group(1))
    return (min(dates), max(dates)) if dates else (None, None)


def _tail_turns(entry: dict) -> int | None:
    """Ходы сессии, прочитанные прогоном: у выросшей (`review_from_turn`) — хвост, иначе все.

    Берётся до пометки: `mark_reviewed` снимает `review_from_turn`. `None` — по записи не посчитать.
    """
    total = entry.get("turns_total")
    if not isinstance(total, int) or isinstance(total, bool):
        return None
    start = entry.get("review_from_turn")
    if start is None:
        return total
    if not isinstance(start, int) or isinstance(start, bool) or not 0 <= start <= total:
        return None
    return total - start


def _run_turns(judge_turns, tails: list[int], unknown: list[str]) -> tuple[int | None, list[str]]:
    """`turns_total` прогона по индексу (сумма `_tail_turns` оценённых сессий); вернуть (ходы, предупреждения).

    Хоть одну сессию по индексу не посчитать → `metadata.turns_analyzed` судьи целиком и строка.
    Расхождение с судьёй — строка, не ошибка: судья суммирует пачки, индекс знает хвосты.
    """
    if unknown:
        return judge_turns, [
            f"turns_total прогона взят из metadata.turns_analyzed судьи ({judge_turns}): "
            f"по индексу логов не посчитать ходы сессий {', '.join(unknown)}"
        ]
    counted = sum(tails)
    if judge_turns is None:
        return counted, [f"в metadata судьи нет turns_analyzed: turns_total = {counted} по индексу логов"]
    if judge_turns != counted:
        return counted, [
            f"судья указал {judge_turns} ходов, по индексу {counted}: turns_total прогона = {counted} "
            f"(у выросших сессий — только хвост)"
        ]
    return counted, []


def _clamp_from(run: dict, runs: list, frm, to) -> tuple[str | None, list[str]]:
    """Начало периода не раньше `period.to` предыдущего живого прогона человека (выросшая сессия).

    Предыдущий — ближайший живой прогон того же человека с `period.to`, стоящий в `runs` до `run`.
    `to` не трогается; если `to` раньше конца предыдущего — сдвиг дал бы `from > to`: `from` остаётся,
    в предупреждение строка. Нет предыдущего — `from` как есть.
    """
    if not isinstance(frm, str):
        return frm, []
    pos = next((i for i, r in enumerate(runs) if r is run), len(runs))
    for prev in reversed(runs[:pos]):
        prev_to = (prev.get("period") or {}).get("to") if isinstance(prev, dict) else None
        if is_live(prev) and prev.get("person_id") == run.get("person_id") and isinstance(prev_to, str):
            break
    else:
        return frm, []
    if frm >= prev_to:
        return frm, []
    if isinstance(to, str) and to < prev_to:
        return frm, [f"период прогона ({frm} — {to}) заходит в период {prev.get('run_id')} "
                     f"(до {prev_to}), а сдвиг начала дал бы начало позже конца: начало не сдвинуто"]
    return prev_to, []


def _touch_registry(root: Path, reviewed_at: str) -> list[str]:
    """`last_run_at` = `reviewed_at` в реестре workspaces; вернуть предупреждения итога.

    Вызывается после записи трекера и индекса: сбой реестра прогон не откатывает, а уходит
    в предупреждение с текстом ошибки. Workspace не в реестре — тоже предупреждение.
    """
    try:
        entry = registry.find_by_path(root)
        if entry is None:
            return [f"workspace не в реестре ~/.retropair/workspaces.json, "
                    f"last_run_at не записан ({root})"]
        registry.update(entry["id"], last_run_at=reviewed_at)
    except SrError as err:
        return [f"прогон записан, а реестр workspaces нет: last_run_at не обновлён: {err.message}"]
    return []


def append(root: Path, payload_path, person_id: str, *, period_label: str | None = None,
           period_confidence: str = "inferred", replace: str | None = None, skipped=(),
           now: datetime | None = None) -> dict:
    """Записать прогон из выхода судьи и пометить его сессии в индексе логов.

    Порядок: карточка (нет → 2) → метка (2) → файл (нет → 2) → JSON (1) → стаб (1) → схема judge
    (1) → `status.ok` и `data` (1) → перенумерация новых рекомендаций (`_renumber_recs`, строка в
    `warnings`) → `validate_run` и `check_practices` (1) → трекер → индекс. `--replace`: тот же
    человек, `run_id` сохраняется, прогон встаёт на место заменяемого, `revision + 1`,
    `operator_override` переносится, пометки заменяемого снимаются. Нет индекса или сессии в нём →
    прогон пишется, в итоге `logs_unmatched`. Метки нет и `config.period.mode == "run"` → метку
    выдаёт `run_mode_label` (уникальна у человека); явная метка живого прогона того же человека
    без `--replace` → `SrError(1)`. Итоговая метка — в `period_label` результата. Сессии со статусом из `marks.OPEN_STATUSES`
    (`other_period`) в индексе не закрываются (`logs_left_open`); запись со штампом более нового
    прогона не перетирается (`logs_kept_newer`). `turns_total` — по индексу (`_run_turns`: у выросшей
    сессии только хвост), начало периода не раньше конца предыдущего живого прогона (`_clamp_from`).
    """
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    now_iso = marks.iso_utc(now)
    card = people.load_person(root, person_id)
    if period_confidence not in ("inferred", "explicit"):
        raise SrError(f"period_confidence {period_confidence!r}: допустимы inferred, explicit", 2)
    if period_label is not None:
        make_run_id("ws", person_id, period_label, now)  # проверка метки до чтения файлов
    path = Path(payload_path)
    obj = _read_payload(path)

    with file_lock(tracker_path(root)):
        tr = load(root)
        ws_id = tr.get("workspace") or load_config(root)["workspace"]["id"]
        victim, revision, override = None, 1, None
        if replace:
            victim = find_run(tr, replace)
            if victim.get("person_id") != person_id:
                raise SrError(
                    f"прогон {replace} принадлежит {victim.get('person_id')!r}, а пишем {person_id!r}", 1
                )
            if not is_live(victim):
                raise SrError(f"прогон {replace} отозван: заменять нечего, пишите новый прогон", 1)
            revision = int(victim.get("revision") or 1) + 1
            override = victim.get("operator_override")
            label = period_label if period_label is not None else (victim.get("period") or {}).get("label")
            run_id = replace
        elif period_label is not None:
            same = live_run_with_label(tr, person_id, period_label)
            if same is not None:
                raise SrError(
                    f"у {person_id} уже есть прогон {same.get('run_id')} с меткой {period_label}: "
                    f"повтор периода — `--replace {same.get('run_id')}`, новый период — другая метка",
                    1,
                )
            label = period_label
            run_id = make_run_id(ws_id, person_id, label, now)
        else:
            mode = (load_config(root).get("period") or {}).get("mode")
            label = run_mode_label(tr, person_id, now) if mode == "run" else None
            run_id = make_run_id(ws_id, person_id, label, now)
        run = _build_run(obj, run_id=run_id, ws_id=ws_id, person_id=person_id, label=label,
                         confidence=period_confidence, skipped=skipped, now_iso=now_iso,
                         revision=revision, override=override)
        run["source"] = _source(root, path)
        others = [r for r in tr["runs"] if r is not victim]
        # замена встаёт на место заменяемого: номера REC и статусы считаются по прогонам до него
        runs = [run if r is victim else r for r in tr["runs"]] if victim is not None else tr["runs"] + [run]
        work = dict(tr, runs=runs)
        renumbered = _renumber_recs(run, runs)

        feedback = card.get("feedback") or {}
        problems = validate_run(run, work, length=feedback.get("length"), address=feedback.get("address"))
        problems += check_practices(run, obj["data"].get("best_practices"))
        problems += draft_verdict_problems(run, work["runs"], reviewer_gated(load_config(root)))
        if any(isinstance(r, dict) and r.get("run_id") == run_id for r in others):
            problems.append(f"{run_id}: дубликат run_id: прогон с таким id уже есть")
        if problems:
            raise SrError("прогон не записан, инварианты трекера не сошлись:\n  " + "\n  ".join(problems), 1)

        warnings: list[str] = list(renumbered)
        with marks.index_lock(root):
            index = marks.load_index(root)
            # A16: вердикт по рекомендации, выданной после всех логов прогона, — not_applicable_yet
            cfg = load_config(root)
            guard = early_verdict_guard(run, work["runs"], index, reviewer_gated(cfg), cfg.get("lang") or "ru")
            run["warnings"] = list(run["warnings"]) + guard
            warnings += guard
            unmatched: list[str] = []
            entries: list[dict] = []
            kept: list[str] = []
            tails: list[int] = []
            untold: list[str] = []  # оценённые сессии, чьи ходы по индексу не посчитать
            left_open = [_session_ref(s) for s in run["sessions_reviewed"]
                         if s.get("status") in marks.OPEN_STATUSES]
            to_mark = [s for s in run["sessions_reviewed"] if s.get("status") not in marks.OPEN_STATUSES]
            if index is None:
                unmatched = [_session_ref(s) for s in to_mark]
                untold = [_session_ref(s) for s in to_mark
                          if (s.get("status") or REVIEWED_STATUS) == REVIEWED_STATUS]
                warnings.append(
                    f"индекса логов нет ({ws_paths(root)['logs_index']}): пометки не поставлены, "
                    f"следующий прогон увидит эти сессии как новые"
                )
            else:
                old_by_key: dict = {}
                if victim is not None:
                    by_key = _person_entries(index, person_id)
                    for mark in victim.get("logs_index_marks") or []:
                        if not isinstance(mark, dict):
                            continue
                        entry = by_key.get(mark.get("key"))
                        if entry is not None and _unwind(entry, tr, run_id, mark) == "kept_newer":
                            old_by_key[mark.get("key")] = mark
                for session in to_mark:
                    entry = _lookup(index, person_id, session)
                    status = session.get("status") or REVIEWED_STATUS
                    tail = _tail_turns(entry) if entry is not None else None  # до пометки
                    if status == REVIEWED_STATUS:
                        (tails if tail is not None else untold).append(
                            tail if tail is not None else _session_ref(session))
                    if entry is None:
                        unmatched.append(_session_ref(session))
                        continue
                    mark, newer = _place_mark(entry, run, work["runs"], status, session.get("status_note"),
                                              now_iso, old_by_key)
                    if newer is not None:
                        kept.append(entry.get("key"))
                    if status == REVIEWED_STATUS:
                        entries.append(entry)
                    run["logs_index_marks"].append(mark)
                if unmatched:
                    warnings.append(
                        "в индексе логов не нашлись сессии, они останутся новыми: " + ", ".join(unmatched)
                    )
            frm, run["period"]["to"] = _period_dates(run, entries)
            run["period"]["from"], clamp_warn = _clamp_from(run, work["runs"], frm, run["period"]["to"])
            run["turns_total"], turns_warn = _run_turns(run["turns_total"], tails, untold)
            warnings += clamp_warn + turns_warn
            marked, expected = marks.coverage(run, index)
            if index is not None and marked < expected:
                warnings.append(
                    f"прогон объявил {expected} сессий, а пометок встало {marked}: догоните покрытие "
                    f"командой mark-logs {run_id}"
                )
            if not (obj.get("metadata") or {}).get("prompt_version"):
                warnings.append("в metadata судьи нет prompt_version: reviewer записан как log-judge@unknown")

            tr["runs"] = work["runs"]
            tr["workspace"] = ws_id
            _save(root, tr, now)
            if index is not None:
                marks.save_index(root, index, now=now)

    warnings += _touch_registry(root, run["reviewed_at"])
    return {
        "run_id": run_id,
        "period_label": label,
        "revision": revision,
        "logs_marked": len(run["logs_index_marks"]),
        "logs_unmatched": len(unmatched),
        "logs_kept_newer": kept,
        "logs_left_open": left_open,
        "coverage": {"marked": marked, "expected": expected},
        "warnings": warnings,
        "warnings_other_runs": _other_problems(root, tr, run),
    }


# --- рекомендации ------------------------------------------------------------


def rec_statuses(tr: dict, person_id: str) -> dict[str, str]:
    """`REC-id → статус` выданных рекомендаций человека, на лету по живым прогонам.

    `followed`/`ignored` закрывают, `partial` оставляет открытой, `not_observable` и
    `not_applicable_yet` не меняют.
    Рекомендации отозванного прогона сюда не попадают, его вердикты тоже не считаются.
    """
    return {rid: item["status"] for rid, item in issued_recs(tr.get("runs") or [], person_id).items()}


def reviewer_gated(cfg: dict) -> bool:
    """Текст уходит человеку только после approve: режим `team`/`course` и `reviewer_gate: true`.

    Тогда рекомендации неодобренного прогона человек не получал, и вердикт по ним не выносится.
    """
    return cfg.get("mode") in GATED_MODES and cfg.get("reviewer_gate") is True


def _awaits_approval(run: dict, gated: bool) -> bool:
    return gated and run.get("review_status") != APPROVED_STATUS


# --- момент выдачи рекомендации и логи после него -----------------------------


def parse_ts(value) -> datetime | None:
    """Время из трекера или индекса логов (`YYYY-MM-DDTHH:MM:SSZ`, пробел вместо `T`, смещение)
    → aware datetime в UTC; не строка или не разбирается → `None` (вызывающий пишет предупреждение)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def rec_issue_time(run: dict, gated: bool) -> tuple[str | None, str | None]:
    """(момент выдачи рекомендаций прогона, предупреждение или `None`).

    Без гейта ревьюера (self, team/course с `reviewer_gate: false`) текст уходит человеку сразу:
    момент выдачи — `reviewed_at` прогона. С гейтом — `approved_at` (время approve); черновик →
    `(None, None)`: человек его не получал. Одобренный прогон без `approved_at` (одобрен до
    появления поля) → `reviewed_at` со строкой-предупреждением: момент выдачи мог быть позже.
    """
    if _awaits_approval(run, gated):
        return None, None
    if gated:
        approved = run.get("approved_at")
        if isinstance(approved, str) and approved.strip():
            return approved, None
        return run.get("reviewed_at"), (
            f"прогон {run.get('run_id')} одобрен без approved_at (одобрение записано до появления поля): "
            f"момент выдачи его рекомендаций взят по reviewed_at {run.get('reviewed_at')}, "
            f"настоящий approve мог быть позже"
        )
    return run.get("reviewed_at"), None


def _session_label(entry: dict) -> str:
    return str(entry.get("session_id") or entry.get("key") or entry.get("path") or "?")


def _undated(entries: list[dict]) -> list[str]:
    """Предупреждения по сессиям без `log_ended_at` (и без начала после любой выдачи): не угадываем."""
    return [f"у сессии {_session_label(e)} нет дат лога (log_ended_at): она не считается сессией "
            f"после выдачи рекомендаций" for e in entries if parse_ts(e.get("log_ended_at")) is None]


def _after(entry: dict, issued: datetime) -> bool:
    """Сессия шла после выдачи: `log_ended_at > issued` (длинная сессия, начатая до выдачи и
    продолженная после, годится). Конца нет, но начало позже выдачи — тоже после; иначе нет."""
    end = parse_ts(entry.get("log_ended_at"))
    if end is not None:
        return end > issued
    start = parse_ts(entry.get("log_started_at"))
    return start is not None and start > issued


def rec_evaluable(issued_at, entries: list[dict]) -> bool:
    """Есть ли среди записей индекса `entries` сессия, законченная после `issued_at`."""
    issued = parse_ts(issued_at)
    return issued is not None and any(_after(e, issued) for e in entries)


def period_session_entries(root: Path, person_id: str, period_label: str | None) -> tuple[list[dict], list[str]]:
    """Записи индекса, которые пойдут в пачки периода (как `batches`), и предупреждения.

    Нет индекса → `([], [строка])`: без дат сессий ни одна рекомендация не оценима.
    """
    index = marks.load_index(root)
    if index is None:
        return [], [f"индекса логов нет ({ws_paths(root)['logs_index']}): даты сессий периода не "
                    f"известны, ни одна рекомендация не оценима (evaluable: false)"]
    entries = [e for e in index.get("logs") or [] if isinstance(e, dict) and e.get("person_id") == person_id]
    return batches.period_candidates(entries, load_config(root)["batches"], period_label), []


def open_recs(tr: dict, person_id: str, *, gated: bool, sessions: list[dict] | None = None) -> dict:
    """`{open, pending_approval, next_number}`: рекомендации `{id, axis, text, period, run_id, issued_at}`.

    `gated` (`reviewer_gated(config)`) → в `open` только рекомендации одобренных прогонов,
    открытые рекомендации живых неодобренных прогонов — в `pending_approval`: человек их не
    получал, вердикт по ним откладывается до approve. Без гейта `pending_approval` пуст.
    `period` — метка периода прогона, выдавшего рекомендацию; `issued_at` — момент выдачи
    (`rec_issue_time`; у черновых `None`). Номер считается по всем прогонам, включая отозванные
    и черновики: их номера заняты навсегда.

    `sessions` — записи индекса сессий текущего периода (`period_session_entries`): тогда у
    каждой рекомендации из `open` есть `evaluable` (хоть одна сессия закончена после
    `issued_at`), а в результате — `warnings` (сессии без дат, одобрение без `approved_at`).
    """
    by_id = {r.get("run_id"): r for r in tr.get("runs") or [] if isinstance(r, dict)}
    opened, pending, warnings = [], [], []
    for rid, item in issued_recs(tr.get("runs") or [], person_id).items():
        if item["status"] != "open":
            continue
        run = by_id.get(item["run_id"]) or {}
        issued_at, warning = rec_issue_time(run, gated)
        if warning and warning not in warnings:
            warnings.append(warning)
        target = pending if _awaits_approval(run, gated) else opened
        rec = {
            "id": rid,
            "axis": item["rec"].get("axis"),
            "text": item["rec"].get("text"),
            "period": (run.get("period") or {}).get("label"),
            "run_id": item["run_id"],
            "issued_at": issued_at,
        }
        if sessions is not None and target is opened:
            rec["evaluable"] = rec_evaluable(issued_at, sessions)
            if parse_ts(issued_at) is None:
                warnings.append(f"у {rid} нет момента выдачи (reviewed_at/approved_at прогона "
                                f"{item['run_id']}: {issued_at!r}): evaluable: false")
        target.append(rec)
    result = {"open": opened, "pending_approval": pending, "next_number": next_rec_number(tr, person_id)}
    if sessions is not None:
        result["warnings"] = warnings + (_undated(sessions) if opened else [])
    return result


def early_verdict_guard(run: dict, runs: list, index: dict | None, gated: bool, lang: str = "ru") -> list[str]:
    """Страж `append`: вердикт по рекомендации, выданной после всех логов прогона → `not_applicable_yet`.

    Вердикт `followed|partial|ignored|not_observable` по рекомендации, у которой ни одна
    оценённая сессия прогона (`sessions_reviewed` со статусом `reviewed`, даты из индекса) не
    закончена после момента выдачи (`rec_issue_time`), заменяется на `not_applicable_yet`:
    рекомендация остаётся открытой. Каждая замена — строка в возвращаемых предупреждениях (они
    идут в `warnings` прогона и в вывод команды). Сессия без дат или вне индекса после выдачи не
    считается (не угадываем) и тоже даёт строку. Черновые рекомендации не трогаются: вердикт
    по ним отклоняет `draft_verdict_problems`. Проверяется только новый прогон; записанные
    раньше вердикты `validate_file` не трогает.
    """
    pid = run.get("person_id") or ""
    pos = next((i for i, r in enumerate(runs) if r is run), len(runs))
    prior = [r for r in runs[:pos] if isinstance(r, dict)]
    by_id = {r.get("run_id"): r for r in prior}
    issued = issued_recs(prior, pid)
    verdicts = run.get("prev_recommendations") or []
    targets = [v for v in verdicts if isinstance(v, dict) and v.get("verdict") in EVALUATED_VERDICTS
               and v.get("id") in issued]
    if not targets:
        return []
    entries, warnings = [], []
    for session in run.get("sessions_reviewed") or []:
        if not isinstance(session, dict) or (session.get("status") or REVIEWED_STATUS) != REVIEWED_STATUS:
            continue
        entry = _lookup(index, pid, session) if index is not None else None
        if entry is None:
            warnings.append(f"сессии {_session_ref(session)} нет в индексе логов: её даты не известны, "
                            f"после выдачи рекомендаций она не считается")
            continue
        entries.append(entry)
    warnings += _undated(entries)
    for verdict in targets:
        source = by_id.get(issued[verdict["id"]]["run_id"]) or {}
        if _awaits_approval(source, gated):
            continue
        issued_at, warning = rec_issue_time(source, gated)
        if warning and warning not in warnings:
            warnings.append(warning)
        if rec_evaluable(issued_at, entries):
            continue
        old = verdict["verdict"]
        verdict.update(verdict=TOO_EARLY_VERDICT, evidence=None, note=(
            f"the period's logs ended before the recommendation was issued ({issued_at}); "
            f"the judge's verdict {old} was replaced" if lang == "en" else
            f"логи периода закончились до выдачи рекомендации ({issued_at}); вердикт судьи {old} заменён"))
        warnings.append(
            f"вердикт {verdict['id']} заменён: логи периода старше выдачи ({old} → not_applicable_yet; "
            f"выдана {issued_at}, ни одна оценённая сессия прогона не закончена позже; рекомендация "
            f"остаётся открытой, судья не перезапускается)"
        )
    return warnings


# --- правка прогона ----------------------------------------------------------


IMMUTABLE_FIELDS = ("run_id", "person_id", "workspace", "tracker_version", "retracted")


def _run_properties() -> dict:
    try:
        full = schema.load_schema("tracker")
    except (FileNotFoundError, ValueError) as err:
        raise SrError(f"схема трекера не читается: {err} ({schema.schema_path('tracker')})", 1) from err
    return (((full.get("properties") or {}).get("runs") or {}).get("items") or {}).get("properties") or {}


def _live_run(tr: dict, run_id: str) -> dict:
    run = find_run(tr, run_id)
    if not is_live(run):
        why = (run.get("retracted") or {}).get("why")
        raise SrError(f"прогон {run_id} отозван ({why}): править его нельзя", 1)
    return run


def _mutate_run(root: Path, run_id: str, fields_of, now: datetime | None) -> dict:
    """Под замком трекера: найти живой прогон, получить поля `fields_of(run)`, проверить и записать."""
    root = Path(root)
    with file_lock(tracker_path(root)):
        tr = load(root)
        run = _live_run(tr, run_id)
        fields = fields_of(run)
        if not fields:
            raise SrError(f"правка прогона {run_id}: не передано ни одного поля", 2)
        props = _run_properties()
        problems = []
        for key, value in fields.items():
            if key in IMMUTABLE_FIELDS:
                raise SrError(f"правка прогона {run_id}: поле {key} менять нельзя", 2)
            if key not in props:
                raise SrError(
                    f"правка прогона {run_id}: поля {key!r} нет в схеме прогона трекера "
                    f"({schema.schema_path('tracker')})",
                    2,
                )
            problems += schema.mini_validate(value, props[key], f"$.{key}")
        if problems:
            raise SrError(f"прогон {run_id} не изменён, значения не сошлись со схемой:\n  "
                          + "\n  ".join(problems), 1)
        run.update(fields)
        _save(root, tr, now)
        return run


def update_run(root: Path, run_id: str, /, *, now: datetime | None = None, **fields) -> dict:
    """Записать поля живого прогона под замком трекера; ставит `updated_at` трекера, вернуть прогон.

    Поле вне схемы прогона или неизменяемое (`run_id`, `person_id`, `workspace`,
    `tracker_version`, `retracted`) → `SrError(2)`; значение не по схеме → `SrError(1)`;
    нет прогона или он отозван → `SrError(1)`. Ошибка — файл не тронут.
    """
    return _mutate_run(root, run_id, lambda run: dict(fields), now)


def set_level(root: Path, run_id: str, now: datetime | None = None) -> dict:
    """Посчитать уровень формулой (`level.compute`) и записать `level_computed` под замком.

    Считается по прогону, прочитанному внутри замка: правка ревьюера, записанная параллельно,
    не теряется. Возвращает `{run_id, level, median, values}`.
    """
    result: dict = {}

    def fields_of(run: dict) -> dict:
        result.update(level.compute(run))
        return {"level_computed": result["level"]}

    _mutate_run(root, run_id, fields_of, now)
    return {"run_id": run_id, "level": result["level"], "median": result["median"], "values": result["values"]}


# --- retract -----------------------------------------------------------------


def _person_entries(index: dict, person_id: str) -> dict:
    return {e.get("key"): e for e in index.get("logs") or []
            if isinstance(e, dict) and e.get("person_id") == person_id}


def _unwind(entry: dict, tr: dict, run_id: str, mark: dict) -> str:
    """Снять пометку прогона `run_id` с записи; дальше по цепочке, пока штамп от отозванного прогона.

    Запись, которую после `run_id` пометил другой прогон, не трогается (`kept_newer`). Если откат
    вернул штамп прогона, который сам уже отозван, откат продолжается его пометкой: иначе запись
    осталась бы закрытой оценкой, которой больше нет.
    """
    if marks.stamped_by(entry) != run_id:
        return "kept_newer"
    marks.unmark(entry, mark, set())
    seen = {run_id}
    by_id = {r.get("run_id"): r for r in tr.get("runs") or [] if isinstance(r, dict)}
    while True:
        stamp = marks.stamped_by(entry)
        older = by_id.get(stamp)
        if stamp is None or stamp in seen or older is None or is_live(older):
            return "restored"
        seen.add(stamp)
        older_mark = next((m for m in older.get("logs_index_marks") or []
                           if isinstance(m, dict) and m.get("key") == entry.get("key")), None)
        if older_mark is None:
            return "restored"
        marks.unmark(entry, older_mark, set())


def retract(root: Path, run_id: str, why: str, now: datetime | None = None) -> dict:
    """Отозвать прогон, не удаляя его: `retracted{at, why, logs_unmarked}` и откат пометок индекса.

    Пустой `why` → `SrError(2)`; нет прогона или уже отозван → `SrError(1)`. Пометки индекса
    возвращаются к `prev`, кроме записей, переписанных более новым прогоном (`kept_newer`).
    Возвращает `{run_id, logs_unmarked, kept_newer: [key], missing: [key]}`; `missing` — пометки,
    записи которых в индексе нет (или нет самого индекса).
    """
    root = Path(root)
    why = (why or "").strip()
    if not why:
        raise SrError(f"отзыв прогона {run_id} без причины: нужен --why, иначе потом не понять, что случилось", 2)
    now = now or datetime.now(timezone.utc)
    with file_lock(tracker_path(root)):
        tr = load(root)
        run = find_run(tr, run_id)
        if not is_live(run):
            raise SrError(f"прогон {run_id} уже отозван: {(run.get('retracted') or {}).get('why')}", 1)
        restored, kept, missing = 0, [], []
        with marks.index_lock(root):
            index = marks.load_index(root)
            run_marks = [m for m in run.get("logs_index_marks") or [] if isinstance(m, dict)]
            if index is None:
                missing = [m.get("key") for m in run_marks]
            else:
                entries = _person_entries(index, run.get("person_id"))
                for mark in run_marks:
                    entry = entries.get(mark.get("key"))
                    if entry is None:
                        missing.append(mark.get("key"))
                    elif _unwind(entry, tr, run_id, mark) == "restored":
                        restored += 1
                    else:
                        kept.append(mark.get("key"))
            run["retracted"] = {"at": marks.iso_utc(now), "why": why, "logs_unmarked": restored}
            _save(root, tr, now)
            if index is not None and restored:
                marks.save_index(root, index, now=now)
    return {"run_id": run_id, "logs_unmarked": restored, "kept_newer": kept, "missing": missing}


# --- compare -----------------------------------------------------------------


def compare(tr: dict, a: str, b: str) -> dict:
    """Расхождение двух прогонов одного человека (замер воспроизводимости оценки).

    Оси — с учётом правки ревьюера; `insufficient_evidence` не ноль, в дельту не идёт, переход
    «балл ↔ нет данных» — `evidence_shift`. Уровень сравнивается по формуле (`level.compute`),
    гейт — по `gating_triggered`, антипаттерны — по набору id. Разные люди → `SrError(1)`.
    """
    run_a, run_b = find_run(tr, a), find_run(tr, b)
    if run_a.get("person_id") != run_b.get("person_id"):
        raise SrError(
            f"прогоны разных людей не сравниваются: {a} ({run_a.get('person_id')}) и "
            f"{b} ({run_b.get('person_id')})",
            1,
        )
    sa, sb = effective_axes(run_a), effective_axes(run_b)
    axes = {}
    for axis in i18n.keys("axes"):
        va, vb = sa.get(axis), sb.get(axis)
        axes[axis] = {
            "a": va,
            "b": vb,
            "delta": vb - va if va is not None and vb is not None else None,
            "evidence_shift": (va is None) != (vb is None),
        }
    deltas = [abs(x["delta"]) for x in axes.values() if x["delta"] is not None]
    level_a, level_b = level.compute(run_a)["level"], level.compute(run_b)["level"]
    ap_a = {x.get("id") for x in run_a.get("antipatterns") or [] if isinstance(x, dict)}
    ap_b = {x.get("id") for x in run_b.get("antipatterns") or [] if isinstance(x, dict)}
    return {
        "person_id": run_a.get("person_id"),
        "a": a,
        "b": b,
        "axes": axes,
        "max_abs_delta": max(deltas, default=None),
        "shifted_ge2": [k for k, x in axes.items() if x["delta"] is not None and abs(x["delta"]) >= 2],
        "level": {"a": level_a, "b": level_b},
        "level_same": level_a == level_b,
        "gate_same": bool(run_a.get("gating_triggered")) == bool(run_b.get("gating_triggered")),
        "antipatterns_same": ap_a == ap_b,
        "antipatterns": {"only_in_a": sorted(ap_a - ap_b), "only_in_b": sorted(ap_b - ap_a)},
    }


# --- mark-logs ---------------------------------------------------------------


def _split_excluded(value: str) -> tuple[str, str | None]:
    """`REF=причина` → (REF, причина); хвост после `=` — причина, только если похож на токен."""
    if "=" in value:
        head, tail = value.rsplit("=", 1)
        if head and tail and "/" not in tail and " " not in tail:
            return head, tail
    return value, None


def mark_logs(root: Path, run_id: str, sessions=(), excluded=(), force: bool = False,
              exclude_note: str | None = None, now: datetime | None = None) -> dict:
    """Пометить в индексе логи уже записанного прогона (догнать покрытие).

    Сессии прогона (`sessions_reviewed`, со своими статусами) + `sessions` (оценённые, ссылки
    на запись индекса) + `excluded` (`REF` или `REF=причина`, по умолчанию `not_batched`,
    с `exclude_note`). Без `force` пометка добавочная: ставятся только сессии, которых ещё нет в
    `logs_index_marks`; явная ссылка на уже помеченную сессию с другим исходом → `SrError(1)`
    (конфликт, перепометить: `force`). С `force` прежние пометки сначала откатываются.
    Сессии `marks.OPEN_STATUSES` (`other_period`) не помечаются: их оценит прогон своего периода;
    явная причина `other_period` → `SrError(2)`. Запись со штампом более нового живого прогона
    того же человека не перетирается (`kept_newer`), в покрытие идёт. Нет прогона, отозван, нет
    индекса, явная ссылка не нашлась → `SrError(1)`, ничего не записано; неизвестная причина →
    `SrError(2)`. Сессия прогона, которой нет в индексе, попадает в `missing`, остальное пишется.
    Нечего добавить → файлы не пишутся. Возвращает `{marked, kept_newer, missing, coverage}`;
    `marked` — пометки, поставленные этим вызовом.
    """
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    now_iso = marks.iso_utc(now)
    with file_lock(tracker_path(root)):
        tr = load(root)
        run = find_run(tr, run_id)
        if not is_live(run):
            raise SrError(f"прогон {run_id} отозван, его логи не помечаются: "
                          f"{(run.get('retracted') or {}).get('why')}", 1)
        old = [m for m in run.get("logs_index_marks") or [] if isinstance(m, dict)]
        old_by_key = {m.get("key"): m for m in old}
        person_id = run.get("person_id")
        with marks.index_lock(root):
            index = marks.load_index(root)
            if index is None:
                raise SrError(
                    f"индекса логов нет ({ws_paths(root)['logs_index']}): пометить прогон {run_id} "
                    f"нечем, сначала соберите логи",
                    1,
                )
            explicit = []
            for ref in sessions or ():
                entry = marks.find_entry(index, person_id, ref)
                if entry is None:
                    raise SrError(f"сессия {ref!r} не найдена в индексе логов у {person_id}", 1)
                explicit.append((entry, REVIEWED_STATUS, None))
            for value in excluded or ():
                ref, reason = _split_excluded(value)
                reason = reason or marks.DEFAULT_EXCLUDE_REASON
                if reason in marks.OPEN_STATUSES:
                    raise SrError(
                        f"сессия {ref!r}: причина {reason} запись индекса не закрывает, сессию оценит "
                        f"прогон её периода; уберите её из --excluded",
                        2,
                    )
                if reason not in marks.EXCLUDE_REASONS:
                    raise SrError(
                        f"неизвестная причина исключения {reason!r} (сессия {ref!r}); "
                        f"допустимы: {', '.join(r for r in marks.EXCLUDE_REASONS if r not in marks.OPEN_STATUSES)}",
                        2,
                    )
                entry = marks.find_entry(index, person_id, ref)
                if entry is None:
                    raise SrError(f"сессия {ref!r} не найдена в индексе логов у {person_id}", 1)
                explicit.append((entry, reason, exclude_note))
            if not run.get("sessions_reviewed") and not explicit:
                raise SrError(f"в прогоне {run_id} нет sessions_reviewed: назовите сессии через "
                              f"--sessions или --excluded", 1)
            if not force:
                for entry, status, _note in explicit:
                    mark = old_by_key.get(entry.get("key"))
                    if mark is not None and _outcome(mark) != status:
                        raise SrError(
                            f"конфликт пометок: сессия {entry.get('key')} уже помечена прогоном {run_id} "
                            f"как {_outcome(mark)}, а просят {status}; перепометить весь прогон: --force",
                            1,
                        )

            if force:
                entries = _person_entries(index, person_id)
                for mark in old:
                    entry = entries.get(mark.get("key"))
                    if entry is not None:
                        _unwind(entry, tr, run_id, mark)
            targets, missing = [], []
            for session in run.get("sessions_reviewed") or []:
                if session.get("status") in marks.OPEN_STATUSES:
                    continue
                entry = _lookup(index, person_id, session)
                if entry is None:
                    missing.append(_session_ref(session))
                    continue
                targets.append((entry, session.get("status") or REVIEWED_STATUS, session.get("status_note")))
            runs = [r for r in tr["runs"] if isinstance(r, dict)]
            new_marks, kept, done = [], [], set()
            for entry, status, note in targets + explicit:
                key = entry.get("key")
                if key in done or (not force and key in old_by_key):
                    continue
                done.add(key)
                mark, newer = _place_mark(entry, run, runs, status, note, now_iso, old_by_key)
                if newer is not None:
                    kept.append(key)
                new_marks.append(mark)
            if force or new_marks:
                run["logs_index_marks"] = new_marks if force else old + new_marks
            marked, expected = marks.coverage(run, index)
            if force or new_marks:
                _save(root, tr, now)
                marks.save_index(root, index, now=now)
    return {"marked": len(new_marks), "kept_newer": kept, "missing": missing,
            "coverage": {"marked": marked, "expected": expected}}


# Причины, с которыми сессия закрывается без прогона: оценки нет, судить не о чем.
NO_RUN_REASONS = ("transcript_too_short", "empty_transcript", "unreadable_input")
NO_RUN_DEFAULT_REASON = "transcript_too_short"


def mark_logs_no_run(root: Path, person_id: str, excluded, exclude_note: str | None = None,
                     now: datetime | None = None) -> dict:
    """Закрыть в индексе сессии человека без прогона: период, где все новые сессии короткие.

    `excluded` — `REF` или `REF=причина`, причина только из `NO_RUN_REASONS` (по умолчанию
    `transcript_too_short`), иначе `SrError(2)`. Ссылки ищутся только среди записей `person_id`
    (`marks.find_entry`). Флаг ставит `inventory.mark` с `excluded_in_run: null` (прогона нет,
    как у исключения по согласию), `excluded_at` = сейчас; трекер не пишется, `retract` и
    покрытие прогонов эти пометки не видят. Замки те же, что у `mark_logs`: трекер → индекс.
    Не нашлась хоть одна ссылка → ничего не пишется, `missing` непуст. Запись уже помечена
    (`reviewed` или `excluded`) → `SrError(1)`, ничего не пишется. Нет индекса → `SrError(1)`.
    Возвращает `{person_id, marked: [{session_id, key, path, reason}], missing: [ref]}`.
    """
    root = Path(root)
    person_id = (person_id or "").strip()
    if not person_id:
        raise SrError("пометка без прогона: не указан человек (--person)", 2)
    parsed = []
    for value in excluded or ():
        ref, reason = _split_excluded(value)
        reason = reason or NO_RUN_DEFAULT_REASON
        if reason not in NO_RUN_REASONS:
            raise SrError(
                f"причина {reason!r} (сессия {ref!r}) без прогона не ставится: допустимы "
                f"{', '.join(NO_RUN_REASONS)}; остальные причины — у mark-logs с id прогона",
                2,
            )
        parsed.append((ref, reason))
    if not parsed:
        raise SrError(f"пометка без прогона у {person_id}: нет сессий в --excluded", 2)
    now = now or datetime.now(timezone.utc)
    now_iso = marks.iso_utc(now)
    with file_lock(tracker_path(root)):
        with marks.index_lock(root):
            index = marks.load_index(root)
            if index is None:
                raise SrError(f"индекса логов нет ({ws_paths(root)['logs_index']}): пометить сессии "
                              f"{person_id} нечем, сначала соберите логи", 1)
            targets, missing, seen = [], [], set()
            for ref, reason in parsed:
                entry = marks.find_entry(index, person_id, ref)
                if entry is None:
                    missing.append(ref)
                    continue
                if entry.get("key") in seen:
                    continue
                seen.add(entry.get("key"))
                if entry.get("reviewed") or entry.get("excluded"):
                    how = "оценена" if entry.get("reviewed") else f"исключена ({entry.get('excluded_reason')})"
                    raise SrError(
                        f"сессия {entry.get('session_id')} ({entry.get('path')}) у {person_id} уже {how}, "
                        f"прогон {marks.stamped_by(entry) or 'нет'}; без прогона закрываются только "
                        f"непомеченные сессии",
                        1,
                    )
                targets.append((entry, reason))
            if missing:
                return {"person_id": person_id, "marked": [], "missing": missing}
            done = []
            for entry, reason in targets:
                marks.mark_excluded(entry, None, reason, exclude_note, now_iso)
                done.append({"session_id": entry.get("session_id"), "key": entry.get("key"),
                             "path": entry.get("path"), "reason": reason})
            marks.save_index(root, index, now=now)
    return {"person_id": person_id, "marked": done, "missing": []}


def _outcome(mark: dict) -> str:
    """Исход пометки для сверки с новой: `reviewed` или причина исключения."""
    if mark.get("kind") == "excluded":
        return mark.get("reason") or marks.DEFAULT_EXCLUDE_REASON
    return REVIEWED_STATUS


# --- verify ------------------------------------------------------------------


def _rel(root: Path, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def _orphans(root: Path, tr: dict) -> list[str]:
    known = {(r.get("source") or {}).get("sha256") for r in tr.get("runs") or [] if isinstance(r, dict)}
    problems = []
    pending = ws_paths(root)["pending"]
    for path in sorted(pending.glob("*/judge.json")) if pending.is_dir() else []:
        raw = path.read_bytes()
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            obj = None
        if isinstance(obj, dict) and "stub" in obj:
            continue
        if hashlib.sha256(raw).hexdigest() not in known:
            problems.append(
                f"сирота: выход судьи {_rel(root, path)} не записан ни в один прогон трекера "
                f"(человек {path.parent.name}); запишите его командой append или удалите"
            )
    return problems


def verify(root: Path) -> list[str]:
    """Сквозной аудит: закрыт ли каждый живой прогон до конца. Пусто — всё сходится.

    По каждому живому прогону: покрытие пометками индекса, файл `reviews/<person>/<run_id>.md`
    есть и непуст, `level_computed` посчитан. Плюс сироты: непустой `runs/pending/<person>/judge.json`,
    чей sha256 не записан ни в одном `source` (стабы не в счёт).
    """
    root = Path(root)
    tr = load(root)
    with marks.index_lock(root):
        index = marks.load_index(root)
    reviews = ws_paths(root)["reviews"]
    problems = []
    for run in live_runs(tr):
        rid = run.get("run_id")
        marked, expected = marks.coverage(run, index)
        if marked < expected:
            where = "индекса логов нет" if index is None else "догоните командой mark-logs"
            problems.append(f"{rid}: помечено сессий {marked} из {expected} ({where})")
        review = reviews / str(run.get("person_id")) / f"{rid}.md"
        if not review.is_file():
            problems.append(f"{rid}: нет текста ревью {_rel(root, review)}")
        elif not review.read_text(encoding="utf-8").strip():
            problems.append(f"{rid}: текст ревью пуст ({_rel(root, review)})")
        if run.get("level_computed") is None:
            problems.append(f"{rid}: level_computed не посчитан (команда level {rid})")
    return problems + _orphans(root, tr)
