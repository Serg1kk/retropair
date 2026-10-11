# Setup: how to connect RetroPair to a project

Upgrading from SessionRetro? Follow the [migration guide](migration.md) before initializing a new workspace.

To start working in a project, RetroPair needs a workspace: the `.retropair/` folder with
the settings, person cards and a place for logs. The simplest way to create it is a dialogue: open
Claude Code in the project folder and call the skill.

```text
/retropair:setup
```

Plain words work too: "set up RetroPair", "connect the RetroPair". The skill asks up to 12
questions one at a time, shows a summary and applies the answers with one command. It writes no
files itself: everything is written by the plugin CLI `rp.py`, so you can get the same result
without a dialogue, from a script (see "Without a dialogue" below).

## First run

The skill first looks at what is already there: a workspace, Claude Code logs of this project, a
SpecStory folder, the log retention period. For that it runs:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" setup-state --json
```

It sets up the root of the git repository in which you opened Claude Code; if the folder is not in
git, the folder itself. Need a different project: tell the skill, and it passes the path on.

Then come the questions. Each one has the recommended option first, marked `(Recommended)`, and
your own value (people's names, paths, dates) goes into the Other field. Nothing is written while
you answer. At the end the skill shows a "question → value" table, applies the answers and runs the
`doctor` check.

This is what the result of a first run in self mode looks like (a workspace with `lang: en`):

```text
created: .retropair/config.json
created: .retropair/people/_default.yaml
created: .retropair/plugin-state.json
created: me
created: .gitignore
created: .claude/agents/retropair-log-collector.md
created: .claude/agents/retropair-log-judge.md
created: .claude/agents/retropair-feedback-critic.md
advice: Claude Code deletes session logs older than cleanupPeriodDays days (now: not set, default 30); to keep logs until the review, set 90 in ~/.claude/settings.json
Open a new Claude Code session in this project, type anything, exit with `/exit`, and a minute later run `rp.py doctor`: last_collect_at should be updated
```

The `created`, `changed` and `unchanged` lines list files and fields, `advice` says what is worth
doing by hand. The last line appears when the hook is on: it is the instruction for checking it
(below). With `lang: ru` the same lines are printed in Russian.

## Three modes

| Mode | Who reviews | People | Text to the person |
|---|---|---|---|
| `self` | you review yourself | one card `me`, created automatically | ready at once |
| `team` | a team lead or reviewer reviews the team | a card for each member | goes out only after the reviewer approves it |
| `course` | a teacher reviews a class | a card for each member of the class | goes out only after the teacher approves it |

In team and course each person's logs usually live with that person, so log sources are set per
person, not for the whole project. You can change the mode by running setup again: earlier runs
and reports stay, and the output advises you to check the groups and cards.

## The questions in order

1. **Mode**: self, team or course (a class). Required on the first run.
2. **Members**: team and course only. One line per person: `id; Name; group; logs`. The `id` is
   lower-case Latin letters, digits and hyphens (`alex`, `maria-k`). Logs are a link to a git
   repository, a path to `.specstory/history` or any folder with logs; it can be left empty.
   Example: `alex; Alex; team-a; https://example.com/team/alex-logs.git`.
3. **Project sources**: where to take logs from without tying them to a person. In self the
   recommendation is "Claude Code of this project" (plus SpecStory if `.specstory/history` is
   found); the skill tells you how many Claude Code sessions it has already found. In team and
   course the recommendation is "people's sources only": with two or more cards, collection does
   not accept a shared source without an owner.
4. **SpecStory**: asked only if SpecStory is not installed or is older than 2.6.0. The skill shows
   the install commands, and you run them. Without SpecStory the Claude Code logs are read
   directly; it is needed for Codex, Cursor and other agents.
5. **Hook**: collect logs automatically at the end of every Claude Code session, or by hand (below).
6. **Models and effort**: which models the three agents use: the collector reads logs in batches,
   the judge scores and writes the text, the critic checks the text. Presets:

   | Preset | Collector | Judge | Critic | When |
   |---|---|---|---|---|
   | Balanced (recommended) | sonnet / medium | opus / high | sonnet / medium | a strong model where the score is decided; medium cost |
   | Economy | haiku / low | sonnet / medium | haiku / low | several times cheaper, but fewer quotes and subtle points |
   | Maximum | opus / high | opus / max | opus / medium | best quality, the highest cost and the longest run |
   | Custom | | | | in Other: `role=model/effort` separated by commas, for example `judge=opus/max` |

   Models: `sonnet`, `opus`, `haiku`, `inherit`; effort: `low`, `medium`, `high`, `max`. From
   this answer the project agents are written into the project; see [customize.md](customize.md).
7. **Scores**: the view of personal reports. Recommended: `true`, scores and the level, like the
   reviewer's card; `false` shows the dynamics only ↑ → ↓ ◇. The review text never has scores.
8. **Periods**: how to split the work into periods: by run date (each run is its own period
   labelled `run-YYYYMMDD`) or by date windows (`sprint-1: 2026-09-01 — 2026-09-14`). The config
   accepts a third option, `labels`, but in 0.2 the `review` skill does not ask for the label: runs
   are recorded without a period label. If you need your own period names, set them as date
   windows.
9. **Text template**: the plugin template or your own copy in `.retropair/templates/feedback.md`
   to edit (how to connect it to the texts: [customize.md](customize.md)).
10. **Log retention**: asked only if `cleanupPeriodDays` in `~/.claude/settings.json` is below 90
    or not set. Details below.
11. **.gitignore**: add the lines `.retropair/logs/`, `.retropair/runs/pending/`,
    `.retropair/reports/`, `.retropair/collect.log`, `.retropair/collect.lock` to the
    project's `.gitignore`: the logs hold your code and conversation, drafts and reports are
    personal texts, and the collection log and lock are machine service files.

12. **Feedback language**: explicitly choose Russian (`ru`) or English (`en`) for future feedback
    and report interface strings. This is the workspace default; `profile.preferred_language` (`ru`/`en`) in a participant card
    overrides it for that person’s feedback and recommendations. Original log quotes
    remain verbatim. Existing reviews are not translated. Rebuild reports after changing the language.
    Custom templates keep their text: translate them yourself or select `default`.

The skill asks for the language instead of inferring it from your conversation. It takes the
workspace title from the project folder name unless you name it yourself.

## Running it again

Call `/retropair:setup` again. The skill shows the current values as a table and asks what to
change; you can pick several blocks:

- mode, people and sources (questions 1–4);
- hook, cleanup, gitignore (questions 5, 10, 11);
- models (question 6);
- scores, periods, template and language (questions 7–9, 12).

Only the questions of the chosen blocks are asked, with the current value as the first option.
Only changed answers are applied; the rest is left alone. If nothing changed, the skill says so and
shows `doctor`.

A repeated run does not overwrite existing person cards, it only adds new ones. Edit a person's
name, group or profile in the file `.retropair/people/<id>.yaml`; see
[customize.md](customize.md).

## The SessionEnd hook and how to check it

The hook runs log collection in the background when a Claude Code session ends, so the logs are
already in place for the retro. The session does not wait for collection, and the output and errors
go to `.retropair/collect.log`. It is turned on by the automatic option in question 5
(`hook.enabled` in the config).

Check that the hook fires. The setup command prints an instruction; here it is verbatim:

```text
Open a new Claude Code session in this project, type anything, exit with `/exit`, and a minute later run `rp.py doctor`: last_collect_at should be updated
```

That is, open a new session, type any message, exit with `/exit` and a minute later run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" doctor
```

The `last_collect_at` line should show a time that just passed. If it shows `нет` (none) or an old
date, look into `.retropair/collect.log`: it has a `hook session=…` line for every run and the
error text if collection failed. The hook fires only in projects with `.retropair/config.json`.

## Manual mode

The hook is optional. Choose the manual option in question 5 (`"hook": false`) and collect logs
when you need to, with the `/retropair:collect` skill or the command:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" collect
```

Details on collection, sources and the sanitizer: [collect.md](collect.md).

## Log retention: `cleanupPeriodDays`

Claude Code deletes its session logs older than `cleanupPeriodDays` days (30 by default). A log
deleted before collection does not get into the retro. So the skill offers to set 90 in
`~/.claude/settings.json`:

- answer "yes" and `setup-apply` adds the `cleanupPeriodDays` key, keeping the file's other
  settings;
- answer "no" and the file is not touched; the output gives advice with the current value.

If `~/.claude/settings.json` does not parse as JSON, the command stops with code `1` and writes
nothing: fix the file and repeat. The current value is shown by `setup-state` and `doctor` (the
`cleanup_period_days` line). Copies already made by `collect` stay in the workspace after the
originals are deleted.

## Without a dialogue: `setup-apply --answers`

For scripts, CI and repeatable setup you can pass the answers at once as a JSON object, from a file
or through stdin (`-`):

```bash
cd ~/work/my-app
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" setup-apply --answers - <<'EOF'
{
  "mode": "team",
  "title": "Platform team",
  "lang": "en",
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

Or from a file:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" setup-apply --answers answers.json
```

All keys are optional except `mode` on the first run. On a repeated run only the keys you pass
change, for example only the judge model:

```json
{"models": {"judge": {"model": "opus", "effort": "max"}}}
```

| Key | Value |
|---|---|
| `mode` | `self` · `team` · `course` |
| `title` | the workspace title; defaults to the project folder name |
| `lang` | `ru` · `en`; defaults to `ru` |
| `path` | the project folder; defaults to the root of the git repository of the current folder, otherwise the folder itself |
| `people` | a list of `{"id", "name", "group", "sources"}`; only `id` is required; existing cards are not overwritten |
| `sources` | sources without an owner: `claude-jsonl`, `specstory`, `dir`, `git` (as in [collect.md](collect.md)); they replace the previous sources without an owner, people's sources stay. The `person` field is not accepted here (code `2`): a person's source goes into `people[].sources` |
| `hook` | `true` · `false` |
| `models` | per role `collector`, `judge`, `critic`: `{"model", "effort"}`; one role is enough |
| `show_scores` | `true` · `false` |
| `period` | `{"mode": "run"}`, `{"mode": "labels"}` or `{"mode": "dates", "windows": [{"label", "from", "to"}]}`; the bounds are `YYYY-MM-DD` dates (or ISO time), `from` no later than `to`, an open bound is `null`, window labels do not repeat |
| `template` | `default`, `workspace` or the name of an existing file in `.retropair/templates/`; the value also goes into `feedback.template` of `people/_default.yaml`, which the text is built from. Cards with their own `feedback.template` do not change; their ids come back as advice |
| `cleanup_period_days` | `{"value": <days>, "write": true\|false}`; `true` writes `~/.claude/settings.json` (to the target if it is a symlink), but never lowers the period: if it is already higher, the file is not touched and you get advice |
| `gitignore` | `true` adds the missing lines to `.gitignore` |

If the project is not the current folder, use the `path` key in the answers or the global
`--workspace` flag. If both are set and point to different folders, the code is `2` and the error
names both paths:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" --workspace ~/work/my-app setup-apply --answers answers.json
```

`--json` prints the report as one object: `root`, the lists `created`, `changed`, `unchanged`,
`advice` and `hook_check` (the hook check instruction or `null`).

Exit codes: `0` applied; `2` the answers failed validation, the error names the field, nothing is
written; `1` a data problem (for example, a broken `~/.claude/settings.json`). An example of a
rejection (error messages are in Russian in 0.2):

```text
retropair: setup: ответ mode: допустимо self | team | course (получено: 'sel')
```

That is: answer `mode`, allowed `self | team | course`, got `'sel'`.

## Current values: `setup-state`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" setup-state
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

(`нет` means "none".) With `--json` the command prints `workspace`, the current values in the
`setup-apply` answer format, and `found`, what was found in the environment: `specstory_dir`
(whether `.specstory/history` exists), `claude_projects_dir` and `jsonl_files` (the folder and the
number of Claude Code logs of this project), `specstory_version` (`null` means not installed),
`cleanup_period_days` (`null` means not set). Outside a workspace `workspace` is `null` and the
exit code is `0`. You can pass the `workspace` object from the output to `setup-apply` as is to
repeat this project's settings. To carry them over to another project, remove the `path` key from
the JSON (it holds the source project's folder) and run `setup-apply` with
`--workspace <other project>`.

## What `doctor` shows

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" doctor
```

The basic lines are described in [workspace.md](workspace.md). After setup the output has a few
more (the values are printed in Russian in 0.2):

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

| Line | What it means |
|---|---|
| `cleanup_period_days` | how many days Claude Code keeps session logs; `не задан` (not set) means the default 30 |
| `improvements_pending` | how many of your proposals to the plugin authors are recorded and not sent yet; see [feedback.md](feedback.md) |
| `sources_found` | how many local sources (`claude-jsonl`, `specstory`, `dir`) from the config and the `people/<id>.yaml` cards have a folder (`папок найдено`, folders found) and which do not (`нет`); `git` sources are not checked, only counted. A missing folder before the first session or export is not an error |
| `project_agents` | the project agents: `3/3 совпадают с config.models` (3/3 match config.models) means all is well; `нет` (none) means there are no files and the plugin agents run; `расходятся: judge` (differ: judge) means compare them with `agents check` ([customize.md](customize.md)) |
| `gitignore` | whether all five RetroPair lines from question 11 are in the project's `.gitignore`: `да` (yes) if all are; otherwise "no — add it: rp.py setup-apply with {"gitignore": true}" in the workspace language |
| `show_scores` | whether scores are shown in personal reports |
| `period_mode` | how periods are split: `run`, `dates` or `labels` (in 0.2, runs without a label) |
| `template` | which text template was chosen during setup |

## More

- [Customizing: cards, the text template, project agents](customize.md)
- [Proposals to the plugin authors](feedback.md)
- [Collecting logs](collect.md)
- [Workspace: where RetroPair keeps its data](workspace.md)
