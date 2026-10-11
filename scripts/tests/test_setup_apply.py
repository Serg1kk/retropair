"""Онбординг: `setup.apply` (ответы → workspace), `setup.state`, команды `setup-apply`/`setup-state`.

Проект — временная папка без git (корень = cwd), HOME подменён (`TempHome` / `run_sr(home=…)`).
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

import _util
from _util import PLUGIN, TempHome, run_sr

from lib import i18n, registry, setup
from lib.config import load_config
from lib.people import card_path
from lib.sources import munge
from lib.workspace import SrError, ws_paths

SELF = {"mode": "self", "title": "Pet", "lang": "ru", "hook": True, "gitignore": True,
        "models": {"collector": {"model": "haiku", "effort": "low"}}}
TEAM = {"mode": "team", "title": "Team", "people": [
          {"id": "alex", "name": "Alex Demo", "group": "team-a",
           "sources": [{"type": "specstory", "path": "/home/user/alex/.specstory/history"}]},
          {"id": "sam", "name": "Sam Demo", "group": "team-b"}]}
COURSE = {"mode": "course", "title": "Flow", "people": [{"id": "kim", "name": "Kim Demo"}],
          "period": {"mode": "labels"}, "show_scores": True, "template": "workspace"}

ALEX_SRC = {"type": "specstory", "path": "/home/user/alex/.specstory/history", "person": "alex"}


class _Proj(TempHome, unittest.TestCase):
    """Временный HOME и папка проекта `proj` рядом с ним."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-setup-")
        self.proj = Path(self._tmp.name).resolve() / "proj"
        self.proj.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def apply(self, answers: dict) -> dict:
        return setup.apply(copy.deepcopy(answers), self.proj, self.home)

    def cfg(self) -> dict:
        return load_config(self.proj)

    def sr(self, *args: str, stdin: str | None = None):
        if stdin is None:
            return run_sr(*args, home=self.home, cwd=self.proj)
        import subprocess
        import sys
        env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(_util.SCRIPTS / "sr.py"), *args],
                              cwd=str(self.proj), env=env, text=True, capture_output=True,
                              input=stdin)

    def answers_file(self, data) -> Path:
        path = Path(self._tmp.name) / "answers.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return path


class ApplyNew(_Proj):
    def assert_base_files(self) -> None:
        paths = ws_paths(self.proj)
        self.assertTrue(paths["config"].is_file())
        self.assertTrue((paths["people"] / "_default.yaml").is_file())
        self.assertTrue(paths["state"].is_file())

    def test_self(self):
        report = self.apply(SELF)
        self.assert_base_files()
        self.assertTrue(card_path(self.proj, "me").is_file())
        cfg = self.cfg()
        self.assertEqual(cfg["mode"], "self")
        self.assertEqual(cfg["workspace"]["title"], "Pet")
        self.assertEqual(cfg["workspace"]["groups"], ["default"])
        self.assertEqual(cfg["models"]["collector"], {"model": "haiku", "effort": "low"})
        self.assertEqual(cfg["models"]["judge"], {"model": "opus", "effort": "high"})
        self.assertIs(cfg["hook"]["enabled"], True)
        self.assertEqual(report["root"], str(self.proj))
        self.assertEqual(report["hook_check"], i18n.t(("cli", "setup"), "hook_check", "ru"))
        lines = (self.proj / ".gitignore").read_text(encoding="utf-8").splitlines()
        for line in setup.GITIGNORE_LINES:
            self.assertEqual(lines.count(line), 1, line)

    def test_team(self):
        self.apply(TEAM)
        self.assert_base_files()
        self.assertTrue(card_path(self.proj, "alex").is_file())
        self.assertTrue(card_path(self.proj, "sam").is_file())
        self.assertFalse(card_path(self.proj, "me").exists())
        cfg = self.cfg()
        self.assertEqual(cfg["workspace"]["groups"], ["team-a", "team-b"])
        self.assertIn(ALEX_SRC, cfg["sources"])
        entries = json.loads((self.home / ".retropair" / "workspaces.json").read_text("utf-8"))
        entry = [e for e in entries if e["path"] == str(self.proj)]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["mode"], "team")
        self.assertEqual(entry[0]["groups"], ["team-a", "team-b"])
        self.assertEqual(entry[0]["id"], cfg["workspace"]["id"])

    def test_course(self):
        self.apply(COURSE)
        self.assert_base_files()
        cfg = self.cfg()
        self.assertEqual(cfg["workspace"]["groups"], ["default"])
        self.assertEqual(cfg["period"], {"mode": "labels"})
        self.assertIs(cfg["show_scores"], True)
        self.assertEqual(cfg["template"], "workspace")
        copied = ws_paths(self.proj)["templates"] / "feedback.md"
        self.assertEqual(copied.read_bytes(), (PLUGIN / "templates" / "feedback.ru.md").read_bytes())

    def test_template_workspace_keeps_existing_override(self):
        self.apply(TEAM)
        own = ws_paths(self.proj)["templates"] / "feedback.md"
        own.parent.mkdir(parents=True)
        own.write_text("свой шаблон\n", encoding="utf-8")
        self.apply({"template": "workspace"})
        self.assertEqual(own.read_text(encoding="utf-8"), "свой шаблон\n")

    def test_self_hook_check_printed_verbatim(self):
        res = self.sr("setup-apply", "--answers", str(self.answers_file(SELF)))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(i18n.t(("cli", "setup"), "hook_check", "ru"), res.stdout)

    def test_hook_false_no_check(self):
        report = self.apply({**SELF, "hook": False})
        self.assertIsNone(report["hook_check"])
        self.assertIs(self.cfg()["hook"]["enabled"], False)

    def test_answers_from_stdin_and_json(self):
        res = self.sr("setup-apply", "--answers", "-", "--json", stdin=json.dumps(TEAM))
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(set(data), {"root", "created", "changed", "unchanged", "advice", "hook_check"})
        self.assertEqual(data["root"], str(self.proj))

    def test_explicit_path(self):
        other = Path(self._tmp.name).resolve() / "other"
        other.mkdir()
        report = self.apply({**TEAM, "path": str(other)})
        self.assertEqual(report["root"], str(other))
        self.assertTrue(ws_paths(other)["config"].is_file())
        self.assertFalse(ws_paths(self.proj)["config"].exists())


class OwnerlessSources(_Proj):
    """team/course: источники шаблона без владельца ломают collect при двух карточках."""

    def test_team_without_sources_drops_template_sources(self):
        report = self.apply(TEAM)
        self.assertEqual(self.cfg()["sources"], [ALEX_SRC])
        self.assertTrue(any("person" in line or "people" in line for line in report["advice"]))

    def test_course_explicit_sources_kept(self):
        answers = {**COURSE, "sources": [{"type": "dir", "path": "/home/user/shared"}]}
        self.apply(answers)
        self.assertIn({"type": "dir", "path": "/home/user/shared"}, self.cfg()["sources"])

    def test_self_keeps_template_sources(self):
        self.apply(SELF)
        types = [src["type"] for src in self.cfg()["sources"]]
        self.assertEqual(types, ["specstory", "claude-jsonl"])

    def test_rerun_warns_on_ownerless_with_many_cards(self):
        self.apply({**TEAM, "sources": [{"type": "dir", "path": "/home/user/shared"}]})
        report = self.apply({"show_scores": True})
        self.assertTrue(any("/home/user/shared" in line for line in report["advice"]))


class ApplyRerun(_Proj):
    def test_language_change_preserves_history_cards_and_custom_template(self):
        self.apply({**COURSE, "lang": "ru"})
        paths = ws_paths(self.proj)
        history = paths["state_dir"] / "runs" / "process-reviews.json"
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_bytes((PLUGIN / "scripts/tests/fixtures/process-reviews.sample.json").read_bytes())
        custom = paths["templates"] / "feedback.md"
        custom.write_text("# Custom template\nKeep this user-owned text.\n", encoding="utf-8")
        preserved = [history, custom, card_path(self.proj, "kim"), paths["people"] / "_default.yaml"]
        before_bytes = {p: p.read_bytes() for p in preserved}
        before_cfg = self.cfg()

        report = self.apply({"lang": "en"})

        self.assertEqual(report["changed"], ["config.lang"])
        self.assertEqual(self.cfg(), {**before_cfg, "lang": "en"})
        for path, expected in before_bytes.items():
            self.assertEqual(path.read_bytes(), expected, str(path))
        self.assertEqual(self.apply({"lang": "en"})["changed"], [])

    def test_only_passed_key_changes(self):
        self.apply(TEAM)
        card = card_path(self.proj, "alex")
        before_cfg = self.cfg()
        before_card = (card.read_bytes(), card.stat().st_mtime_ns)
        # дефолт шаблона — true (вид scores), меняется на false (вид trend)
        report = self.apply({"show_scores": False, "people": TEAM["people"]})
        after_cfg = self.cfg()
        diff = [k for k in after_cfg if after_cfg[k] != before_cfg[k]]
        self.assertEqual(diff, ["show_scores"])
        self.assertEqual((card.read_bytes(), card.stat().st_mtime_ns), before_card)
        self.assertIn("alex", report["unchanged"])
        self.assertEqual(report["changed"], ["config.show_scores"])

    def test_gitignore_lines_cover_runtime_files(self):
        import hook_session_end
        from lib import marks

        for line in (f".retropair/{hook_session_end.LOG_NAME}",
                     f".retropair/{marks.COLLECT_LOCK}.lock"):
            self.assertIn(line, setup.GITIGNORE_LINES)

    def test_gitignore_not_duplicated(self):
        (self.proj / ".gitignore").write_text("node_modules/", encoding="utf-8")
        self.apply({**TEAM, "gitignore": True})
        self.apply({"gitignore": True})
        lines = (self.proj / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "node_modules/")
        for line in setup.GITIGNORE_LINES:
            self.assertEqual(lines.count(line), 1, line)

    def test_sources_replace_keeps_person_sources(self):
        self.apply(TEAM)
        self.apply({"sources": [{"type": "dir", "path": "/home/user/logs"}]})
        self.assertEqual(self.cfg()["sources"], [{"type": "dir", "path": "/home/user/logs"}, ALEX_SRC])

    def test_person_source_not_duplicated(self):
        self.apply(TEAM)
        self.apply(TEAM)
        self.assertEqual(self.cfg()["sources"].count(ALEX_SRC), 1)

    def test_new_group_appended(self):
        self.apply(TEAM)
        self.apply({"people": [{"id": "lee", "name": "Lee Demo", "group": "team-c"},
                               {"id": "max", "name": "Max Demo"}]})
        cfg = self.cfg()
        self.assertEqual(cfg["workspace"]["groups"], ["team-a", "team-b", "team-c"])
        entry = registry.find_by_path(self.proj)
        self.assertEqual(entry["groups"], ["team-a", "team-b", "team-c"])
        self.assertIn('group: "team-a"', card_path(self.proj, "max").read_text(encoding="utf-8"))

    def test_mode_change_is_advice(self):
        self.apply(TEAM)
        report = self.apply({"mode": "self"})
        self.assertEqual(self.cfg()["mode"], "self")
        self.assertIn("config.mode", report["changed"])
        self.assertTrue(any("team" in a and "self" in a for a in report["advice"]), report["advice"])
        self.assertEqual(registry.find_by_path(self.proj)["mode"], "self")

    def test_models_merge_by_role(self):
        self.apply(SELF)
        self.apply({"models": {"judge": {"model": "sonnet", "effort": "max"}}})
        models = self.cfg()["models"]
        self.assertEqual(models["collector"], {"model": "haiku", "effort": "low"})
        self.assertEqual(models["judge"], {"model": "sonnet", "effort": "max"})

    def test_rerun_mode_change_cli_rc0(self):
        self.assertEqual(self.sr("setup-apply", "--answers", "-", stdin=json.dumps(TEAM)).returncode, 0)
        res = self.sr("setup-apply", "--answers", "-", stdin=json.dumps({"mode": "self"}))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("совет:", res.stdout)


class ApplyErrors(_Proj):
    def assert_nothing_created(self) -> None:
        self.assertFalse((self.proj / ".retropair").exists())
        self.assertFalse((self.proj / ".gitignore").exists())
        self.assertFalse((self.home / ".retropair").exists())

    def assert_code(self, answers, code: int, needle: str) -> None:
        with self.assertRaises(SrError) as ctx:
            self.apply(answers)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        self.assertIn(needle, ctx.exception.message)

    def test_first_run_without_mode(self):
        self.assert_code({"title": "X", "gitignore": True}, 2, "mode")
        self.assert_nothing_created()

    def test_unknown_key(self):
        self.assert_code({**TEAM, "colour": "red"}, 2, "colour")
        self.assert_nothing_created()

    def test_bad_model(self):
        self.assert_code({**SELF, "models": {"judge": {"model": "gpt"}}}, 2, "models.judge.model")
        self.assert_nothing_created()

    def test_bad_person_id(self):
        self.assert_code({"mode": "team", "people": [{"id": "Bad Id", "name": "X"}]}, 2, "people[0].id")
        self.assert_nothing_created()

    def test_both_quotes_in_name(self):
        self.assert_code({"mode": "team", "people": [{"id": "x", "name": "a'b\"c"}]}, 2, "people[0].name")
        self.assert_nothing_created()

    def test_duplicate_person_ids(self):
        people = [{"id": "x", "name": "X"}, {"id": "x", "name": "Y"}]
        self.assert_code({"mode": "team", "people": people}, 2, "people[1].id")

    def test_bad_enums_and_types(self):
        for answers, needle in (
            ({"mode": "solo"}, "mode"),
            ({"mode": "team", "lang": "de"}, "lang"),
            ({"mode": "team", "hook": "yes"}, "hook"),
            ({"mode": "team", "period": {"mode": "weeks"}}, "period.mode"),
            ({"mode": "team", "sources": [{"type": "ftp"}]}, "sources[0].type"),
            ({"mode": "team", "sources": [{"type": "specstory"}]}, "sources[0].path"),
            ({"mode": "team", "sources": [{"type": "dir", "path": "/x", "person": "a"}]},
             "sources[0].person"),
            ({"mode": "team", "cleanup_period_days": 90}, "cleanup_period_days"),
            ({"mode": "team", "cleanup_period_days": {"value": 0, "write": False}},
             "cleanup_period_days.value"),
            ({"mode": "team", "template": "../x.md"}, "template"),
            ({"mode": "team", "title": ""}, "title"),
        ):
            with self.subTest(answers=answers):
                self.assert_code(answers, 2, needle)
        self.assert_nothing_created()

    def test_answers_not_object(self):
        self.assert_code([1, 2], 2, "объект")

    def test_missing_path_dir(self):
        self.assert_code({**TEAM, "path": "/home/user/no-such-dir-sr"}, 2, "path")

    def test_cli_missing_file(self):
        res = self.sr("setup-apply", "--answers", str(Path(self._tmp.name) / "nope.json"))
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("nope.json", res.stderr)

    def test_cli_broken_json(self):
        res = self.sr("setup-apply", "--answers", str(self.answers_file("{mode: team")))
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("answers.json", res.stderr)
        self.assertFalse((self.proj / ".retropair").exists())

    def test_cli_unknown_key(self):
        res = self.sr("setup-apply", "--answers", "-", stdin=json.dumps({**TEAM, "colour": 1}))
        self.assertEqual(res.returncode, 2)
        self.assertIn("colour", res.stderr)


class CleanupSettings(_Proj):
    def settings(self) -> Path:
        return setup.claude_settings_path(self.home)

    def test_settings_path(self):
        self.assertEqual(self.settings(), self.home / ".claude" / "settings.json")

    def test_write_false_only_advice(self):
        report = self.apply({**TEAM, "cleanup_period_days": {"value": 90, "write": False}})
        self.assertFalse(self.settings().exists())
        self.assertTrue(any("90" in a for a in report["advice"]), report["advice"])

    def test_write_true_merges(self):
        self.settings().parent.mkdir(parents=True)
        self.settings().write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        self.apply({**TEAM, "cleanup_period_days": {"value": 90, "write": True}})
        self.assertEqual(json.loads(self.settings().read_text("utf-8")),
                         {"theme": "dark", "cleanupPeriodDays": 90})

    def test_write_true_creates(self):
        report = self.apply({**TEAM, "cleanup_period_days": {"value": 90, "write": True}})
        self.assertEqual(json.loads(self.settings().read_text("utf-8")), {"cleanupPeriodDays": 90})
        self.assertIn("~/.claude/settings.json", report["created"])

    def test_broken_settings_code1_untouched(self):
        self.settings().parent.mkdir(parents=True)
        self.settings().write_text("{broken", encoding="utf-8")
        with self.assertRaises(SrError) as ctx:
            self.apply({**TEAM, "cleanup_period_days": {"value": 90, "write": True}})
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(self.settings().read_text("utf-8"), "{broken")
        self.assertFalse((self.proj / ".retropair").exists())


class StateRoundtrip(_Proj):
    def test_outside_workspace(self):
        jsonl = self.home / ".claude" / "projects" / munge(self.proj)
        jsonl.mkdir(parents=True)
        for name in ("a.jsonl", "b.jsonl", "c.txt"):
            (jsonl / name).write_text("{}\n", encoding="utf-8")
        (self.proj / ".specstory" / "history").mkdir(parents=True)
        st = setup.state(self.proj, self.home)
        self.assertIsNone(st["workspace"])
        found = st["found"]
        self.assertEqual(set(found), {"specstory_dir", "claude_projects_dir", "jsonl_files",
                                      "specstory_version", "cleanup_period_days"})
        self.assertIs(found["specstory_dir"], True)
        self.assertEqual(found["jsonl_files"], 2)
        self.assertEqual(found["claude_projects_dir"], str(jsonl))
        self.assertIsNone(found["cleanup_period_days"])

    def test_no_claude_dir(self):
        found = setup.state(self.proj, self.home)["found"]
        self.assertIsNone(found["claude_projects_dir"])
        self.assertEqual(found["jsonl_files"], 0)
        self.assertIs(found["specstory_dir"], False)

    def test_cleanup_value_read(self):
        path = setup.claude_settings_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"cleanupPeriodDays": 30}), encoding="utf-8")
        self.assertEqual(setup.state(self.proj, self.home)["found"]["cleanup_period_days"], 30)

    def test_roundtrip_team(self):
        self.apply({**TEAM, "gitignore": True, "hook": False})
        ws = setup.state(self.proj, self.home)["workspace"]
        self.assertEqual(ws["mode"], "team")
        self.assertEqual([p["id"] for p in ws["people"]], ["alex", "sam"])
        self.assertIs(ws["gitignore"], True)
        before = self.cfg()
        report = self.apply(ws)
        self.assertEqual(report["changed"], [])
        self.assertEqual(self.cfg(), before)
        self.assertEqual(report["created"], [])

    def test_roundtrip_self(self):
        self.apply(SELF)
        ws = setup.state(self.proj, self.home)["workspace"]
        self.assertEqual(self.apply(ws)["changed"], [])

    def test_cli_json(self):
        res = self.sr("setup-state", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertIsNone(data["workspace"])
        self.sr("setup-apply", "--answers", "-", stdin=json.dumps(TEAM))
        res = self.sr("setup-state", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["workspace"]["title"], "Team")
        res = self.sr("setup-apply", "--answers", "-", "--json", stdin=json.dumps(data["workspace"]))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["changed"], [])

    def test_cli_text(self):
        res = self.sr("setup-state")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("jsonl_files: 0", res.stdout)


class ReviewFixes(_Proj):
    """Правки по ревью кода: C1, I3–I6, M1, M2, M7."""

    def assert_code(self, answers, code: int, *needles: str) -> SrError:
        with self.assertRaises(SrError) as ctx:
            self.apply(answers)
        self.assertEqual(ctx.exception.code, code, ctx.exception.message)
        for needle in needles:
            self.assertIn(needle, ctx.exception.message)
        return ctx.exception

    def settings(self) -> Path:
        return setup.claude_settings_path(self.home)

    # C1 — cleanupPeriodDays не понижается
    def test_cleanup_never_lowered(self):
        self.settings().parent.mkdir(parents=True)
        self.settings().write_text(json.dumps({"cleanupPeriodDays": 365}), encoding="utf-8")
        before = self.settings().read_bytes()
        report = self.apply({**TEAM, "cleanup_period_days": {"value": 90, "write": True}})
        self.assertEqual(self.settings().read_bytes(), before)
        self.assertIn("~/.claude/settings.json", report["unchanged"])
        self.assertTrue(any("365" in a and "90" in a for a in report["advice"]), report["advice"])

    def test_cleanup_raised(self):
        self.settings().parent.mkdir(parents=True)
        self.settings().write_text(json.dumps({"cleanupPeriodDays": 30}), encoding="utf-8")
        self.apply({**TEAM, "cleanup_period_days": {"value": 90, "write": True}})
        self.assertEqual(json.loads(self.settings().read_text("utf-8"))["cleanupPeriodDays"], 90)

    # I4 — симлинк settings.json остаётся симлинком
    def test_settings_symlink_kept(self):
        dotfiles = Path(self._tmp.name) / "dotfiles"
        dotfiles.mkdir()
        real = dotfiles / "settings.json"
        real.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
        self.settings().parent.mkdir(parents=True)
        self.settings().symlink_to(real)
        self.apply({**TEAM, "cleanup_period_days": {"value": 90, "write": True}})
        self.assertTrue(self.settings().is_symlink())
        self.assertEqual(json.loads(real.read_text("utf-8")),
                         {"theme": "dark", "cleanupPeriodDays": 90})

    def test_gitignore_symlink_kept(self):
        real = Path(self._tmp.name) / "shared.gitignore"
        real.write_text("node_modules/\n", encoding="utf-8")
        (self.proj / ".gitignore").symlink_to(real)
        self.apply({**TEAM, "gitignore": True})
        self.assertTrue((self.proj / ".gitignore").is_symlink())
        self.assertIn(setup.GITIGNORE_LINES[0], real.read_text("utf-8"))

    # I3 — --workspace и path в разные папки
    def test_cli_workspace_conflicts_with_path(self):
        other = Path(self._tmp.name).resolve() / "other"
        other.mkdir()
        res = self.sr("--workspace", str(other), "setup-apply", "--answers", "-",
                      stdin=json.dumps({**TEAM, "path": str(self.proj)}))
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn(str(other), res.stderr)
        self.assertIn(str(self.proj), res.stderr)
        self.assertFalse((self.proj / ".retropair").exists())
        self.assertFalse((other / ".retropair").exists())

    def test_cli_workspace_same_as_path_ok(self):
        res = self.sr("--workspace", str(self.proj), "setup-apply", "--answers", "-",
                      stdin=json.dumps({**TEAM, "path": str(self.proj)}))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(ws_paths(self.proj)["config"].is_file())

    # I5 — ответ template действует на рендер
    def default_card(self) -> Path:
        return ws_paths(self.proj)["people"] / "_default.yaml"

    def test_template_workspace_sets_default_card(self):
        from lib import people, render_review
        self.apply({**SELF, "template": "workspace"})
        path = render_review.resolve_template(self.proj, people.load_person(self.proj, "me"),
                                              "ru", None)
        self.assertEqual(path, ws_paths(self.proj)["templates"] / "feedback.md")

    def test_template_only_line_changed(self):
        self.apply(SELF)
        before = self.default_card().read_text("utf-8").splitlines()
        report = self.apply({"template": "workspace"})
        after = self.default_card().read_text("utf-8").splitlines()
        diff = [(a, b) for a, b in zip(before, after) if a != b]
        self.assertEqual(len(before), len(after))
        self.assertEqual(len(diff), 1, diff)
        self.assertIn("template: workspace", diff[0][1])
        self.assertIn(".retropair/people/_default.yaml", report["changed"])
        report = self.apply({"template": "workspace"})
        self.assertNotIn(".retropair/people/_default.yaml", report["changed"])

    def test_template_file_must_exist(self):
        self.apply(SELF)
        err = self.assert_code({"template": "short.md"}, 2, "short.md")
        self.assertIn("templates", err.message)
        own = ws_paths(self.proj)["templates"] / "short.md"
        own.parent.mkdir(parents=True, exist_ok=True)
        own.write_text("свой\n", encoding="utf-8")
        self.apply({"template": "short.md"})
        self.assertIn("template: short.md", self.default_card().read_text("utf-8"))

    def test_template_line_missing_code1_nothing_written(self):
        self.apply(SELF)
        card = self.default_card()
        card.write_text("feedback:\n  address: ты\n", encoding="utf-8")
        cfg_before = ws_paths(self.proj)["config"].read_bytes()
        self.assert_code({"template": "workspace", "show_scores": True}, 1, "_default.yaml")
        self.assertEqual(ws_paths(self.proj)["config"].read_bytes(), cfg_before)
        self.assertFalse((ws_paths(self.proj)["templates"] / "feedback.md").exists())

    def test_template_own_card_template_is_advice(self):
        self.apply(TEAM)
        card = card_path(self.proj, "alex")
        card.write_text(card.read_text("utf-8") + "feedback:\n  template: default\n",
                        encoding="utf-8")
        before = card.read_bytes()
        report = self.apply({"template": "workspace"})
        self.assertEqual(card.read_bytes(), before)
        self.assertTrue(any("alex" in a and "sam" not in a for a in report["advice"]),
                        report["advice"])

    # I6 — даты окон периода
    def test_period_windows_checked(self):
        def period(*windows):
            return {"mode": "team", "period": {"mode": "dates", "windows": list(windows)}}
        good = {"label": "s1", "from": "2026-09-01", "to": "2026-09-14"}
        for answers, needle in (
            (period({"label": "s1", "from": "2026-99-01"}), "period.windows[0].from"),
            (period({"label": "s1", "to": "bad"}), "period.windows[0].to"),
            (period({"label": "s1", "from": "20260901"}), "period.windows[0].from"),
            (period(good, {"label": "s2", "from": "2026-09-20", "to": "2026-09-10"}),
             "period.windows[1]"),
            (period(good, {"label": "s1", "from": "2026-09-15"}), "period.windows[1].label"),
        ):
            with self.subTest(answers=answers):
                self.assert_code(answers, 2, needle)
        self.assertFalse((self.proj / ".retropair").exists())

    def test_period_windows_ok(self):
        windows = [{"label": "s1", "from": "2026-09-01", "to": "2026-09-14"},
                   {"label": "s2", "from": "2026-09-15T00:00:00", "to": None},
                   {"label": "s0", "to": "2026-08-31"}]
        self.apply({**TEAM, "period": {"mode": "dates", "windows": windows}})
        self.assertEqual(self.cfg()["period"]["windows"], windows)

    # M1 — в self только карточка me
    def test_self_rejects_other_people(self):
        self.assert_code({**SELF, "people": [{"id": "bob", "name": "Bob Demo"}]}, 2, "people")
        self.assertFalse((self.proj / ".retropair").exists())

    # M2 — неизвестные поля источника
    def test_unknown_source_key(self):
        self.assert_code({"mode": "team", "sources": [{"type": "dir", "path": "/x", "foo": 1}]},
                         2, "sources[0]", "foo")

    # M7 — не UTF-8
    def test_cli_answers_not_utf8(self):
        path = Path(self._tmp.name) / "answers.json"
        path.write_bytes(b'{"mode": "\xff"}')
        res = self.sr("setup-apply", "--answers", str(path))
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertNotIn("Traceback", res.stderr)
        self.assertIn("answers.json", res.stderr)

    def test_gitignore_not_utf8(self):
        (self.proj / ".gitignore").write_bytes(b"\xff\xfe\n")
        err = self.assert_code({**TEAM, "gitignore": True}, 1, ".gitignore")
        self.assertIn("UTF-8", err.message)


class ProjectRoot(unittest.TestCase):
    def test_not_git_is_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(setup.project_root(Path(tmp)), Path(tmp).resolve())

    def test_git_toplevel(self):
        import shutil
        import subprocess
        if shutil.which("git") is None:
            self.skipTest("нет git")
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp).resolve()
            subprocess.run(["git", "init", "-q", str(top)], check=True)
            sub = top / "a" / "b"
            sub.mkdir(parents=True)
            self.assertEqual(setup.project_root(sub), top)

    def test_answer_keys(self):
        self.assertEqual(setup.ANSWER_KEYS, frozenset({
            "mode", "title", "lang", "path", "people", "sources", "hook", "models", "show_scores",
            "period", "template", "cleanup_period_days", "gitignore"}))


if __name__ == "__main__":
    unittest.main()
