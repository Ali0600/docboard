"""The board's own state: the last sync's cards, and the owner's moves on them.

Sources stay read-only; everything the owner does lives here, in a git-ignored SQLite file
(owner-only). A move is kept with the column the card's source gave at that moment and holds only
while the source still says so. When the source moves the card, the move is dropped and the card's
history says why, so a card never sits where the owner put it after the facts changed under it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from docboard.config import REPO_ROOT
from docboard.derive import COLUMNS, NEEDS_YOU
from docboard.sync import SyncResult, floor_problems
from docboard.textsim import jaccard

DEFAULT_DB = REPO_ROOT / "data" / "board.db"
HIDDEN = "hidden"
MOVABLE = (*COLUMNS, HIDDEN)
RENAME_SAME = 0.6
DOC_KINDS = ("idea", "roadmap")

# Frozen: a shipped migration is never edited. A change is a new entry at the end.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE card (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        kind TEXT NOT NULL,
        column_derived TEXT NOT NULL,
        sub_derived TEXT NOT NULL,
        sub_reason TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        event_at TEXT NOT NULL,
        source TEXT NOT NULL,
        badges TEXT NOT NULL,
        parent TEXT,
        children TEXT NOT NULL,
        decided TEXT NOT NULL,
        evidence TEXT NOT NULL,
        raised INTEGER NOT NULL,
        older INTEGER NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        gone_at TEXT
    );
    CREATE INDEX card_project ON card (project);
    CREATE TABLE override (
        card_id TEXT PRIMARY KEY,
        col TEXT NOT NULL,
        col_derived_when_set TEXT NOT NULL,
        set_at TEXT NOT NULL
    );
    CREATE TABLE sub_override (card_id TEXT PRIMARY KEY, sub TEXT NOT NULL, set_at TEXT NOT NULL);
    CREATE TABLE manual (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        sub TEXT NOT NULL,
        col TEXT NOT NULL,
        title TEXT NOT NULL,
        body TEXT NOT NULL,
        created_at TEXT NOT NULL,
        deleted_at TEXT
    );
    CREATE TABLE alias (
        old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, similarity REAL NOT NULL, at TEXT NOT NULL
    );
    CREATE TABLE event (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        actor TEXT NOT NULL,
        card_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        detail TEXT NOT NULL
    );
    CREATE INDEX event_card ON event (card_id);
    CREATE TABLE sync_run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project TEXT NOT NULL,
        started TEXT NOT NULL,
        status TEXT NOT NULL,
        counts TEXT NOT NULL,
        ref_sha TEXT,
        github TEXT,
        subs TEXT NOT NULL,
        degraded TEXT NOT NULL,
        message TEXT NOT NULL
    );
    CREATE VIEW board AS
    SELECT c.*,
        CASE WHEN o.col_derived_when_set = c.column_derived THEN o.col
             ELSE c.column_derived END AS col,
        COALESCE(s.sub, c.sub_derived) AS sub,
        (o.col_derived_when_set = c.column_derived) IS 1 AS moved,
        s.card_id IS NOT NULL AS sub_moved
    FROM card c
    LEFT JOIN override o ON o.card_id = c.id
    LEFT JOIN sub_override s ON s.card_id = c.id;
    """,
)

_CARD_FIELDS = (
    "kind", "column_derived", "sub_derived", "sub_reason", "title", "body", "event_at",
    "source", "badges", "parent", "children", "decided", "evidence", "raised", "older",
)  # fmt: skip


class FloorError(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("a source collapsed since the last sync: " + "; ".join(problems))
        self.problems = problems


class StoreError(ValueError):
    pass


@dataclass
class ApplyReport:
    added: int = 0
    updated: int = 0
    gone: int = 0
    back: int = 0
    renamed: int = 0
    followed_source: int = 0
    kept_frozen: list[str] = field(default_factory=list)


def _card_row(card: object) -> dict:
    c = card.__dict__
    return {
        "kind": c["kind"],
        "column_derived": c["column"],
        "sub_derived": c["sub"],
        "sub_reason": c["sub_reason"],
        "title": c["title"],
        "body": c["body"],
        "event_at": c["event_at"],
        "source": json.dumps(c["source"], ensure_ascii=False, sort_keys=True),
        "badges": json.dumps(c["badges"], ensure_ascii=False),
        "parent": c["parent"],
        "children": json.dumps(c["children"]),
        "decided": json.dumps(c["decided"], ensure_ascii=False),
        "evidence": c["suggested_evidence"],
        "raised": c["raised"],
        "older": int(c["older"]),
    }


class Store:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        self.path = path
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        self._migrate()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise StoreError(f"{self.path} is schema {version}, newer than this docboard knows")
        for i in range(version, len(MIGRATIONS)):
            with self.tx() as db:
                for stmt in _statements(MIGRATIONS[i]):
                    db.execute(stmt)
                db.execute(f"PRAGMA user_version = {i + 1}")

    def _event(self, at: str, actor: str, card_id: str, kind: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO event (at, actor, card_id, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (at, actor, card_id, kind, detail),
        )

    # --- sync ----------------------------------------------------------------------------

    def last_run(self, project: str, ok_only: bool = False) -> sqlite3.Row | None:
        sql = "SELECT * FROM sync_run WHERE project = ?"
        if ok_only:
            sql += " AND status = 'ok'"
        return self.db.execute(sql + " ORDER BY id DESC LIMIT 1", (project,)).fetchone()

    def apply(self, result: SyncResult, now: str) -> ApplyReport:
        """Writes one sync's cards in one transaction, or nothing when a source collapsed."""
        last = self.last_run(result.project, ok_only=True)
        problems = floor_problems(result.counts, json.loads(last["counts"])) if last else []
        if problems:
            self._run(result, now, "refused", "; ".join(problems))
            raise FloorError(problems)
        frozen = {"pr", "plan"} if any(d.startswith("gh") for d in result.degraded) else set()
        report = ApplyReport(kept_frozen=sorted(frozen))
        with self.tx():
            self._apply(result, now, frozen, report)
            self._run(result, now, "ok", "", in_tx=True)
        return report

    def _run(
        self, result: SyncResult, now: str, status: str, message: str, in_tx: bool = False
    ) -> None:
        sql = (
            "INSERT INTO sync_run (project, started, status, counts, ref_sha, github, subs,"
            " degraded, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        args = (result.project, now, status, json.dumps(result.counts), result.ref_sha,
                result.github, json.dumps(result.subs), json.dumps(result.degraded),
                message)  # fmt: skip
        if in_tx:
            self.db.execute(sql, args)
        else:
            with self.tx():
                self.db.execute(sql, args)

    def _apply(self, result: SyncResult, now: str, frozen: set[str], report: ApplyReport) -> None:
        db = self.db
        existing = {
            r["id"]: r
            for r in db.execute("SELECT * FROM card WHERE project = ?", (result.project,))
        }
        seen: set[str] = set()
        fresh: list[object] = []
        for card in result.cards:
            old = existing.get(card.id)
            if card.kind in frozen and old is not None:
                seen.add(card.id)  # gh is down: keep the last good PR and plan states
                continue
            row = _card_row(card)
            seen.add(card.id)
            if old is None:
                cols = ", ".join(_CARD_FIELDS)
                marks = ", ".join("?" for _ in _CARD_FIELDS)
                db.execute(
                    f"INSERT INTO card (id, project, {cols}, first_seen, last_seen)"  # noqa: S608
                    f" VALUES (?, ?, {marks}, ?, ?)",
                    (card.id, result.project, *(row[f] for f in _CARD_FIELDS), now, now),
                )
                report.added += 1
                fresh.append(card)
                continue
            sets = ", ".join(f"{f} = ?" for f in _CARD_FIELDS)
            db.execute(
                f"UPDATE card SET {sets}, last_seen = ?, gone_at = NULL WHERE id = ?",  # noqa: S608
                (*(row[f] for f in _CARD_FIELDS), now, card.id),
            )
            report.updated += 1
            if old["gone_at"] is not None:
                report.back += 1
                self._event(now, "sync", card.id, "back", "back in its source")
            if old["column_derived"] != row["column_derived"]:
                gone = db.execute(
                    "DELETE FROM override WHERE card_id = ? AND col_derived_when_set != ?"
                    " RETURNING col",
                    (card.id, row["column_derived"]),
                ).fetchone()
                detail = f"source moved it {old['column_derived']} → {row['column_derived']}"
                if gone is not None:
                    detail += f"; your move to {gone['col']} no longer applies"
                    report.followed_source += 1
                self._event(now, "sync", card.id, "source", detail)
        vanished = [
            r for i, r in existing.items()
            if i not in seen and r["gone_at"] is None and r["kind"] not in frozen
        ]  # fmt: skip
        self._renames(vanished, fresh, now, report)

    def _renames(self, vanished: list, fresh: list, now: str, report: ApplyReport) -> None:
        """A reworded doc item is the same card: carry the owner's moves over to its new id."""
        db = self.db
        taken: set[str] = set()
        for old in vanished:
            best, score = None, 0.0
            if old["kind"] in DOC_KINDS:
                for new in fresh:
                    if new.kind != old["kind"] or new.id in taken:
                        continue
                    s = jaccard(old["title"], new.title)
                    if s >= RENAME_SAME and s > score:
                        best, score = new, s
            if best is not None:
                taken.add(best.id)
                db.execute(
                    "INSERT OR REPLACE INTO alias (old_id, new_id, similarity, at)"
                    " VALUES (?, ?, ?, ?)",
                    (old["id"], best.id, score, now),
                )
                for table in ("override", "sub_override"):
                    db.execute(
                        f"UPDATE OR IGNORE {table} SET card_id = ? WHERE card_id = ?",  # noqa: S608
                        (best.id, old["id"]),
                    )
                db.execute("UPDATE event SET card_id = ? WHERE card_id = ?", (best.id, old["id"]))
                self._event(now, "sync", best.id, "renamed", f"was {old['title'][:120]!r}")
                db.execute("DELETE FROM card WHERE id = ?", (old["id"],))
                report.renamed += 1
            else:
                db.execute("UPDATE card SET gone_at = ? WHERE id = ?", (now, old["id"]))
                self._event(now, "sync", old["id"], "gone", "no longer in its source")
                report.gone += 1

    # --- reads ---------------------------------------------------------------------------

    def known(self, project: str) -> tuple[list[str], str | None]:
        """The sub-projects and GitHub repo the last good sync found."""
        run = self.last_run(project, ok_only=True)
        if run is None:
            return [], None
        return json.loads(run["subs"]), run["github"]

    def cards(self, project: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM board WHERE project = ? AND gone_at IS NULL", (project,)
        ))  # fmt: skip

    def gone(self, project: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM board WHERE project = ? AND gone_at IS NOT NULL ORDER BY gone_at DESC",
            (project,),
        ))  # fmt: skip

    def card(self, card_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM board WHERE id = ?", (card_id,)).fetchone()

    def events(self, card_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM event WHERE card_id = ? ORDER BY id DESC", (card_id,)
        ))  # fmt: skip

    def manual(self, project: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM manual WHERE project = ? AND deleted_at IS NULL ORDER BY created_at",
            (project,),
        ))  # fmt: skip

    def manual_card(self, card_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM manual WHERE id = ? AND deleted_at IS NULL", (card_id,)
        ).fetchone()

    # --- the owner's actions -------------------------------------------------------------

    def move(self, card_id: str, col: str, now: str, kind: str = "move") -> None:
        if col not in MOVABLE:
            raise StoreError(f"no column {col!r}")
        with self.tx():
            card = self.card(card_id)
            if card is None:
                raise StoreError(f"no card {card_id!r}")
            if col == card["column_derived"]:
                self.db.execute("DELETE FROM override WHERE card_id = ?", (card_id,))
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO override (card_id, col, col_derived_when_set, set_at)"
                    " VALUES (?, ?, ?, ?)",
                    (card_id, col, card["column_derived"], now),
                )
            self._event(now, "owner", card_id, kind, f"{card['col']} → {col}")

    def resolve(self, card_id: str, now: str) -> None:
        self.move(card_id, "done", now, kind="resolve")

    def dismiss(self, card_id: str, now: str) -> None:
        self.move(card_id, HIDDEN, now, kind="dismiss")

    def set_sub(self, card_id: str, sub: str, subs: list[str], now: str) -> None:
        if sub and sub not in subs:
            raise StoreError(f"no sub-project {sub!r}")
        with self.tx():
            card = self.card(card_id)
            if card is None:
                raise StoreError(f"no card {card_id!r}")
            if sub == card["sub_derived"]:
                self.db.execute("DELETE FROM sub_override WHERE card_id = ?", (card_id,))
            else:
                self.db.execute(
                    "INSERT OR REPLACE INTO sub_override (card_id, sub, set_at) VALUES (?, ?, ?)",
                    (card_id, sub, now),
                )
            self._event(now, "owner", card_id, "sub", f"{card['sub'] or 'root'} → {sub or 'root'}")

    def add_manual(self, project: str, sub: str, col: str, title: str, body: str, now: str) -> str:
        if col not in COLUMNS:
            raise StoreError(f"no column {col!r}")
        title = title.strip()[:200]
        if not title:
            raise StoreError("a card needs a title")
        card_id = "mc:" + uuid.uuid4().hex
        with self.tx():
            self.db.execute(
                "INSERT INTO manual (id, project, sub, col, title, body, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (card_id, project, sub, col, title, body.strip()[:8192], now),
            )
            self._event(now, "owner", card_id, "added", col)
        return card_id

    def move_manual(self, card_id: str, col: str, now: str) -> None:
        if col not in MOVABLE:
            raise StoreError(f"no column {col!r}")
        with self.tx():
            if self.manual_card(card_id) is None:
                raise StoreError(f"no card {card_id!r}")
            self.db.execute("UPDATE manual SET col = ? WHERE id = ?", (col, card_id))
            self._event(now, "owner", card_id, "move", col)

    def delete_manual(self, card_id: str, now: str) -> None:
        with self.tx():
            self.db.execute("UPDATE manual SET deleted_at = ? WHERE id = ?", (now, card_id))
            self._event(now, "owner", card_id, "deleted", "")


def _statements(script: str) -> list[str]:
    """A migration split into statements (none of ours holds a ';' inside a literal)."""
    return [s.strip() for s in script.split(";") if s.strip()]


__all__ = ["DEFAULT_DB", "HIDDEN", "MOVABLE", "NEEDS_YOU", "ApplyReport", "FloorError", "Store"]
