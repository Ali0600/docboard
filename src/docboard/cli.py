"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from docboard import __version__, config, privacy, sync
from docboard.derive import COLUMNS


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
    if not args.dry_run:
        print("sync: only --dry-run exists until the board's database lands", file=sys.stderr)
        return 2
    cfg = config.load(Path(args.config) if args.config else None)
    projects = [cfg.project(args.project)] if args.project else list(cfg.projects)
    status = 0
    for project in projects:
        result = sync.collect(cfg, project, datetime.now(UTC))
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
    moved = Counter()
    for i in a.keys() & b.keys():
        if not keep(a[i]):
            continue
        for f in ("sub", "column"):
            if a[i][f] != b[i][f]:
                moved[f] += 1
        if a[i]["source"].get("path") != b[i]["source"].get("path"):
            moved["path"] += 1
    print(f"only before: {len(gone)}  only after: {len(new)}  in both: {len(a.keys() & b.keys())}")
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
    s.set_defaults(func=_sync)

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
