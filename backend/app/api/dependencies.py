"""Request-scoped dependencies: the database Session, and who is acting.

**The identity boundary.** :func:`get_current_actor` is the one place an
endpoint learns which Person is making a request. Future endpoints take it as a
dependency and never accept an actor id in a path, query string or request body
-- an endpoint that let the caller name the actor would be an endpoint with no
authentication at all, however carefully the domain checked permissions
afterwards.

**It answers exactly one question: who?** Whether that person may manage a
particular ministry is the domain's decision, made by
:func:`app.services.authorization.require_ministry_operator` inside the service
that is about to act. Putting ministry authorization here would split one rule
across two layers and give the HTTP boundary an opinion it has no business
having.

**Identity comes from the signed session cookie, and fails closed.** Task 76
replaced the body of :func:`_resolve_actor_person_id` exactly as this module
said it would: the dependency's signature, its failure behaviour and every
endpoint using it are unchanged, and what is read is now
:mod:`app.auth.session` rather than a header.

Two sources are consulted, strictly in this order:

1. **The session cookie**, established only by a completed Google sign-in
   (:mod:`app.api.routes_auth`). This is the production path and the only one
   that exists in a deployment.
2. **The ``X-Dev-Actor-Person-Id`` header**, and only when
   ``CHURCH_SCHEDULING_DEV_AUTH`` is exactly ``"1"`` *and* ``APP_ENV`` is not
   ``production`` -- see :attr:`app.config.Settings.dev_actor_auth_enabled`,
   where the production override lives. It is a local convenience for working
   on the UI without a Google round trip.

**The order is a security property, not a preference.** A live session is used
and the header is never consulted, so the header cannot be used to switch actor
mid-session even on a developer's machine where it is enabled. And because
``APP_ENV=production`` disables the header outright, a production deployment
authenticates through Google or not at all, whatever else is set in its
environment.

There is no fallback actor, no "first Person in the database", and no hard-coded
Admin: an unauthenticated request simply cannot reach an endpoint that needs one.
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.session import read_session_person_id
from app.config import Settings, get_settings
from app.db import SessionLocal
from app.models.core import Person

__all__ = ["get_current_actor", "get_session"]

#: Deliberately blunt, so it reads as a warning wherever FastAPI renders it --
#: OpenAPI, the interactive docs, editor completions.
_DEV_HEADER_DESCRIPTION = (
    "**LOCAL DEVELOPMENT ONLY — NOT AUTHENTICATION.** The id of the Person to"
    " act as. Accepted only when the server is started with"
    " CHURCH_SCHEDULING_DEV_AUTH=1, and ignored otherwise. Anyone who can send"
    " this header can act as anyone, including an Admin, so it must never be"
    " enabled in a deployed environment, and APP_ENV=production disables it"
    " outright. It exists so the UI and API can be worked on locally without a"
    " Google round trip; a real session always takes precedence over it."
)

#: One message for every identity failure. Saying *why* would leak whether a
#: given Person id exists, and whether it is active -- an unauthenticated
#: caller has no business learning either.
_NOT_AUTHENTICATED = "Not authenticated."


def get_session() -> Iterator[Session]:
    """One Session per request, and the request's unit of work.

    **The division of labour, stated once because everything else follows from
    it:** a domain service owns *what the change means* and never commits (see
    :mod:`app.services`); this dependency owns *when the change becomes real*.
    Putting the commit here rather than in each endpoint means a future
    mutation endpoint cannot forget it, cannot commit halfway, and cannot
    commit a request that went on to fail after the service returned.

    - The request handler returns normally -> **commit, exactly once.**
    - Anything raises -- a dependency, the endpoint, a service, or FastAPI
      serializing the response -- -> **roll back, exactly once**, and let the
      exception continue. Nothing is swallowed and nothing is compensated for
      by hand.
    - Either way -> **close.**

    A read-only request such as ``GET /api/v1/me`` ends the same way. Its
    commit closes a transaction that changed nothing, which costs nothing and
    keeps one policy instead of two.

    **On autobegin.** :func:`get_current_actor` reads before the route body
    runs, so SQLAlchemy has already begun a transaction by the time the
    endpoint is entered. That is exactly why there is no ``session.begin()``
    here -- calling it after the actor lookup would raise, and it is not
    needed: ``commit()`` and ``rollback()`` act on the transaction autobegin
    already opened.

    **If the commit itself fails**, the ``except`` below rolls back and
    re-raises, so a failed commit becomes a failed request rather than a
    successful response describing writes that never landed.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_actor(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
    x_dev_actor_person_id: str | None = Header(
        default=None,
        alias="X-Dev-Actor-Person-Id",
        description=_DEV_HEADER_DESCRIPTION,
    ),
) -> Person:
    """The active :class:`Person` making this request.

    Uses the request's own Session -- FastAPI caches a dependency per request,
    so the lookup here and anything the endpoint does afterwards share one
    Session and one transactional view.

    **Every failure is 401, deliberately.** A missing header, a malformed id, an
    id nobody has, and a deactivated Person all produce the same response. It is
    tempting to return 403 for the deactivated case, since identity was
    established -- but distinguishing the two would tell an unauthenticated
    caller which Person ids exist, and a probe that maps out the church's people
    is not something to hand out for free.

    :raises HTTPException: 401 whenever an active actor cannot be established.
    """
    person_id = _resolve_actor_person_id(
        request=request,
        settings=settings,
        dev_actor_person_id=x_dev_actor_person_id,
    )
    person = session.execute(
        select(Person).where(Person.id == person_id)
    ).scalar_one_or_none()

    if person is None or person.deactivated_at is not None:
        raise _unauthenticated()
    return person


def _resolve_actor_person_id(
    *, request: Request, settings: Settings, dev_actor_person_id: str | None
) -> int:
    """Work out which Person id this request is, or refuse.

    **This function is the seam, and Task 76 is what it was waiting for.** Its
    body now reads a verified session established by Google sign-in; its
    signature gained the request it reads that session from, and nothing else in
    the API changed.

    **The session is consulted first, and a valid one ends the decision.** The
    development header is not read, not parsed, and cannot override it -- so
    even on a machine where the header is enabled, a signed-in developer cannot
    silently act as somebody else by adding one.

    The development gate is then checked unconditionally before the header is
    touched. When it is off -- which includes *every* production deployment,
    regardless of what ``CHURCH_SCHEDULING_DEV_AUTH`` says, because
    :attr:`~app.config.Settings.dev_actor_auth_enabled` refuses in production --
    the header is not parsed, not looked up, and not mentioned in the response.
    """
    session_person_id = read_session_person_id(request)
    if session_person_id is not None:
        return session_person_id

    if not settings.dev_actor_auth_enabled:
        raise _unauthenticated()
    if dev_actor_person_id is None:
        raise _unauthenticated()

    # Strict parsing rather than int(): "+5", " 5", "5_0" and "０" are all
    # accepted by int() and none of them is an id anybody meant to send.
    candidate = dev_actor_person_id.strip()
    if not candidate.isascii() or not candidate.isdigit():
        raise _unauthenticated()
    person_id = int(candidate)
    if person_id <= 0:
        raise _unauthenticated()
    return person_id


def _unauthenticated() -> HTTPException:
    """The single failure response.

    No ``WWW-Authenticate`` header: there is no authentication scheme to name
    yet, and advertising ``Bearer`` would describe something this service does
    not implement.
    """
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail=_NOT_AUTHENTICATED
    )
