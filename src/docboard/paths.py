"""Where the board keeps its own state: one folder per user, never the code's checkout.

A checkout is where the code changes: a branch checked out there (or a tool install, where the
package lives under site-packages) must never decide which database the live board uses. So the
database, the config and the private terms live in the user's data folder, the same for every
checkout, worktree and install; `DOCBOARD_HOME` moves all of them at once (tests, a dev board).
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path

APP = "docboard"


class LegacyDataError(RuntimeError):
    """The board's files are still in the checkout, from before they moved to the data folder."""


def data_dir(
    env: Mapping[str, str] | None = None, platform: str | None = None, home: Path | None = None
) -> Path:
    env = os.environ if env is None else env
    platform = platform or sys.platform
    home = home or Path.home()
    if env.get("DOCBOARD_HOME"):
        return Path(env["DOCBOARD_HOME"]).expanduser()
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP
    xdg = env.get("XDG_DATA_HOME")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg) / APP
    return home / ".local" / "share" / APP


def _checkout() -> Path | None:
    """The source checkout this package runs from, if it is one (not under a tool install)."""
    root = Path(__file__).resolve().parents[2]
    if (root / "pyproject.toml").is_file() and (root / "src" / APP).is_dir():
        return root
    return None


# Used only to notice the board's files left behind in the checkout, never to place data.
LEGACY_ROOT = _checkout()
LEGACY = {"db": "data/board.db", "config": "config/projects.toml",
          "terms": "config/private-identifiers.txt"}  # fmt: skip


def config_path(flag: str | None = None) -> tuple[Path, str]:
    """The config file and how it was chosen: --config, then $DOCBOARD_CONFIG, then the data
    folder's projects.toml."""
    if flag:
        return Path(flag).expanduser(), "--config"
    if os.environ.get("DOCBOARD_CONFIG"):
        return Path(os.environ["DOCBOARD_CONFIG"]).expanduser(), "$DOCBOARD_CONFIG"
    return data_dir() / "projects.toml", "the data folder"


def db_path(flag: str | None = None) -> Path:
    """The board's database: --db, else the data folder's board.db (its lock and backups sit
    beside it)."""
    return Path(flag).expanduser() if flag else data_dir() / "board.db"


def terms_path(flag: str | None = None) -> Path:
    """The private terms the privacy scan looks for (the data folder, so every worktree's
    pre-push hook has them)."""
    return Path(flag).expanduser() if flag else data_dir() / "private-identifiers.txt"


def legacy(kind: str) -> Path | None:
    """The checkout's old copy of the db, config or terms, if it is still there."""
    if LEGACY_ROOT is None:
        return None
    old = LEGACY_ROOT / LEGACY[kind]
    return old if old.exists() else None


def refuse_legacy(chosen: Path, kind: str) -> None:
    """Fails closed when the default file is missing but the checkout still holds the old one:
    starting an empty board there would hide the real one."""
    old = legacy(kind)
    if not chosen.exists() and old is not None:
        raise LegacyDataError(
            f"the board moved to {chosen.parent}, but {old} is still in the checkout: stop the"
            " page, then run `uv run docboard move-data`. Nothing was created."
        )


def serve_hint() -> str:
    """How to start the page from where the code is."""
    return command("serve")


def command(args: str) -> str:
    """A docboard command as the owner runs it from wherever the code is."""
    if LEGACY_ROOT is not None:
        return f"uv run --directory {shlex.quote(str(LEGACY_ROOT))} docboard {args}"
    return f"docboard {args}"
