"""Инвентарь логов: снапшоты `.sessionretro/logs/<person>/` → `.sessionretro/logs-index.json`.

Инвентарь строится по снапшотам, а не по источникам: исходник может исчезнуть (JSONL Claude Code
чистится через 30 дней), снапшот остаётся. `gone` — снапшот удалён руками. Записи не удаляются.

Состояния записи: `new | grown | unchanged | duplicate | gone | excluded`.

- Рост считается только по ходам: `turns_total > reviewed_turns_total` (у исключённой —
  `> excluded_turns_total`). Байты не смотрятся: перевыгрузка или другая редакция санитайзера
  меняет байты, но не даёт нового материала.
- `review_from_turn` есть только у `grown`: с этого хода оценивается хвост.
- `reviewed` и `excluded` взаимоисключающие. Ставятся только через `mark()`; если прежний индекс
  несёт оба — побеждает более поздний по `*_at`, в `warnings` строка.
- Согласие отозвано (`no_consent` у merge/rebuild): записи человека — `excluded` с
  `excluded_reason`/`excluded_by: consent`, `reviewed` снимается; исключение, поставленное
  оценкой, не перетирается. Согласие вернули — записи с `excluded_by: consent` считаются заново.
  Снапшоты не удаляются: строка в `warnings` с их числом.
- Дубли одного ключа: побеждает `claude-jsonl` (таймстемпы, usage, компакты, сабагенты), затем
  больше ходов, больше байтов, путь по алфавиту. Проигравший — `key#dup:<path>`, `duplicate_of`.

Сайдкар `logs/<person>/.meta.json` (пишет collect) хранит поля разбора **исходника**: они
приоритетнее разбора снапшота (`key`, `session_id`, `log_*`, `started_at_source`,
`unparsed_lines`, `unknown_types`) и единственный источник `source*` и `sanitize`.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from lib import normalize
from lib.workspace import SrError, read_json, write_json_atomic, ws_paths

SCHEMA_VERSION = 1
META_FILE = ".meta.json"
SNAPSHOT_SUFFIXES = (".md", ".jsonl")
STATES = ("new", "grown", "unchanged", "duplicate", "gone", "excluded")
# подписи состояний в текстовых сводках collect и inventory (порядок — как STATES)
STATE_LABELS = (
    ("new", "новых"),
    ("grown", "выросших"),
    ("unchanged", "без изменений"),
    ("duplicate", "дублей"),
    ("gone", "пропавших"),
    ("excluded", "исключённых"),
)
PERIOD_MODES = ("run", "dates", "labels")

REVIEWED_FIELDS = ("reviewed_in_run", "reviewed_at", "reviewed_turns_total", "reviewed_bytes")
EXCLUDED_FIELDS = ("excluded", "excluded_in_run", "excluded_at", "excluded_reason",
                   "excluded_note", "excluded_turns_total", "excluded_bytes", "excluded_by")
# исключение по отзыву согласия (`consent.logs: false`): ставит merge, снимает merge при возврате
CONSENT = "consent"
# приоритет формата в дубле: меньше — выше (q-04)
FORMAT_PRIORITY = {"claude-jsonl": 0}
# поля разбора исходника, которые сайдкар перекрывает у разбора снапшота
META_DOC_FIELDS = ("key", "session_id", "log_started_at", "log_ended_at", "started_at_source")
META_METRIC_FIELDS = ("unparsed_lines", "unknown_types")

SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── ключ и имя снапшота ──────────────────────────────────────────────────────


def session_key(doc: dict, rel: str) -> str:
    """Ключ сессии: `session_id` → `ts:<started_at>` → `path:<rel>`.

    Голого `ts:None` не бывает: у логов без шапки он совпал бы, и разные разговоры слились бы.
    """
    if doc.get("session_id"):
        return str(doc["session_id"])
    if doc.get("started_at"):
        return f"ts:{doc['started_at']}"
    return f"path:{rel}"


def snapshot_name(key: str, fmt: str) -> str:
    """Имя снапшота: символы вне `[A-Za-z0-9._-]` → `_`; `.jsonl` для `claude-jsonl`, иначе `.md`."""
    return SAFE_RE.sub("_", key) + (".jsonl" if fmt == "claude-jsonl" else ".md")


# ── сайдкар и индекс ─────────────────────────────────────────────────────────


def load_meta(person_dir: Path) -> dict:
    """`logs/<person>/.meta.json`; нет файла — `{}`; не объект — `SrError(1)`."""
    path = Path(person_dir) / META_FILE
    if not path.is_file():
        return {}
    data = read_json(path)
    if not isinstance(data, dict):
        raise SrError(f"инвентарь: {META_FILE} не объект: получен {type(data).__name__} ({path})", 1)
    return data


def save_meta(person_dir: Path, data: dict) -> None:
    write_json_atomic(Path(person_dir) / META_FILE, data)


def load_index(root: Path) -> dict | None:
    """Прежний `logs-index.json` или None, если его нет. Битый/чужой схемы — `SrError(1)`."""
    path = ws_paths(root)["logs_index"]
    if not path.is_file():
        return None
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("logs"), list):
        raise SrError(f"инвентарь: logs-index.json не объект с массивом logs ({path})", 1)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SrError(
            f"инвентарь: неизвестная schema_version {data.get('schema_version')!r} "
            f"(ожидалась {SCHEMA_VERSION}, файл {path})", 1)
    return data


def save_index(root: Path, index: dict) -> None:
    write_json_atomic(ws_paths(root)["logs_index"], index)


# ── период ───────────────────────────────────────────────────────────────────


def validate_period(cfg: dict) -> None:
    """Проверка `period` конфига один раз до скана: ошибка — `SrError(1)` с именем поля.

    `mode` из `PERIOD_MODES`; у `dates` — `windows` списком объектов с непустым строковым
    `label` и необязательными строковыми `from`/`to`."""
    period = cfg.get("period")
    if period is None:
        return
    if not isinstance(period, dict):
        raise SrError(f"config: period должен быть объектом (получено {period!r})", 1)
    mode = period.get("mode", "run")
    if mode not in PERIOD_MODES:
        raise SrError(f"config: period.mode {mode!r} неизвестен (ожидалось {'|'.join(PERIOD_MODES)})", 1)
    if mode != "dates":
        return
    windows = period.get("windows") or []
    if not isinstance(windows, list):
        raise SrError(f"config: period.windows должен быть списком (получено {windows!r})", 1)
    for i, win in enumerate(windows):
        field = f"period.windows[{i}]"
        if not isinstance(win, dict):
            raise SrError(f"config: {field} должен быть объектом (получено {win!r})", 1)
        label = win.get("label")
        if not isinstance(label, str) or not label.strip():
            raise SrError(f"config: {field}.label — нужна непустая строка (получено {label!r})", 1)
        for bound in ("from", "to"):
            value = win.get(bound)
            if value is not None and not isinstance(value, str):
                raise SrError(f"config: {field}.{bound} — дата строкой YYYY-MM-DD или null "
                              f"(получено {value!r})", 1)


def period_guess(cfg: dict, started_at: str | None) -> tuple[str | None, str]:
    """(метка, источник): `run`/`labels` → (None, "none"); `dates` → окно по дню старта
    (`from`/`to` включительно, пустая граница открыта) → (метка, "date_window"), иначе (None, "unknown")."""
    period = cfg.get("period") or {"mode": "run"}
    mode = period.get("mode", "run")
    if mode in ("run", "labels"):
        return None, "none"
    if mode != "dates":
        raise SrError(f"инвентарь: неизвестный period.mode {mode!r} (ожидалось run|dates|labels)", 1)
    day = (started_at or "")[:10]
    if not day:
        return None, "unknown"
    for win in period.get("windows") or []:
        start, end = win.get("from"), win.get("to")
        if (not start or day >= start[:10]) and (not end or day <= end[:10]):
            return win["label"], "date_window"
    return None, "unknown"


# ── скан ─────────────────────────────────────────────────────────────────────


def _entry(root: Path, person: str, path: Path, meta: dict, cfg: dict) -> dict:
    state_dir = ws_paths(root)["state_dir"]
    rel = path.relative_to(state_dir).as_posix()
    raw = path.read_bytes()
    doc = normalize.parse_file(path)
    if not doc["turns_total"]:
        raise ValueError("0 ходов после разбора")
    rec = {
        "person_id": person,
        "source": meta.get("source"),
        "session_id": doc.get("session_id"),
        "key": session_key(doc, rel),
        "path": rel,
        "source_path": meta.get("source_path"),
        "format": doc["format"],
        "sha": hashlib.sha256(raw).hexdigest(),
        "bytes": doc["bytes"],
        "turns_total": doc["turns_total"],
        "user_turns": doc["user_turns"],
        "log_started_at": doc.get("started_at"),
        "log_ended_at": doc.get("ended_at"),
        "started_at_source": doc.get("started_at_source"),
        "collector": doc.get("collector"),
        "models": doc.get("models") or [],
        "period_guess": None,
        "period_guess_source": "none",
        "metrics": dict(doc.get("mechanical_metrics") or {}),
        "sanitize": meta.get("sanitize"),
    }
    for field in META_DOC_FIELDS:
        if field in meta:
            rec[field] = meta[field]
    for field in META_METRIC_FIELDS:
        if field in meta:
            rec["metrics"][field] = meta[field]
    rec["period_guess"], rec["period_guess_source"] = period_guess(cfg, rec["log_started_at"])
    return rec


def scan(root: Path, cfg: dict) -> tuple[list[dict], dict]:
    """Записи по снапшотам `logs/<person>/*.md|*.jsonl` и мусор.

    `junk = {"unparsed": [{"path", "error"}]}` — снапшот не разобрался или дал 0 ходов;
    rebuild превращает это в строки `warnings`, запись в индекс не попадает.
    """
    logs_dir = ws_paths(root)["logs"]
    entries: list[dict] = []
    junk: dict = {"unparsed": []}
    if not logs_dir.is_dir():
        return entries, junk
    state_dir = ws_paths(root)["state_dir"]
    for person_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
        meta = load_meta(person_dir)
        for path in sorted(person_dir.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.suffix not in SNAPSHOT_SUFFIXES:
                continue
            item_meta = meta.get(path.name) or {}
            if not isinstance(item_meta, dict):
                raise SrError(f"инвентарь: запись {path.name!r} в {META_FILE} не объект "
                              f"({person_dir / META_FILE})", 1)
            try:
                entries.append(_entry(root, person_dir.name, path, item_meta, cfg))
            except SrError:
                raise
            except Exception as err:  # noqa: BLE001 — один битый снапшот не роняет инвентарь, но виден
                junk["unparsed"].append({"path": path.relative_to(state_dir).as_posix(),
                                         "error": f"{type(err).__name__}: {err}"})
    return entries, junk


# ── дубли, слияние, пометки ──────────────────────────────────────────────────


def _rank(e: dict):
    return (FORMAT_PRIORITY.get(e.get("format"), 1), -(e.get("turns_total") or 0),
            -(e.get("bytes") or 0), e.get("path") or "")


def collapse_duplicates(entries: list[dict]) -> list[dict]:
    """Один ключ `(person_id, key)` → победитель плюс помеченные копии (вход не меняется)."""
    groups: dict = {}
    for e in entries:
        groups.setdefault((e["person_id"], e["key"]), []).append(e)
    out = []
    for items in groups.values():
        if len(items) == 1:
            out.append(items[0])
            continue
        items = sorted(items, key=_rank)
        winner = items[0]
        out.append(winner)
        for loser in items[1:]:
            rec = dict(loser)
            rec["key"] = f"{loser['key']}#dup:{loser['path']}"
            rec["duplicate_of"] = winner["path"]
            out.append(rec)
    return out


def _clear_reviewed(rec: dict) -> None:
    rec["reviewed"] = False
    for field in REVIEWED_FIELDS:
        rec[field] = None
    rec.pop("review_from_turn", None)


def _clear_excluded(rec: dict) -> None:
    for field in EXCLUDED_FIELDS:
        rec.pop(field, None)


def _resolve_both(rec: dict, warnings: list[str]) -> None:
    """Запись с reviewed и excluded сразу: остаётся более поздний по `*_at` (при равенстве — reviewed)."""
    if not (rec.get("reviewed") and rec.get("excluded")):
        return
    r_at, x_at = rec.get("reviewed_at") or "", rec.get("excluded_at") or ""
    if x_at > r_at:
        kept = "excluded"
        _clear_reviewed(rec)
    else:
        kept = "reviewed"
        _clear_excluded(rec)
    warnings.append(
        f"запись {rec.get('person_id')}/{rec.get('key')}: одновременно reviewed и excluded, "
        f"оставлен {kept} (reviewed_at {r_at or 'нет'}, excluded_at {x_at or 'нет'})")


def _exclude_for_consent(rec: dict, since: str | None, now: str) -> None:
    """Запись человека без согласия → `excluded` (`excluded_by: consent`); исключение оценкой остаётся."""
    if rec.get("excluded") and rec.get("excluded_by") != CONSENT:
        rec["state"] = "excluded"
        return
    _clear_reviewed(rec)
    _clear_excluded(rec)
    rec.update({"excluded": True, "excluded_in_run": None, "excluded_at": since or now,
                "excluded_reason": CONSENT, "excluded_note": None,
                "excluded_turns_total": rec.get("turns_total"), "excluded_bytes": rec.get("bytes"),
                "excluded_by": CONSENT, "state": "excluded"})


def consent_warning(person_id: str, count: int) -> str:
    return (f"{person_id}: в logs/{person_id}/ лежат снапшоты, собранные до отзыва согласия ({count}); "
            f"оценка их не берёт; удалите папку, если нужно")


def merge(prev: dict | None, entries: list[dict], workspace_id: str, now: str,
          no_consent=frozenset()) -> dict:
    """Слияние свежих записей с прежним индексом → новый индекс (записи не удаляются).

    `no_consent` — id людей с `consent.logs: false`: их записи исключаются (см. описание файла)."""
    warnings: list[str] = []
    no_consent = set(no_consent)
    old = {(r["person_id"], r["key"]): r for r in ((prev or {}).get("logs") or [])}
    fresh = collapse_duplicates(entries)
    out = []
    for e in fresh:
        rec = dict(e)
        rec.pop("review_from_turn", None)
        if e.get("duplicate_of"):
            _clear_excluded(rec)
            _clear_reviewed(rec)
            rec["state"] = "duplicate"
            out.append(rec)
            continue
        before = old.get((e["person_id"], e["key"]))
        denied = e["person_id"] in no_consent
        if before is None:
            _clear_excluded(rec)
            _clear_reviewed(rec)
            rec["state"] = "new"
            if denied:
                _exclude_for_consent(rec, None, now)
            out.append(rec)
            continue
        carried = {"reviewed": bool(before.get("reviewed"))}
        carried.update({f: before.get(f) for f in REVIEWED_FIELDS})
        carried.update({f: before[f] for f in EXCLUDED_FIELDS if f in before})
        carried.update({"person_id": e["person_id"], "key": e["key"]})
        _resolve_both(carried, warnings)
        _clear_excluded(rec)
        rec.update({k: v for k, v in carried.items() if k not in ("person_id", "key")})
        consent_since = None
        if rec.get("excluded_by") == CONSENT:
            consent_since = rec.get("excluded_at")
            _clear_excluded(rec)     # исключение по согласию пересчитывается на каждом прогоне
        turns = rec.get("turns_total") or 0
        if rec.get("excluded"):
            if turns > (rec.get("excluded_turns_total") or 0):
                _clear_excluded(rec)     # вырос после отброса — снова материал
                rec["state"] = "new"
            else:
                rec["state"] = "excluded"
        elif not rec["reviewed"]:
            rec["state"] = "new"
        elif turns > (rec.get("reviewed_turns_total") or 0):
            rec["state"] = "grown"
            rec["review_from_turn"] = rec.get("reviewed_turns_total")
        else:
            rec["state"] = "unchanged"
        if denied:
            _exclude_for_consent(rec, consent_since, now)
        out.append(rec)
    for pid in sorted(no_consent):
        count = sum(1 for e in fresh if e["person_id"] == pid)
        if count:
            warnings.append(consent_warning(pid, count))
    fresh_keys = {(e["person_id"], e["key"]) for e in fresh}
    for k, before in old.items():
        if k in fresh_keys:
            continue
        rec = dict(before)
        rec.pop("review_from_turn", None)
        _resolve_both(rec, warnings)
        rec["state"] = "gone"
        out.append(rec)
    out.sort(key=lambda r: (r["person_id"], r["key"]))
    return {"schema_version": SCHEMA_VERSION, "workspace": workspace_id, "updated_at": now,
            "logs": out, "warnings": warnings}


def mark(entry: dict, flag: str, run_id: str, at: str, **extra) -> dict:
    """Единственный способ поставить `reviewed`/`excluded`; снимает противоположный флаг.

    `reviewed`: `reviewed=True, reviewed_in_run, reviewed_at, reviewed_turns_total, reviewed_bytes`
    (из текущих `turns_total`/`bytes`), `excluded*` и `review_from_turn` удаляются.
    `excluded` (обязателен `reason`, `note` необязателен): `excluded=True, excluded_in_run,
    excluded_at, excluded_reason, excluded_note, excluded_turns_total, excluded_bytes`,
    `reviewed=False`, `reviewed_*=None`, `review_from_turn` удаляется. `state` не меняется —
    его пересчитает следующий merge. Возвращает новый словарь.
    """
    allowed = {"reviewed": set(), "excluded": {"reason", "note"}}
    if flag not in allowed:
        raise SrError(f"mark: неизвестный флаг {flag!r} (ожидалось reviewed|excluded, "
                      f"запись {entry.get('key')})", 2)
    unknown = sorted(set(extra) - allowed[flag])
    if unknown:
        raise SrError(f"mark: лишние параметры {', '.join(unknown)} для флага {flag} "
                      f"(запись {entry.get('key')})", 2)
    for field in ("turns_total", "bytes"):
        if field not in entry:
            raise SrError(f"mark: в записи нет поля {field} (запись {entry.get('key')})", 1)
    rec = dict(entry)
    rec.pop("review_from_turn", None)
    if flag == "reviewed":
        _clear_excluded(rec)
        rec.update({"reviewed": True, "reviewed_in_run": run_id, "reviewed_at": at,
                    "reviewed_turns_total": entry["turns_total"], "reviewed_bytes": entry["bytes"]})
        return rec
    reason = extra.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise SrError(f"mark: для excluded нужен непустой reason (запись {entry.get('key')})", 2)
    _clear_reviewed(rec)
    rec.pop("excluded_by", None)     # исключение оценкой не снимается возвратом согласия
    rec.update({"excluded": True, "excluded_in_run": run_id, "excluded_at": at,
                "excluded_reason": reason, "excluded_note": extra.get("note"),
                "excluded_turns_total": entry["turns_total"], "excluded_bytes": entry["bytes"]})
    return rec


# ── пересчёт и сводка ────────────────────────────────────────────────────────


def rebuild(root: Path, cfg: dict, now: str | None = None, warnings=(), no_consent=frozenset()) -> dict:
    """validate_period → scan → merge с прежним индексом → атомарная запись. Lock держит вызывающий.

    `no_consent` — id людей с `consent.logs: false` (`sources.no_consent`), передаётся в merge."""
    validate_period(cfg)
    entries, junk = scan(root, cfg)
    index = merge(load_index(root), entries,
                  (cfg.get("workspace") or {}).get("id"), now or now_iso(), no_consent=no_consent)
    index["warnings"] = [*warnings,
                         *(f"снапшот не разобран: {j['path']}: {j['error']}" for j in junk["unparsed"]),
                         *index["warnings"]]
    save_index(root, index)
    return index


def summary(index: dict) -> dict:
    """Число записей по каждому из шести состояний."""
    counts = {state: 0 for state in STATES}
    for rec in index.get("logs") or []:
        state = rec.get("state")
        if state not in counts:
            raise SrError(f"инвентарь: неизвестное состояние {state!r} у записи "
                          f"{rec.get('person_id')}/{rec.get('key')}", 1)
        counts[state] += 1
    return counts
