# Reviewing a period: how to run it and what you get

A review is one assessment run per person per period: two stages of agents read the session logs,
the result is recorded in the history, and you get a text with what is worth trying differently.
How the assessment itself works is in [how-it-works.md](how-it-works.md), what each axis looks at
is in [axes.md](axes.md).

Before the first review you need a workspace and person cards ([workspace.md](workspace.md)) and
collected logs (the `collect` skill).

## How to run it

In Claude Code, ask in your own words ("run a retro for the period", "assess my sessions") or call
the skill explicitly:

```text
/retropair:review alex
/retropair:review --all --task-summary "moving the report export to the new queue"
/retropair:review me --no-ask
```

Without a human in the loop, for example on a schedule:

```bash
claude -p "/retropair:review --all --no-ask"
```

## Arguments

| Argument | What it does |
|---|---|
| `<person>` | a card id: a review of one person (in self mode that is `me`) |
| `--all` | a review of everyone who has new or continued sessions; it does not touch the others |
| `--task-summary "…"` | one neutral line about what the work was. No acceptance criteria: what is assessed is how the person framed the task for the agent, not how well they matched someone else's expectations |
| `--no-ask` | not a single question: the answer about the work is "none", the period label is derived automatically. Where it is impossible to go on without an answer, the review of that person stops and the reason shows in the summary |
| `--approve <person> [run]` | only approve a ready draft, with no new assessment |

Without `--no-ask` the skill asks at most two things: the line about the work if you did not give
it, and what to do with an unrecorded result of a previous run if there is one.

## What happens

1. The skill finds people with new or continued sessions. No new logs: no review of that person,
   and that is no conclusion about them.
2. The sessions of the period are split into batches by time. Nothing is cut: every usable session
   goes into exactly one batch. If a person has only short sessions in the period, there is no
   score: the summary has an "insufficient data" line, and the sessions are marked excluded without
   a run and are not fed again next time until they grow.
3. The collectors read the batches in parallel and write down quotes. A script checks that their
   output has no scores, that all eight axes are there and that every quote stands in the log in
   the named turn as your message (or a tool command and its output), not as the agent's words. A
   shifted turn number (the same words in exactly one of your messages) and two pieces of one
   message without "…" are fixed by the script, which writes a warning about it; the agent's words
   are never fixed. If a collector still brings quotes that are not in the log after two restarts,
   those entries are dropped with a warning in the summary, and an axis left without facts gets
   "insufficient data" with the reason "quotes did not match the log".
4. The judge combines the facts, scores under quotes, gives verdicts on last period's
   recommendations and writes the parts of the text.
5. A script checks the result (a score only under a quote, "insufficient data" without a score,
   the text rules) and records the run in the history `.retropair/runs/process-reviews.json`.
   The sessions are marked assessed and are not fed again next time; a continued session is fed
   only with its new tail. When the period is set by run date (`run` mode), the script sets the
   label itself: `run-<YYYYMMDD>` by the UTC date, and the second and later retros of the same day
   get `-2`, `-3` and count as separate periods, not as a repeat of the first. In `dates` mode the
   label is the date window that the day the session started falls into. In `labels` mode the
   skill does not ask for a label (with or without `--no-ask`): the run is recorded without a
   period label, and `run_id` has `run` in its place.
6. A script computes the level by formula and builds the text from the template.
7. Reconciliation: every run has a text, a level and session marks.

At the end the skill shows a summary per person: how many sessions were assessed and excluded, the
path to the text and its status. There are no scores or levels in the summary (in self mode only
with `show_scores`), and it does not compare people with each other.

**Several date windows mean several runs.** In `dates` mode a person's new sessions can fall into
different windows, for example after a break of a couple of sprints. Then the skill makes a
separate run for each window, from the oldest to the newest: its own collectors and judge, its own
text and, in team and course, its own draft for approval. For a scheduled `--all --no-ask` this
means that time, cost and the number of drafts grow with the number of windows, not only with the
number of people. If you do not want them to pile up, run the review at the end of each window.

## What the judge gets and what is not given

The judge gets the collectors' facts, the paths to the logs to verify quotes, the line about the
work, open recommendations from earlier periods and a short slice of the card: role, grade,
experience, stack and how to write the text. The collectors do not get the card at all.

What no agent gets:

- past scores and levels: otherwise the new score is fitted to the old one;
- the reviewer's notes and impressions of the person;
- the task's acceptance criteria and what the person "should have" done;
- an assessment of the code or any other result of the work;
- project rules files (CLAUDE.md, AGENTS.md): that would assess an artifact, not the logs;
- logs and texts of other people.

## The review text

The file `.retropair/reviews/<person>/<run_id>.md` has three parts:

- **the service part** for the reviewer: how many sessions and batches, what was left out and why,
  the judge's warnings;
- **the text for the person** between two `---` rules: an opening, one to three "try differently"
  items with a date, a quote and an action, and a walk through the eight axes. Only this part goes
  to the person;
- **the dossier** for answering "where was that?": a table of sessions, recommendation ids,
  verdicts on earlier ones, and commands to open the right piece of a log.

The text template can be replaced: `feedback.template` in the card, otherwise the plugin template.

## Draft and approval

| Mode | Status of a new text |
|---|---|
| self | `approved`: you are the reviewer |
| team, course with `reviewer_gate: true` | `draft`: the text goes to the person only after it is read |
| team, course with `reviewer_gate: false` | `approved` |

The `setup` skill does not ask about `reviewer_gate`, and `setup-apply` does not accept such a
key: the gate is turned off only by editing `.retropair/config.json` by hand.

If you have read the draft and agree with it, approve it:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" approve alex
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" approve alex <run_id>
```

Without `<run_id>` the person's latest active run is approved. `/retropair:review --approve
alex` does the same and then runs `rp.py people suggestions alex --refresh`, which recomputes the
card suggestions from approved runs. The bare `rp.py approve` only changes the status: after it,
run `people suggestions alex --refresh` yourself.

In team and course with `reviewer_gate: true` the recommendations from the text start to count only
after approval: while the text is a draft, the person has not received them, and the next review
does not ask whether they followed them. `open-recs` shows such recommendations separately
(`pending_approval`), and the review summary says the verdict on them is postponed.

If the draft is wrong on the facts, do not edit the numbers in the history by hand. If the
assessment is wrong as a whole, retract the run: it stays in the history marked as retracted, the
marks of its sessions return to the state before it, and the next review feeds them again. The
recommendation ids of a retracted run are not reused: new recommendations get the next free ids
(if the judge picked a taken one, recording renumbers them itself and warns you).

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" retract <run_id> --why "the judge mixed up sessions"
```

To rebuild the text after editing the template or the card:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" render-review .retropair/runs/pending/alex/judge.json --person alex --run <run_id>
```

## Other commands

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" open-recs alex          # open recommendations
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" compare <run_a> <run_b>  # two runs of the same person
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" verify                   # everything closed to the end
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" validate                 # the run history is intact
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/rp.py" self-test                # self-check of the assessment engine
```

`compare` is useful for measuring reproducibility: how far two assessments of the same period
differ by axis and whether the level matched. It compares only runs of the same person.

`self-test` runs the assessment engine tests that ship with the plugin and prints the number of
checks and the failed ones. Run it after a plugin update or if a result looks odd.

## Exit codes

The same for all commands:

- `0`: all good;
- `1`: the data did not add up or failed a check (for example, a score without a quote, a text
  with a forbidden word, a run not closed to the end); the error text says what exactly;
- `2`: a wrong call: no workspace, unknown person, a typo in the arguments.

An error is printed as one line: what failed, why and with which parameters.
