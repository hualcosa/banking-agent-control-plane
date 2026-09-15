"""What one model call cost, in US dollars.

One implementation, so that every place a cost appears — a stage frame, a span
attribute, a trace row — is the same number computed the same way and they
cannot drift apart.

Two decisions worth naming.

**Prices are data, not constants.** The version of this that TRAIL was
extracted from hardcoded one model's rates at module scope. That is correct
right up until the day someone changes ``TRAIL_MODEL``, at which point every
cost in the system is quietly wrong and nothing says so. Here the table is
keyed by model and overridable from the environment.

**An unpriced model costs ``None``, never zero.** ``0.00`` is a claim that the
call was free. ``None`` is a claim that nobody knows, and the rail renders it as
a dash. The whole point of this scaffold is that the numbers on the screen are
answerable, and a confident zero is the most expensive kind of wrong.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelPrice:
    """Dollars per million tokens, by bucket.

    The buckets are disjoint by construction: ``input`` is the *uncached
    remainder*, not the prompt total. Passing a provider's raw input count
    alongside its cache-read count bills the cached tokens twice, once at each
    rate — the single most error-prone conversion in this file.

    ``cache_write`` exists and is usually zero. OpenAI caches automatically and
    charges no write premium; Anthropic charges 1.25x. A zero that is explained
    is cheaper than a schema change that is not needed yet.
    """

    input: float
    output: float
    cache_read: float = 0.0
    cache_write: float = 0.0


#: Known rates, in USD per million tokens. A model that is not here is not an
#: error — it is an unknown cost, and :func:`compute_cost_usd` says so by
#: returning ``None``.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    "gpt-5.6-luna": ModelPrice(input=0.20, output=1.20, cache_read=0.02),
    # Bedrock on-demand, sa-east-1 (AWS Price List, 2026-09-15).
    "openai.gpt-oss-120b-1:0": ModelPrice(input=0.18, output=0.73),
}


def load_prices(override_json: str = "") -> dict[str, ModelPrice]:
    """The price table, with ``TRAIL_MODEL_PRICES`` merged over the defaults.

    The override is JSON so that a deployment can price a model this repository
    has never heard of without a release::

        TRAIL_MODEL_PRICES='{"my-model": {"input": 0.5, "output": 1.5}}'

    Malformed JSON is logged and ignored rather than raised. The alternative is
    a container that will not boot because someone fat-fingered a price, which
    trades a wrong number for no service at all.
    """
    prices = dict(DEFAULT_PRICES)
    if not override_json.strip():
        return prices
    try:
        raw: dict[str, Any] = json.loads(override_json)
        for name, fields in raw.items():
            prices[name] = ModelPrice(**fields)
    except (ValueError, TypeError) as exc:
        logger.warning("ignoring malformed TRAIL_MODEL_PRICES: %s", exc)
    return prices


def compute_cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    prices: dict[str, ModelPrice] | None = None,
) -> float | None:
    """Cost of one call, or ``None`` if ``model`` has no published rate here.

    ``input_tokens`` must be the uncached remainder. See :class:`ModelPrice`.

    Reasoning tokens need no term of their own: providers report them inside
    ``output_tokens`` and bill them at the output rate.
    """
    price = (prices if prices is not None else DEFAULT_PRICES).get(model)
    if price is None:
        return None
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
        + cache_write_tokens * price.cache_write
    ) / 1_000_000


@dataclass(frozen=True)
class Usage:
    """Tokens and cost for one model call, in the shape the wire wants.

    ``total_input_tokens`` is every input token the model processed: the
    uncached remainder, what was served from cache, and what was written into
    it. The three are disjoint and their sum is the prompt. Keeping that
    definition stable across releases is not pedantry — it is what stops a
    cost-per-turn chart from changing meaning between versions.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None

    @property
    def total_input_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def as_detail(self) -> dict[str, Any]:
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
        }


def usage_from_message(
    message: Any,
    model: str,
    prices: dict[str, ModelPrice] | None = None,
) -> Usage:
    """Read LangChain's ``usage_metadata`` off a message and price it.

    LangChain normalises every provider into one shape, which is the reason
    this scaffold can swap models without touching the accounting::

        {"input_tokens": N, "output_tokens": N,
         "input_token_details": {"cache_read": N, "cache_creation": N}}

    The nested ``input_token_details`` is the field most likely to be wrong in
    silence, because zero is a plausible value for it and nothing looks broken
    when a cache hit is billed as fresh input. Providers that report no details
    genuinely have none; providers that do report them are read here and
    nowhere else.

    A provider that reports no usage at all yields zeros and a ``None`` cost,
    rather than a zero cost — the same rule as an unpriced model.
    """
    meta = getattr(message, "usage_metadata", None) or {}
    if not meta:
        return Usage()
    details = meta.get("input_token_details") or {}
    cache_read = int(details.get("cache_read", 0) or 0)
    cache_write = int(details.get("cache_creation", 0) or 0)
    # LangChain reports `input_tokens` as the prompt total, cache included.
    # Everything downstream wants the uncached remainder, so subtract here —
    # once — rather than at each call site.
    total_input = int(meta.get("input_tokens", 0) or 0)
    fresh_input = max(total_input - cache_read - cache_write, 0)
    output = int(meta.get("output_tokens", 0) or 0)
    return Usage(
        input_tokens=fresh_input,
        output_tokens=output,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=compute_cost_usd(
            model, fresh_input, output, cache_read, cache_write, prices
        ),
    )
