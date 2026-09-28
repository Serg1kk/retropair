"""lib.card_suggestions: предложения правок карточки — генерация, дедуп, приём поля судьи,
правка текста карточки (apply) с откатом, reject, CLI `people suggestions|apply|reject`, doctor."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import _util
from _assess import make_ws, payload
from _util import copy_fixture, run_sr

from lib import card_suggestions as cs
from lib import cmd_people, i18n, people, report_data, tracker, yamlmini
from lib.config import load_config
from lib.workspace import SrError, write_json_atomic, ws_paths

AXES = i18n.keys("axes")
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def make_run(pid: str, label: str, *, status: str = "approved", stack=("claude-code",),
             scores: dict | None = None, retracted: bool = False, day: int = 1, revision: int = 1,
             extra: dict | None = None) -> dict:
    """Запись прогона трекера с теми полями, которые читает генератор."""
    scores = scores or {}
    axes = []
    for axis in AXES:
        score = scores.get(axis, 2)
        axes.append({"id": axis, "score": score, "insufficient_evidence": score is None,
                     "insufficient_evidence_reason": "short_log" if score is None else None,
                     "evidence": [], "subcriteria": [], "note": None})
    run = {
        "run_id": f"demo-team_{pid}_{label}_202609{day:02d}T100000Z",
        "person_id": pid,
        "period": {"label": label, "from": None, "to": None, "anchor": None},
        "reviewed_at": f"2026-09-{day:02d}T10:00:00Z",
        "revision": revision,
        "agent_stack": list(stack),
        "axes": axes,
        "operator_override": None,
        "review_status": status,
    }
    if retracted:
        run["retracted"] = {"at": f"2026-09-{day:02d}T11:00:00Z", "why": "повтор"}
    run.update(extra or {})
    return run


class Base(_util.TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-cardsug-")
        self.root = copy_fixture("workspace-min", Path(self._tmp.name))
        self.people_dir = ws_paths(self.root)["people"]
        self.warnings: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def tracker(self, *runs: dict) -> None:
        write_json_atomic(ws_paths(self.root)["runs"] / "process-reviews.json",
                          {"tracker_version": 2, "workspace": "demo-team", "updated_at": None,
                           "runs": list(runs)})

    def refresh(self, pid: str | None = "alex") -> list[dict]:
        return cs.refresh(self.root, pid, warn=self.warnings.append, now=NOW)

    def card(self, pid: str = "alex") -> Path:
        return self.people_dir / f"{pid}.yaml"

    def items(self, pid: str = "alex") -> list[dict]:
        return cs.load_items(self.root, pid)


class GenerateTest(Base):
    def test_agent_stack_last_approved_run(self):
        self.tracker(make_run("alex", "p1", stack=["codex"], day=1),
                     make_run("alex", "p2", stack=["Claude-Code", "cursor"], day=8),
                     make_run("alex", "p3", status="draft", stack=["windsurf"], day=15))
        new = self.refresh()
        self.assertEqual(len(new), 1)
        item = new[0]
        self.assertEqual(item["id"], "S-alex-001")
        self.assertEqual((item["field"], item["op"]), ("profile.tools", "set"))
        self.assertEqual(item["current"], "Claude Code")
        self.assertEqual(item["proposed"], "Claude Code, cursor")
        self.assertEqual(item["source"], "script:agent_stack")
        self.assertEqual(item["run_id"], "demo-team_alex_p2_20260908T100000Z")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["created_at"], "2026-09-28T12:00:00Z")
        self.assertIsNone(item["decided_at"])
        self.assertEqual(item["evidence"][0]["run_id"], item["run_id"])
        data = json.loads((self.people_dir / "alex.suggestions.json").read_text("utf-8"))
        self.assertEqual((data["version"], data["person_id"]), (1, "alex"))
        self.assertEqual(data["items"], new)

    def test_agent_stack_empty_tools_only_new(self):
        text = self.card().read_text("utf-8").replace("  tools: Claude Code\n", '  tools: ""\n')
        self.card().write_text(text, encoding="utf-8")
        self.tracker(make_run("alex", "p1", stack=["cursor"]))
        self.assertEqual(self.refresh()[0]["proposed"], "cursor")

    def test_agent_stack_nothing_new(self):
        self.tracker(make_run("alex", "p1", stack=["claude code"]))
        self.assertEqual(self.refresh(), [])
        self.assertFalse((self.people_dir / "alex.suggestions.json").exists())

    def test_agent_stack_version_tails_dropped(self):
        """Номера версий сборки отрезаются, дубли без регистра схлопываются, «Opus 5» остаётся."""
        self.tracker(make_run("alex", "p1", stack=[
            "Claude Code 2.1.261", "Claude Code 2.1.278", "Opus 5", "Cursor v1.2.3",
            "cursor", "Aider 0.86.1-dev", "codex@0.46.0", "opus 5"]))
        new = self.refresh()
        self.assertEqual([i["proposed"] for i in new], ["Claude Code, Opus 5, Cursor, Aider, codex"])
        self.assertIn("Opus 5, Cursor, Aider, codex", new[0]["reason"])
        self.assertNotIn("2.1", new[0]["reason"])

    def test_agent_stack_versions_compared_normalized(self):
        text = self.card().read_text("utf-8").replace("  tools: Claude Code\n",
                                                      "  tools: Claude Code 2.1.200, Opus 5\n")
        self.card().write_text(text, encoding="utf-8")
        self.tracker(make_run("alex", "p1", stack=["Claude Code 2.1.278", "opus 5"]))
        self.assertEqual(self.refresh(), [])

    def test_stable_axis_two_last_approved(self):
        strong = {"verification": 3, "task_framing": 3, "ownership": 4}
        self.tracker(
            make_run("alex", "p1", scores={"ownership": 4}, day=1),
            make_run("alex", "p2", scores={"verification": 4, "task_framing": None, "ownership": 2},
                     day=8),
            make_run("alex", "p3", scores=strong, day=15),
            make_run("alex", "p4", status="draft", scores={"verification": 0}, day=22),
        )
        new = [i for i in self.refresh() if i["source"] == "script:stable_axis"]
        self.assertEqual([(i["field"], i["op"], i["proposed"]) for i in new],
                         [("feedback.skip_basics", "append", [i18n.t("axes", "verification", "ru")])])
        self.assertEqual(new[0]["current"], ["что такое git"])

    def test_stable_axis_one_suggestion_lists_all_axes(self):
        """Все устойчиво сильные оси — одним предложением-списком, основание — одна строка."""
        strong = {"verification": 4, "task_framing": 3, "ownership": 4}
        self.tracker(make_run("alex", "p1", scores=strong, day=1),
                     make_run("alex", "p2", scores=strong, day=8))
        new = [i for i in self.refresh() if i["source"] == "script:stable_axis"]
        self.assertEqual(len(new), 1)
        names = [i18n.t("axes", a, "ru") for a in AXES if a in strong]
        self.assertEqual(len(names), 3)
        self.assertEqual((new[0]["field"], new[0]["op"], new[0]["proposed"]),
                         ("feedback.skip_basics", "append", names))
        self.assertNotIn("\n", new[0]["reason"])
        for name in names:
            self.assertIn(name, new[0]["reason"])

    def test_stable_axis_later_new_axis_only_it(self):
        """Прежнее предложение (в любом статусе) не повторяется: новое — только с новой осью."""
        self.tracker(make_run("alex", "p1", scores={"verification": 4}, day=1),
                     make_run("alex", "p2", scores={"verification": 4}, day=8))
        self.assertEqual(len(self.refresh()), 1)
        cs.reject(self.root, "alex", "S-alex-001", "рано", now=NOW)
        self.tracker(make_run("alex", "p1", scores={"verification": 4}, day=1),
                     make_run("alex", "p2", scores={"verification": 4, "ownership": 3}, day=8),
                     make_run("alex", "p3", scores={"verification": 4, "ownership": 4}, day=15))
        new = self.refresh()
        self.assertEqual([(i["id"], i["proposed"]) for i in new],
                         [("S-alex-002", [i18n.t("axes", "ownership", "ru")])])
        self.assertEqual(self.refresh(), [])

    def test_stable_axis_legacy_single_item_counts(self):
        """Старое одиночное append-предложение (строкой) закрывает свою ось для генератора."""
        name = i18n.t("axes", "verification", "ru")
        write_json_atomic(self.people_dir / "alex.suggestions.json", {
            "version": 1, "person_id": "alex", "items": [{
                "id": "S-alex-001", "field": "feedback.skip_basics", "op": "append",
                "current": ["что такое git"], "proposed": name, "reason": "r", "evidence": [],
                "source": "script:stable_axis", "run_id": "run-1", "created_at": "t",
                "status": "pending", "decided_at": None, "decided_by": None, "reject_reason": None}]})
        self.tracker(make_run("alex", "p1", scores={"verification": 4, "ownership": 4}, day=1),
                     make_run("alex", "p2", scores={"verification": 4, "ownership": 4}, day=8))
        new = self.refresh()
        self.assertEqual([i["proposed"] for i in new], [[i18n.t("axes", "ownership", "ru")]])

    def test_stable_axis_override_counts(self):
        over = {"operator_override": {"axes": {"decomposition": 3}, "why": "x", "by": "r", "at": "t"}}
        self.tracker(make_run("alex", "p1", scores={"decomposition": 4}, day=1),
                     make_run("alex", "p2", scores={"decomposition": 1}, day=8, extra=over))
        proposed = [i["proposed"] for i in self.refresh() if i["source"] == "script:stable_axis"]
        self.assertEqual(proposed, [[i18n.t("axes", "decomposition", "ru")]])

    def test_stable_axis_one_approved_nothing(self):
        self.tracker(make_run("alex", "p1", scores={"verification": 4}, day=1),
                     make_run("alex", "p2", status="draft", scores={"verification": 4}, day=8))
        self.assertEqual([i for i in self.refresh() if i["source"] == "script:stable_axis"], [])

    def test_stable_axis_revision_of_same_period_is_one_period(self):
        self.tracker(make_run("alex", "p1", scores={"verification": 4}, day=1),
                     make_run("alex", "p1", scores={"verification": 4}, day=2, revision=2))
        self.assertEqual([i for i in self.refresh() if i["source"] == "script:stable_axis"], [])

    def test_stable_axis_already_in_card(self):
        text = self.card().read_text("utf-8").replace(
            "    - что такое git\n", f"    - что такое git\n    - {i18n.t('axes', 'verification', 'ru')}\n")
        self.card().write_text(text, encoding="utf-8")
        self.tracker(make_run("alex", "p1", scores={"verification": 4}, day=1),
                     make_run("alex", "p2", scores={"verification": 4}, day=8))
        self.assertEqual([i for i in self.refresh() if i["source"] == "script:stable_axis"], [])

    def test_unapproved_and_retracted_give_nothing(self):
        self.tracker(
            make_run("alex", "p1", status="draft", stack=["cursor"], scores={"verification": 4}),
            make_run("alex", "p2", stack=["cursor"], scores={"verification": 4}, retracted=True, day=8),
            make_run("alex", "p3", status="reviewed", stack=["cursor"], scores={"verification": 4},
                     day=15),
        )
        self.assertEqual(self.refresh(), [])

    def test_no_tracker_no_suggestions(self):
        self.assertEqual(self.refresh(), [])

    def test_dedup_and_rejected_not_returned(self):
        self.tracker(make_run("alex", "p1", stack=["cursor"]))
        first = self.refresh()
        self.assertEqual(len(first), 1)
        self.assertEqual(self.refresh(), [])
        cs.reject(self.root, "alex", "S-alex-001", "не пользуется", now=NOW)
        self.assertEqual(self.refresh(), [])
        self.assertEqual(len(self.items()), 1)

    def test_refresh_all_people(self):
        self.tracker(make_run("alex", "p1", stack=["cursor"]), make_run("sam", "p1", stack=["aider"]))
        new = self.refresh(None)
        self.assertEqual(sorted(i["id"] for i in new), ["S-alex-001", "S-sam-001"])

    def test_next_number_continues(self):
        self.tracker(make_run("alex", "p1", stack=["cursor"], day=1))
        self.refresh()
        self.tracker(make_run("alex", "p1", stack=["cursor"], day=1),
                     make_run("alex", "p2", stack=["aider"], day=8))
        self.assertEqual([i["id"] for i in self.refresh()], ["S-alex-002"])

    def test_broken_suggestions_file_exit1(self):
        (self.people_dir / "alex.suggestions.json").write_text('{"version": 7}', encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            self.items()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("alex.suggestions.json", ctx.exception.message)


class JudgeFieldTest(Base):
    def judge_run(self, suggestions) -> dict:
        return make_run("alex", "p1", stack=["claude-code"], extra={"card_suggestions": suggestions})

    def test_judge_field_taken_from_tracker_record(self):
        """`field` судьи — ключ `profile` (форма схемы judge), предложение — `profile.<field>`."""
        self.tracker(self.judge_run([{
            "field": "constraints", "value": "закрытый контур", "reason": "в логах нет сети",
            "evidence": {"session_id": "s-1", "turn_index": 4, "quote": "сети тут нет"}}]))
        new = self.refresh()
        self.assertEqual(len(new), 1)
        item = new[0]
        self.assertEqual((item["field"], item["op"], item["source"]), ("profile.constraints", "set", "judge"))
        self.assertEqual((item["current"], item["proposed"]), ("нет прав на прод", "закрытый контур"))
        self.assertEqual(item["reason"], "в логах нет сети")
        self.assertEqual(item["evidence"], [{"run_id": item["run_id"], "session_id": "s-1",
                                             "turn_index": 4, "quote": "сети тут нет"}])
        self.assertEqual(self.warnings, [])

    def test_judge_years_string_becomes_int(self):
        self.tracker(self.judge_run([{"field": "years", "value": " 7 ", "reason": "r", "evidence": None}]))
        item = self.refresh()[0]
        self.assertEqual((item["field"], item["op"], item["current"], item["proposed"]),
                         ("profile.years", "set", 4, 7))

    def test_judge_full_path_and_feedback_skipped(self):
        """Схема судьи знает только ключи `profile`: полный путь и `feedback` не принимаются."""
        self.tracker(self.judge_run([
            {"field": "profile.tools", "value": "Cursor", "reason": "r", "evidence": None},
            {"field": "boundaries", "value": "без прода", "reason": "r", "evidence": None},
        ]))
        self.assertEqual(self.refresh(), [])
        self.assertEqual(len(self.warnings), 2, self.warnings)
        self.assertIn("profile.tools", self.warnings[0])
        self.assertIn("boundaries", self.warnings[1])

    def test_judge_field_outside_whitelist_skipped_with_warning(self):
        self.tracker(self.judge_run([
            {"field": "tone", "value": "supportive", "reason": "r", "evidence": None},
            "не объект",
            {"field": "years", "value": "много", "reason": "r", "evidence": None},
        ]))
        self.assertEqual(self.refresh(), [])
        # судья читается только в последнем одобренном периоде: p1 второй раз не предупреждает
        self.tracker(self.judge_run([]), make_run("alex", "p2", day=8, extra={"card_suggestions": [
            {"field": "stack", "value": "Go", "reason": "r", "evidence": None, "lol": 1}]}))
        self.assertEqual(self.refresh(), [])
        self.assertEqual(len(self.warnings), 4, self.warnings)
        self.assertIn("tone", self.warnings[0])
        self.assertIn("card_suggestions[1]", self.warnings[1])
        self.assertIn("years", self.warnings[2])
        self.assertIn("lol", self.warnings[3])

    def test_judge_more_than_three_warns(self):
        many = [{"field": "stack", "value": f"v{i}", "reason": "r", "evidence": None} for i in range(4)]
        self.tracker(self.judge_run(many))
        self.assertEqual(len(self.refresh()), 3)
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("3", self.warnings[0])

    def test_judge_field_not_list_warns(self):
        self.tracker(self.judge_run({"field": "stack"}))
        self.assertEqual(self.refresh(), [])
        self.assertEqual(len(self.warnings), 1)

    def test_default_warn_goes_to_stderr(self):
        self.tracker(self.judge_run([{"field": "tone", "value": "x", "reason": "r",
                                      "evidence": None}]))
        with mock.patch("sys.stderr") as err:
            cs.refresh(self.root, "alex", now=NOW)
        self.assertIn("tone", "".join(c.args[0] for c in err.write.call_args_list))


class JudgeAppendFlowTest(_util.TempHome, unittest.TestCase):
    """Выход судьи → `append` → одобрение → генератор: предложение с источником `judge`."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-cardsug-flow-")
        self.addCleanup(self._tmp.cleanup)
        self.root = make_ws(Path(self._tmp.name))

    def test_append_then_generator_gives_judge_suggestion(self):
        item = {"field": "tools", "value": "Claude Code, Cursor", "reason": "в логах правки идут в Cursor",
                "evidence": {"session_id": "s-1", "turn_index": 3, "quote": "открыл это в Cursor"}}
        path = ws_paths(self.root)["pending"] / "alex" / "judge.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload(card_suggestions=[item]), ensure_ascii=False), encoding="utf-8")
        run_id = tracker.append(self.root, path, "alex", period_label="p1", now=NOW)["run_id"]
        self.assertEqual(cs.refresh(self.root, "alex", warn=self.fail, now=NOW), [],
                         "черновик предложений не даёт")
        tracker.update_run(self.root, run_id, review_status="approved")
        judge = [i for i in cs.refresh(self.root, "alex", warn=self.fail, now=NOW) if i["source"] == "judge"]
        self.assertEqual(len(judge), 1)
        self.assertEqual((judge[0]["field"], judge[0]["op"], judge[0]["current"], judge[0]["proposed"],
                          judge[0]["run_id"]),
                         ("profile.tools", "set", "Claude Code", "Claude Code, Cursor", run_id))
        self.assertEqual(judge[0]["evidence"], [{"run_id": run_id, "session_id": "s-1", "turn_index": 3,
                                                 "quote": "открыл это в Cursor"}])


class EditTextTest(unittest.TestCase):
    """Правка текста карточки без дампера."""

    def test_set_keeps_comment_column(self):
        text = "id: a\nprofile:\n  tools: Claude Code             # инструменты\n  stack: Go\n"
        out = cs.edit_card_text(text, "profile.tools", "set", "Claude Code, cursor")
        self.assertEqual(out, 'id: a\nprofile:\n  tools: "Claude Code, cursor"   # инструменты\n'
                              "  stack: Go\n")

    def test_set_long_value_one_space_before_comment(self):
        text = "profile:\n  tools: x # c\n"
        out = cs.edit_card_text(text, "profile.tools", "set", "long value here")
        self.assertEqual(out, 'profile:\n  tools: "long value here" # c\n')

    def test_set_number_and_quote(self):
        text = "profile:\n  years: null\n  role: x\n"
        self.assertEqual(cs.edit_card_text(text, "profile.years", "set", 5),
                         "profile:\n  years: 5\n  role: x\n")
        self.assertEqual(cs.edit_card_text(text, "profile.role", "set", 'a "b"'),
                         "profile:\n  years: null\n  role: 'a \"b\"'\n")

    def test_set_missing_key_added_at_block_end(self):
        text = "profile:\n  role: x\n# комментарий\nfeedback:\n  address: ты\n"
        out = cs.edit_card_text(text, "profile.tools", "set", "cursor")
        self.assertEqual(out, 'profile:\n  role: x\n  tools: "cursor"\n# комментарий\n'
                              "feedback:\n  address: ты\n")

    def test_missing_block_added_at_file_end(self):
        text = "id: a\nname: b\n"
        self.assertEqual(cs.edit_card_text(text, "profile.tools", "set", "cursor"),
                         'id: a\nname: b\nprofile:\n  tools: "cursor"\n')
        self.assertEqual(cs.edit_card_text(text, "feedback.avoid", "append", "x", base=["a"]),
                         'id: a\nname: b\nfeedback:\n  avoid:\n    - "a"\n    - "x"\n')

    def test_append_block_list_after_last_item(self):
        text = ("feedback:\n  skip_basics:   # c\n    - one\n    # между\n    - two\n"
                "  avoid:\nsources:\n")
        out = cs.edit_card_text(text, "feedback.skip_basics", "append", "три")
        self.assertEqual(out, ("feedback:\n  skip_basics:   # c\n    - one\n    # между\n    - two\n"
                               '    - "три"\n  avoid:\nsources:\n'))

    def test_append_same_indent_list(self):
        text = "feedback:\n  avoid:\n  - one\n  tone: direct\n"
        out = cs.edit_card_text(text, "feedback.avoid", "append", "x")
        self.assertEqual(out, 'feedback:\n  avoid:\n  - one\n  - "x"\n  tone: direct\n')

    def test_append_empty_key_writes_base_and_new(self):
        text = "feedback:\n  avoid:        # чего не делать\n  tone: direct\n"
        out = cs.edit_card_text(text, "feedback.avoid", "append", "x", base=["d"])
        self.assertEqual(out, 'feedback:\n  avoid:        # чего не делать\n    - "d"\n    - "x"\n'
                              "  tone: direct\n")

    def test_append_flow_list_refused(self):
        """Flow `[…]` yamlmini не читает — такую карточку правят руками."""
        for text in ('feedback:\n  avoid: [one, "two"]   # c\n', "feedback:\n  avoid: []\n"):
            with self.assertRaises(SrError) as ctx:
                cs.edit_card_text(text, "feedback.avoid", "append", "x")
            self.assertEqual(ctx.exception.code, 1)

    def test_append_list_value_block(self):
        text = "feedback:\n  skip_basics:\n    - one\n  tone: direct\n"
        self.assertEqual(cs.edit_card_text(text, "feedback.skip_basics", "append", ["a", "b"]),
                         'feedback:\n  skip_basics:\n    - one\n    - "a"\n    - "b"\n  tone: direct\n')
        self.assertEqual(cs.edit_card_text("feedback:\n  avoid:\n", "feedback.avoid", "append",
                                           ["a", "b"], base=["d"]),
                         'feedback:\n  avoid:\n    - "d"\n    - "a"\n    - "b"\n')

    def test_append_to_scalar_refused(self):
        with self.assertRaises(SrError) as ctx:
            cs.edit_card_text("feedback:\n  avoid: one\n", "feedback.avoid", "append", "x")
        self.assertEqual(ctx.exception.code, 1)

    def test_inline_block_refused(self):
        with self.assertRaises(SrError) as ctx:
            cs.edit_card_text("profile: null\n", "profile.tools", "set", "x")
        self.assertEqual(ctx.exception.code, 1)


class ApplyTest(Base):
    def put(self, **item) -> str:
        """Положить pending-предложение в файл руками; вернуть id."""
        base = {"id": "S-alex-001", "field": "profile.tools", "op": "set", "current": "Claude Code",
                "proposed": "Claude Code, cursor", "reason": "r", "evidence": [],
                "source": "judge", "run_id": "run-1", "created_at": "2026-09-28T12:00:00Z",
                "status": "pending", "decided_at": None, "decided_by": None, "reject_reason": None}
        base.update(item)
        path = self.people_dir / "alex.suggestions.json"
        data = (json.loads(path.read_text("utf-8")) if path.is_file()
                else {"version": 1, "person_id": "alex", "items": []})
        data["items"].append(base)
        write_json_atomic(path, data)
        return base["id"]

    def test_apply_string_keeps_comment_and_rest_bytes(self):
        text = self.card().read_text("utf-8").replace(
            "  tools: Claude Code\n", "  tools: Claude Code                # инструменты\n")
        self.card().write_text(text, encoding="utf-8")
        before = self.card().read_text("utf-8").splitlines(keepends=True)
        sid = self.put()
        item = cs.apply(self.root, "alex", sid, now=NOW)
        self.assertEqual((item["status"], item["decided_at"], item["decided_by"]),
                         ("applied", "2026-09-28T12:00:00Z", "cli"))
        after = self.card().read_text("utf-8").splitlines(keepends=True)
        self.assertEqual(len(before), len(after))
        diff = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
        self.assertEqual(len(diff), 1)
        new, old = after[diff[0]], before[diff[0]]
        self.assertTrue(new.startswith('  tools: "Claude Code, cursor" '), new)
        self.assertEqual(new.index("# инструменты"), old.index("# инструменты"))
        self.assertEqual(people.load_person(self.root, "alex")["profile"]["tools"],
                         "Claude Code, cursor")
        self.assertEqual(self.items()[0]["status"], "applied")

    def test_apply_block_list(self):
        before = self.card().read_text("utf-8")
        sid = self.put(field="feedback.skip_basics", op="append", current=["что такое git"],
                       proposed="Проверка результата")
        cs.apply(self.root, "alex", sid)
        after = self.card().read_text("utf-8")
        self.assertEqual(after, before.replace("    - что такое git\n",
                                               '    - что такое git\n    - "Проверка результата"\n'))
        self.assertEqual(people.load_person(self.root, "alex")["feedback"]["skip_basics"],
                         ["что такое git", "Проверка результата"])

    def test_apply_list_proposal_adds_all(self):
        before = self.card().read_text("utf-8")
        sid = self.put(field="feedback.skip_basics", op="append", current=["что такое git"],
                       proposed=["Проверка результата", "Постановка задачи"])
        item = cs.apply(self.root, "alex", sid)
        self.assertEqual(item["status"], "applied")
        self.assertEqual(self.card().read_text("utf-8"), before.replace(
            "    - что такое git\n",
            '    - что такое git\n    - "Проверка результата"\n    - "Постановка задачи"\n'))
        self.assertEqual(people.load_person(self.root, "alex")["feedback"]["skip_basics"],
                         ["что такое git", "Проверка результата", "Постановка задачи"])

    def test_apply_list_skips_items_already_in_card(self):
        sid = self.put(field="feedback.skip_basics", op="append", current=["что такое git"],
                       proposed=["Что такое Git", "Проверка результата"])
        cs.apply(self.root, "alex", sid)
        self.assertEqual(people.load_person(self.root, "alex")["feedback"]["skip_basics"],
                         ["что такое git", "Проверка результата"])

    def test_apply_list_all_present_exit1(self):
        before = self.card().read_bytes()
        sid = self.put(field="feedback.skip_basics", op="append", current=[],
                       proposed=["что такое git"])
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "alex", sid)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.card().read_bytes(), before)

    def test_apply_list_key_missing_in_feedback(self):
        sid = self.put(field="feedback.avoid", op="append", current=[], proposed="сравнения")
        cs.apply(self.root, "alex", sid)
        self.assertEqual(people.load_person(self.root, "alex")["feedback"]["avoid"], ["сравнения"])

    def test_apply_flow_list_card_refused_before_write(self):
        """Flow-список `[…]` мини-парсер карточек не читает: карточка битая ещё до правки — код 1,
        файл не тронут."""
        text = self.card().read_text("utf-8").replace(
            "  skip_basics:\n    - что такое git\n", "  skip_basics: [что такое git]\n")
        self.card().write_text(text, encoding="utf-8")
        sid = self.put(field="feedback.skip_basics", op="append", current=["что такое git"],
                       proposed="x")
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "alex", sid)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.card().read_text("utf-8"), text)
        self.assertEqual(self.items()[0]["status"], "pending")

    def test_invalid_value_rolls_back_exit1(self):
        before = self.card().read_bytes()
        sid = self.put(field="profile.years", current=4, proposed=-3)
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "alex", sid)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("profile.years", ctx.exception.message)
        self.assertEqual(self.card().read_bytes(), before)
        self.assertEqual(self.items()[0]["status"], "pending")

    def test_read_back_mismatch_rolls_back(self):
        before = self.card().read_bytes()
        sid = self.put()
        with mock.patch.object(cs, "render_scalar", return_value='"другое"'):
            with self.assertRaises(SrError) as ctx:
                cs.apply(self.root, "alex", sid)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.card().read_bytes(), before)

    def test_stale_current_exit1(self):
        before = self.card().read_bytes()
        sid = self.put(current="Cursor")
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "alex", sid)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.card().read_bytes(), before)

    def test_repeat_apply_exit1(self):
        sid = self.put()
        cs.apply(self.root, "alex", sid)
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "alex", sid)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("applied", ctx.exception.message)

    def test_unknown_id_exit1(self):
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "alex", "S-alex-404")
        self.assertEqual(ctx.exception.code, 1)

    def test_default_card_never_edited(self):
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "_default", "S-x-001")
        self.assertEqual(ctx.exception.code, 2)

    def test_reject(self):
        before = self.card().read_bytes()
        sid = self.put()
        item = cs.reject(self.root, "alex", sid, "не нужно", now=NOW)
        self.assertEqual((item["status"], item["reject_reason"], item["decided_by"], item["decided_at"]),
                         ("rejected", "не нужно", "cli", "2026-09-28T12:00:00Z"))
        self.assertEqual(self.card().read_bytes(), before)
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "alex", sid)
        self.assertEqual(ctx.exception.code, 1)
        with self.assertRaises(SrError):
            cs.reject(self.root, "alex", sid, None)

    def test_pending_count_and_doctor(self):
        self.assertEqual(cs.pending_count(self.root), 0)
        self.put()
        self.put(id="S-alex-002", proposed="x", status="rejected")
        self.assertEqual(cs.pending_count(self.root), 1)
        self.assertEqual(cmd_people.doctor_lines(self.root), [("card_suggestions_pending", 1)])
        self.assertEqual(cmd_people.doctor_lines(None), [])


class StaleTest(Base):
    """Устаревшие pending закрываются статусом `stale`: карточку поправили руками, судья
    прислал новое значение того же поля, другое предложение уже применено."""

    def set_tools(self, value: str) -> None:
        text = self.card().read_text("utf-8").replace("  tools: Claude Code\n", f"  tools: {value}\n")
        self.card().write_text(text, encoding="utf-8")

    def test_refresh_closes_set_changed_by_hand(self):
        self.tracker(make_run("alex", "p1", stack=["cursor"]))
        self.assertEqual([i["id"] for i in self.refresh()], ["S-alex-001"])
        self.set_tools("Claude Code, Codex")
        new = self.refresh()
        self.assertEqual([(i["id"], i["proposed"]) for i in new], [("S-alex-002", "Claude Code, Codex, cursor")])
        old = self.items()[0]
        self.assertEqual((old["status"], old["decided_by"], old["decided_at"]), ("stale", "refresh", "2026-09-28T12:00:00Z"))
        self.assertEqual([i["id"] for i in cs.listing(self.root, "alex")], ["S-alex-002"])
        self.assertEqual(cs.pending_count(self.root), 1)

    def test_refresh_closes_stale_without_new_candidates(self):
        self.tracker(make_run("alex", "p1", stack=["cursor"]))
        self.refresh()
        self.set_tools("cursor, Claude Code")
        self.assertEqual(self.refresh(), [])
        self.assertEqual(self.items()[0]["status"], "stale")
        self.assertEqual(cs.pending_count(self.root), 0)

    def test_stale_set_can_come_back(self):
        """`stale` не отказ: вернули карточку как была — предложение создаётся снова."""
        self.tracker(make_run("alex", "p1", stack=["cursor"]))
        self.refresh()
        self.set_tools("Codex")
        self.refresh()
        text = self.card().read_text("utf-8").replace("  tools: Codex\n", "  tools: Claude Code\n")
        self.card().write_text(text, encoding="utf-8")
        new = self.refresh()
        self.assertEqual([(i["id"], i["proposed"]) for i in new], [("S-alex-003", "Claude Code, cursor")])
        self.assertEqual([i["status"] for i in self.items()], ["stale", "stale", "pending"])

    def test_refresh_closes_append_all_in_card(self):
        name = i18n.t("axes", "verification", "ru")
        self.tracker(make_run("alex", "p1", scores={"verification": 4}, stack=["claude code"], day=1),
                     make_run("alex", "p2", scores={"verification": 4}, stack=["claude code"], day=8))
        self.assertEqual(len(self.refresh()), 1)
        text = self.card().read_text("utf-8").replace("    - что такое git\n", f"    - что такое git\n    - {name}\n")
        self.card().write_text(text, encoding="utf-8")
        self.assertEqual(self.refresh(), [])
        self.assertEqual(self.items()[0]["status"], "stale")

    def test_append_partly_in_card_stays_pending(self):
        names = [i18n.t("axes", a, "ru") for a in ("verification", "ownership")]
        strong = {"verification": 4, "ownership": 4}
        self.tracker(make_run("alex", "p1", scores=strong, stack=["claude code"], day=1),
                     make_run("alex", "p2", scores=strong, stack=["claude code"], day=8))
        self.refresh()
        text = self.card().read_text("utf-8").replace("    - что такое git\n", f"    - что такое git\n    - {names[0]}\n")
        self.card().write_text(text, encoding="utf-8")
        self.refresh()
        self.assertEqual(self.items()[0]["status"], "pending")

    def judge(self, label: str, value: str, day: int) -> dict:
        return make_run("alex", label, stack=["claude code"], day=day, extra={"card_suggestions": [
            {"field": "stack", "value": value, "reason": "r", "evidence": None}]})

    def test_judge_only_last_approved_period(self):
        self.tracker(self.judge("p1", "Old", 1), self.judge("p2", "New", 8))
        new = self.refresh()
        self.assertEqual([(i["field"], i["proposed"]) for i in new], [("profile.stack", "New")])

    def test_new_judge_value_supersedes_old_pending(self):
        self.tracker(self.judge("p1", "Old", 1))
        self.assertEqual([i["proposed"] for i in self.refresh()], ["Old"])
        self.tracker(self.judge("p1", "Old", 1), self.judge("p2", "New", 8))
        self.assertEqual([i["proposed"] for i in self.refresh()], ["New"])
        self.assertEqual([(i["proposed"], i["status"], i["decided_by"]) for i in self.items()],
                         [("Old", "stale", "refresh"), ("New", "pending", None)])

    def test_apply_closes_other_pending_of_same_field(self):
        self.tracker(make_run("alex", "p1", stack=["cursor"], extra={"card_suggestions": [
            {"field": "tools", "value": "Claude Code, Aider", "reason": "r", "evidence": None}]}))
        new = self.refresh()
        self.assertEqual(len(new), 2)
        judge = next(i for i in new if i["source"] == "judge")
        cs.apply(self.root, "alex", judge["id"], now=NOW)
        statuses = {i["source"]: (i["status"], i["decided_by"]) for i in self.items()}
        self.assertEqual(statuses, {"judge": ("applied", "cli"), "script:agent_stack": ("stale", "apply")})

    def test_apply_stale_message_points_to_refresh_and_reject(self):
        self.tracker(make_run("alex", "p1", stack=["cursor"]))
        self.refresh()
        self.set_tools("Codex")
        with self.assertRaises(SrError) as ctx:
            cs.apply(self.root, "alex", "S-alex-001")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("people suggestions alex --refresh", ctx.exception.message)
        self.assertIn("people reject alex S-alex-001", ctx.exception.message)

    def test_old_file_statuses_read_and_stale_not_in_report(self):
        write_json_atomic(self.people_dir / "alex.suggestions.json", {
            "version": 1, "person_id": "alex", "items": [
                {"id": f"S-alex-00{n}", "field": "profile.tools", "op": "set", "current": "Claude Code",
                 "proposed": f"v{n}", "reason": "r", "evidence": [], "source": "judge", "run_id": "r",
                 "created_at": "t", "status": status, "decided_at": None, "decided_by": None,
                 "reject_reason": None}
                for n, status in enumerate(("pending", "applied", "rejected", "stale"), 1)]})
        self.assertEqual(set(cs.STATUSES), {"pending", "applied", "rejected", "stale"})
        self.assertEqual([i["id"] for i in cs.listing(self.root, "alex")], ["S-alex-001"])
        got = report_data._card_suggestions(self.root, ["alex"], self.fail)
        self.assertEqual([i["id"] for i in got["alex"]], ["S-alex-001"])


class TemplateCardTest(Base):
    """Карточка из `people add` (шаблон): блок `feedback:` закомментирован."""

    def setUp(self) -> None:
        super().setUp()
        people.add_person(self.root, "kim", "Kim", load_config(self.root)["workspace"]["groups"][0])

    def test_apply_append_uncomments_header_and_key(self):
        before = self.card("kim").read_text("utf-8")
        self.assertIn("\n# feedback:\n", before)
        write_json_atomic(self.people_dir / "kim.suggestions.json", {
            "version": 1, "person_id": "kim", "items": [{
                "id": "S-kim-001", "field": "feedback.skip_basics", "op": "append", "current": [],
                "proposed": ["Проверка результата"], "reason": "r", "evidence": [],
                "source": "script:stable_axis", "run_id": "r", "created_at": "t", "status": "pending",
                "decided_at": None, "decided_by": None, "reject_reason": None}]})
        cs.apply(self.root, "kim", "S-kim-001", now=NOW)
        after = self.card("kim").read_text("utf-8")
        lines = after.splitlines()
        self.assertEqual([ln for ln in lines if ln.startswith("feedback:")], ["feedback:"])
        self.assertFalse(any(ln.startswith("# feedback:") for ln in lines))
        self.assertEqual(sum(1 for ln in lines if ln.lstrip("# ").startswith("skip_basics:")), 1)
        # остальной текст на месте: без двух раскомментированных строк и нового пункта — как был
        skip = [ln for ln in before.splitlines() if ln.startswith("#   skip_basics:")][0]
        self.assertIn("  skip_basics:" + skip[len("#   skip_basics:"):], lines)
        self.assertEqual([ln for ln in lines if ln not in ("feedback:", '    - "Проверка результата"')
                          and not ln.startswith("  skip_basics:")],
                         [ln for ln in before.splitlines() if ln not in ("# feedback:", skip)])
        card = people.load_person(self.root, "kim")
        self.assertEqual(card["feedback"]["skip_basics"], ["Проверка результата"])
        # ревьюер, как велит шаблон, раскомментирует ещё строку: тон и skip_basics вместе
        tone = [ln for ln in lines if ln.startswith("#   tone:")][0]
        self.card("kim").write_text(after.replace(tone + "\n", "  tone: supportive\n"), encoding="utf-8")
        card = people.load_person(self.root, "kim")
        self.assertEqual((card["feedback"]["tone"], card["feedback"]["skip_basics"]),
                         ("supportive", ["Проверка результата"]))

    def test_edit_commented_block_key_absent_inserted_after_header(self):
        text = "id: a\n# feedback:\n#   tone: direct\nsources:\n"
        out = cs.edit_card_text(text, "feedback.avoid", "append", ["x"], base=["d"])
        self.assertEqual(out, 'id: a\nfeedback:\n  avoid:\n    - "d"\n    - "x"\n#   tone: direct\nsources:\n')

    def test_edit_commented_block_key_uncommented_in_place(self):
        text = "id: a\n# feedback:\n#   tone: direct\n#   avoid:   # c\n#   prefix: \"\"\nsources:\n"
        out = cs.edit_card_text(text, "feedback.avoid", "append", ["x"], base=["d"])
        self.assertEqual(out, 'id: a\nfeedback:\n#   tone: direct\n  avoid:   # c\n    - "d"\n    - "x"\n'
                              '#   prefix: ""\nsources:\n')

    def test_active_block_wins_over_commented(self):
        text = "# feedback:\n#   avoid:\nfeedback:\n  tone: direct\n"
        out = cs.edit_card_text(text, "feedback.avoid", "append", ["x"])
        self.assertEqual(out, '# feedback:\n#   avoid:\nfeedback:\n  tone: direct\n  avoid:\n    - "x"\n')


class RobustApplyTest(Base):
    """Minor ревью: обе кавычки, откат на любом исключении, запись статуса, симлинк, CRLF."""

    put = ApplyTest.put

    def test_both_quotes_value_reads_back(self):
        for value in ('it\'s "x"', '"a" and \'b\'', "a 'b' \"c\" # d"):
            rendered = cs.render_scalar(value)
            self.assertEqual(yamlmini.load(f"k: {rendered}   # c\n"), {"k": value}, rendered)

    def test_both_quotes_unwritable_and_newline_refused(self):
        for value in ("\" #x '", "a\nb"):
            with self.assertRaises(SrError) as ctx:
                cs.render_scalar(value)
            self.assertEqual(ctx.exception.code, 2)

    def test_apply_both_quotes(self):
        sid = self.put(field="profile.role", current="backend developer", proposed='it\'s "lead"')
        cs.apply(self.root, "alex", sid)
        self.assertEqual(people.load_person(self.root, "alex")["profile"]["role"], 'it\'s "lead"')

    def test_judge_unwritable_value_skipped_with_warning(self):
        self.tracker(make_run("alex", "p1", stack=["claude code"], extra={"card_suggestions": [
            {"field": "stack", "value": "\" #x '", "reason": "r", "evidence": None},
            {"field": "role", "value": 'it\'s "lead"', "reason": "r", "evidence": None}]}))
        new = self.refresh()
        self.assertEqual([i["proposed"] for i in new], ['it\'s "lead"'])
        self.assertEqual(len(self.warnings), 1, self.warnings)
        self.assertIn("profile.stack", self.warnings[0])

    def test_any_exception_after_write_restores_card(self):
        before = self.card().read_bytes()
        sid = self.put()
        card = people.load_person(self.root, "alex")
        with mock.patch.object(people, "load_person", side_effect=[card, OSError("диск")]):
            with self.assertRaises(OSError):
                cs.apply(self.root, "alex", sid)
        self.assertEqual(self.card().read_bytes(), before)
        self.assertEqual(self.items()[0]["status"], "pending")

    def test_status_write_failure_restores_card(self):
        before = self.card().read_bytes()
        sid = self.put()
        with mock.patch.object(cs, "write_json_atomic", side_effect=OSError("диск")):
            with self.assertRaises(OSError):
                cs.apply(self.root, "alex", sid)
        self.assertEqual(self.card().read_bytes(), before)
        self.assertEqual(self.items()[0]["status"], "pending")

    def test_symlink_card_stays_symlink(self):
        real = Path(self._tmp.name) / "real-alex.yaml"
        real.write_bytes(self.card().read_bytes())
        self.card().unlink()
        self.card().symlink_to(real)
        sid = self.put()
        cs.apply(self.root, "alex", sid)
        self.assertTrue(self.card().is_symlink())
        self.assertIn('tools: "Claude Code, cursor"', real.read_text("utf-8"))

    def test_crlf_card_keeps_crlf(self):
        raw = self.card().read_bytes().replace(b"\r\n", b"\n")
        self.card().write_bytes(raw.replace(b"\n", b"\r\n"))
        sid = self.put()
        cs.apply(self.root, "alex", sid)
        after = self.card().read_bytes()
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""))
        self.assertIn(b'  tools: "Claude Code, cursor"\r\n', after)


class CliTest(Base):
    def sr(self, *args: str):
        return run_sr(*args, home=self.home, cwd=self.root)

    def test_suggestions_refresh_apply_reject(self):
        self.tracker(make_run("alex", "p1", stack=["cursor"], scores={"verification": 4}, day=1),
                     make_run("alex", "p2", stack=["cursor"], scores={"verification": 4}, day=8))
        res = self.sr("people", "suggestions", "alex")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse((self.people_dir / "alex.suggestions.json").exists())
        self.assertIn(i18n.t(("cli", "setup"), "card_none", "ru"), res.stdout)

        res = self.sr("people", "suggestions", "alex", "--refresh")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('S-alex-001 profile.tools: "Claude Code" → "Claude Code, cursor" '
                      "(script:agent_stack, run demo-team_alex_p2_20260908T100000Z)", res.stdout)
        self.assertIn("sr.py people apply alex S-alex-001", res.stdout)
        self.assertIn("sr.py people reject alex S-alex-002", res.stdout)
        self.assertIn(f'S-alex-002 feedback.skip_basics: + "{i18n.t("axes", "verification", "ru")}" '
                      "(script:stable_axis", res.stdout)

        res = self.sr("people", "apply", "alex", "S-alex-001")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('tools: "Claude Code, cursor"', self.card().read_text("utf-8"))
        res = self.sr("people", "apply", "alex", "S-alex-001")
        self.assertEqual(res.returncode, 1)
        self.assertIn("applied", res.stderr)

        res = self.sr("people", "reject", "alex", "S-alex-002", "--reason", "сам решу")
        self.assertEqual(res.returncode, 0, res.stderr)

        res = self.sr("people", "suggestions", "alex")
        self.assertNotIn("S-alex-001", res.stdout)
        res = self.sr("people", "suggestions", "--all", "--json")
        data = json.loads(res.stdout)
        self.assertEqual([(i["person_id"], i["id"], i["status"]) for i in data["items"]],
                         [("alex", "S-alex-001", "applied"), ("alex", "S-alex-002", "rejected")])
        self.assertEqual(data["new"], [])

        res = self.sr("doctor", "--json")
        self.assertEqual(json.loads(res.stdout)["card_suggestions_pending"], 0)

    def test_refresh_warning_in_output(self):
        self.tracker(make_run("alex", "p1", extra={"card_suggestions": [
            {"field": "feedback.channel", "value": "x", "reason": "r", "evidence": None}]}))
        res = self.sr("people", "suggestions", "alex", "--refresh")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("feedback.channel", res.stdout)

    def test_unknown_person_exit2(self):
        self.assertEqual(self.sr("people", "suggestions", "nobody").returncode, 2)
        self.assertEqual(self.sr("people", "apply", "nobody", "S-x-001").returncode, 2)


if __name__ == "__main__":
    unittest.main()
