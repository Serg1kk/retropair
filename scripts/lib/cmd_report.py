"""Команда `report`: HTML-отчёты workspace — групповой, ревьюера, личные и страница workspaces.

Без флагов целей собирается всё, что уместно режиму: self — reviewer, все личные и index;
team/course — ещё и group. Личный файл проходит гейт чужих имён: нашлись токены других
людей или `sanitize.names` — файл не пишется, старый удаляется, итоговый код 1, остальные
файлы собираются. `--anon` обезличивает все файлы (коды `p01…`, вымышленные имена, без
контактов и домашних путей), личные файлы тогда называются по коду; перед записью каждый
обезличенный файл проверяется на настоящие токены ростера (в личном — и адресата).
Личный файл строится только из одобренных прогонов (при гейте ревьюера); у кого их нет,
тому личный файл не собирается — предупреждение, а для `--personal` код 1.

Личный файл — карточка человека из отчёта ревьюера минус служебное, в одном из двух видов:
`scores` (графики осей с баллами 0–4 и уровень) или `trend` (переходы ↑ → ↓ ◇ без баллов и
уровня — их нет и в данных файла). Вид: `--scores`/`--trend` (совместимо `--show-scores`/
`--hide-scores`; флаги взаимоисключающие) > `feedback.show_scores` карточки человека >
`config.show_scores`. Тот же вид показывает «Как видит адресат» в отчёте ревьюера.

Групповой отчёт по умолчанию несёт карточку человека (`people[].details`): одобренные
отправленные тексты ревью по периодам и рекомендации с историей вердиктов, без баллов и уровней.
Выключается `--no-details` или `config.report.group_details: false` («только цифры»). Имена
участников в групповом законны, гейтом чужих имён он не проверяется; но `sanitize.names` (люди
вне ростера, которых не должно быть нигде) ищутся в нём всегда, как в личном: нашлись — файл не
пишется, старый удаляется, код 1. С `--anon` групповой, как и отчёт ревьюера, проходит проверку
ростера. Устаревший `feedback.html` (страница прежних версий) при сборке группового удаляется.

Коды: 0 — всё собрано; 1 — гейт, нет или битый трекер, нет данных, у человека нет
прогонов или одобренных прогонов, групповой отчёт в self как единственная цель;
2 — ошибка вызова.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib import anon, registry, render
from lib import report_data as rd
from lib.config import load_config
from lib.people import list_people
from lib.workspace import SrError, find_workspace, ws_paths

LANGS = ("ru", "en")
NOT_BUILT = "не собирались"
SELF_NO_GROUP = "в режиме self групповой отчёт не собирается"


def register(subparsers) -> None:
    p = subparsers.add_parser(
        "report", help="HTML-отчёты: групповой, ревьюера, личные, страница workspaces")
    p.add_argument("--group", action="store_true", help="групповой отчёт (team, course)")
    who = p.add_mutually_exclusive_group()
    who.add_argument("--personal", metavar="ID", help="личный отчёт одного человека")
    who.add_argument("--all-personal", action="store_true", help="личные отчёты всех людей")
    p.add_argument("--reviewer", action="store_true", help="отчёт ревьюера")
    p.add_argument("--no-details", action="store_true",
                   help="групповой отчёт без карточки человека (тексты ревью и рекомендации): "
                        "только цифры")
    p.add_argument("--index", action="store_true",
                   help="страница workspaces в ~/.retropair/reports/index.html")
    p.add_argument("--anon", action="store_true",
                   help="обезличить: коды вместо id, вымышленные имена, без контактов и путей")
    p.add_argument("--lang", choices=LANGS, help="язык отчёта (по умолчанию из config.lang)")
    p.add_argument("--out-dir", metavar="DIR",
                   help="папка отчётов вместо <workspace>/.retropair/reports")
    view = p.add_mutually_exclusive_group()
    view.add_argument("--scores", dest="view", action="store_const", const="scores",
                      help="личные отчёты на этот запуск — с баллами и уровнем (вид scores)")
    view.add_argument("--trend", dest="view", action="store_const", const="trend",
                      help="личные отчёты на этот запуск — только динамика ↑ → ↓ ◇, без баллов "
                           "и уровня (вид trend)")
    view.add_argument("--show-scores", dest="view", action="store_const", const="scores",
                      help="то же, что --scores (прежнее имя)")
    view.add_argument("--hide-scores", dest="view", action="store_const", const="trend",
                      help="то же, что --trend")
    p.add_argument("--json", action="store_true", help="вывод списком {kind, person, path, bytes}")
    p.set_defaults(func=handle)


# --- вспомогательное ----------------------------------------------------------------------

def _warn(message: str) -> None:
    print(f"предупреждение: {message}", file=sys.stderr)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / (1024 * 1024):.1f} МБ"


def _targets(args, mode: str) -> list[str]:
    """Цели по порядку сборки: group, reviewer, personal, index.

    Без флагов целей — всё, что уместно режиму."""
    explicit = (args.group or args.reviewer or args.index or args.personal
                or args.all_personal)
    if not explicit:
        base = ["reviewer", "personal", "index"]
        return base if mode == "self" else ["group", *base]
    out = []
    if args.group:
        out.append("group")
    if args.reviewer:
        out.append("reviewer")
    if args.personal or args.all_personal:
        out.append("personal")
    if args.index:
        out.append("index")
    return out


def _mode_label(i18n: dict, key: str, lang: str) -> str:
    entry = i18n.get("report", {}).get(key)
    text = entry.get(lang) if isinstance(entry, dict) else None
    if not text:
        raise SrError(f"в словаре интерфейса нет строки report.{key} для языка {lang}", 1)
    return text


class _Run:
    """Состояние одного запуска: выведенные файлы и итоговый код."""

    def __init__(self, as_json: bool) -> None:
        self.as_json = as_json
        self.items: list[dict] = []
        self.rc = 0

    def wrote(self, kind: str, person: str | None, path: Path, size: int) -> None:
        self.items.append({"kind": kind, "person": person, "path": str(path), "bytes": size})
        if not self.as_json:
            print(f"{path} · {_size(size)}")

    def fail(self, message: str, code: int = 1) -> None:
        print(f"retropair: {message}", file=sys.stderr)
        self.rc = max(self.rc, code)


# --- команда ------------------------------------------------------------------------------

def handle(args) -> int:
    root = find_workspace(Path.cwd(), args.workspace)
    cfg = load_config(root)
    mode = cfg["mode"]
    lang = args.lang or cfg["lang"]
    # Вид личного: флаг запуска (None — не задан) > feedback.show_scores карточки > конфиг.
    scores_cli = None if args.view is None else args.view == "scores"
    out_dir = Path(args.out_dir).resolve() if args.out_dir else ws_paths(root)["reports"]
    run = _Run(args.json)

    targets = _targets(args, mode)
    if args.no_details and "group" not in targets:
        raise SrError(f"report --no-details относится только к групповому отчёту, а он в "
                      f"целях не значится (цели: {', '.join(targets)}); добавьте --group", 2)
    details = not args.no_details and _group_details_on(cfg)
    if "group" in targets and mode == "self":
        targets.remove("group")
        if not targets:
            run.fail(f"{SELF_NO_GROUP} (workspace {root}), других целей нет")
            if args.json:
                print(json.dumps(run.items))
            return run.rc
        print(f"retropair: {SELF_NO_GROUP} (workspace {root}), остальное собирается",
              file=sys.stderr)

    cards = {c["id"]: c for c in list_people(root)}
    if args.personal is not None and args.personal not in cards:
        known = ", ".join(sorted(cards)) or "нет ни одной карточки"
        raise SrError(f"report --personal: нет карточки человека {args.personal!r} "
                      f"(people/{args.personal}.yaml); известны: {known}", 2)

    i18n = rd.load_i18n()
    generated_at = _now()
    data_targets = [t for t in targets if t != "index"]
    if data_targets:
        model = rd.collect(root, _warn)
        with_runs = {p["id"] for p in model["people"]}
        if args.personal is not None and args.personal not in with_runs:
            raise SrError(f"report --personal: у человека {args.personal!r} нет живых прогонов "
                          f"в трекере ({ws_paths(root)['runs'] / 'process-reviews.json'})", 1)
        by_id = {p["id"]: p for p in model["people"]}
        if (args.personal is not None and "personal" in data_targets
                and not rd.approved_points(by_id[args.personal])):
            raise SrError(f"report --personal: у человека {args.personal!r} нет одобренных "
                          f"прогонов — личный отчёт показывает только одобренные; сначала "
                          f"approve", 1)
        extra_names = list(cfg["sanitize"]["names"])
        anonymizer = (anon.Anonymizer(list(cards.values()), extra_names, lang)
                      if args.anon else None)
        common = {"show_scores": bool(cfg["show_scores"]), "scores_cli": scores_cli,
                  "lang": lang, "generated_at": generated_at}
        ws_title = model["workspace"]["title"]

        if "group" in data_targets:
            _drop_stale_feedback(out_dir)
        for kind in ("group", "reviewer"):
            if kind not in data_targets:
                continue
            title = f"{_mode_label(i18n, 'mode_' + kind, lang)} · {ws_title}"
            payload = rd.build_payload(model, mode=kind, title=title,
                                       details=details and kind == "group", **common)
            path = out_dir / f"{kind}.html"
            if anonymizer is not None:
                payload = rd.attach_html(_anonymized(anonymizer, payload))
                hits = _roster_hits(payload, cards, extra_names)
                if hits:
                    _refuse(run, path, hits)
                    continue
            else:
                rd.attach_html(payload)
                hits = _group_hits(payload, extra_names) if kind == "group" else []
                if hits:
                    _refuse(run, path, hits)
                    continue
            html_text = render.render_html(payload, i18n)
            run.wrote(kind, None, path, render.write_html_atomic(path, html_text))

        if "personal" in data_targets:
            ids = [args.personal] if args.personal else [p["id"] for p in model["people"]]
            waiting = [pid for pid in ids if not rd.approved_points(by_id[pid])]
            if waiting:
                _warn(f"нет одобренных прогонов, личный отчёт не собирается: "
                      f"{', '.join(waiting)} (ждут approve ревьюера)")
                ids = [pid for pid in ids if pid not in waiting]
            if anonymizer is None:
                _warn_title_names(ws_title, cards, ids)
            for pid in ids:
                _personal(run, model, cards, pid, anonymizer=anonymizer,
                          extra_names=extra_names, i18n=i18n, out_dir=out_dir,
                          ws_title=ws_title, common=common)

    if "index" in targets:
        index_path = registry.sr_home() / "reports" / "index.html"
        html_text = render.render_index(registry.load(), lang=lang, generated_at=generated_at,
                                        i18n=i18n)
        run.wrote("index", None, index_path,
                  render.write_html_atomic(index_path, html_text, min_bytes=0))

    if args.json:
        print(json.dumps(run.items, ensure_ascii=False, indent=2))
    return run.rc


def _group_details_on(cfg: dict) -> bool:
    """`config.report.group_details` (по умолчанию true); тип проверен схемой config."""
    return (cfg.get("report") or {}).get("group_details", True)


def _anonymized(anonymizer, payload: dict) -> dict:
    """Обезличенная копия payload с пометкой `meta.anon`: групповой отчёт показывает плашку
    «обезличено» вместо «не для рассылки»."""
    out = anonymizer.payload(payload)
    out["meta"]["anon"] = True
    return out


def _drop_old(path: Path) -> str:
    """Файл не собран (гейт): старый удаляется, чтобы не выдать устаревший; хвост сообщения."""
    if path.exists():
        path.unlink()
        return "; старый файл удалён"
    return ""


def _refuse(run: _Run, path: Path, hits: list[str]) -> None:
    """Гейт сработал: файл не пишется, старый удаляется, код 1."""
    removed = _drop_old(path)
    run.fail(f"чужие токены в {path}: {'; '.join(hits)} — файл не записан{removed}")


def _warn_title_names(ws_title: str, cards: dict, ids: list[str]) -> None:
    """Заголовок workspace виден в каждом личном файле, а личный гейт его не проверяет: если в нём
    есть токены карточек, а файл собирается для кого-то другого, — предупреждение по id (без имён)."""
    owners = anon.title_name_owners(ws_title, list(cards.values()))
    if owners and any(set(owners) - {pid} for pid in ids):
        _warn(f"workspace title contains name tokens of: {', '.join(owners)} — заголовок "
              "workspace показывается в каждом личном файле и гейтом чужих имён не проверяется; "
              "уберите имена из workspace.title в .retropair/config.json")


def _roster_hits(payload: dict, cards: dict, extra_names) -> list[str]:
    """Обезличенный group/reviewer: настоящих токенов ростера и `sanitize.names` нет ни одного."""
    patterns = anon.roster_gate_patterns(list(cards.values()), extra_names)
    return anon.personal_gate(anon.gate_text(payload, payload["meta"]["title"]), patterns)


def _group_hits(payload: dict, extra_names) -> list[str]:
    """Групповой без `--anon`: имена участников в нём законны, ищутся только `sanitize.names`
    (заголовок workspace не проверяется, как в личном). Проверяется всегда, а не только с
    карточками: список — люди, которых не должно быть ни в одном файле; без карточек свободного
    текста в payload почти нет, совпадений не будет, а новая строка в payload не проскочит мимо."""
    patterns = anon.roster_gate_patterns([], extra_names)
    if not patterns:
        return []
    text = anon.gate_text(payload, payload["meta"]["title"], skip_workspace_title=True)
    return anon.personal_gate(text, patterns)


def _drop_stale_feedback(out_dir: Path) -> None:
    """`feedback.html` прежних версий (тексты всех людей) больше не собирается и не ссылается из
    index: при сборке группового он удаляется, чтобы не остался лежать в папке отчётов."""
    stale = out_dir / "feedback.html"
    if stale.is_file():
        stale.unlink()
        _warn(f"удалён устаревший {stale}: страница фидбэка заменена карточкой человека "
              "в group.html")


def _personal_hits(payload: dict, pid: str, cards: dict, anonymizer, extra_names) -> list[str]:
    """Гейт личного файла. Без `--anon`: токены других людей и `sanitize.names`. С `--anon`:
    ещё вымышленные имена и коды других людей и настоящие токены самого адресата."""
    others = [c for c in cards.values() if c["id"] != pid]
    title = payload["meta"]["title"]
    if anonymizer is None:
        patterns = anon.personal_gate_patterns(cards[pid], others, extra_names)
        return anon.personal_gate(anon.gate_text(payload, title, skip_workspace_title=True), patterns)
    # --anon: настоящие токены (и адресата) ищутся во всём тексте, заголовок workspace тоже.
    # Вымышленные имена и коды других людей — везде, кроме заголовка workspace: слово заголовка,
    # совпавшее со словом имени («Demo team» и «Alex Demo»), обезличено вымышленным словом, это не
    # упоминание человека (как без --anon).
    target = {"id": anonymizer.id_map[pid], "name": anonymizer.name_map[pid]}
    real = anon.personal_gate_patterns(target, others + [cards[pid]], extra_names)
    fake = anon.personal_gate_patterns(target, [
        {"id": anonymizer.id_map[c["id"]], "name": anonymizer.name_map[c["id"]]} for c in others])
    return (anon.personal_gate(anon.gate_text(payload, title), real)
            + anon.personal_gate(anon.gate_text(payload, title, skip_workspace_title=True), fake))


def _personal(run: _Run, model: dict, cards: dict, pid: str, *, anonymizer, extra_names,
              i18n: dict, out_dir: Path, ws_title: str, common: dict) -> None:
    """Личный файл одного человека: payload → (--anon) → html → гейт чужих имён → запись."""
    lang = common["lang"]
    person = next(p for p in model["people"] if p["id"] == pid)
    shown_name = person["name"] if anonymizer is None else anonymizer.name_map[pid]
    title = f"{_mode_label(i18n, 'mode_personal', lang)} · {shown_name}"
    payload = rd.build_payload(model, mode="personal", person=pid, title=title,
                               show_dynamics=person["show_dynamics"], **common)
    if anonymizer is None:
        file_id = pid
    else:
        payload = _anonymized(anonymizer, payload)
        payload["meta"]["title"] = title
        file_id = anonymizer.id_map[pid]
    rd.attach_html(payload)

    path = out_dir / "personal" / f"{file_id}.html"
    hits = _personal_hits(payload, pid, cards, anonymizer, extra_names)
    if hits:
        _refuse(run, path, hits)
        return
    html_text = render.render_html(payload, i18n)
    run.wrote("personal", file_id, path, render.write_html_atomic(path, html_text))


def doctor_lines(root: Path | None) -> list[tuple[str, object]]:
    """Строка doctor: когда последний раз собирался отчёт ревьюера этого workspace."""
    if root is None:
        return []
    path = ws_paths(root)["reports"] / "reviewer.html"
    if not path.is_file():
        return [("reports_built_at", NOT_BUILT)]
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return [("reports_built_at", stamp.strftime("%Y-%m-%dT%H:%M:%SZ"))]
