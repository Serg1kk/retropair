"""Нарезка сессий человека на пачки коллектора.

Пачка — разбиение, а не выборка: оцениваются все годные сессии, просто разными агентами.
Сессии идут по времени начала и набираются подряд, пока не упрутся в один из двух бюджетов
(`max_bytes` человеческого слоя, `max_user_turns` реплик). Сессия между пачками не делится;
сессия тяжелее бюджета идёт в пачку одна с `batch_truncated: true` (коллектор читает её срезами).
Короче `min_turns` реплик — в список `short`; чужой период — в `skipped` с `other_period`.
У `grown` коллектор читает хвост с хода `from_turn` (индексы с нуля, включительно).

Размер по умолчанию — длина вывода `normalize` с одними репликами человека (`human_measure`):
размер файла врёт в обе стороны из-за трейсов инструментов. `index_measure` — поле `bytes`
индекса, грубая замена, когда `normalize` недоступна.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable

from lib import marks, sources
from lib.config import load_config
from lib.workspace import SrError, ws_paths

TAKEN_STATES = ("new", "grown")
MEASURES = {"human": "human", "index": "file_bytes"}
SR_PY = Path(__file__).resolve().parents[1] / "sr.py"
NORMALIZE_TIMEOUT = 120


def index_measure(entry: dict) -> int:
    """Размер сессии по полю `bytes` индекса (размер файла, не человеческого слоя)."""
    return entry["bytes"]


def human_measure(root: Path) -> Callable[[dict], int]:
    """Замер человеческого слоя: длина stdout `sr.py normalize <лог> --roles user …` в байтах."""
    state_dir = ws_paths(root)["state_dir"]

    def measure(entry: dict) -> int:
        log = state_dir / entry["path"]
        cmd = [sys.executable, str(SR_PY), "--workspace", str(root), "normalize", str(log),
               "--roles", "user", "--drop-synthetic", "--max-turn-chars", "4000"]
        try:
            done = subprocess.run(cmd, shell=False, timeout=NORMALIZE_TIMEOUT, capture_output=True)
        except subprocess.TimeoutExpired as err:
            raise SrError(f"замер лога не уложился в {NORMALIZE_TIMEOUT} с ({log})", 1) from err
        stderr = (done.stderr or b"").decode("utf-8", errors="replace").strip()
        if done.returncode != 0:
            if "invalid choice" in stderr:
                raise SrError(
                    "нет команды normalize: замер человеческого слоя ставится вместе со сбором логов; "
                    f"до тех пор нарезка по размеру файла — флаг --measure index ({stderr})",
                    1,
                )
            raise SrError(f"замер лога упал с кодом {done.returncode} ({log}): {stderr}", 1)
        return len(done.stdout or b"")

    return measure


def _check_people(entries: list) -> str | None:
    people = sorted({e.get("person_id") for e in entries})
    if len(people) > 1:
        raise SrError(f"в нарезку переданы записи разных людей: {', '.join(map(str, people))}", 2)
    return people[0] if people else None


def _taken(entry: dict) -> bool:
    return (entry.get("state") in TAKEN_STATES and not entry.get("excluded")
            and entry.get("duplicate_of") is None)


def _ref(entry: dict) -> str:
    return entry.get("key") or entry.get("path") or str(entry.get("session_id"))


def _user_turns(entry: dict) -> int:
    turns = entry.get("user_turns")
    if not isinstance(turns, int) or isinstance(turns, bool):
        raise SrError(f"в записи индекса нет числа user_turns ({_ref(entry)}): пересоберите индекс сбором логов", 1)
    return turns


def _from_turn(entry: dict) -> int | None:
    if entry.get("state") != "grown":
        return None
    for field in ("review_from_turn", "reviewed_turns_total"):
        if isinstance(entry.get(field), int):
            return entry[field]
    raise SrError(
        f"выросшая сессия без хода, с которого читать хвост: нет review_from_turn и "
        f"reviewed_turns_total ({_ref(entry)})",
        1,
    )


def _session(entry: dict, size: int, turns: int) -> dict:
    return {
        "session_id": entry.get("session_id"), "key": entry.get("key"), "path": entry.get("path"),
        "state": entry.get("state"), "from_turn": _from_turn(entry), "bytes": size, "user_turns": turns,
        "log_started_at": entry.get("log_started_at"), "log_ended_at": entry.get("log_ended_at"),
    }


def _close(sessions: list, truncated: bool) -> dict:
    starts = [s["log_started_at"] for s in sessions if s["log_started_at"]]
    ends = [s["log_ended_at"] or s["log_started_at"] for s in sessions if s["log_ended_at"] or s["log_started_at"]]
    return {
        "sessions": sessions,
        "bytes": sum(s["bytes"] for s in sessions),
        "user_turns": sum(s["user_turns"] for s in sessions),
        "date_from": min(starts) if starts else None,
        "date_to": max(ends) if ends else None,
        "batch_truncated": truncated,
    }


def _split(entries: list, min_turns: int, period_label: str | None) -> tuple[list, list, list]:
    """(кандидаты в пачки по времени начала, короткие, чужой период) из записей одного человека."""
    short, skipped, candidates = [], [], []
    for entry in entries:
        if not _taken(entry):
            continue
        guess = entry.get("period_guess")
        if period_label is not None and guess is not None and guess != period_label:
            skipped.append({"session_id": entry.get("session_id"), "key": entry.get("key"),
                            "path": entry.get("path"), "reason": "other_period", "period_guess": guess})
            continue
        turns = _user_turns(entry)
        if turns < min_turns:
            short.append({"session_id": entry.get("session_id"), "key": entry.get("key"),
                          "path": entry.get("path"), "user_turns": turns, "reason": "transcript_too_short"})
            continue
        candidates.append(entry)
    candidates.sort(key=lambda e: (e.get("log_started_at") is None, e.get("log_started_at") or "", _ref(e)))
    return candidates, short, skipped


def period_candidates(entries, cfg_batches: dict, period_label: str | None = None) -> list[dict]:
    """Записи индекса, которые `plan` разложит по пачкам (без замера): годные, своего периода, не короткие.

    По ним `open-recs` решает, есть ли в периоде сессии после выдачи рекомендации (`evaluable`).
    """
    entries = list(entries)
    _check_people(entries)
    return _split(entries, cfg_batches["min_turns"], period_label)[0]


def plan(entries, cfg_batches: dict, measure: Callable[[dict], int], period_label: str | None = None,
         *, measure_name: str = "human") -> dict:
    """Пачки из записей индекса одного человека.

    Выход: `{person_id, measure, batches: [{index, total, sessions[], bytes, user_turns, date_from,
    date_to, batch_truncated}], short[], skipped[]}`. `period_guess: null` (период не угадан)
    не считается чужим периодом: сессия идёт в нарезку.
    """
    entries = list(entries)
    person_id = _check_people(entries)
    max_bytes, max_turns = cfg_batches["max_bytes"], cfg_batches["max_user_turns"]
    candidates, short, skipped = _split(entries, cfg_batches["min_turns"], period_label)

    batches: list[dict] = []
    current: list[dict] = []
    for entry in candidates:
        size = measure(entry)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise SrError(f"замер вернул не число байт: {size!r} ({_ref(entry)})", 1)
        turns = _user_turns(entry)
        session = _session(entry, size, turns)
        if size > max_bytes or turns > max_turns:
            if current:
                batches.append(_close(current, False))
                current = []
            batches.append(_close([session], True))
            continue
        if current and (sum(s["bytes"] for s in current) + size > max_bytes
                        or sum(s["user_turns"] for s in current) + turns > max_turns):
            batches.append(_close(current, False))
            current = []
        current.append(session)
    if current:
        batches.append(_close(current, False))
    for i, batch in enumerate(batches, start=1):
        batch["index"], batch["total"] = i, len(batches)
    ordered = [{k: b[k] for k in ("index", "total", "sessions", "bytes", "user_turns", "date_from", "date_to",
                                  "batch_truncated")} for b in batches]
    return {"person_id": person_id, "measure": measure_name, "batches": ordered, "short": short,
            "skipped": skipped}


def for_person(root: Path, person_id: str, measure: str = "human", period_label: str | None = None) -> dict:
    """Нарезка по индексу логов workspace: бюджеты из `config.batches`, записи только этого человека.

    В карточке `consent.logs: false` — `SrError(1)`: логи человека не собираются и не оцениваются."""
    if measure not in MEASURES:
        raise SrError(f"неизвестный замер {measure!r}: доступны {', '.join(MEASURES)}", 2)
    if sources.consent_refused(root, person_id):
        raise SrError(f"у {person_id} нет согласия на логи (consent.logs: false в карточке): "
                      f"логи не собираются и не оцениваются", 1)
    cfg = load_config(root)
    index = marks.load_index(root)
    if index is None:
        raise SrError(f"индекса логов нет: сначала соберите логи ({ws_paths(root)['logs_index']})", 1)
    entries = [e for e in index["logs"] if isinstance(e, dict) and e.get("person_id") == person_id]
    if not entries:
        raise SrError(f"у {person_id!r} нет логов в индексе ({ws_paths(root)['logs_index']})", 1)
    fn = human_measure(root) if measure == "human" else index_measure
    result = plan(entries, cfg["batches"], fn, period_label, measure_name=MEASURES[measure])
    result["person_id"] = person_id
    return result
