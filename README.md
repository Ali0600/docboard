# docboard

A Kanban board for each project, built from what the project already writes down: the Backlog in
`docs/DECISIONS.md`, `ROADMAP.md` checkboxes, its GitHub pull requests, the plans approved in Claude
Code, and the "Needs you" items those sessions raised. Every source is read-only; a card you move is
remembered in a local SQLite file. A monorepo's sub-projects (each game of a games monorepo) get a
board of their own.

Status: early. The repo skeleton, the gate and the privacy check are in; the source parsers and
`docboard sync --dry-run` come next, then the web page.

## Run it

```sh
uv sync                    # Python 3.12 and the dev tools, from uv.lock
uv run docboard --version
./scripts/gate.sh          # lint, format check and tests: what CI runs
uv run docboard privacy    # scan the repo for personal identifiers
```

Only the projects listed in a local `config/projects.toml` are ever read (an example lands with the
parsers).

## How it works

- `src/docboard/cli.py`: the `docboard` command.
- `src/docboard/privacy.py`: the push guard. This repo is public, so every tracked or addable file is
  scanned for home-folder paths naming a real user, email addresses outside the placeholder domains,
  and the terms in a local, git-ignored `config/private-identifiers.txt`. `tests/test_privacy.py`
  runs it over the whole repo on every gate.
- Local state never enters git: `data/` (the database), `config/projects.toml` and the private list.
- Dependencies resolve only to releases uploaded at least 14 days ago (`[tool.uv] exclude-newer`).
- Decisions and their rejected alternatives: [docs/DECISIONS.md](docs/DECISIONS.md).

## Experience Gained

- A supply-chain cooldown on every dependency: uv resolves only releases at least 14 days old,
  checked against PyPI's upload times (ruff held at 0.17.0 while 0.17.1 to 0.17.3 were inside the
  window).
- A privacy gate over 3 kinds of personal identifier, proven to fail on a planted home path before
  the first push to a public repo.
- CI on GitHub Actions with both third-party actions pinned to full commit SHAs and a read-only
  token.
