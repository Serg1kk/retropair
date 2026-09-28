"""Отметка хранилища `plugin-state.json`: сдвиг на новую версию плагина.

`sync_stamp` вызывается перед каждой командой `sr.py` (см. `registry_cmds.dispatch`) для
глобальной отметки `~/.sessionretro/plugin-state.json` и отметки найденного workspace.
Отметку не создаёт: её пишут `init` и первая регистрация workspace. Миграций в формате 1
нет, поэтому сдвиг — это только `pluginVersion` и `lastSync`.

Статусы: `absent` — файла нет; `corrupt` — не JSON-объект, версия не разбирается как
semver или `storeKind` не тот (файл не меняется); `synced` — отметка была старше плагина и
сдвинута; `current` — версии равны; `newer` — отметка новее плагина (файл не меняется).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from lib.version import __version__
from lib.workspace import write_json_atomic

STORE_KINDS = ("global", "workspace")

_NUM = r"(0|[1-9]\d*)"
_SEMVER_RE = re.compile(
    rf"^{_NUM}\.{_NUM}\.{_NUM}(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def _semver_key(version) -> tuple | None:
    """Ключ сравнения по semver 2.0; пре-релиз (`-dev`) меньше релиза. Не semver → None."""
    if not isinstance(version, str):
        return None
    match = _SEMVER_RE.match(version)
    if match is None:
        return None
    major, minor, patch, pre = match.groups()
    if pre is None:
        pre_key: tuple = (1,)
    else:
        idents = pre.split(".")
        if "" in idents:
            return None
        pre_key = (0, tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in idents))
    return (int(major), int(minor), int(patch), pre_key)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plugin_key() -> tuple:
    key = _semver_key(__version__)
    if key is None:
        raise RuntimeError(f"версия плагина не semver: {__version__!r} (lib/version.py)")
    return key


def read_version(path: Path) -> object:
    """`pluginVersion` отметки как есть (для текста предупреждения); None, если не прочитать."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("pluginVersion") if isinstance(data, dict) else None


def sync_stamp(path: Path, store_kind: str) -> str:
    """Сдвигает отметку на текущую версию плагина, если она старше; возвращает статус.

    Ошибки чтения и записи файла (`OSError`) не глушатся — их ловит вызывающий.
    """
    if store_kind not in STORE_KINDS:
        raise ValueError(f"store_kind должен быть одним из {STORE_KINDS}, получено {store_kind!r}")
    path = Path(path)
    if not path.exists():
        return "absent"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:  # JSONDecodeError и UnicodeDecodeError
        return "corrupt"
    if not isinstance(data, dict):
        return "corrupt"
    if "storeKind" in data and data["storeKind"] != store_kind:
        return "corrupt"
    stamp_key = _semver_key(data.get("pluginVersion"))
    if stamp_key is None:
        return "corrupt"
    plugin_key = _plugin_key()
    if stamp_key == plugin_key:
        return "current"
    if stamp_key > plugin_key:
        return "newer"
    data["pluginVersion"] = __version__
    data["lastSync"] = _utc_now()
    write_json_atomic(path, data)
    return "synced"
