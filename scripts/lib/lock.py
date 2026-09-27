"""Межпроцессная блокировка файла: advisory `fcntl.flock` на соседний `<path>.lock`."""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from lib.workspace import SrError

POLL_SECONDS = 0.2


class LockBusy(SrError):
    """Блокировка не получена за `timeout`: файл держит другой процесс (код 1)."""


def _is_windows() -> bool:
    return os.name == "nt"


@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 10):
    """Держит исключительную блокировку `<path>.lock` на время блока `with`.

    Опрос каждые 0.2 с до `timeout`; не взяли — `LockBusy` (подкласс `SrError`, код 1). Lock-файл не удаляется.
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
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
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
