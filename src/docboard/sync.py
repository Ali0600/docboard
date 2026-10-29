"""One project's sources → cards, with counts per source so a silent drop shows."""

from __future__ import annotations

import json
import os
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from docboard import docsrc, ghsrc
from docboard.attribution import Attributor
from docboard.config import Config, ConfigError, Project
from docboard.derive import COLUMNS, Card, DocFile, Inputs, derive
from docboard.gitrepo import GitError, Repo
from docboard.route import Router
from docboard.transcripts import MEMO, scan_all

JSON_VERSION = 1


@dataclass
class SyncResult:
    project: str
    root_name: str
    ref: str | None
    ref_sha: str | None
    github: str | None
    generated_at: str
    subs: list[str]
    counts: dict[str, int]
    timings: dict[str, float]
    cards: list[Card]
    warnings: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    sub_names: dict[str, str] = field(default_factory=dict)  # slug → the game's own name
    # Every session whose transcript was read: an open ask whose session is missing was cleaned
    # up, not answered. Kept out of the JSON file, which is per card.
    sessions: set[str] = field(default_factory=set)
    # Transcript cards another board owns -> that board: handed over, never left on no board.
    handover: dict[str, str] = field(default_factory=dict)
    # session -> {"title", "decided", "first_at", "last_at"}: answers no plan of this board took
    session_notes: dict[str, dict] = field(default_factory=dict)

    def to_json(self) -> dict:
        skip = ("cards", "sessions", "handover", "session_notes")
        data = {k: v for k, v in self.__dict__.items() if k not in skip}
        data["version"] = JSON_VERSION
        data["cards"] = [c.to_json() for c in self.cards]
        return data


def _subprojects(
    repo: Repo, project: Project, warnings: list[str]
) -> tuple[Attributor, dict[str, str]]:
    sp = project.subprojects
    if sp is None:
        return Attributor(parent=""), {}
    slugs = [d for d in repo.dirs(sp.parent) if d not in sp.exclude]
    names: dict[str, str] = {}
    if sp.name_from:
        file, _, key = sp.name_from.partition(":")
        for slug in slugs:
            text = repo.read(f"{sp.parent}/{slug}/{file}")
            if text is None:
                continue
            try:
                value = tomllib.loads(text).get(key)
            except tomllib.TOMLDecodeError as e:
                warnings.append(f"{sp.parent}/{slug}/{file}: not TOML ({e})")
                continue
            if isinstance(value, str):
                names[slug] = value
    return Attributor.build(sp.parent, slugs, names, sp.aliases, sp.branches), names


def router(cfg: Config) -> Router:
    """Every configured project with its GitHub repo and its games' names, for routing session
    items to boards."""
    pairs, subs = [], {}
    for p in cfg.projects:
        repo = p.github
        try:
            opened = Repo.open(p.path, p.docs_ref)
            repo = repo or opened.github_slug()
            subs[p.name] = _subprojects(opened, p, [])[0]
        except (GitError, OSError, ConfigError):
            pass  # its own sync reports what is wrong; the other boards route without its games
        pairs.append((p.name, repo))
    return Router.build(pairs, subs)


def collect(cfg: Config, project: Project, now: datetime, gh: str | None = None) -> SyncResult:
    timings: dict[str, float] = {}
    warnings: list[str] = []
    degraded: list[str] = []
    t0 = time.monotonic()
    repo = Repo.open(project.path, project.docs_ref)
    github = project.github or repo.github_slug()
    att, names = _subprojects(repo, project, warnings)

    doc_roots = [DocFile("docs/DECISIONS.md", "")]
    roadmaps = [DocFile("ROADMAP.md", ""), DocFile("docs/ROADMAP.md", "")]
    for slug in att.slugs:
        base = f"{att.parent}/{slug}"
        doc_roots.append(DocFile(f"{base}/docs/DECISIONS.md", slug))
        roadmaps += [DocFile(f"{base}/ROADMAP.md", slug), DocFile(f"{base}/docs/ROADMAP.md", slug)]
    backlog, roadmap, checklist = [], [], []
    decision_files = roadmap_files = 0
    for doc in doc_roots:
        text = repo.read(doc.path)
        if text is not None:
            decision_files += 1
            bullets = docsrc.parse_backlog(text)
            starts = docsrc.backlog_starts(text)
            if starts != len(bullets):
                warnings.append(
                    f"{doc.path}: {starts} Backlog bullets start but {len(bullets)} were read"
                )
            backlog.extend((doc, b) for b in bullets)
    for doc in roadmaps:
        text = repo.read(doc.path)
        if text is not None:
            roadmap_files += 1
            roadmap.extend((doc, i) for i in docsrc.parse_roadmap(text))
    folders = [("docs/checklists", "")] + [
        (f"{att.parent}/{slug}/docs/checklists", slug) for slug in att.slugs
    ]
    for folder, sub in folders:
        for path in repo.files(folder):
            if not path.endswith(".md") or "/" in path[len(folder) + 1 :]:
                continue  # the folder's own files only (not data/ beside them)
            text = repo.read(path)
            if text is not None:
                checklist.extend((DocFile(path, sub), i) for i in docsrc.parse_checklist(text))
    timings["docs"] = time.monotonic() - t0

    t1 = time.monotonic()
    prs: list[ghsrc.PullRequest] = []
    if github:
        gh = gh or ghsrc.find_gh()
        if gh is None:
            degraded.append("gh: not installed")
        else:
            try:
                prs = ghsrc.fetch(github, gh)
            except (ghsrc.GhError, OSError) as e:
                degraded.append(f"gh: {e}")
    squash = repo.squash_files()
    timings["github"] = time.monotonic() - t1

    t2 = time.monotonic()
    # Every configured project's transcripts: a session filed under one project may work on
    # another, and its cards follow the files it touched (route.py). Unchanged files are not
    # read again (transcripts.MEMO).
    scan = scan_all([(p.name, p.path) for p in cfg.projects], cfg.transcripts_dir, memo=MEMO)
    routing = router(cfg)
    timings["transcripts"] = time.monotonic() - t2

    routed: dict[str, int] = {}
    extras: dict = {}
    cards, more = derive(
        Inputs(
            project=project.name,
            github=github,
            attributor=att,
            backlog=backlog,
            roadmap=roadmap + checklist,
            prs=prs,
            squash=squash,
            scan=scan,
            now=now,
            ref_sha=repo.ref_sha,
            router=routing,
        ),
        counts=routed,
        extras=extras,
    )
    warnings += more
    states = Counter(p.state for p in prs)
    counts = {
        "decision_files": decision_files,
        "backlog": len(backlog),
        "roadmap_files": roadmap_files,
        "roadmap": len(roadmap),
        "checklist_items": len(checklist),
        "prs": len(prs),
        "prs_merged": states["merged"],
        "prs_open": states["open"],
        "prs_closed": states["closed"],
        # Files filed under this project (its own folders), so the floors mean what they did:
        "transcript_files": scan.files_by[project.name],
        "transcript_bytes": scan.bytes_by[project.name],
        "transcript_foreign": scan.foreign_by[project.name],
        "transcript_long_lines": scan.long_by[project.name],
        "transcript_parse_errors": scan.parse_errors_by[project.name],
        "transcript_unreadable": scan.unreadable_by[project.name],
        # What the one pass read for every project (informational):
        "transcript_files_read": scan.files,
        "transcript_bytes_read": scan.bytes,
        # Floors: what the parser finds in every board's transcripts. A collapse there means a
        # parser broke; one board's share also moves when routing moves a card.
        "plan_calls": len(scan.plans),
        "needs_items": len(scan.needs),
        # This board's share of the session items, after routing (not floored):
        "plan_calls_here": routed.get("plan_calls", 0),
        "plan_cards": sum(1 for c in cards if c.kind == "plan"),
        "needs_items_here": routed.get("needs_items", 0),
        "needs_cards": sum(1 for c in cards if c.kind == "needs"),
        "needs_headings_without_items": routed.get("needs_headings_without_items", 0),
        "questions": routed.get("questions", 0),
        "questions_waiting": sum(1 for c in cards if c.kind == "question"),
        "wrap_asks": routed.get("wrap_asks", 0),
        "routed_here": sum(1 for c in cards if c.source.get("routed")),
    }
    if counts["transcript_parse_errors"]:
        warnings.append(f"{counts['transcript_parse_errors']} transcript lines were not JSON")
    if counts["transcript_unreadable"]:
        warnings.append(
            f"{counts['transcript_unreadable']} transcript files could not be read"
            " (deleted mid-scan?)"
        )
    if counts["needs_headings_without_items"]:
        warnings.append(
            f"{counts['needs_headings_without_items']} 'Needs you' headings had no items in a"
            " known shape"
        )
    timings["total"] = time.monotonic() - t0
    return SyncResult(
        project=project.name,
        root_name=project.root_name,
        ref=repo.ref,
        ref_sha=repo.ref_sha,
        github=github,
        generated_at=now.isoformat(),
        subs=list(att.slugs),
        counts=counts,
        timings=timings,
        cards=cards,
        warnings=warnings,
        degraded=degraded,
        sub_names=names,
        sessions=set(scan.session_mtime),
        handover=extras.get("handover", {}),
        session_notes=extras.get("sessions", {}),
    )


# Sources that never shrink to nothing in a working project; a drop means a parser broke.
FLOOR_KEYS = (
    "backlog", "decision_files", "roadmap", "prs_merged", "transcript_files", "plan_calls",
    "needs_items",
)  # fmt: skip
# Counts that come from gh: while gh is down they are zero for want of an answer, not because
# a parser broke, so a degraded sync skips them (and its PR and plan cards stay frozen).
GH_FLOOR_KEYS = ("prs_merged",)


def gh_degraded(degraded: list[str]) -> bool:
    return any(d.startswith("gh") for d in degraded)


def floor_drops(
    current: dict[str, int], baseline: dict[str, int], skip: tuple[str, ...] = ()
) -> dict[str, str]:
    """Each count that fell to zero, or by more than half, since the baseline: key → why."""
    drops = {}
    for key in FLOOR_KEYS:
        if key in skip or key not in baseline:
            continue
        before, now = baseline[key], current.get(key, 0)
        if before > 0 and now == 0:
            drops[key] = f"{key}: {before} → 0"
        elif before >= 4 and now < before / 2:
            drops[key] = f"{key}: {before} → {now} (more than half gone)"
    return drops


def floor_problems(current: dict[str, int], baseline: dict[str, int]) -> list[str]:
    """A count that fell to zero, or by more than half, since the baseline."""
    return list(floor_drops(current, baseline).values())


def write_json(result: SyncResult, path: Path) -> None:
    """Private: titles and evidence come from the owner's own sessions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(result.to_json(), fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def table(result: SyncResult) -> str:
    """Cards per sub-project and column; "(n)" is how many are from the last 14 days."""
    top = [c for c in result.cards if c.parent is None]
    rows = [("", result.root_name)] + [(s, s) for s in result.subs]
    heads = ["Needs you", "In progress", "Next", "Ideas", "Done"]
    width = max(len(label) for _, label in rows) + 2
    lines = [" " * width + "".join(f"{h:>14}" for h in heads)]
    for slug, label in [*rows, (None, "total")]:
        cells = []
        for col in COLUMNS:
            sel = [c for c in top if c.column == col and (slug is None or c.sub == slug)]
            if col in ("needs_you", "next"):
                fresh = sum(1 for c in sel if not c.older)
                cells.append(f"{len(sel)} ({fresh})")
            else:
                cells.append(str(len(sel)))
        if slug is None or any(cells_val != "0" and cells_val != "0 (0)" for cells_val in cells):
            lines.append(f"{label:<{width}}" + "".join(f"{v:>14}" for v in cells))
    return "\n".join(lines)
