"""Whether the owner answered a Needs-you item in chat, read from their own typed turns.

A reply answers an item only when it names it: an option letter the item offers ("do A",
"A, go ahead", "go with B"), a numbered line ("1: B", "2. do A"), or "done" for the one Do item
of a block. A bare letter that two of the block's Decide items both offer answers neither, unless
the reply says "for both". Anything else ("ok", "yes", a question, a new request) is no answer
(docs/DECISIONS.md, 2026-11-04, has the survey these rules came from).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

QUOTE_CAP = 120


@dataclass(frozen=True)
class Ask:
    """One item of a Needs-you block, as a reply sees it."""

    n: int
    kind: str  # Decide | Do
    options: frozenset[str]


# The block's own options: a line each ("- **A)** text", "* B) text", "**C)** text"), or the
# older style on one line ("*Options:* A) leave it · B) make it 170").
_OPTION = re.compile(
    r"(?m)^[ \t]*(?:[-*+][ \t]+)?\*{0,2}\(?([A-H])\)\*{0,2}[ \t]"
    r"|(?:(?i:options)\*{0,2}:\*{0,2}|[·•|])[ \t]*\*{0,2}\(?([A-H])\)"
)
# Turns the app or a command wrote into the owner's slot: "<command-name>", "<bash-input>",
# "<scheduled-task>", "<system-reminder>", "<task-notification>". A paste is the owner's own turn.
_APP_TAG = re.compile(r"\s*<([a-z][\w-]*)[\s>/]")
_APP_PREFIXES = (
    "This session is being continued",
    "Continue from where you left off",
    "[Request interrupted",
    "Do this task here:",  # the app's "Do it here" button on a proposal
)
_PASTED = re.compile(r"<pasted_content\b[^>]*>.*?(?:</pasted_content>|\Z)", re.S)
_QUOTED = re.compile(r"(?m)^[ \t]*>.*$")  # a line the owner quoted is not their answer
# "1. do A", "2: B", "#3 - done", "item 2) A" at a line's start; ", 2: B" inside a line.
_NUMBER = re.compile(
    r"(?m)(?:^[ \t]*(?:#|(?i:item)[ \t]*)?|[,;][ \t]*)"
    r"(\d{1,2})(?:[ \t]*[.:)–—]|[ \t]+-)[ \t]*"
)
_VERB = r"(?i:do|pick|choose|go[ \t]+with|go[ \t]+for|try|take|option)"
# A letter that is a word of its own: not "A/B testing", "A-side", "A's".
_ALONE = r"(?![\w'’]|[/&-]\w)"
_VERB_LETTER = re.compile(
    rf"\b{_VERB}[ \t]+(?:(?i:option)[ \t]+)?\(?([A-H]){_ALONE}"
    # a lower-case letter only where it cannot be the article: "do a and c", "pick b."
    rf"|\b{_VERB}[ \t]+([a-h])"
    r"(?=[ \t]*$|[ \t]*[,;:!)]|[ \t]*\.(?!\w)|[ \t]+(?i:and)[ \t]+[a-hA-H]\b)",
    re.M,
)
_LEAD = r"(?:(?i:ok(?:ay)?|yes|yeah|yep|sure|alright)[ \t]*[,.!]?[ \t]+)?(?:(?i:option)[ \t]+)?"
# A letter opening a line: "A", "A, go ahead", "B and keep the rest", "C) the smaller one";
# never the article ("A window opened").
_BARE_LETTER = re.compile(
    rf"(?m)^[ \t]*{_LEAD}(?:\(?([A-H])\)|([A-H])(?=[ \t]*$|[ \t]*[,;:!&+–—]"
    r"|[ \t]*\.(?!\w)|[ \t]+-(?:[ \t]|$)"
    r"|[ \t]+(?i:and|or|plus|for|please|then|but|so|with|first|too|both|all)\b))"
)
_DONE = re.compile(
    r"^[ \t]*(?:(?i:ok(?:ay)?|yes|yep|yeah)[ \t]*[,.!]?[ \t]*)?"
    r"(?i:(?:already[ \t]+|all[ \t]+)?done|did[ \t]+it|did[ \t]+that|i[ \t]+did(?:[ \t]+it)?"
    r"|finished|completed|all[ \t]+set)\b"
)
_BOTH = re.compile(r"(?i)\b(?:for|to|on)[ \t]+(?:both|all|each)\b|\bboth\b")
_SENTENCE_END = re.compile(r"[.!?\n]")
_ASKING = re.compile(
    r"(?i)\b(?:what|how|why|which|where|when|who|does|do|is|are|should|could|would|can|will|did)\b"
)


def options(body: str) -> frozenset[str]:
    """The option letters an item's body offers."""
    return frozenset(a or b for a, b in _OPTION.findall(body or ""))


def spoken(text: str) -> bool:
    """Whether the owner typed this turn, rather than ran a slash or `!` command, or the app
    wrote it (a scheduled task, a reminder, a notification, a compaction, a button)."""
    m = _APP_TAG.match(text or "")
    if m and m.group(1) != "pasted_content":
        return False
    return not (text or "").lstrip().startswith(_APP_PREFIXES)


def _asks(text: str, end: int) -> bool:
    """Whether the sentence running on from text[end] ends with a question mark."""
    m = _SENTENCE_END.search(text, end)
    return m is not None and m.group(0) == "?"


def _in_question(text: str, start: int, end: int) -> bool:
    """Whether text[start:end] sits in a question: its sentence ends with a question mark and a
    question word comes before it. "What do A and B mean?" asks about the options; "do A, but
    why is B slow?" picks one."""
    if not _asks(text, end):
        return False
    begun = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    return _ASKING.search(text, begun, start) is not None


def _letters(text: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for pattern in (_VERB_LETTER, _BARE_LETTER):
        for m in pattern.finditer(text):
            letter = next(g for g in m.groups() if g)
            if not _in_question(text, m.start(), m.end()):
                found.append((m.start(), letter.upper()))
    return [letter for _, letter in sorted(found)]


def _done(text: str) -> bool:
    first = text.strip().split("\n", 1)[0]
    m = _DONE.match(first)
    return m is not None and not _asks(first, m.end())  # "done?" asks, it does not tell


def _quote(text: str) -> str:
    said = " ".join(text.split())
    return said if len(said) <= QUOTE_CAP else said[: QUOTE_CAP - 1].rstrip() + "…"


def _segments(text: str) -> tuple[str, dict[int, tuple[str, str]]]:
    """The reply's free text, and each numbered part as (its text, the words quoted for it)."""
    free: list[str] = []
    numbered: dict[int, tuple[str, str]] = {}
    marks = list(_NUMBER.finditer(text))
    free.append(text[: marks[0].start()] if marks else text)
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        said = text[m.start() : end].lstrip(" \t\n,;")
        numbered.setdefault(int(m.group(1)), (text[m.end() : end], said))
    return "".join(free), numbered


def answered(reply: str, asks: Sequence[Ask]) -> dict[int, str]:
    """The items of one block this reply answers, each with the words that answered it."""
    text = _QUOTED.sub("", _PASTED.sub("", reply or ""))
    by_n = {a.n: a for a in asks}
    free, numbered = _segments(text)
    out: dict[int, str] = {}
    for n, (part, said) in numbered.items():
        ask = by_n.get(n)
        if ask is None:
            continue  # a number the block does not hold: some other list
        if set(_letters(part)) & ask.options or (ask.kind == "Do" and _done(part)):
            out[n] = _quote(said)
    # The free text answers only what no numbered line took up, and only when it can say which.
    rest = [a for a in asks if a.n not in numbered]
    letters = set(_letters(free))
    if letters:
        fits = [a for a in rest if letters & a.options]
        if len(fits) == 1 or (fits and _BOTH.search(free)):
            for a in fits:
                out[a.n] = _quote(free)
    if _done(free):
        dos = [a for a in rest if a.kind == "Do"]
        if len(dos) == 1:
            out[dos[0].n] = _quote(free)
    return out
