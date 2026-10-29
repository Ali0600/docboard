"""Cards follow the files their work touched; plans join their PRs across continued sessions."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import factories as f
import pytest

from docboard import derive as D
from docboard import transcripts as T
from docboard.attribution import Attributor
from docboard.ghsrc import PullRequest
from docboard.route import Router

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
GADGET = Path("/" + "srv/work/gadget-repo")  # a second configured project, synthetic like ROOT
PROJECTS = [("demo", f.ROOT), ("gadget", GADGET)]
ROUTER = Router.build([("demo", "acme/demo"), ("gadget", "acme/gadget")])


def at(root: Path, rec: dict) -> dict:
    """A record written while the session's folder was `root`."""
    return dict(rec, cwd=str(root))


def edit(session: str, uid: str, ts: str, root: Path, rel: str) -> dict:
    return f.tool_use(session, uid, ts, "Edit", f"t-{uid}", {"file_path": f"{root}/{rel}"})


def write(tmp_path: Path, files: dict[str, tuple[Path, list[dict]]]) -> Path:
    projects = tmp_path / "projects"
    for name, (root, recs) in files.items():
        f.write(projects / T.encode_path(root) / f"{name}.jsonl", recs)
    return projects


def scan_of(tmp_path: Path, files: dict[str, tuple[Path, list[dict]]]) -> T.Scan:
    scan = T.scan_all(PROJECTS, write(tmp_path, files))
    scan.session_mtime = dict.fromkeys(scan.session_mtime, NOW.timestamp())  # still active
    return scan


def pr(
    n: int, created: str, title: str = "work", repo: str = "acme/demo", merged: str | None = None
) -> PullRequest:
    state = "merged" if merged else "open"
    return PullRequest(repo, n, title, state, created, merged, None, f"branch-{n}")


def board(
    scan: T.Scan, project: str, prs: list[PullRequest] = (), github: str | None = "default"
) -> dict[str, D.Card]:
    gh = {"demo": "acme/demo", "gadget": "acme/gadget"}[project] if github == "default" else github
    att = Attributor.build("games", ["alpha", "beta"], {"alpha": "Alpha Game"}, {}, {})
    cards, _ = D.derive(
        D.Inputs(
            project=project,
            github=gh,
            attributor=att,
            backlog=[],
            roadmap=[],
            prs=list(prs),
            squash={},
            scan=scan,
            now=NOW,
            router=ROUTER,
        )
    )
    return {c.id: c for c in cards}


# --- one pass over every project's transcripts ---------------------------------------------


def test_one_pass_files_each_transcript_under_its_project(tmp_path: Path) -> None:
    projects = write(
        tmp_path,
        {
            "s1": (f.ROOT, [f.human("s1", "h1", "2026-10-20T10:00:00Z", "a")]),
            "s2": (GADGET, [f.human("s2", "h2", "2026-10-20T10:00:00Z", "b")]),
        },
    )
    wt = f.ROOT / ".claude" / "worktrees" / "w1"
    f.write(
        projects / T.encode_path(wt) / "s3.jsonl",
        [at(wt, f.human("s3", "h3", "2026-10-20T10:00:00Z", "c"))],
    )
    sibling = Path(str(GADGET) + "-x")  # only looks like gadget's folder
    f.write(
        projects / T.encode_path(sibling) / "s4.jsonl",
        [at(sibling, f.human("s4", "h4", "2026-10-20T10:00:00Z", "d"))],
    )
    scan = T.scan_all(PROJECTS, projects)
    assert (scan.files_by["demo"], scan.files_by["gadget"], scan.foreign_by["gadget"]) == (2, 1, 1)
    assert scan.session_project == {"s1": "demo", "s2": "gadget", "s3": "demo"}


def test_touches_go_to_the_deepest_configured_root(tmp_path: Path) -> None:
    inner = f.ROOT / "games" / "alpha"
    recs = [
        edit("s1", "e1", "2026-10-20T10:00:00Z", f.ROOT, "games/alpha/x.luau"),
        edit("s1", "e2", "2026-10-20T10:01:00Z", f.ROOT, "games/beta/y.luau"),
        edit("s1", "e3", "2026-10-20T10:02:00Z", GADGET, "src/z.py"),
    ]
    scan = T.scan_all([*PROJECTS, ("alpha", inner)], write(tmp_path, {"s1": (f.ROOT, recs)}))
    assert sorted((t.project, t.rel) for t in scan.touches.values()) == [
        ("alpha", "x.luau"),
        ("demo", "games/beta/y.luau"),
        ("gadget", "src/z.py"),
    ]


def test_a_folder_in_another_case_is_read(tmp_path: Path) -> None:
    spelled = Path("/" + "srv/work/Gadget-Repo")  # how the app wrote the path
    projects = tmp_path / "projects"
    recs = [at(spelled, edit("s1", "e1", "2026-10-20T10:00:00Z", spelled, "src/z.py"))]
    f.write(projects / T.encode_path(spelled) / "s1.jsonl", recs)
    scan = T.scan_all(PROJECTS, projects)  # the config says gadget-repo, in lower case
    assert scan.files_by["gadget"] == 1
    assert [(t.project, t.rel) for t in scan.touches.values()] == [("gadget", "src/z.py")]


def test_a_copied_plan_is_held_by_both_sessions(tmp_path: Path) -> None:
    approval = f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# A plan")
    copy = [dict(r, sessionId="s2") for r in approval]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, approval), "s2": (f.ROOT, copy)})
    assert scan.plans["p1"].session == "s1"
    assert scan.holding("plan", "p1", "s1") == {"s1", "s2"}


def test_an_unchanged_file_is_not_read_again_and_a_grown_one_is(tmp_path: Path) -> None:
    projects = write(tmp_path, {"s1": (f.ROOT, [f.human("s1", "h1", "2026-10-20T10:00:00Z", "a")])})
    memo = T.FileMemo()
    T.scan_all(PROJECTS, projects, memo)
    T.scan_all(PROJECTS, projects, memo)
    assert memo.reads == 1
    path = projects / T.encode_path(f.ROOT) / "s1.jsonl"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f.dumps(f.human("s1", "h2", "2026-10-20T11:00:00Z", "b")) + "\n")
    scan = T.scan_all(PROJECTS, projects, memo)
    assert memo.reads == 2
    assert sorted(h.text for h in scan.humans.values()) == ["a", "b"]


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX modes, not root")
def test_a_failed_read_is_not_remembered(tmp_path: Path) -> None:
    projects = write(tmp_path, {"s1": (f.ROOT, [f.human("s1", "h1", "2026-10-20T10:00:00Z", "a")])})
    path = projects / T.encode_path(f.ROOT) / "s1.jsonl"
    memo = T.FileMemo()
    path.chmod(0)
    try:
        scan = T.scan_all(PROJECTS, projects, memo)
    finally:
        path.chmod(0o600)
    assert (scan.unreadable, scan.files) == (1, 0)
    scan = T.scan_all(PROJECTS, projects, memo)
    assert (scan.unreadable, scan.files, memo.reads) == (0, 1, 1)


# --- plans and PRs across continued sessions ------------------------------------------------


def test_a_pr_from_a_continued_session_joins_the_plan_copied_into_it(tmp_path: Path) -> None:
    approval = f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# Alpha Game: rewards")
    child = [
        *(dict(r, sessionId="s2") for r in approval),
        f.pr_link("s2", 7, "2026-10-21T10:00:00Z"),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, approval), "s2": (f.ROOT, child)})
    got = board(scan, "demo", [pr(7, "2026-10-21T09:00:00Z")])
    assert got["pr:acme/demo#7"].parent == "pl:p1"
    assert got["pl:p1"].column == D.IN_PROGRESS


def test_a_plan_the_parent_approved_after_the_fork_claims_no_pr_of_the_fork(
    tmp_path: Path,
) -> None:
    first = f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# First")
    later = f.plan_approval("s1", "2026-10-22T10:00:00Z", "p2", "# Second")
    fork = [*(dict(r, sessionId="s2") for r in first), f.pr_link("s2", 8, "2026-10-23T10:00:00Z")]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, [*first, *later]), "s2": (f.ROOT, fork)})
    got = board(scan, "demo", [pr(8, "2026-10-23T09:00:00Z")])
    assert got["pr:acme/demo#8"].parent == "pl:p1"


def test_a_plan_re_approved_in_a_fork_is_one_card(tmp_path: Path) -> None:
    first = f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# A plan", file="a.md")
    again = f.plan_approval("s2", "2026-10-21T10:00:00Z", "p1b", "# A plan", file="a.md")
    fork = [*(dict(r, sessionId="s2") for r in first), *again]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, first), "s2": (f.ROOT, fork)})
    plans = [c for c in board(scan, "demo").values() if c.kind == "plan"]
    assert [(c.id, c.source["approvals"]) for c in plans] == [("pl:p1", 2)]


def test_a_pr_starting_a_new_series_ends_the_plans_claim(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# Run 1"),
        *(f.pr_link("s1", n, f"2026-10-2{n}T12:00:00Z") for n in (1, 2, 3, 4)),
    ]
    prs = [
        pr(1, "2026-10-21T10:00:00Z", "PR 1 — the first"),
        pr(2, "2026-10-22T10:00:00Z"),
        pr(3, "2026-10-23T10:00:00Z", "PR 1 — next run"),
        pr(4, "2026-10-24T10:00:00Z"),
    ]
    got = board(scan_of(tmp_path, {"s1": (f.ROOT, recs)}), "demo", prs)
    assert [got[f"pr:acme/demo#{n}"].parent for n in (1, 2, 3, 4)] == ["pl:p1", "pl:p1", None, None]


def test_a_plan_in_a_repo_without_github_is_done_once_its_session_moves_on(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# First"),
        *f.plan_approval("s1", "2026-10-22T10:00:00Z", "p2", "# Second"),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, recs)})
    plain = board(scan, "demo", github=None)
    assert (plain["pl:p1"].column, plain["pl:p2"].column) == (D.DONE, D.IN_PROGRESS)
    hosted = board(scan, "demo")  # a GitHub repo waits for a PR
    assert (hosted["pl:p1"].column, hosted["pl:p2"].column) == (D.NEXT, D.NEXT)


# --- which board ---------------------------------------------------------------------------


def test_an_ask_with_no_evidence_sits_at_the_root_whatever_the_branch(tmp_path: Path) -> None:
    rec = f.assistant_text(
        "s1",
        "a1",
        "2026-10-26T10:00:00Z",
        f.needs_block("Do — plug in the Mac"),
        branch="game/alpha-sky",
    )
    got = board(scan_of(tmp_path, {"s1": (f.ROOT, [rec])}), "demo")
    assert (got["ny:a1:1"].sub, got["ny:a1:1"].sub_reason) == ("", "root")


def test_an_ask_follows_its_turns_files_but_not_the_since_plan_window(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval("s1", "2026-10-26T09:00:00Z", "p1", "# Work"),
        f.human("s1", "h1", "2026-10-26T09:30:00Z", "build the tool"),
        *(
            edit("s1", f"e{i}", f"2026-10-26T09:3{i}:00Z", GADGET, f"src/m{i}.py")
            for i in (1, 2, 3)
        ),
        f.assistant_text("s1", "a1", "2026-10-26T09:40:00Z", f.needs_block("Do — restart it")),
        f.human("s1", "h2", "2026-10-26T10:00:00Z", "now the game"),
        f.assistant_text("s1", "a2", "2026-10-26T10:05:00Z", f.needs_block("Do — check it")),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, recs)})
    demo, gadget = board(scan, "demo"), board(scan, "gadget")
    assert "ny:a1:1" in gadget and "ny:a1:1" not in demo  # its turn worked in gadget
    assert gadget["ny:a1:1"].source["routed"] == "turn"
    assert (
        "ny:a2:1" in demo and "ny:a2:1" not in gadget
    )  # the window since the plan does not move it


def test_an_ask_naming_another_project_goes_there(tmp_path: Path) -> None:
    rec = f.assistant_text(
        "s1", "a1", "2026-10-26T10:00:00Z", f.needs_block("Do — restart the gadget page")
    )
    scan = scan_of(tmp_path, {"s1": (f.ROOT, [rec])})
    assert "ny:a1:1" in board(scan, "gadget") and "ny:a1:1" not in board(scan, "demo")


def test_a_re_raise_and_its_wrap_up_follow_the_first_raise(tmp_path: Path) -> None:
    ask = {"recap": "Done.", "ask": "Do — hand check the Alpha Game night"}
    recs = [
        f.assistant_text(
            "s1",
            "a1",
            "2026-10-26T09:00:00Z",
            f.needs_block("Do — hand check the Alpha Game night"),
        ),
        f.human("s1", "h1", "2026-10-26T09:30:00Z", "ok, and the tool"),
        *(
            edit("s1", f"e{i}", f"2026-10-26T09:3{i}:00Z", GADGET, f"src/m{i}.py")
            for i in (1, 2, 3)
        ),
        f.assistant_text(
            "s1",
            "a2",
            "2026-10-26T09:40:00Z",
            f.needs_block("Do — hand check the Alpha Game night"),
        ),
        f.tool_use("s1", "w1", "2026-10-26T09:41:00Z", "mcp__ccd_turn__wrap_up", "t1", ask),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, recs)})
    demo, gadget = board(scan, "demo"), board(scan, "gadget")
    assert demo["ny:a1:1"].raised == 3
    assert not [c for c in gadget.values() if c.kind in ("needs", "wrap")]


def test_a_pr_in_another_configured_repo_gets_its_session_and_plan(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# The tool"),
        f.pr_link("s1", 3, "2026-10-21T10:00:00Z", repo="acme/gadget"),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, recs)})  # filed under demo
    gadget = board(scan, "gadget", [pr(3, "2026-10-21T09:00:00Z", repo="acme/gadget")])
    card = gadget["pr:acme/gadget#3"]
    assert (card.source["session"], card.parent) == ("s1", "pl:p1")
    assert gadget["pl:p1"].source["routed"] == "linked-prs"
    assert "pl:p1" not in board(scan, "demo")


def test_a_pr_whose_plan_is_on_another_board_names_it_without_a_parent(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# Mostly the tool"),
        f.pr_link("s1", 3, "2026-10-21T10:00:00Z", repo="acme/gadget"),
        f.pr_link("s1", 4, "2026-10-21T11:00:00Z", repo="acme/gadget"),
        f.pr_link("s1", 9, "2026-10-21T12:00:00Z"),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, recs)})
    card = board(scan, "demo", [pr(9, "2026-10-21T11:30:00Z")])["pr:acme/demo#9"]
    assert card.parent is None and card.source["plan"] == "pl:p1"
    assert "plan in gadget" in card.badges
    plan = board(
        scan,
        "gadget",
        [
            pr(3, "2026-10-21T09:00:00Z", repo="acme/gadget"),
            pr(4, "2026-10-21T10:30:00Z", repo="acme/gadget"),
        ],
    )["pl:p1"]
    assert "+1 in demo" in plan.badges


def test_an_answer_copied_into_a_fork_lands_on_the_plan_the_fork_approved(tmp_path: Path) -> None:
    q = {"questions": [{"question": "Which?"}]}
    asked = [
        f.tool_use("s1", "u1", "2026-10-20T09:00:00Z", "AskUserQuestion", "q1", q),
        f.tool_result("s1", "r1", "2026-10-20T09:01:00Z", "q1", "ok", {"answers": {"Which?": "B"}}),
    ]
    fork = [
        *(dict(r, sessionId="s2") for r in asked),
        *f.plan_approval("s2", "2026-10-21T10:00:00Z", "p2", "# The fork's plan"),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, asked), "s2": (f.ROOT, fork)})
    assert board(scan, "demo")["pl:p2"].decided == ["Which? → B"]


def test_a_copy_with_the_same_time_belongs_to_the_session_that_started_first(
    tmp_path: Path,
) -> None:
    ask = f.assistant_text("s1", "a1", "2026-10-26T10:00:00Z", f.needs_block("Do — check it"))
    original = [f.human("s1", "h0", "2026-10-25T09:00:00Z", "start"), ask]  # under gadget
    copy = [f.human("s2", "h9", "2026-10-26T09:00:00Z", "go on"), dict(ask, sessionId="s2")]
    # The continuation is filed under demo, whose folder is read first.
    scan = scan_of(tmp_path, {"s1": (GADGET, original), "s2": (f.ROOT, copy)})
    assert scan.needs[("a1", 1)].session == "s1"
    assert "ny:a1:1" in board(scan, "gadget") and "ny:a1:1" not in board(scan, "demo")


def test_a_game_named_in_an_ask_names_its_project(tmp_path: Path) -> None:
    games = Attributor.build("games", ["alpha"], {"alpha": "Alpha Game"}, {}, {})
    router = Router.build([("demo", "acme/demo"), ("gadget", "acme/gadget")], {"demo": games})
    assert router.by_name("Do — launch Alpha Game") == "demo"
    assert router.by_name("Do — launch Alpha Game and the gadget page") is None  # names two
    assert router.by_name("Do — plug in the Mac") is None


# --- PR C, after review ----------------------------------------------------------------------


def counted(scan: T.Scan, project: str) -> dict[str, int]:
    out: dict[str, int] = {}
    att = Attributor.build("games", ["alpha", "beta"], {"alpha": "Alpha Game"}, {}, {})
    D.derive(
        D.Inputs(
            project=project,
            github=None,
            attributor=att,
            backlog=[],
            roadmap=[],
            prs=[],
            squash={},
            scan=scan,
            now=NOW,
            router=ROUTER,
        ),
        counts=out,
    )
    return out


def test_a_continuation_holding_the_tail_of_a_run_is_one_plan(tmp_path: Path) -> None:
    run = [
        r
        for i in (1, 2, 3)
        for r in f.plan_approval("s1", f"2026-10-20T1{i}:00:00Z", f"a{i}", "# Rewards", "r.md")
    ]
    tail = [dict(r, sessionId="s2") for r in run[2:]]  # a2 and a3 only
    own = f.plan_approval("s2", "2026-10-21T10:00:00Z", "b1", "# Something else", "s.md")
    scan = scan_of(tmp_path, {"s1": (f.ROOT, run), "s2": (f.ROOT, [*tail, *own])})
    plans = {c.id: c for c in board(scan, "demo").values() if c.kind == "plan"}
    assert sorted(plans) == ["pl:a1", "pl:b1"] and plans["pl:a1"].source["approvals"] == 3
    shares = sum(counted(scan, p).get("plan_calls", 0) for p in ("demo", "gadget"))
    assert shares == len(scan.plans) == 4


def test_a_full_copy_belongs_to_the_session_whose_file_came_first(tmp_path: Path) -> None:
    first = f.human("s1", "h0", "2026-10-25T09:00:00Z", "start")
    ask = f.assistant_text("s1", "a1", "2026-10-26T10:00:00Z", f.needs_block("Do — check it"))
    write(tmp_path, {"s1": (GADGET, [first, ask])})  # the original, created first
    import time

    time.sleep(0.05)
    copy = [dict(first, sessionId="s2"), dict(ask, sessionId="s2")]  # its whole history
    scan = scan_of(tmp_path, {"s1": (GADGET, [first, ask]), "s2": (f.ROOT, copy)})
    assert scan.needs[("a1", 1)].session == "s1"
    assert "ny:a1:1" in board(scan, "gadget")


def test_a_forks_prs_vote_for_its_plan_after_the_parent_moved_on(tmp_path: Path) -> None:
    p1 = f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# The tool work")
    p2 = f.plan_approval("s1", "2026-10-21T10:00:00Z", "p2", "# Other work")
    fork = [
        *(dict(r, sessionId="s2") for r in p1),
        f.pr_link("s2", 3, "2026-10-22T10:00:00Z", repo="acme/gadget"),
        f.pr_link("s2", 4, "2026-10-22T11:00:00Z", repo="acme/gadget"),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, [*p1, *p2]), "s2": (f.ROOT, fork)})
    gadget = board(
        scan,
        "gadget",
        [
            pr(3, "2026-10-22T09:00:00Z", repo="acme/gadget"),
            pr(4, "2026-10-22T10:30:00Z", repo="acme/gadget"),
        ],
    )
    assert gadget["pr:acme/gadget#3"].parent == "pl:p1"
    assert gadget["pl:p1"].source["routed"] == "linked-prs"


def test_a_later_plans_prs_do_not_route_an_earlier_one(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# First"),
        *f.plan_approval("s1", "2026-10-21T10:00:00Z", "p2", "# Second"),
        f.pr_link("s1", 3, "2026-10-22T10:00:00Z", repo="acme/gadget"),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, recs)})
    assert "pl:p1" in board(scan, "demo") and "pl:p2" in board(scan, "gadget")


def test_a_plan_named_for_a_project_or_worked_there_goes_there(tmp_path: Path) -> None:
    named = f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# The gadget page")
    worked = [
        *f.plan_approval("s2", "2026-10-21T10:00:00Z", "p2", "# Untitled work"),
        *(
            edit("s2", f"e{i}", f"2026-10-21T10:1{i}:00Z", GADGET, f"src/m{i}.py")
            for i in (1, 2, 3)
        ),
    ]
    scan = scan_of(tmp_path, {"s1": (f.ROOT, named), "s2": (f.ROOT, worked)})
    gadget = board(scan, "gadget")
    assert (gadget["pl:p1"].source["routed"], gadget["pl:p2"].source["routed"]) == (
        "name",
        "plan-window",
    )


def test_a_record_with_no_session_lands_on_one_board_only(tmp_path: Path) -> None:
    rec = f.assistant_text("s1", "a1", "2026-10-26T10:00:00Z", f.needs_block("Do — x"))
    rec.pop("sessionId")
    scan = scan_of(tmp_path, {"s1": (GADGET, [rec])})
    on = [p for p in ("demo", "gadget") if "ny:a1:1" in board(scan, p)]
    assert len(on) == 1


def test_turn_routing_needs_three_touches_and_most_of_them() -> None:
    def t(project: str, i: int) -> T.Touch:
        return T.Touch(f"t{project}{i}", "s1", "2026-10-26T10:00:00Z", "x", project)

    assert ROUTER.by_touches([t("gadget", 1), t("gadget", 2)]) is None
    assert ROUTER.by_touches([t("gadget", i) for i in (1, 2, 3)]) == "gadget"
    assert (
        ROUTER.by_touches([*(t("gadget", i) for i in (1, 2, 3)), t("demo", 1), t("demo", 2)])
        == "gadget"
    )
    assert ROUTER.by_touches([t("gadget", 1), t("gadget", 2), t("demo", 1), t("demo", 2)]) is None


def test_a_project_is_named_by_whole_words_in_any_case() -> None:
    assert ROUTER.by_name("Do — restart the Gadget page") == "gadget"
    assert ROUTER.by_name("cd ~/work/gadget && run it") == "gadget"
    assert ROUTER.by_name("Do — gadgets everywhere") is None
    assert ROUTER.by_name("Do — the my-gadget folder") is None


def test_a_sibling_repos_files_are_not_the_projects() -> None:
    roots = T.Roots({"gadget": [str(GADGET)]})
    assert roots.owner(str(GADGET) + "-x/src/m.py") is None
    assert roots.owner(str(GADGET) + "/src/m.py") == ("gadget", "src/m.py")


def test_the_series_rule_also_knows_pr_0(tmp_path: Path) -> None:
    recs = [
        *f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# Run"),
        *(f.pr_link("s1", n, f"2026-10-2{n}T12:00:00Z") for n in (1, 2)),
    ]
    prs = [pr(1, "2026-10-21T10:00:00Z", "first"), pr(2, "2026-10-22T10:00:00Z", "PR 0 — next run")]
    got = board(scan_of(tmp_path, {"s1": (f.ROOT, recs)}), "demo", prs)
    assert (got["pr:acme/demo#1"].parent, got["pr:acme/demo#2"].parent) == ("pl:p1", None)


def test_a_quiet_session_finishes_a_plan_in_a_repo_without_github(tmp_path: Path) -> None:
    recs = f.plan_approval("s1", "2026-10-20T10:00:00Z", "p1", "# Only plan")
    scan = scan_of(tmp_path, {"s1": (f.ROOT, recs)})
    assert board(scan, "demo", github=None)["pl:p1"].column == D.IN_PROGRESS  # still active
    scan.session_mtime = dict.fromkeys(scan.session_mtime, NOW.timestamp() - 3 * 86400)
    assert board(scan, "demo", github=None)["pl:p1"].column == D.DONE


def test_a_deleted_transcript_is_forgotten_by_the_memo(tmp_path: Path) -> None:
    projects = write(tmp_path, {"s1": (f.ROOT, [f.human("s1", "h1", "2026-10-20T10:00:00Z", "a")])})
    memo = T.FileMemo()
    T.scan_all(PROJECTS, projects, memo)
    (projects / T.encode_path(f.ROOT) / "s1.jsonl").unlink()
    T.scan_all(PROJECTS, projects, memo)
    assert memo._items == {}
