"""Самопроверка движка оценки: unittest-прогон файлов тестов движка из `scripts/tests/`.

Файл тестов, которого нет на диске, не пропускается молча: строка «не найден <имя>» и код 1.
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ENGINE_MODULES = [
    "test_tracker", "test_tracker_ops", "test_marks", "test_level", "test_batches",
    "test_collector_check", "test_render_review", "test_approve",
]


def run(modules: list[str], tests_dir: Path) -> tuple[int, str]:
    """Прогнать файлы тестов `modules` (имена без `.py`) из `tests_dir`; вернуть код (0/1) и текст отчёта."""
    tests_dir = Path(tests_dir)
    lines: list[str] = []
    found: list[str] = []
    for name in modules:
        if (tests_dir / f"{name}.py").is_file():
            found.append(name)
        else:
            lines.append(f"не найден {name} ({tests_dir / (name + '.py')})")
    inserted = str(tests_dir) not in sys.path
    if inserted:
        sys.path.insert(0, str(tests_dir))
    try:
        suite = unittest.defaultTestLoader.loadTestsFromNames(found) if found else unittest.TestSuite()
        stream = io.StringIO()
        result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    finally:
        if inserted and str(tests_dir) in sys.path:
            sys.path.remove(str(tests_dir))
    failed = len(result.failures) + len(result.errors) + len(result.unexpectedSuccesses)
    report = [stream.getvalue().rstrip()] if failed else []
    report += lines
    report.append(f"проверок {result.testsRun}, упало {failed}")
    code = 0 if failed == 0 and not lines else 1
    return code, "\n".join(report)
