"""Card ids: frozen, versioned, derived from content and never from where a file lives.

A doc moving between folders (a game's DECISIONS.md split out of the root one) must keep every
card id, or the owner's moves on those cards are lost. Changing any derivation here changes ids
for every board: bump ID_VERSION and migrate instead.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

ID_VERSION = "1"

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_DATED_PAREN = re.compile(r"\(\s*\d{4}-\d{2}-\d{2}[^)]*\)")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_SECTION_TAIL = re.compile(r"\s+(?:—|–|-{1,2})\s+.*$|\s+\(.*$")


def norm(text: str) -> str:
    """Casefolded words only: markdown, links and dated parentheses dropped."""
    s = unicodedata.normalize("NFKC", text)
    s = _LINK.sub(r"\1", s)
    s = _DATED_PAREN.sub(" ", s)
    s = s.casefold()
    return _NON_ALNUM.sub(" ", s).strip()


def section_stem(heading: str) -> str:
    """A ROADMAP heading without what gets appended later ("— merged in PR #143", "(shipped…)")."""
    return norm(_SECTION_TAIL.sub("", heading.strip()))


def item_stem(text: str, words: int = 10) -> str:
    return " ".join(norm(text).split()[:words])


def _h(*parts: str) -> str:
    return hashlib.sha1("\x1f".join((ID_VERSION, *parts)).encode("utf-8")).hexdigest()[:12]  # noqa: S324 - an id, not a secret


def backlog_id(project: str, title: str, salt: str = "") -> str:
    """No path and no sub-project: a bullet moved from the root file into a game's keeps its id."""
    return "bl:" + _h("backlog", project, norm(title), salt)


def roadmap_id(project: str, sub: str, section: str, item: str) -> str:
    return "rm:" + _h("roadmap", project, sub, section_stem(section), item_stem(item))


def pr_id(repo: str, number: int) -> str:
    return f"pr:{repo}#{number}"


def plan_id(tool_use_id: str) -> str:
    return f"pl:{tool_use_id}"


def needs_id(message_uuid: str, n: int) -> str:
    return f"ny:{message_uuid}:{n}"


def ask_id(tool_use_id: str) -> str:
    return f"aq:{tool_use_id}"


def wrap_id(tool_use_id: str) -> str:
    return f"wu:{tool_use_id}"


def ci_id(repo: str, workflow: str) -> str:
    return f"ci:{repo}#{workflow}"


def proposal_id(tool_use_id: str) -> str:
    return f"ss:{tool_use_id}"
