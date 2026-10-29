"""Every test runs against its own empty data folder, never the owner's board.

The data folder is shared by every checkout and worktree (paths.py), so a test that fell back to
the default would read, or migrate, the live board. The private terms are read once, before
that, for the scan of this repo's own files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from docboard import ghsrc, paths, privacy


def _live_terms() -> list[str]:
    candidates = [paths.terms_path()]
    if paths.LEGACY_ROOT is not None:
        candidates.append(paths.LEGACY_ROOT / privacy.PRIVATE_LIST)
    return next((privacy.read_private_terms(p) for p in candidates if p.is_file()), [])


LIVE_TERMS = _live_terms()


@pytest.fixture(autouse=True)
def _isolated_data(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path_factory.mktemp("docboard-home")
    monkeypatch.setenv("DOCBOARD_HOME", str(home))
    monkeypatch.delenv("DOCBOARD_CONFIG", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths, "LEGACY_ROOT", None)
    monkeypatch.setattr(ghsrc, "_DEFAULT_BRANCH", {})  # each test's fake gh names its own
    return home


@pytest.fixture
def live_terms() -> list[str]:
    return list(LIVE_TERMS)
