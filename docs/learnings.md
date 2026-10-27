# Learnings

Teachable things that came up building docboard. Newest first.

### A byte prefilter is a contract with the writer's serializer

Skipping lines that lack `"type":"assistant"` before parsing them is what makes a 1.9 GB scan take
6 seconds, but it only works while the writer keeps writing exactly those bytes.

**Why it came up:** the test fixtures were written with Python's default `json.dumps`, which puts a
space after every colon (`"type": "assistant"`). The prefilter skipped every line, and 16 of 50
tests went red with nothing parsed. Real transcripts are compact: a 3 MB sample held 785
`"type":"` and no `"type": "`, with em dashes unescaped.

**Takeaway:** build fixtures with the writer's exact serializer and keep the evidence beside it;
let a floor (a source that cannot be empty falling to zero) catch the day the writer changes.

### A large tool result is a stub; the structured field still holds the data

**Why it came up:** 7 plan approvals over about 50 KB had been saved to disk, so their result text
was a `<persisted-output>` stub, not "User has approved your plan". Keyed on the text, they read as
never answered. The record's `toolUseResult.plan` was still there.

**Takeaway:** read an outcome from the structured record, never from the text a UI shows.

### Where a transcript is filed says more than its first record

**Why it came up:** 3 sessions filed in the games repo's own transcript folder began in a desktop
scratch folder, so their first `cwd` named that folder. A "first cwd must be inside the project"
rule dropped them, Brightwater High's session among them. The rule exists for a sibling repo whose
folder name only starts with the project's ("roblox-games-x").

**Takeaway:** trust the folder the app filed a transcript under; prove membership only for folders
that merely look alike, by any record's `cwd`, not the first.
