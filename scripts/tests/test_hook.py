"""Хук SessionEnd: холостой путь молчит и быстр, включённый хук запускает фоновый collect."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from _util import FIXTURES, SCRIPTS, TempHome, copy_fixture

HOOK = SCRIPTS / "hook_session_end.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("hook_session_end_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(cwd: Path | str, session: str = "abc-123") -> str:
    return json.dumps({"session_id": session, "cwd": str(cwd), "hook_event_name": "SessionEnd",
                       "reason": "prompt_input_exit"})


class HookBase(TempHome, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="sr-hook-")
        self.tmp = Path(self._tmp.name).resolve()
        self.hook = load_hook()

    def tearDown(self) -> None:
        self._tmp.cleanup()
        super().tearDown()

    def workspace(self, enabled: bool = True, registered: bool = True) -> Path:
        root = copy_fixture("workspace-min", self.tmp)
        cfg_path = root / ".retropair" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["hook"]["enabled"] = enabled
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        if registered:
            self.register(root)
        return root

    def register(self, root: Path) -> None:
        """Запись в реестре `~/.retropair/workspaces.json` временного HOME (как после setup)."""
        path = self.home / ".retropair" / "workspaces.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        entries.append({"id": f"ws-{len(entries) + 1}", "path": str(root.resolve()), "title": "t",
                        "mode": "team", "groups": [], "last_run_at": None, "last_collect_at": None})
        path.write_text(json.dumps(entries), encoding="utf-8")

    def run_main(self, stdin: str, popen=None):
        """main() в процессе: (код, stdout, stderr, секунды, мок Popen)."""
        popen = popen or mock.MagicMock(name="Popen")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("subprocess.Popen", popen), mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            start = time.perf_counter()
            rc = self.hook.main()
            elapsed = time.perf_counter() - start
        return rc, out.getvalue(), err.getvalue(), elapsed, popen


class IdleTest(HookBase):
    def test_no_workspace_silent_fast(self):
        proj = self.tmp / "plain"
        proj.mkdir()
        rc, out, err, elapsed, popen = self.run_main(payload(proj))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertLess(elapsed, 0.05)
        popen.assert_not_called()

    def test_idle_does_not_import_lib(self):
        proj = self.tmp / "plain"
        proj.mkdir()
        before = {m for m in sys.modules if m == "lib" or m.startswith("lib.")}
        with mock.patch.dict(sys.modules):
            for name in before:
                sys.modules.pop(name)
            self.run_main(payload(proj))
            after = {m for m in sys.modules if m == "lib" or m.startswith("lib.")}
        self.assertEqual(after, set())

    def test_global_state_dir_is_not_workspace(self):
        glob = self.home / ".retropair"
        glob.mkdir()
        (glob / "config.json").write_text(json.dumps({"hook": {"enabled": True}}), encoding="utf-8")
        sub = self.home / "x"
        sub.mkdir()
        rc, out, _, _, popen = self.run_main(payload(sub))
        self.assertEqual((rc, out), (0, ""))
        popen.assert_not_called()

    def test_disabled_no_launch(self):
        root = self.workspace(enabled=False)
        rc, out, err, _, popen = self.run_main(payload(root))
        self.assertEqual((rc, out, err), (0, "", ""))
        popen.assert_not_called()
        self.assertFalse((root / ".retropair" / "collect.log").exists())


class RegistryTest(HookBase):
    """Хук доверяет только workspace из реестра пользователя: чужой клон с конфигом не запускает collect."""

    def test_unregistered_workspace_does_not_launch(self):
        root = self.workspace(registered=False)
        rc, out, err, _, popen = self.run_main(payload(root))
        self.assertEqual((rc, out), (0, ""))
        popen.assert_not_called()
        self.assertIn("workspace не зарегистрирован", err)
        self.assertIn("/retropair:setup", err)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertFalse((root / ".retropair" / "collect.log").exists())

    def test_other_path_in_registry_does_not_launch(self):
        root = self.workspace(registered=False)
        other = self.tmp / "other"
        other.mkdir()
        self.register(other)
        rc, _, err, _, popen = self.run_main(payload(root))
        self.assertEqual(rc, 0)
        popen.assert_not_called()
        self.assertIn("не зарегистрирован", err)

    def test_registered_workspace_launches(self):
        root = self.workspace(registered=False)
        self.register(root)
        _, _, err, _, popen = self.run_main(payload(root / "."))
        popen.assert_called_once()
        self.assertEqual(err, "")

    def test_broken_registry_reported_no_launch(self):
        root = self.workspace(registered=False)
        path = self.home / ".retropair" / "workspaces.json"
        path.parent.mkdir(parents=True)
        for text in ("{broken", json.dumps({"path": str(root)})):
            with self.subTest(text=text):
                path.write_text(text, encoding="utf-8")
                rc, out, err, _, popen = self.run_main(payload(root))
                self.assertEqual((rc, out), (0, ""))
                popen.assert_not_called()
                self.assertIn("workspaces.json", err)
                self.assertFalse((root / ".retropair" / "collect.log").exists())


class LaunchTest(HookBase):
    def test_enabled_launches_collect_once(self):
        root = self.workspace()
        rc, out, err, elapsed, popen = self.run_main(payload(root))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertLess(elapsed, 0.2)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        cmd = args[0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(Path(cmd[1]), SCRIPTS / "sr.py")
        self.assertEqual(cmd[2:], ["collect", "--quiet", "--workspace", str(root)])
        self.assertIs(kwargs["start_new_session"], True)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        log = root / ".retropair" / "collect.log"
        self.assertEqual(Path(kwargs["stdout"].name), log)
        text = log.read_text(encoding="utf-8")
        self.assertIn("hook session=abc-123", text)
        self.assertRegex(text.splitlines()[-1], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z hook session=")

    def test_finds_root_from_subdir(self):
        root = self.workspace()
        sub = root / "src" / "deep"
        sub.mkdir(parents=True)
        _, _, _, _, popen = self.run_main(payload(sub))
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0][-1], str(root))

    def test_broken_stdin_uses_getcwd(self):
        root = self.workspace()
        with mock.patch("os.getcwd", return_value=str(root)):
            rc, out, _, _, popen = self.run_main("{not json")
        self.assertEqual((rc, out), (0, ""))
        popen.assert_called_once()

    def test_empty_stdin_uses_getcwd(self):
        proj = self.tmp / "plain"
        proj.mkdir()
        with mock.patch("os.getcwd", return_value=str(proj)):
            rc, out, err, _, popen = self.run_main("")
        self.assertEqual((rc, out, err), (0, "", ""))
        popen.assert_not_called()

    def test_broken_config_logged_exit0(self):
        root = self.workspace()
        (root / ".retropair" / "config.json").write_text("{broken", encoding="utf-8")
        rc, out, _, _, popen = self.run_main(payload(root))
        self.assertEqual((rc, out), (0, ""))
        popen.assert_not_called()
        text = (root / ".retropair" / "collect.log").read_text(encoding="utf-8")
        self.assertIn("config.json", text)
        self.assertIn("session=abc-123", text)

    def test_popen_failure_logged_exit0(self):
        root = self.workspace()
        boom = mock.MagicMock(side_effect=OSError("нет прав"))
        rc, out, _, _, _ = self.run_main(payload(root), popen=boom)
        self.assertEqual((rc, out), (0, ""))
        text = (root / ".retropair" / "collect.log").read_text(encoding="utf-8")
        self.assertIn("нет прав", text)

    def test_big_log_trimmed(self):
        root = self.workspace()
        log = root / ".retropair" / "collect.log"
        log.write_bytes(b"a" * (1024 * 1024) + b"TAIL-MARK\n")
        self.run_main(payload(root))
        data = log.read_bytes()
        self.assertLess(len(data), 200 * 1024 + 1024)
        self.assertGreaterEqual(len(data), 200 * 1024)
        self.assertIn(b"TAIL-MARK", data)
        self.assertIn(b"hook session=abc-123", data)

    def test_small_log_kept(self):
        root = self.workspace()
        log = root / ".retropair" / "collect.log"
        log.write_text("старое\n", encoding="utf-8")
        self.run_main(payload(root))
        self.assertTrue(log.read_text(encoding="utf-8").startswith("старое\n"))


class ProcessTest(HookBase):
    def run_hook(self, stdin: str, cwd: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(HOOK)], input=stdin, text=True,
                              capture_output=True, env=env, cwd=str(cwd), timeout=10)

    def test_script_silent_exit0(self):
        proc = self.run_hook(payload("/home/user/project"), self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((proc.stdout, proc.stderr), ("", ""))

    def test_script_empty_stdin(self):
        proc = self.run_hook("", self.home)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_script_writes_nothing_to_home(self):
        self.run_hook("{}", self.home)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_script_real_background_collect(self):
        """Настоящий запуск: хук возвращается сразу, фоновый collect пишет индекс и снапшот."""
        root = self.workspace()
        src = self.tmp / "src"
        src.mkdir()
        shutil.copy(FIXTURES / "logs" / "specstory-claude.md", src / "s.md")
        cfg_path = root / ".retropair" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["sources"] = [{"type": "dir", "path": str(src), "person": "alex"}]
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        start = time.perf_counter()
        proc = self.run_hook(payload(root), self.tmp)
        spent = time.perf_counter() - start
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((proc.stdout, proc.stderr), ("", ""))
        self.assertLess(spent, 5)
        index = root / ".retropair" / "logs-index.json"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not index.is_file():
            time.sleep(0.1)
        self.assertTrue(index.is_file(), (root / ".retropair" / "collect.log").read_text())
        time.sleep(0.5)                    # дать фоновому процессу выйти до чистки tmp
        data = json.loads(index.read_text(encoding="utf-8"))
        self.assertEqual(len(data["logs"]), 1)
        self.assertEqual(data["logs"][0]["person_id"], "alex")


if __name__ == "__main__":
    unittest.main()
