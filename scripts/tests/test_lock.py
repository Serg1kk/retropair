"""lib.lock: межпроцессная блокировка файла, таймаут, отказ на Windows."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest import mock

from _util import SCRIPTS, TempHome

from lib import lock
from lib.lock import file_lock
from lib.workspace import SrError

CHILD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from lib.lock import file_lock
    with file_lock(Path(sys.argv[2]), timeout=5):
        print("locked", flush=True)
        time.sleep(3)
    """
)


class FileLockTest(TempHome, unittest.TestCase):
    def test_acquire_release_and_reacquire(self):
        target = self.home / "sub" / "data.json"
        with file_lock(target):
            self.assertTrue((self.home / "sub" / "data.json.lock").exists())
        with file_lock(target, timeout=0.5):
            pass

    def test_timeout_when_held_by_other_process(self):
        target = self.home / "data.json"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(SCRIPTS), str(target)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            line = child.stdout.readline().strip()
            self.assertEqual(line, "locked", child.stderr.read() if line != "locked" else "")
            with self.assertRaises(SrError) as ctx:
                with file_lock(target, timeout=0.5):
                    self.fail("блокировка взята, хотя её держит другой процесс")
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn(str(target), ctx.exception.message)
        finally:
            child.kill()
            child.wait(timeout=10)
            child.stdout.close()
            child.stderr.close()

    def test_windows_code2(self):
        with mock.patch.object(lock, "_is_windows", return_value=True):
            with self.assertRaises(SrError) as ctx:
                with file_lock(self.home / "data.json"):
                    self.fail("на Windows блокировка должна отказать")
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("Windows", ctx.exception.message)

    def test_is_windows_false_here(self):
        self.assertEqual(lock._is_windows(), os.name == "nt")

    def test_on_wait_not_called_when_free(self):
        calls = []
        with file_lock(self.home / "data.json", timeout=0.5, on_wait=lambda: calls.append(1)):
            pass
        self.assertEqual(calls, [])


def hold_in_thread(target, seconds: float) -> tuple[threading.Thread, threading.Event]:
    """Поток держит `file_lock(target)` `seconds` секунд; событие — замок взят."""
    taken = threading.Event()

    def run():
        with file_lock(target, timeout=5):
            taken.set()
            time.sleep(seconds)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    if not taken.wait(5):
        raise AssertionError(f"поток не взял замок {target}")
    return th, taken


class CollectLockTest(TempHome, unittest.TestCase):
    """B05: замок сбора ждёт долго (`COLLECT_WAIT_SECONDS`) и один раз говорит об ожидании."""

    def setUp(self):
        super().setUp()
        self.state = self.home / ".retropair"
        self.target = self.state / "collect"

    def test_constants(self):
        self.assertEqual(lock.COLLECT_WAIT_SECONDS, 300)
        self.assertEqual(lock.COLLECT_NOTICE_AFTER, 1.0)

    def test_free_lock_no_notice(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), lock.collect_lock(self.state) as lock_path:
            self.assertEqual(lock_path, self.state / "collect.lock")
        self.assertEqual(err.getvalue(), "")

    def test_waits_for_holder_and_prints_one_notice(self):
        th, _ = hold_in_thread(self.target, 1.6)
        err = io.StringIO()
        start = time.monotonic()
        with contextlib.redirect_stderr(err), lock.collect_lock(self.state):
            waited = time.monotonic() - start
        th.join(5)
        self.assertGreaterEqual(waited, 1.0)
        lines = err.getvalue().splitlines()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("сбор логов уже идёт (collect.lock), жду до 300 с", lines[0])

    def test_short_hold_no_notice(self):
        th, _ = hold_in_thread(self.target, 0.3)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), lock.collect_lock(self.state):
            pass
        th.join(5)
        self.assertEqual(err.getvalue(), "")

    def test_timeout_is_code1_with_path_and_seconds(self):
        with file_lock(self.target), mock.patch.object(lock, "COLLECT_WAIT_SECONDS", 0.4), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(lock.LockBusy) as ctx:
                with lock.collect_lock(self.state):
                    self.fail("замок взят, хотя его держат")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(str(self.state / "collect.lock"), ctx.exception.message)
        self.assertIn("0.4 с", ctx.exception.message)

    def test_explicit_wait_overrides_constant(self):
        with file_lock(self.target), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(lock.LockBusy):
                with lock.collect_lock(self.state, wait=0.3):
                    pass


if __name__ == "__main__":
    unittest.main()
