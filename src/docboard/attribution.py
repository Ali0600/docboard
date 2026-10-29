"""Which sub-project (a game of a monorepo) a card belongs to.

Each rule answers a slug or None, and the caller tries them in order, first match wins, storing
which rule fired so the board can show why a card sits where it does.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from docboard.config import ConfigError

_WORKTREE = re.compile(r"^\.claude/worktrees/[^/]+/")
VOTE_MIN_HITS = 3
VOTE_MIN_SHARE = 0.6


@dataclass
class Attributor:
    parent: str  # "games"; "" for a project without sub-projects
    slugs: tuple[str, ...] = ()
    aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    branches: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        owner: dict[str, str] = {}
        self._patterns: list[tuple[str, re.Pattern[str]]] = []
        for slug in self.slugs:
            for alias in self.aliases.get(slug, ()):
                key = alias.casefold()
                if key in owner and owner[key] != slug:
                    raise ConfigError(f"alias {alias!r} names both {owner[key]} and {slug}")
                owner[key] = slug
                pat = re.compile(r"(?<![\w+])" + re.escape(alias) + r"(?!\w)", re.IGNORECASE)
                self._patterns.append((slug, pat))

    @classmethod
    def build(
        cls,
        parent: str,
        slugs: Iterable[str],
        names: dict[str, str],
        extra_aliases: dict[str, tuple[str, ...]],
        extra_branches: dict[str, tuple[str, ...]],
    ) -> Attributor:
        slugs = tuple(slugs)
        aliases: dict[str, tuple[str, ...]] = {}
        branches: dict[str, tuple[str, ...]] = {}
        for s in slugs:
            found = [s, s.replace("-", " ")]
            if names.get(s):
                found.append(names[s])
            found.extend(extra_aliases.get(s, ()))
            aliases[s] = tuple(dict.fromkeys(a for a in found if a.strip()))
            branches[s] = tuple(dict.fromkeys([f"{s}-", f"game/{s}-", *extra_branches.get(s, ())]))
        unknown = (set(extra_aliases) | set(extra_branches)) - set(slugs)
        if unknown:
            raise ConfigError(f"aliases or branches for unknown sub-projects: {sorted(unknown)}")
        return cls(parent=parent, slugs=slugs, aliases=aliases, branches=branches)

    def by_path(self, rel: str) -> str | None:
        """`games/<slug>/…`, also inside a worktree (`.claude/worktrees/<name>/games/<slug>/…`)."""
        if not self.parent:
            return None
        rel = _WORKTREE.sub("", rel)
        head = self.parent + "/"
        if not rel.startswith(head):
            return None
        slug = rel[len(head) :].split("/", 1)[0]
        return slug if slug in self.slugs else None

    def by_text(self, text: str) -> str | None:
        """The one sub-project the text names; None when it names none or several."""
        hit = {slug for slug, pat in self._patterns if pat.search(text)}
        return hit.pop() if len(hit) == 1 else None

    def by_branch(self, branch: str) -> str | None:
        """The sub-project whose branch prefix the branch starts with, the longest one winning.
        An agent's own prefix (`codex/`, `claude/`) is looked past when nothing matches."""
        found = self._by_branch(branch)
        if found is None and "/" in branch:
            found = self._by_branch(branch.split("/", 1)[1])
        return found

    def _by_branch(self, branch: str) -> str | None:
        best: tuple[int, str] | None = None
        for slug in self.slugs:
            for prefix in self.branches.get(slug, ()):
                if branch.startswith(prefix) and (best is None or len(prefix) > best[0]):
                    best = (len(prefix), slug)
        return best[1] if best else None

    def spread(self, paths: Iterable[str]) -> int:
        """How many sub-projects the paths touch."""
        return len({s for s in (self.by_path(p) for p in paths) if s})

    def by_files(self, paths: Iterable[str], min_hits: int = VOTE_MIN_HITS) -> str | None:
        """The sub-project owning at least 60% of the files, and at least `min_hits` of them."""
        paths = list(paths)
        counts = Counter(s for s in (self.by_path(p) for p in paths) if s)
        if not counts:
            return None
        slug, hits = counts.most_common(1)[0]
        if hits >= min_hits and hits / len(paths) >= VOTE_MIN_SHARE:
            return slug
        return None
