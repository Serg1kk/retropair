"""lib.lock: межпроцессная блокировка файла, таймаут, отказ на Windows."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
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


if __name__ == "__main__":
    unittest.main()
