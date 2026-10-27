# Learnings

Teachable things that came up building docboard. Newest first.

### `Referrer-Policy: no-referrer` turns a page's own form posts into `Origin: null`

A CSRF guard that accepts only the page's own Origin refuses the page's own forms when the page
also sends `Referrer-Policy: no-referrer`: Chrome then sends `Origin: null` on its form posts.

**Why it came up:** the board's "Add a card" form got a 403 in the first real-browser run, while
all 80 tests passed. The tests sent the Origin header a test author expects, not the one a browser
sends under that policy.

**Takeaway:** use `Referrer-Policy: same-origin` (still nothing to other sites). When Origin is
absent or `null`, decide by `Sec-Fetch-Site`, a header no page can set. Drive a state-changing form
once in a real browser before trusting a request guard.

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
