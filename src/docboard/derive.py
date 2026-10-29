"""Sources → cards, with each card's column derived from its source alone.

The owner's own moves are layered on top later (the store); nothing here knows about them.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache

from docboard import ids
from docboard.attribution import Attributor
from docboard.docsrc import Bullet, RoadmapItem
from docboard.ghsrc import PullRequest
from docboard.route import Router
from docboard.textsim import jaccard, similar
from docboard.transcripts import PlanCall, Scan, Touch

NEEDS_YOU, IN_PROGRESS, NEXT, IDEAS, DONE = "needs_you", "in_progress", "next", "ideas", "done"
COLUMNS = (NEEDS_YOU, IN_PROGRESS, NEXT, IDEAS, DONE)
RECENT = timedelta(days=14)
PLAN_OLDER = timedelta(days=7)
SESSION_IDLE = timedelta(hours=24)
CROSS_SESSION_SAME = 0.8
REPO_WIDE = 3  # a PR whose files span this many sub-projects is the project's, not one game's
BODY_CAP = 8192

_H1 = re.compile(r"^#\s+(.+?)\s*$", re.M)
_PLAN_PREFIX = re.compile(r"^(?:(?:DONE|DRAFT|NOW|NEXT)\s*[—–:-]\s*|Plan:\s*)+", re.I)
# "## PR A — …", "## PR 1: …", "## PR0 — …"; not "## PRs that landed".
_PR_SECTION = re.compile(r"^#{2,3}\s+PR\s*[0-9A-Z]{0,3}\b", re.M)
MERGED_LONG_AGO = timedelta(days=7)
PLAN_QUIET = timedelta(days=14)
_FIRST_PAREN = re.compile(r"^\s*\(([^)]*)\)")
# A PR titled as the first of a series ("PR 1 — …", "PR A: …") starts new work: once a plan
# holds a PR, such a PR and every later one belong to some later plan, not this one.
_SERIES = re.compile(r"\bPR ?(?:0|1|A)\b")
_NEVER = datetime.max.replace(tzinfo=UTC)
TRANSCRIPT_KINDS = ("needs", "question", "wrap", "plan")


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
    raised_at: str = ""  # the latest raise of an ask ("" for anything else: its event_at)

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
    # Every configured project, when the scan covers them all: each transcript card then goes to
    # one board only. None: a single project's scan, every card is this project's.
    router: Router | None = None


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
        self.router = inp.router
        self.cards: list[Card] = []
        self.warnings: list[str] = []
        self.owner: dict[str, tuple[str, str]] = {}  # transcript card id -> (project, why)
        self.tally: Counter = Counter()  # (project, count key) -> n, for the per-board counts
        scan = inp.scan
        self.all_touches: dict[str, list[Touch]] = defaultdict(list)
        for t in scan.touches.values():
            self.all_touches[t.session].append(t)
        for lst in self.all_touches.values():
            lst.sort(key=lambda t: _t(t.ts))
        mine = (lambda t: t.project == inp.project) if self.router else (lambda t: True)
        self.touches = {s: [t for t in ts if mine(t)] for s, ts in self.all_touches.items()}
        self.humans: dict[str, list] = defaultdict(list)
        for h in scan.humans.values():
            self.humans[h.session].append(h)
        for lst in self.humans.values():
            lst.sort(key=lambda h: _t(h.ts))
        self.groups: dict[str, list[_PlanGroup]] = {}  # each session's own runs of approvals
        self.merged: dict[str, _PlanGroup] = {}  # first call id -> one plan, across sessions
        self.holding: dict[str, set[str]] = {}  # first call id -> the sessions holding it

    # --- which board -----------------------------------------------------------------------

    def _folder(self, session: str) -> str:
        return self.inp.scan.session_project.get(session, self.inp.project)

    def _ask_owner(self, session: str, at: datetime, *texts: str) -> tuple[str, str]:
        """An ask's board: the one project its text names, else the project its turn's files are
        in, else the project its transcript is filed under."""
        if self.router is None:
            return self.inp.project, "folder"
        for text in texts:
            name = self.router.by_name(text)
            if name:
                return name, "name"
        start = self._prev_human(session, at)
        turn = self.router.by_touches(
            t for t in self.all_touches.get(session, ()) if start <= _t(t.ts) <= at
        )
        if turn:
            return turn, "turn"
        return self._folder(session), "folder"

    def _own(self, card: Card, owner: tuple[str, str], session: str) -> None:
        self.owner[card.id] = owner
        folder = self._folder(session)
        if self.router is not None and owner[0] != folder:
            card.source["routed"] = owner[1]
            card.source["filed_under"] = folder

    def mine(self, card_id: str) -> bool:
        return self.owner.get(card_id, (self.inp.project,))[0] == self.inp.project

    # --- attribution helpers -------------------------------------------------------------

    def _window(self, sessions: Iterable[str], start: datetime, end: datetime) -> str | None:
        seen: dict[str, str] = {}
        for s in sessions:
            for t in self.touches.get(s, ()):
                if start <= _t(t.ts) <= end:
                    seen[t.id] = t.rel  # a copy in a continued session counts once
        return self.att.by_files(seen.values())

    def _next_human(self, session: str, at: datetime) -> object | None:
        return next((h for h in self.humans.get(session, ()) if _t(h.ts) > at), None)

    def _raise(self, card: Card, times: int, session: str, ts: str) -> None:
        """An ask raised again: one card, whose age, Older fold and "you said next" follow the
        latest raise, while `event_at` keeps the first."""
        card.raised += times
        at = _t(ts)
        if at <= _t(card.raised_at or card.event_at):
            return
        card.raised_at = ts
        card.older = at < self.inp.now - RECENT
        nxt = self._next_human(session, at)
        card.suggested_evidence = nxt.text if nxt else ""

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
        """The game an ask is about: named, else the files of its turn, else of the work since
        the plan before it; else the project's root (never a guess from the session's branch)."""
        for reason, text in zip(("title", "body"), texts, strict=False):
            slug = self.att.by_text(text)
            if slug:
                return slug, reason
        slug = self._window([session], self._prev_human(session, at), at)
        if slug:
            return slug, "turn"
        since = self._last_approval(session, at)
        if since is not None:
            slug = self._window([session], since, at)
            if slug:
                return slug, "since-plan"
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
                # A checklist's own title names its game under any of its sections.
                sub = self.att.by_text(" ".join((item.title, item.section, item.text)))
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
        """Runs of approvals with one title and plan file, per session, then one plan per first
        approval across the sessions holding it: a continued or forked session holds its
        parent's plans, and a PR it opens joins them."""
        scan = self.inp.scan
        approved: list[PlanCall] = []
        for call in scan.plans.values():
            res = scan.results.get(call.id)
            if res is None:
                self.pending_plan(call)
            elif res.outcome == "approved" and not res.is_agent:
                approved.append(call)
            else:
                self.tally[(self._folder(call.session), "plan_calls")] += 1
        by_session: dict[str, list[PlanCall]] = defaultdict(list)
        for call in approved:
            for s in scan.holding("plan", call.id, call.session):
                by_session[s].append(call)
        for session, calls in by_session.items():
            calls.sort(key=lambda c: (_t(c.ts), c.id))
            groups: list[_PlanGroup] = []
            for c in calls:
                title = plan_title(c.plan)
                g = groups[-1] if groups else None
                if g and g.last.file_path == c.file_path and g.title == title:
                    g.calls.append(c)
                else:
                    groups.append(_PlanGroup([c], title))
            self.groups[session] = groups
            for g in groups:
                key = g.first.id
                m = self.merged.get(key)
                if m is None:
                    self.merged[key] = _PlanGroup(list(g.calls), g.title)
                    self.holding[key] = {session}
                else:
                    have = {c.id for c in m.calls}
                    m.calls.extend(c for c in g.calls if c.id not in have)
                    m.calls.sort(key=lambda c: (_t(c.ts), c.id))
                    self.holding[key].add(session)

    def _group_key(self, session: str, at: datetime) -> str | None:
        """The plan the session approved last at or before a time."""
        best: tuple[datetime, str] | None = None
        for g in self.groups.get(session, ()):
            for c in g.calls:
                ct = _t(c.ts)
                if ct <= at and (best is None or ct > best[0]):
                    best = (ct, g.first.id)
        return best[1] if best else None

    def _group_end(self, key: str) -> datetime:
        """When a later plan started in any session holding this one (the end of its window)."""
        start = _t(self.merged[key].first.ts)
        ends = [
            _t(g.first.ts)
            for s in self.holding[key]
            for g in self.groups.get(s, ())
            if g.first.id != key and _t(g.first.ts) > start
        ]
        return min(ends, default=_NEVER)

    def _plan_owner(self, key: str) -> tuple[str, str]:
        """A plan's board: the repo most of its PRs went to, else the project its title names,
        else the one its window's files are in, else where its transcript is filed."""
        g = self.merged[key]
        folder = self._folder(g.first.session)
        if self.router is None:
            return self.inp.project, "folder"
        start, end = _t(g.first.ts), self._group_end(key)
        sessions = self.holding[key]
        linked = {
            (link.repo.casefold(), link.number): link
            for link in self.inp.scan.pr_links.values()
            if link.session in sessions and start <= _t(link.ts) < end
        }
        votes = Counter(
            p for (repo, _), _ in linked.items() if (p := self.router.project_of_repo(repo))
        )
        tied: set[str] | None = None
        if votes:
            top = max(votes.values())
            tied = {p for p, n in votes.items() if n == top}
            if len(tied) == 1:
                return tied.pop(), "linked-prs"

        def ok(p: str | None) -> bool:
            return bool(p) and (tied is None or p in tied)

        name = self.router.by_name(g.title)
        if ok(name):
            return name, "name"
        window = self.router.by_touches(
            {t.id: t for s in sessions for t in self.all_touches.get(s, ())
             if start <= _t(t.ts) < end}.values()
        )  # fmt: skip
        if ok(window):
            return window, "plan-window"
        if tied and folder not in tied:
            return sorted(tied)[0], "linked-prs"
        return folder, "folder"

    def pending_plan(self, call: PlanCall) -> None:
        at = _t(call.ts)
        title = plan_title(call.plan)
        sub, reason = self._transcript_sub(call.session, at, title)
        card = Card(
            id=ids.plan_id(call.id),
            kind="plan",
            column=NEEDS_YOU,
            title=title,
            sub=sub,
            sub_reason=reason,
            body=call.plan[:BODY_CAP],
            event_at=call.ts,
            raised_at=call.ts,
            older=at < self.inp.now - RECENT,
            badges=["waiting for approval"],
            source={"session": call.session},
        )
        owner = self._ask_owner(call.session, at, title)
        self._own(card, owner, call.session)
        self.tally[(owner[0], "plan_calls")] += 1
        self.cards.append(card)

    def _pr_owner(self) -> dict[int, tuple[str, str | None]]:
        """PR number → (the session that opened it, the plan it implements or None)."""
        github = (self.inp.github or "").casefold()
        links: dict[int, list[tuple[str, datetime]]] = defaultdict(list)
        for link in self.inp.scan.pr_links.values():
            if github and link.repo.casefold() == github:
                links[link.number].append((link.session, _t(link.ts)))
        owner: dict[int, tuple[str, str | None]] = {}
        for pr in self.inp.prs:
            cands = links.get(pr.number)
            if not cands:
                continue
            created = _t(pr.created_at)
            session = min(cands, key=lambda c: (abs((c[1] - created).total_seconds()), c[0]))[0]
            owner[pr.number] = (session, self._group_key(session, created))
        # A PR starting a new series ends the plan's claim on it and on every later one.
        by_key: dict[str, list[PullRequest]] = defaultdict(list)
        for pr in self.inp.prs:
            key = owner.get(pr.number, (None, None))[1]
            if key:
                by_key[key].append(pr)
        for prs in by_key.values():
            prs.sort(key=lambda p: (_t(p.created_at), p.number))
            for i, pr in enumerate(prs):
                if i and _SERIES.search(pr.title):
                    for later in prs[i:]:
                        owner[later.number] = (owner[later.number][0], None)
                    break
        return owner

    def prs_and_plans(self) -> None:
        plan_owner = {key: self._plan_owner(key) for key in self.merged}
        for key, (proj, _) in plan_owner.items():
            self.tally[(proj, "plan_calls")] += len(self.merged[key].calls)
        owner = self._pr_owner()
        by_group: dict[str, list[PullRequest]] = defaultdict(list)
        for pr in self.inp.prs:
            cid = ids.pr_id(pr.repo, pr.number)
            column = IN_PROGRESS if pr.state == "open" else DONE
            sub = self.att.by_text(pr.title)
            reason = "title"
            if not sub:
                sub, reason = self.att.by_branch(pr.head_ref), "branch"
            if not sub:
                files = self.inp.squash.get(pr.number, ())
                if self.att.spread(files) >= REPO_WIDE:
                    sub, reason = "", "repo-wide"  # a move across games: the project's own work
                else:
                    sub, reason = self.att.by_files(files, min_hits=1), "files"
            if not sub and reason != "repo-wide":
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
            session, key = owner.get(pr.number, (None, None))
            if session:
                card.source["session"] = session
            if key is not None:
                by_group[key].append(pr)
                plan_proj = plan_owner[key][0]
                if plan_proj == self.inp.project:
                    card.parent = ids.plan_id(key)
                else:
                    # Its plan is on another board: the card stays on this one, naming it.
                    card.source.update(plan=ids.plan_id(key), plan_title=self.merged[key].title,
                                       plan_project=plan_proj)  # fmt: skip
                    card.badges.append(f"plan in {plan_proj}")
            self.cards.append(card)
        other = self._other_repo_prs()
        cards_by_id = {c.id: c for c in self.cards}
        for key in self.merged:
            self._plan_card(key, plan_owner[key], by_group.get(key, []), other.get(key, []),
                            cards_by_id)  # fmt: skip

    def _other_repo_prs(self) -> dict[str, list[tuple[str, int]]]:
        """Each plan's PRs in another configured repo: (repo, number), from its sessions' links."""
        github = (self.inp.github or "").casefold()
        found: dict[str, dict[tuple[str, int], None]] = defaultdict(dict)
        for link in self.inp.scan.pr_links.values():
            if link.repo.casefold() == github:
                continue
            if self.router is not None and not self.router.project_of_repo(link.repo):
                continue
            key = self._group_key(link.session, _t(link.ts))
            if key:
                found[key][(link.repo, link.number)] = None
        return {k: sorted(v) for k, v in found.items()}

    def _plan_card(
        self,
        key: str,
        owner: tuple[str, str],
        prs: list[PullRequest],
        other: list[tuple[str, int]],
        cards_by_id: dict[str, Card],
    ) -> None:
        now = self.inp.now
        g = self.merged[key]
        last = _t(g.last.ts)
        sessions = self.holding[key]
        holding_last = {s for s in sessions for gg in self.groups.get(s, ()) if g.last in gg.calls}
        later_plan = any(
            _t(gg.first.ts) > last for s in holding_last for gg in self.groups.get(s, ())
        )
        mtimes = [self.inp.scan.session_mtime.get(s, 0.0) for s in sessions]
        idle = all(not m or datetime.fromtimestamp(m, UTC) < now - SESSION_IDLE for m in mtimes)
        planned = len(_PR_SECTION.findall(g.last.plan))
        merged = sum(1 for p in prs if p.state == "merged")
        newest = max((_t(p.merged_at or p.closed_at or "") for p in prs), default=None)
        quiet = False
        if not prs and self.inp.github is None:
            # A repo with no GitHub remote has no PRs to wait for: the work is the session's.
            column = DONE if (later_plan or idle) else IN_PROGRESS
        elif not prs:
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
        if prs:
            badges = [f"{merged} of {max(len(prs), planned)} PRs merged"]
        elif self.inp.github is None:
            badges = []
        else:
            badges = ["no PR yet"]
        if quiet and newest is not None:
            badges.append(f"quiet since {newest.date().isoformat()}")
        elsewhere = Counter(
            (self.router.project_of_repo(r) if self.router else None) or r for r, _ in other
        )
        badges.extend(f"+{n} in {p}" for p, n in sorted(elsewhere.items()))
        child_ids = [ids.pr_id(p.repo, p.number) for p in prs]
        subs = {cards_by_id[c].sub for c in child_ids if c in cards_by_id}
        sub = self.att.by_text(g.title)
        reason = "title"
        if not sub and len(subs) == 1 and "" not in subs:
            sub, reason = subs.pop(), "linked-prs"
        if not sub:
            end = min(self._group_end(key), now)
            sub, reason = self._window(sessions, _t(g.first.ts), end), "plan-window"
        if not sub:
            sub, reason = "", "root"
        card = Card(
            id=ids.plan_id(key),
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
                "session": g.first.session,
                "plan_file": g.last.file_path,
                "approvals": len(g.calls),
                "session_title": self.inp.scan.titles.get(g.first.session, ""),
                "other_prs": [f"{r}#{n}" for r, n in other],
            },
        )
        self._own(card, owner, g.first.session)
        self.cards.append(card)

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
                # Raised again later in the same session (turn-audit's similar(), 0.4): the
                # first raise's card, on the first raise's board.
                twin = next((c for c, title in kept if similar(it.title, [title])), None)
                if twin is not None:
                    self._raise(twin, 1, session, it.ts)
                    self.tally[(self.owner[twin.id][0], "needs_items")] += 1
                    continue
                at = _t(it.ts)
                sub, reason = self._transcript_sub(session, at, it.title, it.body)
                nxt = self._next_human(session, at)
                card = Card(
                    id=ids.needs_id(it.uuid, it.n),
                    kind="needs",
                    column=NEEDS_YOU,
                    title=f"{it.kind} — {it.title}"[:200],
                    sub=sub,
                    sub_reason=reason,
                    body=it.body,
                    event_at=it.ts,
                    raised_at=it.ts,
                    older=at < self.inp.now - RECENT,
                    suggested_evidence=nxt.text if nxt else "",
                    source={
                        "session": session,
                        "message": it.uuid,
                        "n": it.n,
                        "session_title": scan.titles.get(session, ""),
                    },
                )
                self._own(card, self._ask_owner(session, at, it.title, it.body), session)
                self.tally[(self.owner[card.id][0], "needs_items")] += 1
                kept.append((card, it.title))
            per_session.extend(kept)
        # Raised again from another session (a fork, a resumed task): one card when the board
        # and sub-project match and the wording is near-identical. The earliest card is kept.
        kept_all: list[tuple[Card, str]] = []
        for card, title in sorted(per_session, key=lambda ct: (_t(ct[0].event_at), ct[0].id)):
            twin = next(
                (
                    c
                    for c, t in kept_all
                    if c.sub == card.sub
                    and self.owner[c.id][0] == self.owner[card.id][0]
                    and jaccard(title, t) >= CROSS_SESSION_SAME
                ),
                None,
            )
            if twin is not None:
                self._raise(twin, card.raised, card.source["session"], card.raised_at)
                continue
            kept_all.append((card, title))
            self.cards.append(card)

    def questions(self) -> None:
        scan = self.inp.scan
        cards_by_id = {c.id: c for c in self.cards}
        for q in scan.questions.values():
            res = scan.results.get(q.id)
            at = _t(q.ts)
            owner = self._ask_owner(q.session, at, " ".join(q.questions))
            self.tally[(owner[0], "questions")] += 1
            if res is None:
                sub, reason = self._transcript_sub(q.session, at, " ".join(q.questions))
                card = Card(
                    id=ids.ask_id(q.id),
                    kind="question",
                    column=NEEDS_YOU,
                    title=(q.questions[0] if q.questions else "A question")[:200],
                    sub=sub,
                    sub_reason=reason,
                    body="\n".join(q.questions),
                    event_at=q.ts,
                    raised_at=q.ts,
                    older=at < self.inp.now - RECENT,
                    badges=["waiting for an answer"],
                    source={"session": q.session},
                )
                self._own(card, owner, q.session)
                self.tally[(owner[0], "questions_waiting")] += 1
                self.cards.append(card)
                continue
            if res.outcome != "answered":
                continue
            target = self._plan_for_question(q, at)
            card = cards_by_id.get(target) if target else None
            if card is not None:
                card.decided.extend(f"{qq} → {a}" for qq, a in res.answers)

    def _plan_for_question(self, q: object, at: datetime) -> str | None:
        """The plan a question shaped: the first approved at or after it, else the last one,
        among the plans held by a session holding the question."""
        holders = self.inp.scan.holding("question", q.id, q.session)  # type: ignore[attr-defined]
        keys = {g.first.id for s in holders for g in self.groups.get(s, ())}
        plans = sorted((self.merged[k] for k in keys), key=lambda g: (_t(g.last.ts), g.first.id))
        for g in plans:
            if _t(g.last.ts) >= at:
                return ids.plan_id(g.first.id)
        return ids.plan_id(plans[-1].first.id) if plans else None

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
                self._raise(twin, 1, w.session, w.ts)
                self.tally[(self.owner[twin.id][0], "wrap_asks")] += 1
                continue
            at = _t(w.ts)
            # The recap tells of the whole session, often several games: the ask alone decides.
            sub, reason = self._transcript_sub(w.session, at, w.ask)
            nxt = self._next_human(w.session, at)
            card = Card(
                id=ids.wrap_id(w.id),
                kind="wrap",
                column=NEEDS_YOU,
                title=w.ask[:200],
                sub=sub,
                sub_reason=reason,
                body=w.recap,
                event_at=w.ts,
                raised_at=w.ts,
                badges=list(w.actions),
                older=at < self.inp.now - RECENT,
                suggested_evidence=nxt.text if nxt else "",
                source={"session": w.session},
            )
            self._own(card, self._ask_owner(w.session, at, w.ask), w.session)
            self.tally[(self.owner[card.id][0], "wrap_asks")] += 1
            self.cards.append(card)

    def headings(self) -> None:
        for _uuid, session in self.inp.scan.empty_needs.items():
            self.tally[(self._folder(session), "needs_headings_without_items")] += 1


def derive(inp: Inputs, counts: dict[str, int] | None = None) -> tuple[list[Card], list[str]]:
    """The project's cards. With a router, transcript cards another board owns are left out,
    and `counts` (when given) receives this board's share of the transcript items."""
    d = _Deriver(inp)
    d.backlog()
    d.roadmap()
    d.plan_groups()
    d.prs_and_plans()
    d.needs()
    d.questions()
    d.wraps()
    d.headings()
    cards = [c for c in d.cards if c.kind not in TRANSCRIPT_KINDS or d.mine(c.id)]
    if counts is not None:
        for (proj, key), n in d.tally.items():
            if proj == inp.project:
                counts[key] = counts.get(key, 0) + n
    order = {c: i for i, c in enumerate(COLUMNS)}
    cards.sort(
        key=lambda c: (order[c.column], -_t(c.event_at).timestamp() if c.event_at else 0, c.id)
    )
    return cards, d.warnings
