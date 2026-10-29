from __future__ import annotations

import re
from pathlib import Path

import pytest
from boardkit import COUNTS, card, now, result
from fastapi.testclient import TestClient

from docboard import cli
from docboard.config import Config, Project
from docboard.store import FloorError, Store
from docboard.web import create_app, linkify

PORT = 8742
BASE = f"http://127.0.0.1:{PORT}"
SAME = {"Origin": BASE}


@pytest.fixture
def env(tmp_path: Path) -> tuple[TestClient, Path]:
    repo = tmp_path / "repo"
    (repo / "docs").mkdir(parents=True)
    cfg = Config(projects=(Project(name="demo", path=repo),), claude_home=tmp_path / "claude")
    db = tmp_path / "data" / "board.db"
    with Store(db) as s:
        s.apply(
            result(
                [
                    card(
                        "ny:1",
                        "needs_you",
                        kind="needs",
                        title="<script>alert(1)</script> set the token",
                        body="see javascript:alert(1) and https://example.com/x.",
                    ),
                    card("ny:old", "needs_you", kind="needs", title="An old ask", older=True),
                    card("ny:b", "needs_you", kind="needs", title="A beta ask", sub="beta"),
                    card(
                        "bl:1",
                        "ideas",
                        title="A pole swing",
                        source={"path": "docs/DECISIONS.md", "line": 5, "ref_sha": "abc123"},
                    ),
                    card(
                        "bl:2",
                        "ideas",
                        title="Outside",
                        source={"path": "../../secrets.txt", "line": 1},
                    ),
                ]
            ),
            now(),
        )
    client = TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False)
    return client, db


def test_only_the_loopback_host_is_answered(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    assert client.get("/p/demo").status_code == 200
    assert client.get("/p/demo", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/p/demo", headers={"Host": f"127.0.0.1:{PORT + 1}"}).status_code == 400


def test_a_write_must_come_from_this_board(env: tuple[TestClient, Path]) -> None:
    client, db = env
    form = {"col": "done"}
    assert client.post("/i/bl:1/move", data=form).status_code == 403
    assert (
        client.post(
            "/i/bl:1/move", data=form, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    assert client.post("/i/bl:1/move", data=form, headers={"Origin": "null"}).status_code == 403
    assert (
        client.post("/i/bl:1/move", data=form, headers={"Sec-Fetch-Site": "cross-site"}).status_code
        == 403
    )
    with Store(db) as s:
        assert s.card("bl:1")["col"] == "ideas"
    assert (
        client.post(
            "/i/bl:1/move", data=form, headers={"Sec-Fetch-Site": "same-origin"}
        ).status_code
        == 303
    )
    with Store(db) as s:
        assert s.card("bl:1")["col"] == "done"


def test_a_form_post_the_browser_sends_with_origin_null_is_this_boards(
    env: tuple[TestClient, Path],
) -> None:
    # Chrome sent "Origin: null" for the page's own form post (2026-10-27, the first real run);
    # its unforgeable Sec-Fetch-Site still says where the request came from.
    client, db = env
    null_same = {"Origin": "null", "Sec-Fetch-Site": "same-origin"}
    null_cross = {"Origin": "null", "Sec-Fetch-Site": "cross-site"}
    assert client.post("/i/bl:1/move", data={"col": "next"}, headers=null_cross).status_code == 403
    assert client.post("/i/bl:1/move", data={"col": "next"}, headers=null_same).status_code == 303
    with Store(db) as s:
        assert s.card("bl:1")["col"] == "next"
    foreign = {"Origin": "https://evil.example", "Sec-Fetch-Site": "same-origin"}
    assert client.post("/i/bl:1/move", data={"col": "done"}, headers=foreign).status_code == 403


def test_a_get_never_changes_anything(env: tuple[TestClient, Path]) -> None:
    client, db = env
    assert client.get("/i/bl:1/move?col=done").status_code == 404
    with Store(db) as s:
        assert s.card("bl:1")["col"] == "ideas"


def test_source_text_is_escaped_everywhere(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    for path in ("/p/demo", "/p/demo/triage", "/i/ny:1"):
        page = client.get(path).text
        assert "<script>alert(1)</script>" not in page, path
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page, path


def test_only_http_links_are_made() -> None:
    html = str(linkify('see javascript:alert(1) and https://example.com/x. <b>"q"</b>'))
    assert 'href="https://example.com/x"' in html
    assert "javascript:alert(1)" in html and 'href="javascript' not in html
    assert "&lt;b&gt;" in html


def test_editor_links_only_inside_the_project(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    inside = client.get("/i/bl:1").text
    assert "vscode://file" in inside
    assert 'href="https://github.com/acme/demo/blob/abc123/docs/DECISIONS.md#L5"' in inside
    assert "vscode://file" not in client.get("/i/bl:2").text


def test_every_page_carries_the_security_headers(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    r = client.get("/p/demo")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"


def test_the_board_folds_old_items(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    page = client.get("/p/demo").text
    assert "An old ask" not in page and "Older (1)" in page
    assert "An old ask" in client.get("/p/demo?older=1").text


def batch_of(page: str) -> str:
    m = re.search(r'name="batch" value="([0-9a-f]+)"', page)
    assert m, "no Undo this batch form on the page"
    return m.group(1)


def test_resolve_older_asks_first_and_can_be_undone(env: tuple[TestClient, Path]) -> None:
    client, db = env
    r = client.post("/p/demo/triage", data={"action": "resolve_older"}, headers=SAME)
    assert r.status_code == 303 and "confirm=older" in r.headers["location"]
    with Store(db) as s:
        assert s.card("ny:old")["col"] == "needs_you"  # asking changes nothing
    page = client.get("/p/demo/triage?confirm=older").text
    assert "An old ask" in page and 'value="ny:old"' in page and 'value="ny:1"' not in page
    client.post("/p/demo/triage", data={"action": "resolve", "ids": ["ny:old"]}, headers=SAME)
    with Store(db) as s:
        assert (s.card("ny:old")["col"], s.card("ny:1")["col"]) == ("done", "needs_you")
    page = client.get("/p/demo/triage").text
    assert "Undo this batch" in page
    client.post("/p/demo/triage/undo", data={"batch": batch_of(page)}, headers=SAME)
    with Store(db) as s:
        assert s.card("ny:old")["col"] == "needs_you"
    assert "Undo this batch" not in client.get("/p/demo/triage").text


def test_triage_filters_by_game(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    page = client.get("/p/demo/triage?sub=beta").text
    assert "A beta ask" in page and "An old ask" not in page
    assert "A beta ask" not in client.get("/p/demo/triage?sub=alpha").text


def test_select_all_checks_every_box_without_script(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    boxes = re.findall(
        r'<input type="checkbox" name="ids"[^>]*>', client.get("/p/demo/triage?select=all").text
    )
    assert len(boxes) == 3 and all("checked" in b for b in boxes)
    plain = re.findall(
        r'<input type="checkbox" name="ids"[^>]*>', client.get("/p/demo/triage").text
    )
    assert not any("checked" in b for b in plain)


def test_undo_refuses_a_cross_site_post(env: tuple[TestClient, Path]) -> None:
    client, db = env
    client.post("/p/demo/triage", data={"action": "resolve", "ids": ["ny:1"]}, headers=SAME)
    batch = batch_of(client.get("/p/demo/triage").text)
    evil = {"Origin": "https://evil.example"}
    assert (
        client.post("/p/demo/triage/undo", data={"batch": batch}, headers=evil).status_code == 403
    )
    with Store(db) as s:
        assert s.card("ny:1")["col"] == "done"


def test_the_refused_banner_names_the_drop_and_the_command(env: tuple[TestClient, Path]) -> None:
    client, db = env
    with Store(db) as s, pytest.raises(FloorError):
        s.apply(result([], {**COUNTS, "backlog": 3}), now())
    page = client.get("/p/demo").text
    assert "backlog: 10 → 3" in page
    assert "uv run docboard sync --accept-drop --project demo" in page


def test_a_failing_background_sync_shows_and_is_not_restarted(tmp_path: Path) -> None:
    plain = tmp_path / "plain"  # not a git repo: every sync of it fails
    plain.mkdir()
    cfg = Config(projects=(Project(name="demo", path=plain),), claude_home=tmp_path / "claude")
    db = tmp_path / "data" / "board.db"
    with Store(db) as s:
        s.apply(result([card("bl:1", "ideas")]), now(-1))  # a day old: the page refreshes it
    app = create_app(cfg, db, PORT)
    client = TestClient(app, base_url=BASE, follow_redirects=False)
    client.get("/p/demo", headers={"Sec-Fetch-Site": "none"})
    assert app.state.board.join(timeout=20)
    page = client.get("/p/demo").text
    assert "The last sync failed" in page and "GitError" in page
    assert app.state.board.join(timeout=20)
    with Store(db) as s:
        statuses = [r[0] for r in s.db.execute("SELECT status FROM sync_run ORDER BY id")]
    assert statuses == ["ok", "failed"]  # the failed run reset the two-minute wait


def test_triage_resolves_and_dismisses_the_selected(env: tuple[TestClient, Path]) -> None:
    client, db = env
    client.post("/p/demo/triage", data={"action": "dismiss", "ids": ["ny:1"]}, headers=SAME)
    with Store(db) as s:
        assert s.card("ny:1")["col"] == "hidden"


def test_a_redirect_never_leaves_the_board(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    for back in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
        r = client.post("/i/bl:1/move", data={"col": "next", "back": back}, headers=SAME)
        assert r.headers["location"] == "/i/bl%3A1", back
    r = client.post("/i/bl:1/move", data={"col": "next", "back": "/p/demo?sub=alpha"}, headers=SAME)
    assert r.headers["location"] == "/p/demo?sub=alpha"


def test_manual_cards_through_the_page(env: tuple[TestClient, Path]) -> None:
    client, db = env
    r = client.post(
        "/p/demo/cards",
        data={"title": "Call the venue", "col": "next", "sub": "alpha"},
        headers=SAME,
    )
    assert r.status_code == 303
    assert "Call the venue" in client.get("/p/demo").text
    with Store(db) as s:
        (m,) = s.manual("demo")
    client.post(f"/i/{m['id']}/move", data={"col": "done"}, headers=SAME)
    client.post(f"/i/{m['id']}/delete", data={}, headers=SAME)
    assert "Call the venue" not in client.get("/p/demo").text
    assert client.post("/i/bl:1/delete", data={}, headers=SAME).status_code == 400


def test_a_card_can_be_given_another_game(env: tuple[TestClient, Path]) -> None:
    client, db = env
    client.post("/i/bl:1/sub", data={"sub": "beta"}, headers=SAME)
    with Store(db) as s:
        assert s.card("bl:1")["sub"] == "beta"
    assert client.post("/i/bl:1/sub", data={"sub": "nope"}, headers=SAME).status_code == 400


def test_serve_refuses_a_non_loopback_address() -> None:
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2  # noqa: S104 - the refused case


def test_an_undo_that_put_nothing_back_says_so(env: tuple[TestClient, Path]) -> None:
    client, db = env
    client.post("/p/demo/triage", data={"action": "resolve", "ids": ["ny:1"]}, headers=SAME)
    batch = batch_of(client.get("/p/demo/triage").text)
    with Store(db) as s:
        s.move("ny:1", "next", now())  # moved again since: undo leaves it
    r = client.post("/p/demo/triage/undo", data={"batch": batch}, headers=SAME)
    page = client.get(r.headers["location"]).text
    assert "Nothing was put back" in page


def test_the_undo_notice_shows_only_numbers(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    r = client.get("/p/demo/triage?undone=all%20your%20cards%20were%20deleted")
    assert "all your cards were deleted" not in r.text


def test_triage_keeps_its_game_across_a_post_and_reads_root(env: tuple[TestClient, Path]) -> None:
    client, db = env
    r = client.post(
        "/p/demo/triage", data={"action": "dismiss", "ids": ["ny:b"], "sub": "beta"}, headers=SAME
    )
    assert r.headers["location"] == "/p/demo/triage?sub=beta"
    with Store(db) as s:
        s.apply(
            result([card("ny:r", "needs_you", kind="needs", title="A root ask", sub="")]), now()
        )
    page = client.get("/p/demo/triage?sub=_root").text
    assert "A root ask" in page and "An old ask" not in page


def test_triage_moves_only_this_boards_cards(tmp_path: Path) -> None:
    cfg = Config(
        projects=(Project(name="one", path=tmp_path), Project(name="two", path=tmp_path)),
        claude_home=tmp_path / "c",
    )
    db = tmp_path / "data" / "board.db"
    with Store(db) as s:
        s.apply(result([card("ny:1", "needs_you", kind="needs")], project="one"), now())
        s.apply(result([card("ny:2", "needs_you", kind="needs")], project="two"), now())
    client = TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False)
    client.post("/p/one/triage", data={"action": "resolve", "ids": ["ny:1", "ny:2"]}, headers=SAME)
    with Store(db) as s:
        assert (s.card("ny:1")["col"], s.card("ny:2")["col"]) == ("done", "needs_you")
    assert client.post("/p/one/triage", data={"action": "burn"}, headers=SAME).status_code == 400


def test_badges_for_a_reopened_ask_and_a_gone_transcript(tmp_path: Path) -> None:
    cfg = Config(projects=(Project(name="demo", path=tmp_path),), claude_home=tmp_path / "c")
    db = tmp_path / "data" / "board.db"
    with Store(db) as s:
        a = card("ny:1", "needs_you", kind="needs", title="Reopened", event_at=now(-3))
        b = card("ny:2", "needs_you", kind="needs", title="Orphan", source={"session": "s9"})
        r = result([a, b])
        r.sessions = {"s9"}
        s.apply(r, now())
        s.dismiss("ny:1", now(-2))
        again = card(
            "ny:1",
            "needs_you",
            kind="needs",
            title="Reopened",
            event_at=now(-3),
            raised=2,
            raised_at=now(-1),
        )
        s.apply(result([again]), now())  # and ny:2's transcript is gone
    client = TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False)
    page = client.get("/p/demo").text
    assert "raised again after you dismissed it" in page and "transcript gone" in page
    triage = client.get("/p/demo/triage").text
    assert "raised again after you dismissed it" in triage


def test_confirm_with_nothing_older_shows_the_list(env: tuple[TestClient, Path]) -> None:
    client, db = env
    client.post("/p/demo/triage", data={"action": "resolve", "ids": ["ny:old"]}, headers=SAME)
    page = client.get("/p/demo/triage?confirm=older").text
    assert "Resolve these 0" not in page and 'value="ny:1"' in page


def test_a_plan_file_outside_the_plans_folder_is_not_shown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    claude = tmp_path / "claude"
    cfg = Config(projects=(Project(name="demo", path=repo),), claude_home=claude)
    db = tmp_path / "data" / "board.db"
    inside, outside = str(claude / "plans" / "a-plan.md"), "/etc/hosts"
    plans = [
        card("pl:1", "next", kind="plan", source={"plan_file": inside}),
        card("pl:2", "next", kind="plan", source={"plan_file": outside}),
    ]
    with Store(db) as s:
        s.apply(result(plans), now())
    client = TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False)
    assert "a-plan.md" in client.get("/i/pl:1").text
    assert "/etc/hosts" not in client.get("/i/pl:2").text


def test_a_plan_file_must_sit_inside_the_plans_folder() -> None:
    from docboard.web import _inside

    plans = Path("/" + "srv/claude/plans")
    assert _inside(str(plans / "a.md"), plans)
    for outside in (
        plans / ".." / "secret.md",
        plans.parent / "x.md",
        Path(str(plans) + "-evil") / "x.md",
    ):
        assert not _inside(str(outside), plans), outside
    assert not _inside("bad\0path", plans)  # a path the OS refuses reads as outside


# --- PR D: the page shows what it hides -----------------------------------------------------


def test_hidden_cards_are_listed_on_request(env: tuple[TestClient, Path]) -> None:
    client, db = env
    with Store(db) as s:
        s.dismiss("bl:1", now())
    assert "A pole swing" not in client.get("/p/demo?ideas=1").text
    page = client.get("/p/demo?hidden=1").text
    assert 'aria-label="Hidden"' in page and "A pole swing" in page


def test_gone_cards_are_listed_with_their_links(env: tuple[TestClient, Path]) -> None:
    client, db = env
    with Store(db) as s:
        rows = (
            ("ny:1", "needs_you", "needs", "x"),
            ("ny:old", "needs_you", "needs", "An old ask"),
            ("ny:b", "needs_you", "needs", "A beta ask"),
            ("bl:1", "ideas", "idea", "A pole swing"),
        )
        keep = [card(i, c, kind=k, title=t) for i, c, k, t in rows]
        s.apply(result(keep), now())
    page = client.get("/p/demo").text
    assert "1 gone from their sources" in page and 'href="/i/bl%3A2"' in page


def test_an_old_card_link_follows_its_rename(tmp_path: Path) -> None:
    cfg = Config(projects=(Project(name="demo", path=tmp_path),), claude_home=tmp_path / "c")
    db = tmp_path / "data" / "board.db"
    with Store(db) as s:
        s.apply(result([card("bl:a", "ideas", title="A pole swing for the city")]), now())
        s.apply(result([card("bl:b", "ideas", title="A pole swing for the whole city")]), now())
    client = TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False)
    r = client.get("/i/bl:a")
    assert r.status_code == 303 and r.headers["location"] == "/i/bl%3Ab"
    assert client.get("/i/bl:nothing").status_code == 404


def test_ideas_fold_in_the_all_view(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    page = client.get("/p/demo").text
    assert "A pole swing" not in page and "2 ideas" in page
    assert "A pole swing" in client.get("/p/demo?ideas=1").text
    assert "A pole swing" in client.get("/p/demo?sub=alpha").text


def test_titles_render_without_markdown(tmp_path: Path) -> None:
    cfg = Config(projects=(Project(name="demo", path=tmp_path),), claude_home=tmp_path / "c")
    db = tmp_path / "data" / "board.db"
    title = "**Bold** move and `code` and [a link](https://x.example/y)"
    with Store(db) as s:
        s.apply(result([card("ny:1", "needs_you", kind="needs", title=title)]), now())
    client = TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False)
    page = client.get("/p/demo").text
    assert "Bold move and code and a link" in page and "**Bold**" not in page


def test_games_show_their_names(env: tuple[TestClient, Path]) -> None:
    client, db = env
    r = result([card("ny:1", "needs_you", kind="needs", title="x")])
    r.sub_names = {"alpha": "Alpha Game"}
    with Store(db) as s:
        s.apply(r, now())
    page = client.get("/p/demo").text
    assert ">Alpha Game" in page and ">alpha<" not in page


def test_new_cards_are_marked_until_you_mark_them_seen(env: tuple[TestClient, Path]) -> None:
    client, db = env
    assert 'class="badge new"' in client.get("/p/demo").text  # synced just now, never seen
    assert client.post("/p/demo/seen", data={}, headers=SAME).status_code == 303
    assert 'class="badge new"' not in client.get("/p/demo").text
    with Store(db) as s:
        fresh = card("ny:2", "needs_you", kind="needs", title="Fresh ask")
        s.apply(result([card("ny:1", "needs_you", kind="needs"), fresh]), now(1))
    page = client.get("/p/demo").text
    assert page.count('class="badge new"') == 1 and "1 new" in page


def test_a_page_refreshes_itself_while_its_sync_runs(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    assert 'http-equiv="refresh"' not in client.get("/p/demo").text
    client.app.state.board._syncing.add("demo")
    assert 'http-equiv="refresh"' in client.get("/p/demo").text


def test_an_unknown_game_says_so(env: tuple[TestClient, Path]) -> None:
    client, _ = env
    assert "No game named" in client.get("/p/demo?sub=nope").text
    assert "No game named" not in client.get("/p/demo?sub=_root").text


def test_only_the_boards_own_page_loads_start_a_sync(tmp_path: Path) -> None:
    cfg = Config(projects=(Project(name="demo", path=tmp_path),), claude_home=tmp_path / "c")
    db = tmp_path / "data" / "board.db"
    with Store(db) as s:
        s.apply(result([card("bl:1", "ideas")]), now(-1))  # stale
    app = create_app(cfg, db, PORT)
    client = TestClient(app, base_url=BASE, follow_redirects=False)
    for site in ("cross-site", "same-site", None):
        headers = {"Sec-Fetch-Site": site} if site else {}
        client.get("/p/demo", headers=headers)  # an <img> on another page, or a script
        assert not app.state.board._threads, site
    client.get("/p/demo", headers={"Sec-Fetch-Site": "none"})  # typed in, or a bookmark
    assert app.state.board._threads
    app.state.board.join(timeout=20)


def test_a_sessions_page_shows_its_cards_on_every_board_and_its_answers(tmp_path: Path) -> None:
    cfg = Config(
        projects=(Project(name="one", path=tmp_path), Project(name="two", path=tmp_path)),
        claude_home=tmp_path / "c",
    )
    db = tmp_path / "data" / "board.db"
    with Store(db) as s:
        r = result(
            [card("ny:1", "needs_you", kind="needs", title="Ask on one", source={"session": "s1"})],
            project="one",
        )
        r.session_notes = {
            "s1": {
                "title": "The big run",
                "decided": ["Which? → B"],
                "first_at": now(-1),
                "last_at": now(),
            }
        }
        s.apply(r, now())
        s.apply(
            result(
                [
                    card(
                        "ny:2",
                        "needs_you",
                        kind="needs",
                        title="Ask on two",
                        source={"session": "s1"},
                    )
                ],
                project="two",
            ),
            now(),
        )
    client = TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False)
    page = client.get("/s/s1").text
    assert "The big run" in page and "Ask on one" in page and "Ask on two" in page
    assert "Which? → B" in page
    assert 'href="/s/s1"' in client.get("/i/ny:1").text
    assert client.get("/s/nope").status_code == 404


# --- PR D, after review ----------------------------------------------------------------------


def two_board_client(tmp_path: Path) -> tuple[TestClient, Path]:
    cfg = Config(
        projects=(Project(name="one", path=tmp_path), Project(name="two", path=tmp_path)),
        claude_home=tmp_path / "c",
    )
    db = tmp_path / "data" / "board.db"
    Store(db).close()
    return TestClient(create_app(cfg, db, PORT), base_url=BASE, follow_redirects=False), db


def noted(project: str, decided: list[str], cards: list = ()) -> object:
    r = result(list(cards), project=project)
    r.session_notes = {
        "s1": {"title": "The run", "decided": decided, "first_at": now(), "last_at": now()}
    }
    return r


def test_a_sessions_answers_from_two_boards_are_both_kept(tmp_path: Path) -> None:
    client, db = two_board_client(tmp_path)
    with Store(db) as s:
        s.apply(noted("one", ["Which? → A"]), now())
        s.apply(noted("two", ["Where? → B"]), now())
    page = client.get("/s/s1").text
    assert "Which? → A" in page and "Where? → B" in page


def test_a_sessions_answers_leave_once_a_plan_takes_them(tmp_path: Path) -> None:
    client, db = two_board_client(tmp_path)
    ask = card("ny:1", "needs_you", kind="needs", source={"session": "s1"})
    with Store(db) as s:
        s.apply(noted("one", ["Which? → A"], [ask]), now())
        r = result([ask], project="one")
        r.sessions = {"s1"}
        s.apply(r, now())  # a later plan took the answer: no note any more
    assert "Which? → A" not in client.get("/s/s1").text


def test_a_gone_cards_session_page_still_answers(tmp_path: Path) -> None:
    client, db = two_board_client(tmp_path)
    with Store(db) as s:
        s.apply(
            result(
                [card("pr:1", "done", kind="pr", title="Old PR", source={"session": "s1"})],
                project="one",
            ),
            now(),
        )
        s.apply(result([], project="one"), now())
    r = client.get("/s/s1")
    assert r.status_code == 200 and "Old PR" in r.text


def test_a_card_stored_after_the_mark_is_new(tmp_path: Path) -> None:
    client, db = two_board_client(tmp_path)
    with Store(db) as s:
        s.mark_seen("one", now())
        s.apply(result([card("ny:1", "needs_you", kind="needs")], project="one"), now(-1))
    assert 'class="badge new"' in client.get("/p/one").text  # the sync started before the mark


def test_mark_all_seen_counts_from_when_the_page_was_drawn(env: tuple[TestClient, Path]) -> None:
    client, db = env
    drawn = now(-0.001)
    with Store(db) as s:
        s.apply(
            result(
                [
                    card("ny:1", "needs_you", kind="needs"),
                    card("ny:late", "needs_you", kind="needs", title="Came in late"),
                ]
            ),
            now(),
        )
    client.post("/p/demo/seen", data={"drawn": drawn}, headers=SAME)
    page = client.get("/p/demo").text
    assert page.count('class="badge new"') >= 1  # cards stored after the page was drawn stay new
    client.post("/p/demo/seen", data={"drawn": "not a time"}, headers=SAME)
    assert 'class="badge new"' not in client.get("/p/demo").text


def test_a_reworded_idea_is_not_new(tmp_path: Path) -> None:
    client, db = two_board_client(tmp_path)
    with Store(db) as s:
        s.apply(
            result([card("bl:a", "ideas", title="A pole swing for the city")], project="one"), now()
        )
        s.mark_seen("one", now())
        s.apply(
            result([card("bl:b", "ideas", title="A pole swing for the whole city")], project="one"),
            now(1),
        )
    assert 'class="badge new"' not in client.get("/p/one?ideas=1").text


def test_a_finished_sync_is_not_restarted_by_the_reload(tmp_path: Path) -> None:
    client, db = two_board_client(tmp_path)
    with Store(db) as s:
        s.apply(result([], project="one"), now(-1))  # its start is long past: stale
    board = client.app.state.board
    board._finished["one"] = __import__("time").monotonic()  # but it just finished
    client.get("/p/one", headers={"Sec-Fetch-Site": "none"})
    assert not board._threads


def test_the_gone_list_follows_the_game(env: tuple[TestClient, Path]) -> None:
    client, db = env
    with Store(db) as s:
        s.apply(result([card("bl:1", "ideas", title="A pole swing")]), now())
    assert "A beta ask" in client.get("/p/demo").text.split("gone from their sources")[-1]
    assert "A beta ask" not in client.get("/p/demo?sub=alpha").text


def test_pages_name_games_and_cap_an_unknown_one(env: tuple[TestClient, Path]) -> None:
    client, db = env
    r = result([card("ny:1", "needs_you", kind="needs", title="x")])
    r.sub_names = {"alpha": "Alpha Game"}
    with Store(db) as s:
        s.apply(r, now())
    assert "Alpha Game" in client.get("/i/ny:1").text
    assert "Alpha Game" in client.get("/p/demo/triage").text
    page = client.get("/p/demo?sub=" + "x" * 300).text
    (shown,) = re.findall(r"No game named “([^”]*)”", page)
    assert shown == "x" * 40 + "…"


def test_a_link_whose_address_has_parentheses_keeps_its_text() -> None:
    from docboard.web import plain

    assert plain("see [the doc](https://x.example/a_(b)) now") == "see the doc now"
