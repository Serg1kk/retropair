"""Скилл `review` и его страницы: команды живые, фазы 1–12, входы агентов, правила, страницы для человека.

Проверяются реальные файлы плагина: `skills/review/SKILL.md`, `skills/review/references/orchestration.md`,
`docs/ru/how-it-works.md`, `docs/ru/review.md`, разделы движка оценки в README и CHANGELOG.
Слова, которые ищет проверка чистоты, собраны эскейпами, чтобы гейт не находил их в этом файле.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import unittest
from pathlib import Path

import _util  # noqa: F401 — кладёт scripts/ в sys.path
from _util import PLUGIN

from lib import collector_check, registry_cmds

SKILL_DIR = PLUGIN / "skills" / "review"
SKILL = SKILL_DIR / "SKILL.md"
ORCH = SKILL_DIR / "references" / "orchestration.md"
JUDGE_INPUT = SKILL_DIR / "references" / "judge-input.md"
HOW = PLUGIN / "docs" / "ru" / "how-it-works.md"
REVIEW_DOC = PLUGIN / "docs" / "ru" / "review.md"

LITERAL = 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py"'

# Команды сбора логов, которые скилл зовёт: они в общем парсере, белого списка нет.
INGEST_COMMANDS = {"inventory", "normalize", "collect"}

# фаза → команда, которая обязана в ней стоять
PHASE_COMMANDS = {
    1: "inventory", 2: "batches", 5: "check-collector", 7: "append", 8: "mark-logs",
    9: "level", 10: "render-review", 11: "verify",
}

# запрещённые слова гейта собраны эскейпами, чтобы гейт не видел их в этом файле
_W = [
    "\u0441\u0442\u0443\u0434\u0435\u043d\u0442",
    "\u043c\u043e\u0434\u0443\u043b",
    "\u043e\u043f\u0435\u0440\u0430\u0442\u043e\u0440",
]
FORBIDDEN = re.compile("(?i)" + "|".join(_W) + "|(?i:\\b\u043a\u0443\u0440\u0441(\u0430|\u0443|\u043e\u043c|\u0435|\u044b|\u043e\u0432|\u0430\u043c|\u0430\u043c\u0438|\u0430\u0445)?\\b)")
INTERNAL = re.compile("(?i:\u0444\u0438\u0447[\u0430\u0435\u0438\u0443\u044b]\u0439? \\d)|\\bTask \\d|q-exec-\\d|\\bD-\\d+\\b")

SR_CMD = re.compile(r'sr\.py"?\s+([a-z][a-z-]*)')
PHASE_RE = re.compile(r"^## Фаза (\d+)\.", re.M)
FENCE_RE = re.compile(r"^```[a-z]*\n(.*?)^```", re.M | re.S)


def _read(path: Path) -> str:
    if not path.is_file():
        raise AssertionError(f"нет файла ({path})")
    return path.read_text(encoding="utf-8")


def parser_commands() -> set[str]:
    parser = registry_cmds.build_parser()
    for action in parser._actions:  # noqa: SLF001 — argparse не даёт публичного доступа к подкомандам
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return set(action.choices)
    raise AssertionError("в парсере sr.py нет подкоманд")


def commands_in(text: str) -> list[str]:
    return SR_CMD.findall(text)


def command_lines(text: str) -> list[str]:
    """Строки вызова `sr.py` из блоков кода, с учётом переноса `\\`."""
    out = []
    for block in FENCE_RE.findall(text):
        joined = re.sub(r"\s*\\\n\s*", " ", block)
        out += [line.strip() for line in joined.splitlines() if "sr.py" in line]
    return out


def phases(text: str) -> dict[int, str]:
    """Номер фазы → её текст до следующей фазы или следующего заголовка второго уровня."""
    parts = re.split(r"^(?=## )", text, flags=re.M)
    out = {}
    for part in parts:
        m = PHASE_RE.match(part)
        if m:
            out[int(m.group(1))] = part
    return out


def judge_input_fields() -> list[str]:
    """Поля входа коллектора и судьи из таблицы контракта `judge-input.md`."""
    text = _read(JUDGE_INPUT)
    section = text.split("## Вход коллектора и судьи", 1)[1].split("\n## ", 1)[0]
    fields = []
    for line in section.splitlines():
        m = re.match(r"^\| `([a-z_]+)", line)
        if m:
            fields.append(m.group(1))
    if len(fields) < 10:
        raise AssertionError(f"таблица входа в judge-input.md не разобралась: {fields}")
    return fields


class HelpersTest(unittest.TestCase):
    """Разборщики теста на синтетическом тексте."""

    def test_commands_and_lines(self) -> None:
        text = ('Текст `sr.py" append`.\n```bash\n' + LITERAL + ' append <judge> \\\n  --person <person>\n```\n')
        self.assertEqual(commands_in(text), ["append", "append"])
        self.assertEqual(command_lines(text), [LITERAL + " append <judge> --person <person>"])

    def test_ellipsis_is_not_a_command(self) -> None:
        self.assertEqual(commands_in('Скрипты: ' + LITERAL + ' …'), [])

    def test_phases_split(self) -> None:
        text = "## Фаза 1. А\nx\n## Фаза 2. Б\ny\n## Прочее\nz\n"
        self.assertEqual(sorted(phases(text)), [1, 2])
        self.assertNotIn("z", phases(text)[2])


class SkillTest(unittest.TestCase):
    """`skills/review/SKILL.md`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(SKILL)
        cls.body = cls.text.split("\n---\n", 1)[1]
        cls.known = parser_commands()

    def test_frontmatter_name_kept(self) -> None:
        self.assertTrue(self.text.startswith("---\nname: review\n"))

    def test_not_a_stub(self) -> None:
        self.assertNotIn("Заглушка", self.body)
        self.assertGreater(len(self.body.splitlines()), 80)

    def test_every_command_exists_in_parser(self) -> None:
        used = set(commands_in(self.text))
        self.assertTrue(used)
        unknown = sorted(used - self.known)
        self.assertEqual(unknown, [], "команды из SKILL.md, которых нет в sr.py")

    def test_ingest_commands_in_parser_and_used(self) -> None:
        used = set(commands_in(self.text)) | set(commands_in(_read(ORCH)))
        self.assertLessEqual(INGEST_COMMANDS, self.known, "команды сбора логов пропали из sr.py")
        self.assertLessEqual(INGEST_COMMANDS, used, "скилл не зовёт команды сбора логов")

    def test_every_call_uses_literal(self) -> None:
        for line in command_lines(self.text):
            with self.subTest(line=line):
                self.assertTrue(line.startswith(LITERAL + " "), "вызов не через литерал плагина")

    def test_command_lines_parse(self) -> None:
        parser = registry_cmds.build_parser()
        lines = [ln for ln in command_lines(self.text) if commands_in(ln)[0] in self.known]
        self.assertGreater(len(lines), 8)
        for line in lines:
            with self.subTest(line=line):
                argv = shlex.split(line[len(LITERAL):])
                err = io.StringIO()
                try:
                    with contextlib.redirect_stderr(err):
                        parser.parse_args(argv)
                except SystemExit as exc:
                    self.fail(f"sr.py не принял вызов (код {exc.code}): {err.getvalue().strip()}")

    def test_literal_caveat(self) -> None:
        self.assertIn("два уровня выше", self.body)
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", self.body)

    def test_phases_one_to_twelve_in_order(self) -> None:
        self.assertEqual([int(n) for n in PHASE_RE.findall(self.body)], list(range(1, 13)))

    def test_phase_commands(self) -> None:
        by_phase = phases(self.body)
        for n, cmd in PHASE_COMMANDS.items():
            with self.subTest(phase=n, cmd=cmd):
                self.assertIn(f'sr.py" {cmd}', by_phase[n])

    def test_arguments(self) -> None:
        for flag in ("--all", "--task-summary", "--no-ask", "--approve"):
            with self.subTest(flag=flag):
                self.assertIn(flag, self.body)
        self.assertIn("ни одного вопроса", self.body)

    def test_agent_inputs_table(self) -> None:
        rows = [ln for ln in self.body.splitlines() if ln.startswith("|")]
        header = [ln for ln in rows if "Коллектор" in ln and "Судья" in ln]
        self.assertTrue(header, "нет таблицы входов агентов с колонками коллектора и судьи")
        card = [ln for ln in rows if "card_slice" in ln]
        self.assertTrue(card, "в таблице входов нет строки card_slice")
        cells = [c.strip() for c in card[0].strip("|").split("|")]
        self.assertEqual(cells[1], "нет", "профиль коллектору не подаётся")

    def test_not_given_to_judge(self) -> None:
        self.assertIn("Что не подаётся судье", self.body)
        section = self.body.split("Что не подаётся судье", 1)[1].split("\n## ", 1)[0]
        for item in ("баллы", "уровни", "суждения", "критерии приёмки", "артефакт", "CLAUDE.md", "AGENTS.md"):
            with self.subTest(item=item):
                self.assertIn(item, section)

    def test_rules(self) -> None:
        for phrase in ("Ничего не срезать", "Две ступени всегда", "Стабы заранее", "Edit",
                       "output_path", "руками не правится", "одним сообщением"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.body)

    def test_agent_choice(self) -> None:
        for phrase in (".claude/agents/sessionretro-log-collector.md", ".claude/agents/sessionretro-log-judge.md",
                       "sessionretro:log-collector", "sessionretro:log-judge", "subagent_type",
                       "config.models.collector", "config.models.judge"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.body)

    def test_references_contract_and_orchestration(self) -> None:
        self.assertIn("references/judge-input.md", self.body)
        self.assertIn("references/orchestration.md", self.body)

    def test_phase1_syncs_project_agents(self) -> None:
        """A15: фаза 1 до коллекторов обновляет непроправленных проектных агентов (`agents sync`)."""
        phase = phases(self.body)[1]
        self.assertIn('sr.py" agents sync', phase)
        self.assertIn("правлен вручную", phase)
        after = phase.split("agents sync", 1)[1]
        self.assertIn("--no-ask", after)
        self.assertIn("в сводку (фаза 12)", after, "при --no-ask строки sync не молча, а в сводку")
        self.assertIn("до выбора агента", phase)
        self.assertLess(phase.index("doctor --json"), phase.index('sr.py" agents sync'))
        self.assertNotIn("agents sync", phases(self.body)[4])
        self.assertIn("agents sync", _read(ORCH).split("## Сводка", 1)[1])

    def test_no_card_stops_person(self) -> None:
        """Нет карточки → прогон по человеку останавливается в фазе 1: `append` без карточки не пишет."""
        phase = phases(self.body)[1]
        self.assertNotIn("card_slice: null", phase)
        self.assertIn('sr.py" people add', phase)
        self.assertIn("останавливается", phase)

    def test_run_mode_batches_without_label(self) -> None:
        """В режиме `run` метка не совпадёт ни с одной `period_guess`: `batches` зовётся без неё."""
        phase = phases(self.body)[2]
        self.assertIn("режиме `run`", phase)
        self.assertIn("без `--period-label`", phase)

    def test_run_mode_append_without_label(self) -> None:
        """В режиме `run` метку выдаёт `append`: скилл её не придумывает и не передаёт."""
        phase = phases(self.body)[7]
        appends = [ln for ln in command_lines(phase) if commands_in(ln)[:1] == ["append"]]
        self.assertTrue(any("--period-label" not in ln for ln in appends),
                        "нет вызова append без --period-label для режима run")
        for line in appends:
            with self.subTest(line=line):
                self.assertIsNone(re.search(r"--period-label\s+\S*run-", line))
        self.assertIn("режим `run`", phase)
        self.assertIn("period_label", phase)
        self.assertIn("уже есть прогон", phase)
        self.assertIn("--replace <run_id>", phase)
        first = phases(self.body)[1]
        self.assertIn("метку не придумывай", first)
        self.assertIn("period_label", first)

    def test_next_rec_number_to_judge_and_renumber_in_append(self) -> None:
        """A07: `next_number` из `open-recs` идёт судье; перенумерация в `append` не повод перезапускать судью."""
        self.assertIn("next_number", phases(self.body)[1])
        self.assertIn("next_rec_number", phases(self.body)[6])
        phase = phases(self.body)[7]
        self.assertIn("перенумер", phase)
        self.assertIn("не перезапускается", phase)

    def test_phase2_only_short_sessions_closed_without_run(self) -> None:
        """B03: пачек нет, короткие есть → `mark-logs --no-run` по всем коротким, агентов не звать."""
        phase = phases(self.body)[2]
        calls = [ln for ln in command_lines(phase) if commands_in(ln)[:1] == ["mark-logs"]]
        self.assertEqual(len(calls), 1, calls)
        argv = shlex.split(calls[0][len(LITERAL):])
        args = registry_cmds.build_parser().parse_args(argv)
        self.assertTrue(args.no_run)
        self.assertEqual(args.person, "<person>")
        self.assertIsNone(args.run_id)
        self.assertTrue(all(ref.endswith("=transcript_too_short") for ref in args.excluded), args.excluded)
        self.assertGreaterEqual(len(args.excluded), 2, "A13: пример на несколько сессий одним флагом")
        for phrase in ("только короткие", "данных мало", "агентов не зови", "новых логов нет",
                       "флаг можно повторять"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, phase)

    def test_phase1_draft_recs_not_given_to_judge(self) -> None:
        """B04: агентам только `open`; `pending_approval` — строка в сводке и предупреждение, прогон идёт."""
        phase = phases(self.body)[1]
        for phrase in ("`open`", "pending_approval", "из неодобренного черновика", "вердикт отложен",
                       "неодобренный черновик прошлого периода", "--no-ask", "не блокируется"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, phase)
        summary = phases(self.body)[12]
        self.assertIn("вердикт отложен", summary)
        self.assertIn("неодобренный черновик прошлого периода", summary)

    def test_phase3_recorded_judge_output_is_not_asked_about(self) -> None:
        """Записанный выход судьи прошлого прогона не повод для вопроса: незаписанный называет `verify`."""
        phase = phases(self.body)[3]
        self.assertIn('sr.py" verify', phase)
        self.assertIn("сирота", phase)
        self.assertIn("перезаписывается стабом", phase)

    def test_phase8_mark_logs_is_additive(self) -> None:
        phase = phases(self.body)[8]
        self.assertIn("--force", phase)
        self.assertIn("добавляет", phase)
        self.assertIn("флаг можно повторять", phase)
        calls = [ln for ln in command_lines(phase) if commands_in(ln)[:1] == ["mark-logs"]]
        self.assertEqual(len(calls), 1, calls)
        args = registry_cmds.build_parser().parse_args(shlex.split(calls[0][len(LITERAL):]))
        self.assertGreaterEqual(len(args.excluded), 2, "A13: пример на несколько сессий одним флагом")
        reasons = phase.split("Причины исключения:", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("other_period", reasons)

    def test_other_period_left_open(self) -> None:
        self.assertIn("не закрываются", phases(self.body)[6])

    def test_card_suggestions_shown_never_applied(self) -> None:
        """После сводки и после `--approve` — `people suggestions <person> --refresh`, в сводке число
        и строки с командами apply/reject; скилл сам ничего не применяет, в `--no-ask` только показ."""
        call = LITERAL + " people suggestions <person> --refresh"
        final = phases(self.body)[12]
        approve = self.body.split("**Одобрение.**", 1)[1].split("\n## ", 1)[0]
        for part in (final, approve):
            self.assertIn(call, command_lines(part))
        for phrase in ("предложения к карточке: N", "people apply", "people reject", "сам не применяет",
                       "--no-ask"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, final)

    def test_clean(self) -> None:
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            with self.subTest(line=lineno):
                self.assertIsNone(FORBIDDEN.search(line), line)
                self.assertIsNone(INTERNAL.search(line), line)


class OrchestrationTest(unittest.TestCase):
    """`skills/review/references/orchestration.md`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.text = _read(ORCH)
        cls.skill = _read(SKILL)

    def test_briefs_carry_every_contract_field(self) -> None:
        for field in judge_input_fields():
            with self.subTest(field=field):
                self.assertIn(f"`{field}", self.text)

    def test_briefs_for_both_agents(self) -> None:
        self.assertIn("## Бриф коллектора", self.text)
        self.assertIn("## Бриф судьи", self.text)

    def test_every_command_known(self) -> None:
        unknown = sorted(set(commands_in(self.text)) - parser_commands())
        self.assertEqual(unknown, [])

    def test_exit_codes_table_covers_skill_commands(self) -> None:
        section = self.text.split("## Коды выхода", 1)[1].split("\n## ", 1)[0]
        for cmd in sorted(set(commands_in(self.skill))):
            with self.subTest(cmd=cmd):
                self.assertRegex(section, rf"(?m)^\| `{re.escape(cmd)}`")

    def test_ingest_rows_match_real_commands(self) -> None:
        # сбор логов в парсере: «команды нет» больше не исход; отдельной сводки по окну он не строит
        section = self.text.split("## Коды выхода", 1)[1].split("\n## ", 1)[0]
        for cmd in ("inventory", "collect"):
            row = next(ln for ln in section.splitlines() if ln.startswith(f"| `{cmd}`"))
            self.assertNotIn("команды нет", row)
            self.assertIn("занят", row, "замок сбора: код 1, пока другой процесс держит индекс")
        self.assertNotIn("Команды нет (сбор логов не установлен)", self.skill)
        brief = self.text.split("## Бриф коллектора", 1)[1].split("\n## ", 1)[0]
        row = next(ln for ln in brief.splitlines() if ln.startswith("| `context_report`"))
        self.assertIn("`null`", row)
        self.assertNotIn("если сбор логов её построил", row)
        self.assertIn("context_peak_tokens", row)

    def test_stub_literal_is_caught_by_engine(self) -> None:
        blocks = [b for b in FENCE_RE.findall(self.text) if '"stub"' in b]
        self.assertTrue(blocks, "нет блока с образцом стаба")
        stub = json.loads(blocks[0])
        problems = collector_check.check_batch(stub)
        self.assertTrue(any("стаб" in p for p in problems), problems)

    def test_no_card_stops_person(self) -> None:
        section = self.text.split("## Коды выхода", 1)[1].split("\n## ", 1)[0]
        row = next(ln for ln in section.splitlines() if ln.startswith("| `people`"))
        self.assertIn("остановить прогон", row)
        self.assertNotIn("card_slice: null", row)
        judge = self.text.split("## Бриф судьи", 1)[1].split("\n## ", 1)[0]
        card_row = next(ln for ln in judge.splitlines() if ln.startswith("| `card_slice`"))
        self.assertNotIn("null", card_row)

    def test_other_period_left_open(self) -> None:
        judge = self.text.split("## Бриф судьи", 1)[1].split("\n## ", 1)[0]
        self.assertIn("не закрываются", judge)

    def test_mark_logs_row_repeatable_flag_and_duplicate(self) -> None:
        """A13: `--excluded` повторяется и принимает список; дубль ссылки — код 2."""
        section = self.text.split("## Коды выхода", 1)[1].split("\n## ", 1)[0]
        row = next(ln for ln in section.splitlines() if ln.startswith("| `mark-logs` |"))
        self.assertIn("флаг можно повторять", row)
        self.assertIn("указана дважды", row)

    def test_open_recs_only_open_and_pending_in_summary(self) -> None:
        """B04: во вход агентов идёт только `open`; `pending_approval` — в сводку, вердикта нет."""
        brief = self.text.split("## Бриф коллектора", 1)[1].split("\n## ", 1)[0]
        row = next(ln for ln in brief.splitlines() if ln.startswith("| `open_recs`"))
        self.assertIn("только `open`", row)
        self.assertIn("pending_approval", row)
        summary = self.text.split("## Сводка", 1)[1]
        for phrase in ("из неодобренного черновика", "вердикт отложен", "неодобренный черновик прошлого периода"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, summary)
        section = self.text.split("## Коды выхода", 1)[1].split("\n## ", 1)[0]
        append_row = next(ln for ln in section.splitlines() if ln.startswith("| `append`"))
        self.assertIn("неодобренного черновика", append_row)

    def test_filled_arrays_reach_summary(self) -> None:
        """A06: опущенный коллектором массив, дописанный пустым (`filled` вывода
        `check-collector`), — код 0 без перезапуска и строка «пачка неполная» в сводке."""
        fifth = phases(self.skill.split("\n---\n", 1)[1])[5]
        for phrase in ("`filled`", "пачка неполная", "не искалось"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, fifth)
        summary = self.text.split("## Сводка", 1)[1]
        self.assertIn("пачка неполная: <строки filled из check-collector", summary)
        final = self.skill.split("## Фаза 12. Сводка", 1)[1]
        self.assertIn("пачка неполная", final)
        section = self.text.split("## Коды выхода", 1)[1].split("\n## ", 1)[0]
        row = next(ln for ln in section.splitlines() if ln.startswith("| `check-collector` |"))
        self.assertIn("`filled`", row)

    def test_open_recs_issued_at_and_evaluable_from_script(self) -> None:
        """A16: `issued_at` и `evaluable` из `open-recs --period-label` идут агентам как есть;
        замена вердикта стражем `append` — код 0, судья не перезапускается."""
        brief = self.text.split("## Бриф коллектора", 1)[1].split("\n## ", 1)[0]
        row = next(ln for ln in brief.splitlines() if ln.startswith("| `open_recs`"))
        for phrase in ("issued_at", "evaluable", "--period-label", "not_applicable_yet"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, row)
        first = phases(self.skill.split("\n---\n", 1)[1])[1]
        for phrase in ("open-recs <person> --json --period-label <label>", "issued_at", "evaluable",
                       "сам не пересчитывай", "заново перед каждым прогоном"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, first)
        seventh = phases(self.skill.split("\n---\n", 1)[1])[7]
        self.assertIn("not_applicable_yet", seventh)
        self.assertIn("логи периода старше выдачи", seventh)
        codes = self.text.split("## Коды выхода", 1)[1].split("\n## ", 1)[0]
        append_row = next(ln for ln in codes.splitlines() if ln.startswith("| `append`"))
        self.assertIn("логи периода старше выдачи", append_row)
        judge = (PLUGIN / "agents" / "log-judge.md").read_text(encoding="utf-8")
        collector = (PLUGIN / "agents" / "log-collector.md").read_text(encoding="utf-8")
        contract = (PLUGIN / "skills" / "review" / "references" / "judge-input.md").read_text(encoding="utf-8")
        self.assertIn("`evaluable: false` → всегда `not_applicable_yet`", judge)
        self.assertIn("Сигналы по рекомендации с `evaluable: false` не собирай", collector)
        self.assertIn("`open_recs[]{id,text,period,issued_at,evaluable}`", contract)

    def test_judge_brief_carries_next_rec_number(self) -> None:
        """A07: судья получает следующий свободный номер, `append` всё равно перенумерует."""
        judge = self.text.split("## Бриф судьи", 1)[1].split("\n## ", 1)[0]
        row = next(ln for ln in judge.splitlines() if ln.startswith("| `next_rec_number`"))
        self.assertIn("next_number", row)
        self.assertIn("open-recs", row)
        self.assertIn("номер новой рекомендации начинать с REC-<person>-<NNN>", judge)
        self.assertIn("перенумер", judge)
        codes = self.text.split("## Коды выхода", 1)[1].split("\n## ", 1)[0]
        append_row = next(ln for ln in codes.splitlines() if ln.startswith("| `append`"))
        self.assertIn("уже выдан", append_row)
        self.assertIn("не перезапускать", append_row)

    def test_recorded_judge_output_reset_to_stub(self) -> None:
        section = self.text.split("## Стабы", 1)[1].split("\n## ", 1)[0]
        self.assertIn("Записанный выход судьи", section)
        self.assertIn('sr.py" verify', section)

    def test_summary_names_card_suggestions(self) -> None:
        summary = self.text.split("## Сводка", 1)[1]
        self.assertIn("предложения к карточке: N", summary)
        self.assertIn("people suggestions <person> --refresh", summary)

    def test_restart_and_summary(self) -> None:
        for phrase in ("## Перезапуск", "## Сводка"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_clean(self) -> None:
        for lineno, line in enumerate(self.text.splitlines(), start=1):
            with self.subTest(line=lineno):
                self.assertIsNone(FORBIDDEN.search(line), line)
                self.assertIsNone(INTERNAL.search(line), line)
                self.assertNotRegex(line, r"\{\{[^{}]*\}\}")


class UserPagesTest(unittest.TestCase):
    """Страницы для человека: как устроена оценка и как запускать ревью."""

    def test_how_it_works(self) -> None:
        text = _read(HOW)
        for phrase in ("две ступени", "восемь осей", "пачк", "цитат", "медиан", "мало данных",
                       "не наказывается", "axes.md"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text.lower() if phrase.islower() else text)

    def test_review_page(self) -> None:
        text = _read(REVIEW_DOC)
        for phrase in ("--no-ask", "--approve", "--task-summary", "--all", "draft", "approved",
                       'sr.py" approve', 'sr.py" render-review', 'sr.py" verify', 'sr.py" self-test',
                       'sr.py" retract', "`0`", "`1`", "`2`", "что не подаётся", "how-it-works.md"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text if not phrase.islower() else text.lower())

    def test_review_page_run_mode_labels(self) -> None:
        """Несколько ретро за сутки в режиме `run` — отдельные периоды с суффиксом метки."""
        text = _read(REVIEW_DOC)
        for phrase in ("режим `run`", "run-<ГГГГММДД>", "`-2`", "отдельными периодами"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_review_page_recs_count_after_approve(self) -> None:
        """B04: в team и course рекомендации начинают учитываться после одобрения текста."""
        text = _read(REVIEW_DOC)
        self.assertIn("рекомендации", text)
        self.assertIn("после одобрения", text)
        self.assertIn("pending_approval", text)

    def test_pages_clean(self) -> None:
        for path in (HOW, REVIEW_DOC):
            for lineno, line in enumerate(_read(path).splitlines(), start=1):
                with self.subTest(page=path.name, line=lineno):
                    self.assertIsNone(FORBIDDEN.search(line), line)
                    self.assertIsNone(INTERNAL.search(line), line)

    def test_pages_commands_known(self) -> None:
        known = parser_commands()
        for path in (HOW, REVIEW_DOC):
            with self.subTest(page=path.name):
                self.assertEqual(sorted(set(commands_in(_read(path))) - known), [])


class SharedDocsTest(unittest.TestCase):
    """Разделы движка оценки в README, README.ru, CHANGELOG, TESTING."""

    # начало раздела: заголовок `## …` (раздел до следующего `## `) или пункт CHANGELOG (одна строка)
    SECTION_START = {
        "README.md": "## Running a review",
        "README.ru.md": "## Как запустить ревью",
        "TESTING.md": "## Движок оценки",
        "CHANGELOG.md": "- Движок оценки и скилл `review`",
    }

    @classmethod
    def section(cls, name: str) -> str:
        start = cls.SECTION_START[name]
        lines = _read(PLUGIN / name).splitlines()
        idx = [i for i, line in enumerate(lines) if line.startswith(start)]
        if len(idx) != 1:
            raise AssertionError(f"раздел «{start}» в {name}: найдено {len(idx)}, ожидался один")
        i = idx[0]
        if not start.startswith("## "):
            return lines[i] + "\n"
        end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        return "\n".join(lines[i:end]) + "\n"

    def test_readmes_link_pages(self) -> None:
        for name, lang in (("README.md", "en"), ("README.ru.md", "ru")):
            with self.subTest(readme=name):
                sec = self.section(name)
                self.assertIn(f"docs/{lang}/review.md", sec)
                self.assertIn(f"docs/{lang}/how-it-works.md", sec)

    def test_changelog_bullet(self) -> None:
        self.assertRegex(self.section("CHANGELOG.md"), r"(?m)^- .+")

    def test_testing_mentions_self_test(self) -> None:
        sec = self.section("TESTING.md")
        self.assertIn("self-test", sec)
        self.assertIn("`test_skill_review.py`", sec)


if __name__ == "__main__":
    unittest.main()
