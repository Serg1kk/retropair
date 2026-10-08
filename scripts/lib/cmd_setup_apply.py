"""Команда `setup-apply`: применить ответы онбординга (JSON-файл или `-` — stdin) к workspace.

`doctor_lines` добавляет в `doctor` пункты онбординга: источники, проектные агенты, `.gitignore`,
`show_scores`, режим периода, шаблон.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from lib import agents, setup
from lib.config import load_config
from lib.i18n import t
from lib.people import list_people
from lib.sources import claude_project_dir
from lib.workspace import STATE_DIR, SrError, portable_path

I18N = ("cli", "setup")
AGENTS_OK = "{n}/{n} совпадают с config.models"
AGENTS_NONE = "нет"
AGENTS_DIFF = "расходятся: {roles}"
LOCAL_SOURCES = ("specstory", "dir", "claude-jsonl")


def register(subparsers) -> None:
    p = subparsers.add_parser("setup-apply", help="применить ответы онбординга к workspace")
    p.add_argument("--answers", required=True, metavar="FILE|-",
                   help="JSON-объект ответов: путь к файлу или - (stdin)")
    p.add_argument("--json", action="store_true", help="отчёт одним JSON-объектом")
    p.set_defaults(func=handle)


def read_answers(source: str):
    """Ответы из файла или stdin; нет файла или битый JSON — `SrError(2)`."""
    where = "stdin" if source == "-" else str(Path(source).expanduser())
    try:
        if source == "-":
            text = sys.stdin.read()
        else:
            path = Path(source).expanduser()
            if not path.is_file():
                raise SrError(f"setup-apply: файл ответов не найден (--answers: {source})", 2)
            text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as err:
        raise SrError(f"setup-apply: ответы не в UTF-8: {err.reason} ({where})", 2) from err
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise SrError(f"setup-apply: ответы не разбираются как JSON: {err} ({where})", 2) from err


def workspace_dir(explicit: str) -> str:
    """Флаг `--workspace`: корень проекта или сама папка `.retropair`."""
    given = Path(explicit).expanduser().resolve()
    return str(given.parent if given.name in (STATE_DIR, ".sessionretro") else given)


def handle(args) -> int:
    answers = read_answers(args.answers)
    if isinstance(answers, dict) and args.workspace is not None:
        flag = workspace_dir(args.workspace)
        if "path" not in answers:
            answers["path"] = flag
        elif isinstance(answers["path"], str):
            given = Path(answers["path"]).expanduser()
            given = (given if given.is_absolute() else Path.cwd() / given).resolve()
            if str(given) != flag:
                raise SrError(f"setup-apply: --workspace и path в ответах указывают на разные "
                              f"папки (--workspace: {flag}; path: {given}) — уберите path из "
                              f"ответов или флаг", 2)
    report = setup.apply(answers, Path.cwd(), Path.home())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    lang = load_config(Path(report["root"]))["lang"]
    for key in ("created", "changed", "unchanged"):
        for item in report[key]:
            print(f"{t(I18N, key, lang)}: {item}")
    for item in report["advice"]:
        print(f"{t(I18N, 'advice', lang)}: {item}")
    if report["hook_check"]:
        print(report["hook_check"])
    return 0


def _source_dir(root: Path, src: dict) -> Path:
    if src["type"] == "claude-jsonl":
        return claude_project_dir(root, src["project"])
    path = Path(src["path"]).expanduser()
    return path if path.is_absolute() else root / path


def _all_sources(root: Path, cfg: dict) -> list[dict]:
    """Источники конфига и карточек `people/<id>.yaml` — те же, что читает сбор логов."""
    found = list(cfg["sources"])
    for card in list_people(root):
        found += card.get("sources") or []
    return found


def _sources_found(root: Path, cfg: dict) -> str:
    """Локальные источники конфига и карточек, чьи папки есть: `N/M папок найдено; нет: …; git: K`."""
    items = _all_sources(root, cfg)
    local = [src for src in items if src.get("type") in LOCAL_SOURCES]
    missing = [portable_path(_source_dir(root, src), root) for src in local
               if not _source_dir(root, src).is_dir()]
    text = f"{len(local) - len(missing)}/{len(local)} папок найдено"
    if missing:
        text += f"; нет: {', '.join(missing)}"
    remote = sum(1 for src in items if src.get("type") not in LOCAL_SOURCES)
    if remote:
        text += f"; не проверяются (git и прочие): {remote}"
    return text


def _project_agents(root: Path, cfg: dict) -> str:
    items = agents.status(root, cfg)
    if not any(item["exists"] for item in items):
        return AGENTS_NONE
    bad = [item["role"] for item in items
           if not (item["exists"] and item["model_ok"] and item["effort_ok"])
           or item["body_edited"] or item["outdated"]]
    if bad:
        return AGENTS_DIFF.format(roles=", ".join(bad))
    return AGENTS_OK.format(n=len(items))


def _gitignore(root: Path, cfg: dict) -> bool | str:
    """Все строки на месте — True; иначе «нет» с подсказкой, как дописать."""
    if setup.gitignore_complete(root):
        return True
    return t(I18N, "gitignore_no", cfg["lang"])


def doctor_lines(root: Path | None) -> list[tuple[str, object]]:
    """Пункты онбординга для `doctor`; вне workspace — ничего."""
    if root is None:
        return []
    cfg = load_config(root)
    return [
        ("sources_found", _sources_found(root, cfg)),
        ("project_agents", _project_agents(root, cfg)),
        ("gitignore", _gitignore(root, cfg)),
        ("show_scores", cfg["show_scores"]),
        ("period_mode", cfg["period"]["mode"]),
        ("template", cfg["template"]),
    ]
