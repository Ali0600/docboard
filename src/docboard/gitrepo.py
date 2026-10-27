"""Reads a repo's files at a ref, never its working tree when a ref is available.

A shared checkout may be on another session's branch with uncommitted work, so docs are read
from the blobs at `origin/main` (or the configured ref). A repo with no remote is read from its
working tree. Nothing here writes to the repo: `--no-optional-locks` keeps even `git status`-like
reads from refreshing the index.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SQUASH = re.compile(r"\(#(\d+)\)\s*$")
_REMOTE = re.compile(r"github\.com[:/]([A-Za-z0-9-]+/[A-Za-z0-9._-]+?)(?:\.git)?/?$")


class GitError(RuntimeError):
    pass


def find_git() -> str:
    found = shutil.which("git") or "/usr/bin/git"
    return str(Path(found).resolve())


@dataclass
class Repo:
    root: Path
    git: str
    ref: str | None  # None: the working tree
    ref_sha: str | None = None

    @classmethod
    def open(cls, root: Path, ref: str | None, git: str | None = None) -> Repo:
        git = git or find_git()
        repo = cls(root=root, git=git, ref=None)
        chosen = ref if ref is not None else repo._default_ref()
        if chosen:
            sha = repo._try("rev-parse", "--verify", "--quiet", f"{chosen}^{{commit}}")
            if sha is None:
                raise GitError(f"{root}: ref {chosen!r} does not exist")
            repo.ref, repo.ref_sha = chosen, sha.strip()
        return repo

    def _run(self, *args: str) -> bytes:
        cmd = [self.git, "--no-optional-locks", "-C", str(self.root), *args]
        done = subprocess.run(cmd, capture_output=True, check=False)  # noqa: S603 - fixed argv
        if done.returncode != 0:
            msg = done.stderr.decode("utf-8", "replace").strip()
            raise GitError(f"git {' '.join(args)}: {msg}")
        return done.stdout

    def _try(self, *args: str) -> str | None:
        try:
            return self._run(*args).decode("utf-8", "replace")
        except GitError:
            return None

    def _default_ref(self) -> str | None:
        head = self._try("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if head:
            return head.strip().removeprefix("refs/remotes/")
        for candidate in ("origin/main", "origin/master"):
            if self._try("rev-parse", "--verify", "--quiet", candidate):
                return candidate
        return None

    def files(self, prefix: str = "") -> list[str]:
        if self.ref:
            out = self._run("ls-tree", "-r", "-z", "--name-only", self.ref, "--", prefix or ".")
        else:
            out = self._run("ls-files", "-z", "--", prefix or ".")
        return sorted(p for p in out.decode("utf-8").split("\0") if p)

    def dirs(self, parent: str) -> list[str]:
        """Immediate sub-directories of `parent` (names only)."""
        if self.ref:
            out = self._run("ls-tree", "-z", "-d", "--name-only", self.ref, "--", parent + "/")
            return sorted(p.rsplit("/", 1)[-1] for p in out.decode("utf-8").split("\0") if p)
        base = self.root / parent
        return sorted(d.name for d in base.iterdir() if d.is_dir()) if base.is_dir() else []

    def read(self, path: str) -> str | None:
        if self.ref:
            out = self._try("cat-file", "-p", f"{self.ref}:{path}")
            return out
        file = (self.root / path).resolve()
        if not file.is_relative_to(self.root.resolve()) or not file.is_file():
            return None
        return file.read_text(encoding="utf-8", errors="replace")

    def squash_files(self) -> dict[int, list[str]]:
        """PR number → files, for commits whose subject ends in "(#N)" (squash merges)."""
        rev = self.ref or "HEAD"
        out = self._run("log", rev, "--name-only", "--format=%x1e%s").decode("utf-8", "replace")
        result: dict[int, list[str]] = {}
        for chunk in out.split("\x1e"):
            lines = [ln for ln in chunk.splitlines() if ln.strip()]
            if not lines:
                continue
            m = _SQUASH.search(lines[0])
            if m:
                result.setdefault(int(m.group(1)), lines[1:])
        return result

    def github_slug(self) -> str | None:
        url = self._try("remote", "get-url", "origin")
        m = _REMOTE.search(url.strip()) if url else None
        return m.group(1) if m else None
