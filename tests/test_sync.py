from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import factories as f
import pytest
from boardkit import COUNTS
from boardkit import result as kit_result

from docboard import cli, ghsrc, runner, sync
from docboard.config import Config, Project, Subprojects
from docboard.store import Store
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


# --- PR A: a sync you can trust ------------------------------------------------------------

MERGED = (
    '[{"number": 1, "title": "x", "state": "MERGED", "createdAt": "2026-10-20T00:00:00Z",'
    ' "mergedAt": "2026-10-21T00:00:00Z", "closedAt": null, "headRefName": "b"}]'
)


def fake_gh(path: Path, script: str) -> Path:
    path.write_text("#!/bin/sh\n" + script + "\n")
    path.chmod(0o755)
    return path


def gh_project(tmp_path: Path, repo: Path) -> tuple[Config, Project]:
    project = Project(name="demo", path=repo, docs_ref="HEAD", github="acme/demo")
    return Config(projects=(project,), claude_home=tmp_path / "claude"), project


def test_gh_failing_after_a_good_sync_keeps_the_board_syncing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": BACKLOG})
    cfg, project = gh_project(tmp_path, repo)
    db = tmp_path / "data" / "board.db"
    good = fake_gh(tmp_path / "gh-good", f"cat <<'J'\n{MERGED}\nJ")
    monkeypatch.setattr(ghsrc, "find_gh", lambda: str(good))
    runner.run_sync(cfg, project, db, now=NOW)
    rec = dict(
        f.assistant_text("s1", "a1", "2026-10-27T10:00:00Z", f.needs_block("Do — x")),
        cwd=str(repo),
    )
    f.write(tmp_path / "claude" / "projects" / encode_path(repo) / "s1.jsonl", [rec])
    bad = fake_gh(tmp_path / "gh-bad", "echo 'auth: keychain locked' >&2; exit 4")
    monkeypatch.setattr(ghsrc, "find_gh", lambda: str(bad))
    runner.run_sync(cfg, project, db, now=NOW)
    with Store(db) as s:
        assert s.last_run("demo")["status"] == "ok"
        assert s.card("ny:a1:1") is not None  # the new ask arrived while gh was down
        assert s.card("pr:acme/demo#1")["gone_at"] is None


def test_a_hung_gh_degrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": BACKLOG})
    cfg, project = gh_project(tmp_path, repo)
    monkeypatch.setattr(ghsrc, "TIMEOUT", 0.3)
    hung = fake_gh(tmp_path / "gh", "exec sleep 5")
    got = sync.collect(cfg, project, NOW, gh=str(hung))
    assert got.degraded and "timed out" in got.degraded[0]


def test_gh_output_that_is_not_json_degrades(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": BACKLOG})
    cfg, project = gh_project(tmp_path, repo)
    chatty = fake_gh(tmp_path / "gh", "echo 'Welcome to gh! A new release is out.'")
    got = sync.collect(cfg, project, NOW, gh=str(chatty))
    assert got.degraded and "not the expected JSON" in got.degraded[0]


def test_a_folder_that_is_not_git_is_recorded_as_failed(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    project = Project(name="demo", path=plain)
    cfg = Config(projects=(project,), claude_home=tmp_path / "claude")
    db = tmp_path / "data" / "board.db"
    with pytest.raises(runner.SyncFailed):
        runner.run_sync(cfg, project, db, now=NOW)
    with Store(db) as s:
        run = s.last_run("demo")
    assert run["status"] == "failed" and run["message"].startswith("GitError:")
    assert "Traceback" not in run["message"] and str(tmp_path) not in run["message"]


def test_a_crash_inside_apply_writes_only_a_failed_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": BACKLOG})
    project = Project(name="demo", path=repo, docs_ref="HEAD")
    cfg = Config(projects=(project,), claude_home=tmp_path / "claude")
    db = tmp_path / "data" / "board.db"
    runner.run_sync(cfg, project, db, now=NOW)
    with Store(db) as s:
        before = {c["id"]: c["title"] for c in s.cards("demo")}

    def boom(self: Store, *args: object, **kw: object) -> None:
        self.db.execute("UPDATE card SET title = 'half-written'")
        raise KeyError("column")

    monkeypatch.setattr(Store, "_apply", boom)
    with pytest.raises(runner.SyncFailed, match="KeyError"):
        runner.run_sync(cfg, project, db, now=NOW)
    with Store(db) as s:
        assert [r[0] for r in s.db.execute("SELECT status FROM sync_run ORDER BY id")] == [
            "ok", "failed",
        ]  # fmt: skip
        assert {c["id"]: c["title"] for c in s.cards("demo")} == before  # rolled back


def two_projects(tmp_path: Path) -> Path:
    plain = tmp_path / "plain"
    plain.mkdir()
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": BACKLOG})
    conf = tmp_path / "projects.toml"
    conf.write_text(
        f'claude_home = "{tmp_path / "claude"}"\n\n'
        f'[[project]]\nname = "broken"\npath = "{plain}"\n\n'
        f'[[project]]\nname = "demo"\npath = "{repo}"\ndocs_ref = "HEAD"\n',
        encoding="utf-8",
    )
    return conf


def test_sync_goes_on_past_a_broken_project_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = two_projects(tmp_path)
    code = cli.main(["sync", "--config", str(conf), "--db", str(tmp_path / "board.db")])
    out = capsys.readouterr().out
    assert code == 1
    assert "broken: FAILED" in out and "GitError" in out
    assert "demo: 2 cards" in out  # the next project still synced


def test_accept_drop_needs_a_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conf = two_projects(tmp_path)
    db = str(tmp_path / "board.db")
    assert cli.main(["sync", "--accept-drop", "--config", str(conf), "--db", db]) == 2
    dry = ["sync", "--accept-drop", "--dry-run", "--project", "demo", "--config", str(conf)]
    assert cli.main(dry) == 2
    assert "--accept-drop" in capsys.readouterr().err


def test_sync_accept_drop_stores_the_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = two_projects(tmp_path)
    db = str(tmp_path / "board.db")
    small = {**COUNTS, "backlog": 3}
    runs = iter([kit_result([]), kit_result([], small), kit_result([], small)])
    monkeypatch.setattr(runner, "collect", lambda cfg, project, now: next(runs))
    args = ["sync", "--project", "demo", "--config", str(conf), "--db", db]
    assert cli.main(args) == 0
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "--accept-drop --project demo" in out
    assert cli.main([*args, "--accept-drop"]) == 0
    assert "ACCEPTED backlog: 10 → 3" in capsys.readouterr().out


def test_describe_names_the_project_not_its_path(tmp_path: Path) -> None:
    project = Project(name="demo", path=tmp_path / "repo")
    home = Path.home()
    e = OSError(f"cannot read {tmp_path / 'repo' / 'x'} or {home / 'y'}\nsecond line")
    text = runner.describe(e, project)
    assert text == "OSError: cannot read demo/x or ~/y"


def test_dry_run_goes_on_past_a_broken_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = two_projects(tmp_path)
    assert cli.main(["sync", "--dry-run", "--config", str(conf)]) == 1
    out = capsys.readouterr().out
    assert "broken: FAILED" in out and "demo: docs at" in out


# --- PR B: cards in the right place ---------------------------------------------------------


def test_squash_files_uses_the_squash_not_a_later_citing_commit(tmp_path: Path) -> None:
    from docboard.gitrepo import Repo

    repo = make_repo(tmp_path, {"games/alpha/a.luau": "a", "games/alpha/b.luau": "b"})
    (repo / "games/alpha/a.luau").write_text("a2")
    git(repo, "commit", "-q", "-am", "Alpha's rewards (#5)")
    (repo / "docs").mkdir()
    (repo / "docs/MILESTONES.md").write_text("- rewards (#5)\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Milestones: the rewards (#5)")  # a later docs commit cites it
    assert Repo.open(repo, "HEAD").squash_files()[5] == ["games/alpha/a.luau"]


CHECKS = "# Alpha Game: going public\n\n- [ ] **1. Look.** At it.\n- [x] **2. Badges.**\n"


def test_checklist_steps_become_cards_under_their_game(tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        {
            "docs/DECISIONS.md": BACKLOG,
            "docs/checklists/launch.md": CHECKS,
            "docs/checklists/plain.md": "# A procedure\n\n1. Do this.\n2. Then that.\n",
            "games/alpha/game.toml": 'name = "Alpha Game"\n',
            "games/alpha/docs/checklists/release.md": "# Release\n\n- [ ] Tag it.\n",
        },
    )
    got = run(tmp_path, repo)
    steps = sorted(
        (c.title, c.column, c.sub, c.badges[0]) for c in got.cards if c.kind == "roadmap"
    )
    assert steps == [
        ("**1. Look.** At it.", "next", "alpha", "Alpha Game: going public"),
        ("**2. Badges.**", "done", "alpha", "Alpha Game: going public"),
        ("Tag it.", "next", "alpha", "Release"),
    ]
    assert got.counts["checklist_items"] == 3


def test_floors_guard_the_docs_too() -> None:
    base = {"roadmap": 54, "decision_files": 9}
    assert sync.floor_problems({"roadmap": 0, "decision_files": 9}, base) == ["roadmap: 54 → 0"]
    assert sync.floor_problems({"roadmap": 54, "decision_files": 2}, base) == [
        "decision_files: 9 → 2 (more than half gone)"
    ]


def test_a_backlog_bullet_the_parser_lost_is_warned_about(tmp_path: Path) -> None:
    lost = "# D\n\n## Backlog — later\n\n- **Fine**: x.\n- **Never closed\n- **Also fine**: y.\n"
    repo = make_repo(tmp_path, {"docs/DECISIONS.md": lost})
    got = run(tmp_path, repo)
    assert any("3 Backlog bullets start but 2 were read" in w for w in got.warnings)
