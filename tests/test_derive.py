from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import factories as f

from docboard import derive as D
from docboard import transcripts as T
from docboard.attribution import Attributor
from docboard.docsrc import Bullet
from docboard.ghsrc import PullRequest

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
S = "sess-1"


def att() -> Attributor:
    return Attributor.build("games", ["alpha", "beta"], {"alpha": "Alpha Game"}, {}, {})


def pr(
    n: int, state: str, created: str, merged: str | None = None, title: str = "work"
) -> PullRequest:
    return PullRequest("acme/demo", n, title, state, created, merged, None, f"branch-{n}")


def cards(
    tmp_path: Path,
    records: list[dict],
    prs: list[PullRequest] = (),
    backlog=(),
    now: datetime = NOW,
) -> dict[str, D.Card]:
    proj = tmp_path / "projects" / T.encode_path(f.ROOT)
    f.write(proj / f"{S}.jsonl", records)
    scan = T.scan_project(f.ROOT, tmp_path / "projects")
    scan.session_mtime = {s: now.timestamp() for s in scan.session_mtime}  # still active
    result, _ = D.derive(
        D.Inputs(
            project="demo",
            github="acme/demo",
            attributor=att(),
            backlog=list(backlog),
            roadmap=[],
            prs=list(prs),
            squash={},
            scan=scan,
            now=now,
        )
    )
    return {c.id: c for c in result}


PLAN_2 = "# Alpha Game: two things\n\n## PR 1: one\n\n## PR 2: two\n"


def test_a_pr_joins_the_plan_approved_last_before_it_was_created(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval(S, "2026-10-20T10:00:00Z", "p1", "# Alpha Game: first"),
        *f.plan_approval(S, "2026-10-21T10:00:00Z", "p2", "# Alpha Game: second"),
        f.pr_link(S, 7, "2026-10-21T15:00:00Z"),  # linked hours after it was created
        f.pr_link(S, 6, "2026-10-21T15:00:00Z"),
    ]
    got = cards(
        tmp_path,
        recs,
        [
            pr(7, "open", "2026-10-21T11:00:00Z"),
            pr(6, "merged", "2026-10-19T09:00:00Z", "2026-10-19T10:00:00Z"),
        ],
    )
    assert got["pr:acme/demo#7"].parent == "pl:p2"
    assert got["pr:acme/demo#6"].parent is None  # created before any plan in its session
    assert got["pl:p2"].column == D.IN_PROGRESS
    assert got["pl:p1"].column == D.NEXT


def test_a_plan_is_done_only_when_every_pr_it_named_is_in(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval(S, "2026-10-25T10:00:00Z", "p1", PLAN_2),
        f.pr_link(S, 1, "2026-10-25T12:00:00Z"),
    ]
    one = [pr(1, "merged", "2026-10-25T11:00:00Z", "2026-10-25T13:00:00Z")]
    got = cards(tmp_path, recs, one)
    assert got["pl:p1"].column == D.IN_PROGRESS
    assert got["pl:p1"].badges == ["1 of 2 PRs merged"]
    recs.append(f.pr_link(S, 2, "2026-10-26T12:00:00Z"))
    both = [*one, pr(2, "merged", "2026-10-26T11:00:00Z", "2026-10-26T13:00:00Z")]
    assert cards(tmp_path, recs, both)["pl:p1"].column == D.DONE


def test_a_plan_without_a_pr_list_waits_for_its_session_to_move_on(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval(S, "2026-10-25T10:00:00Z", "p1", "# Alpha Game: one go"),
        f.pr_link(S, 1, "2026-10-25T12:00:00Z"),
    ]
    prs = [pr(1, "merged", "2026-10-25T11:00:00Z", "2026-10-25T13:00:00Z")]
    assert cards(tmp_path, recs, prs)["pl:p1"].column == D.IN_PROGRESS
    recs.extend(f.plan_approval(S, "2026-10-26T10:00:00Z", "p2", "# Beta: next"))
    assert cards(tmp_path, recs, prs)["pl:p1"].column == D.DONE


def test_a_partly_merged_plan_leaves_in_progress_once_quiet_for_two_weeks(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval(S, "2026-10-01T10:00:00Z", "p1", PLAN_2),
        f.pr_link(S, 1, "2026-10-01T12:00:00Z"),
    ]
    merged_then = [pr(1, "merged", "2026-10-01T11:00:00Z", "2026-10-20T13:00:00Z")]
    assert cards(tmp_path, recs, merged_then)["pl:p1"].column == D.IN_PROGRESS  # 7 days: not yet
    quiet = [pr(1, "merged", "2026-10-01T11:00:00Z", "2026-10-02T13:00:00Z")]
    card = cards(tmp_path, recs, quiet)["pl:p1"]
    assert card.column == D.DONE
    assert card.badges == ["1 of 2 PRs merged", "quiet since 2026-10-02"]


def test_an_unapproved_plan_waits_on_the_owner(tmp_path: Path) -> None:
    recs = [
        f.tool_use(
            S, "u1", "2026-10-26T10:00:00Z", "ExitPlanMode", "p1", {"plan": "# Alpha Game: x"}
        )
    ]
    got = cards(tmp_path, recs)
    assert got["pl:p1"].column == D.NEEDS_YOU and got["pl:p1"].sub == "alpha"


def test_a_needs_item_stays_in_needs_you_whatever_followed_it(tmp_path: Path) -> None:
    recs = [
        f.assistant_text(S, "a1", "2026-10-26T10:00:00Z", f.needs_block("Do — set the token")),
        f.human(S, "h1", "2026-10-26T10:05:00Z", "done, set it"),
    ]
    card = cards(tmp_path, recs)["ny:a1:1"]
    assert card.column == D.NEEDS_YOU
    assert card.suggested_evidence == "done, set it"


def test_a_re_raised_item_is_one_card(tmp_path: Path) -> None:
    recs = [
        f.assistant_text(
            S, "a1", "2026-10-26T10:00:00Z", f.needs_block("Do — hand check the Alpha Game night")
        ),
        f.assistant_text(
            S, "a2", "2026-10-26T11:00:00Z", f.needs_block("Do — hand check Alpha Game's night")
        ),
        f.assistant_text(
            "s2",
            "a3",
            "2026-10-26T12:00:00Z",
            f.needs_block("Do — hand check the Alpha Game night"),
        ),
        f.assistant_text(
            "s3", "a4", "2026-10-26T12:00:00Z", f.needs_block("Do — hand check the Beta night")
        ),
    ]
    got = [c for c in cards(tmp_path, recs).values() if c.kind == "needs"]
    assert sorted((c.id, c.raised) for c in got) == [("ny:a1:1", 3), ("ny:a4:1", 1)]


def test_needs_item_attribution_order(tmp_path: Path) -> None:
    beta = str(f.ROOT / "games" / "beta")
    recs = [
        f.human(S, "h1", "2026-10-26T09:00:00Z", "go"),
        *[
            f.tool_use(
                S,
                f"e{i}",
                f"2026-10-26T09:0{i}:00Z",
                "Edit",
                f"t{i}",
                {"file_path": f"{beta}/f{i}"},
            )
            for i in range(1, 4)
        ],
        f.assistant_text(
            S,
            "a1",
            "2026-10-26T09:30:00Z",
            f.needs_block("Do — check it", "Decide — Alpha Game's sky"),
        ),
    ]
    got = cards(tmp_path, recs)
    assert (got["ny:a1:1"].sub, got["ny:a1:1"].sub_reason) == ("beta", "turn")
    assert (got["ny:a1:2"].sub, got["ny:a1:2"].sub_reason) == ("alpha", "title")


def test_answers_land_on_the_plan_they_shaped(tmp_path: Path) -> None:
    q = {"questions": [{"question": "Where?"}]}
    recs = [
        f.tool_use(S, "u1", "2026-10-26T09:00:00Z", "AskUserQuestion", "q1", q),
        f.tool_result(S, "r1", "2026-10-26T09:01:00Z", "q1", "ok", {"answers": {"Where?": "Here"}}),
        *f.plan_approval(S, "2026-10-26T10:00:00Z", "p1", "# Alpha Game: here"),
        f.tool_use(
            S,
            "u2",
            "2026-10-26T11:00:00Z",
            "AskUserQuestion",
            "q2",
            {"questions": [{"question": "Now?"}]},
        ),
    ]
    got = cards(tmp_path, recs)
    assert got["pl:p1"].decided == ["Where? → Here"]
    assert got["aq:q2"].column == D.NEEDS_YOU


def test_backlog_bullet_keeps_its_id_when_it_moves_into_a_game(tmp_path: Path) -> None:
    bullet = Bullet("A pole swing", " (2026-10-25, the glider): swing.", 5)
    root = cards(tmp_path, [], backlog=[(D.DocFile("docs/DECISIONS.md", ""), bullet)])
    moved = cards(
        tmp_path, [], backlog=[(D.DocFile("games/alpha/docs/DECISIONS.md", "alpha"), bullet)]
    )
    assert root.keys() == moved.keys()
    (cid,) = root
    assert (root[cid].sub, moved[cid].sub, moved[cid].sub_reason) == ("", "alpha", "path")


def test_a_wrap_up_ask_repeating_a_needs_item_is_that_card(tmp_path: Path) -> None:
    ask = {
        "recap": "Built it.",
        "ask": "Do these cards look right enough to build the page?",
        "actions": [{"label": "Build the page"}],
    }
    other = {"recap": "Also.", "ask": "Which colour should the Beta logo use?"}
    recs = [
        f.assistant_text(
            S,
            "a1",
            "2026-10-26T10:00:00Z",
            f.needs_block("Decide — do the board's cards look right before I build the page?"),
        ),
        f.tool_use(S, "w1", "2026-10-26T10:00:01Z", "mcp__ccd_turn__wrap_up", "t1", ask),
        f.tool_use(S, "w2", "2026-10-26T11:00:00Z", "mcp__ccd_turn__wrap_up", "t2", other),
    ]
    got = cards(tmp_path, recs)
    assert sorted(c.id for c in got.values()) == ["ny:a1:1", "wu:t2"]
    assert got["ny:a1:1"].raised == 2


def test_html_entities_in_item_text_are_decoded(tmp_path: Path) -> None:
    recs = [
        f.assistant_text(
            S,
            "a1",
            "2026-10-26T10:00:00Z",
            f.needs_block("Do — open Settings &gt; General &amp; tick it"),
        )
    ]
    assert cards(tmp_path, recs)["ny:a1:1"].title == "Do — open Settings > General & tick it"


def test_a_re_raise_moves_age_fold_and_evidence_to_the_latest(tmp_path: Path) -> None:
    recs = [
        f.assistant_text(
            S, "a1", "2026-10-01T10:00:00Z", f.needs_block("Do — hand check the Alpha Game night")
        ),
        f.human(S, "h1", "2026-10-01T10:05:00Z", "later"),
        f.assistant_text(
            S, "a2", "2026-10-20T10:00:00Z", f.needs_block("Do — hand check Alpha Game's night")
        ),
        f.assistant_text(
            "s2",
            "a3",
            "2026-10-26T12:00:00Z",
            f.needs_block("Do — hand check the Alpha Game night"),
        ),
        f.human("s2", "h2", "2026-10-26T12:05:00Z", "checked it, fine"),
    ]
    c = cards(tmp_path, recs)["ny:a1:1"]
    assert c.raised == 3 and c.event_at == "2026-10-01T10:00:00Z"
    assert c.raised_at == "2026-10-26T12:00:00Z"
    assert c.older is False  # first raised 26 days ago, last raised yesterday
    assert c.suggested_evidence == "checked it, fine"


def test_a_wrap_card_shows_your_next_message(tmp_path: Path) -> None:
    ask = {"recap": "Built it.", "ask": "Which colour should the Beta logo use?"}
    recs = [
        f.tool_use(S, "w1", "2026-10-26T10:00:00Z", "mcp__ccd_turn__wrap_up", "t1", ask),
        f.human(S, "h1", "2026-10-26T10:02:00Z", "blue"),
    ]
    assert cards(tmp_path, recs)["wu:t1"].suggested_evidence == "blue"


def test_a_later_raise_in_the_same_session_is_the_latest(tmp_path: Path) -> None:
    recs = [
        f.assistant_text(S, "a1", "2026-10-25T10:00:00Z", f.needs_block("Do — sign Studio in")),
        f.human(S, "h1", "2026-10-25T10:05:00Z", "first reply"),
        f.assistant_text(S, "a2", "2026-10-26T10:00:00Z", f.needs_block("Do — sign Studio in")),
        f.human(S, "h2", "2026-10-26T10:05:00Z", "done now"),
    ]
    c = cards(tmp_path, recs)["ny:a1:1"]
    assert (c.raised, c.raised_at, c.suggested_evidence) == (2, "2026-10-26T10:00:00Z", "done now")


def test_an_earlier_raise_never_overwrites_a_later_one(tmp_path: Path) -> None:
    recs = [
        f.assistant_text(S, "a1", "2026-10-20T10:00:00Z", f.needs_block("Do — sign Studio in")),
        f.assistant_text(S, "a2", "2026-10-26T10:00:00Z", f.needs_block("Do — sign Studio in")),
        f.human(S, "h2", "2026-10-26T10:05:00Z", "done now"),
        # Another session raised it between the two: folded in, but not the latest.
        f.assistant_text("s2", "b1", "2026-10-22T10:00:00Z", f.needs_block("Do — sign Studio in")),
        f.human("s2", "hb", "2026-10-22T10:05:00Z", "not yet"),
    ]
    c = cards(tmp_path, recs)["ny:a1:1"]
    assert (c.raised, c.raised_at, c.suggested_evidence) == (3, "2026-10-26T10:00:00Z", "done now")


def test_a_wrap_up_repeating_an_ask_is_its_latest_raise(tmp_path: Path) -> None:
    ask = {"recap": "Built it.", "ask": "Do — sign Studio in again"}
    recs = [
        f.assistant_text(
            S, "a1", "2026-10-26T10:00:00Z", f.needs_block("Do — sign Studio in again")
        ),
        f.tool_use(S, "w1", "2026-10-26T11:00:00Z", "mcp__ccd_turn__wrap_up", "t1", ask),
        f.human(S, "h1", "2026-10-26T11:05:00Z", "signed in"),
    ]
    c = cards(tmp_path, recs)["ny:a1:1"]
    assert (c.raised_at, c.suggested_evidence) == ("2026-10-26T11:00:00Z", "signed in")
