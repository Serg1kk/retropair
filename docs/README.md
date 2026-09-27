# sessionretro documentation

The plugin overview, quick start and privacy summary are in the [README](../README.md)
([на русском](../README.ru.md)). The full documentation is in Russian; the three pages most
needed to decide whether to use the plugin are also in English.

## English

| Page | What it covers |
|---|---|
| [How the assessment works](en/how-it-works.md) | the eight axes, two stages, batches, scores only under quotes, insufficient data, the level formula, what the person sees, how well it works |
| [Privacy](en/privacy.md) | where data lives, what the models see, what goes over the network, the sanitizer and its limits |
| [Customizing](en/customize.md) | person cards, `_default.yaml`, your own text template, project agents with their models and effort |

## Русский

| Страница | О чём |
|---|---|
| [Настройка](ru/setup.md) | первый и повторный запуск `/sessionretro:setup`, три режима, вопросы по порядку, хук, `setup-apply` для скриптов |
| [Сбор логов](ru/collect.md) | источники, сводка сбора, статусы сессий, санитайзер, хук SessionEnd, ручной режим, Windows |
| [Как устроена оценка](ru/how-it-works.md) | две ступени, пачки, оценка только под цитату, «мало данных», формула уровня, рекомендации |
| [Восемь осей](ru/axes.md) | что смотрится по каждой оси, гейт, уровни, антипаттерны, что видит человек |
| [Ревью периода](ru/review.md) | как запускать, аргументы, что происходит, черновик и одобрение, коды выхода |
| [Отчёты](ru/reports.md) | отчёт ревьюера, групповой, личный, страница всех workspace, защита от чужих имён, `--anon` |
| [Настройка под себя](ru/customize.md) | карточки, `_default.yaml`, свой шаблон текста, проектные агенты |
| [Workspace](ru/workspace.md) | где лежат данные, команды `init`, `people`, `doctor`, карточка человека, что коммитить |
| [Предложения авторам](ru/feedback.md) | что записывается в `improvements/`, как собрать пакет и отправить issue, Discussions |
| [Приватность](ru/privacy.md) | где лежат данные, что видят модели, что уходит в сеть, чего санитайзер не ловит |
| [Частые вопросы](ru/faq.md) | стоимость, точность, агенты, Windows, пропавшие сессии, обновление и удаление |

## For contributors

[CONTRIBUTING.md](../CONTRIBUTING.md), [MAINTAINERS.md](../MAINTAINERS.md),
[STATE.md](../STATE.md), [TESTING.md](../TESTING.md), [CHANGELOG.md](../CHANGELOG.md),
[ROADMAP.md](../ROADMAP.md).
