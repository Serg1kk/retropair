#!/usr/bin/env python3
"""Генератор фикстур для отчётов: трекер прогонов и три файла ревью.

Во временном workspace (копия `workspace-min` + карточка `kim`) строит свой индекс логов,
собирает девять выходов судьи (три человека `alex`, `sam`, `kim` × три периода `p1`–`p3`)
и прогоняет их настоящим движком: `append` → правка ревьюера → `level` → `retract` и новый
прогон вместо отозванного → `render-review` последнего периода каждого → `approve` двоим из
трёх. На выходе:

    <out>/process-reviews.sample.json
    <out>/reviews/<person>/<run_id>.md   (три файла, один черновик)

Всё время берётся из `now`, пути относительные, тексты синтетические. Тест
`test_assess_samples.py` сверяет файлы в репо с выходом генератора побайтно.

Запуск: python3 make_assess_samples.py --out <папка> [--now 2026-09-01T09:00:00Z]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import _util  # noqa: E402,F401 — кладёт scripts/ в sys.path
from _assess import make_ws, payload  # noqa: E402

from lib import i18n, render_review, tracker  # noqa: E402
from lib.workspace import SrError, write_json_atomic, ws_paths  # noqa: E402

DEFAULT_NOW = datetime(2026, 9, 1, 9, 0, 0, tzinfo=timezone.utc)
PEOPLE = ("alex", "sam", "kim")
LABELS = ("p1", "p2", "p3")
TRACKER_OUT = "process-reviews.sample.json"
UUID_NS = uuid.UUID("6f1c2d3e-4a5b-4c6d-8e7f-9a0b1c2d3e4f")

# --- тексты по осям: цитаты по силе (слабо, средне, сильно), пункт действия, критерии ----

AX = {
    "verification": {
        "quotes": ("ок, мержим", "прогони тесты и покажи вывод",
                   "покажи дифф по каждому файлу, прежде чем коммитить"),
        "rec": "Перед приёмкой диффа запускать тесты и открывать хотя бы один изменённый файл.",
        "moment": "ближе к концу",
        "cost": "изменения ушли в ветку без запуска тестов, и ошибка всплыла бы уже при сборке",
        "try": "перед приёмкой запускать тесты и открывать хотя бы один изменённый файл",
        "levers": ("попросить агента показать команду проверки и её вывод",
                   "держать короткий список того, что смотреть в диффе"),
        "strong": "Изменения принимаются после запуска тестов и просмотра диффа.",
        "weak": "В одной из сессий изменения приняты без запуска тестов.",
        "absent": "Приёмки изменений в разобранных логах не было.",
    },
    "task_framing": {
        "quotes": ("сделай отчёт", "нужно поправить сортировку, остальное не трогай",
                   "цель: отчёт за неделю в CSV, без новых зависимостей, готово, когда тест зелёный"),
        "rec": "В первом сообщении называть цель, границы и признак готовности.",
        "moment": "первое сообщение",
        "cost": "агент начал с догадки о цели, и первые ходы ушли на уточнения",
        "try": "в первом сообщении называть цель, границы и признак готовности",
        "levers": ("дописывать в запрос, чего трогать нельзя",
                   "заканчивать запрос строкой о том, как понять, что готово"),
        "strong": "Цель и границы задачи названы в первом сообщении.",
        "weak": "В одной сессии цель задачи прояснялась по ходу работы.",
        "absent": "Постановки новой задачи в разобранных логах не было.",
    },
    "ownership": {
        "quotes": ("делай как считаешь нужным", "давай второй вариант",
                   "выбираю второй вариант, он проще откатить"),
        "rec": "Выбор между вариантами решения делать самому и записывать причину одной строкой.",
        "moment": "момент выбора структуры",
        "cost": "структуру данных выбрал агент, и её пришлось переделывать при следующей правке",
        "try": "на вопрос агента о выборе отвечать своим решением и одной строкой причины",
        "levers": ("просить агента сравнить два варианта по одному признаку",
                   "записывать принятое решение в описание задачи"),
        "strong": "Решения по структуре приняты и объяснены самостоятельно.",
        "weak": "Выбор структуры в одной сессии отдан агенту без обсуждения.",
        "absent": "Развилок, где нужно было выбирать, в логах не видно.",
    },
    "self_correction": {
        "quotes": ("давай попробуем ещё раз так же", "этот путь не работает, вернёмся к исходной версии",
                   "стоп, откатываем, пробуем через адаптер"),
        "rec": "После второй неудачи одним способом останавливаться и менять подход.",
        "moment": "после второй неудачной попытки",
        "cost": "третья попытка тем же способом заняла ещё несколько ходов без результата",
        "try": "после второй неудачи останавливаться и формулировать другой подход",
        "levers": ("просить агента назвать причину неудачи до новой попытки",
                   "откатываться к последнему рабочему состоянию перед сменой подхода"),
        "strong": "После неудачи подход сменён без лишних повторов.",
        "weak": "Одна и та же попытка повторена несколько раз подряд.",
        "absent": "Неудач, после которых стоило менять подход, в логах не было.",
    },
    "context_engineering": {
        "quotes": ("вот весь лог CI, разберись", "смотри только файл с обработчиком",
                   "начнём новую сессию, контекст перегружен"),
        "rec": "Давать агенту выдержку из лога: упавший шаг и первую ошибку.",
        "moment": "начало разбора",
        "cost": "агент потратил несколько ходов на шум из лога, прежде чем дошёл до упавшего шага",
        "try": "вырезать из лога упавший шаг и первую ошибку и давать агенту только их",
        "levers": ("попросить агента сначала назвать, какой шаг упал",
                   "начинать новую сессию, когда разговор ушёл в сторону"),
        "strong": "Агенту подаются только нужные файлы и выдержки.",
        "weak": "Лог подан агенту целиком без отбора.",
        "absent": "Работы с контекстом в разобранных логах не видно.",
    },
    "decomposition": {
        "quotes": ("сначала сделай всё целиком, потом посмотрим", "сначала один тест, потом остальные",
                   "разбей на три шага и показывай после каждого"),
        "rec": "Делить работу на шаги, каждый из которых можно проверить отдельно.",
        "moment": "начало работы",
        "cost": "правка легла сразу в несколько слоёв, и место ошибки пришлось искать по всему диффу",
        "try": "просить агента сделать первый шаг и показать результат до следующего",
        "levers": ("просить план из нескольких шагов до начала правок",
                   "проверять результат после каждого шага"),
        "strong": "Работа идёт небольшими шагами с проверкой после каждого.",
        "weak": "Одна крупная задача попрошена целиком одним шагом.",
        "absent": "Задач, которые стоило делить, в логах не было.",
    },
    "trust_calibration": {
        "quotes": ("да, применяй всё", "примени первую правку, остальные покажи",
                   "проверь по документации, что этот флаг есть"),
        "rec": "Принимать правку по частям и смотреть результат после первой.",
        "moment": "момент приёмки правок",
        "cost": "правка легла сразу в три места, и откатывать её пришлось целиком",
        "try": "принимать правку по частям и смотреть результат после первой",
        "levers": ("просить агента показать источник утверждения",
                   "проверять одно утверждение агента выборочно"),
        "strong": "Предложения агента принимаются после выборочной проверки.",
        "weak": "Предложение агента принято целиком без выборочной проверки.",
        "absent": "Предложений агента, которые стоило проверять, в логах не было.",
    },
    "reversibility": {
        "quotes": ("удали старые файлы, они не нужны", "закоммить текущее перед рефакторингом",
                   "сделай отдельную ветку под эксперимент"),
        "rec": "Перед рискованным шагом делать коммит или отдельную ветку.",
        "moment": "перед рискованной правкой",
        "cost": "после неудачной правки вернуть рабочее состояние было не к чему",
        "try": "перед рискованным шагом делать коммит или отдельную ветку",
        "levers": ("просить агента назвать, как откатить шаг",
                   "держать эксперименты в отдельной ветке"),
        "strong": "Перед рискованными шагами есть точка отката.",
        "weak": "Одна рискованная правка сделана без точки отката.",
        "absent": "Рискованных шагов в разобранных логах не было.",
    },
}

# --- сессии: (slug, тема, день от начала периода, час, ходов, реплик человека, статус) ----

SESSIONS = {
    ("alex", "p1"): [("refactor-api", "рефакторинг API", 0, 11, 18, 7, "reviewed"),
                     ("login-form", "форма входа", 1, 9, 24, 9, "reviewed"),
                     ("quick-question", "короткий вопрос", 3, 16, 4, 2, "transcript_too_short")],
    ("alex", "p2"): [("fix-tests", "починка тестов", 1, 14, 16, 6, "reviewed"),
                     ("cache-layer", "слой кэша", 3, 10, 22, 8, "reviewed")],
    ("alex", "p3"): [("export-csv", "выгрузка в CSV", 0, 10, 20, 8, "reviewed"),
                     ("search-index", "поисковый индекс", 2, 15, 26, 10, "reviewed")],
    ("sam", "p1"): [("dashboard", "дашборд", 1, 13, 30, 11, "reviewed"),
                    ("chart-legend", "легенда графика", 4, 10, 14, 5, "reviewed")],
    ("sam", "p2"): [("date-picker", "выбор даты", 2, 11, 18, 6, "reviewed")],
    ("sam", "p3"): [("table-sort", "сортировка таблицы", 1, 9, 20, 7, "reviewed"),
                    ("theme-switch", "переключатель темы", 3, 14, 16, 6, "reviewed")],
    ("kim", "p1"): [("airflow-dag", "граф задач", 2, 10, 12, 4, "reviewed")],
    ("kim", "p2"): [("etl-retry", "повторы в загрузке", 1, 12, 22, 8, "reviewed"),
                    ("schema-check", "проверка схемы", 4, 15, 18, 7, "reviewed")],
    ("kim", "p3"): [("backfill", "дозагрузка за месяц", 0, 9, 24, 9, "reviewed"),
                    ("dq-tests", "тесты качества данных", 2, 13, 20, 8, "reviewed")],
}
SHORT_NOTE = "два хода, материала для оценки нет"

# Оси по порядку: verification, task_framing, ownership, self_correction, context_engineering,
# decomposition, trust_calibration, reversibility. Строка — причина «данных нет».
RUNS = [
    {"person": "alex", "label": "p1", "file": "judge-p1.json",
     "scores": [2, 3, 2, 3, "short_log", 2, 2, 3], "level_judge": "competent",
     "recs": ["verification", "trust_calibration"], "levers": {"verification": 2}, "prev": [],
     "antipattern": ("tests_instead_of_eyes", "verification"),
     "opening": "В разбор подано три лога, это три разных разговора; разобраны два, третий оказался слишком коротким.",
     "closing": None},
    {"person": "sam", "label": "p1", "file": "judge-p1.json",
     "scores": [1, 2, 2, "behaviour_absent", 1, 2, 1, 2], "level_judge": "naive",
     "recs": ["verification"], "levers": {}, "prev": [], "gate": ["G1"],
     "antipattern": ("green_report_taken", "verification"),
     "opening": "В разбор подано два лога, это два разных разговора, оба разобраны целиком.",
     "closing": None},
    {"person": "kim", "label": "p1", "file": "judge-p1.json",
     "scores": [2, 2, "short_log", "behaviour_absent", "tool_has_no_trace", "short_log",
                "work_outside_transcript", "nothing_to_verify"], "level_judge": "insufficient",
     "recs": ["verification"], "levers": {"verification": 1}, "prev": [], "task_solved": "unclear",
     "opening": "В разбор подан один лог, он же один разговор, и он разобран целиком. "
                "Коротко: лог короткий, и по большинству сторон работы выводов пока нет.",
     "closing": None},
    {"person": "alex", "label": "p2", "file": "judge-p2.json",
     "scores": [3, 3, 3, "behaviour_absent", 2, 3, 2, 3], "level_judge": "confident_operator",
     "recs": ["context_engineering"], "levers": {"context_engineering": 1},
     "prev": [(1, "followed"), (2, "not_observable")],
     "override": {"axes": {"trust_calibration": 3},
                  "why": "во второй сессии правки приняты по частям, этот фрагмент не попал в пачку"},
     "opening": "В разбор подано два лога, это два разных разговора, оба разобраны целиком.",
     "closing": "Прошлый совет про тесты перед приёмкой виден в работе."},
    {"person": "sam", "label": "p2", "file": "judge-p2.json",
     "scores": [2, 3, 2, 2, 2, 3, 2, 3], "level_judge": "competent",
     "recs": ["trust_calibration"], "levers": {}, "prev": [(1, "partial")],
     "opening": "В разбор подан один лог, он же один разговор, и он разобран целиком.",
     "closing": None},
    {"person": "kim", "label": "p2", "file": "judge-p2-first.json",
     "scores": [2, 3, 2, 2, 3, 2, 2, 2], "level_judge": "competent",
     "recs": ["decomposition"], "levers": {"decomposition": 1}, "prev": [(1, "followed")],
     "retract": "в пачку попала только первая половина второй сессии, прогон пересобран",
     "opening": "В разбор подано два лога, это два разных разговора, оба разобраны целиком. "
                "Коротко: задачи поставлены ясно, проверка результата пока через раз.",
     "closing": None},
    {"person": "kim", "label": "p2", "file": "judge-p2.json",
     "scores": [2, 3, 2, 2, 3, 2, 2, 2], "level_judge": "developing",
     "recs": ["trust_calibration"], "levers": {"trust_calibration": 1}, "prev": [(1, "partial")],
     "shift_hours": 2, "gate": ["G2", "G3"], "flag": ("secret_in_the_open", "токен доступа вставлен в запрос открытым текстом"),
     "opening": "В разбор подано два лога, это два разных разговора, оба разобраны целиком. "
                "Коротко: задачи поставлены ясно, а решения и предупреждения агента чаще проходили без внимания.",
     "closing": None},
    {"person": "alex", "label": "p3", "file": "judge-p3.json",
     "scores": [4, 4, 4, 3, 3, 3, 4, 4], "level_judge": "orchestrator",
     "recs": ["decomposition"], "levers": {"decomposition": 2}, "prev": [(2, "followed"), (3, "partial")],
     "opening": "В разбор подано два лога, это два разных разговора, оба разобраны целиком.",
     "closing": "Прошлый совет про приёмку правок по частям виден в работе."},
    {"person": "sam", "label": "p3", "file": "judge-p3.json",
     "scores": [3, 3, 2, 3, 3, 3, 2, 3], "level_judge": "confident_operator",
     "recs": ["ownership"], "levers": {}, "prev": [(1, "followed"), (2, "ignored")],
     "opening": "В разбор подано два лога, это два разных разговора, оба разобраны целиком.",
     "closing": "Прошлый совет про тесты перед приёмкой виден в работе."},
    {"person": "kim", "label": "p3", "file": "judge-p3.json",
     "scores": [2, 3, 2, 3, 2, 2, 3, 2], "level_judge": "competent",
     "recs": ["ownership"], "levers": {"ownership": 1}, "prev": [(1, "ignored"), (3, "followed")],
     "opening": "В разбор подано два лога, это два разных разговора, оба разобраны целиком. "
                "Коротко: правки теперь принимаются по частям, выбор решений пока за агентом.",
     "closing": "Прошлый совет про приёмку правок по частям виден в работе."},
]
APPROVE = ("alex", "kim")  # sam остаётся черновиком
VERDICT_BAND = {"followed": 2, "partial": 1, "ignored": 0}
NOT_OBSERVABLE_NOTE = "подходящей ситуации в сессиях не было"
GATE_QUOTES = {"G1": "да, применяй всё, там всё равно всё правильно", "G2": "на твоё усмотрение",
               "G3": "предупреждение не важно, продолжай"}
PLACE = {1: "first_place_above", 2: "second_place_above", 3: "third_place_above"}


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def period_start(now: datetime, label: str) -> datetime:
    return now + timedelta(days=7 * LABELS.index(label))


def append_time(now: datetime, person: str, label: str) -> datetime:
    return period_start(now, label) + timedelta(days=6, hours=9, minutes=10 * PEOPLE.index(person))


# --- индекс логов ------------------------------------------------------------------------


def session_rows(now: datetime, person: str, label: str) -> list[dict]:
    rows = []
    for slug, topic, day, hour, turns, user_turns, status in SESSIONS[(person, label)]:
        start = period_start(now, label).replace(hour=hour, minute=0, second=0) + timedelta(days=day)
        key = f"{start:%Y-%m-%d_%H-%M-%S}Z-{slug}"
        rows.append({
            "slug": slug, "topic": topic, "status": status, "key": key,
            "session_id": str(uuid.uuid5(UUID_NS, f"{person}/{label}/{slug}")),
            "path": f"logs/{person}/{key}.md", "start": start,
            "end": start + timedelta(minutes=5 * turns), "turns": turns, "user_turns": user_turns,
        })
    return rows


def _metrics(turns: int, user_turns: int) -> dict:
    """Механические метрики записи SpecStory в формате сбора логов (без таймстемпов и инструментов)."""
    return {
        "timestamps_available": False, "tool_calls_available": False, "commands_available": False,
        "turns_total": turns, "user_turns": user_turns, "synthetic_turns": 0, "sidechain_turns": 0,
        "tool_calls": 0, "shell_tool_calls": None, "write_tool_calls": None,
        "verification_commands": None, "verification_by_kind": None, "verification_examples": [],
        "approvals": 0, "instant_approvals": None, "speed_accept_count": None, "interrupts": 0,
        "repeated_prompts": 0, "session_span_seconds": None, "unparsed_lines": None,
        "unknown_types": None, "context_peak_tokens": None, "cache_share": None, "compactions": None,
        "subagent_files": None,
    }


def index_entry(person: str, label: str, row: dict) -> dict:
    """Запись индекса в формате `inventory.merge` для новой сессии (без полей исключения)."""
    return {
        "person_id": person,
        "source": {"type": "specstory", "id": "specstory:0"},
        "session_id": row["session_id"],
        "key": row["key"],
        "path": row["path"],
        "source_path": f"/home/user/demo/.specstory/history/{row['key']}.md",
        "format": "specstory",
        "sha": hashlib.sha256(row["key"].encode("utf-8")).hexdigest(),
        "bytes": 700 * row["turns"],
        "turns_total": row["turns"],
        "user_turns": row["user_turns"],
        "log_started_at": iso(row["start"]),
        "log_ended_at": iso(row["end"]),
        "started_at_source": "log_header",
        "collector": "claude-code",
        "models": ["sonnet"],
        "period_guess": label,
        "period_guess_source": "date_window",
        "metrics": _metrics(row["turns"], row["user_turns"]),
        "sanitize": {"rules_hit": {}, "renamed": 0},
        "reviewed": False, "reviewed_in_run": None, "reviewed_at": None, "reviewed_turns_total": None,
        "reviewed_bytes": None,
        "state": "new",
    }


def build_index(now: datetime) -> dict:
    logs = [index_entry(person, label, row)
            for label in LABELS for person in PEOPLE for row in session_rows(now, person, label)]
    return {"schema_version": 1, "workspace": "demo-team", "updated_at": iso(now), "logs": logs,
            "warnings": []}


# --- выход судьи ---------------------------------------------------------------------------


def _band(score: int) -> int:
    return 0 if score <= 2 else 1 if score == 3 else 2


def _verdict(axis: str, score, rec_axes: list[str]) -> str:
    if axis in rec_axes:
        return PLACE[rec_axes.index(axis) + 1]
    if isinstance(score, str):
        return "not_visible"
    return "even" if score >= 3 else "even_but_one"


def judge_payload(spec: dict, rows: list[dict], first_rec: int) -> dict:
    """Выход судьи по спецификации прогона; REC-id начинаются с `first_rec`."""
    person = spec["person"]
    axis_ids = i18n.keys("axes")
    reviewed = [r for r in rows if r["status"] == "reviewed"]
    scores = dict(zip(axis_ids, spec["scores"]))
    rec_axes = spec["recs"]

    def turn(row: dict, i: int) -> int:
        return min(2 * i + 1, row["turns"] - 1)

    axes = []
    for i, axis in enumerate(axis_ids):
        score, row = scores[axis], reviewed[i % len(reviewed)]
        if isinstance(score, str):
            axes.append({"id": axis, "score": None, "insufficient_evidence": True,
                         "insufficient_evidence_reason": score, "evidence": [], "subcriteria": [],
                         "note": "материала по этой стороне работы нет"})
            continue
        band = _band(score)
        evidence = [{"session_id": row["session_id"], "turn_index": turn(row, i),
                     "quote": AX[axis]["quotes"][band], "reading": AX[axis]["strong" if band else "weak"]}]
        if axis in rec_axes and band:
            evidence.append({"session_id": row["session_id"], "turn_index": turn(row, i) + 1,
                             "quote": AX[axis]["quotes"][0], "reading": AX[axis]["weak"]})
        axes.append({"id": axis, "score": score, "insufficient_evidence": False,
                     "insufficient_evidence_reason": None, "evidence": evidence, "subcriteria": [],
                     "note": AX[axis]["strong" if band else "weak"]})

    recs, actions, practices = [], [], []
    for n, axis in enumerate(rec_axes):
        row = reviewed[axis_ids.index(axis) % len(reviewed)]
        levers = list(AX[axis]["levers"][:spec["levers"].get(axis, 0)])
        recs.append({"id": f"REC-{person}-{first_rec + n:03d}", "axis": axis, "text": AX[axis]["rec"],
                     "status": "open"})
        actions.append({"place": f"{row['topic']}, {AX[axis]['moment']}", "date": f"{row['start']:%Y-%m-%d}",
                        "quote": AX[axis]["quotes"][0], "cost": AX[axis]["cost"], "try": AX[axis]["try"],
                        "levers": levers})
        if levers:
            practices.append({"axis": axis,
                              "anchor": {"session_id": row["session_id"],
                                         "turn_index": turn(row, axis_ids.index(axis)),
                                         "quote": AX[axis]["quotes"][0]},
                              "practices": levers})

    prev = []
    for number, verdict in spec["prev"]:
        rid = f"REC-{person}-{number:03d}"
        if verdict == "not_observable":
            prev.append({"id": rid, "verdict": verdict, "evidence": None, "note": NOT_OBSERVABLE_NOTE})
            continue
        axis = spec["prev_axes"][rid]
        prev.append({"id": rid, "verdict": verdict,
                     "evidence": {"session_id": reviewed[0]["session_id"], "turn_index": 3,
                                  "quote": AX[axis]["quotes"][VERDICT_BAND[verdict]]},
                     "note": None})

    criteria = [{"axis": axis, "verdict": _verdict(axis, scores[axis], rec_axes),
                 "evidence": AX[axis]["absent" if isinstance(scores[axis], str)
                                      else "strong" if scores[axis] >= 3 and axis not in rec_axes
                                      else "weak"]}
                for axis in axis_ids]
    scored = {a: s for a, s in scores.items() if not isinstance(s, str)}
    gate = spec.get("gate") or []
    gate_rows = [{"trigger": trig, "session_id": reviewed[0]["session_id"], "turn_index": 5 + k,
                  "quote": GATE_QUOTES[trig]}
                 for k, trig in enumerate(gate)]
    antipatterns = []
    if spec.get("antipattern"):
        ap_id, ap_axis = spec["antipattern"]
        row = reviewed[0]
        antipatterns.append({"id": ap_id, "found": True, "axis": ap_axis, "session_id": row["session_id"],
                             "turn_index": turn(row, 0), "quote": AX[ap_axis]["quotes"][0],
                             "note": AX[ap_axis]["weak"]})
    flags = []
    if spec.get("flag"):
        flags.append({"id": spec["flag"][0], "session_id": reviewed[-1]["session_id"], "turn_index": 4,
                      "note": spec["flag"][1]})

    excluded = [r for r in rows if r["status"] != "reviewed"]
    data = {
        "session_scope": {
            "sessions": [{"session_id": r["session_id"], "path": r["path"], "turns_total": r["turns"],
                          "reviewed_from_turn": None, "state": "new", "status": r["status"],
                          "status_note": SHORT_NOTE if r["status"] != "reviewed" else None}
                         for r in rows],
            "agent_stack": ["claude-code", "sonnet"],
        },
        "axes": axes,
        "antipatterns": antipatterns,
        "flags": flags,
        "gating_triggered": bool(gate),
        "gating_evidence": gate_rows,
        "summary": {
            "level_judge": spec["level_judge"],
            "strongest_axis": max(scored, key=lambda a: (scored[a], -axis_ids.index(a))),
            "weakest_axis": min(scored, key=lambda a: (scored[a], axis_ids.index(a))),
            "one_thing_to_change": AX[rec_axes[0]]["try"],
            "task_solved": spec.get("task_solved", "yes"),
        },
        "recommendations": recs,
        "best_practices": practices,
        "prev_recommendations": prev,
        "feedback_parts": {"opening": spec["opening"], "actions": actions, "criteria": criteria,
                           "closing": spec["closing"]},
        "artifact": None,
    }
    if not practices:
        del data["best_practices"]  # поле необязательное: слабых осей с приёмами нет
    obj = payload()
    obj["data"] = data
    obj["metadata"] = {
        "turns_analyzed": sum(r["turns"] for r in reviewed),
        "axes_scored": len(scored),
        "axes_insufficient": len(axis_ids) - len(scored),
        "metrics_available": True,
        "context_report_available": False,
        "prompt_version": "v13",
        "collector_version": "v3",
        "batches": 1,
        "sessions_total": len(rows),
        "warnings": ["sessions_excluded"] if excluded else [],
    }
    return obj


# --- сборка --------------------------------------------------------------------------------


def _write_judge(root: Path, person: str, name: str, obj: dict) -> Path:
    path = ws_paths(root)["pending"] / person / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _clear_old_reviews(out_dir: Path) -> None:
    for person in PEOPLE:
        folder = out_dir / "reviews" / person
        if folder.is_dir():
            for old in folder.glob("*.md"):
                old.unlink()


def build(out_dir: Path, now: datetime) -> dict:
    """Собрать трекер и три файла ревью в `out_dir`; вернуть `{tracker, reviews, runs, live}`."""
    if now.tzinfo is None:
        raise SrError(f"now без часового пояса ({now!r}): время фикстур только в UTC", 2)
    out_dir = Path(out_dir)
    with tempfile.TemporaryDirectory(prefix="sr-samples-") as tmp:
        root = make_ws(Path(tmp))
        paths = ws_paths(root)
        write_json_atomic(paths["logs_index"], build_index(now))

        issued: dict[str, dict[str, str]] = {p: {} for p in PEOPLE}  # REC-id → ось
        last: dict[str, tuple[str, Path]] = {}
        for spec in RUNS:
            person, label = spec["person"], spec["label"]
            moment = append_time(now, person, label) + timedelta(hours=spec.get("shift_hours", 0))
            first_rec = tracker.next_rec_number(tracker.load(root), person)
            obj = judge_payload(dict(spec, prev_axes=issued[person]), session_rows(now, person, label), first_rec)
            judge = _write_judge(root, person, spec["file"], obj)
            result = tracker.append(root, judge, person, period_label=label, now=moment)
            run_id = result["run_id"]
            for rec in obj["data"]["recommendations"]:
                issued[person][rec["id"]] = rec["axis"]
            if spec.get("override"):
                moment += timedelta(minutes=30)
                override = dict(spec["override"], by="reviewer", at=iso(moment))
                tracker.update_run(root, run_id, now=moment, operator_override=override)
            tracker.set_level(root, run_id, now=moment)
            if spec.get("retract"):
                tracker.retract(root, run_id, spec["retract"], now=moment + timedelta(hours=1))
                continue
            last[person] = (run_id, judge)

        reviews = []
        for person in PEOPLE:
            run_id, judge = last[person]
            moment = append_time(now, person, "p3") + timedelta(hours=1)
            reviews.append(render_review.write_review(root, judge, person, run_id, now=moment))
            if person in APPROVE:
                render_review.approve(root, person, run_id, now=moment + timedelta(hours=1))

        problems = tracker.validate_file(root)
        if problems:
            raise SrError("фикстура трекера не прошла validate:\n  " + "\n  ".join(problems), 1)

        out_dir.mkdir(parents=True, exist_ok=True)
        _clear_old_reviews(out_dir)
        tracker_out = out_dir / TRACKER_OUT
        shutil.copyfile(tracker.tracker_path(root), tracker_out)
        written = []
        for path in reviews:
            target = out_dir / "reviews" / path.parent.name / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            written.append(target)
        doc = tracker.load(root)
    return {"tracker": tracker_out, "reviews": written, "runs": len(doc["runs"]),
            "live": len(tracker.live_runs(doc))}


def _parse_now(value: str) -> datetime:
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"--now {value!r}: нужен вид 2026-09-01T09:00:00Z") from err
    return moment.replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Фикстуры трекера и ревью для отчётов.")
    parser.add_argument("--out", required=True, type=Path, help="папка для трекера и reviews/")
    parser.add_argument("--now", type=_parse_now, default=DEFAULT_NOW,
                        help=f"начало первого периода, UTC (по умолчанию {iso(DEFAULT_NOW)})")
    args = parser.parse_args(argv)
    try:
        result = build(args.out, args.now)
    except SrError as err:
        print(f"make_assess_samples: {err.message}", file=sys.stderr)
        return err.code
    print(json.dumps({"tracker": result["tracker"].name, "reviews": [p.name for p in result["reviews"]],
                      "runs": result["runs"], "live": result["live"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
