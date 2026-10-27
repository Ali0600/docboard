"""Owner-facing items from Claude Code session transcripts, read-only.

Only a few record kinds matter, so each line is checked with byte substrings before any JSON is
parsed. Items come only from what the assistant itself wrote in main (non-sidechain) records:
text blocks and tool calls. A tool result can hold a web page's text, so one is read only for the
outcome of a plan approval or a question, never for items. Records are de-duplicated across files
by their natural key, the earliest timestamp winning, because the desktop app re-appends whole
conversations on re-attach and forked sessions copy their parent's records.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

MAX_LINE = 16 * 1024 * 1024
TEXT_CAP = 8192
EVIDENCE_CAP = 300
FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
IGNORED_BRANCHES = frozenset({"", "main", "master", "HEAD"})

_CWD = re.compile(rb'"cwd":"([^"]*)"')
_NY_HEAD = re.compile(r"^#{2,3}[ \t]*Needs you[ \t]*$", re.M)
_NEXT_HEAD = re.compile(r"^#{1,2} ", re.M)
# "**1. Decide — title**"; items before 2026-09-29 used a colon ("**1. Do: title**").
_ITEM = re.compile(r"^[ \t]*\*\*(\d+)\.\s*(Decide|Do)\s*[—–:-]\s*([^*\n]+)", re.M)

_ASSISTANT_MARKERS = (
    b"ExitPlanMode",
    b"AskUserQuestion",
    b"## Needs you",
    b"mcp__ccd_turn__wrap_up",
)
# A large approval is saved to disk and its text is a "<persisted-output>" stub, but its
# structured result still carries the plan (`toolUseResult.isAgent`).
_RESULT_MARKERS = (
    b"User has approved your plan",
    b"doesn't want to proceed",
    b'"answers"',
    b'"isAgent"',
)


@dataclass(frozen=True)
class PrLink:
    session: str
    repo: str
    number: int
    ts: str


@dataclass(frozen=True)
class PlanCall:
    id: str
    session: str
    ts: str
    plan: str
    file_path: str


@dataclass(frozen=True)
class ToolResult:
    tool_use_id: str
    ts: str
    outcome: str  # approved | rejected | answered
    answers: tuple[tuple[str, str], ...] = ()
    is_agent: bool = False


@dataclass(frozen=True)
class Question:
    id: str
    session: str
    ts: str
    questions: tuple[str, ...]


@dataclass(frozen=True)
class NeedsItem:
    uuid: str
    n: int
    session: str
    ts: str
    kind: str  # Decide | Do
    title: str
    body: str


@dataclass(frozen=True)
class WrapAsk:
    id: str
    session: str
    ts: str
    ask: str
    recap: str
    actions: tuple[str, ...]


@dataclass(frozen=True)
class HumanTurn:
    uuid: str
    session: str
    ts: str
    text: str


@dataclass(frozen=True)
class Touch:
    id: str
    session: str
    ts: str
    rel: str


@dataclass
class Scan:
    files: int = 0
    bytes: int = 0
    foreign: int = 0
    skipped_long: int = 0
    parse_errors: int = 0
    empty_needs: set[str] = field(default_factory=set)  # message uuids: a heading with no items
    pr_links: dict[tuple[str, str, int], PrLink] = field(default_factory=dict)
    plans: dict[str, PlanCall] = field(default_factory=dict)
    results: dict[str, ToolResult] = field(default_factory=dict)
    questions: dict[str, Question] = field(default_factory=dict)
    needs: dict[tuple[str, int], NeedsItem] = field(default_factory=dict)
    wraps: dict[str, WrapAsk] = field(default_factory=dict)
    humans: dict[str, HumanTurn] = field(default_factory=dict)
    touches: dict[str, Touch] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    branches: dict[str, Counter] = field(default_factory=dict)
    session_mtime: dict[str, float] = field(default_factory=dict)


def _keep(store: dict, key: object, item: object) -> None:
    old = store.get(key)
    if old is None or item.ts < old.ts:  # type: ignore[attr-defined]
        store[key] = item


def encode_path(path: Path) -> str:
    """How Claude Code names a project's transcript folder: every non-alphanumeric is '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def session_files(root: Path, transcripts_dir: Path) -> list[tuple[Path, bool]]:
    """Main transcripts of `root` and of its worktrees and sub-folders (never subagents').

    Each comes with whether it must still prove it belongs: a file in the project's own folder
    belongs (its first records may name a scratch folder the session started in), while a folder
    that only starts with the project's name could be a sibling repo's ("roblox-games-x").
    """
    enc = encode_path(root)
    if not transcripts_dir.is_dir():
        return []
    found = []
    for d in transcripts_dir.iterdir():
        if d.is_dir() and (d.name == enc or d.name.startswith(enc + "-")):
            found.extend((f, d.name != enc) for f in d.glob("*.jsonl") if f.is_file())
    return sorted(found)


def belongs(path: Path, root: Path) -> bool:
    """Whether any record of the file ran inside `root`."""
    prefix = str(root) + "/"
    with path.open("rb") as fh:
        for raw in fh:
            for m in _CWD.finditer(raw):
                cwd = m.group(1).decode("utf-8", "replace")
                if cwd == str(root) or cwd.startswith(prefix):
                    return True
    return False


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text") or ""))
    return "\n".join(parts)


def user_text(content: object) -> str | None:
    """A user record's text, or None for a tool result."""
    if isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    ):
        return None
    return _text_of(content).strip() or None


def trigger(entry: dict, text: str) -> str:
    """What started a turn (ported from turn-audit.py's trigger(), unchanged)."""
    if "<task-notification" in text:
        return "task-notification"
    if text.startswith("This session is being continued"):
        return "compaction"
    if text.startswith(("<local-command", "<command-name>", "<command-message>")):
        return "slash"
    if "[Request interrupted" in text:
        return "interrupted"
    if entry.get("isMeta") or text.startswith("Continue from where you left off"):
        return "meta"
    return "human"


def parse_needs(text: str) -> list[tuple[int, str, str, str]]:
    """(n, Decide|Do, title, body) for each item under a line-anchored "## Needs you" heading."""
    found = []
    for head in _NY_HEAD.finditer(text):
        start = head.end()
        nxt = _NEXT_HEAD.search(text, start)
        section = text[start : nxt.start() if nxt else len(text)]
        marks = list(_ITEM.finditer(section))
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(section)
            body = section[m.end() : end].strip().strip("*").strip()
            # Stored as plain text: entities written in the markdown ("&gt;") are decoded here,
            # and the page escapes everything again when it renders.
            title = html.unescape(m.group(3).strip())
            found.append((int(m.group(1)), m.group(2), title, html.unescape(body)[:TEXT_CAP]))
    return found


class _FileScan:
    def __init__(self, scan: Scan, root: Path) -> None:
        self.scan = scan
        self.root = str(root)
        self.prefix = self.root + "/"
        self.touch_marker = b'"file_path":"' + self.root.encode("utf-8")
        self.sessions: set[str] = set()

    def line(self, raw: bytes) -> None:
        if b'"type":"pr-link"' in raw or b'"type":"custom-title"' in raw:
            self.meta(raw)
        elif b'"type":"assistant"' in raw:
            if any(m in raw for m in _ASSISTANT_MARKERS) or self.touch_marker in raw:
                self.assistant(raw)
        elif b'"type":"user"' in raw:
            if b'"tool_use_id"' in raw:
                if any(m in raw for m in _RESULT_MARKERS):
                    self.result(raw)
            else:
                self.human(raw)

    def load(self, raw: bytes) -> dict | None:
        try:
            rec = json.loads(raw)
        except ValueError:
            self.scan.parse_errors += 1
            return None
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            return None
        sid = rec.get("sessionId")
        if isinstance(sid, str):
            self.sessions.add(sid)
            branch = rec.get("gitBranch")
            if isinstance(branch, str) and branch not in IGNORED_BRANCHES:
                self.scan.branches.setdefault(sid, Counter())[branch] += 1
        return rec

    def meta(self, raw: bytes) -> None:
        rec = self.load(raw)
        if rec is None:
            return
        if rec.get("type") == "pr-link":
            try:
                link = PrLink(
                    str(rec["sessionId"]),
                    str(rec["prRepository"]),
                    int(rec["prNumber"]),
                    str(rec.get("timestamp") or ""),
                )
            except (KeyError, TypeError, ValueError):
                return
            _keep(self.scan.pr_links, (link.session, link.repo, link.number), link)
        elif rec.get("type") == "custom-title" and isinstance(rec.get("customTitle"), str):
            self.scan.titles[str(rec.get("sessionId"))] = rec["customTitle"][:200]

    def assistant(self, raw: bytes) -> None:
        rec = self.load(raw)
        if rec is None or rec.get("type") != "assistant":
            return
        session, ts, uuid = (
            str(rec.get("sessionId") or ""),
            str(rec.get("timestamp") or ""),
            str(rec.get("uuid") or ""),
        )
        content = (rec.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = str(block.get("text") or "")
                if "Needs you" in text and _NY_HEAD.search(text):
                    items = parse_needs(text)
                    if not items:
                        self.scan.empty_needs.add(uuid)
                    for n, kind, title, body in items:
                        _keep(
                            self.scan.needs,
                            (uuid, n),
                            NeedsItem(uuid, n, session, ts, kind, title[:200], body),
                        )
            elif block.get("type") == "tool_use":
                self.tool_use(block, session, ts)

    def tool_use(self, block: dict, session: str, ts: str) -> None:
        name, tid, args = block.get("name"), str(block.get("id") or ""), block.get("input") or {}
        if not tid or not isinstance(args, dict):
            return
        if name == "ExitPlanMode":
            _keep(
                self.scan.plans,
                tid,
                PlanCall(
                    tid,
                    session,
                    ts,
                    str(args.get("plan") or "")[:65536],
                    str(args.get("planFilePath") or ""),
                ),
            )
        elif name == "AskUserQuestion":
            qs = tuple(
                str(q.get("question") or "")[:500]
                for q in args.get("questions") or []
                if isinstance(q, dict)
            )
            _keep(self.scan.questions, tid, Question(tid, session, ts, qs))
        elif name == "mcp__ccd_turn__wrap_up" and args.get("ask"):
            actions = tuple(
                str(a.get("label") or a.get("action") or "")[:60]
                for a in args.get("actions") or []
                if isinstance(a, dict)
            )
            _keep(
                self.scan.wraps,
                tid,
                WrapAsk(
                    tid,
                    session,
                    ts,
                    html.unescape(str(args["ask"]))[:500],
                    html.unescape(str(args.get("recap") or ""))[:TEXT_CAP],
                    actions,
                ),
            )
        elif name in FILE_TOOLS:
            path = args.get("file_path") or args.get("notebook_path")
            if isinstance(path, str) and path.startswith(self.prefix):
                _keep(self.scan.touches, tid, Touch(tid, session, ts, path[len(self.prefix) :]))

    def result(self, raw: bytes) -> None:
        rec = self.load(raw)
        if rec is None or rec.get("type") != "user":
            return
        ts = str(rec.get("timestamp") or "")
        tur = rec.get("toolUseResult")
        content = (rec.get("message") or {}).get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = str(block.get("tool_use_id") or "")
            text = _text_of(block.get("content")).lstrip()
            plan_result = isinstance(tur, dict) and "plan" in tur and "filePath" in tur
            if plan_result or text.startswith("User has approved your plan"):
                agent = bool(tur.get("isAgent")) if isinstance(tur, dict) else False
                _keep(self.scan.results, tid, ToolResult(tid, ts, "approved", is_agent=agent))
            elif "doesn't want to proceed" in text[:300]:
                _keep(self.scan.results, tid, ToolResult(tid, ts, "rejected"))
            elif isinstance(tur, dict) and isinstance(tur.get("answers"), dict):
                pairs = tuple((str(q)[:500], _answer(a)) for q, a in tur["answers"].items())
                _keep(self.scan.results, tid, ToolResult(tid, ts, "answered", answers=pairs))

    def human(self, raw: bytes) -> None:
        rec = self.load(raw)
        if rec is None or rec.get("type") != "user":
            return
        text = user_text((rec.get("message") or {}).get("content"))
        if not text:
            return
        origin = rec.get("origin")
        if isinstance(origin, dict):
            if origin.get("kind") != "human":
                return
        elif trigger(rec, text) != "human":
            return
        uuid = str(rec.get("uuid") or "")
        if uuid:
            _keep(
                self.scan.humans,
                uuid,
                HumanTurn(
                    uuid,
                    str(rec.get("sessionId") or ""),
                    str(rec.get("timestamp") or ""),
                    text[:EVIDENCE_CAP],
                ),
            )


def _answer(a: object) -> str:
    if isinstance(a, list):
        return ", ".join(str(x) for x in a)[:500]
    return str(a)[:500]


def scan_file(path: Path, root: Path, scan: Scan) -> None:
    """Adds one transcript's items to `scan`."""
    fs = _FileScan(scan, root)
    with path.open("rb") as fh:
        for raw in fh:
            if len(raw) > MAX_LINE:
                scan.skipped_long += 1
                continue
            fs.line(raw)
    st = path.stat()
    scan.files += 1
    scan.bytes += st.st_size
    for sid in fs.sessions:
        scan.session_mtime[sid] = max(scan.session_mtime.get(sid, 0.0), st.st_mtime)


def scan_project(root: Path, transcripts_dir: Path) -> Scan:
    scan = Scan()
    for path, must_prove in session_files(root, transcripts_dir):
        if must_prove and not belongs(path, root):
            scan.foreign += 1
            continue
        scan_file(path, root, scan)
    return scan
