"""Агрегация трекера в модель и payload по режимам (`lib/report_data.py`)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import _util  # noqa: F401  (кладёт scripts/ в sys.path)
import report_fixture
from _util import FIXTURES
from lib import report_data as rd
from lib.config import load_config
from lib.people import add_person
from lib.workspace import SrError, ws_paths


def _keys(value, out: set | None = None) -> set:
    """Все ключи dict при рекурсивном обходе."""
    out = set() if out is None else out
    if isinstance(value, dict):
        for key, item in value.items():
            out.add(key)
            _keys(item, out)
    elif isinstance(value, list):
        for item in value:
            _keys(item, out)
    return out


def _tracker(root: Path) -> dict:
    return json.loads((ws_paths(root)["runs"] / "process-reviews.json").read_text(encoding="utf-8"))


def _write_tracker(root: Path, doc: dict) -> None:
    (ws_paths(root)["runs"] / "process-reviews.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = report_fixture.make_workspace(Path(cls._tmp.name) / "ws")
        cls.warnings: list[str] = []
        cls.model = rd.collect(cls.root, cls.warnings.append)
        cls.tracker = _tracker(cls.root)
        cls.person = {p["id"]: p for p in cls.model["people"]}

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def fresh(self, **kwargs) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return report_fixture.make_workspace(Path(tmp.name) / "ws", **kwargs)

    def runs_of(self, pid: str) -> list[dict]:
        return [r for r in self.tracker["runs"] if r["person_id"] == pid]


class TestI18n(unittest.TestCase):
    def test_load_i18n_drops_cli(self) -> None:
        i18n = rd.load_i18n()
        self.assertNotIn("cli", i18n)
        self.assertEqual(len(i18n["axes"]), 8)
        self.assertEqual(list(i18n["axes"])[0], "verification")

    def test_load_i18n_drops_underscore_names(self) -> None:
        data = {"_note": "x", "axes": {"a": {"ru": "А", "en": "A"}, "_meta": "y"}, "cli": {}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "i18n.json"
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            old, rd._I18N_PATH = rd._I18N_PATH, path
            try:
                got = rd.load_i18n()
            finally:
                rd._I18N_PATH = old
        self.assertEqual(got, {"axes": {"a": {"ru": "А", "en": "A"}}})


class TestCollect(_Base):
    def test_model_shape_and_axes_order(self) -> None:
        self.assertEqual(self.model["axes"], list(rd.load_i18n()["axes"]))
        ws = self.model["workspace"]
        self.assertEqual((ws["id"], ws["mode"], ws["lang"], ws["show_scores"]),
                         ("demo-team", "team", "ru", False))

    def test_retracted_run_is_not_a_point(self) -> None:
        twin = [r for r in self.runs_of("anna") if r.get("retracted")]
        self.assertEqual(len(twin), 1)
        ids = [pt["run_id"] for pt in self.person["anna"]["points"]]
        self.assertNotIn(twin[0]["run_id"], ids)
        self.assertEqual([pt["period"] for pt in self.person["anna"]["points"]], ["P1", "P2", "P3"])

    def test_live_duplicate_takes_latest_revision(self) -> None:
        p1 = [r for r in self.runs_of("boris") if r["period"]["label"] == "P1"]
        self.assertEqual(len(p1), 2)
        newest = max(p1, key=lambda r: r["reviewed_at"])
        self.assertEqual(newest["revision"], 2)
        points = [pt for pt in self.person["boris"]["points"] if pt["period_key"] == "P1"]
        self.assertEqual([pt["run_id"] for pt in points], [newest["run_id"]])

    def test_insufficient_evidence_is_null_not_zero(self) -> None:
        axis = self.person["anna"]["points"][-1]["axes"]["reversibility"]
        self.assertIsNone(axis["score"])
        self.assertTrue(axis["ie"])
        self.assertEqual(axis["reason"], "short_log")
        for person in self.model["people"]:
            for pt in person["points"]:
                for ax in pt["axes"].values():
                    self.assertNotEqual(ax["score"], 0)

    def test_manual_override_replaces_score(self) -> None:
        pt = [p for p in self.person["boris"]["points"] if p["period_key"] == "P2"][0]
        ax = pt["axes"]["verification"]
        self.assertEqual((ax["score"], ax["overridden"], ax["ie"]), (4, True, False))
        self.assertTrue(pt["gate"])
        self.assertFalse(pt["axes"]["ownership"]["overridden"])

    def test_level_falls_back_to_judge_with_warning(self) -> None:
        run = [r for r in self.runs_of("vera") if r["period"]["label"] == "P1"][0]
        pt = self.person["vera"]["points"][0]
        self.assertEqual(pt["level"], run["level_judge"])
        self.assertTrue(any(run["run_id"] in w and "level_judge" in w for w in self.warnings),
                        self.warnings)

    def test_recommendation_open_by_verdict(self) -> None:
        def rec(pid: str) -> dict:
            return [r for r in self.person[pid]["recs"] if r["id"] == f"REC-{pid}-001"][0]

        self.assertEqual(rec("anna")["verdicts"][0]["verdict"], "followed")
        self.assertFalse(rec("anna")["open"])
        self.assertEqual(rec("boris")["verdicts"][0]["verdict"], "not_observable")
        self.assertTrue(rec("boris")["open"])
        self.assertEqual(rec("vera")["verdicts"][0]["verdict"], "ignored")
        self.assertFalse(rec("vera")["open"])
        self.assertEqual(rec("anna")["given_in"], "P1")

    def test_review_of_retracted_run_is_counted_not_warned(self) -> None:
        # отзыв — штатное действие: ревью отозванного прогона не шумит на каждой сборке
        twin = [r for r in self.runs_of("anna") if r.get("retracted")][0]
        self.assertEqual(self.model["unbound_reviews"], [])
        self.assertEqual(self.model["retracted_reviews"], [twin["run_id"]])
        self.assertFalse(any(twin["run_id"] in w for w in self.warnings), self.warnings)
        self.assertFalse(any("отозван" in w for w in self.warnings), self.warnings)

    def test_sessions_count_only_reviewed(self) -> None:
        run = _run_of(self.tracker, "anna", "P2")
        statuses = [s["status"] for s in run["sessions_reviewed"]]
        self.assertIn("reviewed", statuses)
        self.assertEqual(len(statuses) - statuses.count("reviewed"), 2, statuses)
        pt = [p for p in self.person["anna"]["points"] if p["period_key"] == "P2"][0]
        self.assertEqual(pt["sessions"], statuses.count("reviewed"))
        self.assertEqual(pt["sessions_skipped"], 2)
        p1 = self.person["anna"]["points"][0]
        self.assertEqual(p1["sessions_skipped"], 0)

    def test_reviews_bound_to_points(self) -> None:
        pts = self.person["anna"]["points"]
        self.assertEqual([pt["review"]["status"] for pt in pts], ["approved", "approved", "draft"])
        self.assertTrue(pts[0]["review"]["path"].startswith("reviews/anna/"))
        self.assertTrue(pts[0]["review"]["sent_md"])

    def test_person_without_runs_skipped_with_count(self) -> None:
        self.assertEqual(sorted(self.person), ["anna", "boris", "vera"])
        self.assertTrue(any("без прогонов" in w and "1" in w for w in self.warnings), self.warnings)

    def test_people_sorted_by_name(self) -> None:
        root = self.fresh()
        card = ws_paths(root)["people"] / "vera.yaml"
        card.write_text(card.read_text(encoding="utf-8").replace("Вера", "Аврора"), encoding="utf-8")
        model = rd.collect(root, lambda _m: None)
        self.assertEqual([p["id"] for p in model["people"]], ["vera", "anna", "boris"])

    def test_points_ordered_by_period_start(self) -> None:
        root = self.fresh(people=1, periods=3)
        doc = _tracker(root)
        doc["runs"].reverse()
        _write_tracker(root, doc)
        model = rd.collect(root, lambda _m: None)
        self.assertEqual([pt["period"] for pt in model["people"][0]["points"]], ["P1", "P2", "P3"])


class TestTransitions(unittest.TestCase):
    def test_up_down_flat_none(self) -> None:
        def pt(score):
            return {"axes": {"a": {"score": score, "ie": score is None}}}

        points = [pt(2), pt(3), pt(3), pt(1), pt(None), pt(4)]
        got = [t["a"] for t in rd.transitions(points, ["a"])]
        self.assertEqual(got, ["none", "up", "flat", "down", "none", "none"])


class TestPayload(_Base):
    def personal(self, pid: str = "anna", **kwargs) -> dict:
        return rd.build_payload(self.model, mode="personal", person=pid, **kwargs)

    def test_personal_has_one_person_and_no_forbidden_keys(self) -> None:
        payload = self.personal()
        self.assertEqual([p["id"] for p in payload["people"]], ["anna"])
        keys = _keys(payload)
        self.assertFalse(keys & rd.PERSONAL_FORBIDDEN_KEYS, keys & rd.PERSONAL_FORBIDDEN_KEYS)
        self.assertNotIn("score", keys)
        self.assertNotIn("level", keys)
        self.assertNotIn("group", payload["people"][0])
        self.assertIn("transitions", payload["people"][0]["points"][1])
        self.assertNotIn("boris", json.dumps(payload, ensure_ascii=False))

    def test_personal_show_scores_adds_only_score_and_level(self) -> None:
        payload = self.personal(show_scores=True)
        keys = _keys(payload)
        self.assertIn("score", keys)
        self.assertIn("level", keys)
        self.assertFalse(keys & rd.PERSONAL_FORBIDDEN_KEYS, keys & rd.PERSONAL_FORBIDDEN_KEYS)
        self.assertTrue(payload["meta"]["show_scores"])

    def test_personal_review_only_approved_sent_block(self) -> None:
        pts = self.personal()["people"][0]["points"]
        self.assertEqual(set(pts[0]["review"]), {"sent_md"})
        self.assertEqual(pts[0]["review"]["sent_md"], self.person["anna"]["points"][0]["review"]["sent_md"])
        # прогон одобрен в трекере, а файл ревью ещё черновик: отправляемого блока нет
        root = self.fresh()
        doc = _tracker(root)
        _run_of(doc, "anna", "P3")["review_status"] = "approved"
        _write_tracker(root, doc)
        model, _warnings = _collect(root)
        pts = rd.build_payload(model, mode="personal", person="anna")["people"][0]["points"]
        self.assertEqual(pts[-1]["period"], "P3")
        self.assertIsNone(pts[-1]["review"])

    def test_personal_recs_with_status(self) -> None:
        recs = {r["given_in"]: r for r in self.personal()["people"][0]["recs"]}
        self.assertEqual(set(recs["P1"]), {"text", "axis", "given_in", "status", "verdicts"})
        self.assertEqual(recs["P1"]["status"], "followed")
        # у vera вердикт `ignored` вынесен в черновике P3: адресату статус не меняется
        vera = {r["given_in"]: r for r in self.personal("vera")["people"][0]["recs"]}
        self.assertEqual(vera["P1"]["status"], "open")
        self.assertNotIn("P3", vera)
        boris = {r["given_in"]: r for r in self.personal("boris")["people"][0]["recs"]}
        self.assertEqual(boris["P1"]["status"], "open")

    def test_personal_without_dynamics_has_no_transitions(self) -> None:
        self.assertNotIn("transitions", _keys(self.personal(show_dynamics=False)))

    def test_personal_person_required_and_known(self) -> None:
        with self.assertRaises(SrError) as ctx:
            rd.build_payload(self.model, mode="personal")
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SrError) as ctx:
            self.personal("nobody")
        self.assertEqual(ctx.exception.code, 2)
        with self.assertRaises(SrError) as ctx:
            rd.build_payload(self.model, mode="everyone")
        self.assertEqual(ctx.exception.code, 2)

    def test_group_has_no_texts(self) -> None:
        payload = rd.build_payload(self.model, mode="group", title="Команда")
        self.assertEqual(len(payload["people"]), 3)
        keys = _keys(payload)
        for key in ("evidence", "summary", "note", "feedback_draft", "sent_md", "antipatterns"):
            self.assertNotIn(key, keys)
        for person in payload["people"]:
            self.assertEqual(person["recs"], [])
            self.assertIn("group", person)
            for pt in person["points"]:
                self.assertIsNone(pt["review"])
                self.assertIn("score", pt["axes"]["verification"])
        meta = payload["meta"]
        self.assertEqual((meta["mode"], meta["title"], meta["reviews_unbound"]), ("group", "Команда", 0))
        self.assertNotIn("reviews_of_retracted", meta)
        self.assertNotIn("sessions_skipped", _keys(payload))
        # дата логов (period.from первого периода), не дата запуска оценки
        self.assertEqual(meta["date_from"], "2026-01-05")

    def test_group_only_approved_points(self) -> None:
        # гейт ревьюера: у anna P1, P2 одобрены, P3 — черновик последнего периода
        anna_model = self.person["anna"]["points"]
        self.assertEqual([pt["approved"] for pt in anna_model], [True, True, False])
        payload = rd.build_payload(self.model, mode="group")
        people = {p["id"]: p for p in payload["people"]}
        anna = people["anna"]
        self.assertEqual([pt["period"] for pt in anna["points"]], ["P1", "P2"])
        self.assertEqual(anna["points"][-1]["level"], anna_model[1]["level"])
        # ни у кого нет точки черновика: распределение и «последний прогон» — из одобренных
        for pid, person in people.items():
            approved = [pt["period"] for pt in rd.approved_points(self.person[pid])]
            self.assertEqual([pt["period"] for pt in person["points"]], approved, pid)
        text = json.dumps(payload, ensure_ascii=False)
        for p in self.model["people"]:
            for pt in p["points"]:
                if not pt["approved"]:
                    self.assertNotIn(pt["run_id"], text)
        shown = [pt for p in self.model["people"] for pt in rd.approved_points(p)]
        self.assertEqual(payload["meta"]["date_to"],
                         max(pt["logs_to"] or pt["logs_from"] for pt in shown))
        self.assertLess(payload["meta"]["date_to"],
                        max(pt["logs_to"] or pt["logs_from"] for pt in anna_model))
        self.assertEqual(payload["meta"]["awaiting_approval"], 0)

    def test_group_person_without_approved_runs_counted_not_shown(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        for run in doc["runs"]:
            if run["person_id"] == "boris":
                run["review_status"] = "draft"
        _write_tracker(root, doc)
        model, _warnings = _collect(root)
        payload = rd.build_payload(model, mode="group", details=True)
        self.assertEqual([p["id"] for p in payload["people"]], ["anna", "vera"])
        self.assertEqual(payload["meta"]["awaiting_approval"], 1)
        self.assertNotIn("boris", json.dumps(payload, ensure_ascii=False))

    def test_group_self_without_gate_keeps_every_point(self) -> None:
        model, _warnings = _collect(self.fresh(mode="self"))
        payload = rd.build_payload(model, mode="group")
        self.assertEqual(len(payload["people"][0]["points"]), len(model["people"][0]["points"]))
        self.assertEqual(payload["meta"]["awaiting_approval"], 0)

    def test_reviewer_carries_everything(self) -> None:
        payload = rd.build_payload(self.model, mode="reviewer")
        anna = [p for p in payload["people"] if p["id"] == "anna"][0]
        pt = anna["points"][0]
        for key in ("run_id", "summary", "feedback_draft", "antipatterns", "transitions", "gate"):
            self.assertIn(key, pt)
        self.assertEqual(set(pt["review"]), {"status", "service_md", "sent_md", "dossier_md",
                                             "path", "recognized", "warning"})
        self.assertIn("verdicts", anna["recs"][0])
        self.assertTrue(pt["axes"]["verification"]["evidence"])
        self.assertEqual([p["sessions_skipped"] for p in anna["points"]], [0, 2, 0])

    def test_reviewer_meta_counts_reviews_of_retracted_runs_apart(self) -> None:
        meta = rd.build_payload(self.model, mode="reviewer")["meta"]
        self.assertEqual((meta["reviews_unbound"], meta["reviews_of_retracted"]), (0, 1))
        personal = rd.build_payload(self.model, mode="personal", person="anna")["meta"]
        self.assertNotIn("reviews_of_retracted", personal)

    def test_reviewer_quotes_only_in_last_periods(self) -> None:
        model = rd.collect(self.fresh(people=1, periods=6), lambda _m: None)
        pts = rd.build_payload(model, mode="reviewer", quote_periods=3)["people"][0]["points"]
        self.assertEqual(len(pts), 6)
        for pt in pts[:3]:
            self.assertTrue(all(ax["evidence"] == [] for ax in pt["axes"].values()))
            self.assertNotIn("dossier_md", pt["review"])
            self.assertEqual(pt["review"]["dossier_ref"], pt["review"]["path"])
        for pt in pts[3:]:
            self.assertTrue(pt["axes"]["verification"]["evidence"])
            self.assertIn("dossier_md", pt["review"])
            self.assertNotIn("dossier_ref", pt["review"])
        unlimited = rd.build_payload(model, mode="reviewer", quote_periods=None)["people"][0]["points"]
        self.assertTrue(all("dossier_md" in pt["review"] for pt in unlimited))

    def test_build_payload_does_not_mutate_model(self) -> None:
        before = json.dumps(self.model, ensure_ascii=False, sort_keys=True)
        rd.attach_html(rd.build_payload(self.model, mode="reviewer", quote_periods=1))
        rd.build_payload(self.model, mode="personal", person="anna")
        self.assertEqual(json.dumps(self.model, ensure_ascii=False, sort_keys=True), before)

    def test_attach_html_replaces_md(self) -> None:
        payload = rd.attach_html(rd.build_payload(self.model, mode="reviewer"))
        keys = _keys(payload)
        self.assertFalse({k for k in keys if k.endswith("_md")})
        self.assertIn("sent_html", keys)
        self.assertIn("dossier_html", keys)
        self.assertIn("feedback_draft", keys)
        pt = payload["people"][0]["points"][0]
        self.assertTrue(pt["review"]["sent_html"].startswith("<"))
        dossier = payload["people"][0]["points"][0]["review"]["dossier_html"]
        self.assertIn("&lt;img", dossier)
        self.assertNotIn("<img", dossier)


class TestTrackerErrors(_Base):
    def assert_code(self, root: Path, code: int, needle: str) -> None:
        with self.assertRaises(SrError) as ctx:
            rd.collect(root, lambda _m: None)
        self.assertEqual(ctx.exception.code, code)
        self.assertIn(needle, ctx.exception.message)

    def test_missing_tracker(self) -> None:
        root = self.fresh()
        path = ws_paths(root)["runs"] / "process-reviews.json"
        path.unlink()
        self.assert_code(root, 1, str(path))

    def test_broken_tracker(self) -> None:
        root = self.fresh()
        path = ws_paths(root)["runs"] / "process-reviews.json"
        path.write_bytes(b"\xff\xfe{")
        self.assert_code(root, 1, str(path))
        path.write_text("{", encoding="utf-8")
        self.assert_code(root, 1, str(path))

    def test_tracker_v1_rejected(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        doc["tracker_version"] = 1
        _write_tracker(root, doc)
        self.assert_code(root, 1, "tracker_version")

    def test_empty_runs_rejected(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        doc["runs"] = []
        _write_tracker(root, doc)
        self.assert_code(root, 1, "нет прогонов")

    def test_run_without_card_rejected(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        doc["runs"][0]["person_id"] = "ghost"
        _write_tracker(root, doc)
        self.assert_code(root, 1, "ghost")


def _review_file(root: Path, pid: str, period: str) -> Path:
    for path in sorted((ws_paths(root)["reviews"] / pid).glob("*.md")):
        if f"period: {period}\n" in path.read_text(encoding="utf-8"):
            return path
    raise AssertionError(f"нет ревью {pid} {period}")


def _collect(root: Path) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    return rd.collect(root, warnings.append), warnings


def _run_of(doc: dict, pid: str, period: str) -> dict:
    return [r for r in doc["runs"] if r["person_id"] == pid and r["period"]["label"] == period
            and not r.get("retracted")][-1]


class TestReviewBinding(_Base):
    def test_approved_review_without_service_mark_not_sent(self) -> None:
        root = self.fresh()
        path = _review_file(root, "anna", "P1")
        path.write_text(path.read_text(encoding="utf-8").replace("<!-- service -->\n", "", 1),
                        encoding="utf-8")
        model, warnings = _collect(root)
        pt = rd.build_payload(model, mode="personal", person="anna")["people"][0]["points"][0]
        self.assertIsNone(pt["review"])
        self.assertTrue(any("<!-- service -->" in w for w in warnings), warnings)

    def test_foreign_person_id_in_frontmatter_not_bound(self) -> None:
        root = self.fresh()
        path = _review_file(root, "anna", "P1")
        run_id = path.stem
        path.write_text(path.read_text(encoding="utf-8").replace("person_id: anna", "person_id: boris"),
                        encoding="utf-8")
        model, warnings = _collect(root)
        anna = [p for p in model["people"] if p["id"] == "anna"][0]
        self.assertIsNone(anna["points"][0]["review"])
        self.assertIn(run_id, model["unbound_reviews"])
        self.assertTrue(any(run_id in w and "boris" in w for w in warnings), warnings)
        personal = rd.build_payload(model, mode="personal", person="anna")
        self.assertIsNone(personal["people"][0]["points"][0]["review"])

    def test_review_in_foreign_folder_not_bound(self) -> None:
        root = self.fresh()
        path = _review_file(root, "anna", "P1")
        run_id = path.stem
        moved = ws_paths(root)["reviews"] / "boris" / path.name
        path.rename(moved)
        model, warnings = _collect(root)
        anna = [p for p in model["people"] if p["id"] == "anna"][0]
        self.assertIsNone(anna["points"][0]["review"])
        self.assertIn(run_id, model["unbound_reviews"])
        self.assertTrue(any(run_id in w and "reviews/boris/" in w for w in warnings), warnings)

    def test_superseded_review_warns_retracted_does_not(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        old = [r for r in doc["runs"] if r["person_id"] == "boris" and r["period"]["label"] == "P1"
               and r["revision"] == 1][0]
        src = _review_file(root, "boris", "P1")
        (src.parent / f"{old['run_id']}.md").write_text(
            src.read_text(encoding="utf-8").replace(src.stem, old["run_id"]), encoding="utf-8")
        model, warnings = _collect(root)
        self.assertEqual(model["unbound_reviews"], [old["run_id"]])
        superseded = [w for w in warnings if old["run_id"] in w]
        self.assertTrue(any("заменён ревизией 2" in w for w in superseded), superseded)
        twin = [r for r in doc["runs"] if r.get("retracted")][0]
        self.assertEqual(model["retracted_reviews"], [twin["run_id"]])
        self.assertEqual([w for w in warnings if twin["run_id"] in w], [])

    def test_review_without_run_in_tracker_warns(self) -> None:
        root = self.fresh()
        src = _review_file(root, "anna", "P2")
        ghost = "demo-team_anna_P9_20990101T000000Z"
        (src.parent / f"{ghost}.md").write_text(
            src.read_text(encoding="utf-8").replace(src.stem, ghost), encoding="utf-8")
        model, warnings = _collect(root)
        self.assertEqual(model["unbound_reviews"], [ghost])
        self.assertTrue(any(ghost in w and "нет в трекере" in w for w in warnings), warnings)


class TestFreeTextEnums(_Base):
    def test_unknown_reason_becomes_other(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        run = _run_of(doc, "anna", "P3")
        run["review_status"] = "approved"  # в личный отчёт идут только одобренные прогоны
        for axis in run["axes"]:
            if axis["id"] == "reversibility":
                axis["insufficient_evidence_reason"] = "работа шла у Бориса в IDE"
        _write_tracker(root, doc)
        model, warnings = _collect(root)
        payload = rd.build_payload(model, mode="personal", person="anna")
        self.assertNotIn("Бориса", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["people"][0]["points"][-1]["axes"]["reversibility"]["reason"],
                         "other")
        self.assertTrue(any(run["run_id"] in w and "reason" in w for w in warnings), warnings)

    def test_known_reason_kept(self) -> None:
        self.assertEqual(self.person["anna"]["points"][-1]["axes"]["reversibility"]["reason"],
                         "short_log")

    def test_reason_codes_match_judge_schema(self) -> None:
        schema = json.loads((_util.PLUGIN / "references" / "schemas" / "judge.json").read_text("utf-8"))
        axis = schema["properties"]["data"]["properties"]["axes"]["items"]["properties"]
        enum = {code for code in axis["insufficient_evidence_reason"]["enum"] if code}
        self.assertEqual(rd.reason_codes(rd.load_i18n()), enum | {"other"})

    def test_reason_other_has_i18n(self) -> None:
        entry = rd.load_i18n()["report"]["reason_other"]
        self.assertTrue(entry["ru"] and entry["en"])

    def test_recommendation_with_unknown_axis_skipped(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        run = _run_of(doc, "anna", "P2")
        run["recommendations"][0]["axis"] = "у Бориса"
        _write_tracker(root, doc)
        model, warnings = _collect(root)
        anna = [p for p in model["people"] if p["id"] == "anna"][0]
        self.assertNotIn(run["recommendations"][0]["id"], [r["id"] for r in anna["recs"]])
        self.assertNotIn("Бориса", json.dumps(
            rd.build_payload(model, mode="personal", person="anna"), ensure_ascii=False))
        self.assertTrue(any(run["run_id"] in w and "у Бориса" in w for w in warnings), warnings)


class TestTypes(_Base):
    def assert_bad(self, mutate, needle: str) -> None:
        root = self.fresh()
        doc = _tracker(root)
        run = _run_of(doc, "anna", "P2")
        mutate(run)
        _write_tracker(root, doc)
        with self.assertRaises(SrError) as ctx:
            rd.collect(root, lambda _m: None)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(run["run_id"], ctx.exception.message)
        self.assertIn(needle, ctx.exception.message)

    def test_score_types(self) -> None:
        for bad in ("3", 5, -1, True, 2.5):
            def mutate(run, bad=bad):
                run["axes"][0]["score"] = bad
            with self.subTest(bad=bad):
                self.assert_bad(mutate, "score")

    def test_override_types(self) -> None:
        for bad in ("4", 7, False):
            def mutate(run, bad=bad):
                run["operator_override"] = {"axes": {"verification": bad}}
            with self.subTest(bad=bad):
                self.assert_bad(mutate, OVERRIDE_FIELD)

    def test_revision_types(self) -> None:
        for bad in ("2", 0, True):
            def mutate(run, bad=bad):
                run["revision"] = bad
            with self.subTest(bad=bad):
                self.assert_bad(mutate, "revision")

    def test_revision_absent_is_ok(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        for run in doc["runs"]:
            if run["person_id"] == "vera":
                run.pop("revision")
        _write_tracker(root, doc)
        rd.collect(root, lambda _m: None)

    def test_non_dict_run_rejected_with_index(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        doc["runs"].insert(2, "не прогон")
        _write_tracker(root, doc)
        with self.assertRaises(SrError) as ctx:
            rd.collect(root, lambda _m: None)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("runs[2]", ctx.exception.message)

    def test_show_dynamics_validated_in_collect(self) -> None:
        root = self.fresh()
        card = ws_paths(root)["people"] / "boris.yaml"
        card.write_text(card.read_text(encoding="utf-8") + "  show_dynamics: false\n",
                        encoding="utf-8")
        model = rd.collect(root, lambda _m: None)
        flags = {p["id"]: p["show_dynamics"] for p in model["people"]}
        self.assertEqual(flags, {"anna": True, "boris": False, "vera": True})
        reviewer = rd.build_payload(model, mode="reviewer")
        self.assertEqual({p["id"]: p["show_dynamics"] for p in reviewer["people"]}, flags)
        self.assertNotIn("show_dynamics", rd.build_payload(model, mode="group")["people"][0])

    def test_quote_periods_bool_rejected(self) -> None:
        with self.assertRaises(SrError) as ctx:
            rd.build_payload(self.model, mode="reviewer", quote_periods=True)
        self.assertEqual(ctx.exception.code, 2)


OVERRIDE_FIELD = rd.OVERRIDE_KEY


def _logs_in_august(doc: dict) -> None:
    """Логи периода Pn — август (from в двух форматах, как пишет оценка), оценка — сентябрь."""
    for run in doc["runs"]:
        p = int(run["period"]["label"][1:])
        day = 10 + 7 * (p - 1)
        run["period"]["from"] = f"2026-08-{day:02d} 13:47:16"
        run["period"]["to"] = f"2026-08-{day + 2:02d} 09:20:31Z"
        run["reviewed_at"] = f"2026-09-{20 + p:02d}T{run['reviewed_at'][11:]}"


class TestLogDates(_Base):
    """Даты отчёта — даты логов периода (`period.from/to`), а не дата запуска оценки."""

    def logs_model(self, mutate=_logs_in_august) -> dict:
        root = self.fresh()
        doc = _tracker(root)
        mutate(doc)
        _write_tracker(root, doc)
        return rd.collect(root, lambda _m: None)

    def test_point_date_and_meta_range_are_log_dates(self) -> None:
        model = self.logs_model()
        for person in model["people"]:
            self.assertEqual([pt["period"] for pt in person["points"]][0], "P1")
            for pt in person["points"]:
                self.assertTrue(pt["date"].startswith("2026-08-"), pt)
                self.assertEqual(pt["date_source"], "logs")
        anna = {p["id"]: p for p in model["people"]}["anna"]
        self.assertEqual([pt["date"] for pt in anna["points"]],
                         ["2026-08-10", "2026-08-17", "2026-08-24"])
        # group — только одобренные прогоны: черновики P3 в диапазон не входят
        for mode, date_to in (("group", "2026-08-19"), ("reviewer", "2026-08-26")):
            with self.subTest(mode=mode):
                meta = rd.build_payload(model, mode=mode)["meta"]
                self.assertEqual((meta["date_from"], meta["date_to"], meta["date_source"]),
                                 ("2026-08-10", date_to, "logs"))
                pts = rd.build_payload(model, mode=mode)["people"][0]["points"]
                self.assertTrue(all(pt["date"].startswith("2026-08-") for pt in pts))

    def test_run_dates_only_in_reviewer_meta(self) -> None:
        model = self.logs_model()
        reviewer = rd.build_payload(model, mode="reviewer")["meta"]
        self.assertEqual((reviewer["run_from"], reviewer["run_to"]), ("2026-09-21", "2026-09-23"))
        for mode, kw in (("group", {}), ("personal", {"person": "anna"})):
            with self.subTest(mode=mode):
                meta = rd.build_payload(model, mode=mode, **kw)["meta"]
                self.assertNotIn("run_from", meta)
                self.assertNotIn("run_to", meta)

    def test_personal_range_only_over_approved_log_dates(self) -> None:
        payload = rd.build_payload(self.logs_model(), mode="personal", person="anna")
        meta = payload["meta"]
        self.assertEqual((meta["date_from"], meta["date_to"], meta["date_source"]),
                         ("2026-08-10", "2026-08-19", "logs"))
        self.assertEqual([pt["date"] for pt in payload["people"][0]["points"]],
                         ["2026-08-10", "2026-08-17"])

    def test_points_ordered_by_log_start_not_by_run(self) -> None:
        def mutate(doc):
            _logs_in_august(doc)
            for run in doc["runs"]:
                p = int(run["period"]["label"][1:])
                run["reviewed_at"] = f"2026-09-{28 - p:02d}T{run['reviewed_at'][11:]}"
        anna = {p["id"]: p for p in self.logs_model(mutate)["people"]}["anna"]
        self.assertEqual([pt["period"] for pt in anna["points"]], ["P1", "P2", "P3"])

    def test_only_to_gives_date(self) -> None:
        def mutate(doc):
            _logs_in_august(doc)
            for run in doc["runs"]:
                run["period"]["from"] = None
        anna = {p["id"]: p for p in self.logs_model(mutate)["people"]}["anna"]
        self.assertEqual(anna["points"][0]["date"], "2026-08-12")
        self.assertEqual(anna["points"][0]["date_source"], "logs")

    def test_point_without_log_dates_uses_run_date(self) -> None:
        def mutate(doc):
            _logs_in_august(doc)
            for run in doc["runs"]:
                if run["person_id"] == "anna" and run["period"]["label"] == "P1":
                    run["period"]["from"] = run["period"]["to"] = None
        model = self.logs_model(mutate)
        anna = {p["id"]: p for p in model["people"]}["anna"]
        first = [pt for pt in anna["points"] if pt["period"] == "P1"][0]
        self.assertEqual((first["date"], first["date_source"]), ("2026-09-21", "run"))
        meta = rd.build_payload(model, mode="group")["meta"]
        self.assertEqual((meta["date_from"], meta["date_to"], meta["date_source"]),
                         ("2026-08-10", "2026-08-19", "logs"))

    def test_no_log_dates_at_all_is_run_mode(self) -> None:
        def mutate(doc):
            _logs_in_august(doc)
            for run in doc["runs"]:
                run["period"]["from"] = run["period"]["to"] = None
        model = self.logs_model(mutate)
        for mode, date_to in (("group", "2026-09-22"), ("reviewer", "2026-09-23")):
            with self.subTest(mode=mode):
                meta = rd.build_payload(model, mode=mode)["meta"]
                self.assertEqual((meta["date_from"], meta["date_to"], meta["date_source"]),
                                 ("2026-09-21", date_to, "run"))

    def test_broken_log_date_is_an_error(self) -> None:
        for field, bad in (("from", "13.08.2026 13:47"), ("to", "2026-8-14"), ("from", 20260813),
                           ("from", "2026-13-40 10:00:00")):
            with self.subTest(field=field, bad=bad):
                root = self.fresh()
                doc = _tracker(root)
                run = _run_of(doc, "anna", "P2")
                run["period"][field] = bad
                _write_tracker(root, doc)
                with self.assertRaises(SrError) as ctx:
                    rd.collect(root, lambda _m: None)
                self.assertEqual(ctx.exception.code, 1)
                self.assertIn(run["run_id"], ctx.exception.message)
                self.assertIn(f"period.{field}", ctx.exception.message)
                self.assertIn(repr(bad), ctx.exception.message)


class TestPersonalKeys(_Base):
    def test_exact_keys_without_scores(self) -> None:
        self.check(False)

    def test_exact_keys_with_scores(self) -> None:
        self.check(True)

    def check(self, show_scores: bool) -> None:
        payload = rd.build_payload(self.model, mode="personal", person="anna",
                                   show_scores=show_scores)
        self.assertEqual(set(payload), {"meta", "axes", "people"})
        person = payload["people"][0]
        self.assertEqual(set(person), {"id", "name", "points", "recs"})
        # вид scores: баллы и уровень без переходов; вид trend: переходы без баллов и уровня
        point_keys = {"period", "date", "sessions", "axes", "review", "agent_stack"}
        axis_keys = {"ie", "reason"}
        if show_scores:
            point_keys |= {"level"}
            axis_keys |= {"score"}
        else:
            point_keys |= {"transitions"}
        reviews = 0
        for pt in person["points"]:
            self.assertEqual(set(pt), point_keys)
            for item in pt["axes"].values():
                self.assertEqual(set(item), axis_keys)
            if pt["review"] is not None:
                reviews += 1
                self.assertEqual(set(pt["review"]), {"sent_md"})
        self.assertTrue(reviews)
        self.assertTrue(person["recs"])
        verdicts = 0
        for rec in person["recs"]:
            self.assertEqual(set(rec), {"text", "axis", "given_in", "status", "verdicts"})
            for verdict in rec["verdicts"]:
                verdicts += 1
                self.assertEqual(set(verdict), {"period", "verdict", "comment"})
        self.assertTrue(verdicts)


class TestReviewerTrim(_Base):
    def test_older_points_lose_quotes_and_draft(self) -> None:
        root = self.fresh(people=2, periods=6)
        doc = _tracker(root)
        for run in doc["runs"]:
            run["gating_evidence"] = [{"trigger": "x", "session_id": "s", "turn_index": 1,
                                       "quote": "rm -rf build"}]
        _write_tracker(root, doc)
        model = rd.collect(root, lambda _m: None)
        pts = rd.build_payload(model, mode="reviewer", quote_periods=3)["people"][0]["points"]
        for pt in pts[:3]:
            self.assertTrue(pt["trimmed"])
            self.assertEqual(pt["feedback_draft"], "")
            self.assertTrue(pt["antipatterns"])
            self.assertTrue(all(ap["quote"] == "" for ap in pt["antipatterns"]))
            self.assertTrue(all(g["quote"] == "" for g in pt["gating_evidence"]))
        for pt in pts[3:]:
            self.assertNotIn("trimmed", pt)
            self.assertTrue(pt["feedback_draft"])
            self.assertTrue(all(ap["quote"] for ap in pt["antipatterns"]))
            self.assertTrue(all(g["quote"] for g in pt["gating_evidence"]))


DRAFT_REC_TEXT = "Текст рекомендации из неодобренного прогона, адресат его не получал."


class TestUnapprovedRuns(_Base):
    """При гейте ревьюера адресат видит только одобренные прогоны.

    Фикстура team (`reviewer_gate: true`): у каждого человека последний период — черновик.
    У anna P1 и P2 одобрены, P3 — живой черновик с новой рекомендацией и вердиктом
    `followed` по рекомендации P2.
    """

    def gated_model(self, mode: str = "team") -> tuple[dict, dict]:
        root = self.fresh(mode=mode) if mode == "self" else self.fresh()
        doc = _tracker(root)
        pid = "me" if mode == "self" else "anna"
        draft = _run_of(doc, pid, "P3")
        self.assertEqual(draft["review_status"], "draft")
        draft["recommendations"][0]["text"] = DRAFT_REC_TEXT
        draft["prev_recommendations"] = [{"id": f"REC-{pid}-002", "verdict": "followed",
                                          "evidence": None, "note": ""}]
        _write_tracker(root, doc)
        model, _warnings = _collect(root)
        return model, draft

    def test_model_marks_unapproved_points_and_recs(self) -> None:
        model, draft = self.gated_model()
        anna = [p for p in model["people"] if p["id"] == "anna"][0]
        self.assertEqual([pt["approved"] for pt in anna["points"]], [True, True, False])
        recs = {r["id"]: r for r in anna["recs"]}
        self.assertEqual({rid: r["pending_approval"] for rid, r in recs.items()},
                         {"REC-anna-001": False, "REC-anna-002": False, "REC-anna-003": True})
        verdict = recs["REC-anna-002"]["verdicts"][-1]
        self.assertEqual((verdict["verdict"], verdict["approved"]), ("followed", False))

    def test_personal_has_no_draft_recommendation_and_keeps_status(self) -> None:
        model, _draft = self.gated_model()
        payload = rd.build_payload(model, mode="personal", person="anna")
        self.assertNotIn(DRAFT_REC_TEXT, json.dumps(payload, ensure_ascii=False))
        recs = {r["given_in"]: r["status"] for r in payload["people"][0]["recs"]}
        self.assertEqual(recs, {"P1": "followed", "P2": "open"})

    def test_personal_points_counters_and_transitions_without_draft(self) -> None:
        model, _draft = self.gated_model()
        anna = [p for p in model["people"] if p["id"] == "anna"][0]
        approved = anna["points"][:2]
        # переходы — в виде trend (без баллов); в виде scores их заменяют графики
        payload = rd.build_payload(model, mode="personal", person="anna", show_scores=False)
        pts = payload["people"][0]["points"]
        self.assertEqual([pt["period"] for pt in pts], ["P1", "P2"])
        self.assertEqual(sum(pt["sessions"] for pt in pts), sum(pt["sessions"] for pt in approved))
        self.assertEqual(pts[-1]["transitions"], rd.transitions(approved, model["axes"])[-1])
        self.assertEqual(payload["meta"]["date_to"], approved[-1]["date"])
        self.assertEqual(payload["meta"]["date_from"], approved[0]["date"])

    def test_personal_without_approved_runs_refused(self) -> None:
        model, _draft = self.gated_model()
        anna = [p for p in model["people"] if p["id"] == "anna"][0]
        for pt in anna["points"]:
            pt["approved"] = False
        with self.assertRaises(SrError) as ctx:
            rd.build_payload(model, mode="personal", person="anna")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("anna", ctx.exception.message)

    def test_reviewer_keeps_draft_recommendation_with_flag(self) -> None:
        model, _draft = self.gated_model()
        payload = rd.build_payload(model, mode="reviewer")
        anna = [p for p in payload["people"] if p["id"] == "anna"][0]
        recs = {r["id"]: r for r in anna["recs"]}
        self.assertEqual(recs["REC-anna-003"]["text"], DRAFT_REC_TEXT)
        self.assertTrue(recs["REC-anna-003"]["pending_approval"])
        self.assertFalse(recs["REC-anna-002"]["pending_approval"])
        self.assertEqual([pt["approved"] for pt in anna["points"]], [True, True, False])

    def test_group_has_no_recommendations(self) -> None:
        model, _draft = self.gated_model()
        payload = rd.build_payload(model, mode="group")
        self.assertNotIn(DRAFT_REC_TEXT, json.dumps(payload, ensure_ascii=False))
        self.assertTrue(all(p["recs"] == [] for p in payload["people"]))

    def test_self_mode_all_runs_approved(self) -> None:
        model, _draft = self.gated_model(mode="self")
        me = model["people"][0]
        self.assertTrue(all(pt["approved"] for pt in me["points"]))
        self.assertFalse(any(r["pending_approval"] for r in me["recs"]))
        payload = rd.build_payload(model, mode="personal", person="me")
        self.assertEqual([pt["period"] for pt in payload["people"][0]["points"]], ["P1", "P2", "P3"])
        recs = {r["given_in"]: r["status"] for r in payload["people"][0]["recs"]}
        self.assertEqual(recs["P2"], "followed")
        self.assertIn(DRAFT_REC_TEXT, json.dumps(payload, ensure_ascii=False))


NOTE_P2 = "Тесты появились только в новом коде, старые файлы без проверок."
NOTE_P3 = "Каждое изменение закрыто тестом до коммита."
NOTE_DRAFT = "Черновой вердикт, адресат его не получал."


class TestPersonalVerdictHistory(_Base):
    """История вердиктов под рекомендацией в личном отчёте: только одобренные прогоны.

    Фикстура team: у anna P1 и P2 одобрены, P3 — черновик; REC-anna-001 (P1) получает
    вердикт в P2, REC-anna-002 (P2) — только в черновике P3.
    """

    def history(self, payload: dict) -> dict:
        return {r["given_in"]: r["verdicts"] for r in payload["people"][0]["recs"]}

    def four_periods(self) -> tuple[dict, list[str]]:
        """anna, P1–P3 одобрены, P4 черновик; по REC-anna-001 вердикты в P2, P3 и черновом P4.
        Прогоны в трекере переставлены задом наперёд: порядок — по периодам, не по файлу."""
        root = self.fresh(people=1, periods=4)
        doc = _tracker(root)
        verdicts = {"P2": ("partial", NOTE_P2), "P3": ("followed", NOTE_P3),
                    "P4": ("ignored", NOTE_DRAFT)}
        for label, (verdict, note) in verdicts.items():
            _run_of(doc, "anna", label)["prev_recommendations"] = [
                {"id": "REC-anna-001", "verdict": verdict, "evidence": None, "note": note}]
        self.assertEqual(_run_of(doc, "anna", "P4")["review_status"], "draft")
        doc["runs"].reverse()
        _write_tracker(root, doc)
        return _collect(root)

    def test_default_history_fields(self) -> None:
        hist = self.history(rd.build_payload(self.model, mode="personal", person="anna"))
        self.assertEqual(hist["P1"], [{"period": "P2", "verdict": "followed", "comment": ""}])
        # вердикт по REC-anna-002 вынесен только в черновике P3: истории нет
        self.assertEqual(hist["P2"], [])

    def test_history_ordered_by_period_with_comments_only_approved(self) -> None:
        model, _warnings = self.four_periods()
        payload = rd.build_payload(model, mode="personal", person="anna")
        self.assertEqual(self.history(payload)["P1"], [
            {"period": "P2", "verdict": "partial", "comment": NOTE_P2},
            {"period": "P3", "verdict": "followed", "comment": NOTE_P3},
        ])
        self.assertNotIn(NOTE_DRAFT, json.dumps(payload, ensure_ascii=False))
        # ревьюер видит и черновой вердикт — с пометкой
        reviewer = rd.build_payload(model, mode="reviewer")["people"][0]
        rec = [r for r in reviewer["recs"] if r["id"] == "REC-anna-001"][0]
        self.assertEqual([(v["period"], v["approved"]) for v in rec["verdicts"]],
                         [("P2", True), ("P3", True), ("P4", False)])

    def test_history_does_not_depend_on_show_scores(self) -> None:
        model, _warnings = self.four_periods()
        plain = rd.build_payload(model, mode="personal", person="anna")
        scored = rd.build_payload(model, mode="personal", person="anna", show_scores=True)
        self.assertEqual(plain["people"][0]["recs"], scored["people"][0]["recs"])
        self.assertFalse(_keys(plain["people"][0]["recs"]) & (rd.SCORE_KEYS | rd.PERSONAL_FORBIDDEN_KEYS))

    def test_draft_verdict_absent_in_gated_self_present(self) -> None:
        root = self.fresh(mode="self")
        model, _warnings = _collect(root)
        hist = self.history(rd.build_payload(model, mode="personal", person="me"))
        # self без гейта ревьюера: вердикт последнего периода показывается
        self.assertEqual([v["period"] for v in hist["P2"]], ["P3"])

    def test_unknown_verdict_code_skipped_with_warning(self) -> None:
        root = self.fresh()
        doc = _tracker(root)
        run = _run_of(doc, "anna", "P2")
        run["prev_recommendations"][0]["verdict"] = "у Бориса получилось"
        _write_tracker(root, doc)
        model, warnings = _collect(root)
        payload = rd.build_payload(model, mode="personal", person="anna")
        self.assertNotIn("Бориса", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.history(payload)["P1"], [])
        self.assertTrue(any(run["run_id"] in w and "вердикт" in w for w in warnings), warnings)


SUGGESTION_KEYS = {"id", "field", "op", "current", "proposed", "rationale", "source", "run_id"}
SUGGESTION_MARKS = (report_fixture.SUGGEST_PROPOSED, "SuggestTool", report_fixture.SUGGEST_APPEND,
                    "S-anna-00")


class TestCardSuggestions(unittest.TestCase):
    """Предложения к карточке: pending — только в payload ревьюера (`card_suggestions`)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = report_fixture.make_workspace(Path(cls._tmp.name) / "ws", suggestions=True)
        cls.model = rd.collect(cls.root, lambda _m: None)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def people(self, mode: str, **kw) -> dict:
        payload = rd.build_payload(self.model, mode=mode, title="T", **kw)
        return {p["id"]: p for p in payload["people"]}

    def test_reviewer_person_carries_pending_only(self):
        got = self.people("reviewer")
        items = got["anna"]["card_suggestions"]
        self.assertEqual([i["id"] for i in items], ["S-anna-001", "S-anna-002"])
        for item in items:
            self.assertEqual(set(item), SUGGESTION_KEYS)
        first = items[0]
        self.assertEqual((first["field"], first["op"], first["current"], first["proposed"]),
                         ("profile.tools", "set", "claude-code", report_fixture.SUGGEST_PROPOSED))
        self.assertEqual(first["rationale"], report_fixture.SUGGEST_REASON)
        self.assertEqual(first["source"], "script:agent_stack")
        self.assertTrue(first["run_id"])
        self.assertEqual(got["boris"]["card_suggestions"], [])
        text = json.dumps(got, ensure_ascii=False)
        for mark in report_fixture.SUGGEST_DECIDED:
            self.assertNotIn(mark, text, "applied/rejected в отчёт не идут")

    def test_personal_and_group_have_no_key_and_no_text(self):
        cases = [("personal", {"person": "anna"}), ("group", {}), ("group", {"details": True})]
        for mode, kw in cases:
            with self.subTest(mode=mode, **kw):
                payload = rd.attach_html(rd.build_payload(self.model, mode=mode, title="T", **kw))
                self.assertNotIn("card_suggestions", _keys(payload))
                text = json.dumps(payload, ensure_ascii=False)
                for mark in SUGGESTION_MARKS + (report_fixture.SUGGEST_REASON,):
                    self.assertNotIn(mark, text)

    def test_model_people_do_not_carry_suggestions(self):
        for person in self.model["people"]:
            self.assertNotIn("card_suggestions", person)

    def test_no_file_gives_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = report_fixture.make_workspace(Path(tmp) / "ws")
            model = rd.collect(root, lambda _m: None)
        payload = rd.build_payload(model, mode="reviewer")
        self.assertTrue(all(p["card_suggestions"] == [] for p in payload["people"]))

    def test_broken_file_warns_and_report_still_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = report_fixture.make_workspace(Path(tmp) / "ws")
            path = ws_paths(root)["people"] / "anna.suggestions.json"
            path.write_text("{not json", encoding="utf-8")
            warnings: list[str] = []
            model = rd.collect(root, warnings.append)
        self.assertTrue(any("anna.suggestions.json" in w for w in warnings), warnings)
        payload = rd.build_payload(model, mode="reviewer")
        self.assertEqual({p["id"]: p["card_suggestions"] for p in payload["people"]}["anna"], [])


SAMPLE = FIXTURES / "process-reviews.sample.json"
SAMPLE_REVIEWS = FIXTURES / "reviews"


@unittest.skipUnless(SAMPLE.exists(), "нет fixtures/process-reviews.sample.json (пишет оценка)")
class TestCompatWithAssessSample(unittest.TestCase):
    def test_collect_and_payload_all_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            shutil.copytree(FIXTURES / "workspace-min", root)
            paths = ws_paths(root)
            paths["runs"].mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SAMPLE, paths["runs"] / "process-reviews.json")
            if SAMPLE_REVIEWS.is_dir():
                shutil.copytree(SAMPLE_REVIEWS, paths["reviews"])
            group = load_config(root)["workspace"]["groups"][0]
            doc = json.loads(SAMPLE.read_text(encoding="utf-8"))
            for pid in sorted({r["person_id"] for r in doc["runs"]}):
                if not (paths["people"] / f"{pid}.yaml").exists():
                    add_person(root, pid, pid, group)
            model = rd.collect(root, lambda _m: None)
            self.assertTrue(model["people"])
            for mode in ("group", "reviewer"):
                rd.attach_html(rd.build_payload(model, mode=mode))
            rd.attach_html(rd.build_payload(model, mode="personal",
                                            person=model["people"][0]["id"]))


if __name__ == "__main__":
    unittest.main()
