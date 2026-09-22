"""The application session: a signed cookie naming one Person id.

**What is in it, and what is deliberately not.** The session carries the
internal ``person_id`` and the instant it was established. It does **not** carry
the access token, the refresh token, the ID token, the email address, the
display name or the admin flag.

Two reasons, and both matter:

- *The cookie is signed, not encrypted.* Starlette's ``SessionMiddleware`` seals
  the payload against tampering, but anyone holding the cookie can base64-decode
  and read it. A token in there would be a token handed to whoever gets a copy.
- *Authority must be read fresh.* Putting ``is_admin`` in the cookie would mean
  an Admin demoted this morning keeps administering until their session expires,
  because the cookie would still say so. The session answers "who", and every
  request re-reads "what may they do" from the database --
  :func:`app.api.dependencies.get_current_actor` loads the Person row, and
  :mod:`app.services.authorization` reads the live flags off it.

**Expiry is cryptographic, not advisory.** ``SessionMiddleware`` is configured
with ``max_age``, which itsdangerous applies to the signature's own timestamp.
An expired cookie therefore fails to *verify*, not merely to look current: a
client cannot extend its own session by editing a field, because the field is
not what is checked.

**Logging out clears the cookie rather than invalidating a stored record.**
There is no server-side session table, which is the right trade for a pilot on
Cloud Run with minimum instances 0 -- a database round trip per request to
support a revocation feature nobody has asked for. The consequence is stated
plainly rather than glossed: a cookie copied off a machine before logout stays
valid until it expires. The levers that do exist are deactivating the Person
(refused at :func:`get_current_actor`, immediately and for every live session),
unlinking their email (refused at the next sign-in), and rotating
``SESSION_SECRET`` (invalidates every session everywhere, at once).
"""

from __future__ import annotations

import datetime

from starlette.requests import Request

__all__ = [
    "SESSION_COOKIE_NAME",
    "SESSION_MAX_AGE_SECONDS",
    "establish_session",
    "read_session_person_id",
    "clear_session",
]

#: The cookie name. Given a project-specific prefix rather than Starlette's
#: default ``session`` so it cannot collide with another app's cookie if one is
#: ever served from the same hostname.
SESSION_COOKIE_NAME = "church_scheduling_session"

#: Eight hours: comfortably longer than a sitting at the scheduling screens,
#: comfortably shorter than "until the laptop is next rebooted".
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60

#: Session keys. Named constants because the middleware's session is an
#: untyped dict and a typo in one of two places would silently sign nobody in.
_PERSON_ID = "person_id"
_AUTHENTICATED_AT = "authenticated_at"


def establish_session(request: Request, *, person_id: int) -> None:
    """Record ``person_id`` as the signed-in identity for this browser.

    Clears first, so a second sign-in replaces the session rather than merging
    into it -- leaving no key behind from whoever was signed in before.
    """
    request.session.clear()
    request.session[_PERSON_ID] = int(person_id)
    request.session[_AUTHENTICATED_AT] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat()


def read_session_person_id(request: Request) -> int | None:
    """The Person id this session names, or ``None``.

    Strict about the type, because the session dict is deserialized JSON and
    "whatever was in the cookie" is not a Person id:

    - ``bool`` is rejected explicitly. ``True`` is an ``int`` in Python and
      ``isinstance(True, int)`` is ``True``, so without this line a session
      holding ``true`` would resolve to Person 1.
    - a ``float`` or a numeric *string* is rejected rather than coerced. Both
      would mean accepting a shape this application never writes, and the only
      thing that could have written one is something other than this
      application.
    - zero and negatives are rejected: identity columns start at 1.

    Returns ``None`` for every one of those, and for a session that is simply
    absent or expired. There is no error to distinguish -- the caller's next
    step is a 401 in every case.
    """
    try:
        raw = request.session.get(_PERSON_ID)
    except AssertionError:
        # SessionMiddleware is not installed. Cannot happen in the real app --
        # app.main installs it unconditionally -- but a bare ASGI test app
        # would otherwise raise from inside a security check.
        return None

    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if raw <= 0:
        return None
    return raw


def clear_session(request: Request) -> None:
    """Sign this browser out.

    Emptying the session dict is what makes ``SessionMiddleware`` emit an
    expired ``Set-Cookie`` on the response, so the browser drops it rather than
    keeping a cookie that merely no longer resolves.
    """
    request.session.clear()
