"""Owner-facing items from Claude Code session transcripts, read-only.

Only a few record kinds matter, so each line is checked with byte substrings before any JSON is
parsed. Items come only from what the assistant itself wrote in main (non-sidechain) records:
text blocks and tool calls. A tool result can hold a web page's text, so one is read only for the
outcome of a plan approval or a question, never for items. Records are de-duplicated across files
by their natural key, the earliest timestamp winning, because the desktop app re-appends whole
conversations on re-attach and forked sessions copy their parent's records. Which sessions hold a
copy is kept too (`Scan.holders`): a session continued from another holds its plans, so a PR the
continued session opens can still join the plan it implements.

One pass reads the transcript folders of every configured project, so a card can follow the files
its work touched into another project's board. Each file is read once per change: its result is
remembered by the file's identity, size and modification time.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

MAX_LINE = 16 * 1024 * 1024
TEXT_CAP = 8192
EVIDENCE_CAP = 300
FILE_TOOLS = frozenset({"Read", "Edit", "Write", "MultiEdit", "NotebookEdit"})
_PATH_KEYS = (b'"file_path":"', b'"notebook_path":"')
_ALNUM = re.compile(r"[A-Za-z0-9]")
IS_WINDOWS = os.name == "nt"


def _norm(path: str, windows: bool) -> str:
    """A path for comparing: on Windows "/" for "\\" and case ignored, as the file system does."""
    return path.replace("\\", "/").casefold() if windows else path


def _cut(path: str, folded: int, windows: bool) -> int:
    """Where a prefix ends in the original path when it is `folded` characters long case-folded
    (on Windows a letter may fold to two: "ß" to "ss")."""
    if not windows:
        return folded
    end = n = 0
    while n < folded and end < len(path):
        n += len(path[end].casefold())
        end += 1
    return end


def _spellings_of(root: str, windows: bool) -> set[str]:
    """How a root may be written in a transcript: as is, and on Windows with "/" and with the
    drive letter in either case."""
    out = {root}
    if windows:
        for p in (root, root.replace("\\", "/")):
            out.add(p)
            if len(p) > 1 and p[1] == ":":
                out |= {p[0].upper() + p[1:], p[0].lower() + p[1:]}
    return out


def _json_bytes(text: str) -> bytes:
    """A string as JSON writes it inside quotes (backslashes doubled), the transcripts' bytes."""
    return json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8")


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
    rel: str  # relative to the project's root, "/"-joined
    project: str = ""  # the configured project whose root holds the file


class Roots:
    """Every configured project's root, as the transcripts spell it (a folder name may show the
    path in another case than the config does; on Windows "/" or "\\" and either drive case)."""

    def __init__(self, spellings: dict[str, Iterable[str]], windows: bool = IS_WINDOWS) -> None:
        self.windows = windows
        self.spellings = {name: tuple(sorted(set(sp))) for name, sp in spellings.items()}
        pairs = [(sp, name) for name, sps in self.spellings.items() for sp in sps]
        normed = {(_norm(sp, windows), name) for sp, name in pairs}
        self._deepest = sorted(normed, key=lambda p: (-len(p[0]), p[0]))
        markers = {key + _json_bytes(v) for sp, _ in pairs for v in _spellings_of(sp, windows)
                   for key in _PATH_KEYS}  # fmt: skip
        # Windows paths ignore case, so there the bytes are compared lower-cased.
        self.markers = tuple(sorted({m.lower() for m in markers} if windows else markers))
        self.signature = repr((sorted(pairs), windows))

    def owner(self, path: str) -> tuple[str, str] | None:
        """(project, path relative to its root, "/"-joined) for a file inside a root, the
        deepest winning."""
        np = _norm(path, self.windows)
        slashed = path.replace("\\", "/") if self.windows else path
        for sp, name in self._deepest:
            if np.startswith(sp + "/"):
                return name, slashed[_cut(slashed, len(sp), self.windows) + 1 :]
        return None

    def inside(self, path: str, name: str) -> bool:
        np = _norm(path, self.windows)
        return any(
            np == (n := _norm(sp, self.windows)) or np.startswith(n + "/")
            for sp in self.spellings.get(name, ())
        )


@dataclass(frozen=True)
class SessionFile:
    path: Path
    exact: str | None  # the project whose own folder holds the file
    candidates: tuple[str, ...] = ()  # look-alike folders (worktrees, siblings): must prove


@dataclass(frozen=True)
class FileResult:
    """What one transcript file holds, independent of who is syncing."""

    stem: str
    size: int
    sessions: frozenset[str]
    needs: tuple[NeedsItem, ...]
    empty_needs: tuple[tuple[str, str], ...]  # (message uuid, session): a heading with no items
    plans: tuple[PlanCall, ...]
    results: tuple[ToolResult, ...]
    questions: tuple[Question, ...]
    wraps: tuple[WrapAsk, ...]
    humans: tuple[HumanTurn, ...]
    touches: tuple[Touch, ...]
    pr_links: tuple[PrLink, ...]
    titles: tuple[tuple[str, str], ...]
    cwds: frozenset[str]
    parse_errors: int
    skipped_long: int
    starts: tuple[tuple[str, str], ...] = ()  # (session, its earliest record's timestamp here)
    born: float = 0.0  # when the file was created (its creation time where the OS keeps one)


@dataclass
class Scan:
    files: int = 0
    bytes: int = 0
    foreign: int = 0
    skipped_long: int = 0
    parse_errors: int = 0
    unreadable: int = 0  # files that vanished or could not be opened mid-scan
    empty_needs: dict[str, str] = field(default_factory=dict)  # message uuid -> session
    pr_links: dict[tuple[str, str, int], PrLink] = field(default_factory=dict)
    plans: dict[str, PlanCall] = field(default_factory=dict)
    results: dict[str, ToolResult] = field(default_factory=dict)
    questions: dict[str, Question] = field(default_factory=dict)
    needs: dict[tuple[str, int], NeedsItem] = field(default_factory=dict)
    wraps: dict[str, WrapAsk] = field(default_factory=dict)
    humans: dict[tuple[str, str], HumanTurn] = field(default_factory=dict)
    touches: dict[tuple[str, str], Touch] = field(default_factory=dict)
    titles: dict[str, str] = field(default_factory=dict)
    session_mtime: dict[str, float] = field(default_factory=dict)
    # Which sessions hold a copy of a plan, question, needs or wrap-up item: ("plan", id) -> ids.
    holders: dict[tuple[str, object], set[str]] = field(default_factory=lambda: defaultdict(set))
    session_project: dict[str, str] = field(default_factory=dict)  # the project it is filed under
    session_start: dict[str, str] = field(default_factory=dict)  # its earliest record's time
    session_born: dict[str, float] = field(default_factory=dict)  # when its own file was created
    files_by: Counter = field(default_factory=Counter)
    bytes_by: Counter = field(default_factory=Counter)
    foreign_by: Counter = field(default_factory=Counter)
    parse_errors_by: Counter = field(default_factory=Counter)
    long_by: Counter = field(default_factory=Counter)
    unreadable_by: Counter = field(default_factory=Counter)
    touches_by: Counter = field(default_factory=Counter)  # file touches under each project
    roots: Roots | None = None

    def holding(self, kind: str, key: object, session: str) -> set[str]:
        """The sessions holding a copy of an item, its own session always among them."""
        return {session, *self.holders.get((kind, key), ())}


def _keep(store: dict, key: object, item: object, order: dict[str, tuple] | None = None) -> None:
    """Keeps the earliest copy of a record. A copy in a continued or forked session has the same
    time as the original, so on a tie the original keeps it: the session that started first,
    then the one whose own file was created first (a full copy starts at the same record)."""
    old = store.get(key)
    if old is None or item.ts < old.ts:  # type: ignore[attr-defined]
        store[key] = item
    elif order is not None and item.ts == old.ts:  # type: ignore[attr-defined]
        new_rank = order.get(item.session)  # type: ignore[attr-defined]
        old_rank = order.get(old.session)  # type: ignore[attr-defined]
        if new_rank is not None and (old_rank is None or new_rank < old_rank):
            store[key] = item


def encode_path(path: Path) -> str:
    """How Claude Code names a project's transcript folder: every non-alphanumeric is '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def spelling(root: Path, folder: str) -> str:
    """The root as the app wrote it, recovered from its folder name: each letter takes the
    folder's case (the encoding keeps letters and digits as they are)."""
    path = str(root)
    if len(folder) < len(path):
        return path
    return "".join(
        folder[i] if _ALNUM.match(ch) and folder[i].casefold() == ch.casefold() else ch
        for i, ch in enumerate(path)
    )


def discover(
    projects: Sequence[tuple[str, Path]], transcripts_dir: Path, windows: bool = IS_WINDOWS
) -> tuple[list[SessionFile], Roots]:
    """Main transcripts of every project (its worktrees' and sub-folders' too, never subagents').

    A file in a project's own folder belongs to it (its first records may name a scratch folder
    the session started in); a folder that only starts with a project's name could be a sibling
    repo's ("roblox-games-x"), so its files must prove it by a record's `cwd`. Folder names are
    compared ignoring case: the app may spell a path in another case than the config.
    """
    encs = [(name, encode_path(path).casefold(), path) for name, path in projects]
    spellings: dict[str, set[str]] = {name: {str(path)} for name, path in projects}
    files: list[SessionFile] = []
    if not transcripts_dir.is_dir():
        return files, Roots(spellings, windows)
    for d in sorted(transcripts_dir.iterdir()):
        if not d.is_dir():
            continue
        low = d.name.casefold()
        exact = next((n for n, e, _ in encs if low == e), None)
        cands = tuple(n for _, n in sorted(((-len(e), n) for n, e, _ in encs
                                            if low.startswith(e + "-"))))  # fmt: skip
        if exact is None and not cands:
            continue
        for name, e, path in encs:
            if low == e or low.startswith(e + "-"):
                spellings[name].add(spelling(path, d.name))
        files.extend(
            SessionFile(f, exact, () if exact else cands)
            for f in sorted(d.glob("*.jsonl"))
            if f.is_file()
        )
    return files, Roots(spellings, windows)


def session_files(root: Path, transcripts_dir: Path) -> list[tuple[Path, bool]]:
    """One project's transcripts, each with whether it must still prove it belongs."""
    files, _ = discover([("", root)], transcripts_dir)
    return [(f.path, f.exact is None) for f in files]


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
    def __init__(self, roots: Roots, want_cwds: bool) -> None:
        self.roots = roots
        self.want_cwds = want_cwds
        self.sessions: set[str] = set()
        self.cwds: set[str] = set()
        self.needs: list[NeedsItem] = []
        self.empty_needs: list[tuple[str, str]] = []
        self.plans: list[PlanCall] = []
        self.results: list[ToolResult] = []
        self.questions: list[Question] = []
        self.wraps: list[WrapAsk] = []
        self.humans: list[HumanTurn] = []
        self.touches: list[Touch] = []
        self.pr_links: list[PrLink] = []
        self.titles: list[tuple[str, str]] = []
        self.parse_errors = 0
        self.skipped_long = 0
        self.starts: dict[str, str] = {}

    def line(self, raw: bytes) -> None:
        if self.want_cwds:
            for m in _CWD.finditer(raw):
                self.cwds.add(_json_str(m.group(1)))
        if b'"type":"pr-link"' in raw or b'"type":"custom-title"' in raw:
            self.meta(raw)
        elif b'"type":"assistant"' in raw:
            hay = raw.lower() if self.roots.windows else raw
            if any(m in raw for m in _ASSISTANT_MARKERS) or any(
                m in hay for m in self.roots.markers
            ):
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
            self.parse_errors += 1
            return None
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            return None
        sid = rec.get("sessionId")
        if isinstance(sid, str):
            self.sessions.add(sid)
            ts = rec.get("timestamp")
            if isinstance(ts, str) and ts and (sid not in self.starts or ts < self.starts[sid]):
                self.starts[sid] = ts
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
            self.pr_links.append(link)
        elif rec.get("type") == "custom-title" and isinstance(rec.get("customTitle"), str):
            self.titles.append((str(rec.get("sessionId")), rec["customTitle"][:200]))

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
                        self.empty_needs.append((uuid, session))
                    for n, kind, title, body in items:
                        self.needs.append(NeedsItem(uuid, n, session, ts, kind, title[:200], body))
            elif block.get("type") == "tool_use":
                self.tool_use(block, session, ts)

    def tool_use(self, block: dict, session: str, ts: str) -> None:
        name, tid, args = block.get("name"), str(block.get("id") or ""), block.get("input") or {}
        if not tid or not isinstance(args, dict):
            return
        if name == "ExitPlanMode":
            self.plans.append(
                PlanCall(
                    tid,
                    session,
                    ts,
                    str(args.get("plan") or "")[:65536],
                    str(args.get("planFilePath") or ""),
                )
            )
        elif name == "AskUserQuestion":
            qs = tuple(
                str(q.get("question") or "")[:500]
                for q in args.get("questions") or []
                if isinstance(q, dict)
            )
            self.questions.append(Question(tid, session, ts, qs))
        elif name == "mcp__ccd_turn__wrap_up" and args.get("ask"):
            actions = tuple(
                str(a.get("label") or a.get("action") or "")[:60]
                for a in args.get("actions") or []
                if isinstance(a, dict)
            )
            self.wraps.append(
                WrapAsk(
                    tid,
                    session,
                    ts,
                    html.unescape(str(args["ask"]))[:500],
                    html.unescape(str(args.get("recap") or ""))[:TEXT_CAP],
                    actions,
                )
            )
        elif name in FILE_TOOLS:
            path = args.get("file_path") or args.get("notebook_path")
            found = self.roots.owner(path) if isinstance(path, str) else None
            if found is not None:
                self.touches.append(Touch(tid, session, ts, found[1], found[0]))

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
                self.results.append(ToolResult(tid, ts, "approved", is_agent=agent))
            elif "doesn't want to proceed" in text[:300]:
                self.results.append(ToolResult(tid, ts, "rejected"))
            elif isinstance(tur, dict) and isinstance(tur.get("answers"), dict):
                pairs = tuple((str(q)[:500], _answer(a)) for q, a in tur["answers"].items())
                self.results.append(ToolResult(tid, ts, "answered", answers=pairs))

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
            self.humans.append(
                HumanTurn(
                    uuid,
                    str(rec.get("sessionId") or ""),
                    str(rec.get("timestamp") or ""),
                    text[:EVIDENCE_CAP],
                )
            )

    def freeze(self, stem: str, size: int) -> FileResult:
        return FileResult(
            stem=stem,
            size=size,
            sessions=frozenset(self.sessions),
            needs=tuple(self.needs),
            empty_needs=tuple(self.empty_needs),
            plans=tuple(self.plans),
            results=tuple(self.results),
            questions=tuple(self.questions),
            wraps=tuple(self.wraps),
            humans=tuple(self.humans),
            touches=tuple(self.touches),
            pr_links=tuple(self.pr_links),
            titles=tuple(self.titles),
            cwds=frozenset(self.cwds),
            parse_errors=self.parse_errors,
            skipped_long=self.skipped_long,
            starts=tuple(sorted(self.starts.items())),
        )


def _json_str(raw: bytes) -> str:
    """A JSON string's raw bytes (without quotes) as text, escapes decoded."""
    try:
        return json.loads(b'"' + raw + b'"')
    except ValueError:
        return raw.decode("utf-8", "replace")


def _answer(a: object) -> str:
    if isinstance(a, list):
        return ", ".join(str(x) for x in a)[:500]
    return str(a)[:500]


def read_file(path: Path, roots: Roots, want_cwds: bool) -> tuple[FileResult, tuple | None]:
    """One transcript's items, and the file's identity if it did not change while being read
    (None when it did: such a result is used once and never remembered)."""
    fs = _FileScan(roots, want_cwds)
    with path.open("rb") as fh:
        before = os.fstat(fh.fileno())
        for raw in fh:
            if len(raw) > MAX_LINE:
                fs.skipped_long += 1
                continue
            fs.line(raw)
        after = os.fstat(fh.fileno())
    ident = _ident(before)
    result = fs.freeze(path.stem, before.st_size)
    result = replace(result, born=getattr(before, "st_birthtime", before.st_ctime))
    return result, (ident if ident == _ident(after) else None)


def _ident(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


class FileMemo:
    """Each file's result, kept while the file is the same file, at the same size and time, read
    with the same roots. A read that raised, or a file that changed while it was read, is not
    kept. Transcripts only grow, so a changed file is simply read again."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, tuple[tuple, FileResult]] = {}
        self.reads = 0  # files actually read (for tests and timings)

    def get(self, sf: SessionFile, roots: Roots) -> tuple[FileResult, float]:
        want = sf.exact is None
        st = sf.path.stat()
        key = (*_ident(st), roots.signature, want)
        with self._lock:
            hit = self._items.get(str(sf.path))
        if hit is not None and hit[0] == key:
            return hit[1], st.st_mtime
        result, ident = read_file(sf.path, roots, want)
        with self._lock:
            self.reads += 1
            if ident is not None:
                self._items[str(sf.path)] = ((*ident, roots.signature, want), result)
        return result, st.st_mtime

    def forget_missing(self, keep: Iterable[Path]) -> None:
        live = {str(p) for p in keep}
        with self._lock:
            for k in [k for k in self._items if k not in live]:
                del self._items[k]


MEMO = FileMemo()


def _merge(scan: Scan, fr: FileResult, order: dict[str, tuple]) -> None:
    starts = order
    for item in fr.needs:
        _keep(scan.needs, (item.uuid, item.n), item, starts)
        scan.holders[("needs", (item.uuid, item.n))].add(item.session)
    for uuid, session in fr.empty_needs:
        scan.empty_needs.setdefault(uuid, session)
    for call in fr.plans:
        _keep(scan.plans, call.id, call, starts)
        scan.holders[("plan", call.id)].add(call.session)
    for res in fr.results:
        _keep(scan.results, res.tool_use_id, res)
    for q in fr.questions:
        _keep(scan.questions, q.id, q, starts)
        scan.holders[("question", q.id)].add(q.session)
    for w in fr.wraps:
        _keep(scan.wraps, w.id, w, starts)
        scan.holders[("wrap", w.id)].add(w.session)
    for h in fr.humans:
        _keep(scan.humans, (h.session, h.uuid), h)
    for t in fr.touches:
        _keep(scan.touches, (t.session, t.id), t)
    for link in fr.pr_links:
        _keep(scan.pr_links, (link.session, link.repo, link.number), link)
    for sid, title in fr.titles:
        scan.titles[sid] = title


def scan_all(
    projects: Sequence[tuple[str, Path]],
    transcripts_dir: Path,
    memo: FileMemo | None = None,
    windows: bool = IS_WINDOWS,
) -> Scan:
    """Every configured project's transcripts in one pass, each file filed under one project."""
    files, roots = discover(projects, transcripts_dir, windows)
    memo = memo or FileMemo()
    memo.forget_missing(f.path for f in files)
    scan = Scan(roots=roots)
    read: list[FileResult] = []
    for sf in files:
        first = sf.exact if sf.exact is not None else sf.candidates[0]
        try:
            fr, mtime = memo.get(sf, roots)
        except OSError:
            # Deleted between the listing and the read (Claude Code's cleanup), or unreadable:
            # counted, and the transcript_files floor still catches a mass loss.
            scan.unreadable += 1
            scan.unreadable_by[first] += 1
            continue
        filed = sf.exact
        if filed is None:
            filed = next(
                (n for n in sf.candidates if any(roots.inside(c, n) for c in fr.cwds)), None
            )
        if filed is None:
            scan.foreign += 1
            scan.foreign_by[first] += 1
            continue
        scan.files += 1
        scan.bytes += fr.size
        scan.parse_errors += fr.parse_errors
        scan.skipped_long += fr.skipped_long
        scan.files_by[filed] += 1
        scan.bytes_by[filed] += fr.size
        scan.parse_errors_by[filed] += fr.parse_errors
        scan.long_by[filed] += fr.skipped_long
        for sid in fr.sessions:
            if sid == fr.stem or sid not in scan.session_project:
                scan.session_project[sid] = filed
            scan.session_mtime[sid] = max(scan.session_mtime.get(sid, 0.0), mtime)
        for sid, ts in fr.starts:
            if sid not in scan.session_start or ts < scan.session_start[sid]:
                scan.session_start[sid] = ts
        if fr.born and fr.stem in fr.sessions:  # the session's own file, not a copy's
            scan.session_born[fr.stem] = fr.born
        read.append(fr)
    # Once every session's start and file are known, the tie between copies can be decided.
    order = {
        sid: (start, scan.session_born.get(sid, float("inf")))
        for sid, start in scan.session_start.items()
    }
    for fr in read:
        _merge(scan, fr, order)
    scan.touches_by.update(t.project for t in scan.touches.values())
    return scan


def scan_project(
    root: Path, transcripts_dir: Path, name: str = "", windows: bool = IS_WINDOWS
) -> Scan:
    """One project's transcripts (the single-project form of `scan_all`)."""
    return scan_all([(name, root)], transcripts_dir, windows=windows)
