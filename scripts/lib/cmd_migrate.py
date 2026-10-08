"""Explicit, byte-preserving SessionRetro → RetroPair state migration.

Preview never writes. Apply uses a same-parent atomic rename, without copying or
rewriting any user logs, profiles, assessments, configuration or approval state.
Disable/uninstall the old plugin before apply and stop other workspace commands.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path

from lib.version import __version__
from lib.workspace import LEGACY_STATE_DIR, STATE_DIR, SrError, write_json_atomic, write_text_atomic


def register(subparsers):
    p = subparsers.add_parser("migrate", help="предпросмотр миграции SessionRetro → RetroPair")
    p.add_argument("--apply", action="store_true", help="применить проверенный атомарный перенос")
    p.add_argument("--global", dest="global_state", action="store_true", help="перенести ~/.sessionretro")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=handle)


def migration_root(explicit=None):
    given = Path(explicit).expanduser().absolute() if explicit else Path.cwd().resolve()
    if given.name in (STATE_DIR, LEGACY_STATE_DIR):
        return given.parent
    if explicit:
        return given
    for root in (given, *given.parents):
        if any((root / name).exists() for name in (STATE_DIR, LEGACY_STATE_DIR)):
            return root
    return given


def _inventory(folder):
    """Names + size + digest, no content in output; reject links outside state."""
    files = {}
    for p in sorted(folder.rglob("*")):
        if p.is_symlink():
            raise SrError(f"migrate: симлинк требует ручной проверки: {p}", 1)
        if p.is_file() and not p.name.endswith(".lock"):
            files[p.relative_to(folder).as_posix()] = {
                "bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
    return files


def preview(root, global_state=False):
    root = Path(root)
    old, new = root / LEGACY_STATE_DIR, root / STATE_DIR
    for p in (old, new):
        if p.is_symlink():
            raise SrError(f"migrate: каталог является симлинком: {p}", 1)
    if old.exists() and new.exists():
        raise SrError(f"migrate: конфликт: существуют {old} и {new}; ничего не изменено", 1)
    status = "ready" if old.is_dir() else "already_current" if new.is_dir() else "absent"
    if old.exists() and not old.is_dir():
        raise SrError(f"migrate: не каталог: {old}", 1)
    if status == "ready":
        required = "workspaces.json" if global_state else "config.json"
        if not (old / required).is_file():
            raise SrError(f"migrate: нет обязательного файла {old / required}", 1)
        # Validate the primary state contract before renaming; all other bytes stay untouched.
        try:
            primary = json.loads((old / required).read_text())
        except (OSError, ValueError) as err:
            raise SrError(f"migrate: {old / required}: {err}", 1) from err
        if global_state:
            if not isinstance(primary, list):
                raise SrError(f"migrate: реестр не массив: {old / required}", 1)
        else:
            from lib.config import check_config
            check_config(primary, old / required)
    files = _inventory(old) if status == "ready" else {}
    return {"status": status, "source": str(old), "destination": str(new),
            "files": len(files), "bytes": sum(v["bytes"] for v in files.values()),
            "applied": False, "precondition": "Disable the old plugin and stop workspace commands first."}


@contextlib.contextmanager
def _locks(old):
    """Refuse active collect/tracker/registry operations, including legacy lock names."""
    import fcntl
    with contextlib.ExitStack() as stack:
        paths = set(old.rglob("*.lock")) | {old / "collect.lock"}
        for p in sorted(paths):
            fh = stack.enter_context(open(p, "a+b"))
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                raise SrError(f"migrate: активная операция, файл занят: {p}", 1) from err
        yield


def migrate(root, apply=False, global_state=False):
    result = preview(root, global_state)
    if not apply or result["status"] != "ready":
        return result
    old, new = Path(result["source"]), Path(result["destination"])
    # Protect migrated files before they appear under their new names; retain old rules.
    ignore = Path(root) / ".gitignore"
    if not global_state:
        from lib.setup import GITIGNORE_LINES
        text = ignore.read_text() if ignore.exists() else ""
        rules = [*GITIGNORE_LINES, ".retropair-migration.json", ".retropair-migration.lock"]
        # Carry custom legacy protections forward as well (including raw snapshots).
        rules += [line.replace(LEGACY_STATE_DIR, STATE_DIR) for line in text.splitlines()
                  if LEGACY_STATE_DIR in line and not line.lstrip().startswith("#")]
        have = set(text.splitlines())
        extra = list(dict.fromkeys(rule for rule in rules if rule not in have))
        if extra:
            write_text_atomic(ignore.resolve(), text + ("\n" if text and not text.endswith("\n") else "")
                              + "\n# RetroPair migration: preserve private state protections\n"
                              + "\n".join(extra) + "\n")
    # Separate lock survives rename and serializes competing migrations.
    from lib.lock import file_lock
    with file_lock(Path(root) / ".retropair-migration", timeout=0):
        result = preview(root, global_state)
        if result["status"] != "ready":
            return result
        with _locks(old):
            before = _inventory(old)
            if new.exists() or new.is_symlink():
                raise SrError(f"migrate: каталог назначения появился: {new}", 1)
            os.rename(old, new)
            try:
                if _inventory(new) != before:
                    raise SrError(f"migrate: проверка переноса не сошлась: {new}", 1)
            except BaseException:
                os.rename(new, old)
                raise
        audit = {"from": LEGACY_STATE_DIR, "to": STATE_DIR, "version": __version__,
                 "files": before, "method": "atomic-rename", "user_data_rewritten": False}
        write_json_atomic(Path(root) / ".retropair-migration.json", audit)
        os.chmod(Path(root) / ".retropair-migration.json", 0o600)
        result.update(status="migrated", applied=True)
    return result


def handle(args):
    root = Path.home() if args.global_state else migration_root(args.workspace)
    result = migrate(root, args.apply, args.global_state)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{result['status']}: {result['source']} → {result['destination']} "
              f"({result['files']} files, {result['bytes']} bytes)")
        if result['status'] == 'ready':
            print(result['precondition'] + " Apply with --apply.")
    return 0
