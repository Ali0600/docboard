from __future__ import annotations

from docboard import docsrc
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


WRAPPED = """# Decisions

## Backlog — later

- **A fifth district** (2026-10-20): a wide one.
- **More cosmetics: survivor gear, cards and banners for the
  winter season** (2026-10-21): sold in the shop.
- **An unclosed title with no end
- **Last** : z.
"""


def test_a_bold_title_that_wraps_is_one_bullet() -> None:
    bullets = parse_backlog(WRAPPED)
    titles = [b.title for b in bullets]
    assert titles[:2] == [
        "A fifth district",
        "More cosmetics: survivor gear, cards and banners for the winter season",
    ]
    assert "survivor gear" not in bullets[0].body  # it is no longer folded into the bullet above
    assert bullets[1].line == 6 and "sold in the shop" in bullets[1].body
    assert titles[-1] == "Last"


def test_bullet_starts_are_counted_so_a_lost_one_shows() -> None:
    assert docsrc.backlog_starts(WRAPPED) == 4
    assert len(parse_backlog(WRAPPED)) == 3  # the unclosed one: the sync warns 4 → 3


CHECKLIST = """# Alpha Game: going public

The owner's steps.

- [ ] **1. Look at staging.** Play it
  on a phone too.
- [x] **2. Badges.** All created.

## Later

- [ ] **3. Go public.**
"""


def test_checklist_steps_take_the_file_title_as_their_section() -> None:
    items = docsrc.parse_checklist(CHECKLIST)
    assert [(i.section, i.done) for i in items] == [
        ("Alpha Game: going public", False),
        ("Alpha Game: going public", True),
        ("Later", False),
    ]
    assert items[0].text == "**1. Look at staging.** Play it on a phone too."
