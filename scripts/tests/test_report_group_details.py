"""Карточка человека в групповом отчёте (`group.html`) и вкладка «все» в текстах ревью.

По клику на человека в группе: графики осей (как раньше) + «Фидбэк по периодам» (вкладки
периодов и «все») + «Рекомендации и что с ними стало» с историей вердиктов. Данные карточки
(`people[].details`) — белым списком: только одобренные прогоны, одобренный отправляемый блок,
рекомендации и вердикты одобренных прогонов с очищенным пояснением. Досье, доказательств,
антипаттернов, флагов, черновиков, служебных блоков, путей, баллов и уровней в них нет.
Включено по умолчанию; выключается `report --group --no-details` или
`config.report.group_details: false`. Фикстура team: у каждого человека последний период —
черновик (`reviewer_gate: true`).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _util
import report_fixture
from _util import SCRIPTS, run_sr
from lib import render
from lib import report_data as rd
from lib.workspace import SrError, ws_paths

DATA_RE = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
DRAFT_REC_TEXT = "Текст рекомендации из черновика, адресат его не получал."
DRAFT_REVIEW_TEXT = "Абзац чернового ревью, который никто не отправлял."
DETAILS_KEYS = {"feedback_intro", "feedback_periods", "feedback_periods_hint", "recs_outcome",
                "reviews_all", "all_periods", "rec_given_selected"}
REMOVED_KEYS = {"mode_feedback", "idx_report_feedback", "participant"}


def _keys(value, out: set | None = None) -> set:
    out = set() if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            out.add(key)
            _keys(item, out)
    elif isinstance(value, list):
        for item in value:
            _keys(item, out)
    return out


def _data(html_text: str) -> dict:
    found = DATA_RE.findall(html_text)
    if len(found) != 1:
        raise AssertionError(f"ожидался один блок данных, найдено {len(found)}")
    return json.loads(found[0])


def _details_text(data: dict) -> str:
    """Только данные карточек (`people[].details`) одной строкой JSON."""
    return json.dumps([p.get("details") for p in data["people"]], ensure_ascii=False)


def _tracker_path(root: Path) -> Path:
    return ws_paths(root)["runs"] / "process-reviews.json"


def _edit_runs(root: Path, fn) -> None:
    path = _tracker_path(root)
    doc = json.loads(path.read_text(encoding="utf-8"))
    fn(doc)
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def _live_run(doc: dict, pid: str, period: str) -> dict:
    return [r for r in doc["runs"] if r["person_id"] == pid and r["period"]["label"] == period
            and not r.get("retracted")][-1]


def _plant_drafts(root: Path) -> None:
    """У anna в черновом P3: своя рекомендация и абзац в отправляемом блоке ревью."""
    _edit_runs(root, lambda doc: _live_run(doc, "anna", "P3")["recommendations"][0].update(
        text=DRAFT_REC_TEXT))
    for path in (ws_paths(root)["reviews"] / "anna").glob("*.md"):
        text = path.read_text(encoding="utf-8")
        if "period: P3" in text and "status: draft" in text:
            head = "### Ревью по сессиям, P3"
            path.write_text(text.replace(head, head + "\n\n" + DRAFT_REVIEW_TEXT, 1),
                            encoding="utf-8")
            return
    raise AssertionError("нет чернового ревью P3 у anna")


def _plant_in_approved_review(root: Path, pid: str, sentence: str) -> None:
    for path in sorted((ws_paths(root)["reviews"] / pid).glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if "status: approved" in text and "period: P2" in text:
            head = "### Ревью по сессиям, P2"
            path.write_text(text.replace(head, head + "\n\n" + sentence, 1), encoding="utf-8")
            return
    raise AssertionError(f"нет одобренного ревью P2 у {pid}")


def _plant_verdict_note(root: Path, pid: str, period: str, note: str) -> None:
    """Пояснение первого вердикта в живом прогоне `period` человека `pid`."""
    def edit(doc):
        run = _live_run(doc, pid, period)
        if not run.get("prev_recommendations"):
            raise AssertionError(f"у {pid} в {period} нет вердиктов")
        run["prev_recommendations"][0]["note"] = note
    _edit_runs(root, edit)


def _all_drafts(pid: str | None = None):
    def edit(doc):
        for run in doc["runs"]:
            if pid is None or run["person_id"] == pid:
                run["review_status"] = "draft"
    return edit


def _comments(data: dict) -> list[str]:
    return [v["comment"] for p in data["people"] for r in (p.get("details") or p)["recs"]
            for v in r["verdicts"] if v["comment"]]


HOME_NOTE = ("Проверено по /home/user/proj/.sessionretro/logs/p1/x.md, "
             "правка в /home/user/proj/app/main.py.")
HOME_NOTE_SHOWN = "Проверено по logs/p1/x.md, правка в main.py."
# синтетическое имя из `sanitize.names`: его нет ни в ростере, ни в фикстуре
SANITIZED_NAME = "Тимофей Ракитин"
SANITIZED_SENTENCE = "Настройку помог сделать Тимофей Ракитин."


def _leak_patterns(people: list[tuple[str, str]]) -> list[tuple[str, re.Pattern]]:
    out = []
    for pid, name in people:
        out.append((pid, re.compile(r"(?<![A-Za-z0-9])" + re.escape(pid) + r"(?![A-Za-z0-9])",
                                    re.I)))
        for word in name.split():
            stem = word[:-1] if len(word) >= 4 else word
            stem = re.sub("[её]", "[её]", re.escape(stem))
            out.append((word, re.compile(r"(?<!\w)" + stem + r"[а-яё]{0,3}(?!\w)", re.I)))
    return out


# --- данные -------------------------------------------------------------------------------

class _Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = report_fixture.make_workspace(Path(cls._tmp.name) / "ws")
        cls.model = rd.collect(cls.root, lambda _m: None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def fresh(self, **kwargs) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return report_fixture.make_workspace(Path(tmp.name) / "ws", **kwargs)

    def group(self, model: dict | None = None, **kw) -> dict:
        kw.setdefault("title", "Группа")
        kw.setdefault("details", True)
        return rd.build_payload(model or self.model, mode="group", **kw)


class AddresseeCommentTest(unittest.TestCase):
    """Пояснение вердикта для адресата: пути машины → имя файла или `logs/<id>/<file>`, логин ОС
    не остаётся; остальной текст как есть."""

    def clean(self, text: str) -> str:
        from lib.sanitize import Sanitizer
        return rd.addressee_comment(text, Sanitizer(local_users=["ivanov"]))

    def test_state_dir_path_becomes_logs_relative(self):
        self.assertEqual(self.clean("см. /home/user/proj/.sessionretro/logs/p1/x.md"),
                         "см. logs/p1/x.md")
        self.assertEqual(self.clean("см. .sessionretro/logs/p1/x.md"), "см. logs/p1/x.md")

    def test_path_with_spaces_and_login(self):
        # каталог macOS собран из частей, как в test_sanitize: гейт репозитория ловит его в исходниках
        got = self.clean("см. /" + "Users/ivanov/Local Documents /Projects/demo/.sessionretro/logs/"
                         "p1/x.md, ход 19")
        self.assertEqual(got, "см. logs/p1/x.md, ход 19")

    def test_other_absolute_paths_keep_file_name(self):
        self.assertEqual(self.clean("правка в /home/ivanov/proj/app/main.py."),
                         "правка в main.py.")
        self.assertEqual(self.clean("файл C:\\" + "Users\\Ivan Petrov\\proj\\notes.md"),
                         "файл notes.md")
        self.assertEqual(self.clean("лог /tmp/run/out.txt"), "лог out.txt")

    def test_bare_login_removed(self):
        self.assertNotIn("ivanov", self.clean("запускал под ivanov в терминале"))

    def test_urls_relative_paths_and_plain_text_unchanged(self):
        text = "см. src/app/x.py, https://example.org/a/b и 1/2 теста; ход 19"
        self.assertEqual(self.clean(text), text)
        self.assertEqual(self.clean(""), "")


class GroupDetailsPayloadTest(_Model):
    def test_feedback_mode_is_gone(self):
        self.assertNotIn("feedback", rd.MODES)
        with self.assertRaises(SrError) as ctx:
            rd.build_payload(self.model, mode="feedback")
        self.assertEqual(ctx.exception.code, 2)

    def test_without_details_no_card_data(self):
        payload = rd.build_payload(self.model, mode="group", title="Группа")
        self.assertIs(payload["meta"]["details"], False)
        for person in payload["people"]:
            self.assertNotIn("details", person)

    def test_details_only_for_group(self):
        with self.assertRaises(SrError) as ctx:
            rd.build_payload(self.model, mode="reviewer", details=True)
        self.assertEqual(ctx.exception.code, 2)

    def test_exact_whitelisted_keys(self):
        payload = self.group()
        self.assertIs(payload["meta"]["details"], True)
        self.assertEqual([p["id"] for p in payload["people"]], ["anna", "boris", "vera"])
        reviews = verdicts = 0
        for person in payload["people"]:
            details = person["details"]
            self.assertEqual(set(details), {"periods", "recs"})
            for pt in details["periods"]:
                self.assertEqual(set(pt), {"period", "date", "review"})
                if pt["review"] is not None:
                    self.assertEqual(set(pt["review"]), {"sent_md"})
                    reviews += 1
            for rec in details["recs"]:
                self.assertEqual(set(rec), {"text", "axis", "given_in", "status", "verdicts"})
                for verdict in rec["verdicts"]:
                    self.assertEqual(set(verdict), {"period", "verdict", "comment"})
                    verdicts += 1
        self.assertTrue(reviews)
        self.assertTrue(verdicts)

    def test_no_forbidden_keys_in_details_even_with_show_scores(self):
        payload = self.group(show_scores=True)
        keys = _keys([p["details"] for p in payload["people"]])
        self.assertTrue(rd.PERSONAL_FORBIDDEN_KEYS <= rd.FEEDBACK_FORBIDDEN_KEYS)
        self.assertTrue(rd.SCORE_KEYS <= rd.FEEDBACK_FORBIDDEN_KEYS)
        self.assertFalse(keys & rd.FEEDBACK_FORBIDDEN_KEYS, keys & rd.FEEDBACK_FORBIDDEN_KEYS)
        for key in ("score", "level", "transitions", "sessions", "dossier_md", "service_md",
                    "path", "evidence", "antipatterns", "flags", "feedback_draft", "note"):
            self.assertNotIn(key, keys)

    def test_graphs_unchanged_by_details(self):
        plain = rd.build_payload(self.model, mode="group", title="Группа")
        full = self.group()
        for a, b in zip(plain["people"], full["people"]):
            b = dict(b)
            b.pop("details")
            self.assertEqual(a, b)

    def test_only_approved_periods_same_text_as_personal(self):
        by_id = {p["id"]: p["details"] for p in self.group()["people"]}
        self.assertEqual([pt["period"] for pt in by_id["anna"]["periods"]], ["P1", "P2"])
        self.assertEqual([pt["period"] for pt in by_id["vera"]["periods"]], ["P1"])
        for pid in ("anna", "boris", "vera"):
            with self.subTest(person=pid):
                personal = rd.build_payload(self.model, mode="personal", person=pid)["people"][0]
                self.assertEqual([pt["review"] for pt in by_id[pid]["periods"]],
                                 [pt["review"] for pt in personal["points"]])
                self.assertEqual(by_id[pid]["recs"], personal["recs"])

    def test_draft_run_text_and_recommendation_absent(self):
        root = self.fresh()
        _plant_drafts(root)
        model = rd.collect(root, lambda _m: None)
        dumped = json.dumps(self.group(model), ensure_ascii=False)
        self.assertNotIn(DRAFT_REC_TEXT, dumped)
        self.assertNotIn(DRAFT_REVIEW_TEXT, dumped)
        # ревьюер черновик видит — значит, он в модели есть
        self.assertIn(DRAFT_REC_TEXT, json.dumps(rd.build_payload(model, mode="reviewer"),
                                                 ensure_ascii=False))

    def test_approved_run_with_draft_review_file_has_no_text(self):
        root = self.fresh()
        _edit_runs(root, lambda doc: _live_run(doc, "anna", "P3").update(review_status="approved"))
        model = rd.collect(root, lambda _m: None)
        anna = [p for p in self.group(model)["people"] if p["id"] == "anna"][0]["details"]
        self.assertEqual([pt["period"] for pt in anna["periods"]], ["P1", "P2", "P3"])
        self.assertIsNone(anna["periods"][2]["review"])

    def test_person_without_approved_runs_not_shown_but_counted(self):
        root = self.fresh()
        _edit_runs(root, _all_drafts("boris"))
        model = rd.collect(root, lambda _m: None)
        payload = self.group(model)
        self.assertEqual(sorted(p["id"] for p in payload["people"]), ["anna", "vera"])
        self.assertEqual(payload["meta"]["awaiting_approval"], 1)

    def test_nobody_approved_is_not_an_error(self):
        root = self.fresh()
        _edit_runs(root, _all_drafts())
        model = rd.collect(root, lambda _m: None)
        payload = self.group(model)
        self.assertEqual(payload["people"], [])
        self.assertEqual(payload["meta"]["awaiting_approval"], 3)
        self.assertIsNone(payload["meta"]["date_from"])

    def test_verdict_comment_cleaned_reviewer_keeps_note(self):
        root = self.fresh()
        _plant_verdict_note(root, "anna", "P2", HOME_NOTE)
        model = rd.collect(root, lambda _m: None)
        self.assertEqual(_comments(self.group(model)), [HOME_NOTE_SHOWN])
        personal = rd.build_payload(model, mode="personal", person="anna")
        self.assertEqual(_comments(personal), [HOME_NOTE_SHOWN])
        reviewer = rd.build_payload(model, mode="reviewer")
        self.assertIn(HOME_NOTE, json.dumps(reviewer, ensure_ascii=False))

    def test_attach_html_gives_sent_html_only(self):
        payload = rd.attach_html(self.group())
        keys = _keys([p["details"] for p in payload["people"]])
        self.assertIn("sent_html", keys)
        self.assertFalse({k for k in keys if k.endswith("_md")})
        self.assertFalse({"dossier_html", "service_html"} & keys)


# --- отрисовка ----------------------------------------------------------------------------

# Минимальный DOM для report.js в node: элементы с детьми и атрибутами, сериализация в текст.
# argv: report.js, data.json, [hash], [действия JSON]. Действия: ["select", id, value],
# ["tab", номер вкладки], ["tab_text", начало подписи вкладки]; после каждого — снимок.
NODE_HARNESS = r"""
const fs = require('fs');
const [jsPath, dataPath, hash, actionsJson] = process.argv.slice(1);
const WARN = [];
class E {
  constructor(tag) { this.tagName = tag; this.children = []; this.attrs = {}; this._html = ''; this._text = null;
    this.className = ''; this.style = {}; this.id = ''; this.value = ''; this.hidden = false;
    this.classList = { toggle() {}, add() {}, remove() {} }; }
  set innerHTML(v) { this._html = String(v); this._text = null; this.children = []; }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = String(v); this._html = ''; this.children = []; }
  get textContent() { return this._text != null ? this._text : this._html + this.children.map(c => c.textContent).join(''); }
  get childNodes() { return this.children; }
  appendChild(c) { if (c.frag) c.children.forEach(x => this.children.push(x)); else this.children.push(c); return c; }
  insertAdjacentHTML(_pos, h) { const x = new E('#raw'); x._html = String(h); this.children.push(x); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener() {}
  querySelector() { return new E('a'); }
  querySelectorAll() { return []; }
  focus() {}
  get offsetWidth() { return 0; } get offsetHeight() { return 0; }
  html() {
    if (this.tagName === '#raw') return this._html;
    const attrs = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${v}"`).join('');
    const cls = this.className ? ` class="${this.className}"` : '';
    const id = this.id ? ` id="${this.id}"` : '';
    const val = this.tagName === 'option' || this.tagName === 'select' ? ` value="${this.value}"` : '';
    const text = this._text != null ? this._text : '';
    return `<${this.tagName}${id}${cls}${val}${attrs}>${this._html}${text}${this.children.map(c => c.html()).join('')}</${this.tagName}>`;
  }
  find(pred) { if (pred(this)) return this; for (const c of this.children) { const f = c.find ? c.find(pred) : null; if (f) return f; } return null; }
  findAll(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach(c => c.findAll && c.findAll(pred, out)); return out; }
}
const APP = new E('div'); const TIP = new E('div');
const DATA_EL = { textContent: fs.readFileSync(dataPath, 'utf8') };
global.document = {
  createElement: t => new E(t),
  createDocumentFragment: () => { const f = new E('#frag'); f.frag = true; return f; },
  getElementById: id => (id === 'data' ? DATA_EL : id === 'app' ? APP : id === 'tip' ? TIP : null),
  documentElement: new E('html'), addEventListener() {}, querySelectorAll: () => [], activeElement: null, title: '',
};
global.window = { addEventListener() {}, scrollTo() {}, innerWidth: 1200, innerHeight: 800 };
global.location = { hash: hash || '' };
global.localStorage = { getItem: () => null, setItem() {} };
global.requestAnimationFrame = () => 0; global.cancelAnimationFrame = () => {};
global.navigator = {};
console.warn = (...a) => WARN.push(a.join(' '));
eval(fs.readFileSync(jsPath, 'utf8'));
const tabsOf = () => APP.findAll(e => e.tagName === 'button' && e.attrs.role === 'tab');
const state = () => {
  const selects = {};
  APP.findAll(e => e.tagName === 'select').forEach(s => { selects[s.id] = { value: s.value, options: s.children.map(o => [o.value, o.textContent]) }; });
  return { html: APP.html(), tabs: tabsOf().map(b => [b.textContent, b.attrs['aria-selected']]), selects };
};
const out = { first: APP.html(), title: document.title, snaps: [state()] };
for (const act of JSON.parse(actionsJson || '[]')) {
  if (act[0] === 'select') {
    const s = APP.find(e => e.id === act[1]); if (!s) throw new Error('нет списка ' + act[1]);
    s.onchange({ target: { value: act[2] } });
  } else if (act[0] === 'tab') {
    const b = tabsOf()[act[1]]; if (!b) throw new Error('нет вкладки ' + act[1]); b.onclick();
  } else if (act[0] === 'tab_text') {
    const b = tabsOf().find(x => x.textContent.startsWith(act[1])); if (!b) throw new Error('нет вкладки ' + act[1]); b.onclick();
  } else if (act[0] === 'button') {
    const b = APP.find(e => e.tagName === 'button' && e.textContent === act[1]); if (!b) throw new Error('нет кнопки ' + act[1]); b.onclick();
  } else throw new Error('неизвестное действие ' + act[0]);
  out.snaps.push(state());
}
out.hash = location.hash;
out.warn = WARN;
process.stdout.write(JSON.stringify(out));
"""


def run_page(payload: dict, hash_: str = "", actions: list | None = None) -> dict:
    """Отрисовать payload скриптом отчёта в node; снимки после каждого действия."""
    html_text = render.render_html(payload, rd.load_i18n())
    data = DATA_RE.findall(html_text)[0]
    with tempfile.TemporaryDirectory() as tmp:
        data_path = Path(tmp) / "data.json"
        data_path.write_text(data, encoding="utf-8")
        js_path = SCRIPTS / "lib" / "report_assets" / "report.js"
        res = subprocess.run(["node", "-e", NODE_HARNESS, str(js_path), str(data_path), hash_,
                              json.dumps(actions or [])],
                             capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise AssertionError(f"node упал: {res.stderr}")
    return json.loads(res.stdout)


def _tab_labels(snap: dict) -> list[tuple[str, str]]:
    return [(re.split(r"\s|<", text, maxsplit=1)[0], sel) for text, sel in snap["tabs"]]


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class GroupCardRenderTest(_Model):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.i18n = rd.load_i18n()
        cls.ru = cls.i18n["report"]
        cls.all_ru = cls.ru["reviews_all"]["ru"]

    def payload(self, lang: str = "ru", model: dict | None = None, details: bool = True) -> dict:
        return rd.attach_html(self.group(model, lang=lang, details=details))

    def test_html_mode_and_data(self):
        payload = self.payload()
        html_text = render.render_html(payload, self.i18n)
        self.assertIn('data-mode="group"', html_text)
        self.assertEqual({k: v for k, v in _data(html_text).items() if k != "i18n"}, payload)

    def test_card_with_details_all_tab_by_default(self):
        got = run_page(self.payload(), "p-anna")
        self.assertEqual(got["warn"], [], "нет строк интерфейса или переводов")
        first = got["first"]
        for key in ("charts", "feedback_periods", "recs_outcome", "feedback_intro",
                    "verdict_history", "not_for_distribution"):
            with self.subTest(key=key):
                self.assertIn(self.ru[key]["ru"], first)
        # вкладки: «все» и одобренные периоды anna; по умолчанию «все» — оба текста подряд
        self.assertEqual(_tab_labels(got["snaps"][0]),
                         [(self.all_ru, "true"), ("P1", "false"), ("P2", "false")])
        self.assertIn("Ревью по сессиям, P1", first)
        self.assertIn("Анна, за период P2", first)
        self.assertLess(first.index("Ревью по сессиям, P1"), first.index("Анна, за период P2"))
        self.assertEqual(len(re.findall(r'<h3 class="review-period">', first)), 2)
        # чипы вердиктов: REC P1 проверена в P2, REC P2 — ещё нет
        self.assertIn("P2 ✓", first)
        self.assertIn(self.ru["rec_not_checked"]["ru"], first)
        self.assertIn("<h1>Анна Вершинина", first)

    def test_card_has_no_reviewer_blocks(self):
        first = run_page(self.payload(), "p-anna")["first"]
        for key in ("evidence", "antipatterns", "flags", "dossier", "feedback_draft",
                    "service_header", "copy_sent", "review_source", "as_person", "rec_from_draft",
                    "reviewer_only", "summary"):
            with self.subTest(key=key):
                self.assertNotIn(self.ru[key]["ru"], first)

    def test_period_selector_filters_tabs_and_highlights_recs(self):
        got = run_page(self.payload(), "p-anna", [["select", "f-gperiod", "0"],
                                                  ["tab_text", "P2"],
                                                  ["select", "f-gperiod", "all"]])
        sel = got["snaps"][0]["selects"]["f-gperiod"]
        self.assertEqual(sel["value"], "all")
        self.assertEqual([o[0] for o in sel["options"]], ["all", "0", "1"])
        self.assertEqual(sel["options"][0][1], self.ru["all_periods"]["ru"])
        self.assertTrue(sel["options"][1][1].startswith("P1"))
        mark = self.ru["rec_given_selected"]["ru"]
        self.assertNotIn(mark, got["first"])
        # период P1: вкладка P1, её текст, рекомендация P1 подсвечена
        p1 = got["snaps"][1]
        self.assertEqual(_tab_labels(p1)[1], ("P1", "true"))
        self.assertIn("Ревью по сессиям, P1", p1["html"])
        self.assertNotIn("Анна, за период P2", p1["html"])
        self.assertEqual(p1["html"].count(mark), 1)
        hl = re.findall(r'<div class="rec hl">(.*?)</div><div class="text">', p1["html"])
        self.assertEqual(len(hl), 1)
        self.assertIn("P1", hl[0])
        # вкладка P2 — текст P2, селектор остаётся на P1
        p2 = got["snaps"][2]
        self.assertIn("Анна, за период P2", p2["html"])
        self.assertNotIn("Ревью по сессиям, P1", p2["html"])
        # обратно «все»: оба текста, подсветки нет
        back = got["snaps"][3]
        self.assertEqual(_tab_labels(back)[0], (self.all_ru, "true"))
        self.assertIn("Ревью по сессиям, P1", back["html"])
        self.assertIn("Анна, за период P2", back["html"])
        self.assertNotIn(mark, back["html"])

    def test_without_details_only_charts(self):
        got = run_page(self.payload(details=False), "p-anna")
        first = got["first"]
        self.assertEqual(got["warn"], [])
        self.assertIn(self.ru["charts"]["ru"], first)
        for key in ("feedback_periods", "recs_outcome", "feedback_intro", "all_periods"):
            with self.subTest(key=key):
                self.assertNotIn(self.ru[key]["ru"], first)
        self.assertEqual(got["snaps"][0]["tabs"], [])
        self.assertNotIn("f-gperiod", first)
        self.assertNotIn("Анна, за период", first)

    def test_person_without_texts_shows_empty_cards(self):
        root = self.fresh()

        def no_recs(doc):
            for run in doc["runs"]:
                if run["person_id"] == "vera":
                    run["recommendations"] = []
                    run["prev_recommendations"] = []
        _edit_runs(root, no_recs)
        shutil.rmtree(ws_paths(root)["reviews"] / "vera")
        model = rd.collect(root, lambda _m: None)
        got = run_page(self.payload(model=model), "p-vera")
        self.assertEqual(got["warn"], [])
        self.assertIn(self.ru["no_reviews"]["ru"], got["first"])
        self.assertIn(self.ru["no_recs"]["ru"], got["first"])
        self.assertIn(self.ru["charts"]["ru"], got["first"])

    def test_awaiting_approval_tile_only_when_someone_waits(self):
        label = self.ru["tile_awaiting_approval"]["ru"]
        self.assertNotIn(label, run_page(self.payload())["first"])
        root = self.fresh()
        _edit_runs(root, _all_drafts("boris"))
        got = run_page(self.payload(model=rd.collect(root, lambda _m: None)))
        self.assertEqual(got["warn"], [])
        self.assertRegex(got["first"], re.escape(label) + r'</div><div class="value tnum">1<')
        self.assertNotIn("Борис", got["first"])

    def test_english_strings(self):
        got = run_page(self.payload(lang="en"), "p-anna")
        self.assertEqual(got["warn"], [])
        for key in ("feedback_periods", "recs_outcome", "reviews_all", "not_for_distribution"):
            with self.subTest(key=key):
                self.assertIn(self.ru[key]["en"], got["first"])

    def test_group_anon_banner(self):
        payload = self.payload()
        self.assertIn(self.ru["not_for_distribution"]["ru"], run_page(payload)["first"])
        payload["meta"]["anon"] = True
        first = run_page(payload)["first"]
        self.assertIn(self.ru["anon_banner"]["ru"], first)
        self.assertNotIn(self.ru["not_for_distribution"]["ru"], first)


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class AllTabRenderTest(_Model):
    """Вкладка «все» в личном отчёте и в карточке ревьюера — та же `reviewsCard`."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.all_ru = rd.load_i18n()["report"]["reviews_all"]["ru"]

    def test_personal_all_tab(self):
        payload = rd.attach_html(rd.build_payload(self.model, mode="personal", person="anna",
                                                  title="Отчёт"))
        got = run_page(payload, "", [["tab_text", self.all_ru]])
        self.assertEqual(got["warn"], [])
        # по умолчанию — последний период
        self.assertEqual(_tab_labels(got["snaps"][0]),
                         [(self.all_ru, "false"), ("P1", "false"), ("P2", "true")])
        self.assertNotIn("Ревью по сессиям, P1", got["first"])
        after = got["snaps"][1]["html"]
        self.assertEqual(_tab_labels(got["snaps"][1])[0], (self.all_ru, "true"))
        self.assertIn("Ревью по сессиям, P1", after)
        self.assertIn("Анна, за период P2", after)

    def test_reviewer_all_tab_marks_drafts(self):
        payload = rd.attach_html(rd.build_payload(self.model, mode="reviewer", title="Ревьюер"))
        got = run_page(payload, "p-anna", [["tab_text", self.all_ru]])
        self.assertEqual(got["warn"], [])
        labels = _tab_labels(got["snaps"][0])
        self.assertEqual(labels[0], (self.all_ru, "false"))
        self.assertEqual([x[0] for x in labels[1:]], ["P1", "P2", "P3"])
        after = got["snaps"][1]["html"]
        for period in ("P1", "P2", "P3"):
            self.assertIn(f"Ревью по сессиям, {period}", after)
        draft = rd.load_i18n()["review_statuses"]["draft"]["ru"]
        self.assertIn(draft, after)
        self.assertEqual(len(re.findall(r'<h3 class="review-period">', after)), 3)

    def test_reviewer_as_person_all_tab(self):
        payload = rd.attach_html(rd.build_payload(self.model, mode="reviewer", title="Ревьюер"))
        as_person = rd.load_i18n()["report"]["as_person"]["ru"]
        got = run_page(payload, "p-anna", [["button", as_person], ["tab_text", self.all_ru]])
        after = got["snaps"][2]["html"]
        self.assertIn("Ревью по сессиям, P1", after)
        self.assertIn("Анна, за период P2", after)
        self.assertNotIn("Ревью по сессиям, P3", after)


class GroupDetailsI18nTest(unittest.TestCase):
    def test_new_keys_in_both_languages(self):
        report = rd.load_i18n()["report"]
        for key in sorted(DETAILS_KEYS):
            with self.subTest(key=key):
                self.assertIn(key, report)
                self.assertTrue(report[key]["ru"].strip() and report[key]["en"].strip())

    def test_removed_keys_gone(self):
        report = rd.load_i18n()["report"]
        self.assertFalse(REMOVED_KEYS & set(report))

    def test_js_uses_strings_and_no_feedback_page(self):
        js = (SCRIPTS / "lib" / "report_assets" / "report.js").read_text(encoding="utf-8")
        for key in sorted(DETAILS_KEYS):
            with self.subTest(key=key):
                self.assertIn(f"t('{key}')", js)
        self.assertNotIn("FEEDBACK", js)
        self.assertNotIn("feedbackView", js)
        # карточка группы больше не обрывается на графиках
        self.assertNotIn("if (GROUP) return frag;", js)


# --- команда ------------------------------------------------------------------------------

class _Cli(_util.TempHome, unittest.TestCase):
    MODE = "team"

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-group-")
        self.base = Path(self._tmp.name).resolve()
        self.ws = report_fixture.make_workspace(self.base / "ws", people=3, periods=3,
                                                mode=self.MODE)
        self.reports = ws_paths(self.ws)["reports"]
        self.index = self.home / ".sessionretro" / "reports" / "index.html"

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def sr(self, *args: str):
        return run_sr(*args, home=self.home, cwd=self.ws)

    def ok(self, *args: str):
        res = self.sr("report", *args)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def page(self) -> dict:
        return _data((self.reports / "group.html").read_text(encoding="utf-8"))

    def set_config(self, **report) -> None:
        cfg_path = ws_paths(self.ws)["config"]
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["report"] = report
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


class GroupDetailsCliTest(_Cli):
    def test_default_run_group_has_details(self):
        self.ok()
        data = self.page()
        self.assertIs(data["meta"]["details"], True)
        self.assertEqual({p["id"]: [pt["period"] for pt in p["details"]["periods"]]
                          for p in data["people"]},
                         {"anna": ["P1", "P2"], "boris": ["P1", "P2"], "vera": ["P1"]})
        self.assertFalse((self.reports / "feedback.html").exists())

    def test_no_details_flag(self):
        self.ok("--group", "--no-details")
        data = self.page()
        self.assertIs(data["meta"]["details"], False)
        self.assertTrue(all("details" not in p for p in data["people"]))
        self.assertNotIn("sent_html", json.dumps(data["people"], ensure_ascii=False))

    def test_config_group_details_false(self):
        self.set_config(group_details=False)
        self.ok("--group")
        data = self.page()
        self.assertIs(data["meta"]["details"], False)
        self.assertTrue(all("details" not in p for p in data["people"]))

    def test_config_group_details_true_explicit(self):
        self.set_config(group_details=True)
        self.ok("--group")
        self.assertIs(self.page()["meta"]["details"], True)

    def test_config_group_details_bad_type_is_schema_error(self):
        self.set_config(group_details="no")
        res = self.sr("report", "--group")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("group_details", res.stderr)

    def test_config_report_unknown_key_is_schema_error(self):
        self.set_config(group_detail=False)
        res = self.sr("report", "--group")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("group_detail", res.stderr)

    def test_no_details_without_group_target_is_usage_error(self):
        res = self.sr("report", "--reviewer", "--no-details")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("--no-details", res.stderr)
        self.assertFalse((self.reports / "reviewer.html").exists())

    def test_feedback_flag_is_gone(self):
        res = self.sr("report", "--feedback")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("--feedback", res.stderr)
        self.assertFalse((self.reports / "feedback.html").exists())

    def test_show_scores_does_not_reach_details(self):
        self.ok("--group", "--show-scores")
        text = _details_text(self.page())
        self.assertNotIn('"score"', text)
        self.assertNotIn('"level"', text)

    def test_no_paths_dossier_or_evidence_in_details(self):
        self.ok("--group")
        text = _details_text(self.page())
        for leak in ("/home/user", "run_id", "dossier", "Досье", "@fake-handle",
                     "github.com/fake", "Служебная шапка", "Цитаты, на которых", "evidence"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text)

    def test_drafts_not_in_file(self):
        _plant_drafts(self.ws)
        self.ok("--group")
        text = (self.reports / "group.html").read_text(encoding="utf-8")
        self.assertNotIn(DRAFT_REC_TEXT, text)
        self.assertNotIn(DRAFT_REVIEW_TEXT, text)

    def test_lang_en(self):
        self.ok("--group", "--lang", "en")
        text = (self.reports / "group.html").read_text(encoding="utf-8")
        self.assertIn('<html lang="en"', text)
        self.assertIn("Group report", TITLE_RE.search(text).group(1))

    def test_everyone_draft_group_still_built(self):
        _edit_runs(self.ws, _all_drafts())
        self.ok("--group")
        self.assertTrue(all(p["details"] == {"periods": [], "recs": []}
                            for p in self.page()["people"]))

    def test_names_of_other_participants_allowed_group_not_gated(self):
        # гейт чужих имён — для личных; групповой им не проверяется
        _plant_in_approved_review(self.ws, "anna", "Как и Борису, стоит начать с тестов.")
        res = self.ok("--group")
        self.assertNotIn("чужие токены", res.stderr)
        self.assertIn("Борису", _details_text(self.page()))

    def set_sanitize_names(self, names: list[str]) -> None:
        cfg_path = ws_paths(self.ws)["config"]
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["sanitize"]["names"] = names
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def test_sanitize_name_in_review_refuses_group(self):
        # имена участников в группе законны, а `sanitize.names` — нет: файл не пишется, старый удалён
        self.ok("--group")
        self.set_sanitize_names([SANITIZED_NAME])
        _plant_in_approved_review(self.ws, "anna", SANITIZED_SENTENCE)
        res = self.sr("report", "--group")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("чужие токены", res.stderr)
        self.assertIn("sanitize.names", res.stderr)
        self.assertIn("старый файл удалён", res.stderr)
        self.assertFalse((self.reports / "group.html").exists())

    def test_sanitize_name_in_verdict_note_refuses_group(self):
        self.set_sanitize_names([SANITIZED_NAME])
        _plant_verdict_note(self.ws, "anna", "P2", SANITIZED_SENTENCE)
        res = self.sr("report", "--group", "--reviewer")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertFalse((self.reports / "group.html").exists())
        self.assertTrue((self.reports / "reviewer.html").exists(), "остальное собирается")

    def test_sanitize_name_only_in_review_group_without_details_built(self):
        # без карточек текста ревью в файле нет — совпадать нечему
        self.set_sanitize_names([SANITIZED_NAME])
        _plant_in_approved_review(self.ws, "anna", SANITIZED_SENTENCE)
        res = self.ok("--group", "--no-details")
        self.assertNotIn("чужие токены", res.stderr)
        self.assertTrue((self.reports / "group.html").exists())

    def test_sanitize_names_without_match_group_built(self):
        self.set_sanitize_names([SANITIZED_NAME])
        res = self.ok("--group")
        self.assertNotIn("чужие токены", res.stderr)
        self.assertIs(self.page()["meta"]["details"], True)

    def test_stale_feedback_page_removed_with_warning(self):
        stale = self.reports / "feedback.html"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("<html>старая страница</html>", encoding="utf-8")
        res = self.ok("--group")
        self.assertFalse(stale.exists())
        lines = [ln for ln in res.stderr.splitlines() if "feedback.html" in ln]
        self.assertEqual(len(lines), 1, res.stderr)
        self.assertTrue(lines[0].startswith("предупреждение: "), lines[0])

    def test_stale_feedback_page_kept_without_group_target(self):
        stale = self.reports / "feedback.html"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("<html>старая страница</html>", encoding="utf-8")
        self.ok("--reviewer")
        self.assertTrue(stale.exists())

    def test_verdict_comment_paths_cleaned_in_group_and_personal(self):
        _plant_verdict_note(self.ws, "anna", "P2", HOME_NOTE)
        self.ok("--group", "--personal", "anna")
        for path in (self.reports / "group.html", self.reports / "personal" / "anna.html"):
            with self.subTest(file=path.name):
                data = _data(path.read_text(encoding="utf-8"))
                self.assertEqual(_comments(data), [HOME_NOTE_SHOWN])
                text = _details_text(data) if path.name == "group.html" else json.dumps(
                    data["people"], ensure_ascii=False)
                self.assertNotIn("/home/", text)

    def test_draft_verdict_comment_not_in_group(self):
        _plant_verdict_note(self.ws, "anna", "P3", "Черновое пояснение про /home/user/proj/x.md")
        self.ok("--group")
        self.assertNotIn("Черновое пояснение",
                         (self.reports / "group.html").read_text(encoding="utf-8"))


class GroupAnonCliTest(_Cli):
    def test_anon_has_codes_texts_and_no_real_tokens(self):
        _plant_in_approved_review(self.ws, "anna",
                                  "Как и Борису Каменеву, пишите anna@example.com.")
        _plant_verdict_note(self.ws, "anna", "P2",
                            "Сделано вместе с Борисом Каменевым. " + HOME_NOTE)
        res = self.ok("--group", "--anon", "--json")
        self.assertNotRegex(res.stdout, r"anna|boris|vera")
        text = (self.reports / "group.html").read_text(encoding="utf-8")
        data = _data(text)
        self.assertIs(data["meta"].get("anon"), True)
        self.assertIs(data["meta"]["details"], True)
        self.assertEqual(len(data["people"]), 3)
        for person in data["people"]:
            self.assertRegex(person["id"], r"^p\d{2}$")
            self.assertTrue(person["details"]["periods"], "тексты остаются")
        comments = _comments(data)
        self.assertEqual(len(comments), 1, comments)
        self.assertIn("Сделано вместе с", comments[0])
        self.assertIn("logs/p1/x.md", comments[0])
        plain = dict(data)
        plain.pop("i18n", None)
        plain = json.dumps(plain, ensure_ascii=False)
        title = TITLE_RE.search(text).group(1)
        people = report_fixture.NAMES[:3] + [report_fixture.EXTRA]
        for label, pat in _leak_patterns(people):
            with self.subTest(token=label):
                self.assertIsNone(pat.search(plain), f"{label}: {pat.search(plain)}")
                self.assertIsNone(pat.search(title), title)
        for leak in ("anna@example.com", "/home/"):
            self.assertNotIn(leak, plain)

    def test_anon_roster_gate_checks_details(self):
        from lib import anon, cmd_report
        from lib.people import list_people
        model = rd.collect(self.ws, lambda _m: None)
        cards = {c["id"]: c for c in list_people(self.ws)}
        a = anon.Anonymizer(list(cards.values()))
        payload = a.payload(rd.build_payload(model, mode="group", title="Группа", details=True))
        self.assertEqual(cmd_report._roster_hits(payload, cards, []), [])
        recs = [r for p in payload["people"] for r in p["details"]["recs"]]
        recs[0]["text"] = "Сделано вместе с Борисом."
        self.assertTrue(cmd_report._roster_hits(payload, cards, []))

    def test_no_anon_meta_without_flag(self):
        self.ok("--group", "--anon")
        self.ok("--group")
        self.assertNotIn("anon", self.page()["meta"])


class GroupSelfTest(_Cli):
    MODE = "self"

    def test_self_default_run_has_no_group_and_no_details_error(self):
        self.ok()
        self.assertFalse((self.reports / "group.html").exists())
        res = self.sr("report", "--group")
        self.assertEqual(res.returncode, 1, res.stderr)


class IndexLinksTest(_Cli):
    def test_index_row_links_group_reviewer_personal_no_feedback(self):
        from lib import registry
        registry.register(self.ws, "Стенд", "team", ["team-a"])
        self.ok()
        page = self.index.read_text(encoding="utf-8")
        for target in (self.reports / "group.html", self.reports / "reviewer.html",
                       self.reports / "personal"):
            with self.subTest(target=target.name):
                self.assertIn(f'href="{target.as_uri()}"', page)
        self.assertNotIn("feedback.html", page)
        for lang in ("ru", "en"):
            self.assertNotIn("Фидбэк всех" if lang == "ru" else "Everyone's feedback", page)


if __name__ == "__main__":
    unittest.main()
