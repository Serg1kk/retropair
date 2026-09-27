"""Команда `agents`: проектные агенты `.claude/agents/sessionretro-*.md` — write, check."""

from __future__ import annotations

import json
from pathlib import Path

from lib import agents
from lib.config import load_config
from lib.i18n import t
from lib.workspace import SrError, find_workspace, portable_path

I18N = ("cli", "setup")
REPORT_KEYS = (("created", "created"), ("updated", "changed"), ("unchanged", "unchanged"))


def register(subparsers) -> None:
    p = subparsers.add_parser("agents", help="проектные агенты с моделью и effort из config.models")
    sub = p.add_subparsers(dest="agents_command", metavar="<действие>", required=True)

    wr = sub.add_parser("write", help="записать агентов из болванок плагина в .claude/agents/")
    wr.add_argument("--force", action="store_true", help="перезаписать и правленые вручную файлы")
    wr.add_argument("--json", action="store_true", help="отчёт одним JSON-объектом (роли)")
    wr.set_defaults(func=handle_write)

    ck = sub.add_parser("check", help="сверить файлы агентов с config.models (код 1 — расхождения)")
    ck.add_argument("--json", action="store_true", help="статус по ролям JSON-массивом")
    ck.set_defaults(func=handle_check)


def _workspace(args) -> tuple[Path, dict]:
    root = find_workspace(Path.cwd(), args.workspace)
    return root, load_config(root)


def _rel(root: Path, role: str) -> str:
    return portable_path(agents.target(root, role), root)


def handle_write(args) -> int:
    root, cfg = _workspace(args)
    res = agents.write(root, cfg, force=args.force)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        for key, label in REPORT_KEYS:
            for role in res[key]:
                print(f"{t(I18N, label, cfg['lang'])}: {_rel(root, role)}")
    if res["skipped_edited"]:
        paths = ", ".join(_rel(root, role) for role in res["skipped_edited"])
        raise SrError(f"agents write: правлены вручную и не перезаписаны: {paths} — "
                      f"перезаписать: agents write --force", 1)
    return 0


def problems(item: dict, cfg: dict) -> list[str]:
    """Расхождения одной роли словами; пустой список — файл совпадает с конфигом."""
    if not item["exists"]:
        return ["нет файла"]
    spec = cfg["models"][item["role"]]
    out = []
    if not item["model_ok"]:
        out.append(f"model ≠ config {spec['model']}")
    if not item["effort_ok"]:
        out.append(f"effort ≠ config {spec['effort']}")
    if item["body_edited"]:
        out.append("тело правлено вручную")
    if item.get("outdated"):
        out.append("устарел: болванка плагина обновилась, файл не правили — agents write обновит")
    return out


def handle_check(args) -> int:
    root, cfg = _workspace(args)
    items = agents.status(root, cfg)
    bad = False
    lines = []
    for item in items:
        found = problems(item, cfg)
        bad = bad or bool(found)
        lines.append(f"{item['role']}: {'; '.join(found) if found else 'ok'} "
                     f"({_rel(root, item['role'])})")
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        print("\n".join(lines))
    return 1 if bad else 0
