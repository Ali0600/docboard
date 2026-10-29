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
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from docboard.derive import COLUMNS, DONE, NEEDS_YOU, RECENT
from docboard.sync import GH_FLOOR_KEYS, SyncResult, ci_degraded, floor_drops, gh_degraded
from docboard.textsim import jaccard

HIDDEN = "hidden"
MOVABLE = (*COLUMNS, HIDDEN)
RENAME_SAME = 0.6
BACKUP_EVERY = timedelta(hours=24)
KEEP_DAILY, KEEP_MIGRATION = 7, 5
NEW_WINDOW = timedelta(hours=24)  # "new" before the owner ever marked the board seen
DOC_KINDS = ("idea", "roadmap")
# Kinds that wait on the owner: while one sits in Needs you, it stays there even after its
# transcript is deleted (Claude Code removes old transcripts after 30 days by default).
ASK_KINDS = ("needs", "question", "wrap", "plan")

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
    # Every column and table the 2026-10-28 run needs, in one step, so the running page has to
    # be restarted once. The view's `c.*` is expanded when a query is prepared, so it needs no
    # change.
    """
    ALTER TABLE sync_run ADD COLUMN timings TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE sync_run ADD COLUMN warnings TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE sync_run ADD COLUMN sub_names TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE card ADD COLUMN raised_at TEXT NOT NULL DEFAULT '';
    UPDATE card SET raised_at = event_at;
    ALTER TABLE card ADD COLUMN source_gone_at TEXT;
    ALTER TABLE card ADD COLUMN reopened_at TEXT;
    ALTER TABLE card ADD COLUMN reopened_was TEXT;
    ALTER TABLE card ADD COLUMN moved_from TEXT;
    CREATE TABLE visit (project TEXT PRIMARY KEY, last_seen_at TEXT NOT NULL, prev_seen_at TEXT);
    CREATE TABLE batch (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        kind TEXT NOT NULL,
        at TEXT NOT NULL,
        undone_at TEXT
    );
    CREATE TABLE batch_item (
        batch_id TEXT NOT NULL,
        card_id TEXT NOT NULL,
        prev TEXT,
        after TEXT,
        PRIMARY KEY (batch_id, card_id)
    );
    CREATE INDEX batch_project ON batch (project, at);
    CREATE TABLE session (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        title TEXT NOT NULL,
        decided TEXT NOT NULL,
        first_at TEXT,
        last_at TEXT
    );
    """,
    # A session's answers kept per board: two boards noting one session no longer overwrite
    # each other (the review of 2026-10-28).
    """
    CREATE TABLE session_note (
        id TEXT NOT NULL,
        project TEXT NOT NULL,
        title TEXT NOT NULL,
        decided TEXT NOT NULL,
        first_at TEXT,
        last_at TEXT,
        PRIMARY KEY (id, project)
    );
    INSERT INTO session_note SELECT id, project, title, decided, first_at, last_at FROM session;
    DROP TABLE session;
    ALTER TABLE session_note RENAME TO session;
    """,
)

_CARD_FIELDS = (
    "kind", "column_derived", "sub_derived", "sub_reason", "title", "body", "event_at",
    "source", "badges", "parent", "children", "decided", "evidence", "raised", "older",
    "raised_at",
)  # fmt: skip


class FloorError(RuntimeError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("a source collapsed since the last sync: " + "; ".join(problems))
        self.problems = problems


def _ts(value: str | None) -> datetime | None:
    """A stored timestamp as an aware datetime; None when it is empty or not a timestamp."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


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
    accepted: list[str] = field(default_factory=list)  # drops the owner accepted
    duplicates: list[str] = field(default_factory=list)  # ids repeated within this sync
    elsewhere: list[str] = field(default_factory=list)  # ids another board derives too
    adopted: int = 0  # cards moved here from another board
    reopened: int = 0  # raised again after the owner resolved or dismissed them
    kept_open: int = 0  # open asks whose transcript is gone
    handed_over: int = 0  # cards another board now owns, moved there

    def warnings(self) -> list[str]:
        out = []
        if self.duplicates:
            out.append("ids repeated in one sync (the first kept): " + ", ".join(self.duplicates))
        if self.elsewhere:
            out.append("ids another board also derives (left there): " + ", ".join(self.elsewhere))
        return out


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
        "raised_at": c.get("raised_at") or c["event_at"],
    }


class Store:
    def __init__(self, path: Path) -> None:
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
        if 0 < version < len(MIGRATIONS):
            # The owner's moves exist only here: a copy before any schema change.
            backup(self.db, self.path, f"before-v{len(MIGRATIONS)}", datetime.now(UTC))
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

    def backup_if_due(self, now: datetime) -> Path | None:
        """A daily copy, made at most once a day (when the newest is over a day old)."""
        folder = self.path.parent / "backups"
        newest = max((p.stat().st_mtime for p in folder.glob("board-*-daily.db")), default=None)
        if newest is not None and now.timestamp() - newest < BACKUP_EVERY.total_seconds():
            return None
        return backup(self.db, self.path, "daily", now)

    # --- sync ----------------------------------------------------------------------------

    def last_run(self, project: str, ok_only: bool = False) -> sqlite3.Row | None:
        sql = "SELECT * FROM sync_run WHERE project = ?"
        if ok_only:
            sql += " AND status = 'ok'"
        return self.db.execute(sql + " ORDER BY id DESC LIMIT 1", (project,)).fetchone()

    def _baseline(self, project: str) -> dict[str, int] | None:
        """The counts a sync is compared with: the last good run's, except the PR counts, which
        come from the last good run that gh answered (an outage's zero never becomes a
        baseline)."""
        runs = self.db.execute(
            "SELECT counts, degraded FROM sync_run WHERE project = ? AND status = 'ok'"
            " ORDER BY id DESC LIMIT 500",
            (project,),
        ).fetchall()
        if not runs:
            return None
        base = dict(json.loads(runs[0]["counts"]))
        answered = next((r for r in runs if not gh_degraded(json.loads(r["degraded"]))), None)
        for key in GH_FLOOR_KEYS:
            if answered is None:
                base.pop(key, None)
            else:
                base[key] = json.loads(answered["counts"]).get(key, 0)
        return base

    def _refused_keys(self, project: str) -> set[str] | None:
        """The keys the newest refusal since the last good run named (a failed run in between
        does not hide it), or None when nothing was refused since."""
        ok = self.last_run(project, ok_only=True)
        row = self.db.execute(
            "SELECT message FROM sync_run WHERE project = ? AND status = 'refused' AND id > ?"
            " ORDER BY id DESC LIMIT 1",
            (project, ok["id"] if ok else 0),
        ).fetchone()
        if row is None:
            return None
        return {p.split(":", 1)[0].strip() for p in row["message"].split("; ") if ":" in p}

    def apply(self, result: SyncResult, now: str, accept_drop: bool = False) -> ApplyReport:
        """Writes one sync's cards in one transaction, or nothing when a source collapsed.

        `accept_drop` stores a sync whose drop the owner says is real (a trimmed Backlog,
        transcripts cleaned up). When a refusal came first, only the drops it named are
        accepted, so accepting what the page showed never also accepts another collapse.
        """
        gh_down = gh_degraded(result.degraded)
        baseline = self._baseline(result.project)
        skip = GH_FLOOR_KEYS if gh_down else ()
        drops = floor_drops(result.counts, baseline, skip) if baseline else {}
        accepted: dict[str, str] = {}
        if drops and accept_drop:
            named = self._refused_keys(result.project)
            accepted = {k: v for k, v in drops.items() if named is None or k in named}
            if len(accepted) < len(drops):
                accepted = {}  # a drop no refusal showed: refuse, naming every drop this time,
                # so the owner sees them all and the next accept takes them in one step
            else:
                drops = {}
        if drops:
            problems = list(drops.values())
            self._run(result, now, "refused", "; ".join(problems))
            raise FloorError(problems)
        frozen = {"pr", "plan"} if gh_down else set()
        if ci_degraded(result.degraded):
            frozen.add("ci")  # the runs could not be read: keep the CI cards as they were
        report = ApplyReport(kept_frozen=sorted(frozen), accepted=list(accepted.values()))
        message = ("accepted drop: " + "; ".join(accepted.values())) if accepted else ""
        with self.tx():
            self._apply(result, now, frozen, report)
            # This board's notes are what this sync found: an answer a later plan took leaves.
            self.db.execute("DELETE FROM session WHERE project = ?", (result.project,))
            for sid, note in result.session_notes.items():
                self.db.execute(
                    "INSERT OR REPLACE INTO session (id, project, title, decided, first_at,"
                    " last_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (sid, result.project, note.get("title", ""),
                     json.dumps(note.get("decided", []), ensure_ascii=False),
                     note.get("first_at"), note.get("last_at")),
                )  # fmt: skip
            self._run(result, now, "ok", message, warnings=report.warnings(), in_tx=True)
        return report

    def record_failed(
        self, project: str, now: str, message: str, result: SyncResult | None = None
    ) -> None:
        """A sync that crashed: nothing of it is stored but this row, which the page shows and
        which resets the page's wait before it tries again."""
        with self.tx():
            self.db.execute(
                "INSERT INTO sync_run (project, started, status, counts, ref_sha, github, subs,"
                " degraded, message, timings, warnings, sub_names)"
                " VALUES (?, ?, 'failed', ?, ?, ?, ?, ?, ?, ?, '[]', '{}')",
                (
                    project,
                    now,
                    json.dumps(result.counts if result else {}),
                    result.ref_sha if result else None,
                    result.github if result else None,
                    json.dumps(result.subs if result else []),
                    json.dumps(result.degraded if result else []),
                    message,
                    json.dumps(result.timings if result else {}),
                ),
            )

    def _run(
        self,
        result: SyncResult,
        now: str,
        status: str,
        message: str,
        warnings: Iterable[str] = (),
        in_tx: bool = False,
    ) -> None:
        sql = (
            "INSERT INTO sync_run (project, started, status, counts, ref_sha, github, subs,"
            " degraded, message, timings, warnings, sub_names)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        args = (result.project, now, status, json.dumps(result.counts), result.ref_sha,
                result.github, json.dumps(result.subs), json.dumps(result.degraded),
                message, json.dumps(result.timings),
                json.dumps([*result.warnings, *warnings], ensure_ascii=False),
                json.dumps(result.sub_names, ensure_ascii=False))  # fmt: skip
        if in_tx:
            self.db.execute(sql, args)
        else:
            with self.tx():
                self.db.execute(sql, args)

    def _apply(self, result: SyncResult, now: str, frozen: set[str], report: ApplyReport) -> None:
        db = self.db
        # A card is first seen when it is stored, not when its sync started: a "Mark all seen"
        # made while the sync was reading must not hide what it brings.
        stored = datetime.now(UTC).isoformat()
        existing = {
            r["id"]: r
            for r in db.execute("SELECT * FROM board WHERE project = ?", (result.project,))
        }
        seen: set[str] = set()
        fresh: list[object] = []
        for card in result.cards:
            if card.id in seen:
                report.duplicates.append(card.id)  # a parser emitted one id twice: keep the first
                continue
            seen.add(card.id)
            old = existing.get(card.id)
            adopted = False
            if old is None:
                other = db.execute("SELECT * FROM card WHERE id = ?", (card.id,)).fetchone()
                if other is not None:
                    if other["gone_at"] is None:
                        # Another board still derives it: it stays there (boards that both
                        # derive one id would otherwise trade it on every sync). Once that board
                        # stops deriving it, this one takes it over.
                        report.elsewhere.append(card.id)
                        continue
                    self._adopt(other, result, now, report)
                    old, adopted = other, True
            if card.kind in frozen and old is not None:
                continue  # gh is down: keep the last good PR and plan states
            row = _card_row(card)
            if old is None:
                cols = ", ".join(_CARD_FIELDS)
                marks = ", ".join("?" for _ in _CARD_FIELDS)
                db.execute(
                    f"INSERT INTO card (id, project, {cols}, first_seen, last_seen)"  # noqa: S608
                    f" VALUES (?, ?, {marks}, ?, ?)",
                    (card.id, result.project, *(row[f] for f in _CARD_FIELDS), stored, now),
                )
                report.added += 1
                fresh.append(card)
                continue
            sets = ", ".join(f"{f} = ?" for f in _CARD_FIELDS)
            db.execute(
                f"UPDATE card SET {sets}, last_seen = ?, gone_at = NULL, source_gone_at = NULL"  # noqa: S608
                " WHERE id = ?",
                (*(row[f] for f in _CARD_FIELDS), now, card.id),
            )
            report.updated += 1
            if old["gone_at"] is not None and not adopted:
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
            self._raised_again(old, row, card.id, now, report)
        vanished = [
            r for i, r in existing.items()
            if i not in seen and r["gone_at"] is None and r["kind"] not in frozen
        ]  # fmt: skip
        vanished = [r for r in vanished if not self._hand_over(r, result, now, report)]
        vanished = [r for r in vanished if not self._keep_open(r, result, now, report)]
        self._renames(vanished, fresh, now, report)
        # An ask kept open after its transcript went, which the owner then put back in Needs you
        # (an undo, a move): open again, not gone.
        for r in db.execute(
            "SELECT * FROM board WHERE project = ? AND gone_at IS NOT NULL"
            " AND source_gone_at IS NOT NULL AND col = ?",
            (result.project, NEEDS_YOU),
        ).fetchall():
            if r["id"] not in seen and self._keep_open(r, result, now, report):
                db.execute("UPDATE card SET gone_at = NULL WHERE id = ?", (r["id"],))
                self._event(now, "sync", r["id"], "back", "back in Needs you, kept open")

    def _adopt(self, other: sqlite3.Row, result: SyncResult, now: str, report: ApplyReport) -> None:
        """A card another board held moves here, with the owner's moves and its history."""
        db = self.db
        db.execute(
            "UPDATE card SET project = ?, moved_from = ? WHERE id = ?",
            (result.project, other["project"], other["id"]),
        )
        detail = f"moved here from {other['project']}"
        sub = db.execute(
            "SELECT sub FROM sub_override WHERE card_id = ?", (other["id"],)
        ).fetchone()
        if sub is not None and sub["sub"] and sub["sub"] not in result.subs:
            db.execute("DELETE FROM sub_override WHERE card_id = ?", (other["id"],))
            detail += f"; your game {sub['sub']!r} is not one of this board's"
        self._event(now, "sync", other["id"], "moved", detail)
        report.adopted += 1

    def _raised_again(
        self, old: sqlite3.Row, row: dict, card_id: str, now: str, report: ApplyReport
    ) -> None:
        """A later raise of an ask: it reopens a card the owner resolved or dismissed before it
        was raised again; otherwise the history notes it."""
        new_at, old_at = _ts(row["raised_at"]), _ts(old["raised_at"] or old["event_at"])
        if new_at is None or (old_at is not None and new_at <= old_at):
            return
        if row["column_derived"] != NEEDS_YOU:
            return
        when = row["raised_at"][:10]
        ov = self.db.execute("SELECT * FROM override WHERE card_id = ?", (card_id,)).fetchone()
        set_at = _ts(ov["set_at"]) if ov is not None else None
        if (
            ov is not None
            and ov["col"] in (DONE, HIDDEN)
            and set_at is not None
            and set_at < new_at
        ):
            self.db.execute("DELETE FROM override WHERE card_id = ?", (card_id,))
            self.db.execute(
                "UPDATE card SET reopened_at = ?, reopened_was = ? WHERE id = ?",
                (now, ov["col"], card_id),
            )
            verb = "resolved" if ov["col"] == DONE else "dismissed"
            self._event(
                now, "sync", card_id, "reopened", f"raised again {when} after you {verb} it"
            )
            report.reopened += 1
        elif row["raised"] > old["raised"]:
            self._event(now, "sync", card_id, "raised", f"raised again {when} ({row['raised']}×)")

    def _hand_over(self, r: sqlite3.Row, result: SyncResult, now: str, report: ApplyReport) -> bool:
        """A card routing now gives another board moves there at once, with the owner's moves,
        rather than going gone here and waiting for that board's next sync."""
        target = result.handover.get(r["id"])
        if target is None or target == result.project:
            return False
        self.db.execute(
            "UPDATE card SET project = ?, moved_from = ? WHERE id = ?",
            (target, result.project, r["id"]),
        )
        self._event(now, "sync", r["id"], "moved", f"moved to {target}: its work is there")
        report.handed_over += 1
        return True

    def _keep_open(self, r: sqlite3.Row, result: SyncResult, now: str, report: ApplyReport) -> bool:
        """An ask still waiting on the owner whose session is no longer read (its transcript was
        deleted) stays in Needs you until the owner acts on it."""
        if r["kind"] not in ASK_KINDS or r["col"] != NEEDS_YOU:
            return False
        session = json.loads(r["source"]).get("session")
        if not session or session in result.sessions:
            return False  # its session is still read: it left for a reason (an answer)
        if r["source_gone_at"] is None:
            self.db.execute("UPDATE card SET source_gone_at = ? WHERE id = ?", (now, r["id"]))
            self._event(
                now, "sync", r["id"], "source-gone",
                "its transcript is gone; it stays in Needs you until you act on it",
            )  # fmt: skip
        # No derive sees it any more, so its Older fold is kept up to date here.
        raised, at = _ts(r["raised_at"] or r["event_at"]), _ts(now)
        if raised is not None and at is not None:
            older = int(raised < at - RECENT)
            if older != r["older"]:
                self.db.execute("UPDATE card SET older = ? WHERE id = ?", (older, r["id"]))
        report.kept_open += 1
        return True

    def _renames(self, vanished: list, fresh: list, now: str, report: ApplyReport) -> None:
        """A reworded doc item is the same card: carry the owner's moves over to its new id."""
        db = self.db
        # Every (vanished, fresh) pair scored at once and matched best first, so when two
        # bullets share a title the one whose text also matches wins, whichever comes first.
        pairs = []
        for i, old in enumerate(vanished):
            if old["kind"] not in DOC_KINDS:
                continue
            for j, new in enumerate(fresh):
                if new.kind != old["kind"]:
                    continue
                s = jaccard(old["title"], new.title)
                if s >= RENAME_SAME:
                    pairs.append(((s, jaccard(old["body"] or "", new.body or "")), i, j))
        pairs.sort(key=lambda p: (p[0], -p[1], -p[2]), reverse=True)
        match: dict[int, tuple[object, tuple[float, float]]] = {}
        taken: set[int] = set()
        for key, i, j in pairs:
            if i not in match and j not in taken:
                match[i] = (fresh[j], key)
                taken.add(j)
        for i, old in enumerate(vanished):
            best, key = match.get(i, (None, None))
            if best is not None:
                score = key[0]
                db.execute(
                    "INSERT OR REPLACE INTO alias (old_id, new_id, similarity, at)"
                    " VALUES (?, ?, ?, ?)",
                    (old["id"], best.id, score, now),
                )
                detail = f"was {old['title'][:120]!r}"
                ov = db.execute("SELECT * FROM override WHERE card_id = ?", (old["id"],)).fetchone()
                if ov is not None and ov["col_derived_when_set"] != best.column:
                    # Reworded and moved by its source at once: like any source move, it drops
                    # the owner's move rather than carry it over to wait for a later undo.
                    db.execute("DELETE FROM override WHERE card_id = ?", (old["id"],))
                    detail += (
                        f"; its source also moved it {ov['col_derived_when_set']} → {best.column},"
                        f" so your move to {ov['col']} no longer applies"
                    )
                    report.followed_source += 1
                for table in ("override", "sub_override"):
                    db.execute(
                        f"UPDATE OR IGNORE {table} SET card_id = ? WHERE card_id = ?",  # noqa: S608
                        (best.id, old["id"]),
                    )
                db.execute("UPDATE event SET card_id = ? WHERE card_id = ?", (best.id, old["id"]))
                db.execute(  # the same card reworded: not new
                    "UPDATE card SET first_seen = ? WHERE id = ?", (old["first_seen"], best.id)
                )
                self._event(now, "sync", best.id, "renamed", detail)
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

    def renamed_to(self, card_id: str, hops: int = 5) -> str | None:
        """The card an old id became through renames (followed a few steps), or None."""
        seen = {card_id}
        for _ in range(hops):
            row = self.db.execute(
                "SELECT new_id FROM alias WHERE old_id = ?", (card_id,)
            ).fetchone()
            if row is None or row["new_id"] in seen:
                break
            card_id = row["new_id"]
            seen.add(card_id)
            if self.card(card_id) is not None:
                return card_id
        return None

    def session_notes(self, session_id: str) -> list[sqlite3.Row]:
        """Each board's note of one session (its title, the answers no plan there took)."""
        return list(self.db.execute(
            "SELECT * FROM session WHERE id = ? ORDER BY project", (session_id,)
        ))  # fmt: skip

    def session_cards(self, session_id: str) -> list[sqlite3.Row]:
        """Every board's cards from one session, its PRs and plans included, gone ones too."""
        return list(self.db.execute(
            "SELECT * FROM board WHERE json_extract(source, '$.session') = ?"
            " ORDER BY gone_at IS NOT NULL, project, event_at",
            (session_id,),
        ))  # fmt: skip

    def seen_mark(self, project: str) -> str | None:
        row = self.db.execute(
            "SELECT last_seen_at FROM visit WHERE project = ?", (project,)
        ).fetchone()
        return row["last_seen_at"] if row else None

    def new_since(self, project: str, now: datetime) -> datetime:
        """Cards first seen after this are new: the owner's last "Mark all seen", or a day."""
        mark = _ts(self.seen_mark(project))
        return mark if mark is not None else now - NEW_WINDOW

    def summary(self, project: str, sub: str | None, now: datetime) -> tuple[int, int]:
        """(asks in Needs you from the last 14 days, new cards) on the board or one game, counted
        as the page's chips count them: top-level cards and the owner's own, none hidden."""
        since = self.new_since(project, now)
        rows = [(r["col"], bool(r["older"]), r["first_seen"], r["sub"])
                for r in self.cards(project) if r["parent"] is None]  # fmt: skip
        rows += [(m["col"], False, m["created_at"], m["sub"]) for m in self.manual(project)]
        needs = new = 0
        for col, older, first_seen, card_sub in rows:
            if sub is not None and card_sub != sub:
                continue
            if col == NEEDS_YOU and not older:
                needs += 1
            first = _ts(first_seen)
            if col != HIDDEN and first is not None and first > since:
                new += 1
        return needs, new

    def mark_seen(self, project: str, now: str) -> None:
        """The owner's "Mark all seen": cards first seen after this are new."""
        with self.tx():
            self.db.execute(
                "INSERT INTO visit (project, last_seen_at, prev_seen_at) VALUES (?, ?, NULL)"
                " ON CONFLICT(project) DO UPDATE SET prev_seen_at = last_seen_at,"
                " last_seen_at = excluded.last_seen_at",
                (project, now),
            )

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

    def _override(self, card_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT col, col_derived_when_set, set_at FROM override WHERE card_id = ?", (card_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def _move(self, card_id: str, col: str, now: str, kind: str) -> tuple[dict | None, dict | None]:
        """Moves one card inside an open transaction; returns its override before and after."""
        if col not in MOVABLE:
            raise StoreError(f"no column {col!r}")
        card = self.card(card_id)
        if card is None:
            raise StoreError(f"no card {card_id!r}")
        before = self._override(card_id)
        if col == card["column_derived"]:
            self.db.execute("DELETE FROM override WHERE card_id = ?", (card_id,))
        else:
            self.db.execute(
                "INSERT OR REPLACE INTO override (card_id, col, col_derived_when_set, set_at)"
                " VALUES (?, ?, ?, ?)",
                (card_id, col, card["column_derived"], now),
            )
        self._event(now, "owner", card_id, kind, f"{card['col']} → {col}")
        return before, self._override(card_id)

    def move(self, card_id: str, col: str, now: str, kind: str = "move") -> None:
        with self.tx():
            self._move(card_id, col, now, kind)

    def move_batch(
        self, project: str, ids: Iterable[str], col: str, kind: str, now: str
    ) -> str | None:
        """Moves several cards as one batch that can be undone; returns its id, or None when
        none of the ids is a card of this project."""
        batch_id = uuid.uuid4().hex
        with self.tx():
            items = []
            for card_id in dict.fromkeys(ids):
                card = self.card(card_id)
                if card is None or card["project"] != project or card["col"] == col:
                    continue  # not this board's, or already there: nothing to undo later
                before, after = self._move(card_id, col, now, kind)
                items.append((card_id, before, after))
            if not items:
                return None
            self.db.execute(
                "INSERT INTO batch (id, project, kind, at) VALUES (?, ?, ?, ?)",
                (batch_id, project, kind, now),
            )
            self.db.executemany(
                "INSERT INTO batch_item (batch_id, card_id, prev, after) VALUES (?, ?, ?, ?)",
                [(batch_id, c, _js(b), _js(a)) for c, b, a in items],
            )
        return batch_id

    def undo_batch(self, batch_id: str, project: str, now: str) -> tuple[int, int]:
        """Puts each card of a batch back as it was, unless something moved it since (a sync
        following its source, a reopen, another move of yours): those are left as they are.
        Returns (restored, kept)."""
        restored = kept = 0
        with self.tx():
            batch = self.db.execute("SELECT * FROM batch WHERE id = ?", (batch_id,)).fetchone()
            if batch is None or batch["project"] != project:
                raise StoreError("no such batch on this board")
            if batch["undone_at"] is not None:
                raise StoreError("that batch was already undone")
            for item in self.db.execute(
                "SELECT * FROM batch_item WHERE batch_id = ?", (batch_id,)
            ).fetchall():
                card_id = item["card_id"]
                if _js(self._override(card_id)) != item["after"]:
                    kept += 1
                    continue
                prev = json.loads(item["prev"]) if item["prev"] else None
                if prev is None:
                    self.db.execute("DELETE FROM override WHERE card_id = ?", (card_id,))
                else:
                    self.db.execute(
                        "INSERT OR REPLACE INTO override (card_id, col, col_derived_when_set,"
                        " set_at) VALUES (?, ?, ?, ?)",
                        (card_id, prev["col"], prev["col_derived_when_set"], prev["set_at"]),
                    )
                self._event(now, "owner", card_id, "undo", f"undid the {batch['kind']}")
                card = self.card(card_id)
                if card["gone_at"] and card["source_gone_at"] and card["col"] == NEEDS_YOU:
                    # A sync let it go once it was resolved; back in Needs you, it is kept open.
                    self.db.execute("UPDATE card SET gone_at = NULL WHERE id = ?", (card_id,))
                restored += 1
            self.db.execute("UPDATE batch SET undone_at = ? WHERE id = ?", (now, batch_id))
        return restored, kept

    def last_batch(self, project: str) -> sqlite3.Row | None:
        """The owner's newest batch on this board that was not undone."""
        return self.db.execute(
            "SELECT * FROM batch WHERE project = ? AND undone_at IS NULL ORDER BY at DESC, rowid"
            " DESC LIMIT 1",
            (project,),
        ).fetchone()

    def batch_cards(self, batch_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT b.* FROM batch_item i JOIN board b ON b.id = i.card_id WHERE i.batch_id = ?",
            (batch_id,),
        ))  # fmt: skip

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


def backup(db: sqlite3.Connection, db_path: Path, reason: str, now: datetime) -> Path:
    """A consistent copy of the database (SQLite's backup), owner-only, beside it in backups/,
    and the old ones pruned: 7 daily copies and 5 taken before a migration are kept."""
    folder = db_path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = f"board-{now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}-{reason}.db"
    tmp = folder / (name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    dst = sqlite3.connect(tmp)
    try:
        db.backup(dst)
    finally:
        dst.close()
    os.replace(tmp, folder / name)
    prune(folder)
    return folder / name


def prune(folder: Path, keep_daily: int = KEEP_DAILY, keep_migration: int = KEEP_MIGRATION) -> None:
    """Daily copies and pre-migration copies are kept apart, so dailies never push one out."""
    for pattern, keep in (
        ("board-*-daily.db", keep_daily),
        ("board-*-before-v*.db", keep_migration),
    ):
        for old in sorted(folder.glob(pattern))[:-keep]:
            old.unlink()


def _js(value: dict | None) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def _statements(script: str) -> list[str]:
    """A migration split into statements (none of ours holds a ';' inside a literal)."""
    return [s.strip() for s in script.split(";") if s.strip()]


__all__ = ["HIDDEN", "MOVABLE", "NEEDS_YOU", "ApplyReport", "FloorError", "Store"]
