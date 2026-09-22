"""Google sign-in, sign-out, and nothing else.

Three routes, and the shape of the flow is what makes them safe:

``GET  /api/v1/auth/google/login``     -> 302 to Google
``GET  /api/v1/auth/google/callback``  -> 302 back to the app, with or without a session
``POST /api/v1/auth/logout``           -> 204, cookie cleared

**All three are unauthenticated, and only these three are.** Every other route in
the API takes :func:`app.api.dependencies.get_current_actor` and refuses without
one. These cannot -- they are how an actor comes to exist -- so each is written
to give nothing away to an anonymous caller: the login route reveals only that
Google sign-in exists, the callback route answers every failure with the same
redirect shape, and logout is unconditionally a 204 whether or not anyone was
signed in.

**Redirects are relative on purpose.** The callback answers ``Location: /``
rather than an absolute URL. The browser resolves that against the origin it is
talking to -- which is the frontend, because the frontend proxies these routes on
its own origin -- so the backend never has to be told the frontend's public URL,
and there is no configurable absolute redirect target for anyone to point
somewhere else. The one absolute URL in the flow is the Google redirect URI,
which is pinned by configuration *and* by Google's own allow-list.

**Why the callback never returns an error status.** A failed sign-in redirects to
``/?auth_error=<reason>`` with a 302, not a 401 with a JSON body. The caller here
is a browser mid-navigation, arriving from Google: answering it with JSON would
leave the person looking at raw text instead of the application. The reason codes
are the fixed identifiers in :data:`app.auth.errors.REASONS` and never carry
anything derived from the request, the token, or the database.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.auth.email_link import find_person_by_email
from app.auth.errors import SignInError
from app.auth.google import build_oauth, verified_email_of
from app.auth.session import clear_session, establish_session
from app.config import Settings, get_settings

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["authentication"])

#: Where a browser lands after the callback, success or failure. Relative, so it
#: resolves against whatever origin the browser is actually on.
_APP_ROOT = "/"

#: 303, not 302. The callback is reached by GET and answers with a GET, so the
#: distinction is invisible here -- but 303 states "fetch the other resource with
#: GET" explicitly, which is what is meant, and it is the status that stays
#: correct if the route is ever reached by anything but a GET.
_SEE_OTHER = status.HTTP_303_SEE_OTHER


@router.get(
    "/google/login",
    summary="Begin Google sign-in",
    response_class=RedirectResponse,
    responses={
        302: {"description": "Redirect to Google's consent screen."},
        303: {"description": "Google sign-in is not configured on this deployment."},
    },
)
async def google_login(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> Response:
    """Send the browser to Google.

    Authlib generates the ``state`` and ``nonce`` and stores them in the session
    cookie before redirecting, which is what makes the callback able to prove
    that the response it receives belongs to the request it sent. Neither value
    is generated, stored or checked by hand anywhere in this module.
    """
    try:
        client = build_oauth(settings)
    except SignInError as refusal:
        # The only failure reachable here is "not configured", and it is a
        # deployment fault rather than a caller's. Logged as a warning with no
        # values attached; the browser gets the same redirect shape as any
        # other refusal.
        logger.warning("Google sign-in unavailable: %s", refusal.reason)
        return _refuse(refusal.reason)

    return await client.authorize_redirect(request, _redirect_uri(request, settings))


@router.get(
    "/google/callback",
    summary="Complete Google sign-in",
    response_class=RedirectResponse,
    responses={303: {"description": "Redirect to the application, signed in or refused."}},
)
async def google_callback(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Verify Google's response, find the linked Person, and start a session.

    The order of operations is the security of this endpoint, so it is stated:

    1. **Authlib validates the response** -- ``state`` matches what the login
       route stored, the code is exchanged over TLS with the client secret, and
       the returned ID token's signature, issuer, audience, expiry and ``nonce``
       are checked against Google's published keys. A failure at this step is
       indistinguishable from a forged callback and is treated as one.
    2. **The email must be one Google says it verified**
       (:func:`app.auth.verified_email_of`).
    3. **An existing Person must already carry that address.** No Person is
       created, no name is consulted, and no near-match is accepted.
    4. **That Person must be active.**
    5. Only then does a session exist.

    Nothing from the browser influences any of it. The request's own query
    string is read exclusively by Authlib, for the ``code`` and ``state`` it
    validates; this function never reads an email, a person id or an actor from
    the request.
    """
    try:
        client = build_oauth(settings)
    except SignInError as refusal:
        logger.warning("Google sign-in unavailable: %s", refusal.reason)
        return _refuse(refusal.reason)

    try:
        token = await client.authorize_access_token(request)
    except Exception:
        # Deliberately broad, and deliberately not re-raised. Authlib raises
        # several unrelated types here -- a mismatched state, a replayed or
        # expired code, an error response from Google, a signature that does not
        # verify -- and every one of them means the same thing to this endpoint:
        # this response cannot be trusted. Letting any of them escape would turn
        # a failed sign-in into a 500 with a traceback.
        #
        # ``exc_info`` is off on purpose. An Authlib exception can carry the
        # token endpoint's response body, and that body can contain the
        # authorization code or a token; a stack trace in Cloud Logging is not
        # where those belong.
        logger.warning("Google sign-in response failed validation")
        return _refuse("invalid_response")

    try:
        claims = token.get("userinfo") or {}
        email = verified_email_of(claims)
    except SignInError as refusal:
        logger.warning("Google sign-in refused: %s", refusal.reason)
        return _refuse(refusal.reason)

    person = find_person_by_email(session, email)
    if person is None:
        # **The pilot's central rule.** An address Google verified, belonging to
        # nobody in this church's records, is refused -- not registered, not
        # matched by name, not offered a profile to claim.
        #
        # The address is not logged. It is a real person's email, it is private,
        # and Cloud Logging is not the place for it; the count of refusals is
        # visible, which is what an operator actually needs.
        logger.info("Google sign-in refused: no linked person")
        return _refuse("not_linked")

    if person.deactivated_at is not None:
        logger.info("Google sign-in refused: linked person is deactivated")
        return _refuse("person_inactive")

    establish_session(request, person_id=person.id)
    # The id, never the address or the name: this line goes to Cloud Logging.
    logger.info("Google sign-in succeeded for person_id=%s", person.id)
    return RedirectResponse(_APP_ROOT, status_code=_SEE_OTHER)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out",
    responses={204: {"description": "Session cleared."}},
)
def logout(request: Request) -> Response:
    """Clear the session cookie.

    **POST, not GET.** A GET would be triggerable by any page that could get the
    browser to load a URL -- an ``<img>`` tag is enough -- which would let an
    unrelated site sign people out of this one. It is a mild attack, but the fix
    is free.

    **Unconditionally 204**, whether or not anybody was signed in. There is no
    actor dependency here on purpose: making logout require a valid session
    would mean an expired one could not be cleared, leaving a stale cookie in
    the browser with no way to remove it.

    CSRF is covered by the cookie itself: ``SameSite=Lax`` means a cross-site
    POST does not carry it, so a forged request arrives with no session to
    clear.
    """
    clear_session(request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _redirect_uri(request: Request, settings: Settings) -> str:
    """The absolute URL Google must send the browser back to.

    **Configured explicitly wherever it matters, derived only as a local
    convenience.** ``OAUTH_REDIRECT_URL`` wins whenever it is set, and it is set
    in every deployment, because deriving it from the request is wrong in two
    ways behind a proxy: Cloud Run terminates TLS upstream, so the request this
    process sees claims to be ``http``; and the frontend proxies this route, so
    the host this process sees is the API service, not the origin the browser is
    on. A derived URI would therefore be both the wrong scheme and the wrong
    host -- and, because Google requires the redirect URI to match its
    allow-list exactly, it would fail closed rather than redirect anywhere
    unexpected.

    The fallback exists so a developer running both processes directly, with no
    proxy in front, gets a working sign-in without configuring anything.
    """
    configured = settings.oauth_redirect_url.strip()
    if configured:
        return configured
    return str(request.url_for("google_callback"))


def _refuse(reason: str) -> RedirectResponse:
    """Back to the application, carrying why -- and nothing else.

    ``reason`` is always one of :data:`app.auth.errors.REASONS`' fixed keys, so
    this cannot become a reflection of caller-supplied text into the response.
    """
    return RedirectResponse(f"{_APP_ROOT}?auth_error={reason}", status_code=_SEE_OTHER)
