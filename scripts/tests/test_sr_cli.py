"""CLI sr.py: диспетчер, --version, --workspace в любой позиции, init, people."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _util
from _util import run_sr

from lib import cmd_init, registry, yamlmini
from lib.schema import validate
from lib.version import __version__


class SrCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-cli-")
        base = Path(self._tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.proj = base / "proj"
        self.proj.mkdir()
        self.base = base

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def sr(self, *args: str, cwd: Path | None = None):
        return run_sr(*args, home=self.home, cwd=cwd or self.proj)

    def registry(self) -> list[dict]:
        return json.loads((self.home / ".retropair" / "workspaces.json").read_text(encoding="utf-8"))

    # --- диспетчер ---

    def test_no_command_exit2(self):
        res = self.sr()
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage", res.stderr.lower())

    def test_version(self):
        res = self.sr("--version")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"retropair {__version__}", res.stdout)

    def test_unknown_command_exit2(self):
        self.assertEqual(self.sr("nope").returncode, 2)

    # --- init ---

    def test_init_self_creates_files(self):
        res = self.sr("init", "--mode", "self", "--title", "T")
        self.assertEqual(res.returncode, 0, res.stderr)
        state = self.proj / ".retropair"
        for rel in ("config.json", "people/_default.yaml", "people/me.yaml", "plugin-state.json"):
            self.assertTrue((state / rel).is_file(), rel)
        cfg = json.loads((state / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(validate(cfg, "config"), [])
        self.assertEqual(cfg["mode"], "self")
        self.assertEqual(cfg["workspace"]["title"], "T")
        self.assertEqual(cfg["workspace"]["id"], "t")
        self.assertIn("t", res.stdout)
        self.assertIn(str(state), res.stdout)
        entries = self.registry()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["path"], str(self.proj))
        pstate = json.loads((state / "plugin-state.json").read_text(encoding="utf-8"))
        self.assertEqual(pstate, {
            "formatVersion": 1, "pluginVersion": __version__, "storeKind": "workspace",
            "migrations": [], "lastSync": None,
        })
        default_src = (_util.PLUGIN / "templates" / "_default.yaml").read_text(encoding="utf-8")
        self.assertEqual((state / "people" / "_default.yaml").read_text(encoding="utf-8"), default_src)

    def test_init_me_card_valid_ru(self):
        self.assertEqual(self.sr("init", "--mode", "self").returncode, 0)
        people = self.proj / ".retropair" / "people"
        text = (people / "me.yaml").read_text(encoding="utf-8")
        self.assertNotIn("{{", text)
        self.assertIn('name: "Я"', text)
        card = yamlmini.load(text)
        self.assertEqual(card["id"], "me")
        self.assertEqual(card["name"], "Я")
        self.assertEqual(card["group"], "default")
        # эффективная карточка: feedback из _default, None-списки → []
        default = yamlmini.load((people / "_default.yaml").read_text(encoding="utf-8"))
        feedback = dict(default["feedback"])
        for key in ("skip_basics", "boundaries", "avoid"):
            feedback[key] = feedback[key] or []
        card["feedback"] = feedback
        card["sources"] = card["sources"] or []
        self.assertEqual(validate(card, "person"), [])

    def test_init_me_card_en(self):
        self.assertEqual(self.sr("init", "--mode", "self", "--lang", "en").returncode, 0)
        text = (self.proj / ".retropair" / "people" / "me.yaml").read_text(encoding="utf-8")
        self.assertIn('name: "Me"', text)
        cfg = json.loads((self.proj / ".retropair" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["lang"], "en")

    def test_init_default_title_is_folder_name(self):
        self.assertEqual(self.sr("init", "--mode", "team").returncode, 0)
        cfg = json.loads((self.proj / ".retropair" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["workspace"]["title"], "proj")
        self.assertEqual(cfg["workspace"]["id"], "proj")

    def test_init_path_flag(self):
        other = self.base / "other"
        other.mkdir()
        res = self.sr("init", "--mode", "course", "--path", str(other))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((other / ".retropair" / "config.json").is_file())
        self.assertFalse((self.proj / ".retropair").exists())

    def test_init_missing_path_exit2(self):
        res = self.sr("init", "--mode", "team", "--path", str(self.base / "missing"))
        self.assertEqual(res.returncode, 2)
        self.assertIn("retropair:", res.stderr)

    def test_init_twice_exit1(self):
        self.assertEqual(self.sr("init", "--mode", "self").returncode, 0)
        res = self.sr("init", "--mode", "self")
        self.assertEqual(res.returncode, 1)
        self.assertIn("workspace уже создан", res.stderr)
        self.assertTrue(res.stderr.startswith("retropair: "))

    def test_init_bogus_mode_exit2(self):
        self.assertEqual(self.sr("init", "--mode", "bogus").returncode, 2)
        self.assertFalse((self.proj / ".retropair").exists())

    def test_init_team_no_me_card(self):
        self.assertEqual(self.sr("init", "--mode", "team").returncode, 0)
        people = self.proj / ".retropair" / "people"
        self.assertTrue((people / "_default.yaml").is_file())
        self.assertFalse((people / "me.yaml").exists())

    def test_init_id_collision_app_2(self):
        second = self.base / "second"
        second.mkdir()
        self.assertEqual(self.sr("init", "--mode", "team", "--title", "App").returncode, 0)
        res = self.sr("init", "--mode", "team", "--title", "App", cwd=second)
        self.assertEqual(res.returncode, 0, res.stderr)
        cfg = json.loads((second / ".retropair" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["workspace"]["id"], "app-2")
        ids = {e["path"]: e["id"] for e in self.registry()}
        self.assertEqual(ids[str(second)], "app-2")
        self.assertEqual(ids[str(self.proj)], "app")

    # --- --workspace в любой позиции ---

    def test_workspace_flag_any_position(self):
        self.assertEqual(self.sr("init", "--mode", "team").returncode, 0)
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        before = self.sr("--workspace", str(self.proj), "doctor", cwd=elsewhere)
        after = self.sr("doctor", "--workspace", str(self.proj), cwd=elsewhere)
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertEqual(after.returncode, 0, after.stderr)
        line = f"workspace: {self.proj}"
        self.assertIn(line, before.stdout.splitlines())
        self.assertIn(line, after.stdout.splitlines())

    def test_workspace_abbreviation_rejected_exit2(self):
        # `--works X` не должен молча пропадать и запускать команду на workspace из cwd
        self.assertEqual(self.sr("init", "--mode", "team").returncode, 0)
        other = self.base / "other"
        other.mkdir()
        self.assertEqual(self.sr("init", "--mode", "self", cwd=other).returncode, 0)
        for argv in (("--works", str(self.proj), "people", "list"),
                     ("people", "list", "--works", str(self.proj))):
            with self.subTest(argv=argv):
                res = self.sr(*argv, cwd=other)
                self.assertEqual(res.returncode, 2, res.stdout + res.stderr)
                self.assertNotIn("me ·", res.stdout)

    # --- init в домашней папке ---

    def test_init_in_home_exit2_nothing_written(self):
        res = self.sr("init", "--mode", "team", "--path", str(self.home))
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("retropair:", res.stderr)
        self.assertFalse((self.home / ".retropair").exists())

    def test_init_in_home_with_existing_global_dir_exit2(self):
        glob = self.home / ".retropair"
        glob.mkdir()
        res = self.sr("init", "--mode", "team", cwd=self.home)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertEqual(list(glob.iterdir()), [])


class InitOrderTest(_util.TempHome, unittest.TestCase):
    """`init` в процессе: реестр пишется последним, упавшая запись его не трогает."""

    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-init-")
        self.proj = Path(self._tmp.name).resolve() / "proj"
        self.proj.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def args(self, **kw):
        base = {"mode": "self", "title": "App", "lang": "ru", "path": str(self.proj),
                "workspace": None}
        base.update(kw)
        return argparse.Namespace(**base)

    def test_failed_write_leaves_no_registry_entry(self):
        with mock.patch("lib.cmd_init.write_json_atomic", side_effect=OSError("диск полон")):
            with self.assertRaises(OSError):
                cmd_init.handle(self.args())
        self.assertEqual(registry.load(), [])
        self.assertFalse((self.home / ".retropair" / "workspaces.json").exists())

    def test_failed_registration_after_files(self):
        with mock.patch("lib.cmd_init.registry.register", side_effect=OSError("нет места")):
            with self.assertRaises(OSError):
                cmd_init.handle(self.args())
        self.assertTrue((self.proj / ".retropair" / "plugin-state.json").is_file())
        self.assertEqual(registry.load(), [])

    def test_id_matches_registry_on_collision(self):
        registry.register(self.home / "elsewhere", "App", "team", ["default"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_init.handle(self.args()), 0)
        cfg = json.loads((self.proj / ".retropair" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["workspace"]["id"], "app-2")
        self.assertEqual(registry.find_by_path(self.proj)["id"], "app-2")

    def test_registry_id_changed_between_preview_and_register(self):
        # гонка: кто-то занял id между предпросмотром и регистрацией — config переписывается
        real = registry.register

        def racing(root, title, mode, groups):
            real(self.home / "racer", title, mode, groups)
            return real(root, title, mode, groups)

        with mock.patch("lib.cmd_init.registry.register", side_effect=racing):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cmd_init.handle(self.args()), 0)
        cfg = json.loads((self.proj / ".retropair" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["workspace"]["id"], registry.find_by_path(self.proj)["id"])
        self.assertEqual(cfg["workspace"]["id"], "app-2")


class PeopleCliTest(unittest.TestCase):
    """`people {add,show,list,context}` на свежем workspace режима self."""

    setUp_base = SrCliTest.setUp
    tearDown = SrCliTest.tearDown
    sr = SrCliTest.sr

    def setUp(self) -> None:
        self.setUp_base()
        res = self.sr("init", "--mode", "self")
        self.assertEqual(res.returncode, 0, res.stderr)

    def add_alex(self):
        res = self.sr("people", "add", "alex", "--name", "Alex", "--group", "team-a")
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def test_add_reports_new_group(self):
        res = self.add_alex()
        self.assertIn("alex.yaml", res.stdout)
        self.assertIn("team-a", res.stdout)
        cfg = json.loads((self.proj / ".retropair" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["workspace"]["groups"], ["default", "team-a"])

    def test_add_defaults_name_and_group(self):
        self.assertEqual(self.sr("people", "add", "kim").returncode, 0)
        data = json.loads(self.sr("people", "show", "kim", "--json").stdout)
        self.assertEqual(data["name"], "kim")
        self.assertEqual(data["group"], "default")

    def test_show_text(self):
        self.add_alex()
        res = self.sr("people", "show", "alex")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Alex", res.stdout)
        self.assertIn("# feedback:", res.stdout)  # текст файла как есть

    def test_show_workspace_flag_from_other_cwd(self):
        self.add_alex()
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        res = self.sr("people", "show", "alex", "--workspace", str(self.proj), cwd=elsewhere)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Alex", res.stdout)

    def test_show_json_effective(self):
        self.add_alex()
        res = self.sr("people", "show", "alex", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["feedback"]["address"], "ты")
        self.assertEqual(validate(data, "person"), [])

    def test_list_text_and_json(self):
        self.add_alex()
        res = self.sr("people", "list")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("me", res.stdout)
        self.assertIn("alex", res.stdout)
        data = json.loads(self.sr("people", "list", "--json").stdout)
        self.assertEqual([c["id"] for c in data], ["alex", "me"])

    def test_context_lines(self):
        self.add_alex()
        res = self.sr("people", "context", "alex")
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = [line for line in res.stdout.splitlines() if line.strip()]
        self.assertTrue(5 <= len(lines) <= 8, lines)
        data = json.loads(self.sr("people", "context", "alex", "--json").stdout)
        self.assertEqual(data, lines)

    def test_show_unknown_exit2(self):
        res = self.sr("people", "show", "nobody")
        self.assertEqual(res.returncode, 2)
        self.assertTrue(res.stderr.startswith("retropair: "), res.stderr)

    def test_context_unknown_exit2(self):
        self.assertEqual(self.sr("people", "context", "nobody").returncode, 2)

    def test_add_without_id_exit2(self):
        self.assertEqual(self.sr("people", "add").returncode, 2)

    def test_people_without_subcommand_exit2(self):
        self.assertEqual(self.sr("people").returncode, 2)

    def test_add_duplicate_exit1(self):
        self.add_alex()
        self.assertEqual(self.sr("people", "add", "alex").returncode, 1)

    def test_add_bad_id_exit2(self):
        self.assertEqual(self.sr("people", "add", "Bad_ID").returncode, 2)

    def test_add_yaml_scalar_like_ids(self):
        # id, похожие на число или ключевое слово YAML, остаются строками
        for pid in ("2024", "007", "true", "nan", "1e5"):
            with self.subTest(pid=pid):
                res = self.sr("people", "add", pid)
                self.assertEqual(res.returncode, 0, res.stderr)
                shown = self.sr("people", "show", pid, "--json")
                self.assertEqual(shown.returncode, 0, shown.stderr)
                self.assertEqual(json.loads(shown.stdout)["id"], pid)
        self.assertEqual(self.sr("people", "list").returncode, 0)

    def test_add_id_with_trailing_newline_exit2(self):
        res = self.sr("people", "add", "bob\n", "--name", "Bob")
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertEqual(self.sr("people", "list").returncode, 0)

    def test_people_outside_workspace_exit2(self):
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.assertEqual(self.sr("people", "list", cwd=elsewhere).returncode, 2)


class FixtureCliTest(unittest.TestCase):
    """Реальный артефакт: команды people на копии фикстуры workspace-min."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-fixture-")
        base = Path(self._tmp.name).resolve()
        self.home = base / "home"
        self.home.mkdir()
        self.ws = _util.copy_fixture("workspace-min", base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_list_and_context(self):
        res = run_sr("people", "list", "--json", "--workspace", str(self.ws), home=self.home, cwd=self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([c["id"] for c in json.loads(res.stdout)], ["alex", "sam"])
        for pid in ("alex", "sam"):
            res = run_sr("people", "context", pid, "--workspace", str(self.ws), home=self.home, cwd=self.home)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue(5 <= len(res.stdout.splitlines()) <= 8, res.stdout)

    def test_doctor_counts_people(self):
        res = run_sr("doctor", "--json", "--workspace", str(self.ws), home=self.home, cwd=self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["people"], 2)
        self.assertEqual(data["mode"], "team")


if __name__ == "__main__":
    unittest.main()
