from __future__ import annotations

from pathlib import Path

import pytest
from boardkit import card, now, result
from fastapi.testclient import TestClient

from docboard import cli
from docboard.config import Config, Project
from docboard.store import Store
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


def test_the_board_folds_old_items_and_triage_clears_them(env: tuple[TestClient, Path]) -> None:
    client, db = env
    page = client.get("/p/demo").text
    assert "An old ask" not in page and "Older (1)" in page
    assert "An old ask" in client.get("/p/demo?older=1").text
    r = client.post("/p/demo/triage", data={"action": "resolve_older"}, headers=SAME)
    assert r.status_code == 303
    with Store(db) as s:
        assert (s.card("ny:old")["col"], s.card("ny:1")["col"]) == ("done", "needs_you")


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
