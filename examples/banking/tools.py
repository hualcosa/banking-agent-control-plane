"""What the model may call. Every tool is a thin call into the control plane.

None of these functions touches the bank. ``propose_pix`` produces a proposal
and gets back what the control plane needs; ``confirm_pix`` hands over a
token; the bank is reached only inside ``ControlPlane._execute``, which no
tool can call. That is the boundary, enforced by import structure rather than
by prompt.

Nor is there a tool that *approves* anything. Step-up assurance is granted out
of band — by the channel that can actually authenticate the customer — and the
model is not that channel: a tool it can call to say "the customer approved" is
a tool that lets a sentence stand in for an authentication factor. So
``ControlPlane.step_up`` exists and nothing in this list reaches it.

Results are JSON so the model relays fields rather than paraphrasing them. A
``status`` the model did not receive is a status it cannot truthfully claim.

Identity comes from the channel, never from here. ``app.py`` verifies the
signed identity header and puts the resolved customer in the graph's
``configurable`` dict, next to the thread; ``context_for`` reads both from
there. The customer is therefore something the agent is *told*, not something
it decides — a constant in this module would be an identity a prompt could
argue with, and the whole control plane scopes on it.

Scope, too, comes from the channel. ``PLANE`` is the *template* — the ledger,
the clock, the secret, and a bank in its opening state — and each conversation
gets a plane of its own over that same ledger, with a bank of its own. See
``plane_for``.

# ponytail: planes derived per conversation and kept in a process dict. V1
# stores the account in Postgres and the scope is a row, not a key here.
"""

from __future__ import annotations

import json
import threading
from decimal import Decimal, InvalidOperation
from typing import Any
from weakref import WeakKeyDictionary

from langchain.tools import ToolRuntime

from control_plane import (
    Context,
    ControlPlane,
    GetBalance,
    GetCardTransactions,
    Outcome,
    ProposedPix,
)

#: The template every conversation's plane is derived from. Still a module
#: attribute, and still the thing a test replaces: swapping it swaps the bank,
#: the clock and the ledger every scope is built over.
PLANE = ControlPlane()

#: ``template plane → {(customer, session): plane}``. Weak on the template so
#: that replacing ``PLANE`` — a test's monkeypatch, a re-import — drops every
#: scope derived from the old one instead of leaking it into the next caller.
_SCOPES: WeakKeyDictionary[ControlPlane, dict[tuple[str, str], ControlPlane]] = (
    WeakKeyDictionary()
)
_SCOPES_LOCK = threading.Lock()


def plane_for(ctx: Context) -> ControlPlane:
    """The plane for one conversation: shared ledger, bank of its own.

    Two mutable things in :class:`~control_plane.bank.MockBank` are also policy
    inputs — the balance, and ``paid_before``, which is where the
    ``new_recipient`` risk signal comes from. One bank per process therefore
    means the *second* payment to João scores differently from the first, and
    that is not confined to one conversation: it survives across whole eval
    runs in a long-lived container, so a second ``make eval`` scores something
    the first one cannot be compared to.

    The key is ``(customer, session)`` — a conversation. Per *customer* alone
    would be the more natural banking scope, but every eval case authenticates
    as the same customer (``trail.cli.identity_headers`` signs one id for the
    whole run), so a per-customer bank would leave all cases, and all reruns,
    sharing exactly the state that makes the run irreproducible. The session is
    the unit the harness creates fresh per case and per run, which makes it the
    only key that actually scopes them apart. The customer stays in the key so
    that two principals in one thread id are still two accounts.

    Nothing about the single-customer chat changes: one conversation is one
    thread, so it is one account, continuous from the first turn to the last.

    The ledger, the clock and the secret are the template's, shared: intents
    are already scoped by their :class:`Context`, so the audit trail stays
    whole (``sweep`` on boot still sees every stranded intent) while the bank —
    the only state that leaks *outcomes* between callers — does not.

    Nothing evicts: a bank is a few hundred bytes and an evicted one would
    answer ``reconcile`` with "the bank never paid" about a payment it *did*
    make, which is a lie about money in exchange for memory nobody is short of.
    """
    template = PLANE
    key = (ctx.customer_id, ctx.session_id)
    with _SCOPES_LOCK:
        scopes = _SCOPES.setdefault(template, {})
        plane = scopes.get(key)
        if plane is None:
            plane = ControlPlane(
                template.bank.fresh(),
                clock=template.clock,
                store=template.store,
                secret=template.secret,
            )
            scopes[key] = plane
        return plane


#: Only for a caller with no channel at all — an in-process drive of a tool
#: (a unit test, a REPL). Every HTTP request carries a verified customer in
#: ``configurable`` because ``app.py`` refuses the request otherwise, so this
#: name cannot be reached from the network: it is a fixture, not a fallback
#: identity. Anything that makes it reachable from a request is a bug.
DEFAULT_CUSTOMER_ID = "cust_123"


def context_for(runtime: ToolRuntime) -> Context:
    """Who is acting and in which conversation, both from ``configurable``.

    The customer was resolved from the channel's signed identity by ``app.py``
    and travels down here untouched; the session is the thread, which is what
    makes a confirmation non-transferable between conversations. Neither is
    readable or writable by the model — they are not tool arguments.
    """
    configurable = (runtime.config or {}).get("configurable", {})
    return Context(
        customer_id=str(configurable.get("customer_id") or DEFAULT_CUSTOMER_ID),
        session_id=str(configurable.get("thread_id", "no-thread")),
        channel="whatsapp",
    )


def _render(outcome: Outcome) -> str:
    return json.dumps(outcome.model_dump(mode="json"), ensure_ascii=False)


def _amount(text: str) -> Decimal | None:
    """``"300"``, ``"300,50"``, ``"R$ 1.200,00"`` → ``Decimal``; garbage → ``None``."""
    cleaned = text.replace("R$", "").replace(" ", "")
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def get_balance(runtime: ToolRuntime) -> str:
    """Consulta o saldo da conta corrente do cliente.

    Returns:
        JSON com ``data.display`` (saldo formatado em reais).
    """
    ctx = context_for(runtime)
    return _render(plane_for(ctx).query(ctx, GetBalance()))


def get_card_transactions(runtime: ToolRuntime, days: int = 7) -> str:
    """Lista as compras do cartão de crédito dos últimos dias.

    Args:
        days: Quantos dias para trás olhar (1 a 90). Use 2 para "ontem".

    Returns:
        JSON com ``data.transactions``: data, estabelecimento, valor, categoria.
    """
    ctx = context_for(runtime)
    return _render(plane_for(ctx).query(ctx, GetCardTransactions(days=days)))


def propose_pix(runtime: ToolRuntime, recipient: str, amount: str) -> str:
    """Propõe um PIX. NÃO envia dinheiro: devolve o que falta para enviar.

    Args:
        recipient: O nome do destinatário como o cliente disse (ex.: "Renata").
        amount: O valor como o cliente disse (ex.: "300", "1.250,00").

    Returns:
        JSON com ``status``:
        - ``REQUIRE_CONFIRMATION``: mostre ``data.recipient`` e ``data.display``
          ao cliente e peça confirmação explícita. Guarde ``confirmation_id``.
        - ``REQUIRE_MORE_INFO``: pergunte qual dos ``data.candidates``.
        - ``REQUIRE_STEP_UP_AUTH``: peça ao cliente para aprovar no aplicativo.
          Guarde ``intent_id``.
        - ``DENY``: explique ``message``. Não tente de novo com outro valor.
    """
    value = _amount(amount)
    if value is None:
        return _render(
            Outcome(status="REQUIRE_MORE_INFO", message=f"valor inválido: {amount!r}")
        )
    try:
        proposed = ProposedPix(recipient=recipient, amount=value)
    except ValueError as exc:
        return _render(
            Outcome(status="REQUIRE_MORE_INFO", message=str(exc).splitlines()[0])
        )
    ctx = context_for(runtime)
    return _render(
        plane_for(ctx).propose(ctx, proposed, request_text=f"{recipient} {amount}")
    )


def confirm_pix(runtime: ToolRuntime, confirmation_id: str) -> str:
    """Executa o PIX que o cliente acabou de confirmar explicitamente.

    Chame SOMENTE depois de o cliente responder que sim ao valor e destinatário
    exatos que você apresentou. Idempotente: chamar duas vezes não paga duas.

    Args:
        confirmation_id: O ``confirmation_id`` devolvido por ``propose_pix``.

    Returns:
        JSON com ``status``: ``COMPLETED``, ``FAILED``, ``UNKNOWN`` (sem
        resposta do banco — use ``check_pix``) ou ``DENY``.
    """
    ctx = context_for(runtime)
    return _render(plane_for(ctx).confirm(ctx, confirmation_id))


def cancel_pix(runtime: ToolRuntime, confirmation_id: str) -> str:
    """Cancela um PIX proposto que o cliente decidiu não enviar.

    Args:
        confirmation_id: O ``confirmation_id`` devolvido por ``propose_pix``.
    """
    ctx = context_for(runtime)
    return _render(plane_for(ctx).cancel(ctx, confirmation_id))


def check_pix(runtime: ToolRuntime, intent_id: str) -> str:
    """Consulta o estado de um PIX; se estava ``UNKNOWN``, reconcilia com o banco.

    Args:
        intent_id: O ``intent_id`` do PIX.
    """
    ctx = context_for(runtime)
    return _render(plane_for(ctx).reconcile(ctx, intent_id))


def explain_action(runtime: ToolRuntime, intent_id: str) -> str:
    """A trilha de auditoria de uma ação: cada evento registrado, em ordem.

    Só devolve o que pertence a esta conversa.

    Args:
        intent_id: O ``intent_id`` de um PIX ou o id de uma consulta.
    """
    ctx = context_for(runtime)
    events: list[dict[str, Any]] = [
        e.model_dump(mode="json") for e in plane_for(ctx).explain(ctx, intent_id)
    ]
    if not events:
        return json.dumps(
            {"intent_id": intent_id, "events": [], "message": "nenhum registro"}
        )
    return json.dumps({"intent_id": intent_id, "events": events}, ensure_ascii=False)


TOOLS = [
    get_balance,
    get_card_transactions,
    propose_pix,
    confirm_pix,
    cancel_pix,
    check_pix,
    explain_action,
]
