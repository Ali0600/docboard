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
ALLOWED_USERS = frozenset({"you", "example", "user", "runner", "name", "…", "...", "<name>"})
# Email domains that identify nobody, and the co-author line other repos' commits may carry.
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "users.noreply.github.com"}
)
ALLOWED_EMAILS = frozenset({"noreply@anthropic.com", "noreply@github.com"})
PRIVATE_LIST = Path("config/private-identifiers.txt")  # where the list lived before the data folder

# /Users/name and /home/name, also after "host:" (scp, rsync, a git remote) or "file:"; not right
# after a real drive letter (C:/Users/name), which _WIN_HOME reports instead.
_HOME = re.compile(r"(?<!(?<![A-Za-z0-9])[A-Za-z]:)(?:/Users|/home)/([A-Za-z0-9._-]+)")
# C:\Users\name, C:/Users/name, and JSON's C:\\Users\\name (escaped once or twice), any case; the
# name runs to the next separator or delimiter (a prompt's ">", a backtick, a comma), so names
# with ~, spaces' first word, or letters outside ASCII are all caught.
_WIN_HOME = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:(?:\\+|/)Users(?:\\+|/)([^\\/\"'\s`,;:()\[\]<>|*?]+)",
    re.IGNORECASE,
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


def _decode(data: bytes) -> str | None:
    """Text as Windows tools also write it (a UTF-8 BOM, UTF-16 with its BOM); None for binary."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8-sig", errors="replace")


def read_private_terms(path: Path) -> list[str]:
    """The local list, one term per line; '#' starts a comment. Missing file: no terms."""
    if not path.is_file():
        return []
    terms = []
    for raw in (_decode(path.read_bytes()) or "").splitlines():
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
        found.extend(scan_text(f"{rel} (its name)", rel, terms))  # a name is published too
        path = root / rel
        if not path.is_file():
            continue  # deleted in the working tree
        text = _decode(path.read_bytes())
        if text is not None:  # binary files are skipped
            found.extend(scan_text(rel, text, terms))
    return found


_DIFF_HEADER = re.compile(
    r"^(?:diff --git |rename (?:from|to) |copy (?:from|to) |\+\+\+ (?:[ab]/|/dev/null))"
)


def scan_commits(
    root: Path, revs: list[str], terms: Iterable[str] = (), skip_author: str = ""
) -> list[Finding]:
    """Every commit in a range: its message, author and committer addresses, the names of the
    files it adds or renames, and each line it adds (a merge's against its first parent; a term
    added in one commit and removed in the next is still published). `skip_author` leaves out
    commits by one author (a bot's release notes)."""
    terms = list(terms)
    fmt = "--format=%x1ecommit %H%n%an <%ae>%n%cn <%ce>%n%B%x1f"
    out = _git(root, "log", "-p", "--no-color", "--no-ext-diff", "--diff-merges=first-parent",
               fmt, *revs, "--")  # fmt: skip
    found: list[Finding] = []
    for chunk in out.decode("utf-8", "replace").split("\x1e")[1:]:
        head, _, diff = chunk.partition("\x1f")
        lines = head.split("\n", 2)
        sha = lines[0].removeprefix("commit ")[:12]
        if skip_author and len(lines) > 1 and skip_author in lines[1]:
            continue
        found.extend(scan_text(f"commit {sha}", head.split("\n", 1)[-1], terms))
        headers, added = [], []
        for ln in diff.splitlines():
            if _DIFF_HEADER.match(ln):
                headers.append(ln)
            elif ln.startswith("+"):
                added.append(ln[1:])  # an added line that itself starts with "++" included
        found.extend(scan_text(f"commit {sha} (file names)", "\n".join(headers), terms))
        found.extend(scan_text(f"commit {sha} (added lines)", "\n".join(added), terms))
    return found


def scan_push(
    root: Path, hook_stdin: str, terms: Iterable[str] = (), remote: str | None = None
) -> list[Finding]:
    """What a `git push` would publish, from the lines git hands the pre-push hook:
    `<local ref> <local sha> <remote ref> <remote sha>` per ref. A new branch publishes every
    commit the remote being pushed to lacks (another remote having it publishes nothing here);
    an annotated tag publishes its message too."""
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
        kind = _git(root, "cat-file", "-t", local_sha).decode().strip()
        if kind == "tag":
            tag = _git(root, "cat-file", "-p", local_sha).decode("utf-8", "replace")
            found.extend(scan_text(f"tag {local_ref}", tag, terms))
        if remote_sha == _ZERO:
            theirs = f"--remotes={remote}" if remote else "--remotes"
            revs = [local_sha, "--not", theirs]
        else:
            revs = [f"{remote_sha}..{local_sha}"]
        found.extend(scan_commits(root, revs, terms))
    return found
