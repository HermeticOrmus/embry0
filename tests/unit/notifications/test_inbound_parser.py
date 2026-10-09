"""Pure-function parser for /answer <ref>: <text> and /skip <ref> syntax."""

from embry0.notifications.inbound_parser import parse_answer_directives


def test_parse_single_answer_one_line():
    body = "/answer 1: PostgreSQL"
    assert parse_answer_directives(body) == [("1", "answer", "PostgreSQL")]


def test_parse_multiple_answers():
    body = "/answer 1: PostgreSQL\n/answer 2: yes"
    assert parse_answer_directives(body) == [
        ("1", "answer", "PostgreSQL"),
        ("2", "answer", "yes"),
    ]


def test_parse_skip():
    body = "/skip 3"
    assert parse_answer_directives(body) == [("3", "skip", "")]


def test_parse_mixed():
    body = "/answer 1: PostgreSQL\n/skip 2\n/answer 3: maybe later"
    assert parse_answer_directives(body) == [
        ("1", "answer", "PostgreSQL"),
        ("2", "skip", ""),
        ("3", "answer", "maybe later"),
    ]


def test_parse_ignores_non_directive_lines():
    body = "hey team\n/answer 1: yes\nthanks"
    assert parse_answer_directives(body) == [("1", "answer", "yes")]


def test_parse_multiline_answer_value_takes_to_next_directive():
    body = "/answer 1: yes\nand also do X\nand Y\n/answer 2: no"
    assert parse_answer_directives(body) == [
        ("1", "answer", "yes\nand also do X\nand Y"),
        ("2", "answer", "no"),
    ]


def test_parse_handles_carriage_returns():
    body = "/answer 1: yes\r\n/answer 2: no"
    assert parse_answer_directives(body) == [
        ("1", "answer", "yes"),
        ("2", "answer", "no"),
    ]


def test_parse_zero_directives_returns_empty():
    assert parse_answer_directives("hello world") == []


def test_parse_rejects_negative_or_zero_sequence():
    body = "/answer 0: x\n/answer -1: y\n/answer 1: ok"
    assert parse_answer_directives(body) == [("1", "answer", "ok")]


def test_parse_strips_whitespace_around_value():
    assert parse_answer_directives("/answer 1:    yes please   ") == [("1", "answer", "yes please")]


def test_parse_accepts_an_input_id_as_the_reference():
    """#253: the canonical form addresses the stable issue_inputs id."""
    body = "/answer inp-a1b2c3d4e5f6: PostgreSQL"
    assert parse_answer_directives(body) == [("inp-a1b2c3d4e5f6", "answer", "PostgreSQL")]


def test_parse_accepts_an_input_id_for_skip():
    assert parse_answer_directives("/skip inp-a1b2c3d4e5f6") == [("inp-a1b2c3d4e5f6", "skip", "")]


def test_parse_mixes_ids_and_positions():
    body = "/answer inp-abc123def456: yes\n/answer 2: no"
    assert parse_answer_directives(body) == [
        ("inp-abc123def456", "answer", "yes"),
        ("2", "answer", "no"),
    ]


def test_parse_rejects_a_malformed_reference():
    """Anything outside the id alphabet is not a directive at all, so a prose
    line that happens to start with /answer cannot address a question."""
    assert parse_answer_directives("/answer ../../etc/passwd: x") == []
    assert parse_answer_directives("/answer inp abc: x") == []
    assert parse_answer_directives("/answer : x") == []
