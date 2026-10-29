"""`docboard move-data`: the one-time move of the board's files out of the code's checkout.

Run with the page stopped (it refuses otherwise): a page still running from the checkout would
go on writing to the old database, and those moves would be lost. The database is copied with
SQLite's backup, and the copy must match the original (integrity, schema, every count) before
anything is moved aside. Idempotent: once the data folder holds a board, it does nothing more.
"""

from __future__ import annotations

import os
import socket
import sqlite3
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
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
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
    """Whether anything answers on the port, on IPv4 or IPv6, with no proxy in the way."""
    for host in ("127.0.0.1", "::1"):
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            continue
    return False


def _private_copy(src: Path, dst: Path) -> None:
    """A copy only its owner can read, from the moment it exists."""
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(src.read_bytes())


def _empty(db: Path) -> bool:
    """A board nothing was ever stored in (no cards, moves or cards of the owner's own)."""
    snap = snapshot(db)
    return not (snap["card"] or snap["override"] or snap["manual"])


def move_data(port: int, now: datetime | None = None, out=print) -> int:  # noqa: ANN001
    new_dir = paths.data_dir()
    new_db = new_dir / "board.db"
    old_db, old_cfg, old_terms = paths.legacy("db"), paths.legacy("config"), paths.legacy("terms")
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    if old_db is None and old_cfg is None and old_terms is None:
        out(f"move-data: nothing of the board is left in the checkout; it lives in {new_dir}")
        return 0
    if page_up(port):
        out(f"move-data: something answers on port {port}: stop the page first (Ctrl-C in its"
            " terminal), so no move you make lands in the old copy")  # fmt: skip
        return 2
    new_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if old_db is not None and new_db.exists():
        if not _empty(new_db):
            out(f"move-data: both {old_db} and {new_db} hold a board; nothing was moved. Keep"
                " the one you want (the old one is the board you used until now) and move the"
                " other away, then run this again")  # fmt: skip
            return 1
        os.replace(new_db, new_dir / f"board.db.empty-{stamp}")  # started by mistake, empty
    if old_db is not None:
        code = _copy_db(old_db, new_db, out)
        if code:
            return code
    for old, name in ((old_cfg, "projects.toml"), (old_terms, "private-identifiers.txt")):
        if old is not None and not (new_dir / name).exists():
            _private_copy(old, new_dir / name)
    root = paths.LEGACY_ROOT or new_dir
    aside = (old_db.parent if old_db is not None else root / "data") / f"moved-{stamp}"
    aside.mkdir(parents=True, exist_ok=True, mode=0o700)
    left = [old_cfg, old_terms]
    if old_db is not None:
        left += [old_db, old_db.with_name("board.db-wal"), old_db.with_name("board.db-shm")]
    for old in left:
        if old is not None and old.exists():
            os.replace(old, aside / old.name)
    out(f"move-data: the board now lives in {new_dir}")
    out(f"  the old files are in {aside} (git-ignored); delete them once the board looks right")
    return 0


def _copy_db(old_db: Path, new_db: Path, out) -> int:  # noqa: ANN001
    """Copies the database with SQLite's backup and checks the copy before anything moves."""
    con = sqlite3.connect(old_db)
    tmp = new_db.with_name("board.db.tmp")
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = snapshot(old_db)
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
        dst = sqlite3.connect(tmp)
        try:
            con.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # one file, no -wal or -shm left beside it
        finally:
            dst.close()
    finally:
        con.close()
    after = snapshot(tmp)
    for leftover in (tmp, tmp.with_name(tmp.name + "-wal"), tmp.with_name(tmp.name + "-shm")):
        if leftover != tmp and leftover.exists():
            leftover.unlink()
    if after != before or before["integrity"] != "ok":
        tmp.unlink()
        out(f"move-data: the copy does not match the original ({before} vs {after}); nothing"
            " was moved")  # fmt: skip
        return 1
    os.replace(tmp, new_db)
    out(f"  {before['card']} cards, {before['override']} of your moves, {before['event']} events:"
        " the same in the copy")  # fmt: skip
    return 0
