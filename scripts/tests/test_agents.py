"""Проектные агенты: `agents.render|target|status|write`, команды `agents write|check`,
запись агентов из `setup.apply` и пункты `doctor` из `cmd_setup_apply.doctor_lines`.

Проекты — временные папки без git, HOME подменён (`TempHome` / `run_sr(home=…)`).
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _util import PLUGIN, TempHome, copy_fixture, run_sr

import test_doctor
from lib import agents, cmd_setup_apply, i18n, setup
from lib.config import default_config, load_config, save_config
from lib.people import card_path
from lib.workspace import SrError

SELF = {"mode": "self", "title": "Pet", "lang": "ru", "hook": True, "gitignore": True,
        "models": {"collector": {"model": "haiku", "effort": "low"}}}
JUDGE_REL = ".claude/agents/sessionretro-log-judge.md"
GITIGNORE_NO_RU = 'нет — добавьте: sr.py setup-apply с {"gitignore": true}'
GITIGNORE_SKIPPED_RU = ('строки sessionretro в .gitignore не дописаны — добавьте: '
                        'sr.py setup-apply с {"gitignore": true}')


def split_frontmatter(text: str) -> tuple[list[str], str]:
    lines = text.splitlines(keepends=True)
    assert lines[0] == "---\n", lines[0]
    end = next(i for i in range(1, len(lines)) if lines[i] == "---\n")
    return [line.rstrip("\n") for line in lines[1:end]], "".join(lines[end + 1:])


def edit_body(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8") + "\nсвоя строка\n", encoding="utf-8")


class Render(unittest.TestCase):
    def test_roles(self):
        self.assertEqual(agents.ROLES, {"collector": "log-collector", "judge": "log-judge",
                                        "critic": "feedback-critic"})

    def test_judge_frontmatter_and_body(self):
        text = agents.render("judge", "opus", "high")
        self.assertNotIn("{{", text)
        fm, body = split_frontmatter(text)
        self.assertIn("name: sessionretro-log-judge", fm)
        self.assertIn("model: opus", fm)
        self.assertIn("effort: high", fm)
        _, plugin_body = split_frontmatter((PLUGIN / "agents" / "log-judge.md").read_text("utf-8"))
        self.assertEqual(body.encode("utf-8"), plugin_body.encode("utf-8"))

    def test_all_roles_render(self):
        for role, name in agents.ROLES.items():
            with self.subTest(role=role):
                fm, _ = split_frontmatter(agents.render(role, "sonnet", "medium"))
                self.assertIn(f"name: sessionretro-{name}", fm)
                self.assertIn("model: sonnet", fm)
                self.assertIn("effort: medium", fm)

    def test_unknown_role_code2(self):
        with self.assertRaises(SrError) as ctx:
            agents.render("writer", "opus", "high")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("writer", ctx.exception.message)

    def test_leftover_placeholder_code1(self):
        with tempfile.TemporaryDirectory() as tmp:
            stub = Path(tmp) / "log-judge.md"
            stub.write_text("---\nname: x\nmodel: {{model}}\neffort: {{effort}}\n---\n{{extra}}\n",
                            encoding="utf-8")
            with mock.patch.object(agents, "stub_path", return_value=stub):
                with self.assertRaises(SrError) as ctx:
                    agents.render("judge", "opus", "high")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("log-judge.md", ctx.exception.message)

    def test_target(self):
        root = Path("/home/user/proj")
        self.assertEqual(agents.target(root, "critic"),
                         root / ".claude" / "agents" / "sessionretro-feedback-critic.md")


class _Proj(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-agents-")
        self.proj = Path(self._tmp.name).resolve() / "proj"
        self.proj.mkdir()
        self.cfg = default_config(ws_id="demo", title="Demo", mode="self", lang="ru")

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()


class Write(_Proj):
    def test_empty_then_repeat(self):
        res = agents.write(self.proj, self.cfg)
        self.assertEqual(res, {"created": ["collector", "judge", "critic"], "updated": [],
                               "unchanged": [], "skipped_edited": []})
        for role in agents.ROLES:
            self.assertTrue(agents.target(self.proj, role).is_file(), role)
        res = agents.write(self.proj, self.cfg)
        self.assertEqual(res["unchanged"], ["collector", "judge", "critic"])
        self.assertEqual(res["created"] + res["updated"] + res["skipped_edited"], [])

    def test_model_change_updates_only_role(self):
        agents.write(self.proj, self.cfg)
        self.cfg["models"]["judge"] = {"model": "sonnet", "effort": "max"}
        res = agents.write(self.proj, self.cfg)
        self.assertEqual(res["updated"], ["judge"])
        self.assertEqual(res["unchanged"], ["collector", "critic"])
        fm, _ = split_frontmatter(agents.target(self.proj, "judge").read_text("utf-8"))
        self.assertIn("model: sonnet", fm)
        self.assertIn("effort: max", fm)

    def test_edited_body_skipped_then_force(self):
        agents.write(self.proj, self.cfg)
        path = agents.target(self.proj, "judge")
        edit_body(path)
        edited = path.read_text("utf-8")
        self.cfg["models"]["judge"]["model"] = "sonnet"
        res = agents.write(self.proj, self.cfg)
        self.assertEqual(res["skipped_edited"], ["judge"])
        self.assertEqual(path.read_text("utf-8"), edited)
        res = agents.write(self.proj, self.cfg, force=True)
        self.assertEqual(res["updated"], ["judge"])
        self.assertEqual(path.read_text("utf-8"), agents.render("judge", "sonnet", "high"))

    def test_hand_model_line_is_not_body_edit(self):
        agents.write(self.proj, self.cfg)
        path = agents.target(self.proj, "judge")
        path.write_text(path.read_text("utf-8").replace("model: opus", "model: haiku"), "utf-8")
        st = {s["role"]: s for s in agents.status(self.proj, self.cfg)}["judge"]
        self.assertFalse(st["body_edited"])
        self.assertFalse(st["model_ok"])
        self.assertTrue(st["effort_ok"])
        self.assertEqual(agents.write(self.proj, self.cfg)["updated"], ["judge"])

    def test_status(self):
        st = agents.status(self.proj, self.cfg)
        self.assertEqual([s["role"] for s in st], ["collector", "judge", "critic"])
        for item in st:
            self.assertEqual(set(item), {"role", "path", "exists", "model_ok", "effort_ok",
                                         "body_edited", "outdated"})
            self.assertFalse(item["exists"])
        agents.write(self.proj, self.cfg)
        for item in agents.status(self.proj, self.cfg):
            self.assertEqual((item["exists"], item["model_ok"], item["effort_ok"],
                              item["body_edited"]), (True, True, True, False), item["role"])
        edit_body(agents.target(self.proj, "critic"))
        st = {s["role"]: s for s in agents.status(self.proj, self.cfg)}
        self.assertTrue(st["critic"]["body_edited"])
        self.assertTrue(st["critic"]["model_ok"])


class PluginUpdate(_Proj):
    """I2: файл, который плагин записал и никто не правил, догоняет новую болванку."""

    def new_stub(self, role: str = "judge") -> Path:
        stub = Path(self._tmp.name) / f"{agents.ROLES[role]}.md"
        stub.write_text(agents.stub_path(role).read_text("utf-8") + "\nновая строка промпта\n",
                        encoding="utf-8")
        return stub

    def patched(self, stub: Path):
        real = agents.stub_path
        return mock.patch.object(agents, "stub_path",
                                 side_effect=lambda role: stub if role == "judge" else real(role))

    def test_record_written(self):
        agents.write(self.proj, self.cfg)
        data = json.loads(agents.record_path(self.proj).read_text("utf-8"))
        self.assertEqual(set(data), set(agents.ROLES))
        self.assertEqual(set(data["judge"]), {"sha256", "plugin_version"})
        self.assertEqual(agents.record_path(self.proj),
                         self.proj / ".sessionretro" / "agents.json")

    def test_untouched_file_updated_to_new_stub(self):
        agents.write(self.proj, self.cfg)
        stub = self.new_stub()
        with self.patched(stub):
            st = {s["role"]: s for s in agents.status(self.proj, self.cfg)}["judge"]
            self.assertTrue(st["outdated"])
            self.assertFalse(st["body_edited"])
            res = agents.write(self.proj, self.cfg)
            self.assertEqual(res["updated"], ["judge"])
            self.assertEqual(res["skipped_edited"], [])
            self.assertIn("новая строка промпта", agents.target(self.proj, "judge").read_text("utf-8"))
            st = {s["role"]: s for s in agents.status(self.proj, self.cfg)}["judge"]
            self.assertEqual((st["outdated"], st["body_edited"]), (False, False))

    def test_edited_file_not_updated(self):
        agents.write(self.proj, self.cfg)
        edit_body(agents.target(self.proj, "judge"))
        with self.patched(self.new_stub()):
            st = {s["role"]: s for s in agents.status(self.proj, self.cfg)}["judge"]
            self.assertEqual((st["outdated"], st["body_edited"]), (False, True))
            self.assertEqual(agents.write(self.proj, self.cfg)["skipped_edited"], ["judge"])

    def test_no_record_is_edited(self):
        agents.write(self.proj, self.cfg)
        agents.record_path(self.proj).unlink()
        with self.patched(self.new_stub()):
            st = {s["role"]: s for s in agents.status(self.proj, self.cfg)}["judge"]
            self.assertEqual((st["outdated"], st["body_edited"]), (False, True))
            self.assertEqual(agents.write(self.proj, self.cfg)["skipped_edited"], ["judge"])

    def test_model_line_change_keeps_outdated_detection(self):
        agents.write(self.proj, self.cfg)
        path = agents.target(self.proj, "judge")
        path.write_text(path.read_text("utf-8").replace("model: opus", "model: haiku"), "utf-8")
        with self.patched(self.new_stub()):
            self.assertEqual(agents.write(self.proj, self.cfg)["updated"], ["judge"])

    def test_setup_apply_reports_changed_without_advice(self):
        setup.apply(copy.deepcopy(SELF), self.proj, self.home)
        with self.patched(self.new_stub()):
            report = setup.apply({"models": {"collector": {"model": "haiku", "effort": "low"}}},
                                 self.proj, self.home)
        self.assertIn(JUDGE_REL, report["changed"])
        self.assertFalse([a for a in report["advice"] if JUDGE_REL in a], report["advice"])

    def test_check_shows_outdated_separately(self):
        from lib import cmd_agents
        agents.write(self.proj, self.cfg)
        with self.patched(self.new_stub()):
            item = {s["role"]: s for s in agents.status(self.proj, self.cfg)}["judge"]
            found = cmd_agents.problems(item, self.cfg)
            self.assertEqual(len(found), 1, found)
            self.assertIn("устарел", found[0])
            self.assertNotIn("вручную", found[0])

    def test_symlinked_agent_kept(self):
        agents.write(self.proj, self.cfg)
        path = agents.target(self.proj, "judge")
        real = Path(self._tmp.name) / "judge-real.md"
        real.write_text(path.read_text("utf-8"), encoding="utf-8")
        path.unlink()
        path.symlink_to(real)
        self.cfg["models"]["judge"]["model"] = "sonnet"
        agents.write(self.proj, self.cfg)
        self.assertTrue(path.is_symlink())
        self.assertIn("model: sonnet", real.read_text("utf-8"))


class Sync(_Proj):
    """A15: `agents.sync` после обновления плагина — обновляет только «устаревшие» файлы."""

    new_stub = PluginUpdate.new_stub
    patched = PluginUpdate.patched

    def test_outdated_updated_edited_kept(self):
        agents.write(self.proj, self.cfg)
        edit_body(agents.target(self.proj, "critic"))
        edited = agents.target(self.proj, "critic").read_text("utf-8")
        with self.patched(self.new_stub()):
            res = agents.sync(self.proj, self.cfg)
            self.assertEqual(res, {"updated": ["judge"], "unchanged": ["collector"],
                                   "edited": ["critic"], "missing": [], "record": True})
            self.assertIn("новая строка промпта",
                          agents.target(self.proj, "judge").read_text("utf-8"))
            st = {s["role"]: s for s in agents.status(self.proj, self.cfg)}
            self.assertEqual((st["judge"]["outdated"], st["judge"]["body_edited"]), (False, False))
            record = json.loads(agents.record_path(self.proj).read_text("utf-8"))
            self.assertEqual(record["judge"]["sha256"],
                             agents._sha(agents.target(self.proj, "judge").read_text("utf-8")))
        self.assertEqual(agents.target(self.proj, "critic").read_text("utf-8"), edited)

    def test_models_from_config(self):
        agents.write(self.proj, self.cfg)
        path = agents.target(self.proj, "judge")
        path.write_text(path.read_text("utf-8").replace("model: opus", "model: haiku"), "utf-8")
        with self.patched(self.new_stub()):
            self.assertEqual(agents.sync(self.proj, self.cfg)["updated"], ["judge"])
            self.assertEqual(path.read_text("utf-8"), agents.render("judge", "opus", "high"))

    def test_model_only_mismatch_not_touched(self):
        agents.write(self.proj, self.cfg)
        path = agents.target(self.proj, "judge")
        before = path.read_text("utf-8")
        self.cfg["models"]["judge"]["model"] = "sonnet"
        res = agents.sync(self.proj, self.cfg)
        self.assertEqual(res["updated"], [])
        self.assertEqual(path.read_text("utf-8"), before)

    def test_no_files_does_nothing(self):
        res = agents.sync(self.proj, self.cfg)
        self.assertEqual(res["missing"], ["collector", "judge", "critic"])
        self.assertEqual(res["updated"] + res["unchanged"] + res["edited"], [])
        self.assertFalse((self.proj / ".claude").exists())
        self.assertFalse(agents.record_path(self.proj).exists())

    def test_missing_role_not_created(self):
        agents.write(self.proj, self.cfg)
        agents.target(self.proj, "critic").unlink()
        res = agents.sync(self.proj, self.cfg)
        self.assertEqual(res["missing"], ["critic"])
        self.assertFalse(agents.target(self.proj, "critic").exists())

    def test_no_record_not_overwritten(self):
        agents.write(self.proj, self.cfg)
        agents.record_path(self.proj).unlink()
        before = agents.target(self.proj, "judge").read_text("utf-8")
        with self.patched(self.new_stub()):
            res = agents.sync(self.proj, self.cfg)
        self.assertEqual((res["edited"], res["updated"], res["record"]), (["judge"], [], False))
        self.assertEqual(agents.target(self.proj, "judge").read_text("utf-8"), before)
        self.assertFalse(agents.record_path(self.proj).exists())


class Cli(TempHome, unittest.TestCase):
    """Команды `agents write|check` в копии фикстуры `workspace-min`."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-agents-cli-")
        self.root = copy_fixture("workspace-min", Path(self._tmp.name).resolve())

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def sr(self, *args: str):
        return run_sr(*args, home=self.home, cwd=self.root)

    def test_write_json_then_check_ok(self):
        res = self.sr("agents", "write", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout)["created"], ["collector", "judge", "critic"])
        res = self.sr("agents", "check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        res = self.sr("agents", "check", "--json")
        self.assertEqual(res.returncode, 0)
        data = json.loads(res.stdout)
        self.assertEqual([s["role"] for s in data], ["collector", "judge", "critic"])

    def test_write_text(self):
        res = self.sr("agents", "write")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"создал: {JUDGE_REL}", res.stdout)
        res = self.sr("agents", "write")
        self.assertIn(f"без изменений: {JUDGE_REL}", res.stdout)

    def test_edited_code1_with_path_then_force(self):
        self.assertEqual(self.sr("agents", "write").returncode, 0)
        edit_body(self.root / JUDGE_REL)
        res = self.sr("agents", "write")
        self.assertEqual(res.returncode, 1)
        self.assertIn(JUDGE_REL, res.stderr)
        self.assertIn("--force", res.stderr)
        self.assertIn("своя строка", (self.root / JUDGE_REL).read_text("utf-8"))
        res = self.sr("agents", "write", "--force")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("своя строка", (self.root / JUDGE_REL).read_text("utf-8"))

    def test_check_model_mismatch(self):
        self.assertEqual(self.sr("agents", "write").returncode, 0)
        cfg = load_config(self.root)
        cfg["models"]["judge"]["model"] = "haiku"
        save_config(self.root, cfg)
        res = self.sr("agents", "check")
        self.assertEqual(res.returncode, 1)
        bad = [line for line in res.stdout.splitlines() if line.startswith("judge")]
        self.assertEqual(len(bad), 1, res.stdout)
        self.assertIn("haiku", bad[0])

    def test_check_missing_files(self):
        res = self.sr("agents", "check", "--json")
        self.assertEqual(res.returncode, 1)
        self.assertTrue(all(not s["exists"] for s in json.loads(res.stdout)))

    def test_outside_workspace_code2(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = run_sr("agents", "check", home=self.home, cwd=Path(tmp))
        self.assertEqual(res.returncode, 2)

    def old_plugin_judge(self) -> str:
        """Файл судьи как от прошлой версии плагина: тело старое, отпечаток в agents.json его."""
        path = self.root / JUDGE_REL
        old = path.read_text("utf-8") + "\nстарая строка прошлой версии\n"
        path.write_text(old, encoding="utf-8")
        rec = agents.record_path(self.root)
        data = json.loads(rec.read_text("utf-8"))
        data["judge"]["sha256"] = agents._sha(old)
        rec.write_text(json.dumps(data), encoding="utf-8")
        return old

    def test_sync_updates_outdated_then_check_ok(self):
        """A15: после обновления плагина `agents sync` обновляет непроправленный файл, код 0."""
        self.assertEqual(self.sr("agents", "write").returncode, 0)
        self.old_plugin_judge()
        res = self.sr("agents", "check")
        self.assertEqual(res.returncode, 1)
        self.assertIn("устарел", res.stdout)
        res = self.sr("agents", "sync")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"обновлён: judge ({JUDGE_REL})", res.stdout)
        self.assertIn("без изменений: collector", res.stdout)
        self.assertNotIn("старая строка", (self.root / JUDGE_REL).read_text("utf-8"))
        res = self.sr("agents", "check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_sync_edited_warns_code0(self):
        self.assertEqual(self.sr("agents", "write").returncode, 0)
        edit_body(self.root / JUDGE_REL)
        res = self.sr("agents", "sync")
        self.assertEqual(res.returncode, 0, res.stderr)
        line = next(ln for ln in res.stdout.splitlines() if ln.startswith("правлен вручную"))
        self.assertIn(f"judge ({JUDGE_REL})", line)
        self.assertIn("agents write --force", line)
        self.assertIn("своя строка", (self.root / JUDGE_REL).read_text("utf-8"))

    def test_sync_json(self):
        self.assertEqual(self.sr("agents", "write").returncode, 0)
        self.old_plugin_judge()
        res = self.sr("agents", "sync", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout),
                         {"updated": ["judge"], "unchanged": ["collector", "critic"],
                          "edited": [], "missing": [], "record": True})

    def test_sync_no_agents_code0(self):
        res = self.sr("agents", "sync")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), i18n.t(("cli", "setup"), "sync_none", "ru"))
        self.assertFalse((self.root / ".claude" / "agents").exists())

    def test_sync_no_record_warns(self):
        self.assertEqual(self.sr("agents", "write").returncode, 0)
        self.old_plugin_judge()
        agents.record_path(self.root).unlink()
        before = (self.root / JUDGE_REL).read_text("utf-8")
        res = self.sr("agents", "sync")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("agents.json", res.stdout)
        self.assertIn("agents write --force", res.stdout)
        self.assertEqual((self.root / JUDGE_REL).read_text("utf-8"), before)


class SetupWritesAgents(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-agents-setup-")
        self.proj = Path(self._tmp.name).resolve() / "proj"
        self.proj.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def apply(self, answers: dict) -> dict:
        return setup.apply(copy.deepcopy(answers), self.proj, self.home)

    def test_self_creates_agents_with_models(self):
        report = self.apply(SELF)
        fm, _ = split_frontmatter(agents.target(self.proj, "collector").read_text("utf-8"))
        self.assertIn("model: haiku", fm)
        self.assertIn("effort: low", fm)
        fm, _ = split_frontmatter(agents.target(self.proj, "judge").read_text("utf-8"))
        self.assertIn("model: opus", fm)
        self.assertIn(JUDGE_REL, report["created"])

    def test_rerun_without_models_leaves_agents(self):
        self.apply(SELF)
        report = self.apply({"show_scores": True})
        self.assertNotIn(JUDGE_REL, report["created"] + report["changed"] + report["unchanged"])

    def test_rerun_models_updates_role(self):
        self.apply(SELF)
        report = self.apply({"models": {"judge": {"model": "sonnet", "effort": "max"}}})
        self.assertIn(JUDGE_REL, report["changed"])
        self.assertIn(".claude/agents/sessionretro-log-collector.md", report["unchanged"])

    def test_missing_file_recreated_without_models(self):
        self.apply(SELF)
        (self.proj / JUDGE_REL).unlink()
        report = self.apply({"show_scores": True})
        self.assertIn(JUDGE_REL, report["created"])

    def test_edited_is_advice_not_error(self):
        self.apply(SELF)
        edit_body(self.proj / JUDGE_REL)
        report = self.apply({"models": {"judge": {"model": "sonnet"}}})
        advice = [a for a in report["advice"] if JUDGE_REL in a]
        self.assertEqual(len(advice), 1, report["advice"])
        self.assertIn("agents write --force", advice[0])
        self.assertIn("своя строка", (self.proj / JUDGE_REL).read_text("utf-8"))
        self.assertEqual(load_config(self.proj)["models"]["judge"]["model"], "sonnet")


class DoctorLines(TempHome, unittest.TestCase):
    KEYS = ("sources_found", "project_agents", "gitignore", "show_scores", "period_mode",
            "template")

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-agents-doctor-")
        self.proj = Path(self._tmp.name).resolve() / "proj"
        self.proj.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def lines(self) -> dict:
        return dict(cmd_setup_apply.doctor_lines(self.proj))

    def test_none_root(self):
        self.assertEqual(cmd_setup_apply.doctor_lines(None), [])

    def test_keys_and_values_after_apply(self):
        setup.apply(copy.deepcopy(SELF), self.proj, self.home)
        lines = cmd_setup_apply.doctor_lines(self.proj)
        self.assertEqual([k for k, _ in lines], list(self.KEYS))
        data = dict(lines)
        self.assertEqual(data["project_agents"], "3/3 совпадают с config.models")
        self.assertIs(data["gitignore"], True)
        # дефолт шаблона конфига: личный отчёт с баллами (вид scores)
        self.assertIs(data["show_scores"], True)
        self.assertEqual(data["period_mode"], "run")
        self.assertEqual(data["template"], "default")
        self.assertTrue(data["sources_found"].startswith("0/2"), data["sources_found"])
        (self.proj / ".specstory" / "history").mkdir(parents=True)
        self.assertTrue(self.lines()["sources_found"].startswith("1/2"))

    def test_project_agents_states(self):
        setup.apply({"mode": "team", "title": "T"}, self.proj, self.home)
        self.assertEqual(self.lines()["gitignore"], GITIGNORE_NO_RU)
        cfg = load_config(self.proj)
        cfg["models"]["judge"]["model"] = "haiku"
        save_config(self.proj, cfg)
        self.assertEqual(self.lines()["project_agents"], "расходятся: judge")
        edit_body(self.proj / ".claude/agents/sessionretro-feedback-critic.md")
        self.assertEqual(self.lines()["project_agents"], "расходятся: judge, critic")
        for role in agents.ROLES:
            agents.target(self.proj, role).unlink()
        self.assertEqual(self.lines()["project_agents"], "нет")

    def write_card(self, pid: str, sources: str) -> None:
        text = f'id: {pid}\nname: "Person {pid}"\ngroup: default\nsources:\n{sources}'
        card_path(self.proj, pid).write_text(text, encoding="utf-8")

    def test_sources_found_counts_card_sources(self):
        """m2: источники из `people/<id>.yaml` входят в `sources_found` наравне с конфигом."""
        setup.apply({"mode": "team", "title": "T"}, self.proj, self.home)
        self.assertEqual(load_config(self.proj)["sources"], [])
        self.assertEqual(self.lines()["sources_found"], "0/0 папок найдено")
        (self.proj / "logs" / "p06").mkdir(parents=True)
        self.write_card("p06", "  - type: dir\n    path: logs/p06\n")
        self.write_card("p07", "  - type: dir\n    path: logs/p07\n"
                               "  - type: git\n    url: https://example.com/p07.git\n")
        self.assertEqual(self.lines()["sources_found"],
                         "1/2 папок найдено; нет: logs/p07; не проверяются (git и прочие): 1")

    def test_sources_found_config_and_card_together(self):
        setup.apply(copy.deepcopy(SELF), self.proj, self.home)
        self.assertTrue(self.lines()["sources_found"].startswith("0/2"))
        (self.proj / "mine").mkdir()
        self.write_card("me", "  - type: dir\n    path: mine\n")
        self.assertTrue(self.lines()["sources_found"].startswith("1/3"),
                        self.lines()["sources_found"])

    def test_gitignore_missing_hint(self):
        """m3: неполный `.gitignore` — строка doctor с подсказкой, как исправить; полный — True."""
        setup.apply({"mode": "team", "title": "T"}, self.proj, self.home)
        self.assertEqual(self.lines()["gitignore"], GITIGNORE_NO_RU)
        self.assertIn('sr.py setup-apply', GITIGNORE_NO_RU)
        self.assertIn('{"gitignore": true}', GITIGNORE_NO_RU)
        setup.apply({"gitignore": True}, self.proj, self.home)
        self.assertIs(self.lines()["gitignore"], True)

    def test_gitignore_missing_hint_en(self):
        setup.apply({"mode": "team", "title": "T", "lang": "en"}, self.proj, self.home)
        self.assertEqual(self.lines()["gitignore"],
                         i18n.t(("cli", "setup"), "gitignore_no", "en"))
        self.assertIn('{"gitignore": true}', self.lines()["gitignore"])

    def test_setup_apply_advises_gitignore(self):
        """m3: новый workspace без ключа gitignore — совет в отчёте, запись не меняется."""
        report = setup.apply({"mode": "team", "title": "T"}, self.proj, self.home)
        self.assertIn(GITIGNORE_SKIPPED_RU, report["advice"])
        self.assertFalse((self.proj / ".gitignore").exists())

    def test_setup_apply_no_gitignore_advice_when_answered(self):
        for answers in ({"mode": "team", "title": "T", "gitignore": True},
                        {"mode": "team", "title": "T", "gitignore": False}):
            with self.subTest(gitignore=answers["gitignore"]):
                proj = Path(self._tmp.name).resolve() / f"p-{answers['gitignore']}"
                proj.mkdir()
                report = setup.apply(copy.deepcopy(answers), proj, self.home)
                self.assertNotIn(GITIGNORE_SKIPPED_RU, report["advice"])
        report = setup.apply({"mode": "team", "title": "T"}, self.proj, self.home)
        self.assertIn(GITIGNORE_SKIPPED_RU, report["advice"])
        rerun = setup.apply({"title": "T2"}, self.proj, self.home)
        self.assertNotIn(GITIGNORE_SKIPPED_RU, rerun["advice"])

    def test_setup_apply_cli_prints_gitignore_advice(self):
        path = Path(self._tmp.name) / "answers.json"
        path.write_text(json.dumps({"mode": "team", "title": "T"}), encoding="utf-8")
        res = run_sr("setup-apply", "--answers", str(path), home=self.home, cwd=self.proj)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(GITIGNORE_SKIPPED_RU, res.stdout)
        res = run_sr("doctor", home=self.home, cwd=self.proj)
        self.assertIn(f"gitignore: {GITIGNORE_NO_RU}", res.stdout)

    def test_doctor_cli_json(self):
        answers = json.dumps(SELF)
        path = Path(self._tmp.name) / "answers.json"
        path.write_text(answers, encoding="utf-8")
        res = run_sr("setup-apply", "--answers", str(path), home=self.home, cwd=self.proj)
        self.assertEqual(res.returncode, 0, res.stderr)
        res = run_sr("doctor", "--json", home=self.home, cwd=self.proj)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        data = json.loads(res.stdout)
        keys = list(data)
        self.assertEqual(keys[: len(test_doctor.BASE_KEYS)], list(test_doctor.BASE_KEYS))
        for key in self.KEYS:
            self.assertIn(key, data)
        self.assertEqual(data["project_agents"], "3/3 совпадают с config.models")

    def test_doctor_outside_workspace_no_setup_keys(self):
        res = run_sr("doctor", "--json", home=self.home, cwd=self.proj)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("project_agents", json.loads(res.stdout))


if __name__ == "__main__":
    unittest.main()
