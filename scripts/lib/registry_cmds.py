"""Авто-регистрация команд `sr.py`: файлы `lib/cmd_*.py` находятся и подключаются сами.

Контракт файла команды (новая команда = новый файл, `sr.py` и этот файл не правятся):

- Один файл на команду верхнего уровня: `lib/cmd_<команда>.py`; дефисы в имени команды
  становятся `_` в имени файла (`check-collector` → `cmd_check_collector.py`).
- Файл экспортирует `register(subparsers)`: вызывает `subparsers.add_parser(<имя>, …)` и
  `set_defaults(func=handler)`. Подкоманды — `add_subparsers(dest=…, required=True)`.
- `handler(args) -> int` возвращает код выхода (0 ок, 1 данные/гейт/валидация, 2 ошибка
  вызова); не `int` → `RuntimeError`. Ожидаемые ошибки — `SrError(message, code)` из
  `lib.workspace`, `sr.py` печатает их в stderr.
- Workspace: `find_workspace(Path.cwd(), args.workspace)` (флаг `--workspace` общий, его
  разбирает `sr.py` в любой позиции).
- Необязательно: `doctor_lines(root: Path | None) -> list[tuple[str, object]]` — строки,
  которые `doctor` добавит в свой вывод (`root` — корень workspace или None). Исключение
  внутри превращается в строку `<файл>: ошибка <текст>` и код 1 у `doctor`.
- Имя команды (и alias) уникально: дубль → `RuntimeError` с именами обоих файлов.
- Перед handler `dispatch` сдвигает отметки `plugin-state.json` на версию плагина
  (`lib/plugin_state.py`); сбой — строка предупреждения в stderr, команда продолжается.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from lib import plugin_state, registry
from lib.version import __version__
from lib.workspace import STATE_DIR, SrError, find_workspace

LIB_DIR = Path(__file__).resolve().parent


def module_file(module: ModuleType) -> str:
    """Имя файла команды (`cmd_init.py`) — для сообщений об ошибках."""
    return Path(module.__file__).name


def _load_external(path: Path) -> ModuleType:
    digest = hashlib.sha1(str(path.parent).encode("utf-8")).hexdigest()[:10]
    spec = importlib.util.spec_from_file_location(f"_sr_cmds_{digest}_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"файл команды не загружается: нет загрузчика ({path})")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover(lib_dir: Path | None = None) -> list[ModuleType]:
    """Импортирует все `cmd_*.py` из `lib_dir` (по умолчанию — эта папка) по алфавиту."""
    folder = Path(lib_dir).resolve() if lib_dir is not None else LIB_DIR
    if not folder.is_dir():
        raise RuntimeError(f"папка файлов команд не найдена ({folder})")
    modules = []
    for path in sorted(folder.glob("cmd_*.py")):
        if folder == LIB_DIR:
            modules.append(importlib.import_module(f"lib.{path.stem}"))
        else:
            modules.append(_load_external(path))
    return modules


class _GuardedSubparsers:
    """Обёртка над `subparsers`: помнит, какой файл объявил какое имя команды."""

    def __init__(self, real, owners: dict[str, str]) -> None:
        self._real = real
        self._owners = owners
        self.current = ""

    def add_parser(self, name, **kwargs):
        for candidate in (name, *kwargs.get("aliases", ())):
            owner = self._owners.get(candidate)
            if owner is not None:
                raise RuntimeError(
                    f"команда '{candidate}' объявлена дважды: {owner} и {self.current}"
                )
            self._owners[candidate] = self.current
        return self._real.add_parser(name, **kwargs)

    def __getattr__(self, attr):
        return getattr(self._real, attr)


def build_parser(modules=None) -> argparse.ArgumentParser:
    """Парсер `sr.py`: общие флаги и команды из всех файлов `cmd_*.py`."""
    if modules is None:
        modules = discover()
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name if Path(sys.argv[0]).name in ("sr.py", "rp.py") else "rp.py", description="retropair: ретро работы с кодинг-агентом по логам сессий.",
        # без сокращений: `--works X` иначе попадал бы в --workspace и молча терялся
        allow_abbrev=False,
    )
    parser.add_argument("--workspace", metavar="DIR",
                        help="корень проекта или папка .retropair (по умолчанию поиск вверх от cwd)")
    parser.add_argument("--version", action="version", version=f"retropair {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<команда>")
    guard = _GuardedSubparsers(subparsers, {})
    for module in modules:
        register = getattr(module, "register", None)
        if not callable(register):
            raise RuntimeError(
                f"в файле команды нет функции register(subparsers) ({module_file(module)})"
            )
        guard.current = module_file(module)
        register(guard)
    return parser


def _stamp_warning(path: Path, reason: str) -> None:
    print(f"retropair: предупреждение: plugin-state.json {path}: {reason}, не изменён",
          file=sys.stderr)


def _sync_one(path: Path, store_kind: str) -> None:
    try:
        status = plugin_state.sync_stamp(path, store_kind)
    except (OSError, SrError) as err:
        text = err.message if isinstance(err, SrError) else str(err)
        _stamp_warning(path, f"ошибка {type(err).__name__}: {text}")
        return
    if status == "corrupt":
        _stamp_warning(path, "битый")
    elif status == "newer":
        _stamp_warning(path, f"новее плагина ({plugin_state.read_version(path)})")


def sync_plugin_stamps(args: argparse.Namespace) -> None:
    """Сдвигает отметки `plugin-state.json` (глобальную и workspace, если он найден) на
    текущую версию плагина. Сбой — одна строка предупреждения в stderr, команда идёт дальше."""
    try:
        global_state = registry.sr_home() / "plugin-state.json"
    except SrError as err:
        # Local validation/report commands must not depend on migrating another store.
        _stamp_warning(Path.home() / ".retropair" / "plugin-state.json", err.message)
    else:
        _sync_one(global_state, "global")
    try:
        root = find_workspace(Path.cwd(), getattr(args, "workspace", None))
    except (SrError, OSError):
        return  # команда без workspace — не ошибка; нужен — handler сам сообщит
    _sync_one(root / STATE_DIR / "plugin-state.json", "workspace")


def dispatch(args: argparse.Namespace) -> int:
    """Сдвигает отметки `plugin-state.json`, вызывает handler; результат не `int` → `RuntimeError`."""
    func = getattr(args, "func", None)
    if func is None:
        raise RuntimeError(
            f"команда '{args.command}' не задала handler через set_defaults(func=…)"
        )
    if args.command != "migrate":
        sync_plugin_stamps(args)
    rc = func(args)
    if not isinstance(rc, int) or isinstance(rc, bool):
        raise RuntimeError(
            f"handler команды '{args.command}' вернул {type(rc).__name__}, а нужен int"
        )
    return rc
