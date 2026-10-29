"""Which configured board a directory belongs to, its address, and whether the page is up.

`docboard board` runs from any session's directory: the project's own checkout, a folder inside
it, or a git worktree of it (inside the repo, as `.claude/worktrees/<name>`, or anywhere else). A
worktree is mapped back to the main checkout through git's common directory, so the same folder
inside it finds the same board. Nothing here reads a project that is not in the config.
"""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from docboard.config import Config, Project
from docboard.gitrepo import find_git


@dataclass(frozen=True)
class Located:
    project: Project
    sub: str | None  # a sub-project's directory name, or None for the whole board


def _git_roots(cwd: Path, git: str) -> tuple[Path, Path] | None:
    """(this worktree's root, the main checkout) for a folder in a git work tree, else None."""
    args = ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"]
    cmd = [git, "--no-optional-locks", "-C", str(cwd), *args]
    done = subprocess.run(cmd, capture_output=True, check=False)  # noqa: S603 - fixed argv
    if done.returncode != 0:
        return None
    lines = done.stdout.decode("utf-8", "replace").splitlines()
    if len(lines) != 2:
        return None
    top, common = Path(lines[0]).resolve(), Path(lines[1]).resolve()
    if common.name != ".git":  # a bare repo's worktree: no main checkout to map onto
        return None
    return top, common.parent


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def locate(cfg: Config, cwd: Path, git: str | None = None) -> Located | None:
    """The configured project holding `cwd` (the deepest one when projects nest), or None."""
    here = cwd.resolve()
    roots = _git_roots(here, git or find_git())
    if roots is not None:
        top, main = roots
        if top != main:  # a linked worktree: the same place in the main checkout
            here = main / here.relative_to(top)
    found = [p for p in cfg.projects if _inside(here, p.path.resolve())]
    if not found:
        return None
    project = max(found, key=lambda p: len(p.path.resolve().parts))
    rel = here.relative_to(project.path.resolve()).parts
    sp = project.subprojects
    sub = None
    if sp is not None:
        parent = tuple(sp.parent.split("/"))
        n = len(parent)
        if len(rel) > n and rel[:n] == parent and rel[n] not in sp.exclude:
            sub = rel[n]
    return Located(project=project, sub=sub)


def board_url(port: int, project: str, sub: str | None = None) -> str:
    url = f"http://127.0.0.1:{port}/p/{quote(project, safe='')}"
    return url + (f"?sub={quote(sub, safe='')}" if sub else "")


def page_state(url: str, timeout: float = 2.0) -> str:
    """Whether the page answers: up; stale when it does not know this project (it was started
    before the project was added to the config); "answered <code>" for another error; down."""
    return page_check(url, timeout)[0]


def page_check(url: str, timeout: float = 2.0) -> tuple[str, str | None]:
    """The page's state (see page_state) and the code it runs (its X-Docboard-Code header)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # loopback: no proxy
    try:
        with opener.open(url, timeout=timeout) as r:
            return "up", r.headers.get("X-Docboard-Code")
    except urllib.error.HTTPError as e:
        return ("stale" if e.code == 404 else f"answered {e.code}"), e.headers.get(
            "X-Docboard-Code"
        )
    except (urllib.error.URLError, OSError):
        return "down", None
