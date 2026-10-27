"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from docboard import __version__, privacy


def _privacy(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    findings = privacy.scan_repo(root)
    for f in findings:
        print(f"{f.path}:{f.line}: {f.kind}: {f.excerpt}")
    print(f"privacy: {len(findings)} finding(s) in {root}")
    return 1 if findings else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="docboard", description=__doc__)
    parser.add_argument("--version", action="version", version=f"docboard {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("privacy", help="scan this repo's files for personal identifiers")
    p.add_argument("--root", default=".", help="repo to scan (default: current directory)")
    p.set_defaults(func=_privacy)
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
