"""The small sources: MILESTONES lines and hand checks on PR cards, session proposals as Ideas,
main's CI as a card, and the stanza for a folder no board reads."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import factories as f
import pytest

from docboard import cli, docsrc, ghsrc
from docboard import derive as D
from docboard import transcripts as T
from docboard.attribution import Attributor
from docboard.ghsrc import PullRequest, Run

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
S = "sess-1"

MILESTONES = """# Milestones

| Milestone | Status |
|---|---|
| M2 — game #1 public | in progress (#40) |

## Log

- 2026-10-02 ([PR #74](https://github.com/acme/demo/pull/74)): a field interface.
- 2026-10-06: Shared libs, part 1 (#134): codes and the board,
  and their tests (#135).
- 2026-10-08: thirty findings fixed (#162-#165).
- Not a PR: game #2 and issue #999.
"""


def test_milestone_lines_cite_known_prs_only() -> None:
    known = {74, 134, 135, 162, 163, 164, 165, 40}
    refs = docsrc.milestone_refs(MILESTONES, known)
    assert sorted(refs) == [74, 134, 135, 162, 163, 164, 165]  # not the table's #40
    assert refs[135] == refs[134]  # one bullet, over two lines
    assert "and their tests (#135)" in refs[134][0]
    assert refs[163][0].startswith("2026-10-08")


def att() -> Attributor:
    return Attributor.build("games", ["alpha", "beta"], {"alpha": "Alpha Game"}, {}, {})


def pr(n: int, body: str = "", state: str = "merged") -> PullRequest:
    return PullRequest("acme/demo", n, "work", state, "2026-10-20T10:00:00Z",
                       "2026-10-20T11:00:00Z" if state == "merged" else None, None,
                       f"b-{n}", body=body)  # fmt: skip


def derived(tmp_path: Path, records: list[dict] = (), prs: list[PullRequest] = (),
            milestones: dict[int, list[str]] | None = None, runs: list[Run] = (),
            refused: dict[int, bool] | None = None) -> dict[str, D.Card]:  # fmt: skip
    proj = tmp_path / "projects" / T.encode_path(f.ROOT)
    f.write(proj / f"{S}.jsonl", list(records))
    scan = T.scan_project(f.ROOT, tmp_path / "projects")
    cards, _ = D.derive(
        D.Inputs(project="demo", github="acme/demo", attributor=att(), backlog=[], roadmap=[],
                 prs=list(prs), squash={}, scan=scan, now=NOW, milestones=milestones or {},
                 runs=list(runs), refused_runs=refused or {})
    )  # fmt: skip
    return {c.id: c for c in cards}


HAND = "Summary.\n\n## Hand check\n\n- Play the night in Studio.\n\n## Tests\n\n- all green\n"


def test_a_pr_card_carries_its_milestone_lines_and_its_hand_check(tmp_path: Path) -> None:
    got = derived(tmp_path, prs=[pr(74, HAND), pr(75)],
                  milestones={74: ["2026-10-02: a field interface."]})  # fmt: skip
    card = got["pr:acme/demo#74"]
    assert "hand check listed" in card.badges
    assert "2026-10-02: a field interface." in card.body
    assert "Play the night in Studio." in card.body and "all green" not in card.body
    plain = got["pr:acme/demo#75"]
    assert plain.body == "" and "hand check listed" not in plain.badges


def proposal(uid: str, tid: str, ts: str, title: str, task: str) -> list[dict]:
    args = {"initiation": "own_initiative", "title": title, "prompt": f"Do {title}.",
            "context": "fresh"}  # fmt: skip
    text = f'Proposed "{title}" (task_id: {task}). The user sees a card and nothing runs.'
    return [
        f.tool_use(S, uid, ts, "mcp__ccd_session__start_session", tid, args),
        f.tool_result(S, f"r-{uid}", ts, tid, text),
    ]


def test_session_proposals_are_ideas_until_dismissed(tmp_path: Path) -> None:
    recs = [
        *proposal("u1", "t1", "2026-10-25T10:00:00Z", "Speed up the Alpha Game specs", "task_aa11"),
        *proposal("u2", "t2", "2026-10-25T11:00:00Z", "Trace a flaky scenario", "task_bb22"),
        f.tool_use(
            S,
            "u3",
            "2026-10-26T09:00:00Z",
            "mcp__ccd_session__dismiss_task",
            "t3",
            {"task_id": "task_bb22", "reason": "fixed here"},
        ),  # fmt: skip
    ]
    got = derived(tmp_path, recs)
    ideas = {c.id: c for c in got.values() if c.kind == "proposal"}
    assert list(ideas) == ["ss:t1"]
    card = ideas["ss:t1"]
    assert (card.column, card.sub, card.title) == (
        D.IDEAS,
        "alpha",
        "Speed up the Alpha Game specs",
    )


def run(n: int, ts: str, conclusion: str, workflow: str = "ci") -> Run:
    return Run(repo="acme/demo", id=n, workflow=workflow, status="completed",
               conclusion=conclusion, created_at=ts, url=f"https://github.com/acme/demo/run/{n}")  # fmt: skip


def test_a_red_main_is_a_card_until_it_is_green_again(tmp_path: Path) -> None:
    red = [run(3, "2026-10-26T12:00:00Z", "failure"), run(2, "2026-10-26T10:00:00Z", "failure"),
           run(1, "2026-10-25T10:00:00Z", "success")]  # fmt: skip
    card = derived(tmp_path, runs=red, refused={3: False})["ci:acme/demo#ci"]
    assert card.column == D.IN_PROGRESS and card.event_at == "2026-10-26T10:00:00Z"
    green = [run(4, "2026-10-27T09:00:00Z", "success"), *red]
    again = derived(tmp_path, runs=green)["ci:acme/demo#ci"]
    assert again.column == D.DONE and again.event_at == "2026-10-27T09:00:00Z"
    quiet = derived(tmp_path, runs=[run(1, "2026-10-25T10:00:00Z", "success")])
    assert "ci:acme/demo#ci" not in quiet  # never red in the window: no card


def test_a_run_refused_before_it_ran_says_so(tmp_path: Path) -> None:
    red = [run(3, "2026-10-26T12:00:00Z", "failure"), run(1, "2026-10-25T10:00:00Z", "success")]
    card = derived(tmp_path, runs=red, refused={3: True})["ci:acme/demo#ci"]
    assert any("refused before it ran" in b for b in card.badges)


def test_runs_and_bodies_come_from_gh(tmp_path: Path) -> None:
    prs = [{"number": 7, "title": "x", "state": "MERGED", "createdAt": "2026-10-20T00:00:00Z",
            "mergedAt": "2026-10-21T00:00:00Z", "closedAt": None, "headRefName": "b",
            "body": HAND}]  # fmt: skip
    runs = [{"databaseId": 9, "workflowName": "ci", "status": "completed",
             "conclusion": "failure", "createdAt": "2026-10-26T00:00:00Z", "url": "u"}]  # fmt: skip
    jobs = {"jobs": [{"name": "ci", "conclusion": "failure", "steps": []}]}
    body = (
        "args = sys.argv[1:]\n"
        f"if args[:2] == ['pr', 'list']: print({json.dumps(prs)!r})\n"
        f"elif args[:2] == ['run', 'list']: print({json.dumps(runs)!r})\n"
        f"elif args[:2] == ['run', 'view']: print({json.dumps(jobs)!r})\n"
        "else: sys.exit(2)\n"
    )
    gh = f.fake_exe(tmp_path, "gh", body)
    (pr7,) = ghsrc.fetch("acme/demo", str(gh))
    assert "Hand check" in pr7.body
    (r,) = ghsrc.fetch_runs("acme/demo", str(gh), "main")
    assert (r.id, r.workflow, r.conclusion) == (9, "ci", "failure")
    assert ghsrc.refused_before_running("acme/demo", str(gh), 9) is True


def test_board_in_an_unconfigured_folder_prints_a_stanza(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "loose"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    conf = tmp_path / "p.toml"
    conf.write_text(f'[[project]]\npath = "{tmp_path / "other"}"\nname = "other"\n')
    assert cli.main(["board", "--config", str(conf), "--cwd", str(repo / "src"), "--no-sync"]) == 2
    out = capsys.readouterr().out
    assert f'[[project]]\npath = "{repo.resolve()}"' in out
