# sessionretro documentation

The plugin overview, quick start and privacy summary are in the [README](../README.md)
([на русском](../README.ru.md)). Every page exists in English and in Russian.

## English

| Page | What it covers |
|---|---|
| [Setup](en/setup.md) | first and repeated run of `/sessionretro:setup`, three modes, the questions in order, the hook, `setup-apply` for scripts |
| [Collecting logs](en/collect.md) | sources, the collection summary, session statuses, the sanitizer, the SessionEnd hook, manual mode, Windows |
| [How the assessment works](en/how-it-works.md) | the eight axes, two stages, batches, scores only under quotes, insufficient data, the level formula, what the person sees, how well it works |
| [The eight axes](en/axes.md) | what each axis looks at, the gate, levels, anti-patterns, what the person sees |
| [Reviewing a period](en/review.md) | how to run it, arguments, what happens, draft and approval, exit codes |
| [Customizing](en/customize.md) | person cards, `_default.yaml`, card suggestions, your own text template, project agents with their models and effort |
| [Workspace](en/workspace.md) | where the data lives, the `init`, `people`, `doctor` commands, the person card, what to commit |
| [Proposals to the authors](en/feedback.md) | what goes into `improvements/`, how to build a pack and open an issue, Discussions |
| [Privacy](en/privacy.md) | where data lives, what the models see, what goes over the network, the sanitizer and its limits |
| [FAQ](en/faq.md) | cost, accuracy, agents, Windows, missing sessions, updating and removing |
| [Reports](en/reports.md) | the reviewer report, the group report with the person card (`--no-details`, `group_details`), the personal report and its two views, what only the reviewer sees, the workspaces page, the guard against other people's names, `--anon`, with screenshots |

## Русский

| Страница | О чём |
|---|---|
| [Настройка](ru/setup.md) | первый и повторный запуск `/sessionretro:setup`, три режима, вопросы по порядку, хук, `setup-apply` для скриптов |
| [Сбор логов](ru/collect.md) | источники, сводка сбора, статусы сессий, санитайзер, хук SessionEnd, ручной режим, Windows |
| [Как устроена оценка](ru/how-it-works.md) | две ступени, пачки, оценка только под цитату, «мало данных», формула уровня, рекомендации |
| [Восемь осей](ru/axes.md) | что смотрится по каждой оси, гейт, уровни, антипаттерны, что видит человек |
| [Ревью периода](ru/review.md) | как запускать, аргументы, что происходит, черновик и одобрение, коды выхода |
| [Отчёты](ru/reports.md) | отчёт ревьюера, групповой с карточкой человека (`--no-details`, `group_details`), личный и два его вида, что видит только ревьюер, вкладка «все», страница всех workspace, защита от чужих имён, `--anon`, со скриншотами |
| [Настройка под себя](ru/customize.md) | карточки, `_default.yaml`, свой шаблон текста, проектные агенты |
| [Workspace](ru/workspace.md) | где лежат данные, команды `init`, `people`, `doctor`, карточка человека, что коммитить |
| [Предложения авторам](ru/feedback.md) | что записывается в `improvements/`, как собрать пакет и отправить issue, Discussions |
| [Приватность](ru/privacy.md) | где лежат данные, что видят модели, что уходит в сеть, чего санитайзер не ловит |
| [Частые вопросы](ru/faq.md) | стоимость, точность, агенты, Windows, пропавшие сессии, обновление и удаление |

## For contributors

[CONTRIBUTING.md](../CONTRIBUTING.md), [MAINTAINERS.md](../MAINTAINERS.md),
[STATE.md](../STATE.md), [TESTING.md](../TESTING.md), [CHANGELOG.md](../CHANGELOG.md),
[ROADMAP.md](../ROADMAP.md).
