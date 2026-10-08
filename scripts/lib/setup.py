"""Онбординг: ответы setup → workspace `.retropair/`, текущее состояние в формате ответов.

`apply` сначала проверяет ответы (`validate_answers`, код 2 с именем поля), собирает новый конфиг
в памяти и прогоняет схему, читает `~/.claude/settings.json`, если его надо менять, и только потом
пишет. Новый workspace создаётся командой `init` (её код не копируется), затем в конфиг ложатся
группы и ответы, затем карточки людей. Проектные агенты пишутся, если переданы `models` или
какого-то файла агента нет; правленый вручную агент — совет, не ошибка. Регистрация в реестре —
последней. На повторе меняются
только переданные ключи; существующие карточки не перезаписываются.

`template` пишется и в `config.template`, и в `feedback.template` файла `people/_default.yaml` (по
нему строится текст); `cleanup_period_days.write` срок не понижает; `settings.json`, `.gitignore` и
агенты-симлинки пишутся в цель ссылки.

`state` отдаёт текущие значения в том же формате (вывод принимается `apply` без изменений) и
находки окружения: SpecStory, папка JSONL Claude Code, `cleanupPeriodDays`.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import re
import shutil
import subprocess
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from lib import agents, cmd_init, registry, yamlmini
from lib.cmd_doctor import specstory_version_line
from lib.config import check_config, default_config, load_config, save_config
from lib.i18n import t
from lib.people import DEFAULT_CARD, add_person, card_path, check_id, list_people, quote_name
from lib.schema import load_schema
from lib.sources import REQUIRED, claude_project_dir
from lib.version import plugin_root
from lib.workspace import (SrError, portable_path, read_json, write_json_atomic,
                           write_text_atomic, ws_paths)

ANSWER_KEYS = frozenset({
    "mode", "title", "lang", "path", "people", "sources", "hook", "models", "show_scores",
    "period", "template", "cleanup_period_days", "gitignore",
})
GITIGNORE_LINES = (".retropair/logs/", ".retropair/runs/pending/", ".retropair/reports/",
                   ".retropair/collect.log", ".retropair/collect.lock")
ROLES = ("collector", "judge", "critic")
PERSON_KEYS = frozenset({"id", "name", "group", "sources"})
SOURCE_KEYS = frozenset({"type", "path", "project", "url", "branch"})
PERIOD_KEYS = frozenset({"mode", "windows"})
SETTINGS_LABEL = "~/.claude/settings.json"
SPECSTORY_TIMEOUT = 5
GIT_TIMEOUT = 10
I18N = ("cli", "setup")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}(?:\Z|T)")
# строка `template:` внутри блока `feedback:` файла `_default.yaml`
_FEEDBACK_HEAD = re.compile(r"^feedback:\s*(?:#.*)?$")
_TEMPLATE_LINE = re.compile(r"^(\s+template:[ \t]*)(\"[^\"]*\"|'[^']*'|[^\s#]*)(.*)$")


# ── корень проекта и пути ────────────────────────────────────────────────────


def project_root(cwd: Path) -> Path:
    """Корень git-репозитория от `cwd`; не git (или git не установлен) → сам `cwd`."""
    cwd = Path(cwd).resolve()
    try:
        res = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(cwd),
                             capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError:
        return cwd
    except subprocess.TimeoutExpired as err:
        raise SrError(
            f"setup: git rev-parse --show-toplevel не ответил за {GIT_TIMEOUT} с (папка: {cwd})", 1
        ) from err
    top = res.stdout.strip()
    if res.returncode != 0 or not top:
        return cwd
    return Path(top).resolve()


def claude_settings_path(home: Path) -> Path:
    return Path(home) / ".claude" / "settings.json"


def _target_root(path, cwd: Path) -> Path:
    if path is None:
        return project_root(cwd)
    given = Path(path).expanduser()
    if not given.is_absolute():
        given = Path(cwd) / given
    if not given.is_dir():
        raise SrError(f"setup: path — папка проекта не найдена (path: {path})", 2)
    return given.resolve()


# ── проверка ответов ─────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _config_schema() -> dict:
    return load_schema("config")


def _config_enum(*keys: str) -> list[str]:
    node = _config_schema()
    for key in keys:
        node = node[key]
    return list(node["enum"])


def _bad(field: str, why: str, value) -> SrError:
    return SrError(f"setup: ответ {field}: {why} (получено: {value!r})", 2)


def _need_bool(answers: dict, key: str) -> None:
    if key in answers and not isinstance(answers[key], bool):
        raise _bad(key, "нужно true или false", answers[key])


def _need_text(value, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise _bad(field, "нужна непустая строка", value)


def _need_enum(value, field: str, allowed: list[str]) -> None:
    if value not in allowed:
        raise _bad(field, f"допустимо {' | '.join(allowed)}", value)


def _check_card_text(value, field: str) -> None:
    _need_text(value, field)
    try:
        quote_name(value)
    except SrError as err:
        raise _bad(field, f"не записать в карточку: {err.message}", value) from err


def _check_sources(items, field: str) -> None:
    if not isinstance(items, list):
        raise _bad(field, "нужен список источников", items)
    types = _config_enum("properties", "sources", "items", "properties", "type")
    for i, src in enumerate(items):
        where = f"{field}[{i}]"
        if not isinstance(src, dict):
            raise _bad(where, "источник должен быть объектом", src)
        _need_enum(src.get("type"), f"{where}.type", types)
        if "person" in src:
            raise _bad(f"{where}.person", "владелец задаётся в people[].sources, не полем person",
                       src["person"])
        unknown = sorted(set(src) - SOURCE_KEYS)
        if unknown:
            raise _bad(where, f"неизвестные поля {', '.join(unknown)}; допустимы "
                              f"{', '.join(sorted(SOURCE_KEYS))}", src)
        need = REQUIRED.get(src["type"])
        if need is not None:
            _need_text(src.get(need), f"{where}.{need}")


def _check_people(people) -> None:
    if not isinstance(people, list):
        raise _bad("people", "нужен список людей", people)
    seen: set[str] = set()
    for i, person in enumerate(people):
        where = f"people[{i}]"
        if not isinstance(person, dict):
            raise _bad(where, "человек должен быть объектом", person)
        unknown = sorted(set(person) - PERSON_KEYS)
        if unknown:
            raise _bad(where, f"неизвестные поля {', '.join(unknown)}; допустимы "
                              f"{', '.join(sorted(PERSON_KEYS))}", person)
        pid = person.get("id")
        try:
            check_id(pid)
        except SrError as err:
            raise _bad(f"{where}.id", err.message, pid) from err
        if pid in seen:
            raise _bad(f"{where}.id", "id повторяется в people", pid)
        seen.add(pid)
        if "name" in person:
            _check_card_text(person["name"], f"{where}.name")
        if "group" in person:
            _check_card_text(person["group"], f"{where}.group")
        if "sources" in person:
            _check_sources(person["sources"], f"{where}.sources")


def _check_models(models) -> None:
    if not isinstance(models, dict):
        raise _bad("models", "нужен объект ролей", models)
    for role, value in models.items():
        field = f"models.{role}"
        if role not in ROLES:
            raise _bad(field, f"неизвестная роль; допустимы {', '.join(ROLES)}", role)
        if not isinstance(value, dict) or not value:
            raise _bad(field, "нужен объект {model, effort}", value)
        unknown = sorted(set(value) - {"model", "effort"})
        if unknown:
            raise _bad(field, f"неизвестные поля {', '.join(unknown)}; допустимы model, effort",
                       value)
        role_schema = ("properties", "models", "properties", role, "properties")
        for key in ("model", "effort"):
            if key in value:
                _need_enum(value[key], f"{field}.{key}", _config_enum(*role_schema, key))


def _check_period(period) -> None:
    if not isinstance(period, dict):
        raise _bad("period", "нужен объект {mode, windows?}", period)
    unknown = sorted(set(period) - PERIOD_KEYS)
    if unknown:
        raise _bad("period", f"неизвестные поля {', '.join(unknown)}; допустимы mode, windows",
                   period)
    _need_enum(period.get("mode"), "period.mode", _config_enum("properties", "period",
                                                               "properties", "mode"))
    windows = period.get("windows")
    if windows is None:
        return
    if not isinstance(windows, list):
        raise _bad("period.windows", "нужен список окон", windows)
    labels: set[str] = set()
    for i, win in enumerate(windows):
        where = f"period.windows[{i}]"
        if not isinstance(win, dict):
            raise _bad(where, "окно должно быть объектом {label, from?, to?}", win)
        _need_text(win.get("label"), f"{where}.label")
        if win["label"] in labels:
            raise _bad(f"{where}.label", "метка окна повторяется", win["label"])
        labels.add(win["label"])
        for key in ("from", "to"):
            if win.get(key) is not None:
                _check_day(win[key], f"{where}.{key}")
        start, end = win.get("from"), win.get("to")
        if start is not None and end is not None and start[:10] > end[:10]:
            raise _bad(where, "from позже to", win)


def _check_day(value, field: str) -> None:
    """Граница окна: `ГГГГ-ММ-ДД` или ISO-время `ГГГГ-ММ-ДДT…` (период сравнивает первые 10 символов)."""
    ok = isinstance(value, str) and bool(_DAY.match(value))
    if ok:
        try:
            if len(value) == 10:
                date.fromisoformat(value)
            else:
                datetime.fromisoformat(value)
        except ValueError:
            ok = False
    if not ok:
        raise _bad(field, "нужна дата ГГГГ-ММ-ДД (или ISO-время ГГГГ-ММ-ДДTчч:мм) либо null", value)


def _check_template(value) -> None:
    _need_text(value, "template")
    if "/" in value or "\\" in value or ".." in value:
        raise _bad("template", "нужно default, workspace или имя файла в .retropair/templates/ "
                               "без разделителей пути и ..", value)


def _check_cleanup(value) -> None:
    field = "cleanup_period_days"
    if not isinstance(value, dict) or set(value) != {"value", "write"}:
        raise _bad(field, 'нужен объект {"value": <дней>, "write": true|false}', value)
    days = value["value"]
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise _bad(f"{field}.value", "нужно целое число дней ≥ 1", days)
    if not isinstance(value["write"], bool):
        raise _bad(f"{field}.write", "нужно true или false", value["write"])


def validate_answers(answers: dict) -> None:
    """Ответы до любой записи: неизвестный ключ, тип, enum, id — `SrError(2)` с именем поля."""
    if not isinstance(answers, dict):
        raise SrError(f"setup: ответы должны быть JSON-объектом (получен {type(answers).__name__})", 2)
    unknown = sorted(set(answers) - ANSWER_KEYS)
    if unknown:
        raise SrError(
            f"setup: неизвестные ключи ответов: {', '.join(unknown)} "
            f"(допустимы: {', '.join(sorted(ANSWER_KEYS))})",
            2,
        )
    if "mode" in answers:
        _need_enum(answers["mode"], "mode", list(cmd_init.MODES))
    if "lang" in answers:
        _need_enum(answers["lang"], "lang", list(cmd_init.LANGS))
    if "title" in answers:
        _need_text(answers["title"], "title")
    if "path" in answers:
        _need_text(answers["path"], "path")
    for key in ("hook", "show_scores", "gitignore"):
        _need_bool(answers, key)
    if "people" in answers:
        _check_people(answers["people"])
    if "sources" in answers:
        _check_sources(answers["sources"], "sources")
    if "models" in answers:
        _check_models(answers["models"])
    if "period" in answers:
        _check_period(answers["period"])
    if "template" in answers:
        _check_template(answers["template"])
    if "cleanup_period_days" in answers:
        _check_cleanup(answers["cleanup_period_days"])


# ── сборка конфига в памяти ──────────────────────────────────────────────────


def _src_key(src: dict) -> tuple:
    return (src.get("type"), src.get("path"), src.get("project"), src.get("url"), src.get("person"))


def _merged_config(base: dict, answers: dict, fresh: bool) -> dict:
    cfg = copy.deepcopy(base)
    for key in ("mode", "lang", "show_scores", "template"):
        if key in answers:
            cfg[key] = answers[key]
    if "title" in answers:
        cfg["workspace"]["title"] = answers["title"]
    if "hook" in answers:
        cfg["hook"]["enabled"] = answers["hook"]
    if "period" in answers:
        cfg["period"] = copy.deepcopy(answers["period"])
    for role, value in answers.get("models", {}).items():
        cfg["models"][role].update(value)
    if "sources" in answers:
        kept = [src for src in cfg["sources"] if "person" in src]
        cfg["sources"] = copy.deepcopy(answers["sources"]) + kept
    elif fresh and cfg["mode"] != "self":
        # team/course: источники шаблона без владельца при двух карточках ломают collect
        cfg["sources"] = []

    people = answers.get("people", [])
    if fresh:
        groups = ["default"] if cfg["mode"] == "self" else []
    else:
        groups = cfg["workspace"]["groups"]
    for person in people:
        group = person.get("group")
        if group is not None and group not in groups:
            groups.append(group)
    cfg["workspace"]["groups"] = groups or ["default"]

    for person in people:
        for src in person.get("sources", []):
            item = {**copy.deepcopy(src), "person": person["id"]}
            if not any(_src_key(have) == _src_key(item) for have in cfg["sources"]):
                cfg["sources"].append(item)
    return cfg


def _diff(old: dict, new: dict) -> list[str]:
    """Изменённые поля конфига: `config.<ключ>`, у объектов — на уровень глубже."""
    out = []
    for key in list(old) + [k for k in new if k not in old]:
        a, b = old.get(key), new.get(key)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            subs = list(a) + [k for k in b if k not in a]
            out.extend(f"config.{key}.{sub}" for sub in subs if a.get(sub) != b.get(sub))
        else:
            out.append(f"config.{key}")
    return out


# ── ~/.claude/settings.json ──────────────────────────────────────────────────


def _read_settings(path: Path) -> dict | None:
    if not path.is_file():
        return None
    data = read_json(path)  # битый JSON — SrError(1) с путём
    if not isinstance(data, dict):
        raise SrError(f"setup: settings.json не объект: получен {type(data).__name__} ({path})", 1)
    return data


def _current_cleanup(data: dict | None, path: Path) -> int | None:
    if data is None or "cleanupPeriodDays" not in data:
        return None
    value = data["cleanupPeriodDays"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SrError(f"setup: cleanupPeriodDays в settings.json не целое число "
                      f"(значение: {value!r}; {path})", 1)
    return value


# ── применение ───────────────────────────────────────────────────────────────


def _rel(path: Path, root: Path) -> str:
    return portable_path(path, root)


def _create_workspace(root: Path, cfg: dict) -> None:
    """Создание workspace командой `init`, её вывод глушится (отчёт печатает `setup-apply`)."""
    args = argparse.Namespace(mode=cfg["mode"], title=cfg["workspace"]["title"], lang=cfg["lang"],
                              path=str(root), workspace=None)
    with contextlib.redirect_stdout(io.StringIO()):
        cmd_init.handle(args)


def _read_text(path: Path) -> str:
    """Текст файла или пустая строка, если файла нет; не UTF-8 — `SrError(1)` с путём."""
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as err:
        raise SrError(f"setup: файл не в UTF-8: {err.reason} ({path})", 1) from err


def _apply_gitignore(root: Path, text: str, report: dict) -> None:
    path = root / ".gitignore"
    have = {line.strip() for line in text.splitlines()}
    missing = [line for line in GITIGNORE_LINES if line not in have]
    if not missing:
        report["unchanged"].append(".gitignore")
        return
    if text and not text.endswith("\n"):
        text += "\n"
    existed = path.is_file()
    # симлинк (dotfiles) остаётся симлинком: пишем в его цель
    write_text_atomic(path.resolve(), text + "\n".join(missing) + "\n")
    report["changed" if existed else "created"].append(".gitignore")


def _apply_cleanup(answer: dict, path: Path, data: dict | None, lang: str, report: dict) -> None:
    value = answer["value"]
    current = _current_cleanup(data, path)
    if not answer["write"]:
        shown = str(current) if current is not None else t(I18N, "not_set", lang)
        report["advice"].append(t(I18N, "advice_cleanup", lang).format(value=value, current=shown))
        return
    if current is not None and current >= value:
        # понижение срока удалило бы логи старше value: не трогаем
        report["unchanged"].append(SETTINGS_LABEL)
        if current > value:
            report["advice"].append(
                t(I18N, "cleanup_kept", lang).format(current=current, value=value))
        return
    merged = dict(data or {})
    merged["cleanupPeriodDays"] = value
    write_json_atomic(path.resolve(), merged)
    report["changed" if data is not None else "created"].append(SETTINGS_LABEL)


# ── шаблон текста: config.template + feedback.template в _default.yaml ────────


def _check_template_file(root: Path, value: str) -> None:
    if value in ("default", "workspace"):
        return
    path = ws_paths(root)["templates"] / value
    if not path.is_file():
        raise SrError(f"setup: ответ template: нет файла шаблона в .retropair/templates/ "
                      f"(template: {value!r}; {path})", 2)


def set_default_template(text: str, value: str, path: Path) -> str:
    """`_default.yaml` с новым значением строки `template:` блока `feedback:`; остальное байт в
    байт. Нет такой строки — `SrError(1)` с путём."""
    lines = text.splitlines(keepends=True)
    inside = False
    hits = []
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        if body and not body[0].isspace() and not body.startswith("#"):
            inside = bool(_FEEDBACK_HEAD.match(body))
            continue
        if inside and _TEMPLATE_LINE.match(body):
            hits.append(i)
    if len(hits) != 1:
        raise SrError(f"setup: в файле дефолтов карточек нет строки template: в блоке feedback: "
                      f"— верните её (  template: default) ({path})", 1)
    i = hits[0]
    body = lines[i].rstrip("\r\n")
    ending = lines[i][len(body):]
    m = _TEMPLATE_LINE.match(body)
    value_text = value if re.fullmatch(r"[A-Za-z0-9._-]+", value) else quote_name(value)
    lines[i] = f"{m.group(1)}{value_text}{m.group(3)}{ending}"
    return "".join(lines)


def _own_templates(root: Path, value: str) -> list[str]:
    """id карточек со своим `feedback.template`, отличным от `value` (их дефолт не касается)."""
    out = []
    folder = ws_paths(root)["people"]
    for card in list_people(root):
        raw = yamlmini.load(_read_text(folder / f"{card['id']}.yaml"))
        own = (raw.get("feedback") or {}).get("template") if isinstance(raw, dict) else None
        if own is not None and own != value:
            out.append(card["id"])
    return out


def _apply_template(root: Path, value: str, lang: str, report: dict) -> None:
    paths = ws_paths(root)
    if value == "workspace":
        target = paths["templates"] / "feedback.md"
        if target.exists():
            report["unchanged"].append(_rel(target, root))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plugin_root() / "templates" / f"feedback.{lang}.md", target)
            report["created"].append(_rel(target, root))
    card = paths["people"] / f"{DEFAULT_CARD}.yaml"
    old = _read_text(card)
    new = set_default_template(old, value, card)
    if new != old:
        write_text_atomic(card.resolve(), new)
        report["changed"].append(_rel(card, root))
    own = _own_templates(root, value)
    if own:
        report["advice"].append(t(I18N, "template_own", lang).format(ids=", ".join(own)))


def _apply_agents(root: Path, cfg: dict, lang: str, report: dict) -> None:
    res = agents.write(root, cfg)
    for key, where in (("created", "created"), ("updated", "changed"), ("unchanged", "unchanged")):
        report[where] += [_rel(agents.target(root, role), root) for role in res[key]]
    for role in res["skipped_edited"]:
        report["advice"].append(
            t(I18N, "agent_edited", lang).format(path=_rel(agents.target(root, role), root)))


def _advise_ownerless(root: Path, cfg: dict, answers: dict, fresh: bool, lang: str,
                      report: dict) -> None:
    """team/course: подсказка про источники без поля person (collect требует его при 2+ карточках)."""
    if cfg["mode"] == "self":
        return
    if fresh and "sources" not in answers:
        report["advice"].append(t(I18N, "sources_per_person", lang).format(mode=cfg["mode"]))
    cards = [card for card in list_people(root) if card["id"] != "_default"]
    if len(cards) < 2:
        return
    for src in cfg["sources"]:
        if "person" not in src:
            where = src.get("path") or src.get("project") or src.get("url") or src.get("type")
            report["advice"].append(t(I18N, "ownerless_source", lang).format(src=where))


def apply(answers: dict, cwd: Path, home: Path) -> dict:
    """Применить ответы онбординга; отчёт `{root, created, changed, unchanged, advice, hook_check}`."""
    validate_answers(answers)
    root = _target_root(answers.get("path"), cwd)
    paths = ws_paths(root)
    fresh = not paths["config"].is_file()
    if fresh:
        if "mode" not in answers:
            raise SrError(f"setup: первый запуск — нужен ответ mode (self | team | course) "
                          f"(папка: {root})", 2)
        if paths["state_dir"].resolve() == registry.sr_home().resolve():
            raise SrError(f"setup: домашняя папка не может быть workspace — укажите папку проекта "
                          f"(папка: {root})", 2)
        title = answers.get("title", root.name)
        old = None
        base = default_config(ws_id=registry.preview_id(root, title), title=title,
                              mode=answers["mode"], lang=answers.get("lang", "ru"))
    else:
        old = load_config(root)
        base = old
    cfg = _merged_config(base, answers, fresh)
    check_config(cfg, paths["config"])
    lang = cfg["lang"]
    if cfg["mode"] == "self":
        extra = [p["id"] for p in answers.get("people", []) if p["id"] != cmd_init.SELF_ID]
        if extra:
            raise _bad("people", f"в режиме self только карточка {cmd_init.SELF_ID}; лишние: "
                                 f"{', '.join(extra)}", answers["people"])

    template = answers.get("template")
    if template is not None:
        _check_template_file(root, template)
        default_card = (paths["people"] / f"{DEFAULT_CARD}.yaml" if not fresh
                        else plugin_root() / "templates" / f"{DEFAULT_CARD}.yaml")
        set_default_template(_read_text(default_card), template, default_card)
    gitignore = _read_text(root / ".gitignore") if answers.get("gitignore") is True else None

    settings_path = claude_settings_path(home)
    cleanup = answers.get("cleanup_period_days")
    settings = _read_settings(settings_path) if cleanup is not None else None

    report = {"root": str(root), "created": [], "changed": [], "unchanged": [], "advice": [],
              "hook_check": None}

    # запись: workspace → конфиг → карточки → шаблон → .gitignore → settings → агенты → реестр
    if fresh:
        _create_workspace(root, cfg)
        cfg["workspace"]["id"] = load_config(root)["workspace"]["id"]
        save_config(root, cfg)
        report["created"] += [_rel(paths["config"], root),
                              _rel(paths["people"] / "_default.yaml", root),
                              _rel(paths["state"], root)]
        if cfg["mode"] == "self":
            report["created"].append(cmd_init.SELF_ID)
    else:
        changed = _diff(old, cfg)
        if changed:
            save_config(root, cfg)
            report["changed"] += changed
        if old["mode"] != cfg["mode"]:
            report["advice"].append(
                t(I18N, "mode_changed", lang).format(old=old["mode"], new=cfg["mode"]))

    for person in answers.get("people", []):
        pid = person["id"]
        if card_path(root, pid).exists():
            report["unchanged"].append(pid)
            continue
        add_person(root, pid, person.get("name", pid),
                   person.get("group", cfg["workspace"]["groups"][0]))
        report["created"].append(pid)

    _advise_ownerless(root, cfg, answers, fresh, lang, report)

    if template is not None:
        _apply_template(root, template, lang, report)

    if gitignore is not None:
        _apply_gitignore(root, gitignore, report)
    elif fresh and "gitignore" not in answers and not gitignore_complete(root):
        # строки пишутся только по согласию; без ответа — подсказка, как дописать
        report["advice"].append(t(I18N, "gitignore_skipped", lang))

    if cleanup is not None:
        _apply_cleanup(cleanup, settings_path, settings, lang, report)

    if cfg["hook"]["enabled"] is True and answers.get("hook") is True:
        report["hook_check"] = t(I18N, "hook_check", lang)

    if "models" in answers or any(not agents.target(root, role).is_file() for role in agents.ROLES):
        _apply_agents(root, cfg, lang, report)

    entry = registry.register(root, cfg["workspace"]["title"], cfg["mode"], cfg["workspace"]["groups"])
    if entry["id"] != cfg["workspace"]["id"]:
        # запись реестра по этому пути уже была с другим id — источник правды реестр
        report["changed"].append("config.workspace.id")
        cfg = load_config(root)
        cfg["workspace"]["id"] = entry["id"]
        save_config(root, cfg)
    return report


# ── состояние ────────────────────────────────────────────────────────────────


def _specstory_version() -> str | None:
    try:
        res = subprocess.run(["specstory", "--version"], capture_output=True, text=True,
                             timeout=SPECSTORY_TIMEOUT)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return f"нет ответа за {SPECSTORY_TIMEOUT} с"
    if res.returncode != 0:
        return f"ошибка (код {res.returncode})"
    return specstory_version_line(res.stdout or res.stderr)


def gitignore_complete(root: Path) -> bool:
    """Все строки `GITIGNORE_LINES` есть в `<root>/.gitignore`."""
    have = {line.strip() for line in _read_text(Path(root) / ".gitignore").splitlines()}
    return all(line in have for line in GITIGNORE_LINES)


def _workspace_answers(root: Path) -> dict:
    cfg = load_config(root)
    own: dict[str, list[dict]] = {}
    for src in cfg["sources"]:
        if "person" in src:
            item = {k: v for k, v in src.items() if k != "person"}
            own.setdefault(src["person"], []).append(item)
    return {
        "mode": cfg["mode"],
        "title": cfg["workspace"]["title"],
        "lang": cfg["lang"],
        "path": str(root),
        "people": [{"id": card["id"], "name": card["name"], "group": card["group"],
                    "sources": own.get(card["id"], [])} for card in list_people(root)],
        "sources": [copy.deepcopy(src) for src in cfg["sources"] if "person" not in src],
        "hook": cfg["hook"]["enabled"],
        "models": copy.deepcopy(cfg["models"]),
        "show_scores": cfg["show_scores"],
        "period": copy.deepcopy(cfg["period"]),
        "template": cfg["template"],
        "gitignore": gitignore_complete(root),
    }


def state(cwd: Path, home: Path, path: str | None = None) -> dict:
    """`{"workspace": <ответы>|None, "found": {...}}`; вне workspace — `workspace: None`."""
    root = _target_root(path, cwd)
    workspace = _workspace_answers(root) if ws_paths(root)["config"].is_file() else None
    jsonl_dir = claude_project_dir(root, "auto")
    has_dir = jsonl_dir.is_dir()
    settings_path = claude_settings_path(home)
    found = {
        "specstory_dir": (root / ".specstory" / "history").is_dir(),
        "claude_projects_dir": str(jsonl_dir) if has_dir else None,
        "jsonl_files": sum(1 for p in jsonl_dir.glob("*.jsonl") if p.is_file()) if has_dir else 0,
        "specstory_version": _specstory_version(),
        "cleanup_period_days": _current_cleanup(_read_settings(settings_path), settings_path),
    }
    return {"workspace": workspace, "found": found}
