"""SpecStory CLI: версия, `specstory sync <provider>` в корне проекта, выбор провайдеров.

`sync` не бросает исключений: бинаря нет, версия ниже минимальной, код ≠ 0 или таймаут —
строка с причиной для сводки collect (сбор идёт по уже лежащим файлам).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from lib.cmd_doctor import SPECSTORY_TIMEOUT, specstory_version_line

MIN_VERSION = (2, 6, 0)
SYNC_TIMEOUT = 120
DEFAULT_PROVIDERS = ["claude"]

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")


def _fmt(ver: tuple[int, int, int]) -> str:
    return ".".join(str(n) for n in ver)


def _probe() -> tuple[tuple[int, int, int] | None, str]:
    """(версия, причина): версия `None` — причина объясняет, почему."""
    if shutil.which("specstory") is None:
        return None, "specstory не найден"
    try:
        res = subprocess.run(
            ["specstory", "--version"], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=SPECSTORY_TIMEOUT,
        )
    except FileNotFoundError:
        return None, "specstory не найден"
    except subprocess.TimeoutExpired:
        return None, f"specstory --version: нет ответа за {SPECSTORY_TIMEOUT} с"
    if res.returncode != 0:
        return None, f"specstory --version: код {res.returncode}"
    line = specstory_version_line(res.stdout or res.stderr)
    match = _VERSION_RE.match(line)
    if not match:
        return None, f"specstory: {line}"
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch or 0)), ""


def version() -> tuple[int, int, int] | None:
    """Версия `specstory` кортежем; `None`, если бинаря нет или версия не распознана."""
    return _probe()[0]


def _tail(text: str) -> str:
    return " ".join((text or "").strip().splitlines()[-3:])


def sync(root: Path, provider: str) -> str:
    """`specstory sync <provider>` с cwd = корень проекта; возвращает строку для сводки."""
    ver, reason = _probe()
    if ver is None:
        return f"{reason}, sync пропущен"
    if ver < MIN_VERSION:
        return f"specstory {_fmt(ver)} ниже {_fmt(MIN_VERSION)}, sync пропущен"
    try:
        res = subprocess.run(
            ["specstory", "sync", provider], stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=SYNC_TIMEOUT, cwd=str(root),
        )
    except FileNotFoundError:
        return "specstory не найден, sync пропущен"
    except subprocess.TimeoutExpired:
        return f"specstory sync {provider}: нет ответа за {SYNC_TIMEOUT} с"
    if res.returncode != 0:
        return f"specstory sync {provider}: код {res.returncode}, {_tail(res.stderr or res.stdout)}"
    return f"specstory sync {provider}: ок"


def providers(args_providers: list[str] | None, cfg: dict) -> list[str]:
    """`--provider` (повторяемый) → иначе `config.hook.providers` → иначе `["claude"]`; без повторов."""
    chosen = args_providers or (cfg.get("hook") or {}).get("providers") or DEFAULT_PROVIDERS
    return list(dict.fromkeys(chosen))
