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
        s.apply(result([
            card("ny:1", "needs_you", kind="needs", title="<script>alert(1)</script> set the token",
                 body="see javascript:alert(1) and https://example.com/x."),
            card("ny:old", "needs_you", kind="needs", title="An old ask", older=True),
            card("ny:b", "needs_you", kind="needs", title="A beta ask", sub="beta"),
            card("bl:1", "ideas", title="A pole swing",
                 source={"path": "docs/DECISIONS.md", "line": 5, "ref_sha": "abc123"}),
            card("bl:2", "ideas", title="Outside", source={"path": "../../secrets.txt", "line": 1}),
        ]), now())  # fmt: skip
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
    boxes = re.findall(r'<input type="checkbox" name="ids"[^>]*>', client.get(
        "/p/demo/triage?select=all").text)  # fmt: skip
    assert len(boxes) == 3 and all("checked" in b for b in boxes)
    plain = re.findall(r'<input type="checkbox" name="ids"[^>]*>', client.get(
        "/p/demo/triage").text)  # fmt: skip
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
    client.get("/p/demo")
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
    r = client.post("/p/demo/triage", data={"action": "dismiss", "ids": ["ny:b"], "sub": "beta"},
                    headers=SAME)  # fmt: skip
    assert r.headers["location"] == "/p/demo/triage?sub=beta"
    with Store(db) as s:
        s.apply(
            result([card("ny:r", "needs_you", kind="needs", title="A root ask", sub="")]), now()
        )
    page = client.get("/p/demo/triage?sub=_root").text
    assert "A root ask" in page and "An old ask" not in page


def test_triage_moves_only_this_boards_cards(tmp_path: Path) -> None:
    cfg = Config(projects=(Project(name="one", path=tmp_path), Project(name="two", path=tmp_path)),
                 claude_home=tmp_path / "c")  # fmt: skip
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
        again = card("ny:1", "needs_you", kind="needs", title="Reopened", event_at=now(-3),
                     raised=2, raised_at=now(-1))  # fmt: skip
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
