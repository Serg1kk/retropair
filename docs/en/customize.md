# Customizing: person cards, the text template and project agents

After `/sessionretro:setup` everything works on defaults. This page is about what you can tune by
hand: how to write to a particular person, what the text itself looks like, and which models and
prompts the agents in your project use. All of these are files in your project; plugin updates
leave them alone.

To see which settings the plugin is working with right now:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people show me --json
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" agents check
```

## Person card

Each person has a file `.sessionretro/people/<id>.yaml` (in self mode, `me.yaml`). `setup` creates
the card; running it again does not overwrite it, so edit the file freely.

```yaml
id: "alex"
name: "Alex"
group: team-a
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
```

- `name` is how the text addresses the person; `group` is a team or learning group from
  `config.json`.
- `profile`: role, grade, years of experience, stack, tools, constraints. All optional, but the
  fuller it is, the more precise the advice: the judge will not suggest what the person cannot use.
- `feedback`: how to write the text to this person. List only what differs from the shared
  settings; the rest comes from `_default.yaml`.

The card is validated on every read: an unknown value (for example `tone: soft`) is an error with
code `1` and the field name. More on the card fields in [workspace.md](workspace.md).

## Shared text settings: `_default.yaml`

`.sessionretro/people/_default.yaml` sets the text settings for all cards of the workspace at once.
Change it here and it changes for everyone, except those who have their own value in the card.

| Field | Allowed values | Default | What it changes |
|---|---|---|---|
| `address` | `ты` · `вы` · `you` | `ты` | form of address; `you` is for English texts |
| `length` | `short` · `detailed` | `detailed` | length of the text |
| `tone` | `direct` · `supportive` | `direct` | tone: direct or supportive |
| `opening_summary` | `true` · `false` | `false` | whether to start with a plain-language summary |
| `mark_sections` | `true` · `false` | `false` | whether to label the "what to change" and "what holds" parts |
| `practices` | `true` · `false` | `true` | whether to give "how to fix it" techniques for weak axes |
| `skip_basics` | list of strings | empty | what the person already knows and needs no explanation |
| `boundaries` | list of strings | empty | hard limits: do not carry advice over to the work stack, compliance requirements |
| `avoid` | list of strings | empty | what not to do in the text: comparisons, slang, emoji |
| `value_focus` | `finding` · `full` | `finding` | value in findings the person did not see, or a full pass over all axes |
| `show_dynamics` | `true` · `false` | `true` | whether to show changes since the previous period |
| `channel` | `file` · `telegram` · `slack` · `email` | `file` | where the text goes; only a label for now: stored and validated, but in 0.1.13 the text is built the same way for every channel |
| `template` | `default` · `workspace` · file name | `default` | which text template to use (below) |
| `prefix` | string | `""` | what to put before the text, for example a greeting |
| `show_scores` | `true` · `false` | none (follows `show_scores` in `config.json`) | the view of the personal report: scores and level, or dynamics only ↑ → ↓ ◇; the `report --scores`/`--trend` flag wins; a line in `_default.yaml` applies to everyone and beats `config.json`. Not given to the judge |

For an English workspace set `address: you` in `_default.yaml` and `lang: en` in `config.json`.
Lists are written one item per line:

```yaml
feedback:
  address: you
  tone: supportive
  boundaries:
    - do not suggest cloud services
    - do not touch production
```

To override a setting for one person, uncomment the `feedback:` block in their card and keep only
the lines you need.

## The card is updated through suggestions

After an approved review, sessionretro compares the card with what the logs show and collects
suggested edits. It never changes the card itself: an edit reaches the file only after your
`people apply` command. Scores, levels and the review text do not depend on suggestions.

Where suggestions come from (approved runs only, retracted runs do not count):

- **tools from the logs**: agents and models of the last approved run that are missing from
  `profile.tools` → a new `profile.tools` value. Build version numbers are dropped
  (`Claude Code 2.1.261` → `Claude Code`), case-insensitive duplicates are merged;
- **consistently strong axes**: axes scored 3 or higher in the last two approved periods → add
  them to `feedback.skip_basics` in one suggestion per run. An axis with "insufficient data" does
  not count; an axis that was already suggested (even if rejected) is not suggested again;
- **the judge**: if the logs disagree with the card (stack, tools, environment constraints), it may
  suggest an edit to a `profile` field with a reason and a quote. The other `feedback` fields
  (tone, length, channel) are edited by hand only.

The `review` skill recalculates suggestions at the end of a run and after `--approve`, and lists
them in its summary with the commands, but never applies them. The bare `sr.py approve` does not
recalculate them: after it, run `people suggestions --refresh` yourself. The same by hand:

```bash
# pending suggestions for everyone; --refresh recalculates from approved runs
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people suggestions --refresh
# one person, including applied and rejected ones
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people suggestions alex --all
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people apply alex S-alex-001
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" people reject alex S-alex-002 --reason "my call"
```

Without `--refresh` the command only reads files; `--json` prints the same as one object. `apply`
changes one line of the card (or adds list items) and keeps comments and order in place; the card
is then read back, and if it fails validation the file is restored and the command exits with code
`1`. If the `profile` value was changed by hand after the suggestion was made, `apply` also stops
with code `1`: the suggestion is outdated, `--refresh` closes it with the `stale` status (or reject
it with `people reject`). The same happens to a judge suggestion replaced by a newer one for the
same field, and to other pending suggestions for a field `apply` has just changed. A rejected
suggestion does not come back. The judge is read from the last approved period only.

If the card has no list of its own for `feedback.skip_basics` (or `boundaries`, `avoid`), `apply`
writes the list from `_default.yaml` into the card together with the new items: from then on the
list lives in the card, and edits to `_default.yaml` no longer affect it. In a card created by
`people add` the `feedback:` block is commented out: `apply` uncomments the `# feedback:` line and
the key it needs, the other lines stay commented and can be uncommented as usual.

Suggestions are stored next to the card, in `.sessionretro/people/<id>.suggestions.json`: the
field, the old and new value, the reason, the run, the status (`pending`, `applied`, `rejected`, `stale`),
and when and why it was decided. The file lives with the workspace, like the card itself.

Pending suggestions are shown in `reports/reviewer.html` on the person's card, in the "Card
suggestions" block with the `apply` and `reject` commands. The personal file and `group.html` do
not have this block. `doctor` prints `card_suggestions_pending: N`, the number of suggestions
waiting for a decision.

## Your own text template

The template only sets the layout of the text: order of the parts, greeting, signature. What the
parts say is decided by the assessment and the settings above.

1. Get a copy of the plugin template: answer "Own copy of the template" to the "Text template"
   question in `/sessionretro:setup`, or apply the answer `{"template": "workspace"}` with
   `setup-apply` (see [setup.md](setup.md)). The copy goes to `.sessionretro/templates/feedback.md`; an existing copy is not
   overwritten.
2. Connect it to the texts: in `.sessionretro/people/_default.yaml` (for everyone) or in a person
   card (for one person) set

   ```yaml
   feedback:
     template: workspace
   ```

3. Edit `.sessionretro/templates/feedback.md`. The comment at the top of the file explains the
   format and is not sent to the person. The template has five substitutions in double curly
   braces: `prefix`, `opening`, `actions`, `criteria`, `closing`; there are no others.

You can keep several templates: put a file such as `short.md` into `.sessionretro/templates/` and
set `template: short.md` in the card (file name only, no folders). If the file is missing, building
the text stops with code `1` and the path it looked for; there is no silent fallback to the plugin
template.

The `template` line in `doctor` shows the choice made during setup; which template is used for a
particular person is decided by `feedback.template` in their card (or in `_default.yaml`).

## Project agents

Three agents do the assessment: the collector reads logs in batches and gathers facts, the judge
scores and writes the text, the critic checks the text. During setup `setup` puts their copies
into your project:

| Role | File in the project | Model in `config.json` |
|---|---|---|
| collector | `.claude/agents/sessionretro-log-collector.md` | `models.collector` |
| judge | `.claude/agents/sessionretro-log-judge.md` | `models.judge` |
| critic | `.claude/agents/sessionretro-feedback-critic.md` | `models.critic` |

If the file exists in the project, the retro uses it; if not, it uses the plugin's agent. Plugin
updates do not touch files you edited: your edits stay (how unedited ones are updated: see
"After a plugin update" below).

### Model and effort

An agent's model and effort live in two places: in `config.json` (`models.<role>`) and in the
`model:` and `effort:` lines at the top of the agent file. Change them through setup (run
`/sessionretro:setup` again, the "Models" block) or directly:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" setup-apply --answers - <<'EOF'
{"models": {"judge": {"model": "opus", "effort": "max"}}}
EOF
```

The command changes `config.json` and rewrites only the `model:` and `effort:` lines in the agent
file. If you edited `config.json` by hand, run `agents write` so the files catch up with the
config:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" agents write
```

Models: `sonnet`, `opus`, `haiku`, `inherit` (same as the main session); effort: `low`, `medium`,
`high`, `max`.

### Editing the prompt

The body of the agent file is its prompt, and you can edit it: add a rule, shift an emphasis. Do
not change the `name:` line: the retro finds the agent by it. The plugin does not overwrite an
edited file afterwards, neither `setup` nor `agents write` without `--force`. A model change is not
applied to such an agent either: you get advice instead, and you fix its `model:` and `effort:`
lines by hand with the same values as in `config.json`.

### Checking: `agents check`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" agents check
```

```text
collector: ok (.claude/agents/sessionretro-log-collector.md)
judge: тело правлено вручную (.claude/agents/sessionretro-log-judge.md)
critic: ok (.claude/agents/sessionretro-feedback-critic.md)
```

For each role it shows whether the file exists (`нет файла`, no file), whether the model and effort
match `config.json` (`model ≠ config …`, `effort ≠ config …`), and whether the body was edited by
hand (`тело правлено вручную`, body edited by hand); these markers are printed in Russian in 0.1.
All `ok` gives code `0`, anything else code `1`. `--json` prints the same per role. The same summary is the `project_agents` line in `doctor`.

"Body edited by hand" is not a failure, it is a reminder that your agent differs from the plugin's.

### After a plugin update

Every time it writes an agent, the plugin remembers a fingerprint of its body in
`.sessionretro/agents.json` (`{"<role>": {"sha256", "plugin_version"}}`). That is how it tells two
cases apart when a file differs from the new version's prompt:

- **the file was not edited**: the body matches the fingerprint. `agents check` reports it as
  outdated (`устарел: болванка плагина обновилась, файл не правили — agents write обновит`: the
  plugin template changed, the file was not edited, `agents write` will update it), and
  `agents write` and the next `setup-apply` update the file by themselves (reported as `changed`),
  without `--force`;
- **the file was edited**: the body matches neither the fingerprint nor the new prompt. It is
  marked "edited by hand" and left alone, as described above.

You do not have to do this by hand: `/sessionretro:review` runs `agents sync` at the start of
every run, before any agent is launched. It updates only unedited files to the current prompt
with the models from `config.json`, leaves edited ones alone, creates no missing files and always
exits with code `0`, so the retro does not stop. The retro shows you its lines; with `--no-ask`
it puts them into the final summary:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" agents sync
```

```text
updated: collector (.claude/agents/sessionretro-log-collector.md)
unchanged: critic (.claude/agents/sessionretro-feedback-critic.md)
edited by hand: judge (.claude/agents/sessionretro-log-judge.md) — left as is, the new plugin prompt is not in it; to take the plugin agent (your edits are lost): `agents write --force`
```

The lines follow the workspace language (above is `lang: en`). No agent files: it prints "no
project agents" and the retro uses the plugin agents. `--json`
prints the same report as an object `{"updated", "unchanged", "edited", "missing", "record"}`.

Projects set up before `agents.json` existed have no record: there any difference counts as a
human edit, and `agents sync` leaves such a file alone and reminds you about it. If you did not
edit the files, run `agents write --force` once and updates will flow
from then on. `agents.json` can be kept in git together with `.claude/agents/`.

### Back to the plugin's agent: `agents write --force`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" agents write --force
```

Overwrites all three files with fresh copies from the plugin with the models from `config.json`.
**Your edits to the body are lost**: if you need them, save the file first and move the edits over
afterwards. Without `--force` the command leaves edited files alone and exits with code `1`,
listing them (the message is in Russian in 0.1: edited by hand and not overwritten, overwrite with
`agents write --force`):

```text
sessionretro: agents write: правлены вручную и не перезаписаны: .claude/agents/sessionretro-log-judge.md — перезаписать: agents write --force
```

You can also delete an agent file from `.claude/agents/`: the retro then uses the plugin's agent.
`agents write` and any later `setup-apply` put a fresh copy back.

## More

- [Setup: first and repeated run](setup.md)
- [Workspace: where sessionretro keeps its data](workspace.md)
- [Proposals to the plugin authors](feedback.md)
- [How the assessment works](how-it-works.md)
- [Privacy](privacy.md)
