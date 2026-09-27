# Настройка: как подключить sessionretro к проекту

Чтобы sessionretro начал работать в проекте, ему нужен workspace — папка `.sessionretro/` с
настройками, карточками людей и местом под логи. Проще всего создать его диалогом: откройте
Claude Code в папке проекта и вызовите скилл.

```text
/sessionretro:setup
```

То же сработает словами: «настрой sessionretro», «подключи ретро сессий». Скилл задаст до 11
вопросов по одному, покажет итог и применит ответы одной командой. Сам он файлов не пишет: всё
пишет CLI плагина `sr.py`, поэтому тот же результат можно получить без диалога, из скрипта (см.
«Без диалога» ниже).

## Первый запуск

Скилл сначала смотрит, что уже есть: workspace, логи Claude Code этого проекта, папка SpecStory,
срок хранения логов. Для этого он запускает:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" setup-state --json
```

Настраивается корень git-репозитория, в котором вы открыли Claude Code; если папка не в git —
сама папка. Нужен другой проект — скажите об этом скиллу, он передаст путь.

Дальше идут вопросы. У каждого первым стоит рекомендуемый вариант с пометкой `(Recommended)`, а
своё значение (имена людей, пути, даты) вписывается в поле Other. Пока вы отвечаете, ничего не
записывается. В конце скилл показывает таблицу «вопрос → значение», применяет ответы и запускает
проверку `doctor`.

Так выглядит итог первого запуска в режиме self:

```text
создал: .sessionretro/config.json
создал: .sessionretro/people/_default.yaml
создал: .sessionretro/plugin-state.json
создал: me
создал: .gitignore
создал: .claude/agents/sessionretro-log-collector.md
создал: .claude/agents/sessionretro-log-judge.md
создал: .claude/agents/sessionretro-feedback-critic.md
совет: Claude Code удаляет логи сессий старше cleanupPeriodDays дней (сейчас: не задано, по умолчанию 30); чтобы логи дожили до разбора, поставьте 90 в ~/.claude/settings.json
Открой новую сессию Claude Code в этом проекте, напиши что-нибудь, выйди `/exit`, через минуту проверь `sr.py doctor`: last_collect_at должен обновиться
```

Строки `создал`, `изменил`, `без изменений` перечисляют файлы и поля, `совет` — что стоит сделать
руками. Последняя строка появляется, когда включён хук: это инструкция его проверки (ниже).

## Три режима

| Режим | Кто разбирает | Люди | Текст человеку |
|---|---|---|---|
| `self` | вы себя | одна карточка `me`, создаётся сама | готов сразу |
| `team` | тимлид или ревьюер — команду | карточка на каждого участника | уходит только после одобрения ревьюера |
| `course` | преподаватель — учебную группу | карточка на каждого участника группы | уходит только после одобрения преподавателя |

В team и course логи каждого человека обычно лежат у него самого, поэтому источники логов
задаются на человека, а не на весь проект. Режим можно сменить повторным запуском: прошлые
прогоны и отчёты останутся, в выводе будет совет проверить группы и карточки.

## Вопросы по порядку

1. **Режим** — self, team или course (учебная группа). Обязателен на первом запуске.
2. **Участники** — только в team и course. По строке на человека: `id; Имя; группа; логи`.
   `id` — латиница в нижнем регистре, цифры и дефис (`alex`, `maria-k`). Логи — ссылка на
   git-репозиторий, путь к `.specstory/history` или любая папка с логами; можно оставить пустым.
   Пример: `alex; Alex; team-a; https://example.com/team/alex-logs.git`.
3. **Источники проекта** — откуда брать логи без привязки к человеку. В self рекомендуется
   «Claude Code этого проекта» (плюс SpecStory, если найдена `.specstory/history`); скилл
   подскажет, сколько сессий Claude Code уже нашёл. В team и course рекомендуется «только
   источники людей»: общий источник без владельца при двух и больше карточках сбор не примет.
4. **SpecStory** — задаётся, только если SpecStory не установлена или старее 2.6.0. Скилл
   покажет команды установки, запускаете их вы. Без SpecStory логи Claude Code читаются напрямую;
   она нужна для Codex, Cursor и других агентов.
5. **Хук** — собирать логи самим в конце каждой сессии Claude Code или вручную (ниже).
6. **Модели и effort** — какие модели у трёх агентов: коллектор читает логи пачками, судья ставит
   оценки и пишет текст, критик проверяет текст. Пресеты:

   | Пресет | Коллектор | Судья | Критик | Когда |
   |---|---|---|---|---|
   | Сбалансировано (рекомендуется) | sonnet / medium | opus / high | sonnet / medium | сильная модель там, где решается оценка; средняя цена |
   | Экономно | haiku / low | sonnet / medium | haiku / low | в разы дешевле, но цитат и тонких мест меньше |
   | Максимум | opus / high | opus / max | opus / medium | лучшее качество, самая высокая цена и самый долгий прогон |
   | Свой | | | | в Other: `роль=модель/effort` через запятую, например `judge=opus/max` |

   Модели: `sonnet`, `opus`, `haiku`, `inherit`; effort: `low`, `medium`, `high`, `max`. По этому
   ответу в проекте пишутся проектные агенты — о них в [customize.md](customize.md).
7. **Баллы** — показывать ли баллы по осям в личных отчётах. По умолчанию нет: человек видит
   текст, а не цифры.
8. **Периоды** — как делить работу на периоды: по дате запуска (каждый прогон — свой период),
   окнами дат (`sprint-1: 2026-09-01 — 2026-09-14`) или метками, которые вы называете при ретро.
9. **Шаблон текста** — шаблон плагина или своя копия в `.sessionretro/templates/feedback.md`
   для правки (как подключить её к текстам — в [customize.md](customize.md)).
10. **Срок хранения логов** — задаётся, только если `cleanupPeriodDays` в
    `~/.claude/settings.json` меньше 90 или не задан. Подробно ниже.
11. **.gitignore** — дописать в `.gitignore` проекта строки `.sessionretro/logs/`,
    `.sessionretro/runs/pending/`, `.sessionretro/reports/`, `.sessionretro/collect.log`,
    `.sessionretro/collect.lock`: в логах ваш код и переписка, черновики и отчёты — личные
    тексты, журнал и замок сбора — служебные файлы машины.

Язык (`ru` или `en`) скилл берёт из языка разговора, название workspace — из имени папки
проекта, если вы не назвали его сами.

## Повторный запуск

Вызовите `/sessionretro:setup` ещё раз. Скилл покажет текущие значения таблицей и спросит, что
поменять; можно выбрать несколько блоков:

- режим, люди и источники (вопросы 1–4);
- хук, cleanup, gitignore (вопросы 5, 10, 11);
- модели (вопрос 6);
- баллы, периоды, шаблон (вопросы 7–9).

Задаются только вопросы выбранных блоков, первым вариантом идёт текущее значение. Применяются
только изменённые ответы, остальное не трогается. Ничего не поменяли — скилл так и скажет и
покажет `doctor`.

Существующие карточки людей повторный запуск не перезаписывает, только добавляет новых. Имя,
группу или профиль человека правьте в файле `.sessionretro/people/<id>.yaml` — см.
[customize.md](customize.md).

## Хук SessionEnd и его проверка

Хук запускает сбор логов в фоне, когда сессия Claude Code заканчивается: к ретро логи уже на
месте. Сессия сбора не ждёт, а вывод и ошибки пишутся в `.sessionretro/collect.log`. Включается
ответом «Автоматически» на вопрос 5 (в конфиге — `hook.enabled`).

Проверьте, что хук срабатывает. Команда настройки печатает инструкцию; вот она дословно:

```text
Открой новую сессию Claude Code в этом проекте, напиши что-нибудь, выйди `/exit`, через минуту проверь `sr.py doctor`: last_collect_at должен обновиться
```

То есть откройте новую сессию, напишите любое сообщение, выйдите командой `/exit` и через минуту
запустите:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" doctor
```

В строке `last_collect_at` должно стоять только что прошедшее время. Если там `нет` или старая
дата — загляните в `.sessionretro/collect.log`: там строка `hook session=…` на каждый запуск и
текст ошибки, если сбор упал. Хук срабатывает только в проектах с `.sessionretro/config.json`.

## Ручной режим

Хук не обязателен. Ответьте «Вручную» на вопрос 5 (`"hook": false`) и собирайте логи, когда
нужно, — скиллом `/sessionretro:collect` или командой:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" collect
```

Подробно о сборе, источниках и санитайзере — в [collect.md](collect.md).

## Срок хранения логов: `cleanupPeriodDays`

Claude Code удаляет свои логи сессий старше `cleanupPeriodDays` дней (по умолчанию 30). Лог,
удалённый до сбора, в ретро не попадёт. Поэтому скилл предлагает поставить 90 в
`~/.claude/settings.json`:

- ответили «да» — `setup-apply` допишет ключ `cleanupPeriodDays`, остальные настройки файла
  сохранятся;
- ответили «нет» — файл не трогается, в выводе будет совет с текущим значением.

Файл `~/.claude/settings.json` не разбирается как JSON — команда остановится с кодом `1` и ничего
не запишет: почините файл и повторите. Текущее значение показывают `setup-state` и `doctor` (строка
`cleanup_period_days`). Копии, которые уже сделал `collect`, лежат в workspace и после удаления
исходников.

## Без диалога: `setup-apply --answers`

Для скриптов, CI и повторяемой настройки ответы можно передать сразу JSON-объектом — из файла или
через stdin (`-`):

```bash
cd ~/work/my-app
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" setup-apply --answers - <<'EOF'
{
  "mode": "team",
  "title": "Platform team",
  "lang": "ru",
  "people": [
    {"id": "alex", "name": "Alex", "group": "team-a",
     "sources": [{"type": "git", "url": "https://example.com/team/alex-logs.git"}]},
    {"id": "sam", "name": "Sam", "group": "team-b",
     "sources": [{"type": "dir", "path": "/home/user/shared/sam-logs"}]}
  ],
  "sources": [],
  "hook": true,
  "models": {"collector": {"model": "sonnet", "effort": "medium"},
             "judge": {"model": "opus", "effort": "high"},
             "critic": {"model": "sonnet", "effort": "medium"}},
  "show_scores": false,
  "period": {"mode": "dates", "windows": [{"label": "sprint-1", "from": "2026-09-01", "to": "2026-09-14"}]},
  "template": "default",
  "cleanup_period_days": {"value": 90, "write": false},
  "gitignore": true
}
EOF
```

Или из файла:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" setup-apply --answers answers.json
```

Все ключи необязательны, кроме `mode` на первом запуске. На повторном меняются только
переданные ключи, например только модель судьи:

```json
{"models": {"judge": {"model": "opus", "effort": "max"}}}
```

| Ключ | Значение |
|---|---|
| `mode` | `self` · `team` · `course` |
| `title` | название workspace; по умолчанию имя папки проекта |
| `lang` | `ru` · `en` |
| `path` | папка проекта; по умолчанию корень git-репозитория от текущей папки, иначе сама папка |
| `people` | список `{"id", "name", "group", "sources"}`; обязателен только `id`; существующие карточки не перезаписываются |
| `sources` | источники без владельца: `claude-jsonl`, `specstory`, `dir`, `git` (как в [collect.md](collect.md)); заменяют прежние источники без владельца, источники людей остаются |
| `hook` | `true` · `false` |
| `models` | по ролям `collector`, `judge`, `critic`: `{"model", "effort"}`; можно одну роль |
| `show_scores` | `true` · `false` |
| `period` | `{"mode": "run"}`, `{"mode": "labels"}` или `{"mode": "dates", "windows": [{"label", "from", "to"}]}`; границы — даты `ГГГГ-ММ-ДД` (или ISO-время), `from` не позже `to`, пустая граница — `null`, метки окон не повторяются |
| `template` | `default`, `workspace` или имя существующего файла в `.sessionretro/templates/`; значение попадает и в `feedback.template` файла `people/_default.yaml`, по нему строится текст. Карточки со своим `feedback.template` не меняются — их id придут советом |
| `cleanup_period_days` | `{"value": <дней>, "write": true\|false}`; `true` записывает `~/.claude/settings.json` (в цель, если это симлинк), но срок не понижает: стоит больше — файл не трогается, придёт совет |
| `gitignore` | `true` дописывает недостающие строки в `.gitignore` |

Проект не в текущей папке — ключ `path` в ответах или глобальный флаг `--workspace`. Заданы оба и
указывают на разные папки — код `2`, в ошибке оба пути:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" --workspace ~/work/my-app setup-apply --answers answers.json
```

`--json` печатает отчёт одним объектом: `root`, списки `created`, `changed`, `unchanged`,
`advice` и `hook_check` (инструкция проверки хука или `null`).

Коды выхода: `0` — применено; `2` — ответы не прошли проверку, в тексте ошибки имя поля, ничего
не записано; `1` — данные (например, битый `~/.claude/settings.json`). Пример отказа:

```text
sessionretro: setup: ответ mode: допустимо self | team | course (получено: 'sel')
```

## Текущие значения: `setup-state`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" setup-state
```

```text
workspace: /home/user/work/my-app
mode: self · lang: ru · people: 1
specstory_dir: нет
claude_projects_dir: нет
jsonl_files: 0
specstory_version: 2.6.0 (SpecStory)
cleanup_period_days: нет
```

С `--json` команда печатает `workspace` — текущие значения в формате ответов `setup-apply` — и
`found` — что найдено в окружении: `specstory_dir` (есть ли `.specstory/history`),
`claude_projects_dir` и `jsonl_files` (папка и число логов Claude Code этого проекта),
`specstory_version` (`null` — не установлена), `cleanup_period_days` (`null` — не задан). Вне
workspace `workspace` равен `null`, код выхода `0`. Объект `workspace` из вывода можно отдать
`setup-apply` как есть, чтобы повторить настройки этого проекта. Чтобы перенести их в другой
проект, удалите из JSON ключ `path` (в нём папка исходного проекта) и запустите `setup-apply` с
`--workspace <другой проект>`.

## Что показывает `doctor`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" doctor
```

Базовые строки описаны в [workspace.md](workspace.md). После настройки в выводе есть ещё:

```text
cleanup_period_days: не задан
improvements_pending: 0
sources_found: 1/2 папок найдено; нет: .specstory/history
project_agents: 3/3 совпадают с config.models
gitignore: да
show_scores: нет
period_mode: run
template: default
```

| Строка | Что значит |
|---|---|
| `cleanup_period_days` | сколько дней Claude Code хранит логи сессий; «не задан» — 30 по умолчанию |
| `improvements_pending` | сколько ваших предложений авторам плагина записано и ещё не отправлено — см. [feedback.md](feedback.md) |
| `sources_found` | у скольких локальных источников (`claude-jsonl`, `specstory`, `dir`) есть папка и каких нет; источники `git` не проверяются. Папки нет, пока не было первой сессии или выгрузки, — это не ошибка |
| `project_agents` | проектные агенты: `3/3 совпадают с config.models` — всё хорошо; `нет` — файлов нет, работают агенты плагина; `расходятся: judge` — сверьте командой `agents check` ([customize.md](customize.md)) |
| `gitignore` | все ли три строки sessionretro есть в `.gitignore` проекта |
| `show_scores` | показываются ли баллы в личных отчётах |
| `period_mode` | как делятся периоды: `run`, `dates` или `labels` |
| `template` | какой шаблон текста выбран при настройке |

## Подробнее

- [Настройка под себя: карточки, шаблон текста, проектные агенты](customize.md)
- [Предложения авторам плагина](feedback.md)
- [Сбор логов](collect.md)
- [Workspace: где sessionretro хранит данные](workspace.md)
