"""Источники логов: владелец, разрешение файлов по типу источника, мусор, git-клоны.

Источник — объект `{type, …, person?}` из `config.sources` или из `sources` карточки человека.
Типы: `specstory {path}`, `claude-jsonl {project}`, `dir {path}`, `git {url, branch?}`;
`codex-jsonl` и `cursor-jsonl` в 0.1 не поддерживаются — строка предупреждения.
Нумерация источников в сообщениях и в `source_id` — с 1 (`config#1`, `person:<id>#1`).
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lib.people import DEFAULT_CARD
from lib.registry import sr_home
from lib.workspace import STATE_DIR, SrError

# Где в чужом репозитории лежат логи сессий (раскладки, встреченные на практике).
LOG_GLOBS = [
    "**/.specstory/history/**/*",
    "**/ai-logs/**/*", "**/logs/**/*", "**/session-logs/**/*", "**/session_logs/**/*",
    "**/sessions/**/*", "**/chat-logs/**/*", "**/chatlogs/**/*", "**/agent-logs/**/*",
    "**/claude-logs/**/*", "**/codex-logs/**/*", "**/cursor-logs/**/*",
    "**/session-log/**/*", "**/ide-session*",
]
LOG_SUFFIXES = {".md", ".markdown", ".txt", ".jsonl"}
SKIP_DIRS = {".git", "node_modules", STATE_DIR}
UNSUPPORTED = {"codex-jsonl", "cursor-jsonl"}
REQUIRED = {"specstory": "path", "claude-jsonl": "project", "dir": "path", "git": "url"}
GIT_TIMEOUT = 300

_CONTEXT_REPORT_RE = re.compile(r"^context[-_]report", re.IGNORECASE)
# Файл SpecStory со слагом: `2026-09-01_10-00-00Z-fix-bug.md`.
_SLUG_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:Z|[+-]\d{4})?-.+\.md$")


@dataclass
class SourceFile:
    person_id: str
    source_type: str
    source_id: str
    path: Path


def munge(path: Path | str) -> str:
    """Имя папки проекта в `~/.claude/projects/`: каждый символ вне `[A-Za-z0-9]` → `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def claude_project_dir(root: Path, project: str) -> Path:
    """Папка JSONL Claude Code: `auto` — для корня проекта, абсолютный путь — для другого проекта.

    Абсолютный путь, который уже лежит прямо в `~/.claude/projects/`, берётся как есть.
    """
    if project == "auto":
        return _projects_dir() / munge(Path(root).resolve())
    given = Path(str(project)).expanduser()
    if not given.is_absolute():
        raise SrError(
            f"claude-jsonl: project должен быть auto или абсолютным путём (project: {project})", 1
        )
    if given.resolve().parent == _projects_dir().resolve():
        return given
    return _projects_dir() / munge(given.resolve())


def _card_ids(people: list[dict]) -> list[str]:
    return [p["id"] for p in people if p.get("id") != DEFAULT_CARD]


def owner(src: dict, people: list[dict], index: int) -> str:
    """Чей источник конфига: `person` источника, иначе единственная карточка, иначе ошибка."""
    ids = _card_ids(people)
    person = src.get("person")
    if person is not None:
        if person not in ids:
            raise SrError(
                f"источник {index} конфига: person {person!r} — нет такой карточки "
                f"(карточки: {', '.join(ids) or 'нет'})",
                1,
            )
        return person
    if len(ids) == 1:
        return ids[0]
    raise SrError(
        f"источник {index} конфига без person, а карточек {len(ids)}: укажите person в источнике "
        f"(тип: {src.get('type')})",
        1,
    )


def _is_junk(path: Path, junk: dict) -> bool:
    if path.name.endswith(".orig"):
        junk["orig"] += 1
        return True
    if _CONTEXT_REPORT_RE.match(path.name):
        junk["context_report"] += 1
        return True
    return False


def _is_subagent_file(path: Path) -> bool:
    """`<stem>/subagents/*.jsonl` при наличии `<stem>.jsonl` — его подтягивает парсер."""
    parent = path.parent
    if path.suffix != ".jsonl" or parent.name != "subagents":
        return False
    stem_dir = parent.parent
    return stem_dir.with_name(stem_dir.name + ".jsonl").is_file()


def _walk(folder: Path, suffixes: set[str]) -> list[Path]:
    """Файлы с нужными суффиксами рекурсивно, без `.git/`, `node_modules/`, `.sessionretro/`."""
    found = []
    for path in sorted(folder.rglob("*")):
        rel_parts = path.relative_to(folder).parts[:-1]
        if any(part in SKIP_DIRS for part in rel_parts) or not path.is_file():
            continue
        if path.name.endswith(".orig") or path.suffix.lower() in suffixes:
            found.append(path)
    return found


def _resolve_path(root: Path, value: str) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else Path(root) / path


def _run_git(args: list[str], what: str) -> None:
    try:
        res = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=GIT_TIMEOUT,
            stdin=subprocess.DEVNULL, env=_git_env(),
        )
    except FileNotFoundError as err:
        raise SrError(f"{what}: git не найден в PATH", 1) from err
    except subprocess.TimeoutExpired as err:
        raise SrError(f"{what}: нет ответа за {GIT_TIMEOUT} с", 1) from err
    if res.returncode != 0:
        tail = " ".join((res.stderr or res.stdout or "").strip().splitlines()[-3:])
        raise SrError(f"{what}: код {res.returncode}, {tail}", 1)


def _git_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _git_files(cfg: dict, person: str, src: dict) -> list[Path]:
    url = str(src["url"])
    if url.startswith("-"):
        raise SrError(f"git: url не может начинаться с «-» — git принял бы его за опцию (url: {url})", 1)
    ws_id = cfg["workspace"]["id"]
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    clone = sr_home() / "clones" / f"{ws_id}-{person}-{digest}"
    if (clone / ".git").is_dir():
        _run_git(["-C", str(clone), "pull", "--ff-only", "-q"], f"git pull --ff-only {url} ({clone})")
    else:
        clone.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["clone", "-q"]
        if src.get("branch"):
            cmd += ["--branch", str(src["branch"])]
        _run_git([*cmd, "--", url, str(clone)], f"git clone {url}")
    found: set[Path] = set()
    for pattern in LOG_GLOBS:
        for path in clone.glob(pattern):
            rel_parts = path.relative_to(clone).parts
            if ".git" in rel_parts or not path.is_file():
                continue
            if path.name.endswith(".orig") or path.suffix.lower() in LOG_SUFFIXES:
                found.add(path)
    return sorted(found)


def _candidates(root: Path, cfg: dict, person: str, src: dict, label: str,
                warnings: list[str]) -> list[Path] | None:
    """Кандидаты-файлы источника (до отсева мусора); `None` — источник пропущен с предупреждением."""
    kind = src.get("type")
    if kind in UNSUPPORTED:
        warnings.append(f"{label}: тип {kind} не поддерживается в 0.1")
        return None
    if kind not in REQUIRED:
        raise SrError(
            f"{label}: неизвестный тип источника {kind!r} "
            f"(допустимы: {', '.join(sorted([*REQUIRED, *UNSUPPORTED]))})",
            1,
        )
    field = REQUIRED[kind]
    if not src.get(field):
        raise SrError(f"{label}: у источника {kind} нет поля {field}", 1)

    if kind == "git":
        if cfg.get("mode") == "self":
            raise SrError(f"{label}: источник git только для режимов team/course, а режим self", 1)
        return _git_files(cfg, person, src)

    if kind == "claude-jsonl":
        folder = claude_project_dir(root, src["project"])
    else:
        folder = _resolve_path(root, src["path"])
    if not folder.is_dir():
        warnings.append(f"{label}: папки нет, источник пропущен ({kind}: {folder})")
        return None

    if kind == "specstory":
        return _walk(folder, {".md"})
    if kind == "claude-jsonl":
        return sorted(p for p in folder.iterdir()
                      if p.is_file() and (p.suffix == ".jsonl" or p.name.endswith(".orig")))
    return [p for p in _walk(folder, LOG_SUFFIXES) if not _is_subagent_file(p)]


def resolve(root: Path, cfg: dict, people: list[dict]) -> tuple[list[SourceFile], list[str], dict]:
    """Все файлы логов по источникам конфига и карточек: (файлы, предупреждения, мусор).

    `*.orig` и `context[-_]report*` не берутся, но считаются; слаг-файлы SpecStory берутся
    и считаются в `slug_named`.
    """
    warnings: list[str] = []
    junk = {"orig": 0, "context_report": 0, "slug_named": 0}
    plan: list[tuple[str, str, str, dict]] = []
    for i, src in enumerate(cfg.get("sources") or [], start=1):
        label = f"источник {i} конфига"
        plan.append((owner(src, people, i), f"config#{i}", label, src))
    for card in people:
        pid = card.get("id")
        if pid == DEFAULT_CARD:
            continue
        for i, src in enumerate(card.get("sources") or [], start=1):
            plan.append((pid, f"person:{pid}#{i}", f"источник {i} карточки {pid}", src))

    files: list[SourceFile] = []
    for person, source_id, label, src in plan:
        found = _candidates(Path(root), cfg, person, src, label, warnings)
        if found is None:
            continue
        for path in found:
            if _is_junk(path, junk):
                continue
            if _SLUG_RE.match(path.name):
                junk["slug_named"] += 1
            files.append(SourceFile(person, src["type"], source_id, path))
    return files, warnings, junk
