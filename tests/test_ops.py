"""Where the board lives, the lock, Windows paths, backups, the move, and the CLI's edges."""

from __future__ import annotations

import io
import json
import socket
import socketserver
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import factories as f
import pytest
from boardkit import card, now, result

from docboard import cli, ghsrc, locate, move, paths, runner, store
from docboard import transcripts as T
from docboard.build import code_id
from docboard.config import ConfigError, parse
from docboard.store import Store

USERS = "Users"  # joined at run time, so no home path sits in this file's text
WIN_ROOT = Path("C:\\" + USERS + "\\you\\code\\demo")


# --- where the board lives -------------------------------------------------------------------


def test_data_dir_per_platform(tmp_path: Path) -> None:
    home = tmp_path
    assert (
        paths.data_dir({}, "darwin", home) == home / "Library" / "Application Support" / "docboard"
    )
    local = str(tmp_path / "AppData" / "Local")
    assert paths.data_dir({"LOCALAPPDATA": local}, "win32", home) == Path(local) / "docboard"
    assert paths.data_dir({}, "win32", home) == home / "AppData" / "Local" / "docboard"
    xdg = str(tmp_path / "xdg")
    assert paths.data_dir({"XDG_DATA_HOME": xdg}, "linux", home) == Path(xdg) / "docboard"
    assert paths.data_dir({"XDG_DATA_HOME": "relative"}, "linux", home) == (
        home / ".local" / "share" / "docboard"
    )


def test_docboard_home_overrides_every_default(tmp_path: Path) -> None:
    env = {"DOCBOARD_HOME": str(tmp_path / "dev"), "LOCALAPPDATA": "x", "XDG_DATA_HOME": "/y"}
    for platform in ("darwin", "win32", "linux"):
        assert paths.data_dir(env, platform, tmp_path) == tmp_path / "dev"


def test_flag_beats_env_beats_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = paths.data_dir()
    assert paths.config_path() == (home / "projects.toml", "the data folder")
    monkeypatch.setenv("DOCBOARD_CONFIG", str(tmp_path / "env.toml"))
    assert paths.config_path() == (tmp_path / "env.toml", "$DOCBOARD_CONFIG")
    assert paths.config_path(str(tmp_path / "flag.toml")) == (tmp_path / "flag.toml", "--config")
    assert paths.db_path() == home / "board.db" and paths.terms_path().parent == home


def test_no_default_lives_inside_the_package() -> None:
    package = Path(paths.__file__).resolve().parent
    for p in (paths.data_dir(), paths.db_path(), paths.config_path()[0], paths.terms_path()):
        assert not p.resolve().is_relative_to(package.parent.parent), p


def test_missing_config_exits_2_naming_the_path_and_creates_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = paths.data_dir()
    assert cli.main(["sync"]) == 2
    err = capsys.readouterr().err
    assert str(home / "projects.toml") in err and "projects.example.toml" in err
    assert not home.exists() or not any(home.iterdir())


def test_old_checkout_board_is_refused_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    old = tmp_path / "checkout"
    (old / "data").mkdir(parents=True)
    Store(old / "data" / "board.db").close()
    monkeypatch.setattr(paths, "LEGACY_ROOT", old)
    conf = tmp_path / "p.toml"
    conf.write_text(f'[[project]]\npath = "{tmp_path}"\n', encoding="utf-8")
    assert cli.main(["sync", "--config", str(conf)]) == 2
    assert "move-data" in capsys.readouterr().err
    assert not paths.db_path().exists()


def test_serve_refuses_a_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conf = tmp_path / "p.toml"
    conf.write_text(f'[[project]]\npath = "{tmp_path}"\n', encoding="utf-8")
    assert cli.main(["serve", "--config", str(conf)]) == 2
    assert "no board at" in capsys.readouterr().err


def test_claude_home_follows_claude_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert (
        parse({"project": [{"path": str(tmp_path)}]}, tmp_path).claude_home == tmp_path / "claude"
    )


def test_config_errors_name_the_field(tmp_path: Path) -> None:
    cases = [
        ({}, "no [[project]]"),
        ({"project": [{}]}, "project[0]: needs a path"),
        ({"project": [{"path": "x", "github": "nope"}]}, "github"),
        ({"project": [{"path": "x", "subprojects": {"glob": "games"}}]}, "glob"),
        ({"project": [{"path": "a", "name": "x"}, {"path": "b", "name": "x"}]}, "repeat"),
    ]
    for data, words in cases:
        with pytest.raises(ConfigError, match=words.replace("[", r"\[").replace("]", r"\]")):
            parse(data, tmp_path)


def test_a_windows_path_in_the_config_gets_a_hint(tmp_path: Path) -> None:
    conf = tmp_path / "p.toml"
    conf.write_text('[[project]]\npath = "C:\\' + USERS + '\\you\\code"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="single quotes"):
        from docboard import config

        config.load(conf)


# --- the lock --------------------------------------------------------------------------------


def test_a_second_holder_is_busy(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    with (
        runner.sync_lock(lock, wait=0),
        pytest.raises(runner.Busy),
        runner.sync_lock(lock, wait=0),
    ):
        pass


def test_the_lock_is_free_after_release_and_after_an_error(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    with runner.sync_lock(lock, wait=0):
        pass
    with pytest.raises(KeyError), runner.sync_lock(lock, wait=0):
        raise KeyError("boom")
    with runner.sync_lock(lock, wait=0):
        pass


def test_the_lock_holds_across_processes(tmp_path: Path) -> None:
    lock = tmp_path / "sync.lock"
    code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from docboard import runner\n"
        "with runner.sync_lock(Path(sys.argv[1]), wait=0):\n"
        "    print('held', flush=True)\n"
        "    sys.stdin.readline()\n"
    )
    child = subprocess.Popen(  # noqa: S603 - this interpreter, fixed code
        [sys.executable, "-c", code, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(runner.Busy), runner.sync_lock(lock, wait=0.5):
            pass
    finally:
        child.communicate("\n", timeout=20)
    with runner.sync_lock(lock, wait=5):
        pass  # free once the holder is gone


def test_every_command_imports_without_fcntl(tmp_path: Path) -> None:
    """As on Windows: no fcntl, an msvcrt instead. Importing the CLI must not need fcntl."""
    code = (
        "import sys, types\n"
        "import json, sqlite3, subprocess, threading, urllib.request  # the stdlib, as loaded\n"
        "sys.modules['fcntl'] = None\n"
        "stub = types.ModuleType('msvcrt'); stub.LK_NBLCK = 2; stub.LK_UNLCK = 0\n"
        "stub.locking = lambda fd, mode, n: None\n"
        "sys.modules['msvcrt'] = stub\n"
        "sys.platform = 'win32'\n"
        "from docboard import cli, runner\n"
        "with runner.sync_lock(__import__('pathlib').Path(sys.argv[1]), wait=0):\n"
        "    pass\n"
        "print('ok')\n"
    )
    done = subprocess.run(  # noqa: S603 - this interpreter, fixed code
        [sys.executable, "-c", code, str(tmp_path / "sync.lock")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0 and done.stdout.strip() == "ok", done.stderr


# --- paths on Windows ------------------------------------------------------------------------


def test_windows_folder_name() -> None:
    assert T.encode_path(WIN_ROOT) == "C--" + USERS + "-you-code-demo"


def test_a_json_escaped_windows_cwd_belongs(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    wt = str(WIN_ROOT) + "\\.claude\\worktrees\\w1"
    rec = dict(f.human("s1", "h1", "2026-10-20T10:00:00Z", "hi"), cwd=wt.lower())
    f.write(projects / (T.encode_path(WIN_ROOT) + "--claude-worktrees-w1") / "s1.jsonl", [rec])
    scan = T.scan_all([("demo", WIN_ROOT)], projects, windows=True)
    assert (scan.files_by["demo"], scan.foreign) == (1, 0)
    plain = T.scan_all([("demo", WIN_ROOT)], projects, windows=False)
    assert plain.files_by["demo"] == 0  # the same bytes do not match without the Windows rules


def test_windows_touches_any_slash_or_drive_case_are_relative(tmp_path: Path) -> None:
    root = str(WIN_ROOT)
    paths_seen = [
        root.replace("\\", "/").lower() + "/games/alpha/x.luau",
        "c" + root[1:] + "\\games\\beta\\y.luau",
        root.replace("demo", "DEMO") + "\\docs\\z.md",
    ]
    recs = [
        f.tool_use("s1", f"u{i}", f"2026-10-20T10:0{i}:00Z", "Edit", f"t{i}", {"file_path": p})
        for i, p in enumerate(paths_seen)
    ]
    projects = tmp_path / "projects"
    f.write(projects / T.encode_path(WIN_ROOT) / "s1.jsonl", recs)
    scan = T.scan_all([("demo", WIN_ROOT)], projects, windows=True)
    assert sorted(t.rel for t in scan.touches.values()) == [
        "docs/z.md", "games/alpha/x.luau", "games/beta/y.luau",
    ]  # fmt: skip


def test_a_sibling_folder_is_outside_on_windows() -> None:
    roots = T.Roots({"demo": [str(WIN_ROOT)]}, windows=True)
    assert roots.owner(str(WIN_ROOT) + "-x\\f.py") is None
    assert roots.inside(str(WIN_ROOT).upper(), "demo")


def test_a_notebook_touch_passes_the_prefilter(tmp_path: Path) -> None:
    rec = f.tool_use("s1", "u1", "2026-10-20T10:00:00Z", "NotebookEdit", "t1",
                     {"notebook_path": str(f.ROOT / "notes" / "a.ipynb")})  # fmt: skip
    projects = tmp_path / "projects"
    f.write(projects / T.encode_path(f.ROOT) / "s1.jsonl", [rec])
    scan = T.scan_project(f.ROOT, projects)
    assert [t.rel for t in scan.touches.values()] == ["notes/a.ipynb"]


def test_a_line_over_max_line_is_skipped_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects = tmp_path / "projects"
    big = f.assistant_text("s1", "a1", "2026-10-20T10:00:00Z", f.needs_block("Do — " + "x" * 400))
    small = f.assistant_text("s1", "a2", "2026-10-20T11:00:00Z", f.needs_block("Do — y"))
    f.write(projects / T.encode_path(f.ROOT) / "s1.jsonl", [big, small])
    monkeypatch.setattr(T, "MAX_LINE", 300)
    scan = T.scan_project(f.ROOT, projects)
    assert scan.skipped_long == 1 and [i.title for i in scan.needs.values()] == ["y"]


# --- backups, timings, the running code ------------------------------------------------------


def v1_db(path: Path) -> None:
    db = sqlite3.connect(path)
    for stmt in store._statements(store.MIGRATIONS[0]):
        db.execute(stmt)
    db.execute("PRAGMA user_version = 1")
    db.execute("INSERT INTO override VALUES ('ny:1', 'done', 'needs_you', 'x')")
    db.commit()
    db.close()


def test_backup_before_migration_keeps_the_owners_moves(tmp_path: Path) -> None:
    path = tmp_path / "board.db"
    v1_db(path)
    Store(path).close()
    (copy,) = (tmp_path / "backups").glob("board-*-before-v*.db")
    db = sqlite3.connect(copy)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.execute("SELECT card_id FROM override").fetchall() == [("ny:1",)]
    db.close()
    Store(path).close()  # already migrated: no second copy
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_daily_backup_once_per_day(tmp_path: Path) -> None:
    with Store(tmp_path / "board.db") as s:
        t = datetime.now(UTC)
        assert s.backup_if_due(t) is not None
        assert s.backup_if_due(t + timedelta(hours=1)) is None
    assert len(list((tmp_path / "backups").glob("board-*-daily.db"))) == 1


def test_prune_keeps_migration_copies(tmp_path: Path) -> None:
    folder = tmp_path / "backups"
    folder.mkdir()
    for i in range(10):
        (folder / f"board-2026010{i}T000000Z-daily.db").write_bytes(b"x")
    for i in range(3):
        (folder / f"board-2025010{i}T000000Z-before-v2.db").write_bytes(b"x")
    store.prune(folder)
    assert len(list(folder.glob("*-daily.db"))) == store.KEEP_DAILY
    assert len(list(folder.glob("*-before-v*.db"))) == 3
    assert (folder / "board-20260109T000000Z-daily.db").exists()  # the newest are kept


def test_a_sync_run_records_its_timings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = result([card("bl:1", "ideas")])
    r.timings = {"docs": 0.1, "total": 0.2}
    monkeypatch.setattr(runner, "collect", lambda cfg, project, now: r)
    from docboard.config import Config, Project

    p = Project(name="demo", path=tmp_path)
    runner.run_sync(Config(projects=(p,), claude_home=tmp_path), p, tmp_path / "board.db")
    with Store(tmp_path / "board.db") as s:
        assert json.loads(s.last_run("demo")["timings"]) == {"docs": 0.1, "total": 0.2}
    assert list((tmp_path / "backups").glob("board-*-daily.db"))


def test_cli_output_survives_a_cp1252_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    a = tmp_path / "a.json"
    a.write_text(json.dumps({"cards": [{"id": "x", "kind": "idea", "title": "Swing → pole",
                                        "sub": "", "column": "ideas", "source": {}}]}))  # fmt: skip
    b = tmp_path / "b.json"
    b.write_text(json.dumps({"cards": []}))
    assert cli.main(["diff", str(a), str(b)]) == 0
    sys.stdout.flush()
    assert "Swing → pole" in raw.getvalue().decode("utf-8")


def test_diff_strict_exits_1_only_when_an_id_comes_or_goes(tmp_path: Path) -> None:
    one = {"id": "x", "kind": "idea", "title": "t", "sub": "", "column": "ideas", "source": {}}
    a, b, c = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "c.json"
    a.write_text(json.dumps({"cards": [one]}))
    b.write_text(json.dumps({"cards": [dict(one, column="next")]}))
    c.write_text(json.dumps({"cards": []}))
    assert cli.main(["diff", str(a), str(b), "--strict"]) == 0  # moved, same ids
    assert cli.main(["diff", str(a), str(c), "--strict"]) == 1
    assert cli.main(["diff", str(a), str(c)]) == 0


def test_ghsrc_parse_maps_states_and_fetch_refuses_at_the_limit(tmp_path: Path) -> None:
    rows = [{"number": n, "title": "t", "state": s, "createdAt": "x", "headRefName": "b"}
            for n, s in ((1, "OPEN"), (2, "MERGED"), (3, "CLOSED"))]  # fmt: skip
    assert [p.state for p in ghsrc.parse("acme/demo", json.dumps(rows))] == [
        "open", "merged", "closed",
    ]  # fmt: skip
    gh = f.fake_exe(tmp_path, "gh", f"print({json.dumps(rows)!r})")
    with pytest.raises(ghsrc.GhError, match="the limit"):
        ghsrc.fetch("acme/demo", str(gh), limit=3)
    assert len(ghsrc.fetch("acme/demo", str(gh), limit=4)) == 3
    with pytest.raises(ghsrc.GhError, match="owner/name"):
        ghsrc.fetch("not a slug", str(gh))


class _Page(BaseHTTPRequestHandler):
    code = "0.0.0+old"

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        self.send_response(200)
        self.send_header("X-Docboard-Code", self.code)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def page() -> Iterator[int]:
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def test_every_response_names_the_running_code(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from docboard.config import Config, Project
    from docboard.web import create_app

    cfg = Config(projects=(Project(name="demo", path=tmp_path),), claude_home=tmp_path)
    with Store(tmp_path / "board.db") as s:
        s.apply(result([]), now())
    client = TestClient(create_app(cfg, tmp_path / "board.db", 8742),
                        base_url="http://127.0.0.1:8742")  # fmt: skip
    assert client.get("/p/demo").headers["x-docboard-code"] == code_id()


def test_board_says_restart_when_the_page_runs_other_code(
    tmp_path: Path, page: int, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "demo"
    repo.mkdir()
    conf = tmp_path / "p.toml"
    conf.write_text(f'[[project]]\npath = "{repo}"\nname = "demo"\n', encoding="utf-8")
    code = cli.main(["board", "--config", str(conf), "--cwd", str(repo), "--no-sync",
                     "--port", str(page)])  # fmt: skip
    assert code == 0
    assert "running older code (0.0.0+old" in capsys.readouterr().out
    assert locate.page_check(locate.board_url(page, "demo")) == ("up", "0.0.0+old")


# --- the move --------------------------------------------------------------------------------


def checkout_with_a_board(tmp_path: Path) -> Path:
    old = tmp_path / "checkout"
    (old / "data").mkdir(parents=True)
    (old / "config").mkdir()
    with Store(old / "data" / "board.db") as s:
        s.apply(result([card("ny:1", "needs_you", kind="needs")]), now())
        s.resolve("ny:1", now())
    (old / "config" / "projects.toml").write_text('[[project]]\npath = "/srv/x"\n')
    (old / "config" / "private-identifiers.txt").write_text("secret\n")
    return old


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_move_data_moves_the_board_and_checks_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = checkout_with_a_board(tmp_path)
    monkeypatch.setattr(paths, "LEGACY_ROOT", old)
    before = move.snapshot(old / "data" / "board.db")
    lines: list[str] = []
    assert move.move_data(closed_port(), out=lines.append) == 0
    home = paths.data_dir()
    assert move.snapshot(home / "board.db") == before
    with Store(home / "board.db") as s:
        assert s.card("ny:1")["col"] == "done"  # the owner's move came along
    assert (home / "projects.toml").read_text().startswith("[[project]]")
    assert not (old / "data" / "board.db").exists()
    assert list((old / "data").glob("moved-*/board.db"))
    assert move.move_data(closed_port(), out=lines.append) == 0  # once moved, nothing more
    assert "already" in lines[-1]


def test_move_data_refuses_while_the_page_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, page: int
) -> None:
    old = checkout_with_a_board(tmp_path)
    monkeypatch.setattr(paths, "LEGACY_ROOT", old)
    assert move.move_data(page, out=lambda *_: None) == 2
    assert (old / "data" / "board.db").exists() and not paths.db_path().exists()
