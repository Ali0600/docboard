"""Pull requests through the gh CLI (its own auth; docboard holds no token)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_SLUG = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
FIELDS = "number,title,state,createdAt,mergedAt,closedAt,headRefName,body"
RUN_FIELDS = "databaseId,workflowName,status,conclusion,createdAt,url,event"
RED = frozenset({"failure", "startup_failure", "timed_out"})
# The events whose runs test the branch's own commits. `gh run list --branch` matches a run's
# head branch, so it also lists a fork's pull request from its own `main` (pull_request) and
# Dependabot's update jobs (dynamic): neither says whether the default branch is red.
# Asked for one event at a time, so a flood of a fork's runs cannot push the branch's own out of
# the window.
BRANCH_EVENTS = ("push", "schedule", "workflow_dispatch", "repository_dispatch")
BRANCH_TTL = 3600.0  # seconds a default branch is trusted before gh is asked again
BODY_CAP = 65536
TIMEOUT = 120.0  # seconds; a hung gh (a login prompt, a dead network) degrades the sync


class GhError(RuntimeError):
    pass


class GhUnreachable(GhError):
    """gh did not answer (it hung on a prompt or a dead network): asking it again this sync
    would only wait as long again."""


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    state: str  # open | merged | closed
    created_at: str
    merged_at: str | None
    closed_at: str | None
    head_ref: str
    body: str = ""

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


@dataclass(frozen=True)
class Run:
    """One workflow run on the default branch."""

    repo: str
    id: int
    workflow: str
    status: str  # queued | in_progress | completed
    conclusion: str  # success | failure | cancelled | skipped | startup_failure | "" ...
    created_at: str
    url: str
    event: str = ""


def decisive(runs: Iterable[Run]) -> list[Run]:
    """The finished runs that say red or green (a success or a RED conclusion; a cancelled,
    skipped or approval-waiting run says neither), newest first, ties broken by id. The sync and
    the CI card read runs in this one order, so both name the same newest run."""
    says = RED | {"success"}
    keep = [r for r in runs if r.status == "completed" and r.conclusion in says]
    return sorted(keep, key=lambda r: (r.created_at, r.id), reverse=True)


def find_gh() -> str | None:
    found = shutil.which("gh") or (
        "/opt/homebrew/bin/gh" if Path("/opt/homebrew/bin/gh").is_file() else None
    )
    return str(Path(found).resolve()) if found else None


def parse(repo: str, payload: str) -> list[PullRequest]:
    rows = json.loads(payload)
    prs = []
    for r in rows:
        prs.append(
            PullRequest(
                repo=repo,
                number=int(r["number"]),
                title=str(r.get("title") or "")[:200],
                state=str(r.get("state") or "").lower(),
                created_at=str(r.get("createdAt") or ""),
                merged_at=r.get("mergedAt") or None,
                closed_at=r.get("closedAt") or None,
                head_ref=str(r.get("headRefName") or ""),
                body=str(r.get("body") or "")[:BODY_CAP],
            )
        )
    return prs


def fetch(repo: str, gh: str, limit: int = 2000) -> list[PullRequest]:
    if not _SLUG.match(repo):
        raise GhError(f"not an owner/name repo: {repo!r}")
    cmd = [
        gh,
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        str(limit),
        "--json",
        FIELDS,
    ]
    try:
        done = subprocess.run(cmd, capture_output=True, check=False, timeout=TIMEOUT)  # noqa: S603 - fixed argv
    except subprocess.TimeoutExpired:
        raise GhUnreachable(f"gh pr list timed out after {TIMEOUT:g} s") from None
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace").strip()[:300]
        raise GhError(f"gh pr list exit {done.returncode}: {err}")
    try:
        prs = parse(repo, done.stdout.decode("utf-8"))
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        # A banner, a changed format: gh answered, but not with the PR list.
        raise GhError(
            f"gh pr list printed something that is not the expected JSON ({e!r:.80})"
        ) from None
    if len(prs) >= limit:
        raise GhError(
            f"gh returned {len(prs)} PRs, the limit: raise the limit rather than drop the rest"
        )
    return prs


def _gh_json(gh: str, args: list[str], what: str) -> object:
    try:
        done = subprocess.run([gh, *args], capture_output=True, check=False, timeout=TIMEOUT)  # noqa: S603 - fixed argv
    except subprocess.TimeoutExpired:
        raise GhUnreachable(f"{what} timed out after {TIMEOUT:g} s") from None
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace").strip()[:300]
        raise GhError(f"{what} exit {done.returncode}: {err}")
    try:
        return json.loads(done.stdout.decode("utf-8"))
    except ValueError as e:
        raise GhError(
            f"{what} printed something that is not the expected JSON ({e!r:.80})"
        ) from None


_DEFAULT_BRANCH: dict[str, tuple[str, float]] = {}  # repo -> (branch, when it was asked)
_clock = time.monotonic


def default_branch(repo: str, gh: str) -> str:
    """The repo's default branch as GitHub names it, asked again after BRANCH_TTL (a page runs
    for days, and a branch can be renamed): the CI card is about it whatever ref the docs are
    read at."""
    if not _SLUG.match(repo):
        raise GhError(f"not an owner/name repo: {repo!r}")
    cached = _DEFAULT_BRANCH.get(repo)
    if cached is not None and _clock() - cached[1] < BRANCH_TTL:
        return cached[0]
    data = _gh_json(gh, ["repo", "view", repo, "--json", "defaultBranchRef"], "gh repo view")
    ref = data.get("defaultBranchRef") if isinstance(data, dict) else None
    name = ref.get("name") if isinstance(ref, dict) else None
    if not isinstance(name, str) or not name:
        raise GhError("gh repo view named no default branch")
    _DEFAULT_BRANCH[repo] = (name, _clock())
    return name


def _runs_of(repo: str, gh: str, branch: str, event: str, limit: int) -> list[Run]:
    args = ["run", "list", "--repo", repo, "--branch", branch, "--event", event,
            "--limit", str(limit), "--json", RUN_FIELDS]  # fmt: skip
    rows = _gh_json(gh, args, "gh run list")
    try:
        return [
            Run(
                repo,
                int(r["databaseId"]),
                str(r.get("workflowName") or "?"),
                str(r.get("status") or ""),
                str(r.get("conclusion") or ""),
                str(r.get("createdAt") or ""),
                str(r.get("url") or ""),
                str(r.get("event") or ""),
            )  # fmt: skip
            for r in rows  # type: ignore[union-attr]
            if r.get("event") == event  # what gh was asked for, checked again
        ]
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise GhError(f"gh run list printed something unexpected ({e!r:.80})") from None


def fetch_runs(repo: str, gh: str, branch: str, limit: int = 50) -> list[Run]:
    """The newest workflow runs testing a branch's own commits (BRANCH_EVENTS), newest first,
    asked event by event, the events at once."""
    if not _SLUG.match(repo):
        raise GhError(f"not an owner/name repo: {repo!r}")
    with ThreadPoolExecutor(max_workers=len(BRANCH_EVENTS)) as pool:
        batches = list(pool.map(lambda e: _runs_of(repo, gh, branch, e, limit), BRANCH_EVENTS))
    runs = [r for batch in batches for r in batch]
    return sorted(runs, key=lambda r: (r.created_at, r.id), reverse=True)


def refused_before_running(repo: str, gh: str, run_id: int) -> bool:
    """Whether a red run never ran: every failed job has no steps (GitHub refusing it, the
    Actions budget for one), which is not a failing build."""
    data = _gh_json(gh, ["run", "view", str(int(run_id)), "--repo", repo, "--json", "jobs"],
                    "gh run view")  # fmt: skip
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise GhError("gh run view printed no jobs")
    failed = [j for j in jobs if isinstance(j, dict) and j.get("conclusion") in RED]
    return bool(failed) and all(not j.get("steps") for j in failed)
