"""Данные отчётов: трекер v2 → модель → payload по режиму.

`collect(root, warn)` читает конфиг, карточки людей, трекер `runs/process-reviews.json` и
файлы ревью и собирает модель:

    model = {workspace:{id,title,mode,lang,show_scores}, axes:[id], unbound_reviews:[run_id],
             warnings:[str], people:[{id, name, group, show_dynamics, points:[P], recs:[R]}]}

Поля, которые показываются как коды (`reason`, `axis` рекомендации), сверяются со словарём:
неизвестная причина → `other`, рекомендация с чужой осью пропускается — свободный текст
оценщика в эти поля адресату не попадает. Ревью привязывается к прогону, только если лежит
в `reviews/<id>/` владельца и `person_id` во frontmatter совпадает.

`build_payload(model, mode=…)` строит из модели данные одного HTML-файла **белым списком**
полей для режима: `personal` (один человек, только то, что можно показать адресату),
`group` (баллы и уровни всех, без текстов) и `reviewer` (всё). Фильтрация идёт здесь, до
встраивания в HTML: то, что спрятано только скриптом страницы, увидит любой читатель файла.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Callable

from lib import md
from lib.config import load_config
from lib.people import list_people
from lib.workspace import SrError, ws_paths

TRACKER_VERSION = 2
MODES = ("group", "reviewer", "personal")
CLOSING_VERDICTS = ("followed", "ignored")
KEEP_MD = ("feedback_draft",)

# Ключ ручной правки баллов в прогоне трекера.
OVERRIDE_KEY = "operator_override"

PERSONAL_FORBIDDEN_KEYS = frozenset({
    "gate", "summary", "antipatterns", "flags", "evidence", "note", "feedback_draft",
    "warnings", "reviewer", "service_md", "service_html", "dossier_md", "dossier_html",
    "run_id", "agent_stack", "gating_evidence", "period_key", "reviewed_at", "dossier_ref",
    "path",
})
SCORE_KEYS = frozenset({"score", "level"})

_I18N_SKIP_SECTIONS = ("cli",)
_I18N_PATH = Path(__file__).resolve().parents[2] / "references" / "i18n.json"


# --- словари ------------------------------------------------------------------------------

def load_i18n() -> dict:
    """Секции `references/i18n.json` без `cli` и без служебных имён на `_` (секций и ключей).

    Файл не читается, битый JSON или секция не объект → `SrError(1)` с путём.
    """
    path = _I18N_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise SrError(f"словарь интерфейса не читается: {err} ({path})", 1) from err
    if not isinstance(data, dict):
        raise SrError(f"словарь интерфейса не объект: {type(data).__name__} ({path})", 1)
    out = {}
    for name, section in data.items():
        if name in _I18N_SKIP_SECTIONS or name.startswith("_"):
            continue
        if not isinstance(section, dict):
            raise SrError(f"секция {name!r} словаря интерфейса не объект ({path})", 1)
        out[name] = {k: v for k, v in section.items() if not k.startswith("_")}
    if not out.get("axes"):
        raise SrError(f"в словаре интерфейса нет секции axes ({path})", 1)
    return out


# --- трекер -------------------------------------------------------------------------------

def load_tracker(path: Path) -> dict:
    """Трекер прогонов v2; нет файла, не читается, битый JSON, не v2 → `SrError(1)` с путём."""
    path = Path(path)
    if not path.is_file():
        raise SrError(f"нет трекера прогонов: файла нет ({path}); сначала оцените сессии", 1)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as err:
        raise SrError(f"трекер прогонов не читается: {err} ({path})", 1) from err
    except json.JSONDecodeError as err:
        raise SrError(f"трекер прогонов — битый JSON: {err} ({path})", 1) from err
    if not isinstance(doc, dict):
        raise SrError(f"трекер прогонов не объект: {type(doc).__name__} ({path})", 1)
    version = doc.get("tracker_version")
    if version != TRACKER_VERSION:
        raise SrError(
            f"tracker_version {version!r} не поддерживается, нужен {TRACKER_VERSION} ({path})", 1)
    if not isinstance(doc.get("runs"), list):
        raise SrError(f"в трекере нет списка runs ({path})", 1)
    return doc


def _period(run: dict) -> dict:
    period = run.get("period")
    return period if isinstance(period, dict) else {}


def _period_key(run: dict) -> str:
    return _period(run).get("label") or run["run_id"]


def _period_label(run: dict) -> str:
    return _period(run).get("label") or (run.get("reviewed_at") or "")[:10]


def _sort_key(run: dict) -> tuple[str, str]:
    reviewed = run.get("reviewed_at") or ""
    return (_period(run).get("from") or reviewed[:10], reviewed)


def _level(run: dict, warn: Callable[[str], None]) -> str | None:
    rid = run["run_id"]
    if run.get("level_computed"):
        return run["level_computed"]
    if run.get("level_judge"):
        warn(f"прогон {rid}: нет level_computed, уровень взят из level_judge")
        return run["level_judge"]
    if run.get("overall_level"):
        warn(f"прогон {rid}: нет level_computed и level_judge, уровень взят из overall_level")
        return run["overall_level"]
    warn(f"прогон {rid}: уровня нет (level_computed, level_judge, overall_level пусты)")
    return None


def _is_score(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 4


def _check_run_types(run: dict, tracker_path: Path) -> None:
    """Типы полей, по которым считается модель: `score` и балл ручной правки — int 0–4
    или null, `revision` — int ≥ 1 или отсутствует. Иначе `SrError(1)` с run_id и полем."""
    rid = run["run_id"]

    def bad(field: str, expected: str, value) -> SrError:
        return SrError(f"прогон {rid}: {field} должно быть {expected}, получено {value!r} "
                       f"({tracker_path})", 1)

    revision = run.get("revision")
    if revision is not None and not (isinstance(revision, int) and not isinstance(revision, bool)
                                     and revision >= 1):
        raise bad("revision", "целым ≥ 1 или отсутствовать", revision)
    for i, axis in enumerate(run.get("axes") or []):
        if not isinstance(axis, dict):
            raise bad(f"axes[{i}]", "объектом", axis)
        score = axis.get("score")
        if score is not None and not _is_score(score):
            raise bad(f"axes[{axis.get('id', i)}].score", "целым 0–4 или null", score)
    override = run.get(OVERRIDE_KEY)
    if override is None:
        return
    if not isinstance(override, dict):
        raise bad(OVERRIDE_KEY, "объектом или null", override)
    over_axes = override.get("axes")
    if over_axes is None:
        return
    if not isinstance(over_axes, dict):
        raise bad(f"{OVERRIDE_KEY}.axes", "объектом или null", over_axes)
    for axis, score in over_axes.items():
        if score is not None and not _is_score(score):
            raise bad(f"{OVERRIDE_KEY}.axes.{axis}", "целым 0–4 или null", score)


def reason_codes(i18n: dict) -> frozenset[str]:
    """Известные коды причины «данных нет»: ключи `reason_*` секции `report` без префикса."""
    return frozenset(k[len("reason_"):] for k in i18n.get("report", {}) if k.startswith("reason_"))


def _axes(run: dict, axes: list[str], warn: Callable[[str], None],
          reasons: frozenset[str] = frozenset()) -> dict:
    by_id = {a.get("id"): a for a in run.get("axes") or [] if isinstance(a, dict)}
    override = ((run.get(OVERRIDE_KEY) or {}).get("axes") or {})
    out = {}
    for axis in axes:
        raw = by_id.get(axis)
        if raw is None:
            warn(f"прогон {run['run_id']}: нет оси {axis}, считается «данных нет»")
            raw = {}
        ie = bool(raw.get("insufficient_evidence")) or raw.get("score") is None
        reason = (raw.get("insufficient_evidence_reason") or None) if ie else None
        if reason is not None and reason not in reasons:
            # свободный текст оценщика адресату не показывается: только код из словаря
            warn(f"прогон {run['run_id']}: ось {axis}, insufficient_evidence_reason "
                 f"{reason!r} не из известных кодов, показывается как other")
            reason = "other"
        item = {
            "score": None if ie else raw.get("score"),
            "ie": ie,
            "reason": reason,
            "note": raw.get("note") or "",
            "overridden": False,
            "evidence": [
                {"session_id": e.get("session_id"), "turn_index": e.get("turn_index"),
                 "quote": e.get("quote"), "reading": e.get("reading")}
                for e in raw.get("evidence") or [] if isinstance(e, dict)
            ],
        }
        if override.get(axis) is not None:
            item.update(score=override[axis], ie=False, reason=None, overridden=True)
        out[axis] = item
    return out


def _review_view(review: dict) -> dict:
    return {key: review.get(key) for key in
            ("status", "service_md", "sent_md", "dossier_md", "path", "recognized", "warning")}


def _point(run: dict, axes: list[str], review: dict | None, warn: Callable[[str], None],
           reasons: frozenset[str]) -> dict:
    reviewed = run.get("reviewed_at") or ""
    return {
        "run_id": run["run_id"],
        "period_key": _period_key(run),
        "period": _period_label(run),
        "date": reviewed[:10],
        "reviewed_at": reviewed,
        "sessions": len(run.get("sessions_reviewed") or []),
        "level": _level(run, warn),
        "gate": bool(run.get("gating_triggered")),
        "agent_stack": list(run.get("agent_stack") or []),
        "summary": run.get("summary") or "",
        "axes": _axes(run, axes, warn, reasons),
        "antipatterns": copy.deepcopy(run.get("antipatterns") or []),
        "flags": copy.deepcopy(run.get("flags") or []),
        "gating_evidence": copy.deepcopy(run.get("gating_evidence") or []),
        "feedback_draft": run.get("feedback_draft") or "",
        "warnings": list(run.get("warnings") or []),
        "reviewer": run.get("reviewer") or "",
        "review": _review_view(review) if review else None,
    }


def _recs(runs: list[dict], axes: list[str], warn: Callable[[str], None]) -> list[dict]:
    """Рекомендации человека по его прогонам (в порядке точек) с поздними вердиктами.

    Рекомендация с осью не из `axes` пропускается с предупреждением (ось — код, свободный
    текст в этом поле адресату не показывается); вердикты по ней тоже пропускаются.
    """
    recs: dict[str, dict] = {}
    skipped: set[str] = set()
    for run in runs:
        label = _period_label(run)
        for verdict in run.get("prev_recommendations") or []:
            if verdict.get("id") in skipped:
                continue
            rec = recs.get(verdict.get("id"))
            if rec is None:
                warn(f"прогон {run['run_id']}: вердикт по неизвестной рекомендации "
                     f"{verdict.get('id')!r}")
                continue
            rec["verdicts"].append({"period": label, "verdict": verdict.get("verdict"),
                                    "note": verdict.get("note") or ""})
        for item in run.get("recommendations") or []:
            rid = item.get("id")
            if not rid or rid in recs or rid in skipped:
                continue
            if item.get("axis") not in axes:
                warn(f"прогон {run['run_id']}: рекомендация {rid!r} с осью {item.get('axis')!r} "
                     f"не из списка осей, пропущена")
                skipped.add(rid)
                continue
            recs[rid] = {"id": rid, "axis": item.get("axis"), "text": item.get("text") or "",
                         "given_in": label, "given_at": run.get("reviewed_at"),
                         "verdicts": [], "open": True}
    for rec in recs.values():
        rec["open"] = not any(v["verdict"] in CLOSING_VERDICTS for v in rec["verdicts"])
    return list(recs.values())


def collect(root: Path, warn: Callable[[str], None]) -> dict:
    """Модель отчёта из workspace `root`; предупреждения — через `warn` и в `model.warnings`.

    Нет прогонов, прогон человека без карточки, трекер не v2 → `SrError(1)`.
    """
    warnings: list[str] = []

    def note(message: str) -> None:
        warnings.append(message)
        warn(message)

    root = Path(root)
    paths = ws_paths(root)
    cfg = load_config(root)
    i18n = load_i18n()
    axes = list(i18n["axes"])
    reasons = reason_codes(i18n)
    tracker_path = paths["runs"] / "process-reviews.json"
    tracker = load_tracker(tracker_path)
    for i, run in enumerate(tracker["runs"]):
        if not isinstance(run, dict):
            raise SrError(f"элемент runs[{i}] трекера не объект: {type(run).__name__} "
                          f"({tracker_path})", 1)
    runs = tracker["runs"]
    live = [r for r in runs if not r.get("retracted")]
    if not live:
        raise SrError(f"нет прогонов для отчёта: в трекере {len(runs)} прогонов, живых нет "
                      f"({tracker_path})", 1)
    cards = {c["id"]: c for c in list_people(root)}
    show_dynamics = {pid: _show_dynamics(card) for pid, card in cards.items()}

    latest: dict[tuple[str, str], dict] = {}
    for run in live:
        if not run.get("run_id"):
            raise SrError(f"прогон без run_id в трекере ({tracker_path})", 1)
        _check_run_types(run, tracker_path)
        pid = run.get("person_id")
        if pid not in cards:
            raise SrError(f"прогон {run['run_id']}: нет карточки человека {pid!r} "
                          f"(people/{pid}.yaml); данные не сошлись", 1)
        key = (pid, _period_key(run))
        rank = (run.get("reviewed_at") or "", run.get("revision") or 0)
        prev = latest.get(key)
        if prev is None or rank > (prev.get("reviewed_at") or "", prev.get("revision") or 0):
            latest[key] = run

    reviews, review_warnings = md.load_reviews(paths["reviews"])
    for message in review_warnings:
        note(message)
    chosen_ids = {run["run_id"] for run in latest.values()}
    retracted_ids = {r.get("run_id") for r in runs if r.get("retracted")}
    replaced_by = {run["run_id"]: latest[(run.get("person_id"), _period_key(run))]
                   for run in live if run["run_id"] not in chosen_ids}
    unbound = [rid for rid in reviews if rid not in chosen_ids]
    for rid in sorted(unbound):
        where = f"{reviews[rid]['path']} (run_id {rid})"
        if rid in retracted_ids:
            note(f"ревью не привязано, прогон отозван: {where}")
        elif rid in replaced_by:
            newer = replaced_by[rid]
            by = (f"ревизией {newer['revision']}" if newer.get("revision") is not None
                  else "более поздним прогоном")
            note(f"ревью не привязано, прогон заменён {by} ({newer['run_id']}): {where}")
        else:
            note(f"ревью не привязано, прогона нет в трекере: {where}")

    by_person: dict[str, list[dict]] = {}
    for (pid, _key), run in latest.items():
        by_person.setdefault(pid, []).append(run)

    people = []
    for pid, card in cards.items():
        person_runs = sorted(by_person.get(pid, []), key=_sort_key)
        if not person_runs:
            continue
        points = []
        for run in person_runs:
            review = _bound_review(reviews.get(run["run_id"]), pid, note)
            if run["run_id"] in reviews and review is None:
                unbound.append(run["run_id"])
            points.append(_point(run, axes, review, note, reasons))
        people.append({"id": pid, "name": card.get("name") or pid, "group": card.get("group"),
                       "show_dynamics": show_dynamics[pid],
                       "points": points, "recs": _recs(person_runs, axes, note)})
    skipped = len(cards) - len(people)
    if skipped:
        note(f"людей без прогонов: {skipped}, в отчёт не попадают")
    people.sort(key=lambda p: (p["name"].casefold(), p["id"]))

    ws = cfg["workspace"]
    return {
        "workspace": {"id": ws["id"], "title": ws["title"], "mode": cfg["mode"],
                      "lang": cfg["lang"], "show_scores": bool(cfg["show_scores"])},
        "axes": axes,
        "people": people,
        "unbound_reviews": sorted(unbound),
        "warnings": warnings,
    }


def _show_dynamics(card: dict) -> bool:
    """`feedback.show_dynamics` карточки (по умолчанию true); не bool → `SrError(1)`."""
    value = (card.get("feedback") or {}).get("show_dynamics", True)
    if not isinstance(value, bool):
        raise SrError(f"people/{card.get('id')}.yaml: feedback.show_dynamics должно быть "
                      f"true/false, получено {value!r}", 1)
    return value


def _bound_review(review: dict | None, pid: str, note: Callable[[str], None]) -> dict | None:
    """Ревью привязывается к прогону человека `pid`, только если лежит в `reviews/<pid>/`
    и `person_id` во frontmatter (если есть) равен `pid`; иначе None и предупреждение."""
    if review is None:
        return None
    parts = review["path"].split("/")
    folder = parts[1] if len(parts) > 2 else None
    owner = review["meta"].get("person_id")
    if folder != pid:
        note(f"{review['path']}: ревью к прогону {pid!r} лежит не в reviews/{pid}/, "
             f"не привязано")
        return None
    if owner and owner != pid:
        note(f"{review['path']}: person_id {owner!r} во frontmatter, прогон — {pid!r}; "
             f"не привязано")
        return None
    return review


# --- payload ------------------------------------------------------------------------------

def transitions(points: list[dict], axes: list[str]) -> list[dict[str, str]]:
    """Для точки i по каждой оси: `up|down|flat`, `none` — первая точка или IE в i или i-1."""
    out = []
    for i, point in enumerate(points):
        row = {}
        for axis in axes:
            cur = point["axes"][axis]
            prev = points[i - 1]["axes"][axis] if i else None
            if prev is None or cur["ie"] or prev["ie"]:
                row[axis] = "none"
            elif cur["score"] > prev["score"]:
                row[axis] = "up"
            elif cur["score"] < prev["score"]:
                row[axis] = "down"
            else:
                row[axis] = "flat"
        out.append(row)
    return out


def _rec_status(rec: dict) -> str:
    status = "open"
    for verdict in rec["verdicts"]:
        if verdict["verdict"] in CLOSING_VERDICTS:
            status = verdict["verdict"]
    return status


def _personal_person(person: dict, axes: list[str], show_scores: bool,
                     show_dynamics: bool) -> dict:
    trans = transitions(person["points"], axes)
    points = []
    for point, tr in zip(person["points"], trans):
        out_axes = {}
        for axis in axes:
            src = point["axes"][axis]
            item = {"ie": src["ie"], "reason": src["reason"]}
            if show_scores:
                item["score"] = src["score"]
            out_axes[axis] = item
        review = point["review"]
        sent = (review.get("sent_md") if review and review.get("status") == "approved"
                else None)
        out = {"period": point["period"], "date": point["date"], "sessions": point["sessions"],
               "axes": out_axes, "review": {"sent_md": sent} if sent else None}
        if show_dynamics:
            out["transitions"] = dict(tr)
        if show_scores:
            out["level"] = point["level"]
        points.append(out)
    recs = [{"text": r["text"], "axis": r["axis"], "given_in": r["given_in"],
             "status": _rec_status(r)} for r in person["recs"]]
    return {"id": person["id"], "name": person["name"], "points": points, "recs": recs}


def _group_person(person: dict, axes: list[str]) -> dict:
    points = []
    for point in person["points"]:
        points.append({
            "period_key": point["period_key"], "period": point["period"], "date": point["date"],
            "level": point["level"], "gate": point["gate"],
            "axes": {a: {"score": point["axes"][a]["score"], "ie": point["axes"][a]["ie"],
                         "overridden": point["axes"][a]["overridden"]} for a in axes},
            "review": None,
        })
    return {"id": person["id"], "name": person["name"], "group": person["group"],
            "points": points, "recs": []}


def _reviewer_person(person: dict, axes: list[str], quote_periods: int | None) -> dict:
    out = copy.deepcopy(person)
    trans = transitions(out["points"], axes)
    cut = 0 if quote_periods is None else max(len(out["points"]) - quote_periods, 0)
    for i, (point, tr) in enumerate(zip(out["points"], trans)):
        point["transitions"] = dict(tr)
        if i >= cut:
            continue
        point["trimmed"] = True
        for axis in point["axes"].values():
            axis["evidence"] = []
        for key in ("antipatterns", "gating_evidence"):
            for item in point[key]:
                if isinstance(item, dict) and "quote" in item:
                    item["quote"] = ""
        point["feedback_draft"] = ""
        review = point["review"]
        if review is not None:
            review.pop("dossier_md", None)
            review["dossier_ref"] = review["path"]
    return out


def build_payload(model: dict, *, mode: str, person: str | None = None,
                  show_scores: bool = False, lang: str = "ru", title: str = "",
                  generated_at: str = "", quote_periods: int | None = 3,
                  show_dynamics: bool = True) -> dict:
    """Данные одного HTML-файла для режима `group|reviewer|personal` (белые списки полей).

    `personal` требует `person` (id из модели), иначе `SrError(2)`. `quote_periods` —
    сколько последних периодов человека в reviewer несут цитаты, досье и черновик фидбэка
    (`None` — все); в старших точках `evidence` пуст, `quote` антипаттернов и гейта и
    `feedback_draft` пустые, досье заменено на `dossier_ref`, у точки `trimmed: true`.
    Модель не меняется.
    """
    if mode not in MODES:
        raise SrError(f"неизвестный режим отчёта {mode!r}, ожидается один из {MODES}", 2)
    if quote_periods is not None and (not isinstance(quote_periods, int)
                                      or isinstance(quote_periods, bool) or quote_periods < 0):
        raise SrError(f"quote_periods должно быть целым ≥ 0 или None, получено {quote_periods!r}", 2)
    axes = list(model["axes"])
    if mode == "personal":
        if not person:
            raise SrError("личный отчёт требует id человека (person)", 2)
        found = [p for p in model["people"] if p["id"] == person]
        if not found:
            known = ", ".join(p["id"] for p in model["people"])
            raise SrError(f"человек {person!r} не найден среди людей с прогонами ({known})", 2)
        people = [_personal_person(found[0], axes, show_scores, show_dynamics)]
        dates_from = found
    elif mode == "group":
        people = [_group_person(p, axes) for p in model["people"]]
        dates_from = model["people"]
    else:
        people = [_reviewer_person(p, axes, quote_periods) for p in model["people"]]
        dates_from = model["people"]
    dates = sorted(pt["date"] for p in dates_from for pt in p["points"] if pt["date"])
    return {
        "meta": {
            "title": title,
            "mode": mode,
            "lang": lang,
            "show_scores": bool(show_scores),
            "generated_at": generated_at,
            "workspace_title": model["workspace"]["title"],
            "date_from": dates[0] if dates else None,
            "date_to": dates[-1] if dates else None,
            "reviews_unbound": 0 if mode == "personal" else len(model["unbound_reviews"]),
            "quote_periods": quote_periods,
        },
        "axes": axes,
        "people": people,
    }


def _attach(value) -> None:
    if isinstance(value, dict):
        for key in [k for k in value if k.endswith("_md") and k not in KEEP_MD]:
            text = value.pop(key)
            value[key[:-3] + "_html"] = md.md_to_html(text) if text is not None else None
        for item in value.values():
            _attach(item)
    elif isinstance(value, list):
        for item in value:
            _attach(item)


def attach_html(payload: dict) -> dict:
    """`*_md` → `*_html` через `md.md_to_html`, `*_md` удаляются (кроме `feedback_draft`).

    Меняет payload на месте и возвращает его.
    """
    _attach(payload)
    return payload
