"""Сборка HTML отчётов, страница workspaces и атомарная запись (`lib/render.py`)."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401  (кладёт scripts/ в sys.path)
import report_fixture
from _util import SCRIPTS
from lib import render
from lib import report_data as rd
from lib.workspace import SrError, ws_paths

DATA_RE = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)
NETWORK = [
    re.compile(r"<script[^>]*\bsrc\s*=", re.I),
    re.compile(r"<link[^>]*\bhref\s*=", re.I),
    re.compile(r"@import", re.I),
    re.compile(r"url\(\s*['\"]?\s*(?:https?:)?//", re.I),
    re.compile(r"<(?:img|iframe|object|embed)\b", re.I),
]


def _data_segment(html_text: str) -> str:
    found = DATA_RE.findall(html_text)
    if len(found) != 1:
        raise AssertionError(f"ожидался один блок данных, найдено {len(found)}")
    return found[0]


def _data(html_text: str) -> dict:
    return json.loads(_data_segment(html_text))


def _without_data(html_text: str) -> str:
    return DATA_RE.sub("", html_text)


CSP_RE = re.compile(r'<meta http-equiv="Content-Security-Policy" content="([^"]*)">')


def _csp(html_text: str) -> str:
    found = CSP_RE.findall(html_text)
    if len(found) != 1:
        raise AssertionError(f"ожидался один meta CSP, найдено {len(found)}")
    return found[0]


def _js_function(js: str, name: str) -> str:
    """Исходник верхнеуровневой функции report.js: от `function name(` до `}` в начале строки."""
    match = re.search(rf"^function {name}\(.*?^\}}$", js, re.S | re.M)
    if not match:
        raise AssertionError(f"в report.js нет функции {name}")
    return match.group(0)


def _run_node(script: str) -> dict:
    import subprocess
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise AssertionError(f"node упал ({res.returncode}): {res.stderr}")
    return json.loads(res.stdout)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = report_fixture.make_workspace(Path(cls._tmp.name) / "ws")
        cls.model = rd.collect(cls.root, lambda _msg: None)
        cls.i18n = rd.load_i18n()
        cls.first = cls.model["people"][0]["id"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def payload(self, mode: str, **kw) -> dict:
        if mode == "personal":
            kw.setdefault("person", self.first)
        kw.setdefault("title", "Отчёт")
        return rd.attach_html(rd.build_payload(self.model, mode=mode, **kw))


class RenderHtmlTest(_Base):
    def test_each_mode_embeds_payload_and_i18n(self):
        for mode in rd.MODES:
            with self.subTest(mode=mode):
                payload = self.payload(mode)
                html_text = render.render_html(payload, self.i18n)
                self.assertTrue(html_text.startswith("<!doctype html>"))
                self.assertIn(f'data-mode="{mode}"', html_text)
                self.assertEqual(_data(html_text), {**payload, "i18n": self.i18n})
                self.assertNotIn("i18n", payload, "payload вызывающего не должен меняться")
                self.assertIsNone(re.search(r"__[A-Z_]+__", html_text))
                self.assertIn("--s4:", html_text)  # CSS встроен
                self.assertIn("retropair-ui", html_text)  # JS встроен

    def test_header_dates_are_log_dates_with_log_wording(self):
        for mode in rd.MODES:
            with self.subTest(mode=mode):
                data = _data(render.render_html(self.payload(mode), self.i18n))
                self.assertEqual(data["meta"]["date_source"], "logs")
                self.assertEqual(data["meta"]["date_from"], "2026-01-05")
                self.assertEqual(data["meta"]["date_from"], data["people"][0]["points"][0]["date"])
                self.assertEqual("run_from" in data["meta"], mode == "reviewer")
                report = data["i18n"]["report"]
                self.assertEqual(report["logs_range"]["ru"], "Логи")
                self.assertEqual(report["tile_period_logs"]["ru"], "период логов")
                self.assertEqual(report["run_range"]["ru"], "оценка")

    def test_personal_html_carries_verdict_history_and_strings(self):
        data = _data(render.render_html(self.payload("personal", person="anna"), self.i18n))
        recs = {r["given_in"]: r for r in data["people"][0]["recs"]}
        self.assertEqual(recs["P1"]["verdicts"],
                         [{"period": "P2", "verdict": "followed", "comment": ""}])
        report = data["i18n"]["report"]
        self.assertEqual(report["rec_not_checked"]["ru"], "ещё не проверялась")
        self.assertTrue(report["rec_not_checked"]["en"])

    def test_no_network_resources(self):
        html_text = render.render_html(self.payload("reviewer"), self.i18n)
        for pat in NETWORK:
            with self.subTest(pattern=pat.pattern):
                self.assertIsNone(pat.search(html_text))
        self.assertEqual(html_text.count("<script"), 2)

    def test_csp_meta_forbids_network(self):
        html_text = render.render_html(self.payload("reviewer"), self.i18n)
        policy = _csp(html_text)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("script-src 'unsafe-inline'", policy)
        self.assertIn("style-src 'unsafe-inline'", policy)
        self.assertNotRegex(policy, r"https?:|\*")
        head = html_text.split("<style>", 1)[0]
        self.assertIn("Content-Security-Policy", head, "CSP должна стоять до стилей и скриптов")

    def test_lang_sets_document_language(self):
        html_text = render.render_html(self.payload("group", lang="en"), self.i18n)
        self.assertIn('<html lang="en"', html_text)
        html_ru = render.render_html(self.payload("group"), self.i18n)
        self.assertIn('<html lang="ru"', html_ru)

    def test_script_breakers_in_review_text_stay_inside_json(self):
        payload = self.payload("reviewer")
        evil = "</script><script>alert(1)</script><!-- x --> <b>&amp;"
        point = payload["people"][0]["points"][0]
        point["summary"] = evil
        payload["people"][0]["name"] = evil
        html_text = render.render_html(payload, self.i18n)
        segment = _data_segment(html_text)
        self.assertNotIn("<", segment)
        self.assertEqual(_data(html_text), {**payload, "i18n": self.i18n})
        self.assertEqual(html_text.count("<script"), 2)
        self.assertEqual(html_text.count("</script>"), 2)

    def test_placeholders_in_data_and_title_are_not_substituted(self):
        payload = self.payload("reviewer", title="__CSS__ <b>x</b> __DATA_JSON__")
        payload["people"][0]["points"][0]["summary"] = "__TITLE__ __JS__ __LANG__"
        html_text = render.render_html(payload, self.i18n)
        self.assertEqual(_data(html_text)["people"][0]["points"][0]["summary"],
                         "__TITLE__ __JS__ __LANG__")
        self.assertIn("<title>__CSS__ &lt;b&gt;x&lt;/b&gt; __DATA_JSON__</title>", html_text)
        self.assertEqual(html_text.count("retropair · стили отчёта"), 1)
        self.assertEqual(html_text.count("'use strict'"), 1)

    def test_bad_meta_is_an_error(self):
        payload = self.payload("group")
        payload["meta"]["lang"] = "de"
        with self.assertRaises(SrError) as ctx:
            render.render_html(payload, self.i18n)
        self.assertEqual(ctx.exception.code, 2)
        payload = self.payload("group")
        payload["meta"]["mode"] = "all"
        with self.assertRaises(SrError):
            render.render_html(payload, self.i18n)

    def test_assets_have_no_closing_tags_and_skeleton_uses_literal_keys(self):
        assets = SCRIPTS / "lib" / "report_assets"
        js = (assets / "report.js").read_text(encoding="utf-8")
        css = (assets / "report.css").read_text(encoding="utf-8")
        self.assertNotIn("</script", js.lower())
        self.assertNotIn("</style", css.lower())
        self.assertIsNone(re.search(r"(?<![\w.$])t\((?!['\"])", js))
        report_keys = self.i18n["report"]
        for key in re.findall(r"(?<![\w.$])t\(['\"]([a-z_]+)['\"]\)", js):
            with self.subTest(key=key):
                self.assertIn(key, report_keys)


JS_CALL_RE = re.compile(r"(?<![\w.$])t\(")
JS_LITERAL_KEY_RE = re.compile(r"(?<![\w.$])t\(['\"]([^'\"]*)['\"]\)")
# Слова собраны из кусков, чтобы гейт запрещённых слов не срабатывал на сам тест.
FORBIDDEN_WORDS = re.compile("(?i)" + "|".join([
    "мод" + "ул", "опер" + "атор", "студ" + "ент", r"\bку" + "рс", "teach" + "er",
    "stud" + "ent", "cour" + "se", "mod" + "ule"]))
# Возможности интерфейса, у каждой — своя строка (обзор, карточка, ревью, личный режим).
REQUIRED_UI_KEYS = {
    "not_for_distribution", "tile_people", "tile_runs", "tile_open_recs", "search",
    "filter_axis", "filter_period", "last_each", "heatmap", "distribution", "no_data",
    "no_data_share", "override", "gate", "charts", "recs", "reviews", "copy_sent",
    "copied", "copy_failed", "service_header", "reviewer_only", "dossier", "dossier_ref",
    "evidence", "antipatterns", "flags", "feedback_draft", "as_person", "as_reviewer",
    "transitions", "trans_up", "trans_down", "trans_flat", "trans_none", "trend_first", "theme", "theme_auto", "theme_light", "theme_dark", "lang",
    "reason_other", "run_trimmed", "filters",
}


class I18nCompletenessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assets = SCRIPTS / "lib" / "report_assets"
        cls.js = (assets / "report.js").read_text(encoding="utf-8")
        cls.assets = {p.name: p.read_text(encoding="utf-8")
                      for p in assets.iterdir() if p.suffix in {".js", ".css", ".html"}}
        cls.report = rd.load_i18n()["report"]

    def test_every_js_key_is_translated(self):
        keys = set(JS_LITERAL_KEY_RE.findall(self.js))
        self.assertTrue(keys)
        for key in sorted(keys):
            with self.subTest(key=key):
                self.assertRegex(key, r"^[a-z_]+$")
                value = self.report.get(key)
                self.assertIsNotNone(value, f"нет ключа report.{key}")
                self.assertTrue(value["ru"].strip() and value["en"].strip(), key)

    def test_t_is_called_only_with_literals(self):
        calls = len(JS_CALL_RE.findall(self.js))
        literals = len(JS_LITERAL_KEY_RE.findall(self.js))
        self.assertEqual(calls, literals, "в report.js есть t(…) с нелитеральным аргументом")
        self.assertIsNone(re.search(r"(?<![\w.$])t\((?!['\"])", self.js))

    def test_ui_features_have_strings(self):
        used = set(JS_LITERAL_KEY_RE.findall(self.js))
        self.assertEqual(REQUIRED_UI_KEYS - used, set())

    def test_no_unused_report_keys(self):
        render_src = (SCRIPTS / "lib" / "render.py").read_text(encoding="utf-8")
        used = set(JS_LITERAL_KEY_RE.findall(self.js))
        used |= set(re.findall(r"['\"](idx_[a-z_]+)['\"]", render_src))
        unused = {k for k in self.report if k not in used}
        self.assertEqual(unused, set(), "ключи report, которые никто не читает")

    def test_foreign_sections_read_directly(self):
        for section in ("axes", "levels", "rec_verdicts", "rec_statuses", "review_statuses"):
            with self.subTest(section=section):
                self.assertIn(f"I18N.{section}", self.js)
                for key in rd.load_i18n()[section]:
                    self.assertNotIn(key, self.report, f"{section}.{key} продублирован в report")

    def test_assets_have_no_forbidden_words(self):
        for name, text in self.assets.items():
            with self.subTest(asset=name):
                self.assertIsNone(FORBIDDEN_WORDS.search(text))
                self.assertNotIn("asr-ui", text)

    @unittest.skipUnless(shutil.which("node"), "node не установлен")
    def test_report_js_syntax(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.js"
            path.write_text(self.js, encoding="utf-8")
            res = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class ReportJsBehaviourTest(unittest.TestCase):
    """Отдельные функции report.js в node, со стабами окружения страницы."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (SCRIPTS / "lib" / "report_assets" / "report.js").read_text(encoding="utf-8")

    def _as_person(self, show_dynamics) -> list:
        person = {"id": "p1", "name": "P", "recs": [], "points": [
            {"period": "w1", "date": "2026-01-01", "sessions": 1, "axes": {"a": {"ie": False, "score": 2}},
             "transitions": {"a": "none"}, "review": None, "level": "l1"},
            {"period": "w2", "date": "2026-01-08", "sessions": 2, "axes": {"a": {"ie": False, "score": 3}},
             "transitions": {"a": "up"}, "review": None, "level": "l2"}]}
        if show_dynamics is not None:
            person["show_dynamics"] = show_dynamics
        script = (
            "const DATA = {axes: ['a']}; const META = {show_scores: false};\n"
            "const recStatus = r => r.status || 'open';\n"
            + _js_function(self.js, "personScores") + "\n"
            + _js_function(self.js, "personTransitions") + "\n"
            + _js_function(self.js, "asPersonModel")
            + f"\nprocess.stdout.write(JSON.stringify(asPersonModel({json.dumps(person)}).points));"
        )
        return _run_node(script)

    def _date_labels(self, meta: dict) -> dict:
        script = (
            "const t = k => k; const fmtDate = s => 'D' + s;\n"
            + _js_function(self.js, "rangeText") + "\n"
            + _js_function(self.js, "dateLabels")
            + f"\nprocess.stdout.write(JSON.stringify(dateLabels({json.dumps(meta)})));"
        )
        return _run_node(script)

    def test_date_labels_say_logs_when_dates_are_log_dates(self):
        got = self._date_labels({"date_source": "logs", "date_from": "2026-08-10",
                                 "date_to": "2026-09-06", "run_from": "2026-09-27",
                                 "run_to": "2026-09-28"})
        self.assertEqual(got, {"range": "logs_range", "tile": "tile_period_logs",
                               "dates": "D2026-08-10 – D2026-09-06",
                               "run": "D2026-09-27 – D2026-09-28"})

    def test_date_labels_fall_back_to_run_wording_without_log_dates(self):
        got = self._date_labels({"date_source": "run", "date_from": "2026-09-27",
                                 "date_to": "2026-09-27", "run_from": "2026-09-27",
                                 "run_to": "2026-09-27"})
        self.assertEqual(got, {"range": "period_range", "tile": "tile_period",
                               "dates": "D2026-09-27", "run": ""})

    def test_topbar_and_tile_use_date_labels(self):
        self.assertIn("dateLabels(", _js_function(self.js, "topbar"))
        self.assertIn("t('run_range')", _js_function(self.js, "topbar"))
        self.assertIn("dateLabels(", _js_function(self.js, "overview"))

    def test_as_person_drops_unapproved_points_and_recs(self):
        def point(period, score, approved):
            return {"period": period, "date": "2026-01-01", "sessions": 1, "approved": approved,
                    "axes": {"a": {"ie": False, "score": score}}, "transitions": {"a": "up"},
                    "review": None, "level": "l1"}

        person = {"id": "p1", "name": "P", "show_dynamics": True,
                  "points": [point("w1", 2, True), point("w2", 4, False), point("w3", 1, True)],
                  "recs": [
                      {"id": "r1", "axis": "a", "text": "old", "given_in": "w1", "open": False,
                       "pending_approval": False,
                       "verdicts": [{"period": "w1", "verdict": "partial", "note": "n1",
                                     "approved": True},
                                    {"period": "w2", "verdict": "followed", "note": "n2",
                                     "approved": False}]},
                      {"id": "r2", "axis": "a", "text": "draft", "given_in": "w2", "open": True,
                       "pending_approval": True, "verdicts": []}]}
        script = (
            "const DATA = {axes: ['a']}; const META = {show_scores: false};\n"
            + _js_function(self.js, "recStatus") + "\n"
            + _js_function(self.js, "personScores") + "\n"
            + _js_function(self.js, "personTransitions") + "\n"
            + _js_function(self.js, "asPersonModel")
            + f"\nprocess.stdout.write(JSON.stringify(asPersonModel({json.dumps(person)})));"
        )
        got = _run_node(script)
        self.assertEqual([p["period"] for p in got["points"]], ["w1", "w3"])
        self.assertEqual([p["transitions"]["a"] for p in got["points"]], ["none", "down"])
        self.assertEqual(got["recs"], [{"text": "old", "axis": "a", "given_in": "w1",
                                        "status": "open", "verdicts": [
                                            {"period": "w1", "verdict": "partial",
                                             "comment": "n1"}]}])

    def _chips(self, verdicts: list) -> str:
        script = (
            "const t = k => 'T:' + k; const recVerdictName = v => 'V:' + v;\n"
            + _js_function(self.js, "esc") + "\n"
            + "\n".join(ln for ln in self.js.splitlines()
                        if ln.startswith(("const CHIP_CLASS", "const CHIP_GLYPH"))) + "\n"
            + _js_function(self.js, "verdictChips")
            + f"\nprocess.stdout.write(JSON.stringify(verdictChips({json.dumps(verdicts)})));"
        )
        return _run_node(script)

    def test_verdict_chips_period_glyph_label_and_tip(self):
        html_text = self._chips([
            {"period": "P2", "verdict": "partial", "comment": "мало <тестов>"},
            {"period": "P3", "verdict": "followed", "comment": ""},
        ])
        chips = re.findall(r'<span class="chip ([a-z]+)"([^>]*)>([^<]*)</span>', html_text)
        self.assertEqual([(c, label) for c, _attrs, label in chips],
                         [("warn", "P2 ◐ V:partial"), ("good", "P3 ✓ V:followed")])
        self.assertIn('data-tip="мало &lt;тестов&gt;"', chips[0][1])
        self.assertIn('tabindex="0"', chips[0][1])
        self.assertNotIn("data-tip", chips[1][1])
        self.assertLess(html_text.index("P2"), html_text.index("P3"))

    def test_verdict_chips_reviewer_note_is_tip_too(self):
        html_text = self._chips([{"period": "P2", "verdict": "ignored", "note": "n", "approved": False}])
        self.assertIn('data-tip="n"', html_text)

    def test_verdict_chips_empty_is_not_checked(self):
        html_text = self._chips([])
        self.assertEqual(re.findall(r'<span class="chip[^"]*">([^<]*)</span>', html_text),
                         ["T:rec_not_checked"])

    def test_open_recs_counters_skip_draft_recs(self):
        tile = [ln for ln in self.js.splitlines() if "t('tile_open_recs')" in ln]
        self.assertEqual(len(tile), 1)
        self.assertIn("pending_approval", tile[0])
        body = _js_function(self.js, "recsCard")
        self.assertIn("pending_approval", body)
        self.assertIn("t('rec_from_draft')", body)

    def test_as_person_hides_transitions_when_dynamics_off(self):
        points = self._as_person(False)
        self.assertEqual(len(points), 2)
        for pt in points:
            self.assertNotIn("transitions", pt)
            self.assertNotIn("score", pt["axes"]["a"])

    def test_as_person_keeps_transitions_by_default(self):
        for flag in (True, None):
            with self.subTest(show_dynamics=flag):
                points = self._as_person(flag)
                self.assertEqual([p["transitions"]["a"] for p in points], ["none", "up"])

    def test_reason_other_has_its_own_string(self):
        script = (
            "const t = k => 'T:' + k; const humanize = id => String(id).replace(/_/g, ' ');\n"
            + _js_function(self.js, "reasonName")
            + "\nprocess.stdout.write(JSON.stringify([reasonName('other'), reasonName('short_log'),"
              " reasonName('some_new_code'), reasonName(null)]));"
        )
        self.assertEqual(_run_node(script),
                         ["T:reason_other", "T:reason_short_log", "some new code", "T:no_data"])

    def _ticks(self, labels: list) -> list:
        script = (
            _js_function(self.js, "periodTick") + "\n" + _js_function(self.js, "tickAnchor")
            + f"\nconst L = {json.dumps(labels)};"
            "\nprocess.stdout.write(JSON.stringify(L.map((l, i) => [periodTick(l, i), tickAnchor(i, L.length)])));"
        )
        return _run_node(script)

    def test_period_tick_run_labels_become_period_number_without_date(self):
        got = [tick for tick, _ in self._ticks(["run-20260927", "run-20260927-2", "run-20261231-14"])]
        self.assertEqual(got, ["№1", "№2", "№3"])
        self.assertFalse(any("." in tick for tick in got))

    def test_period_tick_ellipsis_only_for_long_labels(self):
        got = [tick for tick, _ in self._ticks(["P1", "2026-W38", "sprint-review-12", "run-2026"])]
        self.assertEqual(got, ["P1", "2026-W38", "sprint-…", "run-2026"])
        self.assertTrue(all(len(t) <= 8 for t in got))

    def test_tick_anchor_keeps_edge_labels_inside(self):
        self.assertEqual([a for _, a in self._ticks(["a", "b", "c"])], ["start", "middle", "end"])
        self.assertEqual([a for _, a in self._ticks(["a", "b"])], ["start", "end"])
        self.assertEqual([a for _, a in self._ticks(["a"])], ["middle"])

    def test_line_svg_short_ticks_with_full_label_in_title(self):
        pts = [{"period": p, "date": d, "axes": {"a": {"ie": False, "score": 2}}}
               for p, d in (("run-20260927", "2026-09-27"), ("run-20260927-2", "2026-09-27"),
                            ("run-20260928", "2026-09-28"))]
        script = (
            "const esc = s => String(s); const axName = a => a; const t = k => k;\n"
            "const fmtDate = s => s; const fmtShort = s => s.slice(8, 10) + '.' + s.slice(5, 7);\n"
            "const reasonName = r => r;\n"
            + _js_function(self.js, "periodTick") + "\n" + _js_function(self.js, "tickAnchor") + "\n"
            + _js_function(self.js, "lineSVG")
            + f"\nprocess.stdout.write(JSON.stringify(lineSVG({json.dumps(pts)}, 'a', 300, -1)));"
        )
        svg = _run_node(script)
        ticks = re.findall(r'<text class="tick-period"[^>]*text-anchor="(\w+)"[^>]*>'
                           r'<title>([^<]*)</title>([^<]*)</text>', svg)
        self.assertEqual(ticks, [("start", "run-20260927", "№1"),
                                 ("middle", "run-20260927-2", "№2"),
                                 ("end", "run-20260928", "№3")])
        dates = re.findall(r'<text class="tick-date"[^>]*text-anchor="(\w+)"', svg)
        self.assertEqual(dates, ["start", "middle", "end"])

    def test_chart_svg_clips_overflow(self):
        css = (SCRIPTS / "lib" / "report_assets" / "report.css").read_text(encoding="utf-8")
        rule = re.search(r"^\.chart svg \{([^}]*)\}", css, re.M)
        self.assertIsNotNone(rule)
        self.assertIn("overflow: hidden", rule.group(1))

    def test_person_heads_count_assessed_sessions(self):
        # одна карточка на ревьюера, адресата и группу
        body = _js_function(self.js, "personCard")
        self.assertIn("t('sessions_assessed')", body)
        self.assertNotIn("t('tile_sessions')", body)
        self.assertIn("t('sessions_skipped')", body)
        self.assertNotIn("function personalView", self.js)

    def test_retracted_reviews_shown_small_not_as_warning_chip(self):
        body = _js_function(self.js, "topbar")
        self.assertIn("m.reviews_of_retracted", body)
        line = [ln for ln in body.splitlines() if "t('retracted_reviews')" in ln]
        self.assertEqual(len(line), 1, body)
        self.assertNotIn("chip warn", line[0])
        self.assertIn("muted small", line[0])

    def test_trimmed_point_note_reads_point_flag(self):
        body = _js_function(self.js, "evidenceCard")
        self.assertIn("run.trimmed", body)
        self.assertIn("t('run_trimmed')", body)
        self.assertNotIn("quote_periods", body, "пометка — по флагу точки, а не по пересчёту лимита")


class NarrowScreenCssTest(unittest.TestCase):
    """Таблица в тексте ревью на узком экране (390 px) прокручивается в своём контейнере, а
    страница остаётся без горизонтального скролла: и в обёртке `.tbl`, и без неё."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = (SCRIPTS / "lib" / "report_assets" / "report.css").read_text(encoding="utf-8")

    def rules(self, selector: str) -> str:
        found = re.findall(rf"^{re.escape(selector)} \{{([^}}]*)\}}", self.css, re.M)
        self.assertTrue(found, f"нет правила {selector}")
        return " ".join(found)

    def test_wrapped_table_scrolls_in_wrapper(self):
        body = self.rules(".md .tbl")
        self.assertIn("overflow-x: auto", body)
        self.assertIn("max-width: 100%", body)
        self.assertIn("display: table", self.rules(".md .tbl table"))

    def test_bare_table_scrolls_itself(self):
        body = self.rules(".md table")
        for decl in ("display: block", "overflow-x: auto", "max-width: 100%"):
            with self.subTest(decl=decl):
                self.assertIn(decl, body)

    def test_text_block_can_shrink(self):
        self.assertIn("min-width: 0", self.rules(".md"))


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class PersonalRecsRenderTest(unittest.TestCase):
    """Личная страница, отрисованная report.js в node-заглушке DOM: история вердиктов видна."""

    def test_personal_page_draws_verdict_chips_with_comment_tip(self):
        import subprocess
        from test_report_group_details import NODE_HARNESS
        with tempfile.TemporaryDirectory() as tmp:
            root = report_fixture.make_workspace(Path(tmp) / "ws")
            path = ws_paths(root)["runs"] / "process-reviews.json"
            doc = json.loads(path.read_text(encoding="utf-8"))
            run = [r for r in doc["runs"] if r["person_id"] == "anna"
                   and r["period"]["label"] == "P2" and not r.get("retracted")][0]
            run["prev_recommendations"][0]["note"] = "Тесты теперь пишутся до правки."
            path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
            model = rd.collect(root, lambda _m: None)
            payload = rd.attach_html(rd.build_payload(model, mode="personal", person="anna",
                                                      title="Отчёт"))
            i18n = rd.load_i18n()
            data_path = Path(tmp) / "data.json"
            data_path.write_text(_data_segment(render.render_html(payload, i18n)),
                                 encoding="utf-8")
            js_path = SCRIPTS / "lib" / "report_assets" / "report.js"
            res = subprocess.run(["node", "-e", NODE_HARNESS, str(js_path), str(data_path)],
                                 capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        got = json.loads(res.stdout)
        self.assertEqual(got["warn"], [])
        first = got["first"]
        self.assertIn(i18n["report"]["verdict_history"]["ru"], first)
        followed = i18n["rec_verdicts"]["followed"]["ru"]
        chip = re.search(r'<span class="chip good"([^>]*)>P2 ✓ ' + followed + '</span>', first)
        self.assertIsNotNone(chip, first)
        self.assertIn('data-tip="Тесты теперь пишутся до правки."', chip.group(1))
        # рекомендация P2 проверялась только в черновике P3: чип «ещё не проверялась»
        self.assertIn(i18n["report"]["rec_not_checked"]["ru"], first)


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class CardSuggestionsRenderTest(unittest.TestCase):
    """Блок «Предложения к карточке»: только карточка человека в отчёте ревьюера."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        root = report_fixture.make_workspace(Path(cls._tmp.name) / "ws", suggestions=True)
        cls.model = rd.collect(root, lambda _m: None)
        cls.i18n = rd.load_i18n()
        cls.title = cls.i18n["report"]["sugg_title"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def page(self, mode: str, hash_: str, lang: str = "ru", actions=None, **kw) -> dict:
        from test_report_group_details import run_page
        payload = rd.attach_html(rd.build_payload(self.model, mode=mode, title="T", lang=lang,
                                                  **kw))
        got = run_page(payload, hash_, actions)
        self.assertEqual(got["warn"], [], "нет строк интерфейса")
        return got

    def test_reviewer_card_shows_pending_lines_and_commands(self):
        first = self.page("reviewer", "p-anna")["first"]
        self.assertIn(self.title["ru"], first)
        self.assertEqual(first.count('class="sugg"'), 2, first)
        for sid in ("S-anna-001", "S-anna-002"):
            self.assertIn(f"sr.py people apply anna {sid}", first)
            self.assertIn(f"sr.py people reject anna {sid}", first)
        rep = self.i18n["report"]
        self.assertIn(rep["sugg_f_tools"]["ru"], first)
        self.assertIn(rep["sugg_f_skip_basics"]["ru"], first)
        self.assertIn(rep["sugg_src_agent_stack"]["ru"], first)
        self.assertIn("claude-code", first)
        self.assertIn(report_fixture.SUGGEST_PROPOSED, first)
        self.assertIn(f"+ {report_fixture.SUGGEST_APPEND}, {report_fixture.SUGGEST_APPEND_2}", first)
        self.assertIn("→", first)
        for mark in report_fixture.SUGGEST_DECIDED:
            self.assertNotIn(mark, first)

    def test_reason_is_escaped(self):
        first = self.page("reviewer", "p-anna")["first"]
        self.assertIn("SuggestTool &lt;img src=x onerror=alert(1)&gt;", first)
        self.assertNotIn(report_fixture.XSS_QUOTE, first)

    def test_english_title(self):
        first = self.page("reviewer", "p-anna", lang="en")["first"]
        self.assertIn(self.title["en"], first)

    def test_no_pending_no_block(self):
        first = self.page("reviewer", "p-boris")["first"]
        self.assertNotIn(self.title["ru"], first)
        self.assertNotIn("sr.py people", first)

    def test_overview_and_as_person_have_no_block(self):
        self.assertNotIn(self.title["ru"], self.page("reviewer", "")["first"])
        label = self.i18n["report"]["as_person"]["ru"]
        snaps = self.page("reviewer", "p-anna", actions=[["button", label]])["snaps"]
        self.assertIn(self.title["ru"], snaps[0]["html"])
        self.assertNotIn(self.title["ru"], snaps[1]["html"])

    def test_personal_and_group_pages_have_no_block(self):
        for mode, kw in (("personal", {"person": "anna"}), ("group", {"details": True})):
            with self.subTest(mode=mode):
                first = self.page(mode, "p-anna", **kw)["first"]
                self.assertNotIn(self.title["ru"], first)
                self.assertNotIn("SuggestTool", first)

    def test_single_call_site_in_reviewer_branch(self):
        js = (SCRIPTS / "lib" / "report_assets" / "report.js").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"(?<![\w.$])suggestionsCard\(", js)), 2,
                         "определение и один вызов")
        call = [ln for ln in js.splitlines()
                if "suggestionsCard(" in ln and not ln.startswith("function ")]
        self.assertEqual(len(call), 1)
        self.assertIn("REVIEWER", call[0])
        for name in ("personView", "recsCard", "personCard"):
            self.assertNotIn("suggestionsCard", _js_function(js, name))

    def test_css_block_wraps_long_commands(self):
        css = (SCRIPTS / "lib" / "report_assets" / "report.css").read_text(encoding="utf-8")
        rule = re.search(r"^\.sugg-cmd code \{([^}]*)\}", css, re.M)
        self.assertIsNotNone(rule)
        self.assertIn("overflow-wrap: anywhere", rule.group(1))


class RenderIndexTest(_Base):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _entry(self, ws_id: str, path: Path, title: str, mode: str = "team",
               groups=None, last_run_at="2026-02-02T10:00:00Z") -> dict:
        return {"id": ws_id, "path": str(path), "title": title, "mode": mode,
                "groups": list(groups or []), "last_run_at": last_run_at,
                "last_collect_at": None}

    def test_two_workspaces_links_and_missing(self):
        built = self.base / "my ws"
        shutil.copytree(self.root, built)
        reports = ws_paths(built)["reports"]
        (reports / "personal").mkdir(parents=True)
        (reports / "reviewer.html").write_text("x", encoding="utf-8")
        (reports / "personal" / "anna.html").write_text("x", encoding="utf-8")
        broken = self.base / "broken"
        (broken / ".retropair").mkdir(parents=True)
        (broken / ".retropair" / "config.json").write_text("{not json", encoding="utf-8")
        gone = self.base / "gone"
        entries = [
            self._entry("my-ws", built, "Команда <script>x</script>"),
            self._entry("broken", broken, "Сломанный", groups=["reg-group"], last_run_at=None),
            self._entry("gone", gone, "Пропавший", mode="self"),
        ]
        rep = self.i18n["report"]
        page = render.render_index(entries, lang="ru", generated_at="2026-03-01", i18n=self.i18n)
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn('<html lang="ru"', page)
        uri = (reports / "reviewer.html").as_uri()
        self.assertIn("%20", uri)
        self.assertIn(f'href="{uri}"', page)
        self.assertIn(f'href="{(reports / "personal").as_uri()}"', page)
        self.assertNotIn((reports / "group.html").as_uri(), page)
        self.assertIn(rep["idx_not_built"]["ru"], page)
        self.assertIn("team-a", page)  # группы из конфига workspace
        self.assertIn("reg-group", page)  # группы из реестра при битом конфиге
        self.assertIn(rep["idx_groups_from_registry"]["ru"], page)
        self.assertIn(rep["idx_folder_missing"]["ru"], page)
        self.assertNotIn(gone.as_uri() + "/", page)
        self.assertNotIn("<script>x", page)
        self.assertIn("Команда &lt;script&gt;x&lt;/script&gt;", page)
        self.assertIn("2026-03-01", page)
        for pat in NETWORK:
            self.assertIsNone(pat.search(page), pat.pattern)
        self.assertNotIn("<script", page)

    def test_empty_registry_and_english(self):
        page = render.render_index([], lang="en", generated_at="2026-03-01", i18n=self.i18n)
        self.assertIn('<html lang="en"', page)
        self.assertIn(self.i18n["report"]["idx_empty"]["en"], page)

    def test_csp_meta_forbids_network_and_scripts(self):
        page = render.render_index([], lang="ru", generated_at="2026-03-01", i18n=self.i18n)
        policy = _csp(page)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("script-src 'none'", policy)
        self.assertIn("style-src 'unsafe-inline'", policy)
        self.assertNotRegex(policy, r"https?:|\*")
        self.assertIn("Content-Security-Policy", page.split("<style>", 1)[0])

    def test_entry_without_path_is_an_error(self):
        with self.assertRaises(SrError) as ctx:
            render.render_index([{"id": "x", "title": "X", "mode": "team"}], lang="ru",
                                generated_at="", i18n=self.i18n)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("x", ctx.exception.message)

    def test_idx_keys_used_by_render_exist(self):
        source = (SCRIPTS / "lib" / "render.py").read_text(encoding="utf-8")
        keys = set(re.findall(r"['\"](idx_[a-z_]+)['\"]", source))
        self.assertTrue(keys)
        for key in sorted(keys):
            with self.subTest(key=key):
                value = self.i18n["report"].get(key)
                self.assertIsNotNone(value)
                self.assertTrue(value["ru"] and value["en"])


class WriteHtmlAtomicTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_writes_without_leftovers_and_returns_bytes(self):
        out = self.base / "a b" / "reports" / "x.html"
        text = "<!doctype html>" + "й" * 3000
        size = render.write_html_atomic(out, text)
        self.assertEqual(size, len(text.encode("utf-8")))
        self.assertEqual(out.read_text(encoding="utf-8"), text)
        size2 = render.write_html_atomic(out, text + "!")
        self.assertEqual(size2, size + 1)
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ["x.html"])

    def test_too_short_is_an_error_and_writes_nothing(self):
        out = self.base / "short.html"
        with self.assertRaises(SrError) as ctx:
            render.write_html_atomic(out, "<p>x</p>")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(str(out), ctx.exception.message)
        self.assertFalse(out.exists())
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertEqual(render.write_html_atomic(out, "<p>x</p>", min_bytes=0), 8)


if __name__ == "__main__":
    unittest.main()
