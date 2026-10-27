"""Finds personal identifiers in a repo's files before they reach a public remote.

Three kinds: a home folder path naming a real user, an email address outside the allowed
domains, and any term from a local, git-ignored list (config/private-identifiers.txt).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Placeholder user names that docs and fixtures may use in home paths.
ALLOWED_USERS = frozenset({"you", "example", "user", "runner", "name"})
# Email domains that identify nobody.
ALLOWED_EMAIL_DOMAINS = frozenset(
    {"example.com", "example.org", "example.net", "users.noreply.github.com"}
)
PRIVATE_LIST = Path("config/private-identifiers.txt")

_HOME = re.compile(r"(?:/Users|/home)/([A-Za-z0-9._-]+)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    excerpt: str


def scan_text(path: str, text: str, private_terms: Iterable[str] = ()) -> list[Finding]:
    """Every identifier in one file's text, one finding per match."""
    terms = [t.casefold() for t in private_terms if t.strip()]
    found: list[Finding] = []
    for n, line in enumerate(text.splitlines(), start=1):
        for m in _HOME.finditer(line):
            if m.group(1).casefold() not in ALLOWED_USERS:
                found.append(Finding(path, n, "home-path", m.group(0)))
        for m in _EMAIL.finditer(line):
            if m.group(1).casefold() not in ALLOWED_EMAIL_DOMAINS:
                found.append(Finding(path, n, "email", m.group(0)))
        folded = line.casefold()
        for term in terms:
            if term in folded:
                found.append(Finding(path, n, "private-term", f"<term {len(term)} chars>"))
    return found


def read_private_terms(root: Path) -> list[str]:
    """The local list, one term per line; '#' starts a comment. Missing file: no terms."""
    path = root / PRIVATE_LIST
    if not path.is_file():
        return []
    terms = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        term = raw.split("#", 1)[0].strip()
        if term:
            terms.append(term)
    return terms


def repo_files(root: Path) -> list[str]:
    """Tracked files plus untracked ones git would add (what a push could carry)."""
    out = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        check=True,
        capture_output=True,
    ).stdout
    return sorted({p for p in out.decode("utf-8").split("\0") if p})


def scan_repo(root: Path) -> list[Finding]:
    terms = read_private_terms(root)
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
