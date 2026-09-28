"""Генератор синтетического workspace для тестов и живого экрана отчётов.

`make_workspace(dst, people=3, periods=3, mode="team", lang="ru", realistic=False)` пишет
корень проекта с `.sessionretro/`: config.json, plugin-state.json, карточки людей
(+ карточка `nora` без прогонов), трекер v2 `runs/process-reviews.json` и файлы ревью
`reviews/<id>/<run_id>.md`. Все данные вымышленные, пути только `/home/user/…`.

Детерминированные особые случаи (i — номер человека с нуля, P — период):
- i=0: в P1 двойник с `retracted` и более поздним `reviewed_at` (у него есть ревью —
  оно станет непривязанным); в последнем периоде ось `reversibility` без данных
  (`insufficient_evidence`, `short_log`); в одной цитате `<img src=x onerror=alert(1)>`;
  в P2 к оценённым сессиям добавлены две отсеянные (`transcript_too_short`, `excluded_other`);
- i=1: в P1 живой дубль `revision: 2` с более поздним `reviewed_at` (ревью только у него);
  в P2 `operator_override {axes: {verification: 4}}` и `gating_triggered`;
- i=2: пропущен P2; в P1 `level_computed: null`, есть только `level_judge`;
- рекомендация `REC-<id>-NNN` в каждом живом периоде; в следующем живом периоде — вердикт
  по предыдущей: для первой `followed` / `not_observable` / `ignored` у i=0/1/2, дальше по кругу;
- ревью последнего периода человека `status: draft`, остальные `approved`.

CLI: `python3 report_fixture.py --out <dir> [--people N] [--periods M]
[--mode team|course|self] [--lang ru|en] [--realistic]` — печатает путь корня.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from lib.config import check_config  # noqa: E402
from lib.version import __version__  # noqa: E402
from lib.workspace import STATE_DIR, write_json_atomic  # noqa: E402

PLUGIN = SCRIPTS.parent
MODES = ("self", "team", "course")
LANGS = ("ru", "en")

# Вымышленные люди. Ни id, ни основа любого слова имени (первые 3 буквы) не являются
# префиксом ключа payload отчёта — иначе гейт имён ловил бы схему, а не людей.
NAMES = [
    ("anna", "Анна Вершинина"), ("boris", "Борис Каменев"), ("vera", "Вера Ольховская"),
    ("gleb", "Глеб Сорокин"), ("dina", "Дина Журавлёва"), ("egor", "Егор Туманов"),
    ("zhanna", "Жанна Белозёрова"), ("igor", "Игорь Ракитин"), ("kira", "Кира Полянская"),
    ("roman", "Роман Ветров"), ("mira", "Мира Снегирёва"), ("nikita", "Никита Орлов"),
    ("olga", "Ольга Ясенева"), ("pavel", "Павел Грачёв"), ("rimma", "Римма Зорина"),
    ("semyon", "Семён Бураков"), ("tamara", "Тамара Листова"), ("fedor", "Фёдор Кленов"),
    ("yulia", "Юлия Рябинина"), ("yakov", "Яков Мельник"), ("zoya", "Зоя Осокина"),
    ("viktor", "Виктор Соболев"), ("emma", "Эмма Дубравина"), ("ulyana", "Ульяна Ковылёва"),
    ("khariton", "Харитон Верескин"),
]
EXTRA = ("nora", "Нора Лесная")
SELF_ID = "me"
SELF_NAME = {"ru": "Я", "en": "Me"}
PAYLOAD_KEYS = ("level", "axes", "status", "transitions", "text", "date", "period", "mode", "lang")

BASE_DATE = datetime(2026, 1, 5, tzinfo=timezone.utc)
PERIOD_DAYS = 14
LEVELS = ("developing", "competent", "confident_operator", "orchestrator", "naive", "passenger")
FIRST_VERDICTS = ("followed", "not_observable", "ignored")
CYCLE_VERDICTS = ("followed", "partial", "ignored", "not_observable")
XSS_QUOTE = "<img src=x onerror=alert(1)>"

TEXT = {
    "ru": {
        "service": "Служебная шапка, в отправку не идёт.",
        "svc_sessions": "Разобрано сессий: {n}",
        "svc_warn": "Предупреждения: нет",
        "title": "### Ревью по сессиям, {label}",
        "coverage": "{name}, за период {label} разобрано сессий: {n}, ходов: {turns}.",
        "quick": "**Если читать быстро: что попробовать иначе.**",
        "first": ("**Первое.** В сессии `{sid}` (ход {turn}) правка ушла без прогона тестов, "
                  "потом пришлось откатывать. Стоит попробовать: прогон тестов до коммита."),
        "second": ("**Второе.** Задача ставилась без критерия готовности. Стоит попробовать: "
                   "одна строка «готово, когда…» в начале сессии."),
        "criteria": "**Дальше по восьми критериям, на чём это стоит.**",
        "evidence": "Видно по сессии `{sid}`, ход {turn}.",
        "dossier": "## Досье (для ответа на дозапросы, в отправку не идёт)",
        "table": "| Дата | Файл лога | session_id | Ходов |",
        "quotes": "### Цитаты, на которых стоят утверждения",
        "contacts": "Контакт в логе: @fake-handle-{pid}, репозиторий https://github.com/fake-{pid}/repo",
        "flags": "### Флаги (внутреннее)",
        "gate": "### Гейт",
        "gate_yes": "Сработал: удаление без резервной копии.",
        "gate_no": "Не сработал.",
        "recs": "### Рекомендации этого прогона",
        "rec_text": "Перед коммитом прогонять тесты и смотреть дифф целиком.",
        "summary": "Уверенно ведёт агента, проверка результата нестабильна.",
        "note": "Наблюдение по оси {axis} в периоде {label}.",
        "reading": "Проверил результат сам, не поверил отчёту агента.",
        "quote": "запусти тесты в /home/user/proj/{pid}/tests и покажи вывод",
        "draft_para": ("Абзац черновика фидбэка: что было видно в сессиях периода {label}, "
                       "где агент ошибся и как это заметили. "),
        "why_retracted": "повторный прогон по неполным логам",
    },
    "en": {
        "service": "Service header, not sent.",
        "svc_sessions": "Sessions reviewed: {n}",
        "svc_warn": "Warnings: none",
        "title": "### Session review, {label}",
        "coverage": "{name}, sessions reviewed in {label}: {n}, turns: {turns}.",
        "quick": "**If you read one thing: what to try differently.**",
        "first": ("**First.** In session `{sid}` (turn {turn}) a change went in without a test run "
                  "and had to be reverted. Worth trying: run the tests before the commit."),
        "second": ("**Second.** The task was set without a done criterion. Worth trying: one line "
                   "\"done when…\" at the start of the session."),
        "criteria": "**The eight criteria behind this.**",
        "evidence": "Seen in session `{sid}`, turn {turn}.",
        "dossier": "## Dossier (for follow-up questions, not sent)",
        "table": "| Date | Log file | session_id | Turns |",
        "quotes": "### Quotes behind the claims",
        "contacts": "Contact in the log: @fake-handle-{pid}, repo https://github.com/fake-{pid}/repo",
        "flags": "### Flags (internal)",
        "gate": "### Gate",
        "gate_yes": "Triggered: deletion without a backup.",
        "gate_no": "Not triggered.",
        "recs": "### Recommendations of this run",
        "rec_text": "Run the tests and read the whole diff before committing.",
        "summary": "Drives the agent confidently, result checks are uneven.",
        "note": "Observation on {axis} in period {label}.",
        "reading": "Checked the result personally instead of trusting the agent report.",
        "quote": "run the tests in /home/user/proj/{pid}/tests and show the output",
        "draft_para": ("Feedback draft paragraph: what the sessions of {label} showed, "
                       "where the agent went wrong and how it was caught. "),
        "why_retracted": "rerun on incomplete logs",
    },
}


def check_names(pairs) -> None:
    """ValueError, если id или основа слова имени — префикс ключа payload отчёта."""
    for pid, name in pairs:
        stems = {pid[:3].lower()} | {w[:3].lower() for w in name.split() if len(w) >= 3}
        for stem in stems:
            for key in PAYLOAD_KEYS:
                if key.startswith(stem):
                    raise ValueError(
                        f"имя фикстуры совпадает с ключом payload: основа {stem!r} "
                        f"у {pid!r} ({name}) — префикс ключа {key!r}"
                    )


check_names(NAMES + [EXTRA])


def person_index(pid: str) -> int:
    """Номер человека в фиксированном списке (для `me` — 0)."""
    if pid == SELF_ID:
        return 0
    for i, (known, _name) in enumerate(NAMES):
        if known == pid:
            return i
    raise KeyError(f"нет такого человека в списке фикстуры: {pid}")


def _i18n() -> dict:
    path = PLUGIN / "references" / "i18n.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _config(mode: str, lang: str) -> dict:
    ws_id = f"demo-{mode}"
    return {
        "schema_version": 1,
        "workspace": {
            "id": ws_id,
            "title": f"Demo {mode}",
            "groups": ["default"] if mode == "self" else ["team-a", "team-b"],
        },
        "mode": mode,
        "lang": lang,
        "period": {"mode": "labels"},
        "sources": [
            {"type": "specstory", "path": "/home/user/demo/.specstory/history"},
            {"type": "claude-jsonl", "project": "auto"},
        ],
        "hook": {"enabled": True, "providers": ["claude"]},
        "sanitize": {"names": [], "extra_patterns": []},
        "models": {
            "collector": {"model": "sonnet", "effort": "medium"},
            "judge": {"model": "opus", "effort": "high"},
            "critic": {"model": "sonnet", "effort": "medium"},
        },
        "batches": {"max_bytes": 100000, "max_user_turns": 150, "min_turns": 4},
        "show_scores": False,
        "reviewer_gate": mode != "self",
        "template": "default",
        "extensions": {"artifact": None},
    }


def _card(pid: str, name: str, group: str) -> str:
    return (
        f"id: {pid}\n"
        f"name: \"{name}\"\n"
        f"group: {group}\n"
        "feedback:\n"
        "  address: ты\n"
    )


def _score(i: int, p: int, ax: int) -> int:
    return 1 + (i + ax + (p - 1) // 2 + (ax * p) % 2) % 4


def _verdict_key(score: int | None) -> str:
    if score is None:
        return "not_visible"
    return {1: "first_place_above", 2: "first_place_above", 3: "even_but_one", 4: "even"}[score]


class _Builder:
    def __init__(self, root: Path, people: list[tuple[str, str]], periods: int, mode: str,
                 lang: str, realistic: bool) -> None:
        self.root = root
        self.state = root / STATE_DIR
        self.people = people
        self.periods = periods
        self.mode = mode
        self.lang = lang
        self.realistic = realistic
        self.ws_id = f"demo-{mode}"
        self.t = TEXT[lang]
        i18n = _i18n()
        self.axes = [k for k in i18n["axes"] if not k.startswith("_")]
        self.axis_titles = {k: v[lang] for k, v in i18n["axes"].items() if not k.startswith("_")}
        self.verdicts = {k: v[lang] for k, v in i18n["verdicts"].items() if not k.startswith("_")}

    # --- прогоны ---------------------------------------------------------------------

    def _sessions(self, pid: str, i: int, p: int, start: datetime) -> list[dict]:
        out = []
        for k in range(1, 3 + (i + p) % 3):
            day = (start + timedelta(days=k)).strftime("%Y-%m-%d")
            out.append({
                "session_id": f"s-{pid}-{p}-{k}",
                "path": f"/home/user/proj/{pid}/.specstory/history/{day}_session-{k}.md",
                "turns_total": 20 + 7 * k + i,
                "state": "new",
                "status": "reviewed",
            })
        return out

    @staticmethod
    def _skipped_sessions(pid: str, p: int, start: datetime) -> list[dict]:
        """Сессии, отсеянные до оценки: короткий лог и исключённая вручную."""
        day = start.strftime("%Y-%m-%d")
        return [{"session_id": f"s-{pid}-{p}-{kind}",
                 "path": f"/home/user/proj/{pid}/.specstory/history/{day}_{kind}.md",
                 "turns_total": turns, "state": "new", "status": status}
                for kind, turns, status in (("short", 2, "transcript_too_short"),
                                            ("manual", 30, "excluded_other"))]

    def _axes(self, pid: str, i: int, p: int, sessions: list[dict], ie_axis: str | None,
              xss: bool) -> list[dict]:
        per_axis = 3 if self.realistic else 1
        out = []
        for ax, axis in enumerate(self.axes):
            if axis == ie_axis:
                out.append({"id": axis, "score": None, "insufficient_evidence": True,
                            "insufficient_evidence_reason": "short_log", "evidence": [],
                            "subcriteria": [], "note": ""})
                continue
            evidence = []
            for q in range(per_axis):
                sess = sessions[(ax + q) % len(sessions)]
                quote = self.t["quote"].format(pid=pid)
                if q == 1:
                    quote = f"@fake-handle-{pid}: {quote}"
                if q == 2:
                    quote = f"{quote} (https://github.com/fake-{pid}/repo)"
                if xss and ax == 0 and q == 0:
                    quote = XSS_QUOTE
                evidence.append({"session_id": sess["session_id"], "turn_index": 3 + ax + q,
                                 "quote": quote, "reading": self.t["reading"]})
            out.append({"id": axis, "score": _score(i, p, ax), "insufficient_evidence": False,
                        "insufficient_evidence_reason": None, "evidence": evidence,
                        "subcriteria": [],
                        "note": self.t["note"].format(axis=axis, label=f"P{p}")})
        return out

    def _feedback_draft(self, label: str) -> str:
        para = self.t["draft_para"].format(label=label)
        target = 6200 if self.realistic else 300
        chunks = []
        while len("\n\n".join(chunks).encode("utf-8")) < target:
            chunks.append(para * 3)
        return "\n\n".join(chunks)

    def _run(self, pid: str, i: int, p: int, *, reviewed_at: datetime, revision: int = 1,
             ie_axis: str | None = None, xss: bool = False) -> dict:
        start = BASE_DATE + timedelta(days=PERIOD_DAYS * (p - 1))
        sessions = self._sessions(pid, i, p, start)
        label = f"P{p}"
        level = LEVELS[(i + p) % len(LEVELS)]
        run_id = f"{self.ws_id}_{pid}_{label}_{reviewed_at.strftime('%Y%m%dT%H%M%SZ')}"
        first = sessions[0]
        return {
            "run_id": run_id,
            "workspace": self.ws_id,
            "period": {"label": label, "from": start.strftime("%Y-%m-%d"), "to": None,
                       "anchor": None},
            "person_id": pid,
            "reviewed_at": _ts(reviewed_at),
            "reviewer": "judge@v1",
            "collector": "collector@v1",
            "batches": 1,
            "sessions_total": len(sessions),
            "tracker_version": 2,
            "sessions_reviewed": sessions,
            "sessions_skipped_known": [],
            "agent_stack": ["claude-code"] if i % 2 == 0 else ["claude-code", "codex"],
            "metrics_available": False,
            "context_report_available": False,
            "rules_audit_available": None,
            "warnings": [],
            "turns_total": sum(s["turns_total"] for s in sessions),
            "axes": self._axes(pid, i, p, sessions, ie_axis, xss),
            "antipatterns": [{"id": "accepts_without_check", "found": True, "axis": "verification",
                              "session_id": first["session_id"], "turn_index": 5,
                              "quote": self.t["quote"].format(pid=pid), "note": ""}],
            "flags": [{"id": "long_session", "session_id": first["session_id"], "turn_index": 1,
                       "note": ""}],
            "gating_triggered": False,
            "gating_evidence": [],
            "overall_level": level,
            "level_computed": level,
            "level_judge": level,
            "review_status": "approved",
            "task_solved": "yes",
            "summary": self.t["summary"],
            "recommendations": [],
            "prev_recommendations": [],
            "feedback_draft": self._feedback_draft(label),
            "revision": revision,
            "operator_override": None,
            "logs_index_marks": [],
            "artifact": None,
        }

    def build_runs(self) -> tuple[list[dict], set[str]]:
        """Прогоны трекера и run_id, у которых нет файла ревью (заменённые ревизии)."""
        runs: list[dict] = []
        no_review: set[str] = set()
        for i, (pid, _name) in enumerate(self.people):
            live_periods = [p for p in range(1, self.periods + 1) if not (i == 2 and p == 2)]
            last = live_periods[-1]
            prev_rec: str | None = None
            rec_no = 0
            verdict_no = 0
            for p in live_periods:
                start = BASE_DATE + timedelta(days=PERIOD_DAYS * (p - 1))
                reviewed = start + timedelta(days=PERIOD_DAYS - 1, hours=10, minutes=i)
                run = self._run(pid, i, p, reviewed_at=reviewed,
                                ie_axis="reversibility" if (i == 0 and p == last) else None,
                                xss=(i == 0 and p == 1))
                if i == 1 and p == 2:
                    run["operator_override"] = {"axes": {"verification": 4}}
                    run["gating_triggered"] = True
                    run["gating_evidence"] = [{
                        "trigger": "destructive_without_backup",
                        "session_id": run["sessions_reviewed"][0]["session_id"],
                        "turn_index": 7, "quote": "rm -rf /home/user/proj/build",
                    }]
                if i == 0 and p == 2:
                    run["sessions_reviewed"] += self._skipped_sessions(pid, p, start)
                    run["sessions_total"] = len(run["sessions_reviewed"])
                if i == 2 and p == 1:
                    run["level_computed"] = None
                    run["overall_level"] = None
                if prev_rec is not None:
                    verdict = (FIRST_VERDICTS[i % 3] if verdict_no == 0
                               else CYCLE_VERDICTS[(i + verdict_no) % len(CYCLE_VERDICTS)])
                    run["prev_recommendations"] = [{"id": prev_rec, "verdict": verdict,
                                                    "evidence": None, "note": ""}]
                    verdict_no += 1
                rec_no += 1
                prev_rec = f"REC-{pid}-{rec_no:03d}"
                run["recommendations"] = [{"id": prev_rec,
                                           "axis": self.axes[(i + p) % len(self.axes)],
                                           "text": self.t["rec_text"], "status": "open"}]
                run["review_status"] = "draft" if p == last else "approved"

                if i == 1 and p == 1:
                    old = self._run(pid, i, p, reviewed_at=reviewed - timedelta(hours=2))
                    old.update({k: run[k] for k in ("recommendations", "review_status")})
                    runs.append(old)
                    no_review.add(old["run_id"])
                    run["revision"] = 2
                runs.append(run)
                if i == 0 and p == 1:
                    twin = self._run(pid, i, p, reviewed_at=reviewed + timedelta(days=1))
                    twin["retracted"] = {"at": _ts(reviewed + timedelta(days=2)),
                                         "why": self.t["why_retracted"]}
                    runs.append(twin)
        return runs, no_review

    # --- ревью -----------------------------------------------------------------------

    def review_md(self, run: dict, name: str) -> str:
        t = self.t
        pid, label = run["person_id"], run["period"]["label"]
        sessions = run["sessions_reviewed"]
        status = run["review_status"] if not run.get("retracted") else "approved"
        first_name = name.split()[0]
        lines = [
            "---",
            f"person_id: {pid}",
            f"run_id: {run['run_id']}",
            f"period: {label}",
            f"status: {status}",
            f"lang: {self.lang}",
            "template: default",
            "---",
            "<!-- service -->",
            f"> **{t['service']}**",
            f"> - {t['svc_sessions'].format(n=len(sessions))}",
            f"> - run_id: `{run['run_id']}`",
            f"> - {t['svc_warn']}",
            "",
            "---",
            "",
            t["title"].format(label=label),
            "",
            t["coverage"].format(name=first_name, label=label, n=len(sessions),
                                 turns=run["turns_total"]),
            "",
            t["quick"],
            "",
            t["first"].format(sid=sessions[0]["session_id"], turn=5),
            "",
            t["second"],
            "",
            t["criteria"],
            "",
        ]
        for axis in run["axes"]:
            verdict = self.verdicts[_verdict_key(axis["score"])]
            line = f"**{self.axis_titles[axis['id']]}.** {verdict}"
            if axis["evidence"]:
                ev = axis["evidence"][0]
                line += " " + t["evidence"].format(sid=ev["session_id"], turn=ev["turn_index"])
            lines += [line, ""]
        lines += ["---", "", t["dossier"], "", t["table"], "|---|---|---|---|"]
        for s in sessions:
            day = s["path"].rsplit("/", 1)[-1][:10]
            lines.append(f"| {day} | {s['path']} | {s['session_id']} | {s['turns_total']} |")
        lines += ["", t["contacts"].format(pid=pid), "", t["quotes"], ""]
        quote_lines = []
        for axis in run["axes"]:
            for ev in axis["evidence"]:
                quote_lines.append(f"- `{ev['session_id']}` #{ev['turn_index']} "
                                   f"({axis['id']}): «{ev['quote']}» — {ev['reading']}")
        lines += quote_lines
        if self.realistic:
            pad = [q + " " + t["draft_para"].format(label=label) for q in quote_lines]
            while len("\n".join(lines).encode("utf-8")) < 20500:
                lines += pad
        lines += ["", t["flags"], ""]
        lines += [f"- `{f['id']}` `{f['session_id']}` #{f['turn_index']}" for f in run["flags"]]
        lines += ["", t["gate"], "", t["gate_yes"] if run["gating_triggered"] else t["gate_no"],
                  "", t["recs"], ""]
        lines += [f"- `{r['id']}` ({r['axis']}): {r['text']}" for r in run["recommendations"]]
        return "\n".join(lines) + "\n"

    # --- запись ----------------------------------------------------------------------

    def write(self) -> Path:
        state = self.state
        cfg = _config(self.mode, self.lang)
        check_config(cfg, state / "config.json")
        write_json_atomic(state / "config.json", cfg)
        write_json_atomic(state / "plugin-state.json", {
            "formatVersion": 1, "pluginVersion": __version__, "storeKind": "workspace",
            "migrations": [], "lastSync": None,
        })
        people_dir = state / "people"
        people_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PLUGIN / "templates" / "_default.yaml", people_dir / "_default.yaml")
        groups = cfg["workspace"]["groups"]
        cards = list(self.people) + ([] if self.mode == "self" else [EXTRA])
        for i, (pid, name) in enumerate(cards):
            (people_dir / f"{pid}.yaml").write_text(_card(pid, name, groups[i % len(groups)]),
                                                    encoding="utf-8")
        runs, no_review = self.build_runs()
        write_json_atomic(state / "runs" / "process-reviews.json", {
            "tracker_version": 2,
            "workspace": self.ws_id,
            "updated_at": max(r["reviewed_at"] for r in runs),
            "runs": runs,
        })
        names = dict(self.people)
        for run in runs:
            if run["run_id"] in no_review:
                continue
            folder = state / "reviews" / run["person_id"]
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{run['run_id']}.md").write_text(
                self.review_md(run, names[run["person_id"]]), encoding="utf-8")
        return self.root


def make_workspace(dst: Path, *, people: int = 3, periods: int = 3, mode: str = "team",
                   lang: str = "ru", realistic: bool = False) -> Path:
    """Пишет синтетический workspace в `dst` (корень проекта) и возвращает его путь."""
    if mode not in MODES:
        raise ValueError(f"неизвестный режим {mode!r}, ожидается один из {MODES}")
    if lang not in LANGS:
        raise ValueError(f"неизвестный язык {lang!r}, ожидается один из {LANGS}")
    if periods < 1:
        raise ValueError(f"periods должно быть ≥ 1, получено {periods}")
    if mode == "self":
        roster = [(SELF_ID, SELF_NAME[lang])]
    else:
        if not 1 <= people <= len(NAMES):
            raise ValueError(f"people должно быть от 1 до {len(NAMES)}, получено {people}")
        roster = NAMES[:people]
    root = Path(dst)
    if (root / STATE_DIR).exists():
        raise FileExistsError(f"в папке уже есть {STATE_DIR}: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return _Builder(root, roster, periods, mode, lang, realistic).write()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Синтетический workspace для отчётов.")
    parser.add_argument("--out", required=True, help="корень проекта, куда писать")
    parser.add_argument("--people", type=int, default=3)
    parser.add_argument("--periods", type=int, default=3)
    parser.add_argument("--mode", choices=MODES, default="team")
    parser.add_argument("--lang", choices=LANGS, default="ru")
    parser.add_argument("--realistic", action="store_true",
                        help="досье ≈ 20 КБ, черновик ≈ 6 КБ, 3 цитаты на ось")
    args = parser.parse_args(argv)
    try:
        root = make_workspace(Path(args.out), people=args.people, periods=args.periods,
                              mode=args.mode, lang=args.lang, realistic=args.realistic)
    except (ValueError, FileExistsError) as err:
        print(f"report_fixture: {err}", file=sys.stderr)
        return 1
    print(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
