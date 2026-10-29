"""Finds personal identifiers before they reach a public remote.

Three kinds: a home folder path naming a real user (macOS, Linux or Windows, plain or as JSON
writes it), an email address outside the allowed domains, and any term from a local, git-ignored
list (`private-identifiers.txt` in the data folder). The repo's files are scanned by the gate;
what a push carries (every added line of every commit, the messages, the author and committer
addresses, the branch names) by the pre-push hook; a PR's title and body by `--stdin`.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Placeholder user names that docs and fixtures may use in home paths.
ALLOWED_USERS = frozenset({"you", "example", "user", "runner", "name"})
# Email domains that identify nobody, and the co-author line other repos' commits may carry.
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "users.noreply.github.com"}
)
ALLOWED_EMAILS = frozenset({"noreply@anthropic.com", "noreply@github.com"})
PRIVATE_LIST = Path("config/private-identifiers.txt")  # where the list lived before the data folder

_HOME = re.compile(r"(?<![A-Za-z]:)(?:/Users|/home)/([A-Za-z0-9._-]+)")  # not C:/Users: below
# C:\Users\name, C:/Users/name, and JSON's C:\\Users\\name, any case.
_SEP = r"(?:\\{1,2}|/)"
_WIN_HOME = re.compile(
    r"(?i)\b[A-Z]:" + _SEP + r"Users" + _SEP + r"([A-Za-z0-9._ -]+?)(?=\\|/|\"|'|\s|$)"
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
_ZERO = "0" * 40


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    excerpt: str


def scan_text(path: str, text: str, private_terms: Iterable[str] = ()) -> list[Finding]:
    """Every identifier in one text, one finding per match."""
    terms = [t.casefold() for t in private_terms if t.strip()]
    found: list[Finding] = []
    for n, line in enumerate(text.splitlines(), start=1):
        for pat in (_HOME, _WIN_HOME):
            for m in pat.finditer(line):
                if m.group(1).strip().casefold() not in ALLOWED_USERS:
                    found.append(Finding(path, n, "home-path", m.group(0)))
        for m in _EMAIL.finditer(line):
            if (
                m.group(1).casefold() not in ALLOWED_EMAIL_DOMAINS
                and m.group(0).casefold() not in ALLOWED_EMAILS
            ):
                found.append(Finding(path, n, "email", m.group(0)))
        folded = line.casefold()
        for term in terms:
            if term in folded:
                found.append(Finding(path, n, "private-term", f"<term {len(term)} chars>"))
    return found


def read_private_terms(path: Path) -> list[str]:
    """The local list, one term per line; '#' starts a comment. Missing file: no terms."""
    if not path.is_file():
        return []
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.split("#", 1)[0].strip()
        if term:
            terms.append(term)
    return terms


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(root), *args], check=True, capture_output=True
    ).stdout


def repo_files(root: Path) -> list[str]:
    """Tracked files plus untracked ones git would add (what a push could carry)."""
    out = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({p for p in out.decode("utf-8").split("\0") if p})


def scan_repo(root: Path, terms: Iterable[str] = ()) -> list[Finding]:
    terms = list(terms)
    found: list[Finding] = []
    for rel in repo_files(root):
        if rel == PRIVATE_LIST.as_posix():
            continue  # git-ignored, so never listed; guard anyway
        path = root / rel
        if not path.is_file():
            continue  # deleted in the working tree
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        found.extend(scan_text(rel, data.decode("utf-8", errors="replace"), terms))
    return found


def scan_commits(root: Path, revs: list[str], terms: Iterable[str] = ()) -> list[Finding]:
    """Every commit in a range: its message, author and committer addresses, and each line it
    adds (a term added in one commit and removed in the next is still published)."""
    terms = list(terms)
    fmt = "--format=%x1ecommit %H%n%an <%ae>%n%cn <%ce>%n%B%x1f"
    out = _git(root, "log", "-p", "--no-color", "--no-ext-diff", fmt, *revs, "--")
    found: list[Finding] = []
    for chunk in out.decode("utf-8", "replace").split("\x1e")[1:]:
        head, _, diff = chunk.partition("\x1f")
        sha = head.split("\n", 1)[0].removeprefix("commit ")[:12]
        found.extend(scan_text(f"commit {sha}", head.split("\n", 1)[-1], terms))
        added = "\n".join(
            ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")
        )
        found.extend(scan_text(f"commit {sha} (added lines)", added, terms))
    return found


def scan_push(root: Path, hook_stdin: str, terms: Iterable[str] = ()) -> list[Finding]:
    """What a `git push` would publish, from the lines git hands the pre-push hook:
    `<local ref> <local sha> <remote ref> <remote sha>` per ref."""
    terms = list(terms)
    found: list[Finding] = []
    for line in hook_stdin.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if local_sha == _ZERO:
            continue  # a deleted branch publishes nothing
        found.extend(scan_text("ref", f"{local_ref} {remote_ref}", terms))
        if remote_sha == _ZERO:
            revs = [local_sha, "--not", "--remotes"]  # a new branch: what no remote has yet
        else:
            revs = [f"{remote_sha}..{local_sha}"]
        found.extend(scan_commits(root, revs, terms))
    return found
