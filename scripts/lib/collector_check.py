"""Проверка выхода коллектора (первая ступень оценки) до того, как пачка поедет судье.

Коллектор видит часть материала, поэтому баллов и вердиктов у него нет: поле оценки в его
выходе означает сломанную ступень, и об этом говорится прямым текстом, а не «лишним полем»
схемы. Дальше — схема `collector` и смысловые сходимости, которые схемой не выражаются
(условие 4 «Проверок скриптом» в `references/output-contract.md`: каждая из восьми осей ровно
в одном из `observations` и `axes_unobserved`; плюс номер пачки не больше `batch.total` и статус).
"""

from __future__ import annotations

import json
from pathlib import Path

from lib import i18n, tracker

FORBIDDEN = {
    "score", "scores", "axes", "overall_level", "level", "feedback_draft", "feedback_parts",
    "recommendations", "insufficient_evidence", "gating_triggered", "verdict",
}
MAX_PROBLEMS = 25

_BLOCK_TYPES = (
    ("observations", list), ("axes_unobserved", list), ("sessions", list),
    ("batch", dict), ("status", dict), ("subcriteria_unobserved", list),
)


def forbidden_keys(node, path: str = "$") -> list[str]:
    """Поля оценки на любой глубине выхода коллектора."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FORBIDDEN:
                found.append(f"{path}.{key}: первая ступень баллов и вердиктов не ставит — "
                             f"поле {key!r} здесь означает, что коллектор оценивал")
            found += forbidden_keys(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            found += forbidden_keys(item, f"{path}[{i}]")
    return found


def sense_problems(obj: dict) -> list[str]:
    """Смысловые противоречия пачки: схемой они не выражаются, а сведение ломают."""
    problems = []
    for name, kind in _BLOCK_TYPES:
        value = obj.get(name)
        if value is not None and not isinstance(value, kind):
            problems.append(f"$.{name}: {type(value).__name__}, а ожидается "
                            f"{'массив' if kind is list else 'объект'}")
    if problems:
        return problems
    observations = obj.get("observations") or []
    for i, item in enumerate(observations):
        if isinstance(item, dict) and "session_id" in item and not item.get("session_id"):
            problems.append(f"$.observations[{i}]: наблюдение без session_id — "
                            f"номер хода без сессии ничего не адресует")
    obs_axes = {o.get("axis") for o in observations if isinstance(o, dict)}
    unobserved = [u.get("axis") for u in (obj.get("axes_unobserved") or []) if isinstance(u, dict)]
    missing = [a for a in i18n.keys("axes") if a not in obs_axes and a not in unobserved]
    if missing:
        problems.append("$.axes_unobserved: оси не покрыты ни наблюдениями, ни отметкой "
                        f"о ненаблюдаемости: {', '.join(missing)}")
    both = sorted(obs_axes & set(unobserved))
    if both:
        problems.append(f"$.axes_unobserved: ось одновременно в наблюдениях и в ненаблюдаемых: {', '.join(both)}")
    twice = sorted({a for a in unobserved if unobserved.count(a) > 1})
    if twice:
        problems.append(f"$.axes_unobserved: ось перечислена дважды: {', '.join(twice)}")
    batch = obj.get("batch") or {}
    index, total = batch.get("index"), batch.get("total")
    if isinstance(index, int) and isinstance(total, int) and index > total:
        problems.append(f"$.batch: index {index} больше total {total}")
    status = obj.get("status") or {}
    if "code" in status and "ok" in status and bool(status["ok"]) != (status["code"] == "success"):
        problems.append(f"$.status: ok={status['ok']} не сходится с code={status['code']!r}")
    return problems


def check_batch(obj) -> list[str]:
    """Все проблемы одной пачки; пустой список — пачку можно отдавать судье.

    Нет схемы `collector` → `SrError(1)` из `tracker.schema_problems`: это отказ инструмента,
    а не брак пачки.
    """
    if not isinstance(obj, dict):
        return [f"$: верхний уровень {type(obj).__name__}, а не объект — это не выход коллектора"]
    if "stub" in obj:
        return ["$: стаб не заполнен: коллектор не записал выход"]
    return forbidden_keys(obj) + tracker.schema_problems(obj, "collector") + sense_problems(obj)


def _summary(obj) -> dict:
    if not isinstance(obj, dict):
        return {}
    batch = obj.get("batch") if isinstance(obj.get("batch"), dict) else {}
    sessions = obj.get("sessions")
    observations = obj.get("observations")
    return {
        "index": batch.get("index"),
        "total": batch.get("total"),
        "sessions": len(sessions) if isinstance(sessions, list) else None,
        "observations": len(observations) if isinstance(observations, list) else None,
    }


def check_files(paths) -> tuple[int, list[dict]]:
    """Проверить файлы пачек; код 0 все чистые · 1 хоть одна кривая · 2 кривых нет, но есть отсутствующие."""
    results: list[dict] = []
    bad = missing = 0
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            results.append({"path": str(raw), "missing": True, "problems": ["файла нет"], "problems_total": 1})
            missing += 1
            continue
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as err:
            results.append({"path": str(raw), "missing": False, "problems": [f"не разбирается: {err}"],
                            "problems_total": 1})
            bad += 1
            continue
        problems = check_batch(obj)
        if problems:
            bad += 1
        results.append({"path": str(raw), "missing": False, **_summary(obj), "problems": problems[:MAX_PROBLEMS],
                        "problems_total": len(problems)})
    code = 1 if bad else (2 if missing else 0)
    return code, results
