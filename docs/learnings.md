# Learnings

Teachable things that came up building docboard. Newest first.

### A byte prefilter is case-sensitive; Windows paths are not

The scan skips a transcript line unless it holds the exact bytes `"file_path":"<root>`. On
Windows the same file can be written `C:\\Users\\…`, `c:/users/…` or in any case, so most
touches would have been skipped before anything parsed them.

**Why it came up:** a test with three spellings of one Windows root found one touch of three.

**Takeaway:** when a fast byte check stands in front of a slower exact one, make it at least as
forgiving as the data's own rules (here: lower-case the haystack on Windows), and test it with
every spelling the writer can produce.

### A copied record keeps its timestamp, so "earliest copy wins" needs a tie-break

A continued or forked Claude Code session copies its parent's records into its own file with
the parent's timestamps and its own session id. De-duplicating "earliest timestamp wins" then
ties, and the first file read wins the tie: folder sort order decided which session an ask
belonged to, and three asks of a games-repo session showed on another repo's board.

**Why it came up:** routing cards by their session's folder moved "launch +1 Gravity" to
browser-shield, because that repo's folder sorts first and held a continued session's copy.

**Takeaway:** when copies share their sort key, break the tie by something that tells the
original from the copy (here, which session started first), and test it with the original in
the folder read last.

### A safety check that runs before the degrade switch turns an outage into a refusal

A collapse check ("a source fell to zero: refuse the sync") and an outage switch ("gh is down:
keep the last PR states") were both right on their own. In that order, the check saw the
outage's zero PRs first and refused the whole sync, so the freeze never ran, and the Needs-you
items, which have nothing to do with gh, stopped arriving too.

**Why it came up:** the survey reproduced it with a throwaway database: counts with
`prs_merged: 0` and a gh failure gave "REFUSED: prs_merged: 10 → 0". The existing test passed
only because its fixture kept `prs_merged` at 10.

**Takeaway:** when one guard can make another's input look broken, decide the degraded state
first and let the guard skip what it explains; then test the pair with the outage's own numbers,
and keep an outage's zeros out of the next baseline.

### Python's `HTTPServer` looks its own address up in DNS when it binds

`http.server.HTTPServer.server_bind` calls `socket.getfqdn(host)` to fill `server_name`. On this
Mac's Homebrew Python 3.12 a reverse lookup of `127.0.0.1` took 35 s (12.6 s when run again),
while the system Python answered at once, so the cost depends on the interpreter's resolver.

**Why it came up:** the page-check test spent 35 s of its 37 s starting a stub server in a
fixture, and a run of 11 sabotages took six minutes. `--durations` showed the time in setup, not
in any test.

**Takeaway:** start a throwaway test server with `socketserver.TCPServer`, which skips the lookup,
and when a test is slow, time its setup apart from its call before speeding the code up.

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
