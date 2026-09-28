"""Проверка выхода коллектора (первая ступень оценки) до того, как пачка поедет судье.

Коллектор видит часть материала, поэтому баллов и вердиктов у него нет: поле оценки в его
выходе означает сломанную ступень, и об этом говорится прямым текстом, а не «лишним полем»
схемы. Дальше — схема `collector` и смысловые сходимости, которые схемой не выражаются
(условие 4 «Проверок скриптом» в `references/output-contract.md`: каждая из восьми осей ровно
в одном из `observations` и `axes_unobserved`; плюс номер пачки не больше `batch.total` и статус).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from lib import i18n, normalize, tracker, workspace
from lib.workspace import SrError

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


# ── Атрибуция цитат ──────────────────────────────────────────────────────────
# Цитата под наблюдением — это слова человека или факт лога (команда и вывод инструмента), и
# стоит она ровно в том ходе, который назван. Текст агента под видом реплики человека и сдвиг
# номера хода судья ловит только в нескольких сверяемых окнах, поэтому сверка идёт здесь, по
# разметке ролей того же нормализатора, которым читал коллектор.

QUOTE_BLOCKS = ("observations", "antipattern_candidates", "gating_candidates", "rec_signals",
                "outcome_signals")
# Склейка дословных отрывков одного хода: каждый отрывок ищется отдельно.
_ELLIPSIS_RE = re.compile(r"\s*(?:…|\.{3}|\[\s*(?:…|\.{3})\s*\])\s*")
_MARKUP_RE = re.compile(r"[*`]")
_QUOTES_RE = re.compile(r"[«»„“”‟\"]")
_DASHES_RE = re.compile(r"[‐‑‒–—―]")
MIN_FRAGMENT = 3


def _norm(text: str) -> str:
    """Форма для сравнения: регистр, кавычки, тире, ё, разметка `*`/бэктики и пробелы не важны."""
    t = _MARKUP_RE.sub("", text or "").casefold().replace("ё", "е")
    t = _DASHES_RE.sub("-", _QUOTES_RE.sub('"', t))
    return re.sub(r"\s+", " ", t).strip()


def _fragments(quote: str) -> list[str]:
    return [f for f in (_norm(p) for p in _ELLIPSIS_RE.split(quote or "")) if len(f) >= MIN_FRAGMENT]


def _turn_sources(turn: dict) -> list[tuple[str, str]]:
    """(вид, текст) источников хода: `human` — реплика человека, `tool` — команда и вывод
    инструмента, `agent` — проза агента, `harness` — вставка харнесса или служебная строка."""
    role, text = turn.get("role"), turn.get("text") or ""
    if role == "user" and not turn.get("synthetic"):
        kind = "human"
    elif role == "assistant":
        kind = "agent"          # в том числе промпт субагента: его пишет агент, а не человек
    else:
        kind = "harness"
    out = [(kind, _norm(text))]
    for tool in turn.get("tools") or []:
        for key in ("command", "output_head"):
            if tool.get(key):
                out.append(("tool", _norm(tool[key])))
    return out


def _found_in(fragments: list[str], turn: dict) -> set[str]:
    """Виды источников хода, в каждом из которых нашлись все отрывки цитаты."""
    return {kind for kind, text in _turn_sources(turn) if all(f in text for f in fragments)}


def _where(fragments: list[str], turns: list[dict]) -> tuple[list[int], list[int], list[tuple[int, str]]]:
    """Где цитата лежит в логе: ходы человека, ходы с прозой агента, вставки харнесса."""
    human, agent, harness = [], [], []
    for t in turns:
        kinds = _found_in(fragments, t)
        if "human" in kinds:
            human.append(t["index"])
        if "agent" in kinds:
            agent.append(t["index"])
        if "harness" in kinds:
            harness.append((t["index"], t.get("synthetic_reason") or t.get("role") or "?"))
    return human, agent, harness


def _turns_list(indexes: list[int]) -> str:
    return ", ".join(f"ход {i}" for i in indexes[:5])


def quote_problem(item: dict, doc: dict) -> str | None:
    """Почему цитата не стоит как реплика человека (или факт инструмента) в названном ходе;
    None — стоит."""
    turn_index = item.get("turn_index")
    sid = item.get("session_id")
    fragments = _fragments(item.get("quote") or "")
    head = f"сессия {sid}, ход {turn_index}"
    if not fragments:
        return f"{head}: цитата пустая — дословного текста нет"
    turns = doc.get("turns") or []
    at = next((t for t in turns if t.get("index") == turn_index), None)
    if at is not None and _found_in(fragments, at) & {"human", "tool"}:
        return None
    human, agent, harness = _where(fragments, turns)
    if at is None:
        lead = f"{head}: хода нет в логе (ходов {len(turns)})"
    elif at.get("role") == "user" and not at.get("synthetic"):
        lead = f"{head}: цитаты нет в этой реплике человека"
    elif at.get("role") == "assistant":
        lead = f"{head}: это ход агента, а цитата под наблюдением — реплика человека или вывод инструмента"
    else:
        lead = f"{head}: это вставка харнесса ({at.get('synthetic_reason') or at.get('role')}), не реплика человека"
    if human:
        return f"{lead}; этот текст — реплика человека: {_turns_list(human)}"
    if agent:
        return f"{lead}; этот текст агента ({_turns_list(agent)}), а не реплика человека — не цитируется"
    if harness:
        where = ", ".join(f"ход {i} ({why})" for i, why in harness[:5])
        return f"{lead}; этот текст — вставка харнесса ({where}), а не реплика человека"
    return f"{lead}; цитата не найдена в логе ни в одном ходе"


def attribution_problems(obj: dict, docs: dict) -> list[str]:
    """Сверка цитат пачки с нормализованными логами. `docs` — {session_id: документ
    `normalize`} или {session_id: строка-причина, почему лог не разобран}.

    Запись без хода (`turn_index: null`) — числовое доказательство из context_report, её сверять
    не с чем. Сессия цитаты без лога — проблема, а не пропуск."""
    problems = []
    for block in QUOTE_BLOCKS:
        items = obj.get(block)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict) or item.get("turn_index") is None or "quote" not in item:
                continue
            sid = item.get("session_id")
            doc = docs.get(sid)
            if doc is None:
                problems.append(f"$.{block}[{i}]: сессия {sid}, ход {item.get('turn_index')}: "
                                f"лога этой сессии нет в sessions[] пачки — цитату не сверить")
            elif isinstance(doc, str):
                problems.append(f"$.{block}[{i}]: сессия {sid}, ход {item.get('turn_index')}: {doc}")
            else:
                why = quote_problem(item, doc)
                if why:
                    problems.append(f"$.{block}[{i}]: {why}")
    return problems


def _log_path(raw: str, state_dir: Path | None) -> tuple[Path | None, str | None]:
    """Путь лога сессии: абсолютный как есть, относительный — от `.sessionretro/` workspace
    (так его пишет сбор). (путь, None) или (None, почему не определить)."""
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path, None
    if state_dir is None:
        return None, (f"путь лога относительный ({raw}), а workspace не найден — цитаты не сверить; "
                      f"запусти из проекта или укажи --workspace")
    return state_dir / path, None


def load_docs(obj: dict, state_dir: Path | None) -> dict:
    """{session_id: документ normalize или строка-причина} для сессий, на которые ссылаются цитаты."""
    wanted = {item.get("session_id")
              for block in QUOTE_BLOCKS if isinstance(obj.get(block), list)
              for item in obj[block]
              if isinstance(item, dict) and item.get("turn_index") is not None}
    docs: dict = {}
    for s in obj.get("sessions") or []:
        if not isinstance(s, dict) or s.get("session_id") not in wanted or s["session_id"] in docs:
            continue
        path, why = _log_path(str(s.get("path") or ""), state_dir)
        if path is None:
            docs[s["session_id"]] = why
        elif not path.is_file():
            docs[s["session_id"]] = f"лога нет по пути {path} (sessions[].path: {s.get('path')}) — цитаты не сверить"
        else:
            try:
                docs[s["session_id"]] = normalize.parse_file(path)
            except SrError as err:
                docs[s["session_id"]] = f"лог не разобран ({path}): {err.message} — цитаты не сверить"
    return docs


def check_batch(obj, state_dir: Path | None = None, *, quotes: bool = False) -> list[str]:
    """Все проблемы одной пачки; пустой список — пачку можно отдавать судье.

    `quotes=True` — ещё и сверка цитат с логами сессий (`attribution_problems`); относительные
    `sessions[].path` берутся от `state_dir` (папка `.sessionretro/`). Без `quotes` проверяются
    только сам выход: схема, поля оценки, сходимости. Нет схемы `collector` → `SrError(1)` из
    `tracker.schema_problems`: это отказ инструмента, а не брак пачки.
    """
    if not isinstance(obj, dict):
        return [f"$: верхний уровень {type(obj).__name__}, а не объект — это не выход коллектора"]
    if "stub" in obj:
        return ["$: стаб не заполнен: коллектор не записал выход"]
    problems = forbidden_keys(obj) + tracker.schema_problems(obj, "collector") + sense_problems(obj)
    if quotes and isinstance(obj.get("sessions"), list):
        problems += attribution_problems(obj, load_docs(obj, state_dir))
    return problems


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


def _state_dir(batch_path: Path, explicit: str | None) -> Path | None:
    """Папка `.sessionretro/` для относительных путей логов: из `--workspace` (неверный →
    `SrError(2)`) или поиском вверх от файла пачки (`<ws>/runs/pending/<person>/batch-NN.json`).
    Не нашлась — None: пачка с относительными путями получит проблему с этой причиной."""
    if explicit is not None:
        return workspace.find_workspace(explicit=explicit) / workspace.STATE_DIR
    try:
        return workspace.find_workspace(start=batch_path.parent) / workspace.STATE_DIR
    except SrError:
        return None


def check_files(paths, workspace_dir: str | None = None) -> tuple[int, list[dict]]:
    """Проверить файлы пачек вместе со сверкой цитат по логам; код 0 все чистые · 1 хоть одна
    кривая · 2 кривых нет, но есть отсутствующие."""
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
        problems = check_batch(obj, _state_dir(path, workspace_dir), quotes=True)
        if problems:
            bad += 1
        results.append({"path": str(raw), "missing": False, **_summary(obj), "problems": problems[:MAX_PROBLEMS],
                        "problems_total": len(problems)})
    code = 1 if bad else (2 if missing else 0)
    return code, results
