"""Re-raise detection for owner-facing items.

Ported from ~/.claude/scripts/turn-audit.py (toks, similar and STOP, 2026-09-08), unchanged in
behaviour: two item titles are "the same ask" when their content-word sets overlap by a Jaccard
index of at least 0.4.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

STOP = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "it", "is", "are", "be",
        "you", "your", "i", "me", "my", "we", "this", "that", "with", "once", "then", "now",
        "tell", "say", "whether", "if", "when",
    }
)  # fmt: skip
_WORD = re.compile(r"[a-z0-9]+")


def toks(s: str) -> set[str]:
    return {w for w in _WORD.findall(s.lower()) if w not in STOP}


def jaccard(a: str, b: str) -> float:
    ta, tb = toks(a), toks(b)
    return len(ta & tb) / max(1, len(ta | tb))


def similar(h: str, prev: Iterable[str], threshold: float = 0.4) -> bool:
    return any(jaccard(h, p) >= threshold for p in prev)
