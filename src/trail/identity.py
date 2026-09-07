"""Who the caller is, according to the channel — not according to the agent.

The agent must never be the thing that decides whose money it is moving. A
customer id chosen inside the process (a constant in a tool module, a value the
model repeats back) is an identity the model can influence; the only identity
worth acting on is one the *channel* authenticated and handed over.

This module is that hand-over, in its cheapest honest form: the channel signs
the customer id with a shared secret and sends the pair in one header; the
service recomputes the signature and refuses anything that does not match.

    X-Trail-Identity: <customer_id>:<hex sha256 hmac>

Three properties matter and each is one line below:

* **Constant-time comparison.** :func:`hmac.compare_digest`, never ``==``: a
  byte-by-byte comparison leaks the correct prefix through timing, which turns
  forging a signature into 32 cheap guesses instead of one impossible one.
* **One answer for every failure.** A header that is missing, malformed, signed
  with the wrong secret, or signed for a customer who does not exist all
  produce ``None``. The caller turns that into one 401 with one message, so the
  response is not an oracle for which part was wrong or which customers exist.
* **Fail closed on no secret.** An empty ``TRAIL_IDENTITY_SECRET`` authenticates
  nobody rather than everybody. A misconfigured deployment that answers every
  request is the failure this whole file exists to prevent.

This is deliberately not a token format: no expiry, no audience, no rotation.
It is the seam, not the answer. Milestone 4 replaces it with a Cognito JWT
verified at the same point in ``trail.app`` — a different :func:`verify`, the
same one line of plumbing behind it.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

#: Sent by the channel, read by the service. Lower-case because HTTP header
#: names are case-insensitive and every lookup here goes through a mapping that
#: already normalises them.
HEADER = "x-trail-identity"

#: Mixed into every signature so a value signed for some other purpose with the
#: same secret cannot be replayed here as an identity.
_DOMAIN = b"trail.identity.v1:"


def _mac(customer_id: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), _DOMAIN + customer_id.encode("utf-8"), sha256
    ).hexdigest()


def sign(customer_id: str, secret: str) -> str:
    """The header value a channel sends for ``customer_id``.

    Raises on an empty secret rather than emitting a signature nothing can
    verify — a client that silently sends an unverifiable header presents as a
    server-side auth bug, which is the most expensive place to look for it.
    """
    if not secret:
        raise ValueError("cannot sign an identity without TRAIL_IDENTITY_SECRET")
    return f"{customer_id}:{_mac(customer_id, secret)}"


def verify(header_value: str | None, secret: str) -> str | None:
    """The customer id a valid header carries, or ``None`` for every failure.

    ``None`` is the whole vocabulary of failure on purpose: see the module
    docstring. Callers must not distinguish the cases in what they return.
    """
    if not secret or not header_value:
        return None
    # From the right: a customer id is free to contain a colon; a signature is
    # not, so the last one is always the separator.
    customer_id, _, signature = header_value.rpartition(":")
    if not customer_id or not signature:
        return None
    if not hmac.compare_digest(_mac(customer_id, secret), signature):
        return None
    return customer_id
