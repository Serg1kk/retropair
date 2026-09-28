"""Личный отчёт = карточка человека из отчёта ревьюера минус служебное; виды `scores` и `trend`.

Вид `scores` — графики «Оси во времени» с баллами 0–4 и уровень, как у ревьюера; `trend` — та же
карточка, но вместо баллов переходы ↑ лучше / → ровно / ↓ ниже / ◇ нет данных, без уровня.
Выбор вида: флаг запуска (`--scores|--trend`, совместимо `--show-scores|--hide-scores`) >
`feedback.show_scores` карточки человека > `config.show_scores`.

Что не показывается, того нет в файле: проверяется JSON внутри HTML, а не только отрисовка.
Отрисовка — report.js в node-заглушке DOM (`NODE_HARNESS` из теста карточки группы).
Фикстура team: у каждого человека последний период — черновик (`reviewer_gate: true`).
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import _util
import report_fixture
from _util import PLUGIN, SCRIPTS, run_sr
from lib import report_data as rd
from lib.workspace import ws_paths
from test_report_group_details import _plant_in_approved_review, run_page

DATA_RE = re.compile(r'<script type="application/json" id="data">(.*?)</script>', re.S)
MAIN_RE = re.compile(r"<main>(.*)</main>", re.S)
BACK_RE = re.compile(r'<p class="back">.*?</p>', re.S)
TEXT = report_fixture.TEXT["ru"]
# Тексты фикстуры, которых в личном файле нет ни в одном виде: главное по прогону, пояснения
# судьи под графиками, чтение и цитата доказательства, черновик фидбэка, служебная шапка и
# досье ревью, контакты из досье.
LEAKS = (
    TEXT["summary"],
    "Наблюдение по оси",
    TEXT["reading"],
    "/home/user/proj/",
    "Абзац черновика фидбэка",
    TEXT["service"],
    "@fake-handle",
    "Цитаты, на которых",
)
# Ключи, которых нет в личном payload ни в одном виде (сверх `PERSONAL_FORBIDDEN_KEYS`).
SERVICE_KEYS = {"note", "summary", "evidence", "dossier_md", "dossier_html", "dossier_ref",
                "feedback_draft", "service_md", "service_html", "overridden", "warnings",
                "antipatterns", "flags", "gating_evidence", "gate", "run_id", "path", "trimmed",
                "sessions_skipped", "approved", "pending_approval"}


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


def _file_data(path: Path) -> dict:
    """JSON из HTML-файла без словаря интерфейса."""
    found = DATA_RE.findall(path.read_text(encoding="utf-8"))
    if len(found) != 1:
        raise AssertionError(f"ожидался один блок данных, найдено {len(found)} ({path})")
    data = json.loads(found[0])
    data.pop("i18n", None)
    return data


def _set_config(ws: Path, **values) -> None:
    path = ws_paths(ws)["config"]
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.update(values)
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _set_card_scores(ws: Path, pid: str, value) -> None:
    """`feedback.show_scores` в карточке: блок `feedback:` в карточке фикстуры последний."""
    path = ws_paths(ws)["people"] / f"{pid}.yaml"
    text = path.read_text(encoding="utf-8")
    if not text.rstrip().endswith("address: ты"):
        raise AssertionError(f"карточка фикстуры изменилась, блок feedback не последний ({path})")
    path.write_text(text + f"  show_scores: {value}\n", encoding="utf-8")


# --- данные: build_payload ------------------------------------------------------------------

class _Model(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = report_fixture.make_workspace(Path(cls._tmp.name) / "ws")
        cls.model = rd.collect(cls.root, lambda _m: None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def personal(self, pid: str = "anna", **kw) -> dict:
        kw.setdefault("title", "Отчёт")
        return rd.build_payload(self.model, mode="personal", person=pid, **kw)


class PersonalPayloadTest(_Model):
    def test_scores_view_keys(self):
        payload = self.personal(show_scores=True)
        self.assertIs(payload["meta"]["show_scores"], True)
        person = payload["people"][0]
        self.assertEqual(set(person), {"id", "name", "points", "recs"})
        for pt in person["points"]:
            self.assertEqual(set(pt), {"period", "date", "sessions", "axes", "review",
                                       "agent_stack", "level"})
            for item in pt["axes"].values():
                self.assertEqual(set(item), {"ie", "reason", "score"})
        keys = _keys(payload)
        self.assertFalse(keys & rd.PERSONAL_FORBIDDEN_KEYS, keys & rd.PERSONAL_FORBIDDEN_KEYS)
        self.assertFalse(keys & SERVICE_KEYS, keys & SERVICE_KEYS)
        self.assertNotIn("transitions", keys, "в виде scores переходы не показываются")

    def test_trend_view_keys(self):
        payload = self.personal(show_scores=False)
        self.assertIs(payload["meta"]["show_scores"], False)
        for pt in payload["people"][0]["points"]:
            self.assertEqual(set(pt), {"period", "date", "sessions", "axes", "review",
                                       "agent_stack", "transitions"})
            for item in pt["axes"].values():
                self.assertEqual(set(item), {"ie", "reason"})
        keys = _keys(payload)
        self.assertFalse(keys & rd.SCORE_KEYS, keys & rd.SCORE_KEYS)
        self.assertFalse(keys & SERVICE_KEYS, keys & SERVICE_KEYS)

    def test_trend_without_dynamics_has_neither_scores_nor_transitions(self):
        keys = _keys(self.personal(show_scores=False, show_dynamics=False))
        self.assertFalse(keys & (rd.SCORE_KEYS | {"transitions"}))

    def test_score_is_final_after_override(self):
        root = Path(self._tmp.name) / "ov"
        report_fixture.make_workspace(root)
        path = ws_paths(root)["runs"] / "process-reviews.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        run = [r for r in doc["runs"] if r["person_id"] == "anna"
               and r["period"]["label"] == "P1" and not r.get("retracted")][0]
        run[rd.OVERRIDE_KEY] = {"axes": {"verification": 4}}
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        model = rd.collect(root, lambda _m: None)
        pt = rd.build_payload(model, mode="personal", person="anna",
                              show_scores=True)["people"][0]["points"][0]
        self.assertEqual(pt["axes"]["verification"], {"ie": False, "reason": None, "score": 4})

    def test_agent_stack_is_cleaned_like_verdict_comments(self):
        # агенты и модели — свободные строки коллектора и судьи: путь машины сводится к имени
        # файла, как в пояснениях вердиктов (`addressee_comment`)
        root = Path(self._tmp.name) / "stack"
        report_fixture.make_workspace(root)
        path = ws_paths(root)["runs"] / "process-reviews.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for run in doc["runs"]:
            if run["person_id"] == "anna":
                run["agent_stack"] = ["/home/user/tools/agents/opus-judge",
                                      "claude-code (/Volumes/ext/models/sonnet.json)"]
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        model = rd.collect(root, lambda _m: None)
        for scores in (True, False):
            with self.subTest(show_scores=scores):
                payload = rd.build_payload(model, mode="personal", person="anna",
                                           show_scores=scores)
                dumped = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn("/home/", dumped)
                self.assertNotIn("/Volumes/", dumped)
                self.assertEqual(payload["people"][0]["points"][0]["agent_stack"],
                                 ["opus-judge", "claude-code (sonnet.json)"])
        reviewer = rd.build_payload(model, mode="reviewer", show_scores=True)
        anna = [p for p in reviewer["people"] if p["id"] == "anna"][0]
        self.assertEqual(anna["points"][0]["agent_stack"],
                         ["opus-judge", "claude-code (sonnet.json)"],
                         "«Как видит адресат» берёт агентов из payload ревьюера")

    def test_agent_stack_not_strings_is_error(self):
        root = Path(self._tmp.name) / "stack-bad"
        report_fixture.make_workspace(root)
        path = ws_paths(root)["runs"] / "process-reviews.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["runs"][0]["agent_stack"] = ["claude-code", 7]
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(rd.SrError) as caught:
            rd.collect(root, lambda _m: None)
        self.assertIn("agent_stack", str(caught.exception))
        self.assertIn(doc["runs"][0]["run_id"], str(caught.exception))

    def test_card_value_beats_default_cli_beats_card(self):
        model = json.loads(json.dumps(self.model))
        anna = [p for p in model["people"] if p["id"] == "anna"][0]
        anna["show_scores"] = False
        got = rd.build_payload(model, mode="personal", person="anna", show_scores=True)
        self.assertIs(got["meta"]["show_scores"], False)
        self.assertFalse(_keys(got) & rd.SCORE_KEYS)
        got = rd.build_payload(model, mode="personal", person="anna", show_scores=False,
                               scores_cli=True)
        self.assertIs(got["meta"]["show_scores"], True)
        anna["show_scores"] = True
        got = rd.build_payload(model, mode="personal", person="anna", show_scores=False)
        self.assertIs(got["meta"]["show_scores"], True)
        got = rd.build_payload(model, mode="personal", person="anna", show_scores=True,
                               scores_cli=False)
        self.assertIs(got["meta"]["show_scores"], False)

    def test_reviewer_person_carries_resolved_view(self):
        model = json.loads(json.dumps(self.model))
        [p for p in model["people"] if p["id"] == "boris"][0]["show_scores"] = False
        people = {p["id"]: p for p in rd.build_payload(model, mode="reviewer",
                                                       show_scores=True)["people"]}
        self.assertEqual({pid: p["show_scores"] for pid, p in people.items()},
                         {"anna": True, "boris": False, "vera": True})
        people = {p["id"]: p for p in rd.build_payload(model, mode="reviewer", show_scores=True,
                                                       scores_cli=True)["people"]}
        self.assertTrue(all(p["show_scores"] for p in people.values()))

    def test_model_reads_card_value_or_none(self):
        self.assertEqual({p["id"]: p["show_scores"] for p in self.model["people"]},
                         {"anna": None, "boris": None, "vera": None})

    def test_group_details_have_no_agent_stack(self):
        payload = rd.build_payload(self.model, mode="group", details=True)
        self.assertIn("agent_stack", rd.FEEDBACK_FORBIDDEN_KEYS)
        self.assertFalse(_keys([p["details"] for p in payload["people"]])
                         & rd.FEEDBACK_FORBIDDEN_KEYS)


# --- команда: JSON внутри HTML ----------------------------------------------------------------

class _Cli(_util.TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-personal-view-")
        self.ws = report_fixture.make_workspace(Path(self._tmp.name).resolve() / "ws")
        self.reports = ws_paths(self.ws)["reports"]

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def sr(self, *args: str):
        return run_sr("report", *args, home=self.home, cwd=self.ws)

    def ok(self, *args: str):
        res = self.sr(*args)
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def personal(self, pid: str = "anna") -> dict:
        return _file_data(self.reports / "personal" / f"{pid}.html")

    def assert_view(self, data: dict, view: str) -> None:
        keys = _keys(data)
        self.assertFalse(keys & rd.PERSONAL_FORBIDDEN_KEYS, keys & rd.PERSONAL_FORBIDDEN_KEYS)
        self.assertFalse(keys & SERVICE_KEYS, keys & SERVICE_KEYS)
        if view == "scores":
            self.assertIs(data["meta"]["show_scores"], True)
            self.assertTrue(rd.SCORE_KEYS <= keys, keys)
            self.assertNotIn("transitions", keys)
        else:
            self.assertIs(data["meta"]["show_scores"], False)
            self.assertFalse(keys & rd.SCORE_KEYS, keys & rd.SCORE_KEYS)
            self.assertIn("transitions", keys)


class PersonalViewCliTest(_Cli):
    def test_scores_file_has_no_service_texts(self):
        self.ok("--personal", "anna", "--scores")
        data = self.personal()
        self.assert_view(data, "scores")
        text = (self.reports / "personal" / "anna.html").read_text(encoding="utf-8")
        for leak in LEAKS:
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text)

    def test_trend_file_has_no_scores_and_no_service_texts(self):
        self.ok("--personal", "anna", "--trend")
        self.assert_view(self.personal(), "trend")
        text = (self.reports / "personal" / "anna.html").read_text(encoding="utf-8")
        for leak in LEAKS:
            with self.subTest(leak=leak):
                self.assertNotIn(leak, text)

    def test_default_follows_config(self):
        _set_config(self.ws, show_scores=False)
        self.ok("--personal", "anna")
        self.assert_view(self.personal(), "trend")
        _set_config(self.ws, show_scores=True)
        self.ok("--personal", "anna")
        self.assert_view(self.personal(), "scores")

    def test_compat_flags(self):
        _set_config(self.ws, show_scores=True)
        self.ok("--personal", "anna", "--hide-scores")
        self.assert_view(self.personal(), "trend")
        _set_config(self.ws, show_scores=False)
        self.ok("--personal", "anna", "--show-scores")
        self.assert_view(self.personal(), "scores")

    def test_view_flags_are_mutually_exclusive(self):
        for pair in (("--scores", "--trend"), ("--show-scores", "--trend"),
                     ("--scores", "--hide-scores"), ("--show-scores", "--hide-scores")):
            with self.subTest(flags=pair):
                res = self.sr("--personal", "anna", *pair)
                self.assertEqual(res.returncode, 2, res.stderr)
                self.assertIn("not allowed with argument", res.stderr)
        self.assertFalse((self.reports / "personal" / "anna.html").exists())

    def test_card_beats_config_cli_beats_card(self):
        _set_config(self.ws, show_scores=True)
        _set_card_scores(self.ws, "boris", "false")
        self.ok("--all-personal")
        self.assert_view(self.personal("anna"), "scores")
        self.assert_view(self.personal("boris"), "trend")
        self.ok("--all-personal", "--scores")
        self.assert_view(self.personal("boris"), "scores")
        _set_config(self.ws, show_scores=False)
        _set_card_scores(self.ws, "vera", "true")
        self.ok("--all-personal")
        self.assert_view(self.personal("vera"), "scores")
        self.assert_view(self.personal("anna"), "trend")
        self.ok("--all-personal", "--trend")
        self.assert_view(self.personal("vera"), "trend")

    def test_card_bad_value_is_an_error(self):
        _set_card_scores(self.ws, "boris", '"yes"')
        res = self.sr("--personal", "anna")
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("show_scores", res.stderr)

    def test_reviewer_file_carries_view_for_as_person(self):
        _set_config(self.ws, show_scores=False)
        _set_card_scores(self.ws, "anna", "true")
        self.ok("--reviewer")
        people = {p["id"]: p for p in _file_data(self.reports / "reviewer.html")["people"]}
        self.assertEqual({pid: p["show_scores"] for pid, p in people.items()},
                         {"anna": True, "boris": False, "vera": False})

    def test_other_name_gate_in_both_views(self):
        _plant_in_approved_review(self.ws, "anna", "Как и Борису, стоит начать с тестов.")
        for flag in ("--scores", "--trend"):
            with self.subTest(view=flag):
                res = self.sr("--all-personal", flag)
                self.assertEqual(res.returncode, 1, res.stderr)
                self.assertIn("чужие токены", res.stderr)
                self.assertFalse((self.reports / "personal" / "anna.html").exists())
                self.assertTrue((self.reports / "personal" / "boris.html").exists())

    def test_anon_trend_has_no_scores(self):
        self.ok("--all-personal", "--anon", "--trend", "--out-dir", str(self.ws / "anon"))
        files = sorted((self.ws / "anon" / "personal").glob("*.html"))
        self.assertEqual(len(files), 3)
        self.assertTrue(all(re.fullmatch(r"p\d{2}", f.stem) for f in files), files)
        for path in files:
            with self.subTest(file=path.name):
                self.assertFalse(_keys(_file_data(path)) & rd.SCORE_KEYS)


class ConfigDefaultTest(unittest.TestCase):
    def test_template_default_is_scores(self):
        cfg = json.loads((PLUGIN / "templates" / "config.template.json").read_text("utf-8"))
        self.assertIs(cfg["show_scores"], True)

    def test_person_schema_allows_optional_card_value(self):
        schema = json.loads((PLUGIN / "references" / "schemas" / "person.json")
                            .read_text("utf-8"))
        feedback = schema["properties"]["feedback"]
        self.assertEqual(feedback["properties"]["show_scores"], {"type": "boolean"})
        self.assertNotIn("show_scores", feedback["required"])


# --- отрисовка ----------------------------------------------------------------------------

def _main(html_text: str) -> str:
    found = MAIN_RE.search(html_text)
    if not found:
        raise AssertionError("в отрисовке нет <main>")
    return found.group(1)


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class PersonalCardRenderTest(_Model):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.i18n = rd.load_i18n()
        cls.ru = {k: v["ru"] for k, v in cls.i18n["report"].items()}

    def page(self, scores: bool, pid: str = "anna") -> dict:
        return run_page(rd.attach_html(self.personal(pid, show_scores=scores)))

    def assert_no_service(self, html_text: str) -> None:
        for key in ("summary", "as_person", "as_reviewer", "copy_sent", "review_source",
                    "service_header", "dossier", "reviewer_only", "evidence", "antipatterns",
                    "flags", "feedback_draft", "override", "expand", "sessions_skipped",
                    "rec_from_draft", "run_warnings", "gate"):
            with self.subTest(key=key):
                self.assertNotIn(self.ru[key], html_text)
        self.assertNotIn(self.i18n["review_statuses"]["draft"]["ru"], html_text)

    def test_scores_view_is_reviewer_card_without_service(self):
        got = self.page(True)
        self.assertEqual(got["warn"], [])
        first = got["first"]
        for key in ("charts", "legend_score", "level_last", "agent_stack", "tile_runs",
                    "sessions_assessed", "run", "filter_axis", "reviews", "recs",
                    "verdict_history"):
            with self.subTest(key=key):
                self.assertIn(self.ru[key], first)
        self.assertIn('data-axis="verification"', first)
        self.assertIn("claude-code", first, "агенты и модели — как у ревьюера")
        self.assertIn("f-run", json.dumps(got["snaps"][0]["selects"]))
        self.assert_no_service(first)

    def test_trend_view_is_new_card_not_old_table(self):
        got = self.page(False)
        self.assertEqual(got["warn"], [])
        first = got["first"]
        for key in ("transitions", "trans_up", "trans_down", "trans_flat", "trans_none",
                    "agent_stack", "run", "filter_axis", "reviews", "recs"):
            with self.subTest(key=key):
                self.assertIn(self.ru[key], first)
        self.assertIn('class="plot trend" data-axis="verification"', first)
        self.assertIn('class="charts"', first)
        self.assertNotIn('<table class="trend"', first)
        for key in ("level_last", "legend_score", "charts"):
            with self.subTest(key=key):
                self.assertNotIn(self.ru[key], first)
        self.assert_no_service(first)

    def test_run_selector_picks_review_tab(self):
        got = run_page(rd.attach_html(self.personal(show_scores=True)), "",
                       [["select", "f-run", "0"]])
        tabs = [(re.split(r"\s|<", t, maxsplit=1)[0], sel) for t, sel in got["snaps"][1]["tabs"]]
        self.assertIn(("P1", "true"), tabs)

    def test_as_person_equals_personal_in_both_views(self):
        as_person = self.ru["as_person"]
        toggle = re.compile(r'<div class="field"><label>&nbsp;</label><button[^>]*>'
                            + re.escape(self.ru["as_reviewer"]) + r"</button></div>")
        for scores in (True, False):
            with self.subTest(scores=scores):
                reviewer = rd.attach_html(rd.build_payload(self.model, mode="reviewer",
                                                           title="Ревьюер", show_scores=scores))
                seen = run_page(reviewer, "p-anna", [["button", as_person]])
                self.assertEqual(seen["warn"], [])
                addressee = _main(seen["snaps"][1]["html"])
                self.assertEqual(len(toggle.findall(addressee)), 1, "кнопка назад к ревьюеру")
                addressee = toggle.sub("", BACK_RE.sub("", addressee))
                personal = _main(self.page(scores)["first"])
                self.assertEqual(toggle.findall(personal), [])
                self.assertEqual(addressee, personal)

    def test_as_person_follows_card_view(self):
        model = json.loads(json.dumps(self.model))
        [p for p in model["people"] if p["id"] == "anna"][0]["show_scores"] = False
        reviewer = rd.attach_html(rd.build_payload(model, mode="reviewer", title="Ревьюер",
                                                   show_scores=True))
        seen = run_page(reviewer, "p-anna", [["button", self.ru["as_person"]]])
        html_text = _main(seen["snaps"][1]["html"])
        self.assertIn('class="plot trend"', html_text)
        self.assertNotIn(self.ru["level_last"], html_text)

    def test_reviewer_card_keeps_service_blocks(self):
        reviewer = rd.attach_html(rd.build_payload(self.model, mode="reviewer", title="Ревьюер",
                                                   show_scores=False))
        first = run_page(reviewer, "p-anna")["first"]
        for key in ("summary", "as_person", "evidence", "copy_sent", "override", "charts",
                    "level_last", "agent_stack"):
            with self.subTest(key=key):
                self.assertIn(self.ru[key], first)


@unittest.skipUnless(shutil.which("node"), "node не установлен")
class TrendSvgTest(unittest.TestCase):
    """`trendSVG` — график вида trend: значок перехода на точку, без баллов."""

    @classmethod
    def setUpClass(cls) -> None:
        from test_report_render import _js_function, _run_node
        js = (SCRIPTS / "lib" / "report_assets" / "report.js").read_text(encoding="utf-8")
        pts = [
            {"period": "P1", "date": "2026-01-05", "axes": {"a": {"ie": False, "reason": None}},
             "transitions": {"a": "none"}},
            {"period": "P2", "date": "2026-01-19", "axes": {"a": {"ie": False, "reason": None}},
             "transitions": {"a": "up"}},
            {"period": "P3", "date": "2026-02-02", "axes": {"a": {"ie": True, "reason": "short_log"}},
             "transitions": {"a": "none"}},
            {"period": "P4", "date": "2026-02-16", "axes": {"a": {"ie": False, "reason": None}},
             "transitions": {"a": "none"}},
            {"period": "P5", "date": "2026-03-02", "axes": {"a": {"ie": False, "reason": None}},
             "transitions": {"a": "down"}},
            {"period": "P6", "date": "2026-03-16", "axes": {"a": {"ie": False, "reason": None}},
             "transitions": {"a": "flat"}},
        ]
        script = (
            "const esc = s => String(s); const axName = a => 'AX:' + a; const t = k => 'T:' + k;\n"
            "const fmtDate = s => s; const fmtShort = s => s.slice(8, 10) + '.' + s.slice(5, 7);\n"
            "const reasonName = r => 'R:' + r;\n"
            + _js_function(js, "periodTick") + "\n" + _js_function(js, "tickAnchor") + "\n"
            + "\n".join(ln for ln in js.splitlines() if ln.startswith("const TREND_KINDS")) + "\n"
            + _js_function(js, "trendSVG")
            + f"\nprocess.stdout.write(JSON.stringify(trendSVG({json.dumps(pts)}, 'a', 400, 1)));"
        )
        cls.svg = _run_node(script)

    def test_one_glyph_per_point_by_transition(self):
        glyphs = re.findall(r'<text class="glyph"[^>]*>([^<]*)</text>', self.svg)
        self.assertEqual(glyphs, ["↑", "↓", "→"])
        self.assertEqual(self.svg.count('class="nd-mark"'), 2, "P3 без данных и P4 после пропуска")
        self.assertEqual(self.svg.count('class="first-mark"'), 1)

    def test_tips_name_period_and_kind_without_scores(self):
        tips = re.findall(r'data-tip="([^"]*)"', self.svg)
        self.assertEqual(len(tips), 6)
        self.assertIn("P2", tips[1])
        self.assertIn("T:trans_up", tips[1])
        self.assertIn("T:trend_first", tips[0])
        self.assertIn("R:short_log", tips[2])
        self.assertIn("R:short_log", tips[3], "переход после пропуска — причина пропуска")
        self.assertNotRegex(self.svg, r">\s*[0-4]\s*</text>")

    def test_selected_run_highlighted_and_ticks(self):
        self.assertEqual(self.svg.count("var(--accent-soft)"), 1)
        ticks = re.findall(r'<text class="tick-period"[^>]*><title>([^<]*)</title>', self.svg)
        self.assertEqual(ticks, ["P1", "P2", "P3", "P4", "P5", "P6"])


if __name__ == "__main__":
    unittest.main()
