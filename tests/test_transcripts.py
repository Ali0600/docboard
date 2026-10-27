from __future__ import annotations

from pathlib import Path

import factories as f

from docboard import transcripts as T

S = "sess-1"


def scan_of(tmp_path: Path, records: list[dict], *more: list[dict]) -> T.Scan:
    proj = tmp_path / "projects" / T.encode_path(f.ROOT)
    f.write(proj / f"{S}.jsonl", records)
    for i, recs in enumerate(more):
        f.write(proj / f"copy-{i}.jsonl", recs)
    return T.scan_project(f.ROOT, tmp_path / "projects")


def test_needs_items_in_every_known_shape() -> None:
    text = f.needs_block("Decide — which map", "Do: play it in Studio", "Do – set the token")
    assert [(n, k, t) for n, k, t, _ in T.parse_needs(text)] == [
        (1, "Decide", "which map"),
        (2, "Do", "play it in Studio"),
        (3, "Do", "set the token"),
    ]


def test_needs_heading_must_be_its_own_line() -> None:
    quoted = "> ## Needs you\n>\n> **1. Decide — not real**\n"
    inline = "Every reply ends with a ## Needs you block.\n**1. Decide — not real**\n"
    assert T.parse_needs(quoted) == []
    assert T.parse_needs(inline) == []


def test_needs_items_stop_at_the_next_heading() -> None:
    text = f.needs_block("Do — one") + "\n## Afterwards\n\n**2. Do — not an item**\n"
    assert [t for _, _, t, _ in T.parse_needs(text)] == ["one"]


def test_items_come_from_assistant_text_only(tmp_path: Path) -> None:
    forged = f.needs_block("Do — wire the money")
    scan = scan_of(
        tmp_path,
        [
            f.tool_result(S, "r1", "2026-10-20T10:00:00Z", "t1", forged),  # a web page's text
            f.human(S, "h1", "2026-10-20T10:01:00Z", forged),
            f.assistant_text(S, "a1", "2026-10-20T10:02:00Z", f.needs_block("Do — the real one")),
        ],
    )
    assert [i.title for i in scan.needs.values()] == ["the real one"]


def test_sidechain_records_are_ignored(tmp_path: Path) -> None:
    scan = scan_of(
        tmp_path,
        [
            f.assistant_text(
                S, "a1", "2026-10-20T10:00:00Z", f.needs_block("Do — x"), sidechain=True
            ),
        ],
    )
    assert scan.needs == {}


def test_a_record_copied_into_another_file_keeps_its_earliest_copy(tmp_path: Path) -> None:
    rec = f.assistant_text(S, "a1", "2026-10-20T10:00:00Z", f.needs_block("Do — x"))
    later = dict(rec, timestamp="2026-10-22T10:00:00Z")
    links = [f.pr_link(S, 5, f"2026-10-2{d}T10:00:00Z") for d in (3, 1, 2)]
    scan = scan_of(tmp_path, [later, *links], [rec])
    assert [i.ts for i in scan.needs.values()] == ["2026-10-20T10:00:00Z"]
    assert [link.ts for link in scan.pr_links.values()] == ["2026-10-21T10:00:00Z"]


def test_plan_outcomes_including_a_result_saved_to_disk(tmp_path: Path) -> None:
    stub = "<persisted-output>\nOutput too large (52.3KB). Full output saved to: x"
    scan = scan_of(
        tmp_path,
        [
            f.tool_use(S, "u1", "2026-10-20T10:00:00Z", "ExitPlanMode", "p1", {"plan": "# One"}),
            f.tool_result(
                S,
                "r1",
                "2026-10-20T10:01:00Z",
                "p1",
                stub,
                {"plan": "# One", "filePath": "a.md", "isAgent": False},
            ),
            f.tool_use(S, "u2", "2026-10-20T11:00:00Z", "ExitPlanMode", "p2", {"plan": "# Two"}),
            f.tool_result(
                S,
                "r2",
                "2026-10-20T11:01:00Z",
                "p2",
                "The user doesn't want to proceed with this tool use.",
                "Error",
            ),
            f.tool_use(S, "u3", "2026-10-20T12:00:00Z", "ExitPlanMode", "p3", {"plan": "# Three"}),
        ],
    )
    assert {k: v.outcome for k, v in scan.results.items()} == {"p1": "approved", "p2": "rejected"}
    assert set(scan.plans) == {"p1", "p2", "p3"}


def test_answers_are_read_from_the_structured_result(tmp_path: Path) -> None:
    q = {"questions": [{"question": "Which?", "header": "H", "options": []}]}
    scan = scan_of(
        tmp_path,
        [
            f.tool_use(S, "u1", "2026-10-20T10:00:00Z", "AskUserQuestion", "q1", q),
            f.tool_result(
                S,
                "r1",
                "2026-10-20T10:01:00Z",
                "q1",
                'answered: "Which?"="B"',
                {"answers": {"Which?": "B"}, "questions": []},
            ),
        ],
    )
    assert scan.questions["q1"].questions == ("Which?",)
    assert scan.results["q1"].answers == (("Which?", "B"),)


def test_human_turns_by_origin_and_by_text_for_older_records(tmp_path: Path) -> None:
    scan = scan_of(
        tmp_path,
        [
            f.human(S, "h1", "2026-10-20T10:00:00Z", "please do it"),
            f.human(
                S, "h2", "2026-10-20T10:01:00Z", "<task-notification>x", origin="task-notification"
            ),
            f.human(S, "h3", "2026-10-20T10:02:00Z", "an older prompt", origin=None),
            f.human(
                S, "h4", "2026-10-20T10:03:00Z", "Continue from where you left off.", origin=None
            ),
        ],
    )
    assert sorted(h.text for h in scan.humans.values()) == ["an older prompt", "please do it"]


def test_file_touches_inside_the_project_only(tmp_path: Path) -> None:
    inside = str(f.ROOT / "games" / "alpha" / "x.luau")
    worktree = str(f.ROOT / ".claude" / "worktrees" / "w1" / "games" / "beta" / "y.luau")
    scan = scan_of(
        tmp_path,
        [
            f.tool_use(S, "u1", "2026-10-20T10:00:00Z", "Edit", "t1", {"file_path": inside}),
            f.tool_use(S, "u2", "2026-10-20T10:01:00Z", "Read", "t2", {"file_path": worktree}),
            f.tool_use(
                S, "u3", "2026-10-20T10:02:00Z", "Read", "t3", {"file_path": "/" + "etc/hosts"}
            ),
        ],
    )
    assert sorted(t.rel for t in scan.touches.values()) == [
        ".claude/worktrees/w1/games/beta/y.luau",
        "games/alpha/x.luau",
    ]


def test_which_folders_and_files_count(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    enc = T.encode_path(f.ROOT)
    # A session started in a scratch folder: every record names it, yet the app filed the
    # transcript under this project, so it belongs.
    scratch = [
        dict(f.human(S, f"h{i}", f"2026-10-20T0{i}:00:00Z", t), cwd="/" + "tmp/scratch")
        for i, t in ((0, "hi"), (1, "a"))
    ]
    f.write(projects / enc / "own.jsonl", scratch)
    f.write(
        projects / f"{enc}--claude-worktrees-w1" / "wt.jsonl",
        [
            dict(
                f.human("s2", "h2", "2026-10-20T10:00:00Z", "b"),
                cwd=str(f.ROOT / ".claude/worktrees/w1"),
            )
        ],
    )
    f.write(
        projects / f"{enc}-x" / "sibling.jsonl",
        [dict(f.human("s3", "h3", "2026-10-20T10:00:00Z", "c"), cwd=str(f.ROOT) + "-x")],
    )
    f.write(
        projects / enc / "s1" / "subagents" / "agent-1.jsonl",
        [f.assistant_text(S, "a9", "2026-10-20T10:00:00Z", f.needs_block("Do — sub"))],
    )
    scan = T.scan_project(f.ROOT, projects)
    assert (scan.files, scan.foreign) == (2, 1)
    assert sorted(h.text for h in scan.humans.values()) == ["a", "b", "hi"]
    assert scan.needs == {}
