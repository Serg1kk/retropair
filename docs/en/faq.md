# FAQ

Upgrading from SessionRetro? Follow the [migration guide](migration.md) before initializing a new workspace.

## Does the plugin assess my code?

No. RetroPair looks only at how you lead the agent: how you frame the task, verify the result and
keep the decisions in your hands. Code quality, architecture, speed and the outcome of the task are
not assessed. The eight axes are in [axes.md](axes.md).

## Which agents does it work with?

The assessment runs from Claude Code. Claude Code logs are read directly. Logs of Cursor, Codex,
Antigravity CLI and other agents are collected through SpecStory exports or a manual export into a
folder; how to do that for your environment is in the
[environment guides](../../references/collector-instructions/README.md) (Russian).

Codex and Cursor session files in their own format (JSONL) are not read directly in 0.2: collection
shows a warning. The path for them is SpecStory.

## How much does it cost and how long does it take?

Historical measurements before the rebrand, not current pricing or guaranteed estimates:
with the default models (opus judge, sonnet collectors) one period of one person costs about
$3–4 and takes 10–15 minutes; a period made only of short sessions costs $0.3–0.6. These are the
figures of the end-to-end check on real work. You pay through your Claude
Code: a subscription or an API key. The "Economy" preset in `/retropair:setup` is cheaper, but
you get fewer quotes and subtle points ([setup.md](setup.md), the "Models and effort" question).

## How far can the assessment be trusted?

The judge was checked on a reference set of 15 real sessions in two full runs: 96–100 % of axis
scores are within ±1 of the reference, the runs differ by at most 1 point, and the overall level
matched in 9 of 15 sessions. ±1 is a weak bar when almost all scores sit between 2 and 4, and the
judge tends to pull scores towards the middle. So the main value of a review is the quotes and the
concrete steps, not the numbers.

## Why does an axis say "insufficient data" instead of a score?

A log does not show everything: you may have checked the result in another terminal or discussed a
decision by voice. When the behaviour is not visible in the log, the honest answer is "insufficient
data", and it never turns into a low score. A low score needs positive evidence. Details in
[how-it-works.md](how-it-works.md).

## Why don't I see scores?

There are never scores in the review text: the person reads what to change, not numbers. In the
personal HTML report axis scores and the level are shown by default. They are absent if the report
was built in the "dynamics only" view: `show_scores: false` in `config.json` or in the person card,
or `report --trend`. Rebuild with `report --scores` or change the setting; see
[reports.md](reports.md#scores-and-level).

## Can I compare people in a team?

No, in no mode. Everyone is compared only with their own previous period. The group report shows
the axes per person in alphabetical order, with no ranking, texts or quotes.

## Old sessions did not get into the retro

Claude Code deletes its logs older than `cleanupPeriodDays` days (30 by default). What is deleted
before collection does not get into the retro. Raise the period to 90 (`setup` offers this itself)
and turn on the SessionEnd hook: it takes a sanitized copy after every session, and the copy stays
in the workspace after the original is deleted.

## Collection does not find Claude Code sessions

The `claude-jsonl` source with project `auto` looks for sessions opened from the project root.
Claude Code stores sessions started from a subfolder under a different name, and `auto` does not see
them. Open Claude Code from the project root, or add another `claude-jsonl` source with the absolute
path to the subfolder in `"project"` ([collect.md](collect.md)).

## Does it work on Windows?

Not in 0.2: log collection and file locking do not work on Windows, and the commands stop with a
clear error. The path is a manual export of sessions and collection on macOS or Linux.

## Can I get the texts in English?

Yes: `lang: en` in `config.json` and `address: you` in the card. But the axis references are in
Russian for now, and the judge's English text has not been checked on the reference set.

## A person's personal report was not written

Before a personal file is written, it is checked for names and ids of other people in the
workspace. If any are found, the file is not written, and the error says what was found. Rewrite
the approved review or the recommendation without the other person's name and rebuild the report
([reports.md](reports.md)). This check skips the workspace title: if it contains
words from the cards, `report` only warns (`workspace title contains name tokens of: <id>`), and the
title goes into every personal file. Remove the names from `workspace.title` in
`.retropair/config.json`.

## The reviewer report is heavy

The reviewer report contains the dossiers and quotes of all people for all periods: for a group of
20 people over six periods that is about 3 MB. It opens in a browser without a network, but slower
than the others.

## Where do my logs go?

Nowhere except the model through your Claude Code: the agents read the sanitized copies in your
session. Details, including what the sanitizer misses, in [privacy.md](privacy.md).

## How do I update or remove the plugin?

Update and remove it through the Claude Code plugin manager (`/plugin`); after an update run
`/reload-plugins` or open a new session. An update does not touch user state: cards, settings,
history and project agents stay. When the plugin is removed, the projects' `.retropair/` folders
and `~/.retropair/` stay too; delete them by hand if you no longer need them.

## Where can I ask a question?

Questions and ideas go to Discussions in the plugin repository on GitHub, bugs and proposals to
Issues with the templates ([feedback.md](feedback.md)). There is no e-mail. Do not attach logs,
names or keys.
