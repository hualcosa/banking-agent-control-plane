"""Voice as a channel: a transcript, a confidence, and nothing else.

The milestone this module belongs to asks one question — is the control plane
really independent of channel? — and answers it with a number:

    git diff --stat before-voice -- src/control_plane/

So the rule here is that everything voice-specific lives in this file. What
the control plane learns is exactly one new fact, ``Context.stt_confidence``,
because "I am not sure I heard that correctly" is not a fact any text channel
can express and no adapter can fake on the plane's behalf.

**Simulated, and saying so.** ``TRANSCRIPTS`` is a hand-written table of the
confusions pt-BR speech recognition actually makes with money and names, not
a recording of any. A simulation cannot tell you a word error rate — that is
the STT benchmark (T27), against a real recogniser. What it *can* do is
exercise the path a real transcript would take, which is what the diff above
is measuring.

The two confusion classes are not equally dangerous and the table keeps them
apart:

* **magnitude collapse** — "trezentos" heard as "treze" is a twenty-fold
  error in the amount, and the customer who said the first would not notice
  the second in a spoken confirmation read back quickly;
* **recipient confusion** — "Renata" and "Renato" are one phoneme apart and
  are different people with different accounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from control_plane import Context

#: Below this, this module refuses to propose at all — see :func:`heard`.
#: Distinct from ``policy.HEARD_CLEARLY``, which decides how *risky* a
#: transcript is once it becomes an action. This one decides whether there is
#: an action to assess: a transcript nobody can read is not a quiet
#: instruction, it is no instruction.
WORTH_ACTING_ON = 0.55


@dataclass(frozen=True)
class Heard:
    """One utterance, as the recogniser reports it."""

    text: str
    confidence: float
    #: What the customer actually said, when the table knows. Never read by
    #: anything that makes a decision — it exists so a test can assert on the
    #: gap between what was said and what was heard.
    truth: str | None = None


#: ``said → (heard, confidence, kind)``. Written from the failure modes
#: pt-BR recognition is known to make on amounts and names; every row is an
#: illustration, not a measurement.
TRANSCRIPTS: dict[str, tuple[str, float, str]] = {
    # magnitude collapse — the dangerous class
    "manda trezentos reais pra Renata": (
        "manda treze reais pra Renata",
        0.62,
        "magnitude",
    ),
    "transfere mil e duzentos pro João": (
        "transfere mil e dois pro João",
        0.58,
        "magnitude",
    ),
    "pix de quatrocentos pra Maria": ("pix de quatorze pra Maria", 0.51, "magnitude"),
    # recipient confusion — one phoneme, two people
    "manda cem reais pra Renata": ("manda cem reais pra Renato", 0.66, "recipient"),
    "pix de cinquenta pra Ana Lima": (
        "pix de cinquenta pra Ana Costa",
        0.59,
        "recipient",
    ),
    # heard cleanly — the control case, without which the table only proves
    # that low confidence blocks things
    "manda cem reais pra Renata Silva": (
        "manda cem reais pra Renata Silva",
        0.97,
        "clean",
    ),
    "qual é o meu saldo": ("qual é o meu saldo", 0.95, "clean"),
}


def transcribe(said: str) -> Heard:
    """What the recogniser reports for an utterance. Simulated; see the module
    docstring. An utterance the table does not know comes back clean, so a new
    test case does not silently become a low-confidence one."""
    heard, confidence, _kind = TRANSCRIPTS.get(said, (said, 0.96, "clean"))
    return Heard(text=heard, confidence=confidence, truth=said)


def voice_context(customer_id: str, session_id: str, heard: Heard) -> Context:
    """The channel's context. The whole adapter, in one function.

    Note what it does not do: it does not decide, does not lower a limit, does
    not ask for confirmation. It reports how well it heard, and the policy that
    already existed does the rest.
    """
    return Context(
        customer_id=customer_id,
        session_id=session_id,
        channel="voice",
        stt_confidence=heard.confidence,
    )


def worth_acting_on(heard: Heard) -> bool:
    """Is there an instruction here at all?

    Below this bar the honest answer to the customer is "I did not catch
    that", asked in the channel they are already in — not a proposal built on
    a guess and handed to the control plane to worry about. Filtering here
    rather than in policy is the boundary working: the channel owns whether
    it understood, the plane owns whether it may.
    """
    return heard.confidence >= WORTH_ACTING_ON


def spoken_amount(text: str) -> Decimal | None:
    """The amount in a pt-BR utterance, or ``None``.

    Deliberately small: it knows the words the transcript table uses and
    nothing more. A real deployment parses this with the model, and the point
    of this module is that swapping it changes nothing below.
    """
    words = {
        "treze": "13",
        "quatorze": "14",
        "cinquenta": "50",
        "cem": "100",
        "trezentos": "300",
        "quatrocentos": "400",
        "mil e dois": "1002",
        "mil e duzentos": "1200",
    }
    lowered = text.casefold()
    for word, value in sorted(words.items(), key=lambda kv: -len(kv[0])):
        if word in lowered:
            return Decimal(value)
    return None
