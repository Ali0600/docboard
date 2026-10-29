# Decisions

## Backlog — alternatives worth trying later

- **Move a card to another board by hand** (2026-10-28, cards follow the files): an ask that
  names no project and touched no files stays on the board its transcript is filed under (two
  browser-shield follow-ups sit on roblox-games). Revisit hook: a `board_override` table read by
  `derive.mine`, so every sync still agrees.
- **Read transcripts filed under a project's former path** (2026-10-28): only single-session
  leftovers were found, none for a configured project. Revisit hook: a `former_paths` list on
  `config.Project`, added to `transcripts.discover`'s encodings.
- **A Claude Artifact page instead of a local web page** (2026-10-27, where the board lives): a
  private claude.ai page with its own database, readable on a phone, filled from each session.
  Deferred: a page there cannot read files, so every sync would have to go through a session.
  Revisit hook: an exporter that writes the board's cards into an Artifact's database after each
  `docboard sync`.
- **A model reading the loose prose** (2026-10-27, the sources): memory "owed" sentences, wrap-up
  recaps and failed tool calls read by `claude -p` into suggested cards. Deferred: v1 parses only
  structured sources, so it runs unattended and is tested exactly. Revisit hook: a `suggest` source
  kind whose cards land in Needs you marked as suggestions.
- **Sync on SessionEnd** (2026-10-27): a hook syncs the project a session ran in. Deferred: desktop
  sessions rarely end (one session's transcript was still being written 15 days after its first
  PR), and a hook adds work to every session end in every project. Revisit hook: a
  `docboard sync --project-from-cwd` entry in `~/.claude/settings.json`'s hooks.
- **Reading only what a transcript gained since the last sync** (2026-10-27, the page): a cursor
  per file (offset, inode, hashes of its head and of the bytes before the offset). Deferred: a
  full re-read of 1.9 GB takes about 10 s, and the board refreshes in the background. Revisit hook:
  `transcripts.scan_file` and a `cursor` table, with a rewritten or shorter file read from 0.
- **A login service keeping the page up** (2026-10-27, the rollout): a LaunchAgent running
  `docboard serve`, so the link always answers. Deferred: the owner took the cut-over without it,
  and `/board` starts the page for the session when it is down. Revisit hook: a LaunchAgent from
  `uv tool install` with absolute paths, `PATH`, `HOME` and `LANG` set, proved under `env -i`,
  nothing under `~/Documents`.

## 2026-10-28 — Cards follow the files their work touched

docboard's own board was empty: every session that built it ran from the games repo's folder, so
its asks and plan sat on the games repo's Platform and its PRs had no plan.
- **Cards follow the files:** *chosen* by the owner. Each sync reads every configured project's
  transcripts in one pass (a file unchanged since the last read is not read again), and routes an
  ask by the project its text names (a game's name names its repo), else its turn's files (three
  or more, 60%), else the folder its transcript is filed under; a plan by the repo most of its
  PRs went to. A card another board held is adopted with the owner's moves (the store's rule).
  - **Open docboard sessions in docboard's folder instead:** *rejected* by the owner: the past
    would have stayed on the wrong board.
  - **Route by each record's `cwd`:** *rejected*: measured, one session's records changed `cwd`
    back and forth between two repos; the files a turn touched say where its work was.
  - **The window since the last plan as a project rule:** *rejected*: measured, it would move
    four games-repo asks to docboard (a docs split run through Bash touched no files).
- **An ask that names no game and touched no game's files goes to the project's root:** *chosen*
  by the owner; the old fallback (the game of the session's commonest branch) is gone. On the
  live data 47 cards moved from a game to Platform; most are Platform's (CI budget, the Mac,
  keys), a few were game asks that name no game.
  - **Keep the guess, with a badge:** *rejected* by the owner.
- **Plans across continued sessions:** a session continued or forked from another holds copies
  of its parent's records, with the same times. Runs of approvals that share any approval are
  one plan, keyed by its first approval, whichever sessions hold it (a continuation may hold only
  the tail of a run). A PR joins the plan its own session holds, and a link votes for that same
  plan; each session's window ends at its own next plan, not its parent's. A copy's tie goes to
  the session that started first, then to the one whose own file was created first (the
  original), never to the folder read first.
- **A card routing gives another board is handed over in the same sync** (the row moves, with
  the owner's moves), rather than going gone until that board syncs.
- **The collapse floors count what the parser finds in every board's transcripts:** *chosen*.
  A board's own share of plans and asks moves when routing moves a card, so it is reported
  (`plan_calls_here`, `needs_items_here`) but not floored.
- A known one-time effect: the new routing and the started-first tie change which copy anchors a
  few re-raised asks, so on the first sync three open asks take new ids; a move the owner had made
  on the old id would not follow (the owner had made none).
- **A PR titled as the first of a series (`PR 1`, `PR A`) after the plan already holds one ends
  that plan's claim:** *chosen*; it detached Run 14's PRs from Run 13's plan.
  - **Close a plan at `/wrap-up` or `/compact`:** *rejected*: measured, it detached plans' own
    later PRs.
  - **Cap a plan at its `## PR` count:** *rejected*: an unrelated PR came first in one run.
- **In a repo with no GitHub remote, a plan is Done once its session moved on or went quiet:**
  *chosen*, since no PR will ever show its progress.

Revisit hook: `route.py`, `derive._ask_owner`, `_plan_owner`, `_pr_owner`, and
`transcripts.scan_all`.

## 2026-10-28 — Cards in the right place: checklists, wrapped titles, a PR's own files

- **The owner's checklists are read like ROADMAP items** (`docs/checklists/*.md`, a game's too,
  each `- [ ]` step a card under the file's title): *chosen* by the owner, with roblox-games'
  launch steps rewritten as `- [ ] **1. …**` so their numbers still read. A step under a later
  `##` section keeps the file's title for its game, and nothing inside a fenced code block (a
  shell `# comment`) is a heading or a step.
  - **Leave them as numbered lists:** *rejected*: the launch steps then reach the board only as
    asks sessions raise again in their own words.
- **A PR's files come from the oldest commit citing it**, the squash; a later Milestones commit
  ending in `(#N)` had supplied the files for #321 and #322. **A PR whose files span three or more
  games is the project's own:** *chosen*.
  - **The 60% file vote alone:** *rejected*: read correctly, #322 (moving every game's
    screenshots) would have gone to Yeet a Fridge, which held 616 of its 786 files.
- **An agent's own branch prefix (`codex/`, `claude/`) is looked past** when nothing matches:
  *chosen*, which put four Yeet a Fridge PRs on their game.
- **Two bullets with one title:** the owner's move follows the one whose text also matches,
  whether a duplicate was added or removed: renames are matched over all pairs at once, best
  first (the review found that one card at a time lost the move in 12 of 20 removals).
  **A reworded item whose source also moved it** drops the owner's move, as any source move does.
- **The collapse floor also guards `roadmap` and `decision_files`:** *chosen*.
  - **Also `questions` and `wrap_asks`:** *rejected*: they shrink as old transcripts are
    deleted, which `transcript_files` already watches.
- *Rejected* from the survey: an evidence line from a later similar question (little gain), and
  skipping one-prompt sessions as probes (23 of 53 real sessions have one prompt; key on the
  record's `entrypoint` if probes ever need excluding).

Revisit hook: `docsrc.parse_backlog` and `parse_checklist`, `gitrepo.squash_files`,
`attribution.by_branch` and `spread`, `store._renames`.

## 2026-10-28 — A sync the board can trust, and an undo before triage

The survey after the rollout found the board could go stale without a word: with gh down every
sync was refused (the collapse check ran before the gh freeze), a crash was swallowed by the
page's thread and retried on every view, and one broken project stopped `docboard sync`. Triage's
"Resolve all older" moved 109 cards in one click with no way back.
- **A real drop is accepted by a command, `docboard sync --accept-drop --project p`:** *chosen*
  by the owner. The refused banner names what dropped and gives the command; only the drops the
  last refusal named are accepted, so accepting one never accepts another.
  - **Also a button on the refused banner:** *rejected* by the owner: one click could make a
    parser bug the new normal.
- **A raise after the owner resolved a card reopens it in Needs you,** badged, its age and Older
  fold counted from the latest raise: *chosen* by the owner (sessions re-raise only when
  something changed).
  - **Keep it Done with a badge:** *rejected*: the change would show only to someone reading Done.
- **A card id another board holds is adopted once that board stops deriving it** (the row moves,
  with the owner's moves and history; until then the newcomer reports it as "elsewhere"):
  *chosen*, ahead of cards following their files between boards. Transcript ids are global by
  design, `/i/<id>` links stay valid, and no five-table rebuild touches the live database. The
  first holder keeps an id while it still derives it, so any number of boards that derive one id
  never trade it (the review found three nested boards could under an earlier rule).
  - **Key cards by (project, id):** *rejected* for those reasons; a PR two nested projects both
    derive shows on one board with a warning instead.
- **Every schema change of the run in one migration:** *chosen*, so the owner's running page is
  restarted once, not once per PR.
- **Undo by batch** (each card's move before and after kept in `batch_item`; undo restores only
  cards nothing moved since): *chosen*.
  - **Undo every event with the batch's timestamp:** *rejected*: it cannot put back a move the
    owner had made before the batch.
  - **A script confirm dialog:** *rejected*: the confirm is a page listing the cards, which works
    with no script under the Content-Security-Policy.
- A crash is a `failed` run (its type and first line, paths shown as names, never a traceback),
  which also restarts the page's two-minute wait, so a broken source is not resynced on every view.
- A known limit: an ask raised in one session and again in another becomes two cards once the
  first session's transcript is deleted (the kept-open first card, and a new one from the second
  session). Rare (it needs a 30-day-old transcript and a later re-raise); resolving either clears
  it.

Revisit hook: `Store.apply` (`_baseline`, `floor_drops`), `runner.run_sync`, `Store.move_batch` and
`undo_batch`.

## 2026-10-27 — The rollout: `/board`, docboard on its own board, and the cut-over at once

The board worked by hand (`docboard serve`); what remained was a way in from any session and
retiring the earlier dashboard (its SessionEnd hook, its `/sync-board` command and its block in the
owner's global CLAUDE.md).
- **`/board` and docboard as a second project, both boards side by side for a few days:**
  *rejected* by the owner, who took the cut-over now instead (the recommendation, so the old board
  would show what the new one misses before anything was retired).
- **The same plus a login service keeping the page up:** *deferred, worth trying* (Backlog).
- **The same plus the full cut-over now:** *chosen* by the owner. The hook, the command and the
  block went (backed up first); the old repo, its database and its own login service stay as they
  are. What is lost: the old hook flagged every project's sessions and a model scan read tasks,
  suggestions and failures out of them; this board reads only the configured projects, and only
  their structured sources (the model reading is the Backlog's "loose prose" item).
- `/board` runs `docboard board`, which finds the configured project (and game) of the session's
  folder, a git worktree's included, syncs it and prints its link: the folder-to-board rule is
  code with tests, not instructions in a prompt.

Revisit hook: `locate.py` for how a folder finds its board; the Backlog's login service.

## 2026-10-27 — The page's drag and drop is its own script, not htmx and SortableJS

The approved plan named vendored htmx and SortableJS for the page.
- **The browser's own drag-and-drop API in a 50-line script of the board's own, plus a Move
  control on every card that needs no script at all:** *chosen*. No third-party JavaScript
  enters a public repo, and the Content-Security-Policy can allow no script but this one file.
- **Vendored htmx and SortableJS, pinned and hash-checked:** *rejected*: two libraries to
  download, pin and keep current, for a drag and a form post the browser does natively.

Revisit hook: `src/docboard/static/board.js` and `templates/board.html`, if the board ever needs
reordering within a column or live updates.

## 2026-10-27 — A fresh board in its own repo, keeping its own state

The owner wants a Kanban board per Roblox game, and later per project, built from plans, session
logs and docs: a fresh implementation that replaces their earlier dashboard.

- **Where it lives:**
  - A new repo in `~/projects/automation` that reads any repo in the house docs shape, with a
    monorepo's games as sub-projects: *chosen* (the owner).
  - A `lune run board` tool inside the games repo: *rejected — Luau and one repo only; it would need
    a rewrite before any other project could use it.*
  - A Claude Artifact page: *deferred — worth trying* (Backlog).
- **Where a card move is saved:**
  - The board's own SQLite file, git-ignored, with every source read-only: *chosen* (the owner).
  - A `BOARD.md` committed in each game's docs: *rejected — every move would be a commit to main,
    and while the games repo merges on a local gate each would need a full gate run.*
- **The earlier dashboard:**
  - Replace it once this one works: retire its sync command and SessionEnd hook, leave its repo
    untouched: *chosen* (the owner).
  - Keep both running: *rejected — two boards for one purpose.*
  - Retire it now: *rejected — nothing would capture sessions in the meantime.*
- **Visibility:**
  - Public from the start: *chosen* (the owner). Free CI on GitHub's standard runners and a piece
    to show; every commit is public, so the privacy test guards every push.
  - Private for now: *rejected.* Local only: *rejected.*
- **How (the approved plan's picks):** Python 3.12 with uv, stdlib SQLite, FastAPI with Jinja2 and
  vendored htmx for the page; every v1 source parsed deterministically, with no model in the loop;
  card ids derived from content, never from a file's path, so moving a doc between folders keeps
  every card and its moves.
