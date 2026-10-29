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
cp config/projects.example.toml config/projects.toml   # then list your projects
uv run docboard sync                       # read every source into data/board.db
uv run docboard serve                      # the board on http://127.0.0.1:8742
uv run docboard board --cwd ~/some/folder  # sync the board that folder belongs to; print its link
uv run docboard sync --accept-drop --project p   # store a sync refused because a source really shrank
./scripts/gate.sh                          # lint, format check and tests: what CI runs
```

More: `docboard sync --dry-run --sample 3` prints the board as a table without storing anything;
`--json data/{project}.json` writes every card, and `docboard diff a.json b.json --kinds
idea,roadmap --strict` proves a docs move kept every card id; `docboard privacy` scans this repo
for personal identifiers.

Only the projects listed in `config/projects.toml` are read. The database and `--json` files hold
your own sessions' text, so they are owner-only (mode 600) and `data/` is git-ignored. Opening a
board older than two minutes refreshes it in the background and the page reloads itself when the
sync is done; "Sync now" does it at once.

On the page: cards new since you last pressed "Mark all seen" are marked (and counted per game);
the whole project's Ideas fold to a count per game until you open one; cards you hid are listed
with `?hidden=1`, and cards gone from their sources at the foot; a session's own page (`/s/<id>`)
lists its cards on every board and the answers that shaped no plan. `docboard board` prints how
many asks wait in your game and how many cards are new.

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
  games is the project's own ("repo-wide").
- **Transcripts** (`transcripts.py`) are every configured project's `~/.claude/projects/`
  folders, read in one pass (worktrees' included; folder names compared ignoring case), and a file
  that has not changed is not read again. Each line is checked with byte substrings before any
  JSON is parsed. Items come only from main-session assistant text and tool calls, never from tool
  results, which can hold a web page's text. A record copied into several files (forks,
  continued sessions) counts once, and every session holding a copy is known.
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
  while PR and plan cards keep their last good state. A sync that crashes is recorded as failed,
  with its reason on the page, and one broken project never stops the others.
- **The page** (`web.py`, FastAPI and Jinja2) answers only on the loopback address, and only to a
  loopback Host header (against DNS rebinding). Nothing changes except through a POST from the
  board's own page (Origin, or the browser's Sec-Fetch-Site), so an outside page can neither read
  the board nor move a card. All text is escaped, only http(s) links are made, and the
  Content-Security-Policy allows no script but the page's own 50-line drag-and-drop file. Every
  card also has a Move control that works without it.
- **Privacy** (`privacy.py`): this repo is public, so every tracked or addable file is scanned for
  home-folder paths naming a real user, email addresses outside placeholder domains, and the terms
  in a git-ignored `config/private-identifiers.txt`. Test fixtures are synthetic.
- Dependencies resolve only to releases uploaded at least 14 days ago (`[tool.uv] exclude-newer`).
- Decisions: [docs/DECISIONS.md](docs/DECISIONS.md). Gotchas met on the way:
  [docs/learnings.md](docs/learnings.md).

## Experience Gained

- A log-ingestion pipeline over 1.89 GB of Claude Code session transcripts (52 files) in about 6 s,
  using a byte prefilter before JSON parsing and de-duplicating records copied across files.
- An idempotent sources-to-cards derivation with content-derived, versioned ids. A test moves a doc
  from the root into a sub-project and proves all of its cards keep their ids.
- 12 guards each proven by a deliberate sabotage before merging, among them the id rule, the
  cross-file dedupe, the plan rule, the collapse floor that writes nothing, a move that must not
  outlive its source, and the page's Host and same-origin checks.
- A localhost web app hardened against DNS rebinding, cross-site writes and XSS (Host allowlist,
  Origin and Sec-Fetch-Site checks, a strict Content-Security-Policy, no third-party JavaScript),
  checked in a real browser, which caught a bug the 80 tests had missed.
- A supply-chain cooldown on every dependency: uv resolves only releases at least 14 days old,
  checked against PyPI's upload times (ruff held at 0.17.0 while 0.17.1 to 0.17.3 were inside the
  window).
- A privacy gate over 3 kinds of personal identifier, proven to fail on a planted home path before
  the first push to a public repo.
- CI on GitHub Actions with both third-party actions pinned to full commit SHAs and a read-only
  token.
