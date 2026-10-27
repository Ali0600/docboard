"""Hand-built sync results for the store and page tests (synthetic, like every fixture here)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from docboard.derive import Card
from docboard.sync import SyncResult

COUNTS = {"backlog": 10, "prs_merged": 10, "transcript_files": 5, "plan_calls": 5, "needs_items": 8}


def card(cid: str, column: str, kind: str = "idea", title: str = "A card", sub: str = "alpha",
         event_at: str | None = None, **kw: object) -> Card:  # fmt: skip
    at = event_at or (datetime.now(UTC) - timedelta(days=1)).isoformat()
    return Card(id=cid, kind=kind, column=column, title=title, sub=sub, sub_reason="title",
                event_at=at, **kw)  # fmt: skip


def result(cards: list[Card], counts: dict | None = None, degraded: list[str] | None = None,
           project: str = "demo") -> SyncResult:  # fmt: skip
    return SyncResult(
        project=project,
        root_name="Platform",
        ref="HEAD",
        ref_sha="abc123",
        github="acme/demo",
        generated_at="",
        subs=["alpha", "beta"],
        counts=dict(counts or COUNTS),
        timings={},
        cards=cards,
        warnings=[],
        degraded=list(degraded or []),
    )


def now(days: int = 0) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()
