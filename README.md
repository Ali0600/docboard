# docboard

A Kanban board for each project, built from what the project already writes down: the Backlog in
`docs/DECISIONS.md`, `ROADMAP.md` checkboxes, its GitHub pull requests, the plans approved in Claude
Code, and the "Needs you" items those sessions raised. Every source is read-only; a card you move is
remembered in a local SQLite file. A monorepo's sub-projects (each game of a games monorepo) get a
board of their own.

Status: in use. `docboard serve` shows the board; cards can be dragged, resolved, dismissed, given
another game or added by hand, and a triage page clears Needs-you items in bulk, per game, with a
confirm step and an undo for each batch. A Claude Code `/board` command runs `docboard board` from
any session. It replaced an earlier dashboard on 2026-10-27.

## Run it

```sh
uv sync                                    # Python 3.12 and the dev tools, from uv.lock
git config core.hooksPath .githooks        # the privacy scan before every push (once per clone)
uv run docboard sync                       # read every source into the board
uv run docboard serve                      # the board on http://127.0.0.1:8742
uv run docboard board --cwd ~/some/folder  # sync the board that folder belongs to; print its link
uv run docboard sync --accept-drop --project p   # store a sync refused because a source really shrank
./scripts/gate.sh                          # lint, format check and tests: what CI runs
```

More: `docboard sync --dry-run --sample 3` prints the board as a table without storing anything;
`--json out/{project}.json` writes every card, and `docboard diff a.json b.json --kinds
idea,roadmap --strict` proves a docs move kept every card id; `docboard privacy` scans this repo
for personal identifiers (`--commits RANGE` a range's commits, `--stdin` a PR's text).

### Where the board lives

Not in this checkout: a branch checked out here must never change which database the live board
uses. The database (`board.db`, with its `backups/`), the config (`projects.toml`) and the private
terms (`private-identifiers.txt`) live in the user's data folder:

| System | Folder |
|---|---|
| macOS | `~/Library/Application Support/docboard` |
| Windows | `%LOCALAPPDATA%\docboard` |
| Linux | `$XDG_DATA_HOME/docboard`, else `~/.local/share/docboard` |

`DOCBOARD_HOME` moves all of it (a dev board, the tests); `--config`/`$DOCBOARD_CONFIG` and `--db`
name one file. Start the config from `config/projects.example.toml`. On Windows write paths in
single quotes (`path = 'C:\Users\you\code\repo'`) or with `/`: in double quotes `\U` is a TOML
escape. Only the projects listed in `projects.toml` are read. The database and `--json` files
hold your own sessions' text, so they are owner-only (mode 600). A copy of the database is kept
before any schema change and once a day (`backups/`, 7 daily and 5 pre-migration copies, on this
disk only); to restore one, stop the page and copy it over `board.db`.

**Moving the board** (once, from a checkout that kept it in `data/` and `config/`): stop the page,
then `uv run docboard move-data`. It copies the database with SQLite's backup, checks the copy
holds the same cards, moves and events, copies the config and the terms, and puts the old files
aside in `data/moved-<date>/` (git-ignored). Until then every command refuses to start an empty
board. To undo: stop the page, move the three files back from `data/moved-<date>/`, and check out
the commit before the move.

### Windows

The same commands run on Windows (CI tests Linux, Windows and macOS): the lock is
`msvcrt.locking`, transcript paths are matched with either slash and any case, and the PC keeps a
board of its own sessions. Shipping the PC's asks to the Mac's board is not built yet (the
Backlog).

Opening a board older than two minutes refreshes it in the background and the page reloads itself
when the sync is done; "Sync now" does it at once. `docboard board` says when the running page
has older code than the checkout, so it needs a restart.

## How it works

| Column | From |
|---|---|
| Needs you | "Needs you" items (`**1. Decide — …**`, `**1. Do — …**`) in the assistant's own text, unanswered questions, unapproved plans, wrap-up asks. They leave only by the owner's action. |
| In progress | open PRs, and plans whose PRs are still landing |
| Next | approved plans with no PR yet, unticked ROADMAP items |
| Ideas | the DECISIONS.md Backlog |
| Done | merged PRs, ticked ROADMAP items, plans whose PRs are in |

- **Docs** (`docsrc.py`, `gitrepo.py`) are read from the blobs at `origin/main` (or `docs_ref`),
  never a working tree another session may be using: the root `docs/DECISIONS.md` and each
  sub-project's `docs/DECISIONS.md` (a bold title may wrap; a bullet the parser cannot read is
  warned about), `ROADMAP.md` at either level, and the `- [ ]` steps of `docs/checklists/*.md`,
  each under its file's title.
- **Pull requests** (`ghsrc.py`) through `gh`; a failure shows as "degraded", never as no PRs.
  A PR belongs to the game its title names, else its branch prefix (an agent's own `codex/` or
  `claude/` looked past), else most of the files of its squash commit; a PR touching three or more
  games is the project's own ("repo-wide"). A PR card carries the MILESTONES lines that cite it
  and, when its description has one, its hand-check section (badged "hand check listed"). Main's
  CI is a card while a workflow's newest run on the default branch is red ("refused before it
  ran" when GitHub never started its jobs), and moves to Done once a later run is green; only
  runs testing the branch's own commits count, never a fork's PR or Dependabot's jobs.
- **Transcripts** (`transcripts.py`) are every configured project's `~/.claude/projects/`
  folders, read in one pass (worktrees' included; folder names compared ignoring case), and a file
  that has not changed is not read again. Each line is checked with byte substrings before any
  JSON is parsed. Items come only from main-session assistant text and tool calls, never from tool
  results, which can hold a web page's text. A record copied into several files (forks,
  continued sessions) counts once, and every session holding a copy is known.
- **Session proposals**: a session the assistant proposed on its own (`start_session`) is an Idea
  until its task is dismissed or started, or for a week, as long as the app keeps it.
- **Which board** (`route.py`): a session filed under one project often works in another. An
  ask goes to the one project (or one of its games) its text names, else the project its turn's
  files are in, else the project its transcript is filed under; a plan goes where most of its PRs
  went. Every project's sync gives the same answer, so a card is on exactly one board. An ask
  that names no game and touched no game's files sits at the project's root.
- **Plans** (`derive.py`) are cards per run of approvals with one title, one card however many
  continued sessions hold it. A PR joins the plan its session approved last before the PR was
  created, a continued session's copy of the plan included; a PR titled "PR 1" or "PR A" after
  the plan already has one starts a new series and is not that plan's. A plan is Done when every
  PR it named is in. With no PR list it is Done once its session moved on, went quiet for a day,
  or merged nothing for a week; in a repo with no GitHub remote it is Done once its session moved
  on or went quiet. A partly merged plan quiet for two weeks moves to Done with the gap on its
  card. A plan whose PRs are in another configured repo shows them as "+N in <project>".
- **Ids** (`ids.py`) come from content, never a path, so a doc moved into a game's folder keeps
  its cards; they are frozen behind `ID_VERSION`.
- **The store** (`store.py`, SQLite, owner-only) keeps the last sync's cards and your moves. A move
  holds while the card's source still says what it said when you moved it; when the source moves
  the card, your move is dropped and the card's history says so. Needs-you cards never move by
  themselves, so resolving or dismissing one is permanent. A reworded Backlog bullet keeps your
  moves; a card gone from its source is kept and listed, never deleted. An ask raised again after
  you resolved it comes back to Needs you, aged from its latest raise, and an open ask stays even
  when Claude Code deletes its transcript. A sync whose source collapsed (fell to zero, or by more
  than half, since the last good one) writes nothing and says so on the page, with the
  `--accept-drop` command for a drop that is real. With GitHub unreachable the rest still syncs,
  while PR and plan cards keep their last good state, and the banner says since when. A sync that
  crashes is recorded as failed, with its reason on the page, and one broken project never stops
  the others.
- **The page** (`web.py`, FastAPI and Jinja2) answers only on the loopback address, and only to a
  loopback Host header (against DNS rebinding). Nothing changes except through a POST from the
  board's own page (Origin, or the browser's Sec-Fetch-Site), so an outside page can neither read
  the board nor move a card. All text is escaped, only http(s) links are made, and the
  Content-Security-Policy allows no script but the page's own 50-line drag-and-drop file. Every
  card also has a Move control that works without it.
- **Privacy** (`privacy.py`): this repo is public, so every tracked or addable file is scanned for
  home-folder paths naming a real user (macOS, Linux or Windows, also as JSON writes them), email
  addresses outside placeholder domains, and the terms in the data folder's
  `private-identifiers.txt`. A pre-push hook (`.githooks/pre-push`) scans what a push carries
  (every added line and file name, a merge's own lines, messages, tags, author addresses and
  branch names) and refuses without the terms; CI's `privacy` workflow scans a PR's commits and its
  title and body for paths and addresses, again whenever the text is edited. Test fixtures are
  synthetic.
- Dependencies resolve only to releases uploaded at least 14 days ago (`[tool.uv] exclude-newer`);
  Dependabot proposes updates weekly with the same 14-day cooldown.
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md). Gotchas met on the way:
  [docs/learnings.md](docs/learnings.md).

## Experience Gained

- A log-ingestion pipeline over 1.97 GB of Claude Code session transcripts (58 files, four
  projects) in one pass of about 7 s, about 0.2 s a board once warm (a per-file memo keyed by the
  file's identity, size and time), with a byte prefilter before JSON parsing and records copied
  across continued sessions counted once.
- An idempotent sources-to-cards derivation with content-derived, versioned ids. A test moves a doc
  from the root into a sub-project and proves all of its cards keep their ids; a routing test
  proves every session card lands on exactly one of the configured boards.
- Guards each proven by a test that failed first (about 280 tests added over seven PRs, each PR's
  red run in its description), among them the id rule, the collapse floor that writes nothing,
  a gh outage that freezes PRs instead of refusing the sync, and the page's Host and same-origin
  checks.
- A localhost web app hardened against DNS rebinding, cross-site writes and XSS (Host allowlist,
  Origin and Sec-Fetch-Site checks, a strict Content-Security-Policy, no third-party JavaScript),
  checked in a real browser, which caught a bug the 80 tests had missed.
- A supply-chain cooldown on every dependency: uv resolves only releases at least 14 days old,
  and Dependabot proposes updates with the same cooldown.
- A privacy gate over 3 kinds of personal identifier (Windows paths included): the files on every
  CI run, every commit's added lines, messages and authors before each push, and a PR's text in CI.
- CI on Linux, Windows and macOS through GitHub Actions, with third-party actions pinned to full
  commit SHAs and a read-only token.
