from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from boardkit import COUNTS, card, now, result

from docboard.store import FloorError, Store, StoreError


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "data" / "board.db")
    yield s
    s.close()


def col(store: Store, cid: str) -> str:
    return store.card(cid)["col"]


def test_the_database_is_private(tmp_path: Path) -> None:
    Store(tmp_path / "d" / "board.db").close()
    assert oct((tmp_path / "d" / "board.db").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "d").stat().st_mode & 0o777) == "0o700"


def test_a_move_holds_only_until_its_source_moves_the_card(store: Store) -> None:
    store.apply(result([card("c1", "next")]), now())
    store.move("c1", "done", now())
    assert col(store, "c1") == "done"
    store.apply(result([card("c1", "next")]), now())
    assert col(store, "c1") == "done"  # the source said the same: the move holds
    store.apply(result([card("c1", "in_progress")]), now())
    assert col(store, "c1") == "in_progress"  # the source moved it: the move is dropped
    store.apply(result([card("c1", "next")]), now())
    assert col(store, "c1") == "next"  # and it does not come back
    kinds = [e["kind"] for e in store.events("c1")]
    assert kinds[:3] == ["source", "source", "move"]
    assert "no longer applies" in store.events("c1")[1]["detail"]


def test_moving_a_card_back_to_its_source_column_clears_the_move(store: Store) -> None:
    store.apply(result([card("c1", "next")]), now())
    store.move("c1", "done", now())
    store.move("c1", "next", now())
    assert store.card("c1")["moved"] == 0


def test_a_resolved_needs_item_stays_resolved(store: Store) -> None:
    item = card("ny:1", "needs_you", kind="needs")
    store.apply(result([item]), now())
    store.resolve("ny:1", now())
    store.apply(result([item]), now())
    assert col(store, "ny:1") == "done"


def test_a_dismissed_card_comes_back_when_its_source_changes(store: Store) -> None:
    store.apply(result([card("c1", "next")]), now())
    store.dismiss("c1", now())
    assert col(store, "c1") == "hidden"
    store.apply(result([card("c1", "done")]), now())
    assert col(store, "c1") == "done"


def test_a_collapsed_source_writes_nothing(store: Store) -> None:
    store.apply(result([card("c1", "next", title="before")]), now())
    with pytest.raises(FloorError, match="backlog: 10 → 0"):
        store.apply(result([card("c1", "done", title="after")], {**COUNTS, "backlog": 0}), now())
    row = store.card("c1")
    assert (row["col"], row["title"]) == ("next", "before")
    assert store.last_run("demo")["status"] == "refused"
    assert store.last_run("demo", ok_only=True)["status"] == "ok"


def test_with_gh_down_pr_and_plan_cards_keep_their_last_state(store: Store) -> None:
    store.apply(result([card("pr:1", "done", kind="pr"), card("pl:1", "done", kind="plan")]), now())
    store.apply(result([card("pl:1", "next", kind="plan")], degraded=["gh: exit 4"]), now())
    assert (col(store, "pr:1"), col(store, "pl:1")) == ("done", "done")
    assert store.card("pr:1")["gone_at"] is None


def test_a_reworded_idea_keeps_the_owners_move(store: Store) -> None:
    store.apply(result([card("bl:a", "ideas", title="A pole swing for the city")]), now())
    store.move("bl:a", "next", now())
    store.apply(result([card("bl:b", "ideas", title="A pole swing for the whole city")]), now())
    assert store.card("bl:a") is None
    assert col(store, "bl:b") == "next"
    assert store.events("bl:b")[0]["kind"] == "renamed"


def test_an_unrelated_replacement_is_not_a_rename(store: Store) -> None:
    store.apply(result([card("bl:a", "ideas", title="A pole swing for the city")]), now())
    store.move("bl:a", "next", now())
    store.apply(result([card("bl:b", "ideas", title="Canal lofts with boats")]), now())
    assert store.card("bl:a")["gone_at"] is not None
    assert col(store, "bl:b") == "ideas"


def test_a_card_gone_from_its_source_is_kept_and_can_return(store: Store) -> None:
    store.apply(result([card("pr:1", "done", kind="pr")]), now())
    store.apply(result([]), now())
    assert store.card("pr:1")["gone_at"] is not None
    assert [r["id"] for r in store.gone("demo")] == ["pr:1"]
    store.apply(result([card("pr:1", "done", kind="pr")]), now())
    assert store.card("pr:1")["gone_at"] is None
    assert store.events("pr:1")[0]["kind"] == "back"


def test_the_owner_sets_a_game_and_can_set_it_back(store: Store) -> None:
    store.apply(result([card("c1", "next", sub="alpha")]), now())
    store.set_sub("c1", "beta", ["alpha", "beta"], now())
    assert store.card("c1")["sub"] == "beta"
    store.apply(result([card("c1", "next", sub="alpha")]), now())
    assert store.card("c1")["sub"] == "beta"
    store.set_sub("c1", "alpha", ["alpha", "beta"], now())
    assert store.card("c1")["sub_moved"] == 0
    with pytest.raises(StoreError):
        store.set_sub("c1", "nope", ["alpha", "beta"], now())


def test_manual_cards(store: Store) -> None:
    cid = store.add_manual("demo", "alpha", "next", "  Call the venue  ", "", now())
    assert cid.startswith("mc:") and store.manual_card(cid)["title"] == "Call the venue"
    store.move_manual(cid, "done", now())
    assert store.manual_card(cid)["col"] == "done"
    store.delete_manual(cid, now())
    assert store.manual("demo") == []
    with pytest.raises(StoreError):
        store.add_manual("demo", "", "next", "   ", "", now())


def test_a_bad_column_is_refused(store: Store) -> None:
    store.apply(result([card("c1", "next")]), now())
    with pytest.raises(StoreError):
        store.move("c1", "trash", now())


def test_a_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "board.db"
    Store(path).close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version = 99")
    db.close()
    with pytest.raises(StoreError, match="newer"):
        Store(path)
