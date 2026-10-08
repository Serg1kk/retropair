"""Предложения движку: файлы `.retropair/improvements/imp-ГГГГММДД-<слаг>-<hex4>.md`.

Статусы: `pending` → `sent` (пользователь отправил пакет сам) → `implemented (X.Y.Z)` (строка
`внедрено: imp-…` в CHANGELOG плагина под заголовком версии; файл уходит в `improvements/archive/`,
архив дальше не читается).

Анонимизация на записи: пути через `portable_text` (проект — относительные, HOME — `~/…`), затем
`Sanitizer` плагина с именами и id карточек людей и логином ОС; оставшийся домашний путь
`/<home>/[USER]` → `~`. Слаг ID строится из уже обезличенного компонента (`--slug` проходит тот же
санитайзер). Если после этого в тексте или ID остались путь с логином, e-mail, имя, id карточки
или логин — `SrError(1)`, файл не пишется. Пакет (`pack`) проверяется ещё раз: файл могли
дописать руками.

Ничего не отправляется: `gh_command` только собирает строку команды для пользователя.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import secrets
import shlex
from pathlib import Path

from lib.people import DEFAULT_CARD, list_people
from lib.registry import slugify
from lib.sanitize import RULES, Sanitizer, local_user_names, names_from_people
from lib.version import __version__, plugin_root
from lib.workspace import SrError, portable_text, write_text_atomic, ws_paths

ID_RE = r"imp-\d{8}-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{4}"
DONE_RE = r"внедрено\s*:"
SLUG_RE = r"[a-z0-9]+(?:-[a-z0-9]+)*"
PENDING, SENT = "pending", "sent"
IMPLEMENTED = "implemented ({version})"
DOCTOR_KEY = "improvements_pending"

FIELDS = ("ID", "Компонент", "Симптом и контекст", "Локальный обход", "Предложение",
          "Версия плагина", "Дата", "Статус")
STATUS = "Статус"

_ID = re.compile(ID_RE)
_DONE = re.compile(DONE_RE, re.IGNORECASE)
_FIELD = re.compile(r"^- \*\*(.+?):\*\* (.*)$")
_VERSION_HEAD = re.compile(r"^## \[(\d+\.\d+\.\d+[0-9A-Za-z.\-+]*)\]")
_REPO = re.compile(r"^https://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$")
# домашний путь, который санитайзер уже обезличил: `/<home>/[USER]` → `~`
_MASKED_HOME = re.compile(r"(?:/(?:Users|home)/|(?i:[A-Z]:\\Users\\))\[USER\]")
_PLACEHOLDER = re.compile(r"\[[A-Z_]+\]")
# id карточек, которые не выдают человека: дефолты и карточка self
_NEUTRAL_IDS = frozenset({DEFAULT_CARD, "me"})
_EMAIL = re.compile(next(pattern for rule, pattern, _ in RULES if rule == "email"))
_LEAK_PATHS = (
    ("путь", re.compile(r"/(?:Users)/[^\s\"'`)\]]*")),
    ("путь", re.compile(r"/home/[A-Za-z0-9_.\-]+/?")),
    ("путь", re.compile(r"(?i)[A-Z]:\\Users\\[^\s\"'`)\]]*")),
)


# ── проверки текста ──────────────────────────────────────────────────────────


def leaks(text: str, names: list[str]) -> list[str]:
    """Утечки в тексте: пути с логином, e-mail, имена из карточек (длинные раньше коротких,
    одно вхождение — одна находка). Строки вида `путь: …`, `e-mail: …`, `имя: …`."""
    found: list[str] = []
    for kind, pattern in _LEAK_PATHS:
        found += [f"{kind}: {m.group(0)}" for m in pattern.finditer(text)]
    found += [f"e-mail: {m.group(0)}" for m in _EMAIL.finditer(text)]
    clean = sorted({n.strip() for n in names if isinstance(n, str) and n.strip()},
                   key=len, reverse=True)
    if clean:
        names_re = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in clean) + r")(?!\w)",
                              re.IGNORECASE)
        found += [f"имя: {m.group(0)}" for m in names_re.finditer(text)]
    return found


def private_words(root: Path) -> list[str]:
    """Что не должно попасть в предложение: имена из карточек, id карточек (кроме `_default`,
    `me`) и логин ОС (`local_user_names`: не общие вроде user/root)."""
    cards = list_people(root)
    ids = [card["id"] for card in cards if card["id"] not in _NEUTRAL_IDS]
    out: list[str] = []
    for word in names_from_people(cards) + ids + local_user_names():
        if word.casefold() not in {w.casefold() for w in out}:
            out.append(word)
    return out


def _clean(value: str, root: Path, sanitizer: Sanitizer) -> str:
    text, _ = sanitizer.sanitize_text(portable_text(value, root))
    return " ".join(_MASKED_HOME.sub("~", text).split())


# ── файлы предложений ────────────────────────────────────────────────────────


def _folder(root: Path) -> Path:
    return ws_paths(root)["improvements"]


def _date(today) -> _dt.date:
    if today is None:
        return _dt.date.today()
    if isinstance(today, _dt.date):
        return today
    try:
        return _dt.date.fromisoformat(str(today))
    except ValueError as err:
        raise SrError(f"improve: дата не в формате ГГГГ-ММ-ДД (today: {today!r})", 2) from err


def _render(ident: str, values: dict[str, str]) -> str:
    lines = [f"# {ident}", ""] + [f"- **{name}:** {values[name]}" for name in FIELDS]
    return "\n".join(lines) + "\n"


def add(root: Path, component: str, symptom: str, workaround: str, proposal: str,
        slug: str | None = None, today=None) -> Path:
    """Новое предложение со статусом `pending`; путь к файлу. Пустое поле или кривой слаг — код 2,
    утечка после обезличивания — код 1 (файл не пишется)."""
    raw = {"Компонент": component, "Симптом и контекст": symptom,
           "Локальный обход": workaround, "Предложение": proposal}
    for name, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise SrError(f"improve add: пустое поле «{name}»", 2)
    if slug is not None and not re.fullmatch(SLUG_RE, slug):
        raise SrError(f"improve add: слаг — латиница в нижнем регистре, цифры и дефис "
                      f"(--slug: {slug})", 2)
    day = _date(today)
    words = private_words(root)
    sanitizer = Sanitizer(names=words, local_users=local_user_names())
    values = {name: _clean(value, root, sanitizer) for name, value in raw.items()}
    if slug is None:
        slug = slugify(_PLACEHOLDER.sub(" ", values["Компонент"]))
    elif _clean(slug, root, sanitizer) != slug:
        raise SrError(f"improve add: в слаге личные данные, файл не записан — задайте другой "
                      f"--slug (--slug: {slug})", 1)
    bad = [f"{name}: {hit}" for name, value in values.items() for hit in leaks(value, words)]
    bad += [f"ID: {hit}" for hit in leaks(slug, words)]
    if bad:
        raise SrError("improve add: после обезличивания остались личные данные, файл не записан: "
                      + "; ".join(bad), 1)
    folder = _folder(root)
    while True:
        ident = f"imp-{day:%Y%m%d}-{slug}-{secrets.token_hex(2)}"
        path = folder / f"{ident}.md"
        if not path.exists():
            break
    values.update({"ID": ident, "Версия плагина": __version__, "Дата": day.isoformat(),
                   STATUS: PENDING})
    write_text_atomic(path, _render(ident, values))
    return path


def _parse(path: Path) -> dict[str, str]:
    got: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _FIELD.match(line)
        if m:
            got.setdefault(m.group(1), m.group(2).strip())
    missing = [name for name in ("ID", "Компонент", STATUS) if not got.get(name)]
    if missing:
        raise SrError(f"improve: в файле предложения нет полей {', '.join(missing)} ({path})", 1)
    if got["ID"] != path.stem:
        raise SrError(f"improve: ID в файле ({got['ID']}) не совпадает с именем файла ({path})", 1)
    return got


def list_items(root: Path) -> list[dict]:
    """Предложения вне архива: `{"id","status","component","path"}` по имени файла."""
    folder = _folder(root)
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob("imp-*.md")):
        if not _ID.fullmatch(path.stem):
            raise SrError(f"improve: имя файла не подходит под ID {ID_RE} ({path})", 1)
        got = _parse(path)
        out.append({"id": got["ID"], "status": got[STATUS], "component": got["Компонент"],
                    "path": str(path)})
    return out


def _set_status(path: Path, status: str) -> str:
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r"(?m)^- \*\*Статус:\*\* .*$", f"- **{STATUS}:** {status}", text, count=1)
    if n != 1:
        raise SrError(f"improve: в файле предложения нет строки статуса ({path})", 1)
    return new


# ── пакет ────────────────────────────────────────────────────────────────────


def pack(root: Path, out: Path | None = None) -> tuple[Path, str]:
    """Пакет всех `pending` одним markdown-файлом; (путь, текст). Нет `pending` или утечка — код 1,
    файл не пишется."""
    pending = [item for item in list_items(root) if item["status"] == PENDING]
    if not pending:
        raise SrError(f"improve pack: нет pending-предложений в {_folder(root)}", 1)
    parts = [f"# retropair {__version__}: предложений {len(pending)}", ""]
    for item in pending:
        body = Path(item["path"]).read_text(encoding="utf-8").rstrip("\n")
        parts += ["#" + body if body.startswith("# ") else body, ""]
    text = "\n".join(parts)
    bad = leaks(text, private_words(root))
    if bad:
        raise SrError("improve pack: в предложениях личные данные, пакет не собран — поправьте "
                      "файлы в " + str(_folder(root)) + ": " + "; ".join(bad), 1)
    if out is None:
        while True:
            out = _folder(root) / f"pack-{_dt.date.today():%Y%m%d}-{secrets.token_hex(2)}.md"
            if not out.exists():
                break
    out = Path(out)
    write_text_atomic(out, text)
    return out, text


def repo_slug(repository) -> str:
    """`owner/repo` из `repository` манифеста (`https://github.com/o/r[.git]`); иное — код 1."""
    m = _REPO.match(repository) if isinstance(repository, str) else None
    if not m:
        raise SrError(f"improve: repository в plugin.json не ссылка на GitHub вида "
                      f"https://github.com/<owner>/<repo> (получено {repository!r})", 1)
    return f"{m.group(1)}/{m.group(2)}"


def _manifest_repository():
    path = plugin_root() / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("repository")
    except (OSError, json.JSONDecodeError, AttributeError) as err:
        raise SrError(f"improve: не читается plugin.json ({path}): {err}", 1) from err


def gh_command(pack_path: Path, count: int, version: str, repository=None) -> str:
    """Строка `gh issue create …` для отправки пакета пользователем; сама ничего не запускает."""
    repo = repo_slug(_manifest_repository() if repository is None else repository)
    title = shlex.quote(f"retropair {version}: предложений {count}")
    return (f"gh issue create --repo {repo} --label proposal "
            f"--title {title} --body-file {shlex.quote(str(pack_path))}")


# ── статусы ──────────────────────────────────────────────────────────────────


def mark_sent(root: Path, ids: list[str]) -> list[str]:
    """`pending` → `sent`; вернёт изменённые ID. Кривой ID — код 2, нет файла — код 1 (до записи)."""
    folder = _folder(root)
    paths = []
    for ident in ids:
        if not _ID.fullmatch(ident):
            raise SrError(f"improve sent: не ID предложения (ожидается {ID_RE}): {ident}", 2)
        path = folder / f"{ident}.md"
        if not path.is_file():
            raise SrError(f"improve sent: нет предложения {ident} в {folder} "
                          f"(внедрённые лежат в archive/)", 1)
        paths.append(path)
    changed = []
    for path in paths:
        if _parse(path)[STATUS] == PENDING:
            write_text_atomic(path, _set_status(path, SENT))
            changed.append(path.stem)
    return changed


def implemented_ids(changelog_text: str) -> dict[str, str]:
    """ID из строк `внедрено: …` → версия ближайшего заголовка `## [X.Y.Z]` над строкой.
    Строки под `[Unreleased]` и упоминания без `внедрено:` не в счёт."""
    out: dict[str, str] = {}
    version = None
    for line in changelog_text.splitlines():
        if line.startswith("## "):
            m = _VERSION_HEAD.match(line)
            version = m.group(1) if m else None
            continue
        done = _DONE.search(line)
        if done and version:
            for ident in _ID.findall(line[done.end():]):
                out.setdefault(ident, version)
    return out


def sync(root: Path, changelog: Path) -> list[str]:
    """Внедрённые по CHANGELOG предложения → `implemented (X.Y.Z)` и перенос в `archive/`."""
    changelog = Path(changelog)
    if not changelog.is_file():
        raise SrError(f"improve sync: нет файла CHANGELOG ({changelog})", 2)
    done = implemented_ids(changelog.read_text(encoding="utf-8"))
    archive = _folder(root) / "archive"
    moved = []
    for item in list_items(root):
        version = done.get(item["id"])
        if version is None:
            continue
        path = Path(item["path"])
        write_text_atomic(archive / path.name, _set_status(path, IMPLEMENTED.format(version=version)))
        path.unlink()
        moved.append(item["id"])
    return moved


def doctor_lines(root: Path | None) -> list[tuple[str, object]]:
    """`improvements_pending` — число `pending`; вне workspace — ничего."""
    if root is None:
        return []
    return [(DOCTOR_KEY, sum(1 for item in list_items(root) if item["status"] == PENDING))]
