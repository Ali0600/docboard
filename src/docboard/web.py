"""The board's page: a local web app on the loopback address only.

It renders text that came from transcripts, which can hold any web page's words, so everything is
escaped (Jinja's autoescape, no `|safe` on source text), links are http(s) only, scripts are this
app's own file, and nothing changes state except a POST that a same-origin page sent to a
loopback Host. That keeps an outside page from reading the board (DNS rebinding) or moving cards
(cross-site POST).
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from docboard import runner
from docboard.config import Config, Project
from docboard.derive import COLUMNS, DONE, IN_PROGRESS, NEEDS_YOU, NEXT, RECENT
from docboard.store import DEFAULT_DB, HIDDEN, FloorError, Store, StoreError

HERE = Path(__file__).parent
STALE_AFTER = 120.0  # seconds: an open board older than this refreshes in the background
COLUMN_NAMES = {
    NEEDS_YOU: "Needs you", IN_PROGRESS: "In progress", NEXT: "Next", "ideas": "Ideas",
    DONE: "Done", HIDDEN: "Hidden",
}  # fmt: skip
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
_URL = re.compile(r"https?://[^\s<>\"'`]+")


def linkify(text: str) -> Markup:
    """Escaped text with its http(s) URLs as links; nothing else becomes markup."""
    out, pos = [], 0
    for m in _URL.finditer(text or ""):
        url = m.group(0).rstrip(".,);:")
        out.append(escape(text[pos : m.start()]))
        out.append(Markup('<a href="{0}" rel="noopener noreferrer">{0}</a>').format(url))
        pos = m.start() + len(url)
    out.append(escape((text or "")[pos:]))
    return Markup("").join(out)


def _same_origin(origin: str | None, site: str | None, allowed: set[str]) -> bool:
    """A state-changing request from this board's own page.

    The Origin header names this board, or is absent or "null" while the browser's own
    Sec-Fetch-Site (a header no page can set) says same-origin. Anything else is refused,
    including a request carrying neither.
    """
    if origin in allowed:
        return True
    return origin in (None, "null") and site == "same-origin"


def _safe_back(back: str | None, default: str) -> str:
    """Only a path on this site: no scheme, no '//host', no backslashes."""
    if back and back.startswith("/") and not back.startswith("//") and "\\" not in back:
        return back
    return default


def _age(ts: str, now: datetime) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = (now - dt).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))}m"
    if secs < 86400:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 86400)}d"


class Board:
    def __init__(self, cfg: Config, db_path: Path, port: int) -> None:
        self.cfg = cfg
        self.db_path = db_path
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self._syncing: set[str] = set()
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self.subs: dict[str, list[str]] = {}

    def project(self, name: str) -> Project | None:
        return next((p for p in self.cfg.projects if p.name == name), None)

    def store(self) -> Store:
        return Store(self.db_path)

    def refresh_in_background(self, project: Project) -> threading.Thread | None:
        """Starts one sync of the project unless one is already running; returns its thread."""
        with self._guard:
            if project.name in self._syncing:
                return None
            self._syncing.add(project.name)

        def work() -> None:
            try:
                runner.run_sync(self.cfg, project, self.db_path, wait=0)
            except runner.Busy:
                pass  # another sync holds the lock and stores this project's state right now
            except FloorError as e:  # its row in sync_run is what the page shows
                print(f"docboard: sync of {project.name} refused: {e}", file=sys.stderr)
            except runner.SyncFailed as e:  # recorded as failed; the cause goes to the log
                print(f"docboard: sync of {project.name} failed: {e}", file=sys.stderr)
                traceback.print_exception(e.__cause__ or e, file=sys.stderr)
            finally:
                with self._guard:
                    self._syncing.discard(project.name)

        thread = threading.Thread(target=work, name=f"sync-{project.name}", daemon=True)
        with self._guard:
            self._threads[project.name] = thread
        thread.start()
        return thread

    def join(self, timeout: float) -> bool:
        """Waits for the background syncs started so far; True when none is still running."""
        deadline = time.monotonic() + timeout
        with self._guard:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        return not any(t.is_alive() for t in threads)

    def syncing(self, name: str) -> bool:
        with self._guard:
            return name in self._syncing


def _card_view(row: object, now: datetime) -> dict:
    keys = row.keys()  # type: ignore[attr-defined]
    get = lambda k, d=None: row[k] if k in keys else d  # noqa: E731
    is_manual = get("kind") is None
    return {
        "id": row["id"],
        "kind": get("kind") or "card",
        "title": row["title"],
        "body": row["body"],
        "col": row["col"],
        "sub": row["sub"],
        "sub_reason": get("sub_reason", "you"),
        "badges": json.loads(get("badges", "[]")),
        "raised": get("raised", 1),
        "older": bool(get("older", 0)),
        # An ask raised again counts from its latest raise.
        "event_at": get("raised_at") or get("event_at") or get("created_at") or "",
        "age": _age(get("raised_at") or get("event_at") or get("created_at") or "", now),
        "reopened": bool(get("reopened_at")) and row["col"] == NEEDS_YOU,
        "reopened_was": get("reopened_was"),
        "source_gone": bool(get("source_gone_at")) and not get("gone_at"),
        "parent": get("parent"),
        "children": json.loads(get("children", "[]")),
        "moved": bool(get("moved", 0)),
        "manual": is_manual,
        "href": "/i/" + quote(row["id"], safe=""),
    }


def create_app(cfg: Config, db_path: Path = DEFAULT_DB, port: int = 8742) -> FastAPI:
    board = Board(cfg, db_path, port)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["linkify"] = linkify
    templates.env.globals["COLUMN_NAMES"] = COLUMN_NAMES
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.state.board = board

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001, ANN202
        if request.headers.get("host", "") not in board.hosts:
            return PlainTextResponse("This board answers only on its loopback address.", 400)
        if request.method not in ("GET", "HEAD") and not _same_origin(
            request.headers.get("origin"), request.headers.get("sec-fetch-site"), board.origins
        ):
            return PlainTextResponse("Refused: the request did not come from this board.", 403)
        response: Response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Not "no-referrer": under it Chrome sends "Origin: null" on the page's own form posts.
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    def now() -> datetime:
        return datetime.now(UTC)

    def not_found(what: str) -> PlainTextResponse:
        return PlainTextResponse(f"No such {what}.", 404)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        if len(cfg.projects) == 1:
            return RedirectResponse("/p/" + quote(cfg.projects[0].name, safe=""), 303)
        with board.store() as store:
            rows = []
            for p in cfg.projects:
                cards = store.cards(p.name)
                rows.append(
                    {
                        "name": p.name,
                        "needs": sum(1 for c in cards if c["col"] == NEEDS_YOU and not c["older"]),
                        "last": store.last_run(p.name),
                    }
                )
        return templates.TemplateResponse(request, "index.html", {"projects": rows})

    @app.get("/p/{name}", response_class=HTMLResponse)
    def project_board(request: Request, name: str, sub: str = "", older: int = 0,
                      done: str = "", hidden: int = 0) -> Response:  # fmt: skip
        project = board.project(name)
        if project is None:
            return not_found("project")
        t = now()
        with board.store() as store:
            last = store.last_run(name)
            last_ok = store.last_run(name, ok_only=True)
            stale = (
                last is None
                or (t - datetime.fromisoformat(last["started"])).total_seconds() > STALE_AFTER
            )
            if stale:
                board.refresh_in_background(project)
            rows = [_card_view(r, t) for r in store.cards(name)]
            manual = [_card_view(_ManualRow(m), t) for m in store.manual(name)]
            gone = len(store.gone(name))
            subs, _ = store.known(name)
        by_id = {c["id"]: c for c in rows}
        visible = [c for c in rows + manual if c["parent"] is None]

        in_sub = [c for c in visible if _in_sub(c, sub)]
        cutoff = t - RECENT
        columns = []
        for col in COLUMNS:
            all_here = [c for c in in_sub if c["col"] == col]
            shown = all_here
            folded = 0
            if col in (NEEDS_YOU, NEXT) and not older:
                shown = [c for c in all_here if not c["older"]]
            elif col == DONE and done != "all":
                shown = [c for c in all_here if c["event_at"] and _after(c["event_at"], cutoff)]
            folded = len(all_here) - len(shown)
            shown.sort(key=lambda c: c["event_at"], reverse=True)
            columns.append({"key": col, "name": COLUMN_NAMES[col], "cards": shown, "folded": folded,
                            "total": len(all_here)})  # fmt: skip
        for c in rows:
            c["child_cards"] = [by_id[i] for i in c["children"] if i in by_id]
        chips = [{"key": "", "label": "All", "needs": _needs(visible)}]
        chips.append({"key": "_root", "label": project.root_name,
                      "needs": _needs([c for c in visible if c["sub"] == ""])})  # fmt: skip
        for s in subs:
            chips.append(
                {"key": s, "label": s, "needs": _needs([c for c in visible if c["sub"] == s])}
            )
        back = str(request.url.path) + (("?" + request.url.query) if request.url.query else "")
        return templates.TemplateResponse(request, "board.html", {
            "project": project, "columns": columns, "chips": chips, "sub": sub, "older": older,
            "done": done, "last": last, "last_ok": last_ok, "syncing": board.syncing(name),
            "gone": gone, "hidden": sum(1 for c in in_sub if c["col"] == HIDDEN), "back": back,
            "movable": [*COLUMNS, HIDDEN], "subs": subs,
            "degraded": json.loads(last["degraded"]) if last else [],
            "warnings": json.loads(last["warnings"]) if last else [],
            "accept_cmd": runner.accept_command(name),
        })  # fmt: skip

    def triage_url(name: str, sub: str = "", **extra: object) -> str:
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in (("sub", sub), *extra.items())
                         if v)  # fmt: skip
        return f"/p/{quote(name, safe='')}/triage" + (f"?{query}" if query else "")

    @app.get("/p/{name}/triage", response_class=HTMLResponse)
    def triage(request: Request, name: str, sub: str = "", select: str = "", confirm: str = "",
               undone: str = "", kept: str = "") -> Response:  # fmt: skip
        undone_n, kept_n = _count(undone), _count(kept)  # numbers only: a link sets these
        project = board.project(name)
        if project is None:
            return not_found("project")
        t = now()
        with board.store() as store:
            rows = [r for r in store.cards(name) if r["col"] == NEEDS_YOU]
            items = []
            for r in sorted(rows, key=lambda r: r["raised_at"] or r["event_at"], reverse=True):
                view = _card_view(r, t)
                view["evidence"] = r["evidence"]
                if _in_sub(view, sub):
                    items.append(view)
            batch = store.last_batch(name)
            batch_cards = (
                [_card_view(r, t) for r in store.batch_cards(batch["id"])] if batch else []
            )
            subs, _ = store.known(name)
        older = [i for i in items if i["older"]]
        return templates.TemplateResponse(request, "triage.html", {
            "project": project, "items": items, "older": len(older), "sub": sub, "subs": subs,
            "select_all": select == "all",
            "confirm_items": older if confirm == "older" and older else None,
            "batch": batch, "batch_cards": batch_cards, "undone": undone_n, "kept": kept_n or 0,
            "url": lambda **kw: triage_url(name, **kw),
        })  # fmt: skip

    @app.post("/p/{name}/triage")
    def triage_post(name: str, action: str = Form(...), ids: list[str] = Form(default=[]),
                    sub: str = Form(default="")) -> Response:  # fmt: skip
        project = board.project(name)
        if project is None:
            return not_found("project")
        if action == "resolve_older":
            # One click never resolves dozens of cards: the confirm page lists exactly which.
            return RedirectResponse(triage_url(name, sub, confirm="older"), 303)
        if action not in ("resolve", "dismiss"):
            return PlainTextResponse("Unknown action.", 400)
        col = DONE if action == "resolve" else HIDDEN
        with board.store() as store:
            store.move_batch(name, ids, col, action, now().isoformat())
        return RedirectResponse(triage_url(name, sub), 303)

    @app.post("/p/{name}/triage/undo")
    def triage_undo(name: str, batch: str = Form(...), sub: str = Form(default="")) -> Response:
        if board.project(name) is None:
            return not_found("project")
        with board.store() as store:
            try:
                restored, kept = store.undo_batch(batch, name, now().isoformat())
            except StoreError as e:
                return PlainTextResponse(str(e), 400)
        return RedirectResponse(triage_url(name, sub, undone=str(restored), kept=str(kept)), 303)

    @app.post("/p/{name}/sync")
    def sync_now(name: str, back: str = Form(default="")) -> Response:
        project = board.project(name)
        if project is None:
            return not_found("project")
        board.refresh_in_background(project)
        return RedirectResponse(_safe_back(back, f"/p/{quote(name, safe='')}"), 303)

    @app.post("/p/{name}/cards")
    def add_card(name: str, title: str = Form(...), col: str = Form(NEXT), sub: str = Form(""),
                 body: str = Form(""), back: str = Form("")) -> Response:  # fmt: skip
        if board.project(name) is None:
            return not_found("project")
        with board.store() as store:
            try:
                store.add_manual(name, sub, col, title, body, now().isoformat())
            except StoreError as e:
                return PlainTextResponse(str(e), 400)
        return RedirectResponse(_safe_back(back, f"/p/{quote(name, safe='')}"), 303)

    @app.get("/i/{card_id:path}", response_class=HTMLResponse)
    def card_page(request: Request, card_id: str) -> Response:
        t = now()
        with board.store() as store:
            row = store.card(card_id)
            manual = store.manual_card(card_id) if row is None else None
            if row is None and manual is None:
                return not_found("card")
            events = store.events(card_id)
            card = _card_view(row if row is not None else _ManualRow(manual), t)
            project = board.project(row["project"] if row is not None else manual["project"])
            children = [_card_view(c, t) for i in card["children"] if (c := store.card(i))]
            source = json.loads(row["source"]) if row is not None else {}
            if source.get("plan_file") and not _inside(source["plan_file"], cfg.plans_dir):
                source["plan_file"] = None  # only a file in the plans folder is named
            extra = {
                "column_derived": row["column_derived"] if row is not None else "",
                "decided": json.loads(row["decided"]) if row is not None else [],
                "evidence": row["evidence"] if row is not None else "",
            }
            subs, github = store.known(project.name) if project else ([], None)
        links = _source_links(project, github, source) if project else {}
        return templates.TemplateResponse(request, "card.html", {
            "card": card, "events": events, "children": children, "source": source,
            "links": links, "project": project, "movable": [*COLUMNS, HIDDEN], "subs": subs,
            **extra,
        })  # fmt: skip

    def _act(card_id: str, back: str, fn) -> Response:  # noqa: ANN001
        with board.store() as store:
            try:
                fn(store)
            except StoreError as e:
                return PlainTextResponse(str(e), 400)
        return RedirectResponse(_safe_back(back, "/i/" + quote(card_id, safe="")), 303)

    @app.post("/i/{card_id:path}/move")
    def move(card_id: str, col: str = Form(...), back: str = Form("")) -> Response:
        t = now().isoformat()

        def fn(store: Store) -> None:
            if card_id.startswith("mc:"):
                store.move_manual(card_id, col, t)
            else:
                store.move(card_id, col, t)

        return _act(card_id, back, fn)

    @app.post("/i/{card_id:path}/resolve")
    def resolve(card_id: str, back: str = Form("")) -> Response:
        t = now().isoformat()
        if card_id.startswith("mc:"):
            return _act(card_id, back, lambda s: s.move_manual(card_id, DONE, t))
        return _act(card_id, back, lambda s: s.resolve(card_id, t))

    @app.post("/i/{card_id:path}/dismiss")
    def dismiss(card_id: str, back: str = Form("")) -> Response:
        t = now().isoformat()
        if card_id.startswith("mc:"):
            return _act(card_id, back, lambda s: s.move_manual(card_id, HIDDEN, t))
        return _act(card_id, back, lambda s: s.dismiss(card_id, t))

    @app.post("/i/{card_id:path}/sub")
    def set_sub(card_id: str, sub: str = Form(""), back: str = Form("")) -> Response:
        t = now().isoformat()

        def fn(store: Store) -> None:
            row = store.card(card_id)
            if row is None:
                raise StoreError("only a synced card's sub-project can change")
            store.set_sub(card_id, sub, store.known(row["project"])[0], t)

        return _act(card_id, back, fn)

    @app.post("/i/{card_id:path}/delete")
    def delete(card_id: str, back: str = Form("")) -> Response:
        if not card_id.startswith("mc:"):
            return PlainTextResponse("Only a card you added can be deleted.", 400)
        return _act(card_id, back, lambda s: s.delete_manual(card_id, now().isoformat()))

    return app


class _ManualRow:
    """A manual card shaped like a board row."""

    def __init__(self, r: object) -> None:
        # sqlite3.Row iterates over values, not keys, so keys() is needed here.
        self._d = {k: r[k] for k in r.keys()}  # type: ignore[attr-defined]  # noqa: SIM118
        self._d.setdefault("parent", None)

    def keys(self) -> list[str]:
        return list(self._d)

    def __getitem__(self, k: str) -> object:
        if k == "col":
            return self._d["col"]
        return self._d[k]


def _count(value: str) -> int | None:
    """A count from a query parameter, or None for anything that is not one."""
    return int(value) if value.isdigit() and len(value) < 7 else None


def _inside(path: str, folder: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(folder.resolve())
    except (OSError, ValueError):
        return False


def _in_sub(card: dict, sub: str) -> bool:
    """Whether a card shows under the chosen game ("" = all, "_root" = the project's root)."""
    if sub == "_root":
        return card["sub"] == ""
    return not sub or card["sub"] == sub


def _needs(cards: list[dict]) -> int:
    return sum(1 for c in cards if c["col"] == NEEDS_YOU and not c["older"])


def _after(ts: str, cutoff: datetime) -> bool:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) >= cutoff
    except ValueError:
        return False


def _source_links(project: Project, github: str | None, source: dict) -> dict:
    """Links built from config and the card's own fields, never from a URL in a transcript."""
    links: dict[str, str] = {}
    gh = project.github or github
    if source.get("number") and gh:
        links["pr"] = f"https://github.com/{gh}/pull/{int(source['number'])}"
    path, line = source.get("path"), source.get("line")
    if path:
        root = project.path.resolve()
        target = (root / path).resolve()
        if target.is_relative_to(root):
            links["editor"] = (
                "vscode://file" + quote(str(target)) + (f":{int(line)}" if line else "")
            )
        if gh and source.get("ref_sha"):
            links["github"] = (f"https://github.com/{gh}/blob/{source['ref_sha']}/{quote(path)}"
                               + (f"#L{int(line)}" if line else ""))  # fmt: skip
    return links
