"""Parsers for the house docs shape: the DECISIONS.md Backlog and ROADMAP.md checkboxes."""

from __future__ import annotations

import re
from dataclasses import dataclass

BODY_CAP = 8192
_BACKLOG_HEAD = re.compile(r"^## Backlog\b")
_H2 = re.compile(r"^## ")
_BULLET = re.compile(r"^- \*\*(.+?)\*\*(.*)$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_CHECK = re.compile(r"^(\s*)- \[( |x|X)\]\s+(.*)$")


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


def parse_backlog(text: str) -> list[Bullet]:
    """Bullets of the `## Backlog …` section: a column-0 `- **Title**` up to the next one."""
    bullets: list[Bullet] = []
    inside = False
    cur: list[str] | None = None
    title, start = "", 0

    def flush() -> None:
        if cur is not None:
            body = " ".join(s.strip() for s in cur if s.strip())
            bullets.append(Bullet(title=title.strip(), body=body[:BODY_CAP], line=start))

    for n, line in enumerate(text.splitlines(), start=1):
        if _BACKLOG_HEAD.match(line):
            inside = True
            continue
        if not inside:
            continue
        if _H2.match(line):
            break
        m = _BULLET.match(line)
        if m:
            flush()
            title, start, cur = m.group(1), n, [m.group(2)]
        elif cur is not None:
            cur.append(line)
    flush()
    return bullets


def parse_roadmap(text: str) -> list[RoadmapItem]:
    items: list[RoadmapItem] = []
    section = ""
    cur: dict | None = None

    def flush() -> None:
        if cur is not None:
            items.append(
                RoadmapItem(
                    section=cur["section"],
                    text=" ".join(cur["text"])[:BODY_CAP],
                    done=cur["done"],
                    line=cur["line"],
                )
            )

    for n, line in enumerate(text.splitlines(), start=1):
        h = _HEADING.match(line)
        if h and len(h.group(1)) >= 2:
            flush()
            cur = None
            section = h.group(2)
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
