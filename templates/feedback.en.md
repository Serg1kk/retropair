<!--
Template of the outgoing block of a review file, language en.

Purpose. The judge writes the parts of the text without markup (feedback_parts in JSON); the
renderer assembles the outgoing block from them with this template and checks the text bans
(section "Checks by script" in references/output-contract.md). The renderer strips this header
comment, it never reaches the recipient. A workspace keeps its own template in
.sessionretro/templates/feedback.md; the card field feedback.template picks the file. There
are exactly five placeholders and no others.

Placeholders:
- {{prefix}}   feedback.prefix from the card as is; empty string → the template line is
               removed together with the blank line after it.
- {{opening}}  feedback_parts.opening as one paragraph.
- {{actions}}  items of feedback_parts.actions in order, one paragraph each in the format
               below, a blank line between items.
- {{criteria}} eight lines of feedback_parts.criteria in axis order, format below, a blank
               line between lines.
- {{closing}}  feedback_parts.closing as one paragraph; null → the template line is removed.

The block heading with the period (period.label) is not part of the template: the renderer
puts it as a line above the block. The template holds no scores, levels or axis codes;
showing scores in self mode under the show_scores flag is up to the renderer, not the
template.

Action item (one paragraph, one line without line breaks):
**<n>.** <date>, <place>: "<quote>"; <cost>. Worth trying: <try>. A related lever: <lever>.
- <n>: First, Second, Third by the item index;
- <date>: date in words, "March 12"; date = null → "<date>, " is dropped and the first
  letter of <place> is capitalised;
- <quote> in double quotes as is, nothing inside a quote is edited;
- a field without its own final period gets one from the renderer;
- "A related lever: <lever>." repeats once per element of levers (0-2); empty levers → no
  tail.

Criterion line (one line):
**<axis name>.** <verdict> <evidence>
- <axis name>: axes.<axis>.en from i18n.json, only the name is bold;
- <verdict>: verdicts.<verdict>.en from i18n.json, not bold;
- <evidence> as is; empty string → the line ends with <verdict>.

Short mode (feedback.length: short): same template shape, all five parts and all eight
criteria stay. The judge writes shorter: a one-sentence opening, at most two action items,
zero levers, and each criterion is its verdict plus one sentence with a pointer. The
renderer checks this and never trims silently: more than two items or any lever → an error
naming the condition. feedback.practices: false removes levers the same way as short.
-->
{{prefix}}

{{opening}}

**If you read fast: what to try differently.**

{{actions}}

**Next, the eight criteria this rests on.**

{{criteria}}

{{closing}}
