"""Команда doctor: вывод вне и внутри workspace, settings.json, строки doctor_lines."""

from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path

import _util
from _util import run_sr

from lib import cmd_doctor, registry_cmds
from lib.version import __version__

BASE_KEYS = (
    "workspace", "mode", "people", "python", "specstory", "hook_enabled",
    "cleanup_period_days", "registry_count", "last_run_at", "last_collect_at",
    "plugin_version",
)


class DoctorCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-doctor-")
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

    def settings(self, text: str) -> None:
        folder = self.home / ".claude"
        folder.mkdir(exist_ok=True)
        (folder / "settings.json").write_text(text, encoding="utf-8")

    def test_outside_workspace_exit0_not_found(self):
        res = self.sr("doctor")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("workspace: не найден", res.stdout)
        self.assertIn(f"plugin_version: {__version__}", res.stdout)

    def test_json_in_workspace_has_all_keys(self):
        self.assertEqual(self.sr("init", "--mode", "self").returncode, 0)
        self.settings(json.dumps({"cleanupPeriodDays": 45}))
        res = self.sr("doctor", "--json")
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        for key in BASE_KEYS:
            self.assertIn(key, data)
        self.assertEqual(data["workspace"], str(self.proj))
        self.assertEqual(data["mode"], "self")
        self.assertEqual(data["people"], 1)
        self.assertIs(data["hook_enabled"], True)
        self.assertEqual(data["cleanup_period_days"], 45)
        self.assertEqual(data["registry_count"], 1)
        self.assertIsNone(data["last_run_at"])
        self.assertIsNone(data["last_collect_at"])
        self.assertEqual(data["plugin_version"], __version__)

    def test_no_settings_not_set(self):
        self.assertEqual(self.sr("init", "--mode", "team").returncode, 0)
        data = json.loads(self.sr("doctor", "--json").stdout)
        self.assertEqual(data["cleanup_period_days"], "не задан")

    def test_settings_without_key_not_set(self):
        self.settings(json.dumps({"model": "opus"}))
        data = json.loads(self.sr("doctor", "--json").stdout)
        self.assertEqual(data["cleanup_period_days"], "не задан")

    def test_broken_settings_exit1(self):
        self.settings("{not json")
        res = self.sr("doctor")
        self.assertEqual(res.returncode, 1)
        self.assertIn("settings.json", res.stderr)

    def test_workspace_not_in_registry(self):
        self.assertEqual(self.sr("init", "--mode", "team").returncode, 0)
        (self.home / ".sessionretro" / "workspaces.json").write_text("[]\n", encoding="utf-8")
        data = json.loads(self.sr("doctor", "--json").stdout)
        self.assertEqual(data["last_run_at"], "не в реестре")
        self.assertEqual(data["last_collect_at"], "не в реестре")
        self.assertEqual(data["registry_count"], 0)


PROBE_OK = """
def register(subparsers):
    p = subparsers.add_parser("zz-probe")
    p.set_defaults(func=lambda args: 0)


def doctor_lines(root):
    return [("zz_probe", "ok"), ("zz_root", str(root))]
"""

PROBE_FAIL = """
def register(subparsers):
    pass


def doctor_lines(root):
    raise ValueError("сломано")
"""

PROBE_CLASH = """
def register(subparsers):
    pass


def doctor_lines(root):
    return [("python", "чужое значение")]
"""


class DoctorLinesTest(_util.TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._lib = tempfile.TemporaryDirectory(prefix="sr-lib-")
        self.lib = Path(self._lib.name)

    def tearDown(self) -> None:
        self._lib.cleanup()
        super().tearDown()

    def probe(self, body: str) -> list:
        (self.lib / "cmd_zz_probe.py").write_text(textwrap.dedent(body), encoding="utf-8")
        return registry_cmds.discover(self.lib)

    def test_probe_lines_added(self):
        lines, rc = cmd_doctor.collect_doctor_lines(None, self.probe(PROBE_OK))
        self.assertEqual(rc, 0)
        data = dict(lines)
        self.assertEqual(data["zz_probe"], "ok")
        self.assertEqual(data["zz_root"], "None")
        self.assertEqual([k for k, _ in lines][: len(BASE_KEYS)], list(BASE_KEYS))

    def test_probe_exception_line_and_rc1(self):
        lines, rc = cmd_doctor.collect_doctor_lines(None, self.probe(PROBE_FAIL))
        self.assertEqual(rc, 1)
        data = dict(lines)
        self.assertIn("cmd_zz_probe.py", data)
        self.assertIn("ошибка", data["cmd_zz_probe.py"])
        self.assertIn("сломано", data["cmd_zz_probe.py"])

    def test_probe_key_clash_rc1(self):
        lines, rc = cmd_doctor.collect_doctor_lines(None, self.probe(PROBE_CLASH))
        self.assertEqual(rc, 1)
        data = dict(lines)
        self.assertNotEqual(data["python"], "чужое значение")
        self.assertIn("python", data["cmd_zz_probe.py"])

    def test_specstory_version_line_skips_banner(self):
        out = "╭────╮\n│ Welcome │\n╰────╯\n2.6.0 (SpecStory)\n"
        self.assertEqual(cmd_doctor.specstory_version_line(out), "2.6.0 (SpecStory)")
        self.assertEqual(cmd_doctor.specstory_version_line("2.7.1\n"), "2.7.1")

    def test_specstory_version_line_no_version(self):
        self.assertEqual(cmd_doctor.specstory_version_line("\n  \n"), "пустой ответ")
        self.assertEqual(
            cmd_doctor.specstory_version_line("hello\nworld"), "версия не распознана: hello"
        )

    def test_format_text(self):
        text = cmd_doctor.format_text([("a", None), ("b", True), ("c", 3)])
        self.assertEqual(text.splitlines(), ["a: нет", "b: да", "c: 3"])


if __name__ == "__main__":
    unittest.main()
