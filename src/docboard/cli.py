"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from docboard import __version__, config, locate, privacy, runner, sync
from docboard.derive import COLUMNS
from docboard.store import DEFAULT_DB, ApplyReport, FloorError

LOOPBACK = ("127.0.0.1", "localhost", "::1")
DEFAULT_PORT = 8742


def _privacy(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    findings = privacy.scan_repo(root)
    for f in findings:
        print(f"{f.path}:{f.line}: {f.kind}: {f.excerpt}")
    print(f"privacy: {len(findings)} finding(s) in {root}")
    return 1 if findings else 0


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB"


def _sync(args: argparse.Namespace) -> int:
    if args.accept_drop and (args.dry_run or not args.project):
        print(
            "sync: --accept-drop stores one project's sync: give --project, without --dry-run",
            file=sys.stderr,
        )
        return 2
    if not args.dry_run:
        return _sync_store(args)
    cfg = config.load(Path(args.config) if args.config else None)
    projects = [cfg.project(args.project)] if args.project else list(cfg.projects)
    status = 0
    for project in projects:
        try:
            result = sync.collect(cfg, project, datetime.now(UTC))
        except Exception as e:  # one broken project never stops the others
            print(f"{project.name}: FAILED: {runner.describe(e, project)}")
            traceback.print_exc()
            status = 1
            continue
        c, t = result.counts, result.timings
        ref = f"{result.ref} ({(result.ref_sha or '')[:8]})" if result.ref else "the working tree"
        print(f"{result.project}: docs at {ref}, GitHub {result.github or '-'}")
        print(f"  backlog        {c['backlog']} bullets in {c['decision_files']} DECISIONS file(s)")
        print(f"  roadmap        {c['roadmap']} items in {c['roadmap_files']} file(s)")
        print(
            f"  pull requests  {c['prs']} ({c['prs_merged']} merged, {c['prs_closed']} closed,"
            f" {c['prs_open']} open) in {t['github']:.1f} s"
        )
        print(
            f"  transcripts    {c['transcript_files']} files, {_gb(c['transcript_bytes'])} in"
            f" {t['transcripts']:.1f} s ({c['transcript_foreign']} from other folders skipped)"
        )
        print(f"    plans        {c['plan_cards']} cards from {c['plan_calls']} plan calls")
        print(
            f"    needs you    {c['needs_cards']} cards from {c['needs_items']} items"
            " (re-raises merged)"
        )
        print(
            f"    questions    {c['questions']} ({c['questions_waiting']} waiting)"
            f"; wrap-up asks {c['wrap_asks']}"
        )
        for d in result.degraded:
            print(f"  DEGRADED {d}")
        for w in result.warnings:
            print(f"  warning: {w}")
        print()
        print(sync.table(result))
        print(
            "\n(n) = from the last 14 days; nested PRs are counted on their plan;"
            f" {t['total']:.1f} s"
        )
        if args.sample:
            _samples(result, args.sample)
        if args.json:
            path = Path(args.json.replace("{project}", result.project))
            sync.write_json(result, path)
            print(f"\nwrote {path}")
        if args.baseline:
            base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
            problems = sync.floor_problems(c, base.get("counts", {}))
            for p in problems:
                print(f"FLOOR {p}")
            if problems:
                status = 1
    return status


def _sync_store(args: argparse.Namespace) -> int:
    cfg = config.load(Path(args.config) if args.config else None)
    projects = [cfg.project(args.project)] if args.project else list(cfg.projects)
    db = Path(args.db) if args.db else DEFAULT_DB
    status = 0
    for project in projects:
        try:
            result, report = runner.run_sync(cfg, project, db, accept_drop=args.accept_drop)
        except FloorError as e:
            print(f"{project.name}: REFUSED, nothing written: {e}")
            print(f"  if the drop is real, accept it: {runner.accept_command(project.name)}")
            status = 1
            continue
        except runner.Busy as e:
            print(f"{project.name}: {e}")
            status = 1
            continue
        except runner.SyncFailed as e:
            print(f"{project.name}: FAILED, nothing written: {e}")
            traceback.print_exception(e.__cause__ or e, file=sys.stderr)
            status = 1
            continue
        _report(project, result, report)
    return status


def _report(project: config.Project, result: sync.SyncResult, report: ApplyReport) -> None:
    print(
        f"{project.name}: {len(result.cards)} cards in {result.timings.get('total', 0):.1f} s;"
        f" {report.added} new, {report.updated} updated, {report.gone} gone,"
        f" {report.renamed} renamed, {report.followed_source} moves dropped (source changed)"
    )
    for a in report.accepted:
        print(f"  ACCEPTED {a}: the new baseline")
    if report.reopened:
        print(f"  {report.reopened} reopened: raised again after you resolved them")
    if report.adopted:
        print(f"  {report.adopted} moved here from another board")
    if report.kept_open:
        print(f"  {report.kept_open} open asks kept although their transcript is gone")
    for w in report.warnings():
        print(f"  warning: {w}")
    for d in result.degraded:
        print(f"  DEGRADED {d} (kept the last good {', '.join(report.kept_frozen)} cards)")


def _board(args: argparse.Namespace) -> int:
    cfg = config.load(Path(args.config) if args.config else None)
    cwd = Path(args.cwd) if args.cwd else Path.cwd()
    found = locate.locate(cfg, cwd)
    if found is None:
        names = ", ".join(p.name for p in cfg.projects)
        print(f"board: {cwd} is in none of the configured projects ({names});"
              f" add it to {config.DEFAULT_CONFIG.relative_to(config.REPO_ROOT)}")  # fmt: skip
        return 2
    project, sub = found.project, found.sub
    status = 0
    if not args.no_sync:
        db = Path(args.db) if args.db else DEFAULT_DB
        try:
            result, report = runner.run_sync(cfg, project, db)
        except (FloorError, runner.Busy, runner.SyncFailed) as e:
            print(f"{project.name}: sync not stored ({e}); the board shows the last good one")
            if isinstance(e, FloorError):
                print(f"  if the drop is real, accept it: {runner.accept_command(project.name)}")
            status = 1
        except Exception as e:  # the link is printed whatever the sync did
            print(f"{project.name}: sync not stored ({runner.describe(e, project)});"
                  " the board shows the last good one")  # fmt: skip
            traceback.print_exc()
            status = 1
        else:
            _report(project, result, report)
            if sub is not None and sub not in result.subs:
                print(f"  {sub!r} has no board of its own yet; showing the whole project")
                sub = None
    url = locate.board_url(args.port, project.name, sub)
    print(f"board: {url}")
    state = locate.page_state(url)
    if state == "up":
        print("page: running")
    elif state == "stale":
        print(f"page: running, but started before {project.name} was in the config: restart it")
    elif state == "down":
        print(f"page: not running; start it with: cd {config.REPO_ROOT} && uv run docboard serve")
    else:
        print(f"page: {state}")
    return status


def _serve(args: argparse.Namespace) -> int:
    if args.host not in LOOPBACK:
        print(f"serve: refusing {args.host!r}; the board answers on loopback only", file=sys.stderr)
        return 2
    import uvicorn

    from docboard.web import create_app

    cfg = config.load(Path(args.config) if args.config else None)
    app = create_app(cfg, Path(args.db) if args.db else DEFAULT_DB, args.port)
    host = "[::1]" if args.host == "::1" else args.host
    print(f"docboard on http://{host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", server_header=False)
    return 0


def _samples(result: sync.SyncResult, n: int) -> None:
    names = {"": result.root_name}
    for col in COLUMNS:
        sel = [c for c in result.cards if c.column == col and c.parent is None and not c.older][:n]
        if not sel:
            continue
        print(f"\n{col}:")
        for c in sel:
            badges = f" [{'; '.join(c.badges)}]" if c.badges else ""
            print(
                f"  - {names.get(c.sub, c.sub)} ({c.sub_reason}) {c.kind}: {c.title[:90]}{badges}"
            )


def _diff(args: argparse.Namespace) -> int:
    a = {c["id"]: c for c in json.loads(Path(args.before).read_text(encoding="utf-8"))["cards"]}
    b = {c["id"]: c for c in json.loads(Path(args.after).read_text(encoding="utf-8"))["cards"]}
    kinds = set(args.kinds.split(",")) if args.kinds else None

    def keep(c: dict) -> bool:
        return kinds is None or c["kind"] in kinds

    gone = [a[i] for i in a.keys() - b.keys() if keep(a[i])]
    new = [b[i] for i in b.keys() - a.keys() if keep(b[i])]
    both = [i for i in a.keys() & b.keys() if keep(a[i])]
    moved = Counter()
    for i in both:
        for f in ("sub", "column"):
            if a[i][f] != b[i][f]:
                moved[f] += 1
        if a[i]["source"].get("path") != b[i]["source"].get("path"):
            moved["path"] += 1
    print(f"only before: {len(gone)}  only after: {len(new)}  in both: {len(both)}")
    print(
        "changed in both: " + ", ".join(f"{k} {v}" for k, v in sorted(moved.items()))
        if moved
        else "changed in both: none"
    )
    for label, rows in (("only before", gone), ("only after", new)):
        for c in sorted(rows, key=lambda c: c["id"])[: args.limit]:
            print(f"  {label}: {c['kind']} {c['id']} {c['title'][:80]}")
    return 1 if (gone or new) and args.strict else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docboard", description=__doc__)
    parser.add_argument("--version", action="version", version=f"docboard {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("privacy", help="scan this repo's files for personal identifiers")
    p.add_argument("--root", default=".", help="repo to scan (default: current directory)")
    p.set_defaults(func=_privacy)

    s = sub.add_parser("sync", help="read every source and derive the cards")
    s.add_argument("--dry-run", action="store_true", help="print counts and cards; store nothing")
    s.add_argument("--project", help="one project from the config (default: all)")
    s.add_argument(
        "--config", help="config file (default: config/projects.toml or $DOCBOARD_CONFIG)"
    )
    s.add_argument("--json", help="also write every card to this file ({project} is replaced)")
    s.add_argument("--sample", type=int, default=0, help="print this many recent cards per column")
    s.add_argument("--baseline", help="a previous --json file: fail if a source collapsed since")
    s.add_argument("--db", help="the board's database (default: data/board.db)")
    s.add_argument(
        "--accept-drop",
        action="store_true",
        help="store a sync the floor refused because a source really shrank (needs --project)",
    )
    s.set_defaults(func=_sync)

    v = sub.add_parser("serve", help="the board's page on the loopback address")
    v.add_argument("--host", default="127.0.0.1", help="127.0.0.1, localhost or ::1 only")
    v.add_argument("--port", type=int, default=DEFAULT_PORT)
    v.add_argument("--config", help="config file (default: config/projects.toml)")
    v.add_argument("--db", help="the board's database (default: data/board.db)")
    v.set_defaults(func=_serve)

    b = sub.add_parser("board", help="sync the board of the project you are in and print its link")
    b.add_argument("--cwd", help="a folder in the project (default: the current directory)")
    b.add_argument("--no-sync", action="store_true", help="only find the board and its link")
    b.add_argument("--port", type=int, default=DEFAULT_PORT)
    b.add_argument("--config", help="config file (default: config/projects.toml)")
    b.add_argument("--db", help="the board's database (default: data/board.db)")
    b.set_defaults(func=_board)

    d = sub.add_parser("diff", help="compare the cards of two --json files")
    d.add_argument("before")
    d.add_argument("after")
    d.add_argument("--kinds", help="only these kinds, comma separated (idea,roadmap,pr,...)")
    d.add_argument("--limit", type=int, default=20, help="rows to list per side")
    d.add_argument("--strict", action="store_true", help="exit 1 if any id appears or vanishes")
    d.set_defaults(func=_diff)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
