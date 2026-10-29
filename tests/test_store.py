from __future__ import annotations

import json
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


# --- PR A: a sync you can trust ------------------------------------------------------------


def test_migration_2_upgrades_a_version_1_db(tmp_path: Path) -> None:
    from docboard import store as store_mod

    path = tmp_path / "board.db"
    db = sqlite3.connect(path)
    for stmt in store_mod._statements(store_mod.MIGRATIONS[0]):
        db.execute(stmt)
    db.execute("PRAGMA user_version = 1")
    db.execute(
        "INSERT INTO card (id, project, kind, column_derived, sub_derived, sub_reason, title, body,"
        " event_at, source, badges, parent, children, decided, evidence, raised, older,"
        " first_seen, last_seen) VALUES ('ny:1', 'demo', 'needs', 'needs_you', '', 'root', 't',"
        " '', '2026-10-01T00:00:00+00:00', '{}', '[]', NULL, '[]', '[]', '', 1, 0, 'x', 'x')"
    )
    db.execute(
        "INSERT INTO sync_run (project, started, status, counts, ref_sha, github, subs, degraded,"
        " message) VALUES ('demo', 'x', 'ok', '{}', NULL, NULL, '[]', '[]', '')"
    )
    db.commit()
    db.close()
    with Store(path) as s:
        assert s.db.execute("PRAGMA user_version").fetchone()[0] == len(store_mod.MIGRATIONS) >= 2
        row = s.card("ny:1")  # read through the board view: its c.* sees the new columns
        assert row["raised_at"] == "2026-10-01T00:00:00+00:00"
        assert (row["source_gone_at"], row["reopened_at"], row["moved_from"]) == (None,) * 3
        run = s.last_run("demo")
        assert (run["timings"], run["warnings"], run["sub_names"]) == ("{}", "[]", "{}")
        for table in ("visit", "batch", "batch_item", "session"):
            assert s.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_a_gh_outage_stores_everything_else_and_freezes_prs(store: Store) -> None:
    ask = card("ny:1", "needs_you", kind="needs")
    store.apply(result([card("pr:1", "done", kind="pr"), ask]), now())
    down = {**COUNTS, "prs_merged": 0}
    fresh = card("ny:2", "needs_you", kind="needs")
    store.apply(result([ask, fresh], down, degraded=["gh: exit 4"]), now())
    assert col(store, "ny:2") == "needs_you"  # a new ask still arrives while gh is down
    assert col(store, "pr:1") == "done" and store.card("pr:1")["gone_at"] is None
    assert store.last_run("demo")["status"] == "ok"


def test_after_an_outage_a_silent_pr_drop_is_still_refused(store: Store) -> None:
    store.apply(result([card("pr:1", "done", kind="pr")]), now())
    down = {**COUNTS, "prs_merged": 0}
    store.apply(result([], down, degraded=["gh: exit 4"]), now())
    with pytest.raises(FloorError, match="prs_merged: 10 → 0"):
        store.apply(result([], down), now())  # gh answered, with nothing: a parser broke


def test_an_accepted_drop_becomes_the_baseline(store: Store) -> None:
    store.apply(result([card("c1", "ideas")]), now())
    small = {**COUNTS, "backlog": 3}
    with pytest.raises(FloorError):
        store.apply(result([card("c1", "ideas")], small), now())
    report = store.apply(result([card("c1", "ideas")], small), now(), accept_drop=True)
    run = store.last_run("demo")
    assert run["status"] == "ok" and "accepted drop: backlog: 10 → 3" in run["message"]
    assert report.accepted == ["backlog: 10 → 3 (more than half gone)"]
    store.apply(result([card("c1", "ideas")], small), now())  # the new baseline: no refusal


def test_accepting_a_drop_never_accepts_another(store: Store) -> None:
    store.apply(result([]), now())
    with pytest.raises(FloorError):
        store.apply(result([], {**COUNTS, "backlog": 3}), now())
    worse = {**COUNTS, "backlog": 3, "needs_items": 0}
    with pytest.raises(FloorError, match="needs_items: 8 → 0"):
        store.apply(result([], worse), now(), accept_drop=True)
    assert json.loads(store.last_run("demo", ok_only=True)["counts"])["backlog"] == 10


def test_a_repeated_id_in_one_sync_keeps_the_first(store: Store) -> None:
    twice = [card("bl:1", "ideas", title="first"), card("bl:1", "ideas", title="second")]
    report = store.apply(result(twice), now())
    assert store.card("bl:1")["title"] == "first"
    assert report.duplicates == ["bl:1"]
    assert "bl:1" in store.last_run("demo")["warnings"]


def test_a_card_another_board_derives_moves_with_the_owners_moves(store: Store) -> None:
    ask = card("ny:1", "needs_you", kind="needs", sub="alpha")
    store.apply(result([ask], project="one"), now())
    store.resolve("ny:1", now())
    store.set_sub("ny:1", "beta", ["alpha", "beta"], now())
    store.apply(result([], project="one"), now())  # routing moved it: one no longer derives it
    store.apply(result([ask], project="two"), now())
    row = store.card("ny:1")
    assert (row["project"], row["col"], row["sub"], row["moved_from"]) == (
        "two", "done", "beta", "one",
    )  # fmt: skip
    assert store.events("ny:1")[0]["detail"] == "moved here from one"
    store.apply(result([], project="one"), now())
    assert store.card("ny:1")["project"] == "two" and store.card("ny:1")["gone_at"] is None
    assert store.gone("one") == []


def test_two_boards_deriving_one_id_do_not_flap(store: Store) -> None:
    pr = card("pr:acme/demo#1", "done", kind="pr")
    store.apply(result([pr], project="one"), now())
    report = store.apply(result([pr], project="two"), now())
    assert store.card(pr.id)["project"] == "one" and report.elsewhere == [pr.id]
    store.apply(result([pr], project="one"), now())
    store.apply(result([pr], project="two"), now())
    assert store.card(pr.id)["project"] == "one"  # the first holder keeps it while it derives it


def test_an_open_ask_outlives_its_transcript(store: Store) -> None:
    ask = card("ny:1", "needs_you", kind="needs", source={"session": "s1"})
    r = result([ask])
    r.sessions = {"s1"}
    store.apply(r, now())
    store.apply(result([]), now())  # the transcript was cleaned up: s1 is no longer read
    row = store.card("ny:1")
    assert row["gone_at"] is None and row["source_gone_at"] is not None
    assert row["col"] == "needs_you"
    store.resolve("ny:1", now())
    store.apply(result([]), now())
    assert store.card("ny:1")["gone_at"] is not None  # once you acted, it may leave


def test_an_answered_question_still_leaves(store: Store) -> None:
    q = card("aq:1", "needs_you", kind="question", source={"session": "s1"})
    r = result([q])
    r.sessions = {"s1"}
    store.apply(r, now())
    r = result([])
    r.sessions = {"s1"}  # its session is still read: the answer took it away
    store.apply(r, now())
    assert store.card("aq:1")["gone_at"] is not None


def test_a_raise_after_a_resolve_reopens_the_card(store: Store) -> None:
    store.apply(result([card("ny:1", "needs_you", kind="needs", event_at=now(-3))]), now())
    store.resolve("ny:1", now(-2))
    again = card("ny:1", "needs_you", kind="needs", event_at=now(-3), raised=2, raised_at=now(-1))
    report = store.apply(result([again]), now())
    row = store.card("ny:1")
    assert row["col"] == "needs_you" and row["reopened_was"] == "done" and row["reopened_at"]
    assert "after you resolved it" in store.events("ny:1")[0]["detail"]
    assert report.reopened == 1


def test_a_raise_before_the_resolve_does_not_reopen(store: Store) -> None:
    store.apply(result([card("ny:1", "needs_you", kind="needs", event_at=now(-3))]), now())
    store.resolve("ny:1", now())  # resolved after the re-raise below happened
    again = card("ny:1", "needs_you", kind="needs", event_at=now(-3), raised=2, raised_at=now(-1))
    store.apply(result([again]), now())
    assert col(store, "ny:1") == "done"
    store.apply(result([again]), now())
    assert col(store, "ny:1") == "done"


def test_undoing_a_batch_restores_each_cards_previous_move(store: Store) -> None:
    asks = [card(f"ny:{i}", "needs_you", kind="needs") for i in (1, 2, 3)]
    store.apply(result(asks), now())
    store.move("ny:2", "next", now(-1))
    earlier = dict(store.db.execute("SELECT * FROM override WHERE card_id = 'ny:2'").fetchone())
    batch = store.move_batch("demo", ["ny:1", "ny:2", "ny:3", "nope"], "done", "resolve", now())
    assert [col(store, f"ny:{i}") for i in (1, 2, 3)] == ["done"] * 3
    store.move("ny:3", "hidden", now())  # moved again since the batch: left alone
    with pytest.raises(StoreError):
        store.undo_batch(batch, "another-project", now())
    assert store.undo_batch(batch, "demo", now()) == (2, 1)
    assert col(store, "ny:1") == "needs_you" and store.card("ny:1")["moved"] == 0
    after = dict(store.db.execute("SELECT * FROM override WHERE card_id = 'ny:2'").fetchone())
    assert after == earlier  # the earlier move is back exactly, its time included
    assert col(store, "ny:3") == "hidden"
    with pytest.raises(StoreError):
        store.undo_batch(batch, "demo", now())


# --- PR A, after review ----------------------------------------------------------------------


def test_a_failed_run_after_the_refusal_does_not_widen_the_accept(store: Store) -> None:
    store.apply(result([]), now())
    with pytest.raises(FloorError):
        store.apply(result([], {**COUNTS, "backlog": 3}), now())
    store.record_failed("demo", now(), "OSError: transient")
    with pytest.raises(FloorError, match="needs_items"):
        store.apply(result([], {**COUNTS, "backlog": 3, "needs_items": 0}), now(), accept_drop=True)
    assert json.loads(store.last_run("demo", ok_only=True)["counts"])["needs_items"] == 8


def test_a_second_drop_is_shown_then_accepted_in_one_more_step(store: Store) -> None:
    store.apply(result([]), now())
    with pytest.raises(FloorError):
        store.apply(result([], {**COUNTS, "backlog": 3}), now())
    worse = {**COUNTS, "backlog": 3, "needs_items": 0}
    with pytest.raises(FloorError) as refused:
        store.apply(result([], worse), now(), accept_drop=True)
    assert "backlog" in str(refused.value) and "needs_items" in str(refused.value)  # both shown
    report = store.apply(result([], worse), now(), accept_drop=True)
    assert len(report.accepted) == 2


def test_undoing_a_resolve_brings_back_an_ask_whose_transcript_is_gone(store: Store) -> None:
    ask = card("ny:1", "needs_you", kind="needs", source={"session": "s1"})
    r = result([ask])
    r.sessions = {"s1"}
    store.apply(r, now())
    store.apply(result([]), now())  # transcript gone: kept open
    batch = store.move_batch("demo", ["ny:1"], "done", "resolve", now())
    store.apply(result([]), now())  # the owner acted: it may leave
    assert store.card("ny:1")["gone_at"] is not None
    assert store.undo_batch(batch, "demo", now()) == (1, 0)
    row = store.card("ny:1")
    assert row["gone_at"] is None and row["col"] == "needs_you"
    store.apply(result([]), now())
    assert store.card("ny:1")["gone_at"] is None  # and it stays, kept open again


def test_three_boards_deriving_one_id_do_not_trade_it(store: Store) -> None:
    pr = card("pr:acme/demo#1", "done", kind="pr")
    for _ in range(3):
        for board in ("one", "two", "three"):
            store.apply(result([pr], project=board), now())
    assert store.card(pr.id)["project"] == "one"
    assert store.db.execute("SELECT COUNT(*) FROM event WHERE kind = 'moved'").fetchone()[0] == 0


def test_a_card_moves_once_its_old_board_stops_deriving_it(store: Store) -> None:
    ask = card("ny:1", "needs_you", kind="needs")
    store.apply(result([ask], project="one"), now())
    report = store.apply(result([ask], project="two"), now())
    assert store.card("ny:1")["project"] == "one" and report.elsewhere == ["ny:1"]
    store.apply(result([], project="one"), now())
    store.apply(result([ask], project="two"), now())
    assert store.card("ny:1")["project"] == "two" and store.card("ny:1")["gone_at"] is None


def test_a_kept_open_ask_still_folds_into_older(store: Store) -> None:
    ask = card("ny:1", "needs_you", kind="needs", source={"session": "s1"}, event_at=now(-20))
    r = result([ask])
    r.sessions = {"s1"}
    store.apply(r, now())
    assert store.card("ny:1")["older"] == 0  # the fixture's flag, as derive would leave it
    store.apply(result([]), now())
    assert store.card("ny:1")["older"] == 1


def test_resolving_cards_already_resolved_makes_no_batch(store: Store) -> None:
    store.apply(result([card("ny:1", "needs_you", kind="needs")]), now())
    first = store.move_batch("demo", ["ny:1"], "done", "resolve", now())
    assert store.move_batch("demo", ["ny:1"], "done", "resolve", now()) is None
    assert store.last_batch("demo")["id"] == first


def test_a_raise_after_a_dismiss_reopens_it_as_dismissed(store: Store) -> None:
    store.apply(result([card("ny:1", "needs_you", kind="needs", event_at=now(-3))]), now())
    store.dismiss("ny:1", now(-2))
    again = card("ny:1", "needs_you", kind="needs", event_at=now(-3), raised=2, raised_at=now(-1))
    store.apply(result([again]), now())
    row = store.card("ny:1")
    assert (row["col"], row["reopened_was"]) == ("needs_you", "hidden")
    assert "after you dismissed it" in store.events("ny:1")[0]["detail"]


@pytest.mark.parametrize("kind", ["wrap", "plan", "question"])
def test_every_kind_of_open_ask_outlives_its_transcript(store: Store, kind: str) -> None:
    ask = card(f"{kind}:1", "needs_you", kind=kind, source={"session": "s1"})
    r = result([ask])
    r.sessions = {"s1"}
    store.apply(r, now())
    store.apply(result([]), now())
    assert store.card(f"{kind}:1")["gone_at"] is None


def test_the_transcript_gone_mark_clears_when_it_is_read_again(store: Store) -> None:
    ask = card("ny:1", "needs_you", kind="needs", source={"session": "s1"})
    r = result([ask])
    r.sessions = {"s1"}
    store.apply(r, now())
    store.apply(result([]), now())
    assert store.card("ny:1")["source_gone_at"] is not None
    store.apply(r, now())
    assert store.card("ny:1")["source_gone_at"] is None


def test_adopting_drops_a_game_this_board_lacks_and_writes_no_back_event(store: Store) -> None:
    ask = card("ny:1", "needs_you", kind="needs", sub="alpha")
    one = result([ask], project="one")
    one.subs = ["alpha", "gamma"]
    store.apply(one, now())
    store.set_sub("ny:1", "gamma", ["alpha", "gamma"], now())
    store.apply(result([], project="one"), now())  # gone from one
    store.apply(result([ask], project="two"), now())  # two's games: alpha and beta
    row = store.card("ny:1")
    assert (row["project"], row["sub"]) == ("two", "alpha")
    kinds = [e["kind"] for e in store.events("ny:1")]
    assert "moved" in kinds and "back" not in kinds
    assert "not one of this board's" in store.events("ny:1")[0]["detail"]


def test_raised_at_reaches_the_board_view(store: Store) -> None:
    again = card("ny:1", "needs_you", kind="needs", event_at=now(-20), raised=2, raised_at=now(-1))
    store.apply(result([again]), now())
    row = store.card("ny:1")
    assert row["raised_at"] == again.raised_at and row["event_at"] == again.event_at


# --- PR B: cards in the right place ---------------------------------------------------------


def test_a_second_bullet_with_the_same_title_does_not_take_the_move(store: Store) -> None:
    store.apply(
        result([card("bl:a", "ideas", title="Sky", body="a blue sky over the city")]), now()
    )
    store.move("bl:a", "next", now())
    # A second "Sky" bullet salts both ids; the new one comes first in the sync.
    newcomer = card("bl:c", "ideas", title="Sky", body="lanterns and a fair in the square")
    original = card("bl:b", "ideas", title="Sky", body="a blue sky over the city")
    store.apply(result([newcomer, original]), now())
    assert (col(store, "bl:b"), col(store, "bl:c")) == ("next", "ideas")


def test_a_renamed_and_ticked_item_drops_the_stale_move(store: Store) -> None:
    store.apply(
        result([card("rm:a", "next", kind="roadmap", title="Ship the reward cards")]), now()
    )
    store.dismiss("rm:a", now())
    ticked = card("rm:b", "done", kind="roadmap", title="Ship the reward cards now")
    store.apply(result([ticked]), now())
    assert col(store, "rm:b") == "done"
    assert store.db.execute("SELECT COUNT(*) FROM override").fetchone()[0] == 0
    assert "no longer applies" in store.events("rm:b")[0]["detail"]
    store.apply(result([card("rm:b", "next", kind="roadmap", title="Ship the reward cards now")]),
                now())  # fmt: skip
    assert col(store, "rm:b") == "next"  # unticked: the old dismissal does not come back
