from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import factories as f

from docboard import sync
from docboard.config import Config, Project, Subprojects
from docboard.transcripts import encode_path

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=GIT_ENV)


def make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if not (repo / ".git").exists():
        git(repo, "init", "-q", "-b", "main")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "docs", "--allow-empty")
    return repo


BACKLOG = (
    "# Decisions\n\n## Backlog — later\n\n"
    "- **Alpha Game's sky** (2026-10-01): x.\n- **A shared lib**: y.\n"
)


def run(tmp_path: Path, repo: Path) -> sync.SyncResult:
    project = Project(
        name="demo",
        path=repo,
        docs_ref="HEAD",
        subprojects=Subprojects(glob="games/*", exclude=("_harness",), name_from="game.toml:name"),
    )
    cfg = Config(projects=(project,), claude_home=tmp_path / "claude")
    return sync.collect(cfg, project, NOW)


def test_collect_reads_docs_at_the_ref_and_attributes_by_name(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "docs/DECISIONS.md": BACKLOG,
            "games/alpha/game.toml": 'name = "Alpha Game"\n',
            "games/_harness/game.toml": 'name = "Harness"\n',
            "games/alpha/ROADMAP.md": "# R\n\n## 1. One\n\n- [ ] A thing.\n",
        },
    )
    (repo / "docs/DECISIONS.md").write_text("uncommitted edits are not read\n")
    result = run(tmp_path, repo)
    assert result.subs == ["alpha"]
    assert result.counts["backlog"] == 2 and result.counts["roadmap"] == 1
    subs = sorted((c.kind, c.sub) for c in result.cards)
    assert subs == [("idea", ""), ("idea", "alpha"), ("roadmap", "alpha")]
    assert result.degraded == []  # no remote, so no GitHub to ask


def test_moving_a_bullet_into_a_game_keeps_every_id(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path, {"docs/DECISIONS.md": BACKLOG, "games/alpha/game.toml": 'name = "Alpha Game"\n'}
    )
    before = {c.id: c for c in run(tmp_path, repo).cards}
    root_only = "# Decisions\n\n## Backlog — later\n\n- **A shared lib**: y.\n"
    game = "# Alpha — decisions\n\n## Backlog — later\n\n- **Alpha Game's sky** (2026-10-01): x.\n"
    make_repo(tmp_path, {"docs/DECISIONS.md": root_only, "games/alpha/docs/DECISIONS.md": game})
    after = {c.id: c for c in run(tmp_path, repo).cards}
    assert before.keys() == after.keys()
    moved = next(c for c in after.values() if c.title == "Alpha Game's sky")
    assert (moved.sub_reason, moved.source["path"]) == ("path", "games/alpha/docs/DECISIONS.md")


def test_transcripts_of_the_project_are_scanned(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": BACKLOG})
    rec = dict(
        f.assistant_text("s1", "a1", "2026-10-26T10:00:00Z", f.needs_block("Do — x")), cwd=str(repo)
    )
    f.write(tmp_path / "claude" / "projects" / encode_path(repo) / "s1.jsonl", [rec])
    result = run(tmp_path, repo)
    assert result.counts["transcript_files"] == 1 and result.counts["needs_cards"] == 1


def test_a_failing_gh_degrades_instead_of_reporting_no_prs(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": BACKLOG})
    fake = tmp_path / "gh"
    fake.write_text("#!/bin/sh\necho 'auth: keychain locked' >&2\nexit 4\n")
    fake.chmod(0o755)
    project = Project(name="demo", path=repo, docs_ref="HEAD", github="acme/demo")
    cfg = Config(projects=(project,), claude_home=tmp_path / "claude")
    result = sync.collect(cfg, project, NOW, gh=str(fake))
    assert result.counts["prs"] == 0
    assert result.degraded and "keychain locked" in result.degraded[0]


def test_floors_flag_a_source_that_collapsed() -> None:
    base = {
        "backlog": 140,
        "prs_merged": 300,
        "transcript_files": 50,
        "plan_calls": 90,
        "needs_items": 170,
    }
    assert sync.floor_problems(dict(base), base) == []
    assert sync.floor_problems({**base, "backlog": 0}, base) == ["backlog: 140 → 0"]
    assert sync.floor_problems({**base, "needs_items": 60}, base) == [
        "needs_items: 170 → 60 (more than half gone)"
    ]
    assert sync.floor_problems({"backlog": 0}, {}) == []  # nothing to compare with


def test_the_json_file_is_private(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": BACKLOG})
    out = tmp_path / "out" / "board.json"
    sync.write_json(run(tmp_path, repo), out)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
