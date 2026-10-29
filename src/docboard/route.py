"""Which configured project's board a session's item belongs to.

A session often starts in one project's folder and works in another's (a games repo session that
builds the board tool). Every project's sync reads the same transcripts and asks the same
questions here, so each item gets the same owner in every sync and sits on exactly one board.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from docboard.attribution import Attributor
from docboard.transcripts import Touch

TURN_MIN = 3  # touches a turn needs before its files decide the board
TURN_SHARE = 0.6


@dataclass
class Router:
    names: tuple[str, ...]
    repos: dict[str, str] = field(default_factory=dict)  # "owner/name" casefolded -> project
    _patterns: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)
    _subs: dict[str, Attributor] = field(default_factory=dict)  # a project's games name it too

    @classmethod
    def build(
        cls,
        projects: Iterable[tuple[str, str | None]],
        subs: dict[str, Attributor] | None = None,
    ) -> Router:
        """From (project name, its GitHub "owner/name" or None) pairs, and each project's
        sub-projects (a game's name names the games repo)."""
        projects = list(projects)
        repos = {repo.casefold(): name for name, repo in projects if repo}
        patterns = [
            (name, re.compile(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", re.IGNORECASE))
            for name, _ in projects
        ]
        return cls(names=tuple(n for n, _ in projects), repos=repos, _patterns=patterns,
                   _subs=dict(subs or {}))  # fmt: skip

    def project_of_repo(self, repo: str) -> str | None:
        return self.repos.get(repo.casefold())

    def by_name(self, text: str) -> str | None:
        """The one configured project the text names; None when it names none or several."""
        text = text or ""
        hit = {name for name, pat in self._patterns if pat.search(text)}
        hit |= {name for name, att in self._subs.items() if att.mentions(text)}
        return hit.pop() if len(hit) == 1 else None

    def by_touches(self, touches: Iterable[Touch]) -> str | None:
        """The project holding most of the files, when there are enough of them to say."""
        votes = Counter(t.project for t in touches if t.project)
        total = sum(votes.values())
        if total < TURN_MIN:
            return None
        name, hits = votes.most_common(1)[0]
        return name if hits / total >= TURN_SHARE else None
