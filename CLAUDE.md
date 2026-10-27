# docboard — agent contract

## Commands

| Command | Does |
|---|---|
| `./scripts/gate.sh` | `uv sync --locked`, `ruff check`, `ruff format --check`, `pytest`; every step runs and reports, non-zero if any failed. CI runs exactly this. |
| `uv run docboard sync --dry-run [--project p] [--sample n] [--json file] [--baseline file]` | Reads every source of the configured projects and prints counts per source and the board as a table; `--json` writes every card (owner-only file), `--baseline` exits 1 when a source collapsed since that file. |
| `uv run docboard sync [--project p] [--db file]` | Reads every source into the board's database (`data/board.db`) under a machine-wide lock; refuses, writing nothing, when a source collapsed since the last good sync. |
| `uv run docboard serve [--port 8742]` | The board on `http://127.0.0.1:8742`; any host but loopback is refused. Start it in the background and check it in the built-in browser. |
| `uv run docboard diff a.json b.json [--kinds idea,roadmap] [--strict]` | Card ids only in one run, and fields changed in both; `--strict` exits 1 when an id came or went (the check a docs move must pass). |
| `uv run docboard privacy` | Scans every tracked or addable file for personal identifiers. |
| `uv run pytest -k <name>` | One test while iterating. |

## Rules an agent must not guess

- **This repo is public.** Never commit real transcript text, real home paths, email addresses, or
  names of the owner's private projects. Test fixtures are synthetic, built in `tests/factories.py`
  with the transcripts' own serializer (compact separators, raw UTF-8), never copied from a
  transcript. `tests/test_privacy.py` must pass before every push; a new kind of identifier gets a
  pattern there and a test that fails without it.
- **Local state stays out of git:** `data/`, `config/projects.toml`, `config/private-identifiers.txt`
  (all in `.gitignore`; prove a new one with `git check-ignore -v <path>`).
- **Sources are read-only.** Never write to a configured repo, `~/.claude/plans` or a transcript.
  Read only the projects listed in `config/projects.toml`; never discover projects on disk. Docs
  are read at the configured ref, never a working tree another session may be using.
- **Items come only from assistant text and tool calls in main-session records.** Never read
  items out of a tool result or a sidechain: a tool result can hold any web page's text.
- **Store extracted fields only**, never raw transcript records.
- **Card ids are frozen** (`ids.py`, `ID_VERSION`, golden values in `tests/test_ids.py`). An id
  never includes a file path. Changing a derivation means a new version and a migration of stored
  moves.
- **A Needs-you item never leaves that column by derivation**; only the owner's action moves it.
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
