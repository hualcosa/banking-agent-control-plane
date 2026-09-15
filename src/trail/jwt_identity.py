"""Who the caller is, according to Cognito — verified here, not assumed.

In AWS the channel is the AgentCore Runtime with a Cognito JWT authorizer
in front. The Runtime validates the bearer token and, because `Authorization`
is on the request-header allowlist, forwards it to this process. AWS's own
sample then decodes it *without* checking the signature, on the grounds that
upstream already did. This module checks anyway: a JWKS fetch is one cached
HTTPS call per key rotation, and the alternative is a service whose identity
story depends on a header nobody in this process can distinguish from a
forged one when the service is run anywhere else — compose, a laptop, a test.

Same contract as `trail.identity`: the customer id, or `None` for every
failure. Callers must not distinguish the cases.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

import jwt

#: Cognito access tokens carry `client_id`; id tokens carry `aud`. Either is
#: the app client the authorizer allowed, so either satisfies the check.
_CLIENT_CLAIMS = ("client_id", "aud")


def verify_jwt(
    header_value: str | None,
    *,
    issuer: str,
    client_id: str,
    key_for: Callable[[str], Any],
) -> str | None:
    """The `sub` of a valid bearer token, or ``None``."""
    if not header_value or not issuer or not client_id:
        return None
    scheme, _, token = header_value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        key = key_for(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            options={"require": ["exp", "iss", "sub"], "verify_aud": False},
        )
    except (jwt.PyJWTError, ValueError):
        return None
    presented = {claims.get(name) for name in _CLIENT_CLAIMS}
    if client_id not in presented:
        return None
    sub = claims.get("sub")
    return sub if isinstance(sub, str) and sub else None


@lru_cache(maxsize=4)
def jwks_key_resolver(issuer: str) -> Callable[[str], Any]:
    """A ``key_for`` backed by the issuer's JWKS, fetched once and cached.

    Cognito publishes it at ``<issuer>/.well-known/jwks.json``. ``PyJWKClient``
    caches keys and refetches on an unknown ``kid``, which is what a rotation
    looks like from here.
    """
    client = jwt.PyJWKClient(f"{issuer.rstrip('/')}/.well-known/jwks.json")
    return lambda token: client.get_signing_key_from_jwt(token).key
