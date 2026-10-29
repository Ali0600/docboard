"""The small sources: MILESTONES lines and hand checks on PR cards, session proposals as Ideas,
main's CI as a card, and the stanza for a folder no board reads."""

from __future__ import annotations

import json
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import factories as f
import pytest

from docboard import cli, docsrc, ghsrc, sync
from docboard import derive as D
from docboard import transcripts as T
from docboard.attribution import Attributor
from docboard.config import Config, Project, Subprojects
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


def test_a_hand_check_heading_may_say_it_in_other_words() -> None:
    body = "## Feel numbers for the owner's hand check\n\n- jump height 7\n\n## Done\n"
    assert D.hand_check(body) == "- jump height 7"
    assert D.hand_check("## Checks\n\nhand check in prose only\n") == ""


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
    url = f"https://github.com/acme/demo/run/{n}"
    return Run("acme/demo", n, workflow, "completed", conclusion, ts, url)


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
             "conclusion": "failure", "createdAt": "2026-10-26T00:00:00Z", "url": "u",
             "event": "push"}]  # fmt: skip
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
    conf.write_text(f"[[project]]\npath = '{tmp_path / 'other'}'\nname = \"other\"\n")
    assert cli.main(["board", "--config", str(conf), "--cwd", str(repo / "src"), "--no-sync"]) == 2
    stanza = capsys.readouterr().out.split("  as:\n", 1)[1]
    assert tomllib.loads(stanza) == {"project": [{"path": str(repo.resolve())}]}


def test_the_stanza_is_valid_toml_for_any_path() -> None:
    for path in ("D:\\code\\repo", "/srv/it's here", '/srv/"quoted"'):
        assert tomllib.loads(cli.stanza(Path(path))) == {"project": [{"path": str(Path(path))}]}


# --- PR F, after review ----------------------------------------------------------------------


def nothing_proposed(uid: str, tid: str, ts: str, title: str, text: str) -> list[dict]:
    args = {"initiation": "own_initiative", "title": title, "prompt": "x", "context": "fresh"}
    return [
        f.tool_use(S, uid, ts, "mcp__ccd_session__start_session", tid, args),
        f.tool_result(S, f"r-{uid}", ts, tid, text),
    ]


def ideas(got: dict[str, D.Card]) -> list[str]:
    return sorted(i for i, c in got.items() if c.kind == "proposal")


def test_a_start_session_that_proposed_nothing_makes_no_card(tmp_path: Path) -> None:
    recs = [
        *nothing_proposed("u1", "t1", "2026-10-25T10:00:00Z", "Remote one",
                          "Nothing was proposed. The user is working over Remote Control."),
        *nothing_proposed("u2", "t2", "2026-10-25T10:05:00Z", "Refused one",
                          "The user doesn't want to proceed with this tool use."),
        *proposal("u3", "t3", "2026-10-25T10:10:00Z", "A real one", "task_cc33"),
    ]  # fmt: skip
    assert ideas(derived(tmp_path, recs)) == ["ss:t3"]


def test_a_proposal_started_here_leaves(tmp_path: Path) -> None:
    recs = [
        *proposal("u1", "t1", "2026-10-25T10:00:00Z", "Trace the flake", "task_aa11"),
        *proposal("u2", "t2", "2026-10-25T10:01:00Z", "Speed up the specs", "task_bb22"),
        *proposal("u3", "t3", "2026-10-25T10:02:00Z", "Check the sky", "task_cc33"),
        f.human(S, "h1", "2026-10-25T11:00:00Z", "Do this task here: \u201cTrace the flake\u201d"),
        f.human(S, "h2", "2026-10-25T11:01:00Z", 'Do this task here: "Speed up the specs"'),
        # the same words from somewhere else, or before the proposal, start nothing
        f.human("other", "h3", "2026-10-25T11:02:00Z", 'Do this task here: "Check the sky"'),
        f.human(S, "h4", "2026-10-25T09:00:00Z", 'Do this task here: "Check the sky"'),
    ]
    assert ideas(derived(tmp_path, recs)) == ["ss:t3"]


def test_a_proposal_started_by_its_id_leaves(tmp_path: Path) -> None:
    start = f.tool_use(S, "u9", "2026-10-26T09:00:00Z", "mcp__ccd_session__start_session",
                       "t9", {"proposal_id": "task_aa11"})  # fmt: skip
    recs = [*proposal("u1", "t1", "2026-10-25T10:00:00Z", "Trace the flake", "task_aa11"), start]
    assert ideas(derived(tmp_path, recs)) == []


def test_a_proposal_expires_after_seven_days(tmp_path: Path) -> None:
    recs = [
        *proposal("u1", "t1", "2026-10-20T11:59:00Z", "Old", "task_aa11"),  # 7 days and a minute
        *proposal("u2", "t2", "2026-10-20T12:01:00Z", "Recent", "task_bb22"),
    ]
    assert ideas(derived(tmp_path, recs)) == ["ss:t2"]


def test_an_offer_is_not_an_idea(tmp_path: Path) -> None:
    args = {"initiation": "offer", "title": "Asked for", "prompt": "x", "context": "fresh"}
    recs = [
        f.tool_use(S, "u1", "2026-10-25T10:00:00Z", "mcp__ccd_session__start_session", "t1", args),
        f.tool_result(S, "r1", "2026-10-25T10:00:00Z", "t1",
                      'Proposed "Asked for" (task_id: task_aa11).'),
    ]  # fmt: skip
    assert ideas(derived(tmp_path, recs)) == []


def test_a_proposals_game_comes_from_its_title_not_a_path_deep_in_its_prompt(
    tmp_path: Path,
) -> None:
    prompt = "Shorten every contract.\n\nLater: one example is games/beta/CLAUDE.md."
    args = {"initiation": "own_initiative", "title": "Trim the agent contracts",
            "context": "fresh", "prompt": prompt}  # fmt: skip
    recs = [
        f.tool_use(S, "u1", "2026-10-25T10:00:00Z", "mcp__ccd_session__start_session", "t1", args),
        f.tool_result(S, "r1", "2026-10-25T10:00:00Z", "t1",
                      'Proposed "Trim the agent contracts" (task_id: task_aa11).'),
    ]  # fmt: skip
    card = derived(tmp_path, recs)["ss:t1"]
    assert (card.sub, card.sub_reason) == ("", "root")


def test_a_green_main_is_dated_when_it_turned_green(tmp_path: Path) -> None:
    runs = [run(5, "2026-10-27T09:00:00Z", "success"), run(4, "2026-10-26T15:00:00Z", "success"),
            run(3, "2026-10-26T12:30:00Z", "success"), run(2, "2026-10-26T10:50:00Z", "failure"),
            run(1, "2026-10-25T10:00:00Z", "success")]  # fmt: skip
    card = derived(tmp_path, runs=runs)["ci:acme/demo#ci"]
    assert (card.column, card.event_at) == (D.DONE, "2026-10-26T12:30:00Z")


def test_only_a_success_turns_main_green(tmp_path: Path) -> None:
    for newest in ("cancelled", "action_required", "neutral", "skipped", "stale"):
        runs = [run(3, "2026-10-26T12:00:00Z", newest), run(2, "2026-10-26T10:00:00Z", "failure"),
                run(1, "2026-10-25T10:00:00Z", "success")]  # fmt: skip
        card = derived(tmp_path / newest, runs=runs)["ci:acme/demo#ci"]
        assert card.column == D.IN_PROGRESS, newest


def test_a_red_streak_counts_only_the_newest_red_runs(tmp_path: Path) -> None:
    runs = [run(3, "2026-10-26T12:00:00Z", "failure"), run(2, "2026-10-26T10:00:00Z", "success"),
            run(1, "2026-10-25T10:00:00Z", "failure")]  # fmt: skip
    card = derived(tmp_path / "a", runs=runs)["ci:acme/demo#ci"]
    assert card.event_at == "2026-10-26T12:00:00Z" and "1 red runs" in card.badges
    shuffled = derived(tmp_path / "b", runs=[runs[1], runs[2], runs[0]])["ci:acme/demo#ci"]
    assert (shuffled.column, shuffled.event_at, shuffled.badges) == (
        card.column, card.event_at, card.badges)  # fmt: skip


def test_the_refused_badge_reads_only_the_newest_run(tmp_path: Path) -> None:
    red = [run(3, "2026-10-26T12:00:00Z", "failure"), run(2, "2026-10-26T10:00:00Z", "failure"),
           run(1, "2026-10-25T10:00:00Z", "success")]  # fmt: skip
    for refused in ({3: False}, {2: True}, {}):
        card = derived(tmp_path / str(len(refused)), runs=red, refused=refused)["ci:acme/demo#ci"]
        assert not any("refused" in b for b in card.badges), refused


def test_a_startup_failure_is_red(tmp_path: Path) -> None:
    runs = [run(2, "2026-10-26T12:00:00Z", "startup_failure"),
            run(1, "2026-10-25T10:00:00Z", "success")]  # fmt: skip
    assert derived(tmp_path, runs=runs)["ci:acme/demo#ci"].column == D.IN_PROGRESS


def gh_fake(tmp_path: Path, prs: list[dict] = (), runs: list[dict] = (),
            jobs: dict[str, list[dict]] | None = None, branch: str = "main",
            fail: tuple[str, ...] = (),
            hang: tuple[str, ...] = ()) -> tuple[Path, Path]:  # fmt: skip
    """A gh that answers like the real one: only the --json fields asked for, runs filtered by
    --branch, `repo view` with the default branch. Every call's argv goes to the log."""
    data = tmp_path / "gh-data.json"
    spec = {"prs": list(prs), "runs": list(runs), "jobs": jobs or {}, "branch": branch,
            "fail": list(fail), "hang": list(hang)}  # fmt: skip
    data.write_text(json.dumps(spec))
    log = tmp_path / "gh-argv.log"
    body = f"""
import json
d = json.load(open({str(data)!r}, encoding="utf-8"))
args = sys.argv[1:]
with open({str(log)!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(args) + "\\n")
what = " ".join(args[:2])
if what in d["hang"]:
    time.sleep(5)
if what in d["fail"]:
    print("boom", file=sys.stderr)
    sys.exit(4)
def keep(rows):
    keys = args[args.index("--json") + 1].split(",")
    return [{{k: r[k] for k in keys if k in r}} for r in rows]
if what == "pr list":
    print(json.dumps(keep(d["prs"])))
elif what == "run list":
    branch = args[args.index("--branch") + 1]
    event = args[args.index("--event") + 1] if "--event" in args else None
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else 20
    rows = [r for r in d["runs"] if r.get("headBranch", branch) == branch
            and event in (None, r.get("event"))]
    print(json.dumps(keep(rows[:limit])))
elif what == "run view":
    print(json.dumps({{"jobs": d["jobs"].get(args[2], [])}}))
elif what == "repo view":
    print(json.dumps({{"defaultBranchRef": {{"name": d["branch"]}}}}))
else:
    sys.exit(2)
"""
    return f.fake_exe(tmp_path / "bin", "gh", body), log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def gh_run(n: int, ts: str, conclusion: str, event: str = "push", branch: str = "main") -> dict:
    return {"databaseId": n, "workflowName": "ci", "status": "completed", "conclusion": conclusion,
            "createdAt": ts, "url": f"u{n}", "event": event, "headBranch": branch}  # fmt: skip


def test_refused_before_running_only_when_no_failed_job_ran(tmp_path: Path) -> None:
    ran = {"name": "a", "conclusion": "failure", "steps": [{"name": "Gate"}]}
    stepless = {"name": "b", "conclusion": "failure", "steps": []}
    green = {"name": "c", "conclusion": "success", "steps": []}
    jobs = {"1": [ran], "2": [stepless, ran], "3": [green], "4": [stepless, green]}
    gh, _ = gh_fake(tmp_path, jobs=jobs)
    got = {n: ghsrc.refused_before_running("acme/demo", str(gh), n) for n in (1, 2, 3, 4)}
    assert got == {1: False, 2: False, 3: False, 4: True}


def test_fetch_runs_keeps_only_the_branchs_own_runs(tmp_path: Path) -> None:
    rows = [gh_run(5, "2026-10-26T15:00:00Z", "success", "pull_request"),  # a fork's own main
            gh_run(4, "2026-10-26T14:00:00Z", "failure", "dynamic"),  # a Dependabot update
            gh_run(3, "2026-10-26T13:00:00Z", "failure", "schedule"),
            gh_run(2, "2026-10-26T12:00:00Z", "failure", "workflow_dispatch"),
            gh_run(1, "2026-10-26T11:00:00Z", "failure")]  # fmt: skip
    gh, _ = gh_fake(tmp_path, runs=rows)
    assert [r.id for r in ghsrc.fetch_runs("acme/demo", str(gh), "main")] == [3, 2, 1]
    with pytest.raises(ghsrc.GhError, match="owner/name"):
        ghsrc.fetch_runs("not a slug", str(gh), "main")


def git_env() -> dict[str, str]:
    import os

    who = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}  # fmt: skip
    return {"PATH": os.environ.get("PATH", ""), **who}


def gitc(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=git_env())


def ms_project(tmp_path: Path) -> tuple[Config, Project]:
    repo = tmp_path / "repo"
    files = {
        "docs/DECISIONS.md": "# Decisions\n",
        "docs/MILESTONES.md": "# Milestones\n\n- 2026-10-20: the root's line (#7).\n",
        "games/alpha/game.toml": 'name = "Alpha Game"\n',
        "games/alpha/docs/MILESTONES.md": "# Alpha\n\n- 2026-10-21: alpha's line (#8).\n",
    }
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        gitc(repo, *args)
    games = Subprojects(glob="games/*", name_from="game.toml:name")
    project = Project(
        name="demo", path=repo, docs_ref="HEAD", github="acme/demo", subprojects=games
    )
    return Config(projects=(project,), claude_home=tmp_path / "claude"), project


def gh_pr(n: int, body: str = "") -> dict:
    return {"number": n, "title": f"PR {n}", "state": "MERGED", "createdAt": "2026-10-20T00:00:00Z",
            "mergedAt": "2026-10-21T00:00:00Z", "closedAt": None, "headRefName": f"b{n}",
            "body": body}  # fmt: skip


def test_sync_reads_main_ci_milestones_and_hand_checks_through_gh(tmp_path: Path) -> None:
    cfg, project = ms_project(tmp_path)
    rows = [gh_run(32, "2026-10-26T13:00:00Z", "success", "pull_request", "trunk"),
            gh_run(31, "2026-10-26T12:00:00Z", "failure", branch="trunk"),
            gh_run(30, "2026-10-25T12:00:00Z", "success", branch="trunk"),
            gh_run(29, "2026-10-26T14:00:00Z", "success", branch="main")]  # fmt: skip
    jobs = {"31": [{"name": "ci", "conclusion": "failure", "steps": []}]}
    gh, log = gh_fake(tmp_path, prs=[gh_pr(7, HAND), gh_pr(8)], runs=rows, jobs=jobs,
                      branch="trunk")  # fmt: skip
    res = sync.collect(cfg, project, NOW, gh=str(gh))
    assert res.degraded == []
    assert any(
        c[:2] == ["run", "list"] and c[c.index("--branch") + 1] == "trunk" for c in calls(log)
    )  # the default branch gh names, not the docs ref
    cards = {c.id: c for c in res.cards}
    ci = cards["ci:acme/demo#ci"]
    assert ci.column == D.IN_PROGRESS and any("refused before it ran" in b for b in ci.badges)
    assert "hand check listed" in cards["pr:acme/demo#7"].badges
    assert "the root's line (#7)" in cards["pr:acme/demo#7"].body
    assert "alpha's line (#8)" in cards["pr:acme/demo#8"].body


def test_a_ci_outage_is_its_own_source_and_never_freezes_the_prs(tmp_path: Path) -> None:
    cfg, project = ms_project(tmp_path)
    gh, _ = gh_fake(tmp_path, prs=[gh_pr(7)], fail=("run list",))
    res = sync.collect(cfg, project, NOW, gh=str(gh))
    assert sync.ci_degraded(res.degraded) and not sync.gh_degraded(res.degraded)


def test_a_gh_that_does_not_answer_is_asked_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, project = ms_project(tmp_path)
    asked: list[list[str]] = []
    real = subprocess.run

    def hung(cmd: list[str], **kw: object) -> object:
        if cmd[0] != "fake-gh":
            return real(cmd, **kw)  # git
        asked.append(cmd[1:3])  # what a hung gh does, without timing a real process
        raise subprocess.TimeoutExpired(cmd, ghsrc.TIMEOUT)

    monkeypatch.setattr(subprocess, "run", hung)
    res = sync.collect(cfg, project, NOW, gh="fake-gh")
    assert sync.gh_degraded(res.degraded) and sync.ci_degraded(res.degraded)
    assert asked == [["pr", "list"]]


def test_no_gh_freezes_the_ci_cards_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg, project = ms_project(tmp_path)
    monkeypatch.setattr(ghsrc, "find_gh", lambda: None)
    res = sync.collect(cfg, project, NOW)
    assert sync.gh_degraded(res.degraded) and sync.ci_degraded(res.degraded)


def test_milestone_refs_skip_other_numbered_things() -> None:
    text = (
        "- 2026-10-01: game #2 and Game #74 shipped; visual review #24 done; issue #9 closed.\n"
        "- 2026-10-02: landed in PRs #108 and #110; then #175; after the #263 hand check, #178.\n"
    )
    refs = docsrc.milestone_refs(text, {2, 9, 24, 74, 108, 110, 175, 178, 263})
    assert sorted(refs) == [108, 110, 175, 178, 263]


def test_milestone_ranges_cite_known_ends_only_within_reason() -> None:
    known = set(range(150, 171)) - {170} | {1, 200}
    text = "- a: (#160-#170).\n- b: (#1-#200).\n- c: (#162\u2013#165).\n- d: (#167-#162).\n"
    refs = docsrc.milestone_refs(text, known)
    by_bullet = {b[0]: sorted(n for n, ls in refs.items() if any(x.startswith(b) for x in ls))
                 for b in ("a", "b", "c", "d")}  # fmt: skip
    assert by_bullet == {"a": [160], "b": [1, 200], "c": [162, 163, 164, 165], "d": [162, 167]}


def test_a_milestone_bullet_ends_at_a_heading_or_table_and_keeps_its_sub_bullets() -> None:
    text = (
        "- 2026-10-08: fixed (#162)\n## Next\n| M3 | #170 |\n"
        "- 2026-10-09: two parts (#163):\n  - the first (#164)\n"
    )
    refs = docsrc.milestone_refs(text, {162, 163, 164, 170})
    assert refs[162] == ["2026-10-08: fixed (#162)"]
    assert 170 not in refs
    assert refs[164] == refs[163] == ["2026-10-09: two parts (#163): - the first (#164)"]


def test_a_hand_check_section_skips_code_fences_and_keeps_its_subheadings() -> None:
    body = ("## Hand check\n\n```bash\n# open the place\ncd x\n```\n\n- Walk to the shed.\n\n"
            "### Steps\n\n- step one\n\n## Tests\n\n- all\n")  # fmt: skip
    section = D.hand_check(body)
    assert "Walk to the shed." in section and "### Steps" in section and "step one" in section
    assert "- all" not in section
    assert D.hand_check("## Summary\n\n```bash\n# hand check: run this\n```\n\n- x\n") == ""


def test_long_sections_and_bodies_are_capped(tmp_path: Path) -> None:
    big = "- step " + "x" * 20000 + "\n"
    assert len(D.hand_check("## Hand check\n\n" + big)) <= D.BODY_CAP
    refs = docsrc.milestone_refs("- 2026-10-01: " + "y" * 20000 + " (#74)\n", {74})
    assert len(refs[74][0]) <= D.BODY_CAP
    card = derived(tmp_path, prs=[pr(74, "## Hand check\n\n" + big)],
                   milestones={74: ["z" * 20000]})["pr:acme/demo#74"]  # fmt: skip
    assert len(card.body) <= D.BODY_CAP


@pytest.mark.parametrize(
    "path",
    ["D:\\code\\repo", "/srv/it's here", '/srv/"quoted"', "/srv/O'Brien/\U0001f600 repo",
     "/srv/new\nline", "/srv/del\x7fete", "/srv/tab\there"],
)  # fmt: skip
def test_the_stanza_is_valid_toml(path: str) -> None:
    assert tomllib.loads(cli.stanza(Path(path))) == {"project": [{"path": str(Path(path))}]}


def test_a_windows_stanza_reads_as_written() -> None:
    assert cli.stanza(Path("D:\\code\\repo")).endswith("path = 'D:\\code\\repo'")


def test_the_stanza_names_the_main_checkout_from_a_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "loose"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "x"],
                 ["worktree", "add", "-q", str(tmp_path / "wt")]):  # fmt: skip
        gitc(repo, *args)
    conf = tmp_path / "p.toml"
    conf.write_text(f"[[project]]\npath = '{tmp_path / 'other'}'\nname = \"other\"\n")
    assert (
        cli.main(["board", "--config", str(conf), "--cwd", str(tmp_path / "wt"), "--no-sync"]) == 2
    )
    stanza = capsys.readouterr().out.split("  as:\n", 1)[1]
    assert tomllib.loads(stanza) == {"project": [{"path": str(repo.resolve())}]}


# --- PR F, after the fix's own verification ---------------------------------------------------


def test_do_it_here_matches_the_title_as_the_app_writes_it(tmp_path: Path) -> None:
    long = "Rebuild the board " + "x" * 240  # the transcript keeps 200 characters of it
    recs = [
        *proposal("u1", "t1", "2026-10-25T10:00:00Z", 'Fix the "Hand check" parser', "task_aa11"),
        *proposal("u2", "t2", "2026-10-25T10:01:00Z", "Keep the ‘sky’ lit", "task_bb22"),
        *proposal("u3", "t3", "2026-10-25T10:02:00Z", long, "task_cc33"),
        *proposal("u4", "t4", "2026-10-25T10:03:00Z", "Still waiting", "task_dd44"),
        # the app maps every quotation mark to ' and cuts a title at 200 characters with an ellipsis
        f.human(S, "h1", "2026-10-25T11:00:00Z",
                "Do this task here: “Fix the 'Hand check' parser”"),
        f.human(S, "h2", "2026-10-25T11:01:00Z", "Do this task here: “Keep the 'sky' lit”"),
        f.human(S, "h3", "2026-10-25T11:02:00Z",
                "Do this task here: “" + long[:200] + "…”\n\nand then more"),
        # a short title cut by hand is no cut of the app's: it starts nothing
        f.human(S, "h4", "2026-10-25T11:03:00Z", "Do this task here: “Still…”"),
    ]  # fmt: skip
    assert ideas(derived(tmp_path, recs)) == ["ss:t4"]


def test_a_refused_start_by_id_leaves_the_proposal_standing(tmp_path: Path) -> None:
    start = f.tool_use(S, "u9", "2026-10-26T09:00:00Z", "mcp__ccd_session__start_session",
                       "t9", {"proposal_id": "task_aa11"})  # fmt: skip
    refused = f.tool_result(S, "r9", "2026-10-26T09:00:01Z", "t9",
                            "The user doesn't want to proceed with this tool use.")  # fmt: skip
    recs = [*proposal("u1", "t1", "2026-10-25T10:00:00Z", "Trace the flake", "task_aa11"),
            start, refused]  # fmt: skip
    assert ideas(derived(tmp_path, recs)) == ["ss:t1"]


def test_a_proposal_exactly_a_week_old_still_stands(tmp_path: Path) -> None:
    recs = proposal("u1", "t1", "2026-10-20T12:00:00Z", "Old", "task_aa11")
    assert ideas(derived(tmp_path, recs)) == ["ss:t1"]


def test_a_proposals_lead_is_its_whole_first_paragraph(tmp_path: Path) -> None:
    prompt = "Speed up the specs\nof the Alpha Game.\n\nLater: nothing."
    args = {"initiation": "own_initiative", "title": "Speed up the specs",
            "context": "fresh", "prompt": prompt}  # fmt: skip
    recs = [
        f.tool_use(S, "u1", "2026-10-25T10:00:00Z", "mcp__ccd_session__start_session", "t1", args),
        f.tool_result(S, "r1", "2026-10-25T10:00:00Z", "t1",
                      'Proposed "Speed up the specs" (task_id: task_aa11).'),
    ]  # fmt: skip
    assert derived(tmp_path, recs)["ss:t1"].sub == "alpha"


def test_a_hand_check_follows_commonmark_fences() -> None:
    inline = "## Summary\n```lune run test``` is green.\n\n## Hand check\n\n- Walk to the shed.\n"
    assert D.hand_check(inline) == "- Walk to the shed."
    tilde = "## Hand check\n\n~~~bash\n# open the place\n~~~\n\n- Walk to the shed.\n\n## Tests\n"
    assert "Walk to the shed." in D.hand_check(tilde)
    indented = (
        "## Hand check\n\n1. Run:\n   ```bash\n# open the place\n   ```\n2. Walk to the shed.\n"
    )
    assert "Walk to the shed." in D.hand_check(indented)
    tight = "## Hand check\n\n```bash\ncd x\n```\n## Tests\n\n- all green\n"
    section = D.hand_check(tight)
    assert "cd x" in section and "all green" not in section
    nested = ("## Hand check\n\n````md\n```\n# not a heading\n```\n````\n\n- step\n\n"
              "```\n~~~\n# still code\n```\n\n- last\n\n## Tests\n- no\n")  # fmt: skip
    section = D.hand_check(nested)
    assert "- step" in section and "- last" in section and "- no" not in section


def test_red_since_dates_the_streak_s_first_red_run(tmp_path: Path) -> None:
    runs = [run(3, "2026-10-27T09:00:00Z", "failure"), run(2, "2026-10-25T10:00:00Z", "timed_out"),
            run(1, "2026-10-24T10:00:00Z", "success")]  # fmt: skip
    card = derived(tmp_path, runs=runs)["ci:acme/demo#ci"]
    assert card.column == D.IN_PROGRESS and "red since 2026-10-25" in card.badges


def test_runs_of_one_second_are_ordered_by_id(tmp_path: Path) -> None:
    same = "2026-10-26T12:00:00Z"
    runs = [
        run(5, same, "failure"),
        run(6, same, "success"),
        run(1, "2026-10-25T10:00:00Z", "failure"),
    ]
    assert derived(tmp_path, runs=runs)["ci:acme/demo#ci"].column == D.DONE


def test_newest_red_looks_past_runs_still_going_and_skips_a_green_newest() -> None:
    def r(n: int, wf: str, status: str, conclusion: str, ts: str) -> Run:
        return Run("acme/demo", n, wf, status, conclusion, ts, f"u{n}", "push")

    runs = [r(33, "ci", "in_progress", "", "2026-10-27T10:00:00Z"),
            r(31, "ci", "completed", "failure", "2026-10-26T10:00:00Z"),
            r(40, "docs", "completed", "success", "2026-10-27T10:00:00Z"),
            r(39, "docs", "completed", "failure", "2026-10-26T10:00:00Z")]  # fmt: skip
    assert [x.id for x in sync._newest_red(runs)] == [31]


def test_fork_runs_cannot_crowd_main_out_of_the_window(tmp_path: Path) -> None:
    cfg, project = ms_project(tmp_path)
    at = "2026-10-27T{:02d}:{:02d}:00Z"
    forks = [
        gh_run(100 + i, at.format(i // 60, i % 60), "success", "pull_request") for i in range(60)
    ]  # a stranger's PR from their own main, pushed 60 times
    rows = [*forks, gh_run(31, "2026-10-26T12:00:00Z", "failure"),
            gh_run(30, "2026-10-25T12:00:00Z", "success")]  # fmt: skip
    gh, _ = gh_fake(tmp_path, prs=[gh_pr(7)], runs=rows, jobs={"31": []})
    res = sync.collect(cfg, project, NOW, gh=str(gh))
    assert {c.id: c.column for c in res.cards}.get("ci:acme/demo#ci") == D.IN_PROGRESS


def test_the_default_branch_is_asked_again_after_an_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(ghsrc, "_clock", lambda: clock[0])
    gh, log = gh_fake(tmp_path, branch="main")
    assert ghsrc.default_branch("acme/demo", str(gh)) == "main"
    data = tmp_path / "gh-data.json"
    data.write_text(data.read_text().replace('"branch": "main"', '"branch": "trunk"'))
    assert ghsrc.default_branch("acme/demo", str(gh)) == "main"  # asked once within the hour
    assert [c[:2] for c in calls(log)] == [["repo", "view"]]
    clock[0] += ghsrc.BRANCH_TTL + 1
    assert ghsrc.default_branch("acme/demo", str(gh)) == "trunk"


def test_no_default_branch_name_is_an_error(tmp_path: Path) -> None:
    gh, _ = gh_fake(tmp_path, branch="")
    with pytest.raises(ghsrc.GhError, match="default branch"):
        ghsrc.default_branch("acme/demo", str(gh))


def test_a_pr_list_error_still_reads_main_ci(tmp_path: Path) -> None:
    cfg, project = ms_project(tmp_path)
    rows = [
        gh_run(31, "2026-10-26T12:00:00Z", "failure"),
        gh_run(30, "2026-10-25T12:00:00Z", "success"),
    ]
    gh, _ = gh_fake(tmp_path, runs=rows, jobs={"31": []}, fail=("pr list",))
    res = sync.collect(cfg, project, NOW, gh=str(gh))
    assert sync.gh_degraded(res.degraded) and not sync.ci_degraded(res.degraded)
    assert "ci:acme/demo#ci" in {c.id for c in res.cards}


def test_a_gh_that_cannot_run_degrades_both_sources(tmp_path: Path) -> None:
    cfg, project = ms_project(tmp_path)
    gh = tmp_path / "gh"
    gh.write_text("not a program\n")  # there, but not something the OS can run
    res = sync.collect(cfg, project, NOW, gh=str(gh))
    assert sync.gh_degraded(res.degraded) and sync.ci_degraded(res.degraded)


def test_a_range_whose_first_end_is_no_pr_is_not_filled_in() -> None:
    refs = docsrc.milestone_refs("- e: (#149-#152).\n", set(range(150, 171)))
    assert sorted(refs) == [152]


def test_a_hash_inside_a_word_or_a_url_is_no_pr() -> None:
    refs = docsrc.milestone_refs(
        "- x: see https://h.example/#74 and build v2#75; ok (#76).\n", {74, 75, 76}
    )
    assert sorted(refs) == [76]


def test_no_run_link_without_a_github_repo(tmp_path: Path) -> None:
    from docboard import web

    links = web._source_links(Project(name="d", path=tmp_path), None, {"run_id": 7})
    assert "run" not in links


def test_a_tab_keeps_the_literal_form() -> None:
    path = Path("/srv/tab\there")  # spelled with the OS's own separator
    assert cli.stanza(path) == f"[[project]]\npath = '{path}'"
