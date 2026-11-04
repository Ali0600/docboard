"""Which replies answer a Needs-you item (synthetic replies, none copied from a transcript)."""

from __future__ import annotations

import pytest

from docboard.answers import Ask, answered, options, spoken

AB = frozenset("AB")
ABC = frozenset("ABC")


def one_decide(reply: str) -> set[int]:
    return set(answered(reply, [Ask(1, "Decide", ABC)]))


@pytest.mark.parametrize(
    "reply",
    [
        "do A",
        "Do B.",
        "C",
        "ok, do A",
        "A, and keep going",
        "B and write it down too",
        "go with A",
        "let's try B",
        "Do C please",
        "do A) the cheaper one",
        "C) the smaller one",
        "A - test it twice",
        "A — the cheaper one",
        "do a and c",
        "Option B",
        "do A, but why is the other one slower?",
        "First a word on the colours.\nAlso, let's do B, leave the rest",
    ],
)
def test_a_letter_the_item_offers_answers_it(reply: str) -> None:
    assert one_decide(reply) == {1}


@pytest.mark.parametrize(
    "reply",
    [
        "ok",
        "yes",
        "Ok, what is left on the plan?",
        "A window opened and nothing else happened",  # the article, not option A
        "do a full pass over the notes",  # the article again
        "Remove B from the menu",
        "let's do A/B testing on the page",
        "What do A and B do differently?",
        "do D",  # not an option this item offers
        "> do A, you wrote\nwhy that one",  # a line the owner quoted
        "<pasted_content>do A</pasted_content>\nsomething else",  # inside a paste
    ],
)
def test_anything_else_is_no_answer(reply: str) -> None:
    assert one_decide(reply) == set()


def test_numbered_lines_answer_their_items_and_leave_the_rest() -> None:
    asks = [Ask(1, "Decide", ABC), Ask(2, "Decide", AB), Ask(3, "Decide", ABC)]
    got = answered("What did you mean by the second one?\n\n1. do A\n\n3. do B", asks)
    assert got == {1: "1. do A", 3: "3. do B"}
    assert set(answered("1: B, 2: A", asks)) == {1, 2}
    assert set(answered("2. A party of four", asks)) == set()  # the article after a number
    assert set(answered("7. do A", asks)) == set()  # no item 7: some other list
    assert set(answered("1: D", asks)) == set()  # item 1 offers no D
    assert set(answered("1. done", asks)) == set()  # a Decide is not done, it is decided


def test_free_text_answers_only_what_no_numbered_line_took() -> None:
    asks = [Ask(1, "Decide", ABC), Ask(2, "Decide", AB)]
    assert set(answered("do B\n1. what would C cost?", asks)) == {2}


def test_a_letter_two_items_offer_answers_neither_unless_it_says_both() -> None:
    asks = [Ask(1, "Decide", ABC), Ask(2, "Decide", AB)]
    assert set(answered("do B", asks)) == set()
    assert set(answered("do A for both", asks)) == {1, 2}
    assert set(answered("do C", asks)) == {1}  # only the first offers C


def test_done_answers_the_one_do_item() -> None:
    assert set(answered("done", [Ask(1, "Do", frozenset())])) == {1}
    assert set(answered("Ok, all done.", [Ask(1, "Do", frozenset()), Ask(2, "Decide", AB)])) == {1}
    assert set(answered("done", [Ask(1, "Do", frozenset()), Ask(2, "Do", frozenset())])) == set()
    two = [Ask(1, "Do", frozenset()), Ask(2, "Do", frozenset())]
    assert set(answered("1. done\n2. already done", two)) == {1, 2}
    assert set(answered("ok I set it up", [Ask(1, "Do", frozenset())])) == set()  # prose
    assert set(answered("Done with the tests? Then go on", [Ask(1, "Do", frozenset())])) == set()


def test_a_do_item_that_offers_letters_takes_one() -> None:
    assert set(answered("do A", [Ask(1, "Do", AB)])) == {1}


def test_an_answer_is_quoted_short() -> None:
    words = answered("do A, " + "and more " * 40, [Ask(1, "Decide", AB)])[1]
    assert words.startswith("do A, and more") and len(words) == 120 and words.endswith("…")


def test_options_on_their_own_lines_and_in_the_older_inline_style() -> None:
    body = "*Why:* reasons.\n\n*Options:*\n- **A)** one\n- **B)** two\n\n*I'd pick:* **A**."
    assert options(body) == AB
    assert options("*Options:* A) leave it · B) change it · C) drop it") == ABC
    assert options("*Why:* reasons only.") == frozenset()


@pytest.mark.parametrize(
    ("text", "typed"),
    [
        ("do A", True),
        ("<pasted_content>a log</pasted_content>\ndo A", True),
        (
            "<command-message>wrap-up</command-message>\n<command-name>/wrap-up</command-name>",
            False,
        ),
        ("<bash-input>do A</bash-input>", False),
        ("<scheduled-task>do A</scheduled-task>", False),
        ("<system-reminder>\nThe user started a task</system-reminder>", False),
        ("<task-notification>do A</task-notification>", False),
        ("This session is being continued from a previous conversation.", False),
        ("Do this task here: “do A”", False),
    ],
)
def test_only_turns_the_owner_typed_can_answer(text: str, typed: bool) -> None:
    assert spoken(text) is typed
