# docboard

A Kanban board for each project, built from what the project already writes down: the Backlog in
`docs/DECISIONS.md`, `ROADMAP.md` checkboxes, its GitHub pull requests, the plans approved in Claude
Code, and the "Needs you" items those sessions raised. Every source is read-only; a card you move is
remembered in a local SQLite file. A monorepo's sub-projects (each game of a games monorepo) get a
board of their own.

Status: the sources are in. `docboard sync --dry-run` reads every source and prints the board as a
table; the database and the web page come next.

## Run it

```sh
uv sync                                    # Python 3.12 and the dev tools, from uv.lock
cp config/projects.example.toml config/projects.toml   # then list your projects
uv run docboard sync --dry-run --sample 3  # every source, the board as a table, a few cards
uv run docboard sync --dry-run --json data/{project}.json
uv run docboard diff data/before.json data/after.json --kinds idea,roadmap --strict
./scripts/gate.sh                          # lint, format check and tests: what CI runs
uv run docboard privacy                    # scan the repo for personal identifiers
```

Only the projects listed in `config/projects.toml` are read. The `--json` files hold your own
sessions' text, so they are written owner-only (mode 600) and `data/` is git-ignored.

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
  sub-project's `docs/DECISIONS.md`, and `ROADMAP.md` at either level.
- **Pull requests** (`ghsrc.py`) through `gh`; a failure shows as "degraded", never as no PRs.
  A PR belongs to the game its title names, else its branch prefix, else most of its files.
- **Transcripts** (`transcripts.py`) are the project's `~/.claude/projects/` folders, its
  worktrees' included. Each line is checked with byte substrings before any JSON is parsed. Items
  come only from main-session assistant text and tool calls, never from tool results, which can
  hold a web page's text. A record copied into several files (forks, re-attaches) counts once.
- **Plans** (`derive.py`) are cards per run of approvals with one title. A PR joins the plan its
  session approved last before the PR was created. A plan is Done when every PR it named is in.
  With no PR list it is Done once its session moved on, went quiet for a day, or merged nothing
  for a week. A partly merged plan quiet for two weeks moves to Done with the gap on its card.
- **Ids** (`ids.py`) come from content, never a path, so a doc moved into a game's folder keeps
  its cards; they are frozen behind `ID_VERSION`.
- **Floors**: `--baseline` fails a sync when a source that cannot be empty fell to zero or by half.
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
- 8 guards each proven by a deliberate sabotage before merging: dedupe, ids, the plan rule, the
  collapse floor, Needs-you persistence, disk-saved approvals, the folder rule and sidechains.
- A supply-chain cooldown on every dependency: uv resolves only releases at least 14 days old,
  checked against PyPI's upload times (ruff held at 0.17.0 while 0.17.1 to 0.17.3 were inside the
  window).
- A privacy gate over 3 kinds of personal identifier, proven to fail on a planted home path before
  the first push to a public repo.
- CI on GitHub Actions with both third-party actions pinned to full commit SHAs and a read-only
  token.
