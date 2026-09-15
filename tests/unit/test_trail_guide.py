"""The shipped example's tools, and the ground truth its output gate checks.

The tools read this repository, so these tests are about the *live* repository
rather than a fixture. That is on purpose: the guide's whole claim is that it
answers from the documents actually present, and a test against a synthetic
corpus would pass on a checkout where those documents had been deleted.

The one to watch is ``known_identifiers``. It is derived from ``Settings``'
own fields, which is what makes ``no_fabricated_ids`` cheap and un-staleable —
and what would silently turn that guard into a rubber stamp if the derivation
ever broke and returned an empty set.
"""

from __future__ import annotations

import pytest

from examples.trail_guide.tools import known_identifiers, search_docs, stack_status

pytestmark = pytest.mark.unit


def test_search_finds_a_documented_phrase() -> None:
    result = search_docs("Traced Runtime")
    assert "README.md:" in result


def test_a_hit_carries_its_file_and_line() -> None:
    """Citations are the guide's contract; a passage with no source is a rumour."""
    first = search_docs("guardrail").splitlines()[0]
    file, _, line = first.partition(":")
    assert file.endswith(".md")
    assert line.isdigit()


def test_a_miss_says_so_rather_than_returning_nothing() -> None:
    """Silence reads as a tool failure; a sentence reads as an answer.

    The model is told to say a thing is undocumented when the search comes back
    empty, and it can only do that if the emptiness arrives as words.
    """
    result = search_docs("zzzznaoexistezzzz")
    assert "Nada encontrado" in result


def test_a_query_of_only_short_words_is_rejected() -> None:
    assert "vazia" in search_docs("a de o")


def test_a_partial_match_comes_back_labelled_as_partial() -> None:
    """A near miss is returned, and says that it is one.

    Strict AND is worse than it looks: one wrong word returns nothing, and
    nothing is indistinguishable from "this repository does not document that"
    — which the agent is instructed to say out loud. Returning the closest
    passages is only safe because the result says which kind of answer it is.
    """
    result = search_docs("Traced zzzznaoexistezzzz")
    assert "mais próximos" in result
    assert "README.md:" in result


def test_a_full_match_is_not_labelled_as_partial() -> None:
    assert "mais próximos" not in search_docs("Traced Runtime")


def test_a_query_matching_nothing_at_all_still_says_so() -> None:
    """Ranking must not turn 'no match' into 'here is something anyway'."""
    assert "Nada encontrado" in search_docs("zzzznaoexistezzzz qqqjamaisqqq")


def test_a_sentence_wrapped_across_lines_is_found() -> None:
    """The reason this searches paragraphs rather than lines.

    Markdown wraps a sentence over several lines, so two words of one sentence
    routinely sit on different lines — and a line-based search reports that the
    sentence is not there.
    """
    result = search_docs("pipeline rail machinery")
    assert "mais próximos" not in result, "these three words share one sentence"


def test_stack_status_lists_the_services_that_actually_run() -> None:
    listing = stack_status()
    for service in ("agent", "postgres", "ui"):
        assert service in listing


def test_stack_status_reports_a_published_port() -> None:
    assert "porta" in stack_status()


# --------------------------------------------------------------------------
# the ground truth
# --------------------------------------------------------------------------


def test_known_identifiers_is_never_empty() -> None:
    """An empty set turns the fabrication guard into a rubber stamp.

    Every ``TRAIL_*`` name would then be unknown and every answer blocked —
    which is loud. The dangerous inverse is a *partial* set, so the two tests
    below check both ends rather than only that it has contents.
    """
    assert known_identifiers()


def test_every_setting_is_known() -> None:
    from trail.config import Settings

    known = known_identifiers()
    for field in Settings.model_fields:
        assert f"TRAIL_{field.upper()}" in known


def test_the_compose_services_are_known() -> None:
    assert {"agent", "postgres"} <= known_identifiers()


def test_an_invented_name_is_not_known() -> None:
    assert "TRAIL_TURBO_MODE" not in known_identifiers()
