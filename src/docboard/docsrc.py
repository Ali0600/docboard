"""Parsers for the house docs shape: the DECISIONS.md Backlog, ROADMAP.md checkboxes, and the
checkbox steps of a checklist (`docs/checklists/*.md`)."""

from __future__ import annotations

import re
from dataclasses import dataclass

BODY_CAP = 8192
_BACKLOG_HEAD = re.compile(r"^## Backlog\b")
_H2 = re.compile(r"^## ")
_BULLET = re.compile(r"^- \*\*(.+?)\*\*(.*)$")
_BULLET_START = re.compile(r"^- \*\*")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_CHECK = re.compile(r"^(\s*)- \[( |x|X)\]\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Bullet:
    title: str
    body: str
    line: int


@dataclass(frozen=True)
class RoadmapItem:
    section: str
    text: str
    done: bool
    line: int
    title: str = ""  # a checklist's own title (its H1), which names its game under any section


def parse_backlog(text: str) -> list[Bullet]:
    """Bullets of the `## Backlog …` section: a column-0 `- **Title**` up to the next one.

    A bold title may wrap onto the next lines: they are joined until its closing `**`. A bullet
    whose title never closes is not one; `backlog_starts` counts it, so the sync can warn.
    """
    bullets: list[Bullet] = []
    inside = False
    cur: list[str] | None = None
    title, start = "", 0
    lines = text.splitlines()

    def flush() -> None:
        if cur is not None:
            body = " ".join(s.strip() for s in cur if s.strip())
            bullets.append(Bullet(title=title.strip(), body=body[:BODY_CAP], line=start))

    n = 0
    while n < len(lines):
        line = lines[n]
        if _BACKLOG_HEAD.match(line):
            inside = True
            n += 1
            continue
        if not inside:
            n += 1
            continue
        if _H2.match(line):
            break
        m, used = _BULLET.match(line), 0
        if m is None and _BULLET_START.match(line):
            joined = line
            for k in range(n + 1, len(lines)):
                nxt = lines[k]
                if not nxt.strip() or nxt.startswith("- ") or _H2.match(nxt):
                    break
                joined += " " + nxt.strip()
                m = _BULLET.match(joined)
                if m:
                    used = k - n
                    break
        if m:
            flush()
            title, start, cur = m.group(1), n + 1, [m.group(2)]
            n += 1 + used
            continue
        if cur is not None:
            cur.append(line)
        n += 1
    flush()
    return bullets


def backlog_starts(text: str) -> int:
    """How many column-0 `- **` lines the Backlog section holds: what parse_backlog should read."""
    count, inside = 0, False
    for line in text.splitlines():
        if _BACKLOG_HEAD.match(line):
            inside = True
        elif inside and _H2.match(line):
            break
        elif inside and _BULLET_START.match(line):
            count += 1
    return count


def parse_checklist(text: str) -> list[RoadmapItem]:
    """A checklist's `- [ ]` steps; until its first `##`, the file's title is their section."""
    return parse_roadmap(text, title_section=True)


def parse_roadmap(text: str, title_section: bool = False) -> list[RoadmapItem]:
    """`- [ ]` items under their `##` sections. With `title_section` (a checklist) the file's
    first `#` heading is the section until a `##` one, and every item keeps it as its title.
    Lines inside a fenced code block (a shell `# comment`) are never headings or items."""
    items: list[RoadmapItem] = []
    section = ""
    title = ""
    cur: dict | None = None
    fenced = False

    def flush() -> None:
        if cur is not None:
            items.append(
                RoadmapItem(
                    section=cur["section"],
                    text=" ".join(cur["text"])[:BODY_CAP],
                    done=cur["done"],
                    line=cur["line"],
                    title=title,
                )
            )

    for n, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            fenced = not fenced
            if cur is not None and not line.startswith("  "):
                flush()
                cur = None
            continue
        if fenced:
            continue
        h = _HEADING.match(line)
        level = len(h.group(1)) if h else 0
        if h and (level >= 2 or (title_section and level == 1 and not title)):
            flush()
            cur = None
            section = h.group(2)
            if level == 1:
                title = h.group(2)
            continue
        m = _CHECK.match(line)
        if m:
            flush()
            cur = {
                "section": section,
                "text": [m.group(3).strip()],
                "done": m.group(2) != " ",
                "line": n,
            }
        elif cur is not None and line.startswith("  ") and line.strip():
            cur["text"].append(line.strip())
        elif cur is not None and not line.strip():
            flush()
            cur = None
    flush()
    return items


_MS_BULLET = re.compile(r"^- ")
# "(#162)", "#162-#167", "PR #74", "PRs #108 and #110", "after the #263 hand check".
_MS_REF = re.compile(r"(?<![\w/])#(\d+)(?:\s*[-–]\s*#(\d+))?")
# A word right before "#N" names whose number it is: these leave it a PR; any other ("game #2",
# "visual review #24", "issue #9") is that thing's own number.
_PR_WORDS = frozenset(
    {
        "pr",
        "prs",
        "and",
        "or",
        "the",
        "then",
        "also",
        "plus",
        "in",
        "via",
        "see",
        "after",
        "before",
        "from",
        "with",
        "by",
        "on",
        "since",
        "until",
    }
)
_LAST_WORD = re.compile(r"([A-Za-z]+)\s*$")


def _a_pr(bullet: str, start: int) -> bool:
    """Whether the "#N" at `start` stands for a PR, by the word before it (if any)."""
    m = _LAST_WORD.search(bullet[max(0, start - 30) : start])
    return m is None or m.group(1).lower() in _PR_WORDS


def milestone_bullets(text: str) -> list[str]:
    """The column-0 bullets of a MILESTONES.md, each joined with its continuation lines."""
    out: list[str] = []
    cur: list[str] | None = None
    for line in text.splitlines():
        if _MS_BULLET.match(line):
            if cur is not None:
                out.append(" ".join(cur))
            cur = [line[2:].strip()]
        elif cur is not None and line.startswith(" ") and line.strip():
            cur.append(line.strip())
        elif cur is not None:
            out.append(" ".join(cur))
            cur = None
    if cur is not None:
        out.append(" ".join(cur))
    return out


def milestone_refs(text: str, known: set[int]) -> dict[int, list[str]]:
    """PR number -> the MILESTONES bullets citing it, for the numbers that are this repo's PRs. A
    range is filled in only when both of its ends are known PRs, in order and at most 50 apart;
    otherwise each end that is a known PR counts on its own."""
    refs: dict[int, list[str]] = {}
    for bullet in milestone_bullets(text):
        cited: set[int] = set()
        for m in _MS_REF.finditer(bullet):
            if not _a_pr(bullet, m.start()):
                continue
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else None
            if b is not None and a in known and b in known and a < b <= a + 50:
                cited.update(n for n in range(a, b + 1) if n in known)
            else:
                cited.update(n for n in (a, b) if n is not None and n in known)
        for n in sorted(cited):
            refs.setdefault(n, []).append(bullet[:BODY_CAP])
    return refs
