"""Команда `self-test`: прогон тестов движка оценки, итог «проверок N, упало K»."""

from __future__ import annotations

import sys
from pathlib import Path

from lib import self_test


def register(subparsers) -> None:
    p = subparsers.add_parser("self-test", help="прогнать тесты движка оценки")
    p.set_defaults(func=handle)


def handle(args) -> int:
    sys.dont_write_bytecode = True
    tests_dir = Path(__file__).resolve().parents[1] / "tests"
    code, text = self_test.run(self_test.ENGINE_MODULES, tests_dir)
    print(text)
    return code
