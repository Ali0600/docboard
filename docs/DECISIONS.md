# Decisions

## Backlog — alternatives worth trying later

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
