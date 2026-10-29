"""`docboard move-data`: the one-time move of the board's files out of the code's checkout.

Run with the page stopped (it refuses otherwise): a page still running from the checkout would
go on writing to the old database, and those moves would be lost. The database is copied with
SQLite's backup, and the copy must match the original (integrity, schema, every count) before
anything is moved aside. Idempotent: once the data folder holds a board, it does nothing more.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from docboard import paths

_COUNTS = (
    "SELECT 'card', count(*) FROM card",
    "SELECT 'override', count(*) FROM override",
    "SELECT 'sub_override', count(*) FROM sub_override",
    "SELECT 'manual', count(*) FROM manual",
    "SELECT 'event', count(*) FROM event",
    "SELECT 'sync_run', count(*) FROM sync_run",
    "SELECT 'alias', count(*) FROM alias",
)


def snapshot(db: Path) -> dict[str, object]:
    """What must be the same after the copy: integrity, schema version, every table's count and
    each board's cards per column."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        out: dict[str, object] = {
            "integrity": con.execute("PRAGMA integrity_check").fetchone()[0],
            "version": con.execute("PRAGMA user_version").fetchone()[0],
        }
        for sql in _COUNTS:
            name, n = con.execute(sql).fetchone()
            out[name] = n
        out["columns"] = sorted(
            con.execute(
                "SELECT project, col, count(*) FROM board WHERE gone_at IS NULL GROUP BY 1, 2"
            ).fetchall()
        )
        return out
    finally:
        con.close()


def page_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2):  # noqa: S310
            return True
    except urllib.error.HTTPError:
        return True  # it answered
    except (urllib.error.URLError, OSError):
        return False


def _private_copy(src: Path, dst: Path) -> None:
    shutil.copyfile(src, dst)
    os.chmod(dst, 0o600)


def move_data(port: int, now: datetime | None = None, out=print) -> int:  # noqa: ANN001
    new_dir = paths.data_dir()
    new_db = new_dir / "board.db"
    old_db, old_cfg, old_terms = paths.legacy("db"), paths.legacy("config"), paths.legacy("terms")
    if new_db.exists():
        out(f"move-data: the board is already in {new_dir}; nothing to do")
        if old_db is not None:
            out(f"  the old {old_db} is still in the checkout; it is no longer read")
        return 0
    if old_db is None:
        out("move-data: no board in the checkout to move; `docboard sync` starts one in"
            f" {new_dir}")  # fmt: skip
        return 0
    if page_up(port):
        out(f"move-data: the page answers on port {port}: stop it first (Ctrl-C in its terminal),"
            " so no move you make lands in the old copy")  # fmt: skip
        return 2
    new_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(old_db)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = snapshot(old_db)
        tmp = new_db.with_suffix(".db.tmp")
        dst = sqlite3.connect(tmp)
        try:
            con.backup(dst)
        finally:
            dst.close()
    finally:
        con.close()
    os.chmod(tmp, 0o600)
    after = snapshot(tmp)
    if after != before or before["integrity"] != "ok":
        tmp.unlink()
        out(f"move-data: the copy does not match the original ({before} vs {after}); nothing"
            " was moved")  # fmt: skip
        return 1
    os.replace(tmp, new_db)
    for old, name in ((old_cfg, "projects.toml"), (old_terms, "private-identifiers.txt")):
        if old is not None and not (new_dir / name).exists():
            _private_copy(old, new_dir / name)
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    aside = old_db.parent / f"moved-{stamp}"
    aside.mkdir(exist_ok=True, mode=0o700)
    for old in (old_db, old_db.with_name("board.db-wal"), old_db.with_name("board.db-shm"),
                old_cfg, old_terms):  # fmt: skip
        if old is not None and old.exists():
            os.replace(old, aside / old.name)
    out(f"move-data: the board is now in {new_dir}")
    out(f"  {before['card']} cards, {before['override']} of your moves, {before['event']} events:"
        " the same in the copy")  # fmt: skip
    out(f"  the old files are in {aside} (git-ignored); delete them once the board looks right")
    return 0
