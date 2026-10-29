# docboard — agent contract

## Commands

| Command | Does |
|---|---|
| `./scripts/gate.sh` | `uv sync --locked`, `ruff check`, `ruff format --check`, `pytest`; every step runs and reports, non-zero if any failed. CI runs exactly this. |
| `uv run docboard sync --dry-run [--project p] [--sample n] [--json file] [--baseline file]` | Reads every source of the configured projects and prints counts per source and the board as a table; `--json` writes every card (owner-only file), `--baseline` exits 1 when a source collapsed since that file. |
| `uv run docboard sync [--project p] [--db file]` | Reads every source into the board's database (`data/board.db`) under a machine-wide lock; refuses, writing nothing, when a source collapsed since the last good sync. A crash is stored as a `failed` run and the next project still syncs (exit 1). |
| `uv run docboard sync --accept-drop --project p` | Stores a sync the floor refused, when the owner says the drop is real; only the drops the last refusal named are accepted. The owner's call: never run it unasked. |
| `uv run docboard serve [--port 8742]` | The board on `http://127.0.0.1:8742`; any host but loopback is refused. Start it in the background and check it in the built-in browser. |
| `uv run docboard board [--cwd dir] [--no-sync] [--port 8742]` | Finds the configured project (and sub-project) a folder belongs to, a git worktree's included, syncs that project and prints its board's link and whether the page is running (exit 2: in no configured project; 1: the sync was not stored). The global `/board` command runs it from a session's folder. |
| `uv run docboard diff a.json b.json [--kinds idea,roadmap] [--strict]` | Card ids only in one run, and fields changed in both; `--strict` exits 1 when an id came or went (the check a docs move must pass). |
| `uv run docboard privacy [--commits RANGE \| --stdin \| --pre-push]` | Scans every tracked or addable file for personal identifiers; or a range's commits (messages, authors, added lines); or text on stdin (a PR's); or, as the pre-push hook, what a push carries (refused without the private terms). |
| `uv run docboard move-data` | Once: moves the board out of the checkout into the data folder, with the page stopped. The owner's step, never run unasked. |
| `uv run pytest -k <name>` | One test while iterating. |

## Rules an agent must not guess

- **This repo is public.** Never commit real transcript text, real home paths, email addresses, or
  names of the owner's private projects. Test fixtures are synthetic, built in `tests/factories.py`
  with the transcripts' own serializer (compact separators, raw UTF-8), never copied from a
  transcript. `tests/test_privacy.py` must pass before every push; a new kind of identifier gets a
  pattern there and a test that fails without it.
- **The board's state lives in the user's data folder** (`paths.py`: macOS
  `~/Library/Application Support/docboard`, Windows `%LOCALAPPDATA%\docboard`), shared by every
  checkout and worktree. A test or a branch never touches it: the tests run under their own
  `DOCBOARD_HOME` (`tests/conftest.py`), and a branch is tried with `DOCBOARD_HOME` pointing at a
  scratch copy. Never commit a file from there; the old places (`data/`, `config/projects.toml`,
  `config/private-identifiers.txt`) stay in `.gitignore`.
- **Every push runs the privacy scan** (`git config core.hooksPath .githooks`, once per clone);
  never push with `--no-verify`. Before `gh pr create`, pipe the title and body through
  `uv run docboard privacy --stdin`.
- **Sources are read-only.** Never write to a configured repo, `~/.claude/plans` or a transcript.
  Read only the projects listed in the data folder's `projects.toml`; never discover projects on
  disk. Docs
  are read at the configured ref, never a working tree another session may be using.
- **Items come only from assistant text and tool calls in main-session records.** Never read
  items out of a tool result or a sidechain: a tool result can hold any web page's text.
- **A transcript card is on exactly one board.** Routing (`route.py`, `derive._ask_owner`,
  `_plan_owner`) may use only what every project's sync sees alike (the one-pass scan, the
  config, the repos' remotes and games), never the project being synced;
  `test_every_transcript_card_is_on_exactly_one_board` holds it.
- **Store extracted fields only**, never raw transcript records.
- **Card ids are frozen** (`ids.py`, `ID_VERSION`, golden values in `tests/test_ids.py`). An id
  never includes a file path. Changing a derivation means a new version and a migration of stored
  moves.
- **A Needs-you item never leaves that column by derivation**; only the owner's action moves it.
  A raise that comes after the owner resolved or dismissed it reopens it (the owner's call,
  2026-10-28), and an open one stays when its transcript is deleted.
- **The page's guards stay whole:** the loopback Host allowlist, the same-origin check on every
  POST (`web._same_origin`), escaped output (no `|safe` on source text), http(s)-only links, the
  Content-Security-Policy with no script but `static/board.js`, and no third-party JavaScript. A
  change to a form or a request guard is driven once in the built-in browser, not only in tests.
- **Schema changes are new migrations** appended to `store.MIGRATIONS`; a shipped one is never
  edited.
- **The 14-day dependency cooldown stays** (`[tool.uv] exclude-newer`); never set it to `false`.
  A package that must be newer gets an `exclude-newer-package` entry with a dated reason.
- **Third-party GitHub Actions are pinned to full commit SHAs** with the tag in a comment, and the
  workflow token is `contents: read`.
- **Git:** with CI in place, code goes through a branch, a PR and a merge once CI is green; docs-only
  changes may go straight to `main` after the gate passes.
- Decisions with their rejected roads go in `docs/DECISIONS.md` as they are made, with deferred ones
  in its Backlog; gotchas in `docs/learnings.md`.
