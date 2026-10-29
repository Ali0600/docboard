from __future__ import annotations

import pytest

from docboard.attribution import Attributor
from docboard.config import ConfigError


def att() -> Attributor:
    return Attributor.build(
        "games",
        ["plus-one", "neon-breakers", "neon-runner"],
        {"plus-one": "+1 Gravity Every Second", "neon-breakers": "Neon Breakers"},
        {"plus-one": ("+1 Gravity", "pog")},
        {"plus-one": ("game/pog-",)},
    )


def test_text_names_exactly_one_sub_project() -> None:
    a = att()
    assert a.by_text("+1 Gravity: reward cards") == "plus-one"
    assert a.by_text("Neon Breakers' city round") == "neon-breakers"
    assert a.by_text("Neon Breakers and Neon Runner share a sky") is None
    assert a.by_text("Neon lights everywhere") is None
    assert a.by_text("pogo stick") is None  # whole words only
    assert a.by_text("pog-launch") == "plus-one"


def test_branch_longest_prefix_wins() -> None:
    a = att()
    assert a.by_branch("game/pog-store") == "plus-one"
    assert a.by_branch("neon-breakers-city") == "neon-breakers"
    assert a.by_branch("main") is None


def test_path_inside_a_worktree() -> None:
    a = att()
    assert a.by_path(".claude/worktrees/w1/games/neon-runner/src/x.luau") == "neon-runner"
    assert a.by_path("games/unknown/x") is None
    assert a.by_path("libs/ui/x") is None


def test_file_vote_needs_hits_and_a_share() -> None:
    a = att()
    three = ["games/plus-one/a", "games/plus-one/b", "games/plus-one/c", "libs/x"]
    assert a.by_files(three) == "plus-one"
    assert a.by_files(three[:2] + ["libs/x"]) is None  # too few hits
    assert a.by_files(three[:2] + ["libs/x"], min_hits=1) == "plus-one"
    assert a.by_files(three + ["libs/y", "libs/z"]) is None  # 3 of 6 is not 60%


def test_an_alias_shared_by_two_sub_projects_is_refused() -> None:
    with pytest.raises(ConfigError, match="names both"):
        Attributor.build("games", ["a", "b"], {}, {"a": ("Neon",), "b": ("neon",)}, {})


def test_aliases_for_an_unknown_sub_project_are_refused() -> None:
    with pytest.raises(ConfigError, match="unknown"):
        Attributor.build("games", ["a"], {}, {"z": ("Zed",)}, {})


def test_an_agent_prefixed_branch_matches() -> None:
    a = att()
    assert a.by_branch("codex/neon-breakers-city") == "neon-breakers"
    assert a.by_branch("claude/game/pog-store") == "plus-one"
    assert a.by_branch("codex/unrelated-work") is None
    assert a.by_branch("main") is None


def test_how_many_sub_projects_a_change_spans() -> None:
    a = att()
    paths = ["games/plus-one/a", "games/neon-runner/b", "libs/x", "games/plus-one/c"]
    assert a.spread(paths) == 2
    assert a.spread([*paths, "games/neon-breakers/d"]) == 3
