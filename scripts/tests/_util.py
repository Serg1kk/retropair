"""Общие хелперы тестов: пути плагина, запуск sr.py, фикстуры, временный HOME.

Импорт этого файла кладёт `plugin/scripts` в `sys.path`, чтобы тесты могли
делать `from lib import ...`. Ничего вне `plugin/` не используется.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SCRIPTS = TESTS.parent
PLUGIN = SCRIPTS.parent
FIXTURES = TESTS / "fixtures"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def run_sr(*args: str, home: Path, cwd: Path) -> subprocess.CompletedProcess:
    """Запускает `sr.py` отдельным процессом с подменённым HOME."""
    env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "sr.py"), *args],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
    )


def copy_fixture(name: str, dst: Path) -> Path:
    """Копирует `tests/fixtures/<name>` в `dst/<name>` и возвращает новый путь."""
    src = FIXTURES / name
    if not src.is_dir():
        raise FileNotFoundError(f"retropair: фикстура не найдена: нет папки ({src})")
    target = Path(dst) / name
    shutil.copytree(src, target)
    return target


class TempHome:
    """Миксин для unittest.TestCase: временный HOME на каждый тест.

    В setUp создаёт временную папку, кладёт её в `os.environ["HOME"]` и в
    `self.home`; в tearDown возвращает прежний HOME и удаляет папку. Так ни
    один тест не пишет в настоящий домашний каталог.
    """

    def setUp(self) -> None:  # noqa: D401
        super().setUp()
        self._tmp_home = tempfile.TemporaryDirectory(prefix="sr-home-")
        self.home = Path(self._tmp_home.name)
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)

    def tearDown(self) -> None:
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self._tmp_home.cleanup()
        super().tearDown()
