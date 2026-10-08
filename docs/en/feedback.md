# Proposals to the plugin authors: what is recorded and how to send it

Sometimes the work runs into the plugin itself rather than your data: a command cannot do
something, a format does not parse, you had to work around it by hand. Then the RetroPair skill
finishes the task with a workaround and, in one message, offers to record a proposal for the plugin
authors. If you agree, a short anonymized file appears in the project. The plugin itself sends
nothing anywhere and has no mail: proposals leave only when you bundle them into a pack yourself
and open an issue on GitHub.

To see what has been recorded:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" improve list
```

```text
imp-20260927-collect-7fea · pending · collect
imp-20260927-setup-5a56 · sent · setup
```

## When the plugin offers to record one

The `setup`, `collect`, `review` and `report` skills offer a record only when the plugin is in the
way:

- a needed capability is missing: a command cannot do it, a format does not parse, the template
  does not cover the case;
- a script or skill fails reproducibly;
- you had to work around the plugin by hand to finish the job.

The order is always the same: first the workaround, so that your task gets done, then one message
(what got in the way, how it was worked around, what is proposed) and the question "Record a
proposal for the plugin authors?". The file is written only after your "yes". If you say "no", the
question does not come back in this session.

It is not offered:

- about matters of taste: tone, length and style of the text are settings of the card and the
  template ([customize.md](customize.md));
- about errors in your data (a broken config, no logs): that is an error message;
- in background and non-interactive runs (the hook, `claude -p`, `--no-ask`): there is nobody to
  answer;
- again if the same proposal (the same component and symptom) is already recorded: the skill names
  its ID.

## Where the files are and what is inside

Each proposal is a file `.retropair/improvements/imp-YYYYMMDD-<slug>-<4 characters>.md` in your
project. The field labels are in Russian in 0.2: ID, Компонент (component), Симптом и контекст
(symptom and context), Локальный обход (local workaround), Предложение (proposal), Версия плагина
(plugin version), Дата (date), Статус (status):

```text
# imp-20260927-setup-5a56

- **ID:** imp-20260927-setup-5a56
- **Компонент:** setup
- **Симптом и контекст:** setup-apply does not accept a date window without a label
- **Локальный обход:** wrote the window label into config.json by hand
- **Предложение:** cause: a manual config edit; approach: label the window by its dates; success criterion: a window without a label goes through
- **Версия плагина:** 0.2.0
- **Дата:** 2026-09-27
- **Статус:** pending
```

The text is anonymized when it is written: paths inside the project become relative, the home
folder becomes `~`, names from person cards are replaced with `[NAME]`, e-mails with `[EMAIL]`. If
the text still has a path with a login, an e-mail or a name from the cards after that, the file is
not written: the command exits with code `1` and says what it found. You can open the files and
edit them by hand; the check runs again when the pack is built.

You can also record a proposal yourself, without the skill:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" improve add --component review --symptom "a batch of 200 turns did not fit the judge's limit" --workaround "split the period in two" --proposal "cause: long sessions; approach: split the batch automatically; success criterion: a period of 200 turns goes through in one run"
```

`--slug` sets the word in the ID (Latin letters with hyphens); by default it is taken from the
component.

## What goes into the issue and what does not

Only what you see in the proposal files goes: the component, the anonymized symptom, the
workaround, the proposal itself, the plugin version and the date.

Never sent, and must not appear in the text:

- session logs, whole or in fragments, even sanitized;
- quotes from sessions, review texts, scores;
- names, handles and e-mails of people, the names of your company, clients and project;
- paths on your machine with the login, keys, tokens, internal addresses.

The automation catches paths, e-mails and names from the cards, but only you can spot a client name
in free text. Re-read the pack before you send it.

## Building a pack: `improve pack`

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" improve pack
```

The command gathers every `pending` proposal into one file
`.retropair/improvements/pack-YYYYMMDD-<4 characters>.md` (your own path with
`--out <file>`), checks it once more for paths, e-mails and names, prints the whole pack and a
ready command to send it. It sends nothing itself (output of a workspace with `lang: en`):

```text
pack: /home/user/work/my-app/.retropair/improvements/pack-20260927-31f0.md
Nothing was sent. Re-read the pack above; to send it, run the command below yourself, then mark it: `improve sent <ID>…`
gh issue create --repo <plugin repository> --label proposal --title 'RetroPair 0.2.0: предложений 1' --body-file /home/user/work/my-app/.retropair/improvements/pack-20260927-31f0.md
```

(The issue title reads "RetroPair 0.2.0: 1 proposal".) The command takes the repository from the
`repository` field in the plugin's `.claude-plugin/plugin.json`. No `pending` proposals, or
personal data found in the text: code `1`, and no pack is written.

## Sending

1. Re-read the pack: exactly this text goes into the issue.
2. Run the printed `gh issue create …` command (you need the [GitHub CLI](https://cli.github.com)
   and `gh auth login`). Without `gh`, open a new issue on GitHub with the "Proposal" template and
   paste the pack text.
3. Mark the sent proposals so that they do not get into the next pack:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" improve sent imp-20260927-setup-5a56
   ```

## When a proposal is implemented: `improve sync`

The authors mark an accepted proposal in the plugin CHANGELOG with a line `внедрено: imp-…`
("implemented: imp-…") under the version heading. After updating the plugin, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" improve sync
```

Implemented proposals get the status `implemented (X.Y.Z)` and move to
`.retropair/improvements/archive/`. `--changelog <file>` reads a different CHANGELOG.

| Status | What it means | Who sets it |
|---|---|---|
| `pending` | recorded, not sent | `improve add` |
| `sent` | you sent the pack | `improve sent <ID>…` |
| `implemented (X.Y.Z)` | implemented in version X.Y.Z, the file is in `archive/` | `improve sync` |

How many proposals wait to be sent is shown by the `improvements_pending` line in `doctor`.

## Issue templates

The plugin repository has three issue templates:

| Template | File | When |
|---|---|---|
| Proposal | `proposal` | an improvement to the assessment, the text, reports or setup; the `improve pack` pack goes here |
| Bug | `bug` | an error in a script, skill or report: steps, expectation, the `retropair: …` error text, the plugin version, OS and python3 |
| Privacy | `privacy` | personal data or secrets got into a text, a report or a log |

**A privacy leak** has its own template and is the top priority for the authors. It describes
**what kind** of data leaked (a path, a name, a key, an e-mail) and **at which step** (collection,
sanitizer, text to the person, HTML report), without the data itself: do not attach logs, names or
keys, whole or in fragments.

You can send a bug or a leak from the terminal too:

```bash
gh issue create --repo <plugin repository> --label bug --title "collect: error on an empty folder" --body-file bug.md
```

If you need an example, make one up: a made-up name (`alex`), a path `/home/user/…`, a synthetic log
of three or four turns.

## Discussions

Questions, ideas without a ready proposal and stories of how you use RetroPair go to the
Discussions section of the plugin repository on GitHub. The same rules apply: no logs, names or
paths from your machine.

## More

- [Customizing](customize.md)
- [Setup](setup.md)
- [How to contribute](../../CONTRIBUTING.md) (Russian)
