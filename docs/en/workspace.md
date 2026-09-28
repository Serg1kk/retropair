# Workspace: where sessionretro keeps your data

sessionretro never writes inside the plugin itself. Everything you set up and everything the plugin
collects lives in two places:

- **the project workspace**: the `.sessionretro/` folder in the root of your project. It holds the
  config, person cards, logs, reviews and reports of this project;
- **the global folder** `~/.sessionretro/` in your home directory. It holds the registry of all your
  workspaces and shared defaults.

Plugin updates do not touch these folders: your cards, settings and history stay as they are.

## Commands

All commands go through one entry point, `sr.py`. Inside Claude Code the path to the plugin comes
from the `${CLAUDE_PLUGIN_ROOT}` variable:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" --help
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" --version
```

You need python 3.10 or newer; no third-party packages.

Commands find the workspace themselves: from the current folder upwards to the first
`.sessionretro/config.json`. To name the project explicitly, add `--workspace <folder>` anywhere in
the command (both the project root and the `.sessionretro` folder itself work):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people list --workspace ~/work/my-app
```

Exit codes are the same for every command: `0` all good, `1` the data did not add up or failed a
check, `2` a wrong call (no workspace, unknown person, a typo in the arguments). An error is printed
as one line: `sessionretro: <what failed>: <why> (<parameters>)`.

Most command output is in Russian in version 0.1; the examples below show it as printed.

### `init`: create a workspace

```bash
cd ~/work/my-app
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" init --mode self
```

Flags:

| Flag | What it sets | Default |
|---|---|---|
| `--mode self\|team\|course` | the mode: I review myself / a reviewer looks at a team / a teacher looks at a class | required |
| `--title "…"` | the workspace title; the id is derived from it | the project folder name |
| `--lang ru\|en` | the language of texts and reports | `ru` |
| `--path <folder>` | the project folder | the current folder |

What appears:

- `.sessionretro/config.json`: workspace settings;
- `.sessionretro/people/_default.yaml`: shared text settings for all cards;
- `.sessionretro/plugin-state.json`: a service version stamp;
- in `self` mode also the card `.sessionretro/people/me.yaml` (that is you);
- an entry for the project in `~/.sessionretro/workspaces.json`.

An example for a team:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" init --mode team --title "Platform team" --lang en
```

```text
workspace создан: /home/user/work/my-app/.sessionretro
id: platform-team · режим: team · язык: en
```

(`workspace создан` is "workspace created", `режим` is mode, `язык` is language.) Cyrillic in the
title is transliterated into the id. If the folder already has a workspace, `init` does not
overwrite it and exits with code `1`. `init` does not change the project's `.gitignore` (see "What
to commit to git" below).

In any mode `init` puts two shared sources without an owner into `config.json`: SpecStory and
Claude Code of this project. In team and course, as soon as you add a second card, `collect` stops
on them with code `1`: remove them from `sources` or give each of them a `"person"`; details in
[collect.md](collect.md#where-the-logs-come-from).

### `people`: person cards

A card is the file `.sessionretro/people/<id>.yaml`: who the person is and how to write the text to
them. The id is lower-case Latin letters, digits and hyphens (`alex`, `maria-k`, `dev01`).

**Add a person:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people add alex --name "Alex" --group backend
```

```text
карточка создана: /home/user/work/my-app/.sessionretro/people/alex.yaml
группа backend добавлена в workspace.groups
```

(The card is created, and the group `backend` is added to `workspace.groups`.) Without `--name`
the name is the id, without `--group` the first group of the workspace (after `init` that is
`default`). A new group is added to `config.json` by itself. If a card with this id already exists,
the code is `1` and the existing file is not overwritten.

**Show a card:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people show alex
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people show alex --json
```

Without a flag the card file is printed as is. With `--json`, the resulting card the plugin works
with: your card plus the missing text settings from `_default.yaml`.

**List people:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people list
```

```text
alex · Alex · backend
sam · Sam · default
```

`--json` returns the same resulting cards as an array.

**What the judge model will see:**

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people context alex
```

```text
профиль: backend developer, middle, 4 года; стек: Python, Postgres; инструменты: Claude Code
ограничения: нет прав на прод
обращение: ты; длина: detailed; тон: direct
открывать обобщением: нет; подписывать разделы: нет; приёмы исправления: да
не объяснять: что такое git
границы: нет
ценность: finding; динамика: да
```

This is a short digest of the card (5–8 lines) that the judge gets together with the logs: profile,
constraints, form of address, length, tone, whether to open with a summary, label sections and give
fixing techniques, what not to explain, boundaries, value focus and dynamics. Empty profile items
are skipped. The name and the text layout (`channel`, `template`, `prefix`) are not part of the
digest. `--json` returns the same lines as an array.

An unknown id or an id in the wrong format gives code `2`.

### `doctor`: check that everything is in place

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" doctor
```

```text
workspace: /home/user/work/my-app
mode: team
people: 2
python: 3.12.4
specstory: 2.6.0 (SpecStory)
hook_enabled: да
cleanup_period_days: не задан
registry_count: 1
last_run_at: нет
last_collect_at: нет
plugin_version: 0.1.0
```

| Line | What it means |
|---|---|
| `workspace` | the project found, or `не найден` (not found) |
| `mode`, `people` | the workspace mode and the number of cards |
| `python` | the interpreter version |
| `specstory` | the SpecStory CLI version or `не найден` (not found); waits up to 5 seconds for an answer |
| `hook_enabled` | whether the automatic log snapshot at the end of a Claude Code session is on (`да` yes, `нет` no) |
| `cleanup_period_days` | how many days Claude Code keeps session logs (`~/.claude/settings.json`); `не задан` means not set |
| `registry_count` | how many workspaces are in the global registry |
| `last_run_at`, `last_collect_at` | when the last assessment and the last log collection happened; `нет` means never |
| `plugin_version` | the plugin version |

`doctor` does not fail when something is missing: it honestly says not found or not set. Code `1`
comes only if some JSON file is broken (the config, the registry, `~/.claude/settings.json`) or a
card cannot be read. `--json` prints the same as one object. After `/sessionretro:setup` there are
more lines; see [setup.md](setup.md#what-doctor-shows).

## What is in `.sessionretro/`

```text
.sessionretro/
├── config.json              # workspace settings: mode, language, groups, log sources, models
├── people/
│   ├── _default.yaml        # shared text settings for all cards
│   └── <id>.yaml            # a person card (in self mode just one, me.yaml)
├── logs/<id>/…              # sanitized copies of session logs (raw logs are not kept)
├── logs-index.json          # inventory of collected sessions
├── runs/process-reviews.json    # assessment history by period
├── runs/pending/            # intermediate files of the current run
├── reviews/<id>/…           # review texts: draft → approved
├── reports/                 # HTML reports
├── templates/feedback.md    # your text template (optional)
├── improvements/            # your proposals for improving the plugin (optional)
├── agents.json              # fingerprints of project agents: whether you edited them
├── collect.log              # background collection log (written by the hook)
├── collect.lock             # collection lock
└── plugin-state.json        # service stamp: format and plugin version
```

`agents.json` appears when `setup` or `agents write` put the project agents into
`.claude/agents/`. For each role it holds a fingerprint of the agent body and the plugin version.
From it the plugin tells whether you edited the agent file: if not, the file updates itself after a
plugin update; if you did, it stays as is. Details in [customize.md](customize.md), "After a plugin
update". The file can be committed together with `.claude/agents/`.

Right after `init` there are only `config.json`, `people/` and `plugin-state.json`; the rest
appears as you collect logs, run assessments and build reports.

## What is in `~/.sessionretro/`

```text
~/.sessionretro/
├── workspaces.json          # workspace registry: id, path, title, mode, groups, dates
├── config.json              # your shared defaults (models, language)
├── reports/index.html       # the page with all workspaces
├── clones/                  # clones of git repositories with logs (team and course modes)
└── plugin-state.json        # service stamp
```

The registry lets you see all projects from one page. It is kept by the plugin commands (`init`
adds an entry); you do not need to edit the file by hand.

To look at the registry and remove what is no longer needed (for example, a deleted sandbox), use
the `workspaces` command. `list` shows the id, path, title and mode and marks an entry `папки нет`
(folder missing) if its `.sessionretro/config.json` is gone; `remove <id>` removes one entry,
`remove --missing` all marked ones. Project files are not touched; the workspaces page updates on
the next `report --index`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" workspaces list
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" workspaces remove --missing
```

## The person card

```yaml
id: alex
name: "Alex"
group: backend
profile:
  role: backend developer
  grade: middle
  years: 4
  stack: Python, Postgres
  tools: Claude Code
  constraints: no access to production
feedback:
  address: you
  skip_basics:
    - what git is
sources:
consent:
  logs: true
  date: 2026-09-01
```

- `id` matches the file name; `name` is the name for the text; `group` is a group from
  `config.json`.
- `profile`: role, grade, years of experience, stack, tools, constraints. All optional, but the
  fuller it is, the more precise the advice: the judge will not suggest what the person cannot use.
- `feedback`: how to write the text to this person (below). List only what differs from
  `_default.yaml`; the rest is filled in automatically.
- `sources`: additional log sources of this person, for example a folder (`type: dir`,
  `path: …`).
- `consent`: consent to log collection and its date (for team and course modes). `logs: false`
  blocks both collection and assessment: the person's sources are not read, and snapshots collected
  earlier are excluded from batches (details in [collecting logs](collect.md)).

### Text settings: `feedback`

Eleven parameters set the content and manner of the text; the judge sees them. Three more only
affect the layout and are not given to the judge.

| Parameter | Values | What it changes |
|---|---|---|
| `address` | `ты` · `вы` · `you` | how to address the person; `ты` and `вы` are the informal and formal Russian "you", `you` is for English texts |
| `length` | `short` · `detailed` | length of the text: short or detailed |
| `tone` | `direct` · `supportive` | tone: direct or supportive |
| `opening_summary` | `true` · `false` | whether to open the text with a plain-language summary |
| `mark_sections` | `true` · `false` | whether to label the "what to change" and "what holds" parts |
| `practices` | `true` · `false` | whether to give "how to fix it" techniques for weak spots |
| `skip_basics` | list | what the person already knows; it is not explained |
| `boundaries` | list | hard limits: for example, do not carry advice over to the work stack, or compliance requirements |
| `avoid` | list | what not to do in the text (comparisons, slang, emoji…) |
| `value_focus` | `finding` · `full` | where the value of the text lies: in findings the person did not see, or in a full pass over all axes |
| `show_dynamics` | `true` · `false` | whether to show changes since the previous period |
| `channel` | `file` · `telegram` · `slack` · `email` | where the text goes; only a label for now (layout): in 0.1.13 the text is built the same way for every channel |
| `template` | `default` · `workspace` · file name | which text template to use (layout) |
| `prefix` | string | what to put before the text, for example a greeting (layout) |

Lists are written one item per line:

```yaml
feedback:
  boundaries:
    - do not suggest cloud services
    - do not touch production
```

To change a setting for everyone at once, edit `people/_default.yaml`. For one person, uncomment the
`feedback:` block in their card and keep the lines you need.

The card is validated on every read. An unknown value (for example `tone: soft`) or an id that does
not match the file name is an error with code `1` that names the exact field.

## What to commit to git

The workspace is meant as a "feedback folder in the repository": settings, cards, the assessment
history and review texts are good to keep with the project. Do not commit what is heavy or
intermediate:

```gitignore
.sessionretro/logs/
.sessionretro/runs/pending/
.sessionretro/reports/
.sessionretro/collect.log
.sessionretro/collect.lock
```

- `logs/`: copies of session logs. They are cleaned of secrets and paths, but they are still your
  conversation with the agent;
- `runs/pending/`: draft files of an unfinished run;
- `reports/`: HTML that can always be rebuilt;
- `collect.log` and `collect.lock`: the background collection log and its lock, machine service
  files.

`~/.sessionretro/` is your personal folder; it does not go into the project repository.

Cards in team and course modes hold people's names and profiles. Before committing them to a shared
repository, make sure the people agree. What else stays with you, what the models see and what the
sanitizer misses: [privacy.md](privacy.md).

## More

- [Privacy](privacy.md): where the data lives, what the models see, what goes over the network.
- [Reports: what each one shows](reports.md): group, reviewer, personal and the page
  of all workspaces.
- [How state is stored](../../STATE.md) (Russian): rules for those who work on the plugin.
- [Setup: first and repeated run](setup.md): the questions, the hook, `setup-apply` for scripts.
- [Customizing](customize.md): cards and `_default.yaml`, your own text template, project agents.
- [Proposals to the plugin authors](feedback.md): what goes into `improvements/` and how to send it.
