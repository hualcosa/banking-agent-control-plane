"""Cognito JWT → customer id, verified in-process, one answer for every failure."""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from trail.jwt_identity import verify_jwt

pytestmark = pytest.mark.unit

ISSUER = "https://cognito-idp.sa-east-1.amazonaws.com/sa-east-1_TEST"
CLIENT = "client-abc"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key=KEY, **claims) -> str:
    base = {
        "sub": "cust_jwt",
        "iss": ISSUER,
        "client_id": CLIENT,
        "exp": int(time.time()) + 300,
    }
    base.update(claims)
    return jwt.encode(base, key, algorithm="RS256", headers={"kid": "k1"})


def resolver(key=KEY):
    return lambda _token: key.public_key()


def test_a_valid_access_token_yields_sub() -> None:
    assert (
        verify_jwt(
            f"Bearer {token()}", issuer=ISSUER, client_id=CLIENT, key_for=resolver()
        )
        == "cust_jwt"
    )


def test_an_id_token_with_aud_is_accepted_too() -> None:
    t = token(aud=CLIENT, client_id=None)
    assert (
        verify_jwt(f"Bearer {t}", issuer=ISSUER, client_id=CLIENT, key_for=resolver())
        == "cust_jwt"
    )


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Basic abc",
        f"Bearer {token(exp=int(time.time()) - 1)}",
        f"Bearer {token(iss='https://evil.example')}",
        f"Bearer {token(client_id='someone-else')}",
        f"Bearer {token(OTHER_KEY)}",
        f"Bearer {token(sub=None)}",
    ],
)
def test_every_failure_is_none(header) -> None:
    assert (
        verify_jwt(header, issuer=ISSUER, client_id=CLIENT, key_for=resolver()) is None
    )
