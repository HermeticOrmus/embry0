"""Final-text assembly (#329): messages joined with a blank line, tail cut on a line."""

from __future__ import annotations

from embry0.agents.output_text import MAX_OUTPUT_CHARS, MESSAGE_SEPARATOR, FinalText, tail_on_line_boundary


def test_messages_are_separated_by_a_blank_line() -> None:
    ft = FinalText()
    ft.add_message("I checked the existing tickets.")
    ft.add_message("TITLE: Fix the mailbox quota")
    assert ft.render() == "I checked the existing tickets.\n\nTITLE: Fix the mailbox quota"


def test_blocks_within_one_message_stay_continuous() -> None:
    """Text blocks of ONE message are one continuous text — no break mid-sentence."""
    ft = FinalText()
    ft.add_message("The error rate is ", "12 per minute.")
    assert ft.render() == "The error rate is 12 per minute."


def test_empty_and_whitespace_only_messages_add_no_separator() -> None:
    ft = FinalText()
    ft.add_message("first")
    ft.add_message("")
    ft.add_message("   \n")
    ft.add_message()
    ft.add_message("second")
    assert ft.render() == "first\n\nsecond"
    assert bool(FinalText()) is False
    assert bool(ft) is True


def test_message_text_is_not_stripped() -> None:
    """Only the join is added; a message's own leading/trailing text is preserved."""
    ft = FinalText()
    ft.add_message("a\n")
    ft.add_message("  b")
    assert ft.render() == "a\n" + MESSAGE_SEPARATOR + "  b"


def test_short_text_is_returned_whole() -> None:
    assert tail_on_line_boundary("abc\ndef", 100) == "abc\ndef"
    assert tail_on_line_boundary("", 100) == ""


def test_tail_is_taken_after_joining_and_starts_on_a_line() -> None:
    ft = FinalText()
    ft.add_message("narration line\n" * 2000)  # 30,000 chars
    ft.add_message("NO_ISSUE")
    out = ft.render()
    assert len(out) <= MAX_OUTPUT_CHARS
    assert out.endswith("\n\nNO_ISSUE")
    # The cut landed on a line boundary: the first line is a whole line.
    assert out.startswith("narration line\n")


def test_tail_cut_never_exceeds_the_limit() -> None:
    text = "x" * 50 + "\n" + "y" * 30 + "\n" + "z" * 10
    out = tail_on_line_boundary(text, 45)
    assert out == "y" * 30 + "\n" + "z" * 10
    assert len(out) <= 45


def test_tail_cut_exactly_on_a_boundary_keeps_the_whole_line() -> None:
    text = "aaaa\nbbbb\ncccc"
    # The raw 9-char tail is "bbbb\ncccc", which already starts a line.
    assert tail_on_line_boundary(text, 9) == "bbbb\ncccc"


def test_one_giant_line_falls_back_to_a_hard_cut() -> None:
    """No line boundary in the window → keep the raw tail rather than nothing."""
    text = "q" * 500
    assert tail_on_line_boundary(text, 100) == "q" * 100


def test_a_late_newline_does_not_throw_away_most_of_the_tail() -> None:
    """Snapping forward is bounded: a boundary deep in the window is not used."""
    text = "h" * 200 + "\n" + "END"
    out = tail_on_line_boundary(text, 100)
    assert len(out) == 100
    assert out.endswith("\nEND")
