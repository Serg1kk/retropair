"""Команда `collect`: источники → санитайзированные снапшоты → `logs-index.json` → сводка.

Порядок под блокировкой `.sessionretro/collect.lock`: `specstory sync` (если среди источников
есть `specstory`) → файлы источников → снапшоты `logs/<person>/<key>.md|.jsonl` и сайдкар
`.meta.json` → перечистка снапшотов со старым отпечатком санитайзера → пересчёт инвентаря →
`last_collect_at` в реестре → сводка.

Снапшоты:
- markdown-форматы — текст исходника через `sanitize_text` целиком; `claude-jsonl` — компактный
  JSONL (`cc_jsonl.compact_snapshot`, каждая строка через `sanitize_obj`);
- до разбора считается sha256 сырых байтов исходника (у `.jsonl` — плюс файлы сабагентов
  `<stem>/subagents/agent-*.jsonl` по имени, то же правило, что у `compact_snapshot`). Совпали
  `source_sha256` и `sanitizer_fp` в сайдкаре, снапшот есть — исходник не разбирается и не
  санитайзится (ключ и имя берутся из сайдкара по `source_path`);
- отпечаток санитайзера (`sanitize.fingerprint`) сменился — снапшот перечищается: из исходника,
  а если исходника уже нет, то сам снапшот (санитайзер идемпотентен на метках);
- новый снапшот с меньшим числом ходов, чем лежащий из того же исходника, не пишется: строка в
  `warnings` с рецептом `--replace <имя>` (флаг снимает защиту для одного снапшота);
- под одним ключом другая сессия (лежащий снапшот не префикс нового по ходам и взят из другого
  исходника) — снапшот не перезаписывается, новый получает ключ `<key>#<sha8 пути исходника>`,
  строка в `warnings`. Разные исходники одного ключа за прогон разводятся так же; копии одной
  сессии (одна — префикс другой) — побеждает больше ходов, больше байтов, путь; строка
  «дубль ключа …».

Пути в `.meta.json`, `logs-index.json` и сводке санитайзера — переносимые (`workspace.portable_path`):
внутри проекта относительные, внутри HOME `~/…`.

Исходники не меняются никогда. Исходник, который не разобрался или дал 0 ходов, — строка в
`warnings`, остальные собираются.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from lib import cc_jsonl, inventory, lock, normalize, registry, sanitize, sources, specstory
from lib.config import load_config
from lib.people import list_people
from lib.workspace import (SrError, find_workspace, portable_path, portable_text, write_text_atomic,
                           ws_paths)

NO_WORKSPACE = "нет workspace"
NO_INDEX = "нет"
LOCK_TIMEOUT = 10          # ручной запуск: ждать соседний сбор недолго, потом ошибка
LOCK_TIMEOUT_QUIET = 120   # хук (--quiet): соседние терминалы закрываются почти одновременно
SR_CMD = "sr.py collect"


def register(subparsers) -> None:
    p = subparsers.add_parser("collect", help="собрать логи в санитайзированные снапшоты и инвентарь")
    p.add_argument("--quiet", action="store_true", help="при успехе ничего не печатать (для хука)")
    p.add_argument("--provider", action="append", metavar="P",
                   help="провайдер specstory sync (повторяемый); иначе hook.providers конфига")
    p.add_argument("--replace", action="append", metavar="ИМЯ",
                   help="имя снапшота в logs/<id>/, который перезаписать, даже если новый короче "
                        "или не похож на лежащий (повторяемый)")
    p.add_argument("--json", action="store_true", help="сводка одним JSON-объектом")
    p.set_defaults(func=handle)


# ── исходники ────────────────────────────────────────────────────────────────


def source_sha256(path: Path) -> str:
    """sha256 сырых байтов исходника; у `.jsonl` — плюс файлы сабагентов по отсортированным именам."""
    path = Path(path)
    sha = hashlib.sha256(path.read_bytes())
    if path.suffix == ".jsonl":
        sub_dir = cc_jsonl.subagents_dir(path)
        if sub_dir.is_dir():
            for sub in sorted(sub_dir.glob("agent-*.jsonl")):
                sha.update(sub.read_bytes())
    return sha.hexdigest()


def _rel_for_key(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def _candidate(root: Path, sf: sources.SourceFile, sha: str, warnings: list[str]) -> dict | None:
    """Разбор исходника → кандидат в снапшот; не разобрался или 0 ходов — строка warnings."""
    try:
        doc = normalize.parse_file(sf.path)
    except Exception as err:  # noqa: BLE001 — один битый исходник не роняет сбор, но виден
        warnings.append(f"исходник не разобран, пропущен: {sf.path}: {type(err).__name__}: "
                        f"{getattr(err, 'message', err)}")
        return None
    if not doc["turns_total"]:
        warnings.append(f"исходник без ходов, пропущен: {sf.path} (формат {doc['format']})")
        return None
    key = inventory.session_key(doc, _rel_for_key(root, sf.path))
    return {"src": sf, "doc": doc, "key": key, "base_key": key, "sha": sha,
            "portable": portable_path(sf.path, root),
            "name": inventory.snapshot_name(key, doc["format"])}


def _suffix(c: dict) -> None:
    """Ключ `<key>#<sha8 пути исходника>`: стабилен, пока исходник лежит на том же месте."""
    digest = hashlib.sha256(c["portable"].encode("utf-8")).hexdigest()[:8]
    c["key"] = f"{c['base_key']}#{digest}"
    c["name"] = inventory.snapshot_name(c["key"], c["doc"]["format"])


def _pairs(turns: list[dict]) -> list[tuple]:
    return [(t.get("role"), t.get("text")) for t in turns]


def _is_prefix(short: list[tuple], long: list[tuple]) -> bool:
    return len(short) <= len(long) and long[:len(short)] == short


# ── сайдкар ──────────────────────────────────────────────────────────────────


class Metas:
    """Сайдкары `logs/<person>/.meta.json` за прогон: загрузка по требованию, поиск по `source_path`."""

    def __init__(self, root: Path) -> None:
        self.logs = ws_paths(root)["logs"]
        self.data: dict[str, dict] = {}

    def of(self, person: str) -> dict:
        if person not in self.data:
            self.data[person] = inventory.load_meta(self.logs / person)
        return self.data[person]

    def by_source(self, person: str, portable: str, absolute: str) -> str | None:
        """Имя снапшота, взятого из этого исходника (сайдкар старой версии мог хранить абсолютный путь)."""
        for name, entry in self.of(person).items():
            if isinstance(entry, dict) and entry.get("source_path") in (portable, absolute):
                return name
        return None

    def save(self) -> None:
        for person, meta in self.data.items():
            inventory.save_meta(self.logs / person, meta)


def _meta_entry(c: dict, hits: dict, fp: str) -> dict:
    doc, sf = c["doc"], c["src"]
    rep = sanitize.report(c["portable"], hits)
    mm = doc.get("mechanical_metrics") or {}
    return {
        "source": {"type": sf.source_type, "id": sf.source_id},
        "source_path": c["portable"],
        "source_sha256": c["sha"],
        "sanitizer_fp": fp,
        "sanitize": {"rules_hit": rep["rules_hit"], "renamed": rep["renamed"]},
        "key": c["key"],
        "session_id": doc.get("session_id"),
        "log_started_at": doc.get("started_at"),
        "log_ended_at": doc.get("ended_at"),
        "started_at_source": doc.get("started_at_source"),
        "unparsed_lines": mm.get("unparsed_lines"),
        "unknown_types": mm.get("unknown_types"),
    }


# ── прогон ───────────────────────────────────────────────────────────────────


class Run:
    """Состояние одного прогона сбора: санитайзер, сайдкары, предупреждения, отчёт санитайзера."""

    def __init__(self, root: Path, sanitizer: sanitize.Sanitizer, replace: list[str]) -> None:
        self.root = Path(root)
        self.sanitizer = sanitizer
        self.fp = sanitize.fingerprint(sanitizer)
        self.metas = Metas(self.root)
        self.warnings: list[str] = []
        self.sanitized: list[dict] = []
        self.replace = {Path(r).name for r in replace}
        self.replaced: set[str] = set()

    # ── пропуск без разбора (I5) ──────────────────────────────────────────────

    def unchanged(self, sf: sources.SourceFile, sha: str) -> bool:
        """Исходник не менялся, отпечаток санитайзера тот же, снапшот лежит — разбирать нечего."""
        portable = portable_path(sf.path, self.root)
        name = self.metas.by_source(sf.person_id, portable, str(sf.path))
        if name is None or name in self.replace:
            return False
        entry = self.metas.of(sf.person_id)[name]
        return (entry.get("source_sha256") == sha and entry.get("sanitizer_fp") == self.fp
                and entry.get("source_path") == portable
                and (self.metas.logs / sf.person_id / name).is_file())

    # ── ключи и победители ────────────────────────────────────────────────────

    def sticky(self, c: dict) -> None:
        """Исходник, которому раньше выдан суффиксный ключ, получает его снова."""
        name = self.metas.by_source(c["src"].person_id, c["portable"], str(c["src"].path))
        if name is None:
            return
        entry = self.metas.of(c["src"].person_id)[name]
        key = entry.get("key")
        if isinstance(key, str) and key.startswith(c["base_key"] + "#"):
            c["key"], c["name"] = key, name

    def pick_winners(self, cands: list[dict]) -> list[dict]:
        """Одно имя снапшота у человека: копии одной сессии → одна (больше ходов, байтов, путь);
        разные сессии → все, кроме держателя имени, получают суффиксный ключ."""
        groups: dict[tuple[str, str], list[dict]] = {}
        for c in cands:
            self.sticky(c)
            groups.setdefault((c["src"].person_id, c["name"]), []).append(c)
        winners = []
        for (person, name), items in groups.items():
            items.sort(key=lambda c: (-c["doc"]["turns_total"], -c["doc"]["bytes"], str(c["src"].path)))
            clusters: list[list[dict]] = []
            for c in items:
                turns = _pairs(c["doc"]["turns"])
                home = next((cl for cl in clusters if _is_prefix(turns, _pairs(cl[0]["doc"]["turns"]))), None)
                if home is None:
                    clusters.append([c])
                else:
                    home.append(c)
            for cl in clusters:
                if len(cl) > 1:
                    self.warnings.append(f"дубль ключа {cl[0]['key']}: "
                                         + ", ".join(str(c["src"].path) for c in cl)
                                         + f" — взят {cl[0]['src'].path}")
            if len(clusters) > 1:
                entry = self.metas.of(person).get(name)
                holder_path = entry.get("source_path") if isinstance(entry, dict) else None
                holder = next((cl for cl in clusters
                               if any(c["portable"] == holder_path for c in cl)), clusters[0])
                for cl in clusters:
                    if cl is holder:
                        continue
                    _suffix(cl[0])
                    self.warnings.append(
                        f"под ключом {cl[0]['base_key']} другая сессия: {cl[0]['src'].path} "
                        f"собрана отдельно в logs/{person}/{cl[0]['name']} "
                        f"(logs/{person}/{name} — из другого исходника)")
            winners += [cl[0] for cl in clusters]
        return winners

    # ── снапшот ───────────────────────────────────────────────────────────────

    def render(self, c: dict) -> tuple[str, dict]:
        path = c["src"].path
        if c["doc"]["format"] == cc_jsonl.FORMAT:
            text, hits, _sha = cc_jsonl.compact_snapshot(path, self.sanitizer.sanitize_obj)
            return text, hits
        raw = path.read_bytes()
        return self.sanitizer.sanitize_text(raw.decode("utf-8", errors="replace"))

    def snapshot(self, c: dict, redirected: bool = False) -> None:
        person = c["src"].person_id
        meta = self.metas.of(person)
        target = self.metas.logs / person / c["name"]
        old = meta.get(c["name"]) if isinstance(meta.get(c["name"]), dict) else None
        text, hits = self.render(c)
        replace = c["name"] in self.replace
        if replace:
            self.replaced.add(c["name"])
        if target.is_file() and not replace:
            old_text = target.read_text(encoding="utf-8", errors="replace")
            same_src = bool(old) and old.get("source_path") in (c["portable"], str(c["src"].path))
            new_t, old_t = _pairs(normalize.parse(text)["turns"]), _pairs(normalize.parse(old_text)["turns"])
            if same_src and len(new_t) < len(old_t):
                self.warnings.append(
                    f"снапшот logs/{person}/{c['name']} не перезаписан: в новом {len(new_t)} ходов, "
                    f"меньше, чем в лежащем ({len(old_t)}); исходник {c['src'].path}. Если исходник "
                    f"укоротили намеренно (чистка), повторите: {SR_CMD} --replace {c['name']}")
                return
            if not same_src:
                if len(new_t) > len(old_t) and _is_prefix(old_t, new_t):
                    pass                        # та же сессия, новый исходник полнее — берём его
                elif _is_prefix(new_t, old_t):
                    src = old.get("source_path") if old else "неизвестен"
                    self.warnings.append(
                        f"дубль ключа {c['key']}: {c['src'].path} — копия той же сессии, что "
                        f"logs/{person}/{c['name']} (исходник {src}); оставлен лежащий")
                    return
                elif redirected:
                    self.warnings.append(
                        f"снапшот logs/{person}/{c['name']} не перезаписан: в нём другая сессия, "
                        f"исходник {c['src'].path} пропущен (если так и задумано: "
                        f"{SR_CMD} --replace {c['name']})")
                    return
                else:
                    old_name = c["name"]
                    _suffix(c)
                    self.warnings.append(
                        f"под ключом {c['base_key']} другая сессия: {c['src'].path} собрана "
                        f"отдельно в logs/{person}/{c['name']} (logs/{person}/{old_name} "
                        f"из другого исходника не тронут)")
                    self.snapshot(c, redirected=True)
                    return
            if old_text != text:
                write_text_atomic(target, text)
        else:
            write_text_atomic(target, text)
        meta[c["name"]] = _meta_entry(c, hits, self.fp)
        rep = sanitize.report(c["portable"], hits)
        if rep["rules_hit"] or rep["renamed"]:
            self.sanitized.append(rep)

    # ── перечистка по отпечатку (I1) ──────────────────────────────────────────

    def resanitize_text(self, path: Path) -> tuple[str, dict]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix != ".jsonl":
            return self.sanitizer.sanitize_text(text)
        hits: dict[str, int] = {}
        out = []
        for line in text.split("\n"):
            if not line.strip():
                out.append(line)
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                clean, h = self.sanitizer.sanitize_obj(obj)
                line = cc_jsonl._dumps(clean)
            else:                           # строку снапшот писал не collect — чистим как текст
                line, h = self.sanitizer.sanitize_text(line)
            for rule, n in h.items():
                hits[rule] = hits.get(rule, 0) + n
            out.append(line)
        return "\n".join(out), hits

    def resanitize_stale(self) -> None:
        """Снапшоты, у которых отпечаток в сайдкаре не текущий (исходника нет, защита от усыхания
        или сайдкара нет вовсе), перечищаются на месте."""
        if not self.metas.logs.is_dir():
            return
        for person_dir in sorted(p for p in self.metas.logs.iterdir() if p.is_dir()):
            meta = self.metas.of(person_dir.name)
            for path in sorted(person_dir.iterdir()):
                if (not path.is_file() or path.name.startswith(".")
                        or path.suffix not in inventory.SNAPSHOT_SUFFIXES):
                    continue
                entry = meta.get(path.name)
                if isinstance(entry, dict) and entry.get("sanitizer_fp") == self.fp:
                    continue
                entry = dict(entry) if isinstance(entry, dict) else {}
                old_text = path.read_text(encoding="utf-8", errors="replace")
                text, hits = self.resanitize_text(path)
                if text != old_text:
                    write_text_atomic(path, text)
                entry["sanitizer_fp"] = self.fp
                prev = entry.get("sanitize") or {"rules_hit": {}, "renamed": 0}
                rep = sanitize.report(portable_path(path, self.root), hits)
                rules = dict(prev.get("rules_hit") or {})
                for rule, n in rep["rules_hit"].items():
                    rules[rule] = rules.get(rule, 0) + n
                entry["sanitize"] = {"rules_hit": rules, "renamed": (prev.get("renamed") or 0) + rep["renamed"]}
                meta[path.name] = entry
                if rep["rules_hit"] or rep["renamed"]:
                    self.sanitized.append(rep)


# ── оркестрация ──────────────────────────────────────────────────────────────


def _has_specstory(cfg: dict, people: list[dict]) -> bool:
    all_sources = list(cfg.get("sources") or [])
    for card in people:
        all_sources += card.get("sources") or []
    return any(isinstance(s, dict) and s.get("type") == "specstory" for s in all_sources)


def _sanitizer(cfg: dict, people: list[dict]) -> sanitize.Sanitizer:
    conf = cfg.get("sanitize") or {}
    names = [*sanitize.names_from_people(people), *(conf.get("names") or [])]
    return sanitize.Sanitizer(names=names, extra_patterns=conf.get("extra_patterns") or [],
                              local_users=sanitize.local_user_names())


def collect(root: Path, providers: list[str] | None, now: str | None = None,
            replace: list[str] | None = None, lock_timeout: float = LOCK_TIMEOUT) -> dict:
    """Полный прогон сбора; возвращает сводку (поля — в описании файла)."""
    root = Path(root)
    now = now or inventory.now_iso()
    cfg = load_config(root)
    inventory.validate_period(cfg)
    people = list_people(root)
    run = Run(root, _sanitizer(cfg, people), replace or [])
    sync_lines: list[str] = []
    paths = ws_paths(root)
    with lock.file_lock(paths["state_dir"] / "collect", timeout=lock_timeout):
        if _has_specstory(cfg, people):
            sync_lines = [specstory.sync(root, p) for p in specstory.providers(providers, cfg)]
        files, src_warnings, junk = sources.resolve(root, cfg, people)
        run.warnings += src_warnings
        order = {}
        for sf in files:
            order.setdefault(sf.source_id, len(order))
        files = sorted(files, key=lambda sf: (order[sf.source_id], str(sf.path)))

        cands = []
        for sf in files:
            try:
                sha = source_sha256(sf.path)
            except OSError as err:
                run.warnings.append(f"исходник не прочитан, пропущен: {sf.path}: {err}")
                continue
            if run.unchanged(sf, sha):
                continue
            c = _candidate(root, sf, sha, run.warnings)
            if c is not None:
                cands.append(c)
        for c in run.pick_winners(cands):
            run.snapshot(c)
        run.resanitize_stale()
        run.metas.save()
        for name in sorted(run.replace - run.replaced):
            run.warnings.append(f"--replace {name}: снапшот с таким именем в этом сборе не "
                                f"обновлялся (нет исходника или имя с ошибкой)")

        entry = registry.find_by_path(root)
        if entry is None:
            run.warnings.append(f"workspace не в реестре ~/.sessionretro/workspaces.json, "
                                f"last_collect_at не записан ({root})")
        warnings = [portable_text(w, root) for w in run.warnings]
        index = inventory.rebuild(root, cfg, now=now, warnings=warnings)
        if entry is not None:
            registry.update(entry["id"], last_collect_at=now)

    return {
        "counts": inventory.summary(index),
        "sanitized": run.sanitized,
        "orig": junk["orig"],
        "slug_named": junk["slug_named"],
        "context_report": junk["context_report"],
        "warnings": list(index.get("warnings") or []),
        "specstory": sync_lines,
        "last_snapshot_at": index.get("updated_at"),
        "logs_indexed": len(index.get("logs") or []),
        "index_path": str(paths["logs_index"]),
    }


# ── вывод ────────────────────────────────────────────────────────────────────


def format_text(out: dict) -> str:
    counts = out["counts"]
    lines = [f"collect: {out['logs_indexed']} записей в инвентаре ({out['index_path']})",
             " · ".join(f"{label}: {counts[state]}" for state, label in inventory.STATE_LABELS)]
    for line in out["specstory"]:
        lines.append(f"specstory: {line}")
    if out["sanitized"]:
        lines.append("санитайзер:")
        for rep in out["sanitized"]:
            rules = ", ".join(f"{k} {v}" for k, v in sorted(rep["rules_hit"].items())) or "правил нет"
            lines.append(f"- {rep['file']}: {rules}; имён заменено {rep['renamed']}")
    lines.append(f"*.orig найдено (не взяты): {out['orig']} · context-report (не взяты): "
                 f"{out['context_report']} · слаг-файлов SpecStory: {out['slug_named']}")
    if out["warnings"]:
        lines.append("предупреждения:")
        lines += [f"- {w}" for w in out["warnings"]]
    lines.append(f"last_snapshot_at: {out['last_snapshot_at']}")
    return "\n".join(lines)


def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    timeout = LOCK_TIMEOUT_QUIET if args.quiet else LOCK_TIMEOUT
    try:
        out = collect(root, args.provider, replace=getattr(args, "replace", None),
                      lock_timeout=timeout)
    except lock.LockBusy as err:
        if not args.quiet:
            raise
        # хук: соседний сбор держит замок дольше LOCK_TIMEOUT_QUIET — он и соберёт эту сессию
        print(f"sessionretro: сбор уже идёт в другом процессе, этот запуск пропущен ({err.message})",
              file=sys.stderr)
        return 0
    if args.quiet:
        return 0
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_text(out))
    return 0


def doctor_lines(root: Path | None) -> list[tuple[str, object]]:
    """`last_snapshot_at` (`logs-index.updated_at` или «нет») и `logs_indexed`."""
    if root is None:
        return [("last_snapshot_at", NO_WORKSPACE), ("logs_indexed", NO_WORKSPACE)]
    index = inventory.load_index(root)
    if index is None:
        return [("last_snapshot_at", NO_INDEX), ("logs_indexed", 0)]
    return [("last_snapshot_at", index.get("updated_at") or NO_INDEX),
            ("logs_indexed", len(index["logs"]))]
