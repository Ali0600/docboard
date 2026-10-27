"""Card ids are frozen: these literals change only with ID_VERSION and a migration."""

from __future__ import annotations

from docboard import ids


def test_golden_ids() -> None:
    assert ids.ID_VERSION == "1"
    assert ids.backlog_id("demo", "A non-Neon body for the hub's bounce pads") == "bl:88923a07ad9a"
    assert ids.roadmap_id("demo", "game-a", "4. Polish", "Keyboard flow.") == "rm:1fe0036a9c59"
    assert ids.pr_id("acme/demo", 7) == "pr:acme/demo#7"
    assert ids.plan_id("toolu_1") == "pl:toolu_1"
    assert ids.needs_id("u-1", 2) == "ny:u-1:2"


def test_backlog_id_ignores_markdown_case_and_dates() -> None:
    a = ids.backlog_id("demo", "Pole swing for **Neon** (2026-10-25)")
    b = ids.backlog_id("demo", "pole swing for neon")
    assert a == b


def test_backlog_id_depends_on_project_and_title_only() -> None:
    assert ids.backlog_id("demo", "X") != ids.backlog_id("other", "X")
    assert ids.backlog_id("demo", "X") != ids.backlog_id("demo", "Y")


def test_roadmap_section_tail_added_later_keeps_the_id() -> None:
    before = ids.roadmap_id("demo", "g", "4. Polish and acceptance", "Keyboard flow, dashes.")
    merged = ids.roadmap_id(
        "demo", "g", "4. Polish and acceptance — merged in PR #147", "Keyboard flow, dashes."
    )
    shipped = ids.roadmap_id(
        "demo", "g", "4. Polish and acceptance (shipped 2026-10-25, #301)", "Keyboard flow, dashes."
    )
    assert before == merged == shipped


def test_roadmap_item_stem_keeps_the_id_when_the_tail_grows() -> None:
    a = ids.roadmap_id("demo", "g", "S", "one two three four five six seven eight nine ten")
    b = ids.roadmap_id(
        "demo", "g", "S", "one two three four five six seven eight nine ten, then more"
    )
    assert a == b
