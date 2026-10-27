"""Sources → cards, with each card's column derived from its source alone.

The owner's own moves are layered on top later (the store); nothing here knows about them.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache

from docboard import ids
from docboard.attribution import Attributor
from docboard.docsrc import Bullet, RoadmapItem
from docboard.ghsrc import PullRequest
from docboard.textsim import jaccard, similar
from docboard.transcripts import PlanCall, Scan, Touch

NEEDS_YOU, IN_PROGRESS, NEXT, IDEAS, DONE = "needs_you", "in_progress", "next", "ideas", "done"
COLUMNS = (NEEDS_YOU, IN_PROGRESS, NEXT, IDEAS, DONE)
RECENT = timedelta(days=14)
PLAN_OLDER = timedelta(days=7)
SESSION_IDLE = timedelta(hours=24)
CROSS_SESSION_SAME = 0.8
BODY_CAP = 8192

_H1 = re.compile(r"^#\s+(.+?)\s*$", re.M)
_PLAN_PREFIX = re.compile(r"^(?:(?:DONE|DRAFT|NOW|NEXT)\s*[—–:-]\s*|Plan:\s*)+", re.I)
# "## PR A — …", "## PR 1: …", "## PR0 — …"; not "## PRs that landed".
_PR_SECTION = re.compile(r"^#{2,3}\s+PR\s*[0-9A-Z]{0,3}\b", re.M)
MERGED_LONG_AGO = timedelta(days=7)
PLAN_QUIET = timedelta(days=14)
_FIRST_PAREN = re.compile(r"^\s*\(([^)]*)\)")


@dataclass
class Card:
    id: str
    kind: str  # idea | roadmap | pr | plan | needs | question | wrap
    column: str
    title: str
    sub: str  # a sub-project slug, or "" for the project's root
    sub_reason: str
    body: str = ""
    event_at: str = ""
    source: dict = field(default_factory=dict)
    badges: list[str] = field(default_factory=list)
    parent: str | None = None
    children: list[str] = field(default_factory=list)
    decided: list[str] = field(default_factory=list)
    suggested_evidence: str = ""  # shown to the owner, never applied
    raised: int = 1
    older: bool = False

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class DocFile:
    path: str
    sub: str  # the sub-project the file lives in ("" for the root)


@dataclass
class Inputs:
    project: str
    github: str | None
    attributor: Attributor
    backlog: Sequence[tuple[DocFile, Bullet]]
    roadmap: Sequence[tuple[DocFile, RoadmapItem]]
    prs: Sequence[PullRequest]
    squash: dict[int, list[str]]
    scan: Scan
    now: datetime
    ref_sha: str | None = None


@cache
def _t(ts: str) -> datetime:
    """ISO 8601 → aware UTC datetime; an unparsable or empty value sorts first."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def plan_title(text: str) -> str:
    m = _H1.search(text)
    raw = m.group(1) if m else next((ln for ln in text.splitlines() if ln.strip()), "Plan")
    return _PLAN_PREFIX.sub("", raw.strip()).strip()[:200] or "Plan"


@dataclass
class _PlanGroup:
    calls: list[PlanCall]
    title: str

    @property
    def first(self) -> PlanCall:
        return self.calls[0]

    @property
    def last(self) -> PlanCall:
        return self.calls[-1]


class _Deriver:
    def __init__(self, inp: Inputs) -> None:
        self.inp = inp
        self.att = inp.attributor
        self.cards: list[Card] = []
        self.warnings: list[str] = []
        scan = inp.scan
        self.touches: dict[str, list[Touch]] = defaultdict(list)
        for t in scan.touches.values():
            self.touches[t.session].append(t)
        for lst in self.touches.values():
            lst.sort(key=lambda t: _t(t.ts))
        self.humans: dict[str, list] = defaultdict(list)
        for h in scan.humans.values():
            self.humans[h.session].append(h)
        for lst in self.humans.values():
            lst.sort(key=lambda h: _t(h.ts))
        self.groups: dict[str, list[_PlanGroup]] = {}

    # --- attribution helpers -------------------------------------------------------------

    def _window(self, session: str, start: datetime, end: datetime) -> str | None:
        paths = [t.rel for t in self.touches.get(session, ()) if start <= _t(t.ts) <= end]
        return self.att.by_files(paths)

    def _session_branch(self, session: str) -> str | None:
        counts = self.inp.scan.branches.get(session)
        if not counts:
            return None
        return self.att.by_branch(counts.most_common(1)[0][0])

    def _prev_human(self, session: str, at: datetime) -> datetime:
        times = [_t(h.ts) for h in self.humans.get(session, ())]
        i = bisect_right(times, at)
        return times[i - 1] if i else datetime.min.replace(tzinfo=UTC)

    def _last_approval(self, session: str, at: datetime) -> datetime | None:
        best = None
        for g in self.groups.get(session, ()):
            for c in g.calls:
                ct = _t(c.ts)
                if ct <= at and (best is None or ct > best):
                    best = ct
        return best

    def _transcript_sub(self, session: str, at: datetime, *texts: str) -> tuple[str, str]:
        for reason, text in zip(("title", "body"), texts, strict=False):
            slug = self.att.by_text(text)
            if slug:
                return slug, reason
        slug = self._window(session, self._prev_human(session, at), at)
        if slug:
            return slug, "turn"
        since = self._last_approval(session, at)
        if since is not None:
            slug = self._window(session, since, at)
            if slug:
                return slug, "since-plan"
        slug = self._session_branch(session)
        if slug:
            return slug, "session-branch"
        return "", "root"

    # --- docs ----------------------------------------------------------------------------

    def backlog(self) -> None:
        base: dict[str, list[tuple[DocFile, Bullet]]] = defaultdict(list)
        for doc, b in self.inp.backlog:
            base[ids.backlog_id(self.inp.project, b.title)].append((doc, b))
        for cid, entries in base.items():
            if len(entries) > 1:
                self.warnings.append(
                    f"{len(entries)} Backlog bullets share the title {entries[0][1].title!r}"
                )
            for doc, b in entries:
                card_id = (
                    cid
                    if len(entries) == 1
                    else ids.backlog_id(self.inp.project, b.title, salt=ids.norm(b.body)[:80])
                )
                if doc.sub:
                    sub, reason = doc.sub, "path"
                else:
                    paren = _FIRST_PAREN.match(b.body)
                    sub = self.att.by_text(b.title + " " + (paren.group(1) if paren else ""))
                    sub, reason = (sub, "title") if sub else ("", "root")
                self.cards.append(
                    Card(
                        id=card_id,
                        kind="idea",
                        column=IDEAS,
                        title=b.title[:200],
                        sub=sub,
                        sub_reason=reason,
                        body=b.body,
                        source={"path": doc.path, "line": b.line, "ref_sha": self.inp.ref_sha},
                    )
                )

    def roadmap(self) -> None:
        seen: set[str] = set()
        for doc, item in self.inp.roadmap:
            cid = ids.roadmap_id(self.inp.project, doc.sub, item.section, item.text)
            if cid in seen:
                self.warnings.append(
                    f"{doc.path}:{item.line}: a ROADMAP item repeats; kept the first"
                )
                continue
            seen.add(cid)
            if doc.sub:
                sub, reason = doc.sub, "path"
            else:
                sub = self.att.by_text(item.section + " " + item.text)
                sub, reason = (sub, "title") if sub else ("", "root")
            self.cards.append(
                Card(
                    id=cid,
                    kind="roadmap",
                    column=DONE if item.done else NEXT,
                    title=item.text[:200],
                    sub=sub,
                    sub_reason=reason,
                    body=item.text,
                    badges=[item.section[:80]],
                    source={"path": doc.path, "line": item.line, "ref_sha": self.inp.ref_sha},
                )
            )

    # --- plans and pull requests ---------------------------------------------------------

    def plan_groups(self) -> None:
        scan = self.inp.scan
        by_session: dict[str, list[PlanCall]] = defaultdict(list)
        for call in scan.plans.values():
            res = scan.results.get(call.id)
            if res is None:
                self.pending_plan(call)
            elif res.outcome == "approved" and not res.is_agent:
                by_session[call.session].append(call)
        for session, calls in by_session.items():
            calls.sort(key=lambda c: _t(c.ts))
            groups: list[_PlanGroup] = []
            for c in calls:
                title = plan_title(c.plan)
                g = groups[-1] if groups else None
                if g and g.last.file_path == c.file_path and g.title == title:
                    g.calls.append(c)
                else:
                    groups.append(_PlanGroup([c], title))
            self.groups[session] = groups

    def pending_plan(self, call: PlanCall) -> None:
        sub, reason = self._transcript_sub(call.session, _t(call.ts), plan_title(call.plan))
        self.cards.append(
            Card(
                id=ids.plan_id(call.id),
                kind="plan",
                column=NEEDS_YOU,
                title=plan_title(call.plan),
                sub=sub,
                sub_reason=reason,
                body=call.plan[:BODY_CAP],
                event_at=call.ts,
                badges=["waiting for approval"],
                source={"session": call.session},
            )
        )

    def _pr_owner(self) -> dict[int, tuple[str, _PlanGroup | None]]:
        """PR number → (author session, the plan it implements or None)."""
        links: dict[int, list[tuple[str, datetime]]] = defaultdict(list)
        for link in self.inp.scan.pr_links.values():
            if self.inp.github and link.repo == self.inp.github:
                links[link.number].append((link.session, _t(link.ts)))
        owner: dict[int, tuple[str, _PlanGroup | None]] = {}
        for pr in self.inp.prs:
            cands = links.get(pr.number)
            if not cands:
                continue
            created = _t(pr.created_at)
            session = min(cands, key=lambda c: abs((c[1] - created).total_seconds()))[0]
            best: tuple[datetime, _PlanGroup] | None = None
            for g in self.groups.get(session, ()):
                for c in g.calls:
                    ct = _t(c.ts)
                    if ct <= created and (best is None or ct > best[0]):
                        best = (ct, g)
            owner[pr.number] = (session, best[1] if best else None)
        return owner

    def prs_and_plans(self) -> None:
        owner = self._pr_owner()
        by_group: dict[int, list[PullRequest]] = defaultdict(list)
        for pr in self.inp.prs:
            cid = ids.pr_id(pr.repo, pr.number)
            column = IN_PROGRESS if pr.state == "open" else DONE
            sub = self.att.by_text(pr.title)
            reason = "title"
            if not sub:
                sub, reason = self.att.by_branch(pr.head_ref), "branch"
            if not sub:
                sub, reason = (
                    self.att.by_files(self.inp.squash.get(pr.number, ()), min_hits=1),
                    "files",
                )
            if not sub:
                sub, reason = "", "root"
            card = Card(
                id=cid,
                kind="pr",
                column=column,
                title=pr.title,
                sub=sub,
                sub_reason=reason,
                event_at=pr.merged_at or pr.closed_at or pr.created_at,
                badges=["closed"] if pr.state == "closed" else [],
                source={
                    "url": pr.url,
                    "number": pr.number,
                    "branch": pr.head_ref,
                    "state": pr.state,
                },
            )
            session, group = owner.get(pr.number, (None, None))
            if session:
                card.source["session"] = session
            if group is not None:
                by_group[id(group)].append(pr)
                card.parent = ids.plan_id(group.first.id)
            self.cards.append(card)
        cards_by_id = {c.id: c for c in self.cards}
        for session, groups in self.groups.items():
            for i, g in enumerate(groups):
                self._plan_card(session, groups, i, g, by_group.get(id(g), []), cards_by_id)

    def _plan_card(
        self,
        session: str,
        groups: list[_PlanGroup],
        i: int,
        g: _PlanGroup,
        prs: list[PullRequest],
        cards_by_id: dict[str, Card],
    ) -> None:
        now = self.inp.now
        last = _t(g.last.ts)
        later_plan = i + 1 < len(groups)
        mtime = self.inp.scan.session_mtime.get(session, 0.0)
        idle = datetime.fromtimestamp(mtime, UTC) < now - SESSION_IDLE if mtime else True
        planned = len(_PR_SECTION.findall(g.last.plan))
        merged = sum(1 for p in prs if p.state == "merged")
        newest = max((_t(p.merged_at or p.closed_at or "") for p in prs), default=None)
        quiet = False
        if not prs:
            column = NEXT
        elif any(p.state == "open" for p in prs):
            column = IN_PROGRESS
        elif planned and merged >= planned:
            column = DONE  # every PR the plan named is in
        elif merged and len(prs) >= planned and (later_plan or idle):
            column = DONE  # no PR list to check: done once the session moved on or went quiet
        elif merged and len(prs) >= planned and newest < now - MERGED_LONG_AGO:
            column = DONE  # a long-lived session: nothing landed for its plan in a week
        elif merged and newest < now - PLAN_QUIET:
            # Part of its PR list never linked here: the rest usually landed through another
            # session. Out of In progress, with the gap kept on the card.
            column, quiet = DONE, True
        else:
            column = IN_PROGRESS
        badges = [f"{merged} of {max(len(prs), planned)} PRs merged"] if prs else ["no PR yet"]
        if quiet and newest is not None:
            badges.append(f"quiet since {newest.date().isoformat()}")
        child_ids = [ids.pr_id(p.repo, p.number) for p in prs]
        subs = {cards_by_id[c].sub for c in child_ids if c in cards_by_id}
        sub = self.att.by_text(g.title)
        reason = "title"
        if not sub and len(subs) == 1 and "" not in subs:
            sub, reason = subs.pop(), "linked-prs"
        if not sub:
            end = _t(groups[i + 1].first.ts) if later_plan else now
            sub, reason = self._window(session, _t(g.first.ts), end), "plan-window"
        if not sub:
            sub, reason = self._session_branch(session), "session-branch"
        if not sub:
            sub, reason = "", "root"
        self.cards.append(
            Card(
                id=ids.plan_id(g.first.id),
                kind="plan",
                column=column,
                title=g.title,
                sub=sub,
                sub_reason=reason,
                body=g.last.plan[:BODY_CAP],
                event_at=g.last.ts,
                badges=badges,
                children=child_ids,
                older=(column == NEXT and last < now - PLAN_OLDER),
                source={
                    "session": session,
                    "plan_file": g.last.file_path,
                    "approvals": len(g.calls),
                    "session_title": self.inp.scan.titles.get(session, ""),
                },
            )
        )

    # --- owner-facing transcript items ---------------------------------------------------

    def needs(self) -> None:
        scan = self.inp.scan
        by_session: dict[str, list] = defaultdict(list)
        for item in scan.needs.values():
            by_session[item.session].append(item)
        per_session: list[tuple[Card, str]] = []
        for session, items in by_session.items():
            items.sort(key=lambda it: (_t(it.ts), it.n))
            kept: list[tuple[Card, str]] = []
            for it in items:
                # Raised again later in the same session (turn-audit's similar(), 0.4).
                twin = next((c for c, title in kept if similar(it.title, [title])), None)
                if twin is not None:
                    twin.raised += 1
                    continue
                at = _t(it.ts)
                sub, reason = self._transcript_sub(session, at, it.title, it.body)
                nxt = next((h for h in self.humans.get(session, ()) if _t(h.ts) > at), None)
                card = Card(
                    id=ids.needs_id(it.uuid, it.n),
                    kind="needs",
                    column=NEEDS_YOU,
                    title=f"{it.kind} — {it.title}"[:200],
                    sub=sub,
                    sub_reason=reason,
                    body=it.body,
                    event_at=it.ts,
                    older=at < self.inp.now - RECENT,
                    suggested_evidence=nxt.text if nxt else "",
                    source={
                        "session": session,
                        "message": it.uuid,
                        "n": it.n,
                        "session_title": scan.titles.get(session, ""),
                    },
                )
                kept.append((card, it.title))
            per_session.extend(kept)
        # Raised again from another session (a fork, a resumed task): one card when the
        # sub-project matches and the wording is near-identical. The earliest card is kept.
        kept_all: list[tuple[Card, str]] = []
        for card, title in sorted(per_session, key=lambda ct: (_t(ct[0].event_at), ct[0].id)):
            twin = next(
                (
                    c
                    for c, t in kept_all
                    if c.sub == card.sub and jaccard(title, t) >= CROSS_SESSION_SAME
                ),
                None,
            )
            if twin is not None:
                twin.raised += card.raised
                continue
            kept_all.append((card, title))
            self.cards.append(card)

    def questions(self) -> None:
        scan = self.inp.scan
        cards_by_id = {c.id: c for c in self.cards}
        for q in scan.questions.values():
            res = scan.results.get(q.id)
            at = _t(q.ts)
            if res is None:
                sub, reason = self._transcript_sub(q.session, at, " ".join(q.questions))
                self.cards.append(
                    Card(
                        id=ids.ask_id(q.id),
                        kind="question",
                        column=NEEDS_YOU,
                        title=(q.questions[0] if q.questions else "A question")[:200],
                        sub=sub,
                        sub_reason=reason,
                        body="\n".join(q.questions),
                        event_at=q.ts,
                        badges=["waiting for an answer"],
                        source={"session": q.session},
                    )
                )
                continue
            if res.outcome != "answered":
                continue
            target = self._plan_for_question(q.session, at)
            card = cards_by_id.get(target) if target else None
            if card is not None:
                card.decided.extend(f"{qq} → {a}" for qq, a in res.answers)

    def _plan_for_question(self, session: str, at: datetime) -> str | None:
        """The plan a question shaped: the first approved at or after it, else the last one."""
        groups = self.groups.get(session) or []
        for g in groups:
            if _t(g.last.ts) >= at:
                return ids.plan_id(g.first.id)
        return ids.plan_id(groups[-1].first.id) if groups else None

    def wraps(self) -> None:
        needs_by_session: dict[str, list[Card]] = defaultdict(list)
        for c in self.cards:
            if c.kind == "needs":
                needs_by_session[c.source.get("session", "")].append(c)
        for w in sorted(self.inp.scan.wraps.values(), key=lambda w: (_t(w.ts), w.id)):
            # A session that asks in its Needs-you block and again in the wrap-up box asked once.
            twin = next(
                (c for c in needs_by_session.get(w.session, ()) if similar(w.ask, [c.title])), None
            )
            if twin is not None:
                twin.raised += 1
                continue
            at = _t(w.ts)
            # The recap tells of the whole session, often several games: the ask alone decides.
            sub, reason = self._transcript_sub(w.session, at, w.ask)
            self.cards.append(
                Card(
                    id=ids.wrap_id(w.id),
                    kind="wrap",
                    column=NEEDS_YOU,
                    title=w.ask[:200],
                    sub=sub,
                    sub_reason=reason,
                    body=w.recap,
                    event_at=w.ts,
                    badges=list(w.actions),
                    older=at < self.inp.now - RECENT,
                    source={"session": w.session},
                )
            )


def derive(inp: Inputs) -> tuple[list[Card], list[str]]:
    d = _Deriver(inp)
    d.backlog()
    d.roadmap()
    d.plan_groups()
    d.prs_and_plans()
    d.needs()
    d.questions()
    d.wraps()
    order = {c: i for i, c in enumerate(COLUMNS)}
    d.cards.sort(
        key=lambda c: (order[c.column], -_t(c.event_at).timestamp() if c.event_at else 0, c.id)
    )
    return d.cards, d.warnings
