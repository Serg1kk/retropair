"""Формула уровня прогона: медиана оценённых осей с удвоенной `verification` и потолок гейта.

`scored` — баллы осей с учётом правки ревьюера (`operator_override.axes`), оси без данных
(`insufficient_evidence`) не входят; `values = scored + [verification]`, если верификация оценена.
Пороги медианы: `orchestrator ≥ 3.5 · confident_operator ≥ 2.75 · competent ≥ 2.0 · naive ≥ 1.0 ·
passenger`. Меньше трёх оценённых осей → `insufficient`. Потолок гейта считается по числу РАЗНЫХ
триггеров из `gating_evidence`: один → не выше `competent`, два и больше → не выше `developing`
(`passenger` и `naive` под потолком остаются как есть).
"""

from __future__ import annotations

from statistics import median

from lib.workspace import SrError

GATE_CEILING = {
    1: {"passenger", "naive", "competent", "developing"},
    2: {"passenger", "naive", "developing"},  # 2 = «два и больше»
}
GATE_CAP = {1: "competent", 2: "developing"}
THRESHOLDS = ((3.5, "orchestrator"), (2.75, "confident_operator"), (2.0, "competent"), (1.0, "naive"))
MIN_SCORED = 3
DOUBLED_AXIS = "verification"


def level_from_values(values: list[float], scored_count: int, triggers: int) -> str:
    """Уровень по значениям медианы, числу оценённых осей и числу разных триггеров гейта."""
    if triggers < 0 or scored_count < 0:
        raise SrError(f"уровень: отрицательные счётчики (осей {scored_count}, триггеров {triggers})", 2)
    if scored_count < MIN_SCORED:
        return "insufficient"
    if not values:
        raise SrError(f"уровень: оценённых осей {scored_count}, а значений для медианы нет", 2)
    m = median(values)
    result = "passenger"
    for bound, name in THRESHOLDS:
        if m >= bound:
            result = name
            break
    if triggers:
        gate = min(triggers, 2)
        if result not in GATE_CEILING[gate]:
            result = GATE_CAP[gate]
    return result


def effective_scores(run: dict) -> dict[str, int | None]:
    """Ось → балл с учётом `operator_override.axes`; ось без данных → `None`. Порядок — как в `axes`.

    Правка ревьюера по оси, которой нет в прогоне, → `SrError(1)`.
    """
    scores: dict[str, int | None] = {}
    for axis in run.get("axes") or []:
        axis_id = axis.get("id")
        score = axis.get("score")
        scores[axis_id] = None if axis.get("insufficient_evidence") or score is None else score
    override = (run.get("operator_override") or {}).get("axes") or {}
    for axis_id, score in override.items():
        if axis_id not in scores:
            raise SrError(
                f"правка ревьюера по неизвестной оси {axis_id!r} (прогон {run.get('run_id')}); "
                f"оси прогона: {', '.join(map(str, scores))}",
                1,
            )
        scores[axis_id] = score
    return scores


def gate_triggers(run: dict) -> int:
    """Число разных триггеров гейта в `gating_evidence`."""
    return len({e.get("trigger") for e in run.get("gating_evidence") or [] if e.get("trigger")})


def compute(run: dict) -> dict:
    """`{level, median, values}` прогона; `median` — `None`, если оценённых осей нет."""
    scores = effective_scores(run)
    scored = [s for s in scores.values() if s is not None]
    values = list(scored)
    if scores.get(DOUBLED_AXIS) is not None:
        values.append(scores[DOUBLED_AXIS])
    return {
        "level": level_from_values(values, len(scored), gate_triggers(run)),
        "median": median(values) if values else None,
        "values": values,
    }
