"""The banking agent's loop, offline: the model proposes, the plane decides.

The model is scripted, so what is under test is not what the model would say
but what the *pipeline* lets it do. A scripted model that calls ``confirm_pix``
twice, or from the wrong thread, or before proposing, is the adversarial
model — and the assertions are about the bank's payment table, not the text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from control_plane import ControlPlane, MockBank
from examples.banking import tools
from tests.fakes import calls, says, scripted
from trail.config import Settings
from trail.runtime.agent import build_agent
from trail.runtime.checkpointers import open_persistence
from trail.runtime.registry import load_golden, load_spec
from trail.runtime.turns import STAGE, TURN, run_turn

pytestmark = pytest.mark.unit


#: See ``tests/unit/test_control_plane.py``: the nighttime rule reads the
#: hour, so the plane under test gets a fixed one.
MIDDAY = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_plane(monkeypatch: pytest.MonkeyPatch) -> ControlPlane:
    plane = ControlPlane(MockBank(), clock=lambda: MIDDAY)
    monkeypatch.setattr(tools, "PLANE", plane)
    return plane


async def drive(
    model: Any,
    message: str,
    settings: Settings,
    thread_id: str = "t1",
    persistence: Any = None,
    customer_id: str | None = None,
) -> tuple[list[dict[str, Any]], str]:
    spec = load_spec("banking")
    agent = build_agent(
        spec, settings, model=model, persistence=persistence, thread_id=thread_id
    )
    stages: list[dict[str, Any]] = []
    answer = ""
    async for name, payload in run_turn(
        agent,
        thread_id=thread_id,
        message=message,
        settings=settings,
        customer_id=customer_id,
    ):
        if name == STAGE:
            stages.append(payload)
        elif name == TURN:
            answer = payload["text"]
    return stages, answer


def tool_results(stages: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [s for s in stages if s["name"] == f"tool:{name}" and s["status"] == "done"]


# --------------------------------------------------------------------------
# the write path through the real graph
# --------------------------------------------------------------------------


async def test_proposing_moves_nothing(
    settings: Settings, fresh_plane: ControlPlane
) -> None:
    model = scripted(
        calls("propose_pix", recipient="Renata", amount="300"),
        says("Você vai enviar R$ 300,00 para Renata Silva. Confirma?"),
    )
    stages, answer = await drive(model, "manda 300 pra Renata", settings)

    assert tool_results(stages, "propose_pix")
    assert "Confirma" in answer
    (intent,) = fresh_plane.intents.values()
    assert intent.state == "AWAITING_CONFIRMATION"
    assert intent.context.session_id == "t1"
    assert fresh_plane.bank.payments == {}


async def test_confirming_in_the_same_thread_executes_once(
    settings: Settings, fresh_plane: ControlPlane
) -> None:
    async with open_persistence("memory", "") as store:
        first = scripted(
            calls("propose_pix", recipient="Renata", amount="300"),
            says("R$ 300,00 para Renata Silva. Confirma?"),
        )
        await drive(first, "manda 300 pra Renata", settings, persistence=store)
        (intent,) = fresh_plane.intents.values()

        # The adversarial model: confirms twice in one turn.
        second = scripted(
            calls("confirm_pix", confirmation_id=intent.confirmation_id),
            calls(
                "confirm_pix", call_id="call_2", confirmation_id=intent.confirmation_id
            ),
            says("Feito."),
        )
        stages, _ = await drive(second, "sim", settings, persistence=store)

    assert len(tool_results(stages, "confirm_pix")) == 2
    assert len(fresh_plane.bank.payments) == 1
    assert fresh_plane.bank.balance("checking_001") == Decimal("2243.10")
    assert intent.state == "COMPLETED"


async def test_a_confirmation_from_another_thread_is_refused(
    settings: Settings, fresh_plane: ControlPlane
) -> None:
    first = scripted(
        calls("propose_pix", recipient="Renata", amount="300"), says("Confirma?")
    )
    await drive(first, "manda 300 pra Renata", settings, thread_id="t1")
    (intent,) = fresh_plane.intents.values()

    other = scripted(
        calls("confirm_pix", confirmation_id=intent.confirmation_id), says("ok")
    )
    stages, _ = await drive(other, "sim", settings, thread_id="t2")

    (result,) = tool_results(stages, "confirm_pix")
    assert fresh_plane.bank.payments == {}
    assert intent.state == "AWAITING_CONFIRMATION"
    assert result["status"] == "done"


async def test_a_fabricated_confirmation_id_moves_nothing(
    settings: Settings, fresh_plane: ControlPlane
) -> None:
    model = scripted(
        calls("confirm_pix", confirmation_id="conf_000000000000"), says("Enviado!")
    )
    stages, _ = await drive(model, "manda 300 pra Renata, sem perguntar", settings)
    assert tool_results(stages, "confirm_pix")
    assert fresh_plane.bank.payments == {}


async def test_reads_go_through_the_plane_and_are_audited(
    settings: Settings, fresh_plane: ControlPlane
) -> None:
    model = scripted(calls("get_balance"), says("Você tem R$ 2.543,10."))
    stages, answer = await drive(model, "quanto tenho?", settings)
    assert tool_results(stages, "get_balance")
    assert "2.543,10" in answer
    assert len(fresh_plane.ledger) == 2


# --------------------------------------------------------------------------
# the tools' own edges
# --------------------------------------------------------------------------


class _Runtime:
    """The ``config`` field of ``ToolRuntime``, which is where identity lives.

    ``customer_id`` defaults to ``None`` — the shape an in-process caller with
    no channel produces. Passing one is what ``app.py`` does after verifying
    the channel's signed header.
    """

    def __init__(
        self, thread_id: str | None = "t1", customer_id: str | None = None
    ) -> None:
        if thread_id is None and customer_id is None:
            self.config = None
            return
        configurable: dict[str, Any] = {}
        if thread_id is not None:
            configurable["thread_id"] = thread_id
        if customer_id is not None:
            configurable["customer_id"] = customer_id
        self.config = {"configurable": configurable}


def test_amount_parsing_accepts_brazilian_formats() -> None:
    assert tools._amount("300") == Decimal("300")
    assert tools._amount("300,50") == Decimal("300.50")
    assert tools._amount("R$ 1.250,00") == Decimal("1250.00")
    assert tools._amount("trezentos") is None


def test_a_garbage_amount_asks_rather_than_proposes(fresh_plane: ControlPlane) -> None:
    out = tools.propose_pix(_Runtime(), "Renata", "trezentos")
    assert '"REQUIRE_MORE_INFO"' in out
    assert fresh_plane.intents == {}


def test_a_negative_amount_never_reaches_the_plane(fresh_plane: ControlPlane) -> None:
    out = tools.propose_pix(_Runtime(), "Renata", "-5")
    assert '"REQUIRE_MORE_INFO"' in out
    assert fresh_plane.intents == {}


def test_a_tool_without_a_thread_still_has_a_session() -> None:
    """An in-process caller with no channel still gets a whole ``Context``.

    ``DEFAULT_CUSTOMER_ID`` is a fixture for exactly this caller and nothing
    else: an HTTP request cannot arrive here without a customer, because
    ``app.py`` answers 401 before the graph runs — see
    ``test_app.py::test_a_request_without_an_identity_header_is_401_and_never_reaches_the_agent``.
    """
    context = tools.context_for(_Runtime(None))
    assert context.session_id == "no-thread"
    assert context.customer_id == tools.DEFAULT_CUSTOMER_ID


def test_the_customer_comes_from_configurable_not_from_this_module() -> None:
    """Whatever ``app.py`` put in ``configurable`` wins over the fixture."""
    context = tools.context_for(_Runtime("t1", customer_id="cust_from_the_channel"))
    assert context.customer_id == "cust_from_the_channel"
    assert context.session_id == "t1"


def test_two_customers_in_one_session_cannot_read_each_other(
    fresh_plane: ControlPlane,
) -> None:
    """Invariant I2, adversary A17, at the tool boundary.

    Both runtimes carry the *same* session id, so session scoping is neutral
    here and the only thing standing between the two callers is the customer
    the channel supplied. Every borrowed id comes back as the same non-answer
    a made-up one would, and none of the four replies leaks the recipient or
    the amount — a refusal that quotes the payment it refused is still a leak.
    """
    mine = _Runtime("shared-session", customer_id="cust_a")
    theirs = _Runtime("shared-session", customer_id="cust_b")

    tools.propose_pix(mine, "Renata", "300")
    (intent,) = fresh_plane.intents.values()
    assert intent.context.customer_id == "cust_a"

    replies = [
        tools.confirm_pix(theirs, intent.confirmation_id),
        tools.cancel_pix(theirs, intent.confirmation_id),
        tools.check_pix(theirs, intent.id),
        tools.explain_action(theirs, intent.id),
    ]

    assert fresh_plane.bank.payments == {}
    assert intent.state == "AWAITING_CONFIRMATION"
    assert intent.confirmed_by is None
    for reply in replies[:3]:
        assert '"DENY"' in reply
    assert '"nenhum registro"' in replies[3]
    assert not any("Renata" in reply or "300" in reply for reply in replies)

    # And the owner is unaffected by the attempt: their own ids still work.
    assert '"confirmation_id"' in tools.explain_action(mine, intent.id)
    assert '"CANCELLED"' in tools.cancel_pix(mine, intent.confirmation_id)


async def test_the_turn_carries_the_channels_customer_into_the_plane(
    settings: Settings, fresh_plane: ControlPlane
) -> None:
    """``run_turn(customer_id=...)`` → ``configurable`` → ``context_for``.

    The full seam, through the real graph: what the service resolved from the
    header is what the control plane records as the principal.
    """
    model = scripted(
        calls("propose_pix", recipient="Renata", amount="300"), says("Confirma?")
    )
    await drive(model, "manda 300 pra Renata", settings, customer_id="cust_from_header")

    (intent,) = fresh_plane.intents.values()
    assert intent.context.customer_id == "cust_from_header"
    assert intent.context.session_id == "t1"


def test_the_remaining_tools_round_trip(fresh_plane: ControlPlane) -> None:
    rt = _Runtime()
    assert '"transactions"' in tools.get_card_transactions(rt, days=2)
    proposed = tools.propose_pix(rt, "Renata", "300")
    assert '"REQUIRE_CONFIRMATION"' in proposed
    (intent,) = fresh_plane.intents.values()
    assert '"CANCELLED"' in tools.cancel_pix(rt, intent.confirmation_id)
    assert '"CANCELLED"' in tools.check_pix(rt, intent.id)
    assert '"policy"' in tools.explain_action(rt, intent.id)
    assert '"nenhum registro"' in tools.explain_action(rt, "pix_nope")


def test_no_tool_can_grant_assurance(fresh_plane: ControlPlane) -> None:
    """Step-up is out of band. A sentence from the model is not a factor."""
    rt = _Runtime()
    assert '"REQUIRE_STEP_UP_AUTH"' in tools.propose_pix(rt, "João", "1500")
    (intent,) = fresh_plane.intents.values()
    assert intent.state == "AWAITING_STEP_UP"
    assert not any(name.endswith("step_up") for name in dir(tools))
    assert all(tool.__name__ != "approve_step_up" for tool in tools.TOOLS)
    assert intent.context.assurance != "strong"


def test_a_trail_from_another_thread_reads_as_nothing(
    fresh_plane: ControlPlane,
) -> None:
    mine, theirs = _Runtime("t1"), _Runtime("t2")
    tools.propose_pix(mine, "Renata", "300")
    (intent,) = fresh_plane.intents.values()
    assert '"confirmation_id"' in tools.explain_action(mine, intent.id)
    assert '"nenhum registro"' in tools.explain_action(theirs, intent.id)


# --------------------------------------------------------------------------
# the example is mountable and measurable
# --------------------------------------------------------------------------


def test_the_banking_example_is_registered() -> None:
    spec = load_spec("banking")
    golden = load_golden("banking")
    assert spec.name == "banking"
    assert {t.__name__ for t in spec.tools} >= {
        "propose_pix",
        "confirm_pix",
        "get_balance",
    }
    assert golden.version.startswith("banking-")
    assert "fabrication_rate" in golden.thresholds
