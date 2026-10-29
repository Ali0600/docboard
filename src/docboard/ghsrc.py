"""Pull requests through the gh CLI (its own auth; docboard holds no token)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SLUG = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
FIELDS = "number,title,state,createdAt,mergedAt,closedAt,headRefName,body"
RUN_FIELDS = "databaseId,workflowName,status,conclusion,createdAt,url"
RED = frozenset({"failure", "startup_failure", "timed_out"})
BODY_CAP = 65536
TIMEOUT = 120.0  # seconds; a hung gh (a login prompt, a dead network) degrades the sync


class GhError(RuntimeError):
    pass


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
        raise GhError(f"gh pr list timed out after {TIMEOUT:g} s") from None
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
        raise GhError(f"{what} timed out after {TIMEOUT:g} s") from None
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace").strip()[:300]
        raise GhError(f"{what} exit {done.returncode}: {err}")
    try:
        return json.loads(done.stdout.decode("utf-8"))
    except ValueError as e:
        raise GhError(
            f"{what} printed something that is not the expected JSON ({e!r:.80})"
        ) from None


def fetch_runs(repo: str, gh: str, branch: str, limit: int = 50) -> list[Run]:
    """The newest workflow runs on a branch, newest first."""
    if not _SLUG.match(repo):
        raise GhError(f"not an owner/name repo: {repo!r}")
    rows = _gh_json(gh, ["run", "list", "--repo", repo, "--branch", branch, "--limit", str(limit),
                         "--json", RUN_FIELDS], "gh run list")  # fmt: skip
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
            )  # fmt: skip
            for r in rows  # type: ignore[union-attr]
        ]
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise GhError(f"gh run list printed something unexpected ({e!r:.80})") from None


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
