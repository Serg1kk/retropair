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
code `1` and the field name.

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
| `channel` | `file` · `telegram` · `slack` · `email` | `file` | where the text goes; affects line breaks |
| `template` | `default` · `workspace` · file name | `default` | which text template to use (below) |
| `prefix` | string | `""` | what to put before the text, for example a greeting |

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

## Your own text template

The template only sets the layout of the text: order of the parts, greeting, signature. What the
parts say is decided by the assessment and the settings above.

1. Get a copy of the plugin template: answer "Own copy of the template" to the "Text template"
   question in `/sessionretro:setup`, or apply the answer `{"template": "workspace"}` with
   `setup-apply`. The copy goes to `.sessionretro/templates/feedback.md`; an existing copy is not
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
updates do not touch these files: your edits stay.

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

For each role it shows whether the file exists, whether the model and effort match `config.json`,
and whether the body was edited by hand. All `ok` gives code `0`, anything else code `1`. `--json`
prints the same per role. The same summary is the `project_agents` line in `doctor`.

"Body edited by hand" is not a failure, it is a reminder that your agent differs from the plugin's.

### After a plugin update

Every time it writes an agent, the plugin remembers a fingerprint of its body in
`.sessionretro/agents.json` (`{"<role>": {"sha256", "plugin_version"}}`). That is how it tells two
cases apart when a file differs from the new version's prompt:

- **the file was not edited**: the body matches the fingerprint. `agents check` reports it as
  outdated, and `agents write` and the next `setup-apply` update the file by themselves, without
  `--force`;
- **the file was edited**: the body matches neither the fingerprint nor the new prompt. It is
  marked "edited by hand" and left alone, as described above.

Projects set up before `agents.json` existed have no record: there any difference counts as a
human edit. If you did not edit the files, run `agents write --force` once and updates will flow
from then on. `agents.json` can be kept in git together with `.claude/agents/`.

### Back to the plugin's agent: `agents write --force`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sr.py" agents write --force
```

Overwrites all three files with fresh copies from the plugin with the models from `config.json`.
**Your edits to the body are lost**: if you need them, save the file first and move the edits over
afterwards. Without `--force` the command leaves edited files alone and exits with code `1`,
listing them.

You can also delete an agent file from `.claude/agents/`: the retro then uses the plugin's agent.
`agents write` and any later `setup-apply` put a fresh copy back.

## More

- [How the assessment works](how-it-works.md)
- [Privacy](privacy.md)
- In Russian: [setup, first and repeated run](../ru/setup.md), [where state is kept](../ru/workspace.md), [proposals to the plugin authors](../ru/feedback.md)
