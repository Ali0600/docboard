from __future__ import annotations

from docboard.docsrc import parse_backlog, parse_roadmap

DECISIONS = """# Decisions

## Backlog — alternatives worth trying later

- **Pole swing** (2026-10-25, the glider's fork): swing round a pole.
  Revisit hook: `Logic/Traversal`.
- **Canal lofts** (2026-10-25): a seventh district.

- **A third idea**: no date.

## 2026-10-26 — Some decision

- **Not a backlog bullet**: inside a dated entry.
"""

ROADMAP = """# Roadmap

## 1. Foundation — merged in PR #143

- [x] Scaffold the game.
- [ ] Verify 60 FPS; hidden Studio
  stays below it.

## Gates

- [ ] Physical controller acceptance.
"""


def test_backlog_bullets_stop_at_the_next_section() -> None:
    bullets = parse_backlog(DECISIONS)
    assert [b.title for b in bullets] == ["Pole swing", "Canal lofts", "A third idea"]
    assert bullets[0].line == 5
    assert "Revisit hook" in bullets[0].body


def test_no_backlog_section_means_no_bullets() -> None:
    assert parse_backlog("# Decisions\n\n## 2026-10-01 — X\n\n- **Y**: z\n") == []


def test_roadmap_items_with_sections_and_continuations() -> None:
    items = parse_roadmap(ROADMAP)
    assert [(i.section, i.done) for i in items] == [
        ("1. Foundation — merged in PR #143", True),
        ("1. Foundation — merged in PR #143", False),
        ("Gates", False),
    ]
    assert items[1].text == "Verify 60 FPS; hidden Studio stays below it."
    assert items[2].line == 11
