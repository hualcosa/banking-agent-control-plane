"""The rule set, read against a clock we control.

Resolução BCB nº 142/2021 caps PIX to a natural person at R$ 1.000 between
20h and 06h — and 20h means 20h *in São Paulo*, not 20h UTC. These tests pin
the boundary minutes on both sides of both edges, because an off-by-one hour
here is a payment refused at dinner time or a R$ 5.000 transfer waved through
at 2am, and neither shows up as a stack trace.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from control_plane.actions import Context, CreatePix
from control_plane.policy import (
    BR_TZ,
    HARD_LIMIT,
    NIGHT_LIMIT,
    Risk,
    assess_risk,
    evaluate,
    is_nighttime,
)

pytestmark = pytest.mark.unit

CTX = Context(customer_id="cust_123", session_id="thread-1", assurance="strong")
KNOWN = {"contact_renata"}

#: Broad daylight in São Paulo, so the nighttime rule is out of the way.
NOON = datetime(2026, 3, 10, 12, 0, tzinfo=BR_TZ)


def pix(amount: str) -> CreatePix:
    return CreatePix(
        source_account="checking_001",
        recipient_id="contact_renata",
        amount=Decimal(amount),
    )


def sp(hour: int, minute: int = 0) -> datetime:
    """A fixed instant on a São Paulo wall clock."""
    return datetime(2026, 3, 10, hour, minute, tzinfo=BR_TZ)


def verdict(amount: str, moment: datetime) -> tuple[str, str]:
    action = pix(amount)
    risk = assess_risk(action, CTX, known_recipients=KNOWN)
    out = evaluate(action, CTX, risk, now=moment)
    return out.decision, out.rule


# --------------------------------------------------------------------------
# the window itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (sp(19, 59), False),
        (sp(20, 0), True),
        (sp(23, 59), True),
        (sp(0, 0), True),
        (sp(5, 59), True),
        (sp(6, 0), False),
        (sp(12, 0), False),
    ],
)
def test_the_nighttime_window_starts_at_20h_and_ends_at_06h(
    moment: datetime, expected: bool
) -> None:
    assert is_nighttime(moment) is expected


def test_the_window_is_read_on_a_brazilian_clock_not_a_utc_one() -> None:
    """02:00 UTC is 23:00 in São Paulo — night, however the instant is spelt."""
    utc = datetime(2026, 3, 11, 2, 0, tzinfo=timezone.utc)
    assert utc.hour == 2, "the UTC hour is outside the window"
    assert utc.astimezone(BR_TZ).hour == 23
    assert is_nighttime(utc) is True
    assert verdict("1500", utc) == ("DENY", "pix_nighttime_limit")


def test_a_daytime_utc_hour_that_is_still_night_in_sao_paulo() -> None:
    """22:30 UTC is 19:30 in São Paulo — not yet night, despite the UTC hour."""
    utc = datetime(2026, 3, 10, 22, 30, tzinfo=timezone.utc)
    assert utc.astimezone(BR_TZ).hour == 19
    assert is_nighttime(utc) is False


# --------------------------------------------------------------------------
# the rule
# --------------------------------------------------------------------------


@pytest.mark.parametrize("moment", [sp(20, 0), sp(23, 59), sp(0, 0), sp(5, 59)])
def test_over_the_cap_inside_the_window_is_denied_by_name(moment: datetime) -> None:
    assert verdict("1500", moment) == ("DENY", "pix_nighttime_limit")


@pytest.mark.parametrize("moment", [sp(19, 59), sp(6, 0), sp(12, 0)])
def test_the_same_amount_outside_the_window_is_not_denied(moment: datetime) -> None:
    decision, rule = verdict("1500", moment)
    assert decision != "DENY"
    assert rule != "pix_nighttime_limit"


@pytest.mark.parametrize("moment", [sp(19, 59), sp(20, 0), sp(5, 59), sp(6, 0)])
def test_at_or_under_the_cap_the_hour_does_not_matter(moment: datetime) -> None:
    assert verdict(str(NIGHT_LIMIT), moment) == (
        "REQUIRE_CONFIRMATION",
        "capability_requires_confirmation",
    )


def test_one_centavo_over_the_cap_is_the_side_that_is_denied() -> None:
    assert verdict("1000.00", sp(22)) == (
        "REQUIRE_CONFIRMATION",
        "capability_requires_confirmation",
    )
    assert verdict("1000.01", sp(22)) == ("DENY", "pix_nighttime_limit")


def test_the_reason_cites_the_norm_and_reads_in_portuguese() -> None:
    action = pix("1500")
    risk = assess_risk(action, CTX, known_recipients=KNOWN)
    out = evaluate(action, CTX, risk, now=sp(22))
    assert "142/2021" in out.reason
    assert "20h" in out.reason and "06h" in out.reason
    assert f"R$ {NIGHT_LIMIT:.2f}" in out.reason


# --------------------------------------------------------------------------
# ordering: a denial must not hide behind a step-up demand
# --------------------------------------------------------------------------


def test_the_hard_limit_still_wins_over_the_nighttime_rule() -> None:
    """Both apply at 2am; the ceiling is the more specific refusal."""
    assert verdict(str(HARD_LIMIT + 1), sp(2)) == ("DENY", "pix_hard_limit")


def test_the_nighttime_denial_comes_before_the_step_up_demand() -> None:
    """A medium-assurance customer is told 'no', not 'authenticate, then no'."""
    weak = Context(customer_id="cust_123", session_id="thread-1", assurance="medium")
    action = pix("1500")
    risk = assess_risk(action, weak, known_recipients=KNOWN)
    out = evaluate(action, weak, risk, now=sp(22))
    assert (out.decision, out.rule) == ("DENY", "pix_nighttime_limit")

    day = evaluate(action, weak, risk, now=NOON)
    assert (day.decision, day.rule) == ("REQUIRE_STEP_UP_AUTH", "pix_step_up")


# --------------------------------------------------------------------------
# the injected clock
# --------------------------------------------------------------------------


def test_now_defaults_to_the_system_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers that pass no clock get one — that default is why the 26
    existing tests never had to learn about this rule."""
    import control_plane.policy as policy

    monkeypatch.setattr(policy, "system_now", lambda: sp(22))
    action = pix("1500")
    risk = Risk(score=0.0, level="low", signals=())
    assert evaluate(action, CTX, risk).rule == "pix_nighttime_limit"

    monkeypatch.setattr(policy, "system_now", lambda: NOON)
    assert evaluate(action, CTX, risk).rule != "pix_nighttime_limit"


def test_now_is_keyword_only() -> None:
    action = pix("1500")
    risk = Risk(score=0.0, level="low", signals=())
    with pytest.raises(TypeError):
        evaluate(action, CTX, risk, sp(22))  # type: ignore[misc]
