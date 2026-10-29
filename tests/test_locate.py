from __future__ import annotations

import os
import socket
import socketserver
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest
from boardkit import result

from docboard import cli, locate, runner
from docboard.config import Config, Project, Subprojects
from docboard.gitrepo import GitError
from docboard.store import ApplyReport

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=GIT_ENV)


def make_repo(root: Path) -> Path:
    """A monorepo with two games and a harness, committed so worktrees can be made from it."""
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    for rel in ("games/alpha/src/a.txt", "games/beta/b.txt", "games/_harness/h.txt", "docs/d.md"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "files")
    return root


def mono(path: Path, name: str = "demo") -> Project:
    subs = Subprojects(glob="games/*", exclude=("_harness",))
    return Project(name=name, path=path, subprojects=subs)


def cfg(*projects: Project) -> Config:
    return Config(projects=tuple(projects), claude_home=Path("/nonexistent"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return make_repo(tmp_path / "demo")


def where(c: Config, cwd: Path) -> tuple[str, str | None] | None:
    found = locate.locate(c, cwd)
    return None if found is None else (found.project.name, found.sub)


def test_a_folder_finds_its_project_and_game(repo: Path) -> None:
    c = cfg(mono(repo))
    assert where(c, repo) == ("demo", None)
    assert where(c, repo / "docs") == ("demo", None)
    assert where(c, repo / "games") == ("demo", None)
    assert where(c, repo / "games" / "alpha") == ("demo", "alpha")
    assert where(c, repo / "games" / "alpha" / "src") == ("demo", "alpha")
    assert where(c, repo / "games" / "_harness") == ("demo", None)  # excluded: no board of its own


def test_a_worktree_finds_the_main_checkouts_board(repo: Path, tmp_path: Path) -> None:
    c = cfg(mono(repo))
    inside = repo / ".claude" / "worktrees" / "wt"
    git(repo, "worktree", "add", "-q", "-b", "wt", str(inside))
    elsewhere = tmp_path / "elsewhere" / "wt2"
    git(repo, "worktree", "add", "-q", "-b", "wt2", str(elsewhere))
    assert where(c, inside) == ("demo", None)
    assert where(c, inside / "games" / "beta") == ("demo", "beta")
    assert where(c, elsewhere / "games" / "alpha" / "src") == ("demo", "alpha")
    assert where(c, elsewhere / "docs") == ("demo", None)


def test_a_folder_outside_every_project_finds_none(repo: Path, tmp_path: Path) -> None:
    other = make_repo(tmp_path / "other")  # a git repo, but not in the config
    plain = tmp_path / "plain"
    plain.mkdir()
    sibling = tmp_path / "demo-copy" / "games" / "alpha"  # its name starts with the project's
    sibling.mkdir(parents=True)
    c = cfg(mono(repo))
    assert where(c, other / "games" / "alpha") is None
    assert where(c, plain) is None
    assert where(c, sibling) is None
    assert where(c, tmp_path) is None  # the folder above a project is not inside it


def test_the_deepest_project_wins_and_order_does_not_matter(repo: Path) -> None:
    inner = Project(name="inner", path=repo / "games" / "alpha")
    for c in (cfg(mono(repo), inner), cfg(inner, mono(repo))):
        assert where(c, repo / "games" / "alpha" / "src") == ("inner", None)
        assert where(c, repo / "games" / "beta") == ("demo", "beta")


def test_a_symlinked_path_and_an_unresolved_config_path_both_match(
    repo: Path, tmp_path: Path
) -> None:
    link = tmp_path / "link"
    link.symlink_to(repo)
    assert where(cfg(mono(repo)), link / "games" / "beta") == ("demo", "beta")
    assert where(cfg(mono(link)), repo / "games" / "beta") == ("demo", "beta")


def test_a_project_with_a_nested_subproject_glob(tmp_path: Path) -> None:
    root = tmp_path / "deep"
    (root / "apps" / "web" / "one" / "src").mkdir(parents=True)
    p = Project(name="deep", path=root, subprojects=Subprojects(glob="apps/web/*"))
    assert where(cfg(p), root / "apps" / "web" / "one" / "src") == ("deep", "one")
    assert where(cfg(p), root / "apps" / "web") == ("deep", None)
    assert where(cfg(p), root / "apps") == ("deep", None)


def test_board_url_quotes_the_project_and_game() -> None:
    assert locate.board_url(8742, "demo") == "http://127.0.0.1:8742/p/demo"
    assert locate.board_url(8742, "my repo", "a&b") == "http://127.0.0.1:8742/p/my%20repo?sub=a%26b"


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        code = {"/p/demo": 200, "/p/broken": 500}.get(self.path.split("?")[0], 404)
        self.send_response(code)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[int]:
    # Not HTTPServer: its bind looks the host's name up in DNS (socket.getfqdn), 35 s here.
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_page_state_tells_up_stale_error_and_down(server: int) -> None:
    assert locate.page_state(locate.board_url(server, "demo", "alpha")) == "up"
    assert locate.page_state(locate.board_url(server, "newer")) == "stale"
    assert locate.page_state(locate.board_url(server, "broken")) == "answered 500"
    assert locate.page_state(locate.board_url(closed_port(), "demo"), timeout=1) == "down"


def write_config(tmp_path: Path, repo: Path) -> Path:
    path = tmp_path / "projects.toml"
    path.write_text(
        f'[[project]]\npath = "{repo}"\nname = "demo"\n\n'
        '[project.subprojects]\nglob = "games/*"\nexclude = ["_harness"]\n',
        encoding="utf-8",
    )
    return path


def test_board_outside_every_project_exits_2(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = write_config(tmp_path, repo)
    code = cli.main(["board", "--config", str(conf), "--cwd", str(tmp_path), "--no-sync"])
    assert code == 2
    assert "in none of the configured projects (demo)" in capsys.readouterr().out


def test_board_prints_the_games_link_and_how_to_start_the_page(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = write_config(tmp_path, repo)
    port = str(closed_port())
    code = cli.main(["board", "--config", str(conf), "--cwd", str(repo / "games" / "alpha"),
                     "--no-sync", "--port", port])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 0
    assert f"board: http://127.0.0.1:{port}/p/demo?sub=alpha" in out
    assert "page: not running; start it with:" in out


def test_board_syncs_then_falls_back_when_the_game_has_no_board(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = write_config(tmp_path, repo)
    synced: list[str] = []

    def fake_sync(c: Config, project: Project, db: Path) -> tuple[object, ApplyReport]:
        synced.append(project.name)
        r = result([])  # its subs are alpha and beta
        r.timings["total"] = 0.1
        return r, ApplyReport()

    monkeypatch.setattr(runner, "run_sync", fake_sync)
    port = str(closed_port())
    (repo / "games" / "gamma").mkdir()  # on disk, but no board yet
    for game, url in (("beta", "/p/demo?sub=beta"), ("gamma", "/p/demo\n")):
        code = cli.main(["board", "--config", str(conf), "--cwd", str(repo / "games" / game),
                         "--port", port, "--db", str(tmp_path / "board.db")])  # fmt: skip
        out = capsys.readouterr().out
        assert code == 0
        assert f"127.0.0.1:{port}{url}" in out
    assert synced == ["demo", "demo"]
    assert "'gamma' has no board of its own yet" in out


def test_board_reports_a_refused_sync_and_still_gives_the_link(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = write_config(tmp_path, repo)

    def busy(c: Config, project: Project, db: Path) -> None:
        raise runner.Busy("another sync is running")

    monkeypatch.setattr(runner, "run_sync", busy)
    code = cli.main(["board", "--config", str(conf), "--cwd", str(repo / "games" / "alpha"),
                     "--port", str(closed_port()), "--db", str(tmp_path / "board.db")])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 1
    assert "sync not stored (another sync is running)" in out
    assert "/p/demo?sub=alpha" in out  # the path's game is kept when the sync could not check it


def test_board_prints_the_link_when_the_sync_crashes(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = write_config(tmp_path, repo)

    def crash(c: Config, project: Project, db: Path, **kw: object) -> None:
        raise GitError("git log HEAD: fatal: bad object")

    monkeypatch.setattr(runner, "run_sync", crash)
    code = cli.main(["board", "--config", str(conf), "--cwd", str(repo / "games" / "alpha"),
                     "--port", str(closed_port()), "--db", str(tmp_path / "board.db")])  # fmt: skip
    out = capsys.readouterr().out
    assert code == 1
    assert "sync not stored (GitError" in out
    assert "/p/demo?sub=alpha" in out


def test_board_says_how_many_need_you_and_how_many_are_new(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = write_config(tmp_path, repo)
    db = tmp_path / "board.db"

    def fake_sync(c: Config, project: Project, path: Path, **kw: object) -> tuple:
        from boardkit import card, now

        r = result([card("ny:1", "needs_you", kind="needs", sub="alpha"),
                    card("ny:2", "needs_you", kind="needs", sub="beta")])  # fmt: skip
        r.timings["total"] = 0.1
        from docboard.store import Store

        with Store(path) as s:
            return r, s.apply(r, now())

    monkeypatch.setattr(runner, "run_sync", fake_sync)
    code = cli.main(["board", "--config", str(conf), "--cwd", str(repo / "games" / "alpha"),
                     "--port", str(closed_port()), "--db", str(db)])  # fmt: skip
    assert code == 0
    assert "alpha: 1 need you, 1 new" in capsys.readouterr().out
