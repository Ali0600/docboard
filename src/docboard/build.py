"""Which code a running page has: so `docboard board` can say when the page needs a restart.

A hash of the package's own files (code, templates, script, styles), so it works for a checkout
and a tool install alike and changes with any edit, committed or not.
"""

from __future__ import annotations

import hashlib
from functools import cache
from pathlib import Path

from docboard import __version__

HERE = Path(__file__).parent
SUFFIXES = (".py", ".html", ".js", ".css")


@cache
def code_id() -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in HERE.rglob("*") if p.suffix in SUFFIXES and p.is_file()):
        h.update(path.relative_to(HERE).as_posix().encode("utf-8") + b"\0")
        h.update(path.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    return f"{__version__}+{h.hexdigest()[:12]}"
