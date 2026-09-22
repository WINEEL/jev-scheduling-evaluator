"""The Google OAuth/OIDC client.

Authlib does the parts that are easy to get subtly and silently wrong: it
generates and stores the ``state``, generates and checks the ``nonce``, exchanges
the authorization code over TLS with client authentication, fetches and caches
Google's JWKS, and validates the ID token's signature, issuer, audience and
expiry. Hand-writing that is how an authentication bug gets written, so none of
it is hand-written here.

**Scopes are the three minimal ones and nothing else** -- ``openid``, ``email``,
``profile``. No Calendar, no Directory, no offline access: this application asks
Google one question ("who is this?") and has no business holding a refresh token
to ask anything later.

**Discovery is by URL, not by hard-coded endpoints.** ``server_metadata_url``
points at Google's published OIDC document, so the authorization, token and JWKS
endpoints -- and Google's signing-key rotation -- are read from Google rather
than pinned in this file and left to rot.

**Nothing in this module is created at import time.** The client is built on
first use, so the application imports and the test suite runs on a machine with
no Google credentials at all.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Mapping

from authlib.integrations.starlette_client import OAuth

from app.auth.errors import SignInError
from app.config import Settings

__all__ = ["GOOGLE_CLIENT_NAME", "SCOPES", "build_oauth", "verified_email_of"]

GOOGLE_CLIENT_NAME = "google"

#: The only scopes ever requested. ``openid`` asks for an ID token at all;
#: ``email`` adds the address and its verification flag; ``profile`` adds the
#: display name used in the sign-in log line.
SCOPES = "openid email profile"

#: Google's OIDC discovery document.
SERVER_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"

#: The issuer values Google is permitted to claim. Both are legitimate and
#: Google uses them interchangeably; anything else is not Google.
ALLOWED_ISSUERS = frozenset(
    {"https://accounts.google.com", "accounts.google.com"}
)


@lru_cache(maxsize=1)
def _oauth_registry() -> OAuth:
    """The process-wide Authlib registry.

    Cached so the JWKS Authlib fetches on first validation is reused across
    requests rather than re-fetched per sign-in. The cache key is nothing --
    settings are passed to :func:`build_oauth` instead, which reads them at
    registration time.
    """
    return OAuth()


def build_oauth(settings: Settings) -> Any:
    """The registered Google client, ready to redirect or to exchange a code.

    :raises SignInError: ``not_configured`` -- the deployment has no client id,
        client secret or session secret. Raised rather than returning ``None``
        so a misconfigured deployment produces the same "not configured" page as
        any other sign-in refusal, instead of an unhandled ``AttributeError``
        somewhere further in.
    """
    if not settings.google_oauth_configured:
        raise SignInError("not_configured")

    registry = _oauth_registry()
    existing = getattr(registry, GOOGLE_CLIENT_NAME, None)
    if existing is not None:
        return existing

    registry.register(
        name=GOOGLE_CLIENT_NAME,
        client_id=settings.google_oauth_client_id.strip(),
        client_secret=settings.google_oauth_client_secret.strip(),
        server_metadata_url=SERVER_METADATA_URL,
        client_kwargs={"scope": SCOPES},
    )
    return getattr(registry, GOOGLE_CLIENT_NAME)


def reset_oauth_registry() -> None:
    """Drop the cached registry. For tests, and for nothing else."""
    _oauth_registry.cache_clear()


def verified_email_of(claims: Mapping[str, Any]) -> str:
    """The address Google has *verified* this person controls.

    Three checks, and each rejects a token that a laxer reading would accept:

    **The issuer must be Google.** Authlib already validates this against the
    discovery document; re-checking here costs nothing and means the rule is
    visible in the module that depends on it rather than assumed.

    **``email_verified`` must be exactly ``True``.** Google returns a real
    boolean, so the comparison is ``is True`` rather than a truth test: the
    string ``"false"`` is truthy in Python, and an unverified address that
    arrives as text must not become a verified one because of it. An address
    Google has not verified proves only that someone typed it.

    **The address must be present and non-blank.** A token with ``openid`` but
    no readable ``email`` cannot be linked to anybody, and there is no second
    key to fall back to -- falling back to ``sub`` would mean linking accounts
    by an identifier no admin can see or type.

    :raises SignInError: ``invalid_response`` for a bad issuer or missing
        address, ``email_not_verified`` for an unverified one.
    """
    issuer = claims.get("iss")
    if issuer not in ALLOWED_ISSUERS:
        raise SignInError("invalid_response", "ID token issuer is not Google")

    if claims.get("email_verified") is not True:
        raise SignInError("email_not_verified")

    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        raise SignInError("invalid_response", "ID token carries no email claim")

    return email.strip()
