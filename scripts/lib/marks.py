"""Индекс логов `logs-index.json` и пометки сессий прогона: reviewed / excluded, откат, покрытие.

Индекс — формат сбора логов: `{schema_version: 1, workspace, updated_at, logs: [], warnings: []}`,
`path` записи относительно `.sessionretro/`. Читается (`inventory.load_index`) и пишется
(`inventory.save_index`) под тем же замком, что берут `collect` и `inventory`:
`lock.file_lock(<.sessionretro>/collect)` → lock-файл `collect.lock` (`index_lock`). Если нужен и
замок трекера, порядок всегда «трекер → индекс». Пометка трогает только поля `MARK_FIELDS`.

Флаг ставит `inventory.mark` — единственный способ поставить `reviewed`/`excluded` (снимает
противоположный). Здесь остаются проверка причины, `state` записи до следующего пересчёта,
прежнее состояние в `prev` и откат к нему (`unmark`) при отзыве прогона.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path

from lib import inventory
from lib.lock import file_lock
from lib.workspace import SrError, ws_paths

EXCLUDE_REASONS = (
    "empty_transcript", "transcript_too_short", "unreadable_input", "agent_subtranscript",
    "duplicate_export", "other_period", "not_batched", "excluded_other",
)
DEFAULT_EXCLUDE_REASON = "not_batched"
# Статусы сессий прогона, которые запись индекса не закрывают: сессию чужого периода оценит
# прогон её периода, поэтому пометки на ней нет и в ожидаемое покрытие она не входит.
OPEN_STATUSES = ("other_period",)
MIN_PREFIX = 8

# Поля, которые трогает пометка (`inventory.mark` плюс `state`): `prev` снимает их, откат
# возвращает; поле, которого до пометки не было, откат удаляет.
REVIEWED_FIELDS = ("reviewed", *inventory.REVIEWED_FIELDS, "review_from_turn")
EXCLUDED_FIELDS = inventory.EXCLUDED_FIELDS
MARK_FIELDS = (*REVIEWED_FIELDS, *EXCLUDED_FIELDS, "state")
# Замок индекса — замок сбора логов: `file_lock(<state_dir>/COLLECT_LOCK)` → `collect.lock`.
COLLECT_LOCK = "collect"
INDEX_LOCK_TIMEOUT = 10

# Замки индекса, которые этот процесс уже держит: `save_index` внутри `index_lock`
# не берёт замок второй раз (второй flock на том же файле из того же процесса ждал бы себя).
_HELD: set[str] = set()


def iso_utc(now: datetime | None = None) -> str:
    """`YYYY-MM-DDTHH:MM:SSZ` в UTC; `None` → текущее время, наивное время считается UTC."""
    at = now if now is not None else datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _index_path(root: Path) -> Path:
    return ws_paths(root)["logs_index"]


@contextlib.contextmanager
def index_lock(root: Path, timeout: float | None = None):
    """Замок сбора логов `file_lock(.sessionretro/collect)` (файл `collect.lock`), тот же, что у
    `collect` и `inventory`; повторный вход в процессе — без ожидания. `None` → `INDEX_LOCK_TIMEOUT`."""
    target = ws_paths(root)["state_dir"] / COLLECT_LOCK
    timeout = INDEX_LOCK_TIMEOUT if timeout is None else timeout
    ident = str(target.resolve())
    if ident in _HELD:
        yield Path(f"{target}.lock")
        return
    with file_lock(target, timeout=timeout) as lock_path:
        _HELD.add(ident)
        try:
            yield lock_path
        finally:
            _HELD.discard(ident)


def load_index(root: Path) -> dict | None:
    """Индекс логов через `inventory.load_index`: нет файла → `None`; битый JSON, не та форма или
    чужая `schema_version` → `SrError(1)`."""
    try:
        return inventory.load_index(root)
    except SrError as err:
        if err.code != 1:
            raise
        raise SrError(f"{err.message}; почините файл или пересоберите индекс командой inventory", 1) from err


def save_index(root: Path, index: dict, now: datetime | None = None, timeout: float | None = None) -> None:
    """Записать индекс `inventory.save_index` под `index_lock` (ставит `updated_at`); замок занят → `SrError(1)`."""
    if not isinstance(index, dict) or not isinstance(index.get("logs"), list):
        raise SrError(f"индекс логов не той формы для записи ({_index_path(root)})", 2)
    with index_lock(root, timeout=timeout):
        index["updated_at"] = iso_utc(now)
        inventory.save_index(root, index)


def _candidates(index: dict, person_id: str) -> list[dict]:
    return [
        e for e in index.get("logs") or []
        if isinstance(e, dict) and e.get("person_id") == person_id
        and e.get("state") != "gone" and not e.get("duplicate_of")
    ]


def _describe(entry: dict) -> str:
    return f"{entry.get('session_id')} ({entry.get('path')})"


def _pick(hits: list[dict], person_id: str, ref: str, how: str) -> dict | None:
    if len(hits) > 1:
        listed = "; ".join(_describe(e) for e in hits)
        raise SrError(
            f"сессия {ref!r} у {person_id} неоднозначна ({how}): подходят {len(hits)} записи "
            f"индекса: {listed}; укажите полный session_id или путь",
            1,
        )
    return hits[0] if hits else None


def _tail_match(entry_path: str, ref: str) -> bool:
    if not entry_path:
        return False
    return (
        entry_path == ref
        or entry_path.endswith("/" + ref.lstrip("/"))
        or ref.endswith("/" + entry_path)
    )


def find_entry(index: dict, person_id: str, ref: str) -> dict | None:
    """Запись индекса под ссылку на сессию, только среди записей `person_id`.

    Порядок: `session_id` → уникальный префикс `session_id` (≥ 8 знаков) → `key` → `path` →
    хвост пути (в обе стороны, по границе `/`). Записи `gone` и с `duplicate_of` не берутся.
    Неоднозначно на любом шаге → `SrError(1)` со списком кандидатов; не найдено → `None`.
    """
    ref = (ref or "").strip()
    if not ref:
        raise SrError(f"пустая ссылка на сессию (человек: {person_id})", 2)
    mine = _candidates(index, person_id)
    steps = [
        ("session_id", lambda e: e.get("session_id") == ref),
    ]
    if len(ref) >= MIN_PREFIX:
        steps.append(("префикс session_id",
                      lambda e: bool(e.get("session_id")) and e["session_id"].startswith(ref)))
    steps += [
        ("key", lambda e: e.get("key") == ref),
        ("path", lambda e: e.get("path") == ref),
        ("хвост пути", lambda e: _tail_match(e.get("path") or "", ref)),
    ]
    for how, test in steps:
        found = _pick([e for e in mine if test(e)], person_id, ref, how)
        if found is not None:
            return found
    return None


def _snapshot(entry: dict) -> dict:
    return {f: entry[f] for f in MARK_FIELDS if f in entry}


def _apply(entry: dict, marked: dict, state: str) -> None:
    """Перенести результат `inventory.mark` в ту же запись (ссылки на неё остаются живыми)."""
    marked["state"] = state
    entry.clear()
    entry.update(marked)


def mark_reviewed(entry: dict, run_id: str, now_iso: str) -> dict:
    """Пометить сессию оценённой прогоном `run_id`; вернуть пометку с `prev` для отката.

    Флаг ставит `inventory.mark`: объём на момент оценки (`reviewed_turns_total`,
    `reviewed_bytes`) — по нему следующий сбор отделит хвост выросшей сессии; `review_from_turn`
    и поля исключения удаляются. `state` → `unchanged` (так же его посчитает следующий пересчёт).
    """
    prev = _snapshot(entry)
    _apply(entry, inventory.mark(entry, "reviewed", run_id, now_iso), "unchanged")
    return {
        "kind": "reviewed",
        "key": entry.get("key"),
        "session_id": entry.get("session_id"),
        "path": entry.get("path"),
        "marked": {"reviewed_turns_total": entry["reviewed_turns_total"],
                   "reviewed_bytes": entry["reviewed_bytes"]},
        "prev": prev,
    }


def mark_excluded(entry: dict, run_id: str, reason: str | None, note: str | None, now_iso: str) -> dict:
    """Закрыть сессию, не пошедшую в оценку, с причиной; вернуть пометку с `prev`.

    Причина — из `EXCLUDE_REASONS` (`None` → `not_batched`), иначе `SrError(2)` без правки записи.
    Флаг ставит `inventory.mark`; поля оценки снимаются (балла на этой сессии нет), `state` → `excluded`.
    """
    reason = reason or DEFAULT_EXCLUDE_REASON
    if reason not in EXCLUDE_REASONS:
        raise SrError(
            f"неизвестная причина исключения {reason!r} (сессия {entry.get('session_id')}); "
            f"допустимы: {', '.join(EXCLUDE_REASONS)}",
            2,
        )
    note = (note or "").strip() or None
    prev = _snapshot(entry)
    _apply(entry, inventory.mark(entry, "excluded", run_id, now_iso, reason=reason, note=note), "excluded")
    return {
        "kind": "excluded",
        "key": entry.get("key"),
        "session_id": entry.get("session_id"),
        "path": entry.get("path"),
        "reason": reason,
        "note": note,
        "prev": prev,
    }


def kept_mark(entry: dict, kind: str, newer_run_id: str, reason: str | None = None) -> dict:
    """Пометка без правки записи: запись уже закрыл более новый прогон `newer_run_id`.

    Держит сессию в покрытии прогона. Откат её не применяет: штамп на записи чужой, а `_unwind`
    и `unmark` трогают только запись со штампом отзываемого прогона.
    """
    mark = {
        "kind": kind,
        "key": entry.get("key"),
        "session_id": entry.get("session_id"),
        "path": entry.get("path"),
        "kept_newer": newer_run_id,
        "prev": _snapshot(entry),
    }
    if kind == "excluded":
        mark["reason"] = reason or DEFAULT_EXCLUDE_REASON
    return mark


def stamped_by(entry: dict) -> str | None:
    """Прогон, чья пометка сейчас стоит на записи (`excluded_in_run` / `reviewed_in_run`), или `None`."""
    if entry.get("excluded"):
        return entry.get("excluded_in_run")
    if entry.get("reviewed"):
        return entry.get("reviewed_in_run")
    return None


_stamped_by = stamped_by


def unmark(entry: dict, mark: dict, newer_run_ids: set) -> str:
    """Откатить пометку к `mark["prev"]`; запись, переписанную более новым прогоном, не трогать.

    `newer_run_ids` — живые прогоны того же человека новее отзываемого. Возвращает
    `"restored"` или `"kept_newer"`. Пометка не от этой записи (другой `key`) → `SrError(1)`.
    """
    if entry.get("key") != mark.get("key"):
        raise SrError(
            f"пометка не от этой записи индекса: ключ пометки {mark.get('key')!r}, "
            f"ключ записи {entry.get('key')!r}",
            1,
        )
    if _stamped_by(entry) in newer_run_ids:
        return "kept_newer"
    prev = mark.get("prev") or {}
    for field in MARK_FIELDS:
        if field in prev:
            entry[field] = prev[field]
        else:
            entry.pop(field, None)
    return "restored"


def coverage(run: dict, index: dict | None) -> tuple[int, int]:
    """(помечено этим прогоном, ожидается) — ожидаются сессии `sessions_reviewed`, кроме `OPEN_STATUSES`.

    Пометка засчитывается, если запись с её `key` есть у этого человека и закрыта: этим прогоном
    или, поверх него, более новым (выросшая сессия, переоценённая позже, остаётся закрытой).
    Нет индекса → помеченных 0.
    """
    expected = sum(1 for s in run.get("sessions_reviewed") or []
                   if not (isinstance(s, dict) and s.get("status") in OPEN_STATUSES))
    if index is None:
        return 0, expected
    person_id = run.get("person_id")
    by_key = {e.get("key"): e for e in index.get("logs") or []
              if isinstance(e, dict) and e.get("person_id") == person_id}
    counted: set = set()
    for mark in run.get("logs_index_marks") or []:
        key = mark.get("key")
        entry = by_key.get(key)
        if key in counted or entry is None:
            continue
        # закрыта этим прогоном (штамп == run_id) или переписана более новым — оба случая в счёт
        if _stamped_by(entry) is not None:
            counted.add(key)
    return len(counted), expected
