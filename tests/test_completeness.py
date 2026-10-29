"""What the run's completeness check found unpinned or unfinished: the banners' last good sync,
links to a plan's PRs in other repos, the "new" window, renamed links, and the wiring from a real
sync into the store (game names, session notes, the transcript memo)."""

from __future__ import annotations

import os
import shutil
import socketserver
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import factories as f
import pytest
from boardkit import COUNTS, card, now, result
from fastapi.testclient import TestClient

from docboard import build, cli, gitrepo, runner, sync
from docboard import transcripts as T
from docboard.build import code_id
from docboard.config import Config, Project, Subprojects
from docboard.store import FloorError, Store
from docboard.web import create_app

PORT = 8742
BASE = f"http://127.0.0.1:{PORT}"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
GIT_WHO = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}  # fmt: skip


def git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", ""), **GIT_WHO}
    done = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
    return done.stdout.decode()


def make_repo(path: Path, files: dict[str, str]) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "x", "--allow-empty")
    return path


def client_for(tmp_path: Path) -> tuple[TestClient, Path]:
    cfg = Config(projects=(Project(name="demo", path=tmp_path),), claude_home=tmp_path / "c")
    db = tmp_path / "data" / "board.db"
    Store(db).close()
    return TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False), db


# --- banners ---------------------------------------------------------------------------------


def test_the_degraded_banner_says_what_it_keeps_and_since_when(tmp_path: Path) -> None:
    client, db = client_for(tmp_path)
    ask = card("ny:1", "needs_you", kind="needs")
    read = {**COUNTS, "ci_runs": 3}  # a sync that read main's CI
    with Store(db) as s:
        s.apply(result([ask], counts=read), "2026-10-20T10:00:00+00:00")
        s.apply(result([ask], counts=read, degraded=["ci: exit 4"]), "2026-10-21T10:00:00+00:00")
    page = client.get("/p/demo").text
    assert "CI cards keep their state from the sync of 2026-10-20 10:00 UTC" in page
    assert "PR and plan cards keep" not in page
    with Store(db) as s:
        s.apply(result([ask], counts=read, degraded=["gh: exit 4"]), "2026-10-22T10:00:00+00:00")
    page = client.get("/p/demo").text
    assert "PR and plan cards keep their state from the sync of 2026-10-21 10:00 UTC" in page


def test_a_degraded_source_never_read_says_so(tmp_path: Path) -> None:
    client, db = client_for(tmp_path)
    with Store(db) as s:
        s.apply(result([], degraded=["gh: not installed"]), "2026-10-20T10:00:00+00:00")
    assert "no sync has read them yet" in client.get("/p/demo").text


def test_failed_and_refused_banners_name_the_last_good_sync(tmp_path: Path) -> None:
    client, db = client_for(tmp_path)
    with Store(db) as s:
        s.apply(result([card("bl:1", "ideas")]), "2026-10-20T10:00:00+00:00")
        s.record_failed("demo", "2026-10-21T10:00:00+00:00", "GitError: boom")
    page = client.get("/p/demo").text
    assert "The last sync failed" in page and "the sync of 2026-10-20 10:00 UTC" in page
    with Store(db) as s, pytest.raises(FloorError):
        s.apply(result([], counts={**COUNTS, "backlog": 0}), "2026-10-22T10:00:00+00:00")
    page = client.get("/p/demo").text
    assert "The last sync was refused" in page and "the sync of 2026-10-20 10:00 UTC" in page


# --- links on a card --------------------------------------------------------------------------


def test_a_plans_prs_in_other_repos_are_links_to_their_cards(tmp_path: Path) -> None:
    client, db = client_for(tmp_path)
    source = {"plan": "pl:p1", "plan_title": "<b>Rewards</b>", "plan_project": "tool",
              "other_prs": ["acme/tool#4"]}  # fmt: skip
    with Store(db) as s:
        s.apply(result([card("pr:acme/demo#7", "done", kind="pr", source=source)]), now())
    page = client.get("/i/" + quote("pr:acme/demo#7", safe="")).text
    assert f'href="/i/{quote("pr:acme/tool#4", safe="/")}">acme/tool#4</a>' in page
    assert f'href="/i/{quote("pl:p1", safe="/")}">&lt;b&gt;Rewards&lt;/b&gt;</a>' in page
    assert "on the tool board" in page


# --- what counts as new -----------------------------------------------------------------------


def test_new_cards_are_counted_on_their_chip_for_a_day(tmp_path: Path) -> None:
    client, db = client_for(tmp_path)
    with Store(db) as s:
        s.apply(result([card("ny:1", "needs_you", kind="needs"),
                        card("ny:2", "needs_you", kind="needs", sub="beta")]), now())  # fmt: skip
        two_days = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        s.db.execute("UPDATE card SET first_seen = ? WHERE id = 'ny:2'", (two_days,))
        s.db.commit()
    page = client.get("/p/demo").text
    assert page.count('class="badge new"') == 1  # ny:2 is two days old, the window is one
    assert '<span class="n new">1 new</span>' in page  # on the All chip and alpha's


def test_looking_at_the_board_marks_nothing_seen(tmp_path: Path) -> None:
    client, db = client_for(tmp_path)
    with Store(db) as s:
        s.apply(result([card("ny:1", "needs_you", kind="needs")]), now())
    for _ in range(2):
        assert 'class="badge new"' in client.get("/p/demo").text
    with Store(db) as s:
        assert s.seen_mark("demo") is None


# --- renamed links ----------------------------------------------------------------------------


def test_a_link_follows_two_renames_and_no_more_than_five(tmp_path: Path) -> None:
    client, db = client_for(tmp_path)
    with Store(db) as s:
        s.apply(result([card("bl:c", "ideas"), card("bl:z", "ideas")]), now())
        hops = [("bl:a", "bl:b"), ("bl:b", "bl:c")] + [(f"bl:{i}", f"bl:{i + 1}") for i in range(6)]
        hops.append(("bl:6", "bl:z"))  # bl:0 is seven renames from bl:z
        for old, new in hops:
            s.db.execute("INSERT INTO alias VALUES (?, ?, 0.9, ?)", (old, new, now()))
        s.db.commit()
        assert s.renamed_to("bl:a") == "bl:c"
        assert s.renamed_to("bl:0") is None
    r = client.get("/i/bl:a")
    assert r.status_code == 303 and r.headers["location"].endswith("/i/" + quote("bl:c", safe=""))


# --- a real sync's wiring into what the page reads --------------------------------------------


def synced_project(tmp_path: Path, records: list[dict]) -> tuple[Config, Project]:
    files = {"docs/DECISIONS.md": "# Decisions\n", "games/alpha/game.toml": 'name = "Alpha Game"\n'}
    repo = make_repo(tmp_path / "repo", files)
    games = Subprojects(glob="games/*", name_from="game.toml:name")
    project = Project(name="demo", path=repo, docs_ref="HEAD", subprojects=games)
    f.write(tmp_path / "claude" / "projects" / T.encode_path(repo) / "s1.jsonl",
            [dict(r, cwd=str(repo)) for r in records])  # fmt: skip
    return Config(projects=(project,), claude_home=tmp_path / "claude"), project


def test_a_real_sync_stores_game_names_and_unplanned_answers(tmp_path: Path) -> None:
    q = {"questions": [{"question": "Which way?"}]}
    recs = [
        f.tool_use("s1", "u1", "2026-10-26T09:00:00Z", "AskUserQuestion", "q1", q),
        f.tool_result("s1", "r1", "2026-10-26T09:01:00Z", "q1", "ok",
                      {"answers": {"Which way?": "Left"}}),
    ]  # fmt: skip
    cfg, project = synced_project(tmp_path, recs)
    res = sync.collect(cfg, project, NOW)
    assert res.sub_names == {"alpha": "Alpha Game"}
    assert res.session_notes["s1"]["decided"] == ["Which way? → Left"]


def test_a_sync_reads_an_unchanged_transcript_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, project = synced_project(tmp_path, [f.human("s1", "h1", "2026-10-26T09:00:00Z", "hi")])
    memo = T.FileMemo()
    monkeypatch.setattr(sync, "MEMO", memo)
    sync.collect(cfg, project, NOW)
    sync.collect(cfg, project, NOW)
    assert memo.reads == 1


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX modes")
def test_a_transcript_that_cannot_be_read_is_counted_and_warned(tmp_path: Path) -> None:
    cfg, project = synced_project(tmp_path, [f.human("s1", "h1", "2026-10-26T09:00:00Z", "hi")])
    path = tmp_path / "claude" / "projects" / T.encode_path(project.path) / "s1.jsonl"
    path.chmod(0)
    try:
        res = sync.collect(cfg, project, NOW)
    finally:
        path.chmod(0o600)
    assert res.counts["transcript_unreadable"] == 1
    assert any("could not be read" in w for w in res.warnings)


class _GrowingOs:
    """os for the transcript reader, whose second fstat of each file sees it one byte longer:
    the file grew while it was read."""

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> object:
        return getattr(os, name)

    def fstat(self, fd: int) -> os.stat_result:
        self.calls += 1
        st = os.fstat(fd)
        if self.calls % 2:
            return st
        fields = list(st[:10])
        fields[6] += 1  # st_size
        return os.stat_result(fields)


def test_a_file_that_grew_while_read_is_not_remembered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    f.write(projects / T.encode_path(f.ROOT) / "s1.jsonl",
            [f.human("s1", "h1", "2026-10-26T09:00:00Z", "hi")])  # fmt: skip
    memo = T.FileMemo()
    with monkeypatch.context() as m:
        m.setattr(T, "os", _GrowingOs())
        T.scan_all([("demo", f.ROOT)], projects, memo)
    T.scan_all([("demo", f.ROOT)], projects, memo)
    assert memo.reads == 2


# --- the CLI ------------------------------------------------------------------------------------


def test_board_prints_the_accept_command_when_its_sync_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    conf = tmp_path / "p.toml"
    conf.write_text(f"[[project]]\npath = '{repo}'\nname = \"demo\"\n", encoding="utf-8")

    def refused(*args: object, **kw: object) -> None:
        raise FloorError(["backlog: 10 → 0"])

    monkeypatch.setattr(runner, "run_sync", refused)
    cli.main(["board", "--config", str(conf), "--cwd", str(repo), "--port", "9"])
    out = capsys.readouterr().out
    assert "accept it:" in out and "sync --accept-drop --project demo" in out


class _SamePage(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.request.recv(4096)
        head = f"HTTP/1.1 200 OK\r\nX-Docboard-Code: {code_id()}\r\nContent-Length: 0\r\n\r\n"
        self.request.sendall(head.encode())


def test_board_says_nothing_of_restarting_a_page_on_this_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _SamePage)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        repo = tmp_path / "demo"
        repo.mkdir()
        conf = tmp_path / "p.toml"
        conf.write_text(f"[[project]]\npath = '{repo}'\nname = \"demo\"\n", encoding="utf-8")
        port = str(httpd.server_address[1])
        cli.main(["board", "--config", str(conf), "--cwd", str(repo), "--no-sync", "--port", port])
        out = capsys.readouterr().out
        assert "page: running" in out and "restart" not in out
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_code_id_changes_with_any_package_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    copy = tmp_path / "pkg"
    shutil.copytree(build.HERE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(build, "HERE", copy)
    code_id.cache_clear()  # computed once per process: the code a running page loaded
    try:
        first = code_id()
        page = copy / "templates" / "card.html"
        page.write_text(page.read_text(encoding="utf-8") + "<!-- -->\n", encoding="utf-8")
        code_id.cache_clear()
        assert code_id() != first
    finally:
        code_id.cache_clear()


# --- the docs ref -------------------------------------------------------------------------------


def test_the_docs_ref_defaults_to_origins_head_then_main_then_the_working_tree(
    tmp_path: Path,
) -> None:
    upstream = make_repo(tmp_path / "up", {"docs/a.md": "committed\n"})
    git(upstream, "branch", "trunk")
    git(upstream, "symbolic-ref", "HEAD", "refs/heads/trunk")  # its default is not main
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(upstream), str(clone)], check=True,
                   capture_output=True)  # fmt: skip
    assert gitrepo.Repo.open(clone, None).ref == "origin/trunk"  # origin/HEAD names it
    git(clone, "remote", "set-head", "origin", "--delete")
    assert gitrepo.Repo.open(clone, None).ref == "origin/main"  # then main, by name
    loose = make_repo(tmp_path / "loose", {"docs/a.md": "committed\n"})
    (loose / "docs" / "a.md").write_text("in the working tree\n", encoding="utf-8")
    repo = gitrepo.Repo.open(loose, None)
    assert repo.ref is None and repo.read("docs/a.md") == "in the working tree\n"


def test_the_last_read_is_found_however_many_degraded_syncs_came_since(tmp_path: Path) -> None:
    with Store(tmp_path / "board.db") as s:
        s.apply(result([]), "2026-10-01T10:00:00+00:00")
        start = datetime(2026, 10, 2, tzinfo=UTC)
        for i in range(501):
            s.apply(result([], degraded=["gh: auth"]), (start + timedelta(minutes=i)).isoformat())
        assert s.last_read("demo", "gh") == "2026-10-01T10:00:00+00:00"


def test_a_sync_from_before_ci_was_read_is_no_ci_read(tmp_path: Path) -> None:
    old = {k: v for k, v in COUNTS.items() if k != "ci_runs"}
    with Store(tmp_path / "board.db") as s:
        s.apply(result([], counts=old, degraded=["gh: not installed"]), "2026-10-25T10:00:00+00:00")
        assert s.last_read("demo", "ci") is None  # the CI source did not exist yet
        s.apply(result([], counts={**COUNTS, "ci_runs": 3}), "2026-10-26T10:00:00+00:00")
        assert s.last_read("demo", "ci") == "2026-10-26T10:00:00+00:00"
