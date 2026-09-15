"""Token accounting, which is the quietest place a number can go wrong.

Two rules are load-bearing and both are asserted here rather than trusted:

**An unpriced model costs ``None``.** ``0.00`` is a claim the call was free, and
a scoreboard that reports a confident zero for an unknown rate is worse than one
that reports nothing at all.

**Cached input is not fresh input.** LangChain reports ``input_tokens`` as the
prompt total with cache included; every rate here applies to the uncached
remainder. Getting that backwards bills cached tokens at ten times their price
and produces a cost that is plausible, stable, and wrong.
"""

from __future__ import annotations

import pytest

from tests.fakes import says
from trail.costs import (
    DEFAULT_PRICES,
    ModelPrice,
    compute_cost_usd,
    load_prices,
    usage_from_message,
)

pytestmark = pytest.mark.unit

PRICED = "gpt-5.6-luna"


def test_a_known_model_is_priced() -> None:
    # 1M fresh input at $0.20 plus 1M output at $1.20.
    assert compute_cost_usd(PRICED, 1_000_000, 1_000_000) == pytest.approx(1.40)


def test_an_unknown_model_costs_none_not_zero() -> None:
    assert compute_cost_usd("a-model-nobody-priced", 1_000, 1_000) is None


def test_a_cache_read_is_cheaper_than_fresh_input() -> None:
    fresh = compute_cost_usd(PRICED, 1_000_000, 0)
    cached = compute_cost_usd(PRICED, 0, 0, cache_read_tokens=1_000_000)
    assert fresh is not None and cached is not None
    assert cached < fresh


def test_zero_tokens_costs_zero_for_a_priced_model() -> None:
    """Distinct from an unknown model: here the rate is known and the use is nil."""
    assert compute_cost_usd(PRICED, 0, 0) == 0.0


# --------------------------------------------------------------------------
# reading a provider's usage
# --------------------------------------------------------------------------


def test_cached_tokens_are_subtracted_from_the_reported_input() -> None:
    """The conversion this module exists to do exactly once."""
    usage = usage_from_message(
        says(
            "ok",
            usage={
                "input_tokens": 1_000,
                "output_tokens": 100,
                "input_token_details": {"cache_read": 400},
            },
        ),
        PRICED,
    )
    assert usage.input_tokens == 600  # the uncached remainder
    assert usage.cache_read_tokens == 400
    assert usage.total_input_tokens == 1_000  # and their sum is the prompt again


def test_a_provider_that_reports_no_details_is_not_assumed_to_have_cached() -> None:
    usage = usage_from_message(
        says("ok", usage={"input_tokens": 900, "output_tokens": 10}), PRICED
    )
    assert usage.input_tokens == 900
    assert usage.cache_read_tokens == 0


def test_a_message_with_no_usage_reports_an_unknown_cost() -> None:
    """Silence from the provider is not a measurement of zero."""
    usage = usage_from_message(says("ok"), PRICED)
    assert usage.output_tokens == 0
    assert usage.cost_usd is None


def test_the_detail_shown_on_the_rail_is_the_prompt_total() -> None:
    """What a reader sees is what the provider billed as input, cache included."""
    usage = usage_from_message(
        says(
            "ok",
            usage={
                "input_tokens": 1_200,
                "output_tokens": 40,
                "input_token_details": {"cache_read": 200},
            },
        ),
        PRICED,
    )
    assert usage.as_detail()["input_tokens"] == 1_200


def test_a_nonsensical_cache_count_cannot_produce_negative_input() -> None:
    usage = usage_from_message(
        says(
            "ok",
            usage={
                "input_tokens": 10,
                "output_tokens": 1,
                "input_token_details": {"cache_read": 999},
            },
        ),
        PRICED,
    )
    assert usage.input_tokens == 0


# --------------------------------------------------------------------------
# the price table
# --------------------------------------------------------------------------


def test_an_override_prices_a_model_the_repo_has_never_heard_of() -> None:
    prices = load_prices('{"my-model": {"input": 0.5, "output": 1.5}}')
    assert compute_cost_usd("my-model", 1_000_000, 0, prices=prices) == pytest.approx(
        0.5
    )


def test_an_override_can_replace_a_shipped_rate() -> None:
    prices = load_prices(f'{{"{PRICED}": {{"input": 99.0, "output": 0.0}}}}')
    assert prices[PRICED] == ModelPrice(input=99.0, output=0.0)


def test_malformed_json_is_ignored_rather_than_fatal() -> None:
    """A fat-fingered price must not stop the container from booting.

    Trading a wrong number for no service at all is the worse deal, and the
    wrong number is visible on every rail while a boot loop is visible nowhere.
    """
    assert load_prices("{not json") == DEFAULT_PRICES


def test_an_empty_override_leaves_the_defaults_alone() -> None:
    assert load_prices("") == DEFAULT_PRICES
