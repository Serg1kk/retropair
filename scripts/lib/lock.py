"""Межпроцессная блокировка файла: advisory `fcntl.flock` на соседний `<path>.lock`.

Замок сбора логов `.sessionretro/collect.lock` (`collect_lock`) держит `collect` на весь проход,
а после смены правил санитайзера проход перечищает все снапшоты — это минуты. Поэтому команды,
которым нужен индекс (`inventory`, ручной `collect`, пометки), ждут его до
`COLLECT_WAIT_SECONDS` и один раз говорят в stderr, что ждут. Остальные замки (реестр, трекер)
держатся доли секунды: у них прежние 10 с.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

from lib.workspace import SrError

POLL_SECONDS = 0.2
COLLECT_LOCK = "collect"          # `<state_dir>/collect` → lock-файл `collect.lock`
COLLECT_WAIT_SECONDS = 300        # сколько ждать соседний сбор (перечистка всех снапшотов)
COLLECT_NOTICE_AFTER = 1.0        # замок занят дольше — одна строка в stderr об ожидании


class LockBusy(SrError):
    """Блокировка не получена за `timeout`: файл держит другой процесс (код 1)."""


def _is_windows() -> bool:
    return os.name == "nt"


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 10, on_wait: Callable[[], None] | None = None,
              notice_after: float = COLLECT_NOTICE_AFTER):
    """Держит исключительную блокировку `<path>.lock` на время блока `with`.

    Опрос каждые 0.2 с до `timeout`; не взяли — `LockBusy` (подкласс `SrError`, код 1). Lock-файл не удаляется.
    `on_wait` вызывается один раз, если замок занят дольше `notice_after` секунд.
    На Windows — `SrError(2)`: в 0.1 блокировка там не реализована.
    """
    if _is_windows():
        raise SrError(
            f"блокировка файлов на Windows не поддерживается в 0.1 (файл: {path})", 2
        )
    import fcntl

    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        start = time.monotonic()
        deadline = start + timeout
        noticed = on_wait is None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                now = time.monotonic()
                if not noticed and now - start >= notice_after and now < deadline:
                    noticed = True
                    on_wait()
                if now >= deadline:
                    raise LockBusy(
                        f"блокировка не получена: файл занят другим процессом дольше "
                        f"{timeout} с (файл: {path}, lock: {lock_path})",
                        1,
                    ) from None
                time.sleep(POLL_SECONDS)
        try:
            yield lock_path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def collect_lock(state_dir: Path, *, wait: float | None = None, notice: bool = True):
    """Замок сбора логов `<state_dir>/collect.lock` — общий для `collect`, `inventory` и пометок.

    Ждёт до `wait` секунд (`None` → `COLLECT_WAIT_SECONDS`, читается при вызове); занят дольше
    `COLLECT_NOTICE_AFTER` — одна строка в stderr (`notice=False` — молча). Не дождался —
    `LockBusy` (код 1) с путём lock-файла и временем ожидания.
    """
    wait = COLLECT_WAIT_SECONDS if wait is None else wait

    def _say() -> None:
        print(f"sessionretro: сбор логов уже идёт (collect.lock), жду до {wait:g} с… "
              f"({Path(state_dir) / (COLLECT_LOCK + '.lock')})", file=sys.stderr, flush=True)

    return file_lock(Path(state_dir) / COLLECT_LOCK, timeout=wait, on_wait=_say if notice else None)
