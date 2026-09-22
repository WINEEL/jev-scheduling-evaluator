"""Availability management endpoints (Task 56).

``GET    /api/v1/events/{event_id}/availability``
``PUT    /api/v1/events/{event_id}/availability/{ministry_membership_id}``
``DELETE /api/v1/events/{event_id}/availability/{ministry_membership_id}``
``POST   /api/v1/scheduling-periods/{scheduling_period_id}/availability-lock``

The service behind these, :mod:`app.services.availability`, already existed
and was already tested (Task 17), including Task 52's ``BACKUP`` tier. This
module is the HTTP surface Task 51's inspection found missing -- nothing
about the domain rule, the period-lock behavior, or the solver changes here.

**Every route stays thin.** Resolve the actor, resolve the URL's resources,
call the service, map the result. Every domain rule -- who may act, whether
membership and event agree on ministry, whether the period is locked, what a
deactivated target means -- lives in :mod:`app.services.availability`.

**``PUT`` records an explicit answer; ``DELETE`` clears it back to no
response.** :func:`~app.services.availability.set_availability` collapses
both into one setter where ``availability_state=None`` means "return to no
response" -- a shape chosen for the service's own reasons, not one this API
repeats. Splitting them into two verbs keeps ``None``/``null`` out of the
``PUT`` body entirely, exactly as Task 54 kept ``0`` out of the
staffing-requirement contract.

**The period lock is reported, not separately enforced here.** The listing
endpoint's ``availability_locked_at`` lets a caller show a locked period
before ever attempting a write; the actual refusal (409) still comes from
:func:`set_availability` itself when a mutation is attempted against a
locked period, so there is exactly one place that rule is enforced.

**Setting that lock lives here too** (Task 72), because it is the end of
availability collection rather than the start of scheduling: a head finishes
recording answers on these screens and then closes the period. The transition
itself has always existed as
:func:`app.services.scheduling_period.lock_availability`; only the HTTP
surface is new, and without it there was no way to reach the one state
``POST /scheduling-periods/{id}/schedule-versions`` requires.

**Cross-ministry management is refused by authorization and by the service's
own integrity check, not by URL shape.** The mutation routes take an event id
and a membership id with no ministry id to keep in agreement --
:func:`~app.services.authorization.require_ministry_operator`, scoped to
``event.ministry_id``, makes a Head of a *different* ministry's request 403,
and :func:`set_availability`'s own ``_require_same_ministry`` turns an
event/membership pair from two different ministries into a 409.

**Listing a scheduling period's own events is Task 54's, reused unchanged**
(:func:`app.services.scheduling_period.list_period_events`,
``GET /scheduling-periods/{id}/events``): availability is managed per event
exactly as staffing is, and nothing about choosing which event to work on is
specific to either.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.availability_schemas import (
    AvailabilityLockResponse,
    EventAvailabilityResponse,
    LockAvailabilityRequest,
    MembershipAvailabilityResponse,
    SetAvailabilityRequest,
)
from app.api.dependencies import get_current_actor, get_session
from app.models.core import MinistryMembership, Person
from app.models.scheduling_input import Event, SchedulingPeriod
from app.services.authorization import can_operate_ministry
from app.services.availability import EventAvailability, list_event_availability, set_availability
from app.services.scheduling_period import lock_availability

__all__ = ["router"]

router = APIRouter(tags=["availability"])

_EVENT_NOT_FOUND = "Event not found."
_MEMBERSHIP_NOT_FOUND = "Ministry membership not found."
_PERIOD_NOT_FOUND = "Scheduling period not found."


@router.get(
    "/events/{event_id}/availability",
    response_model=EventAvailabilityResponse,
    summary="An event's ministry members and their availability",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this event's ministry."},
        404: {"description": "No such event."},
    },
)
def read_event_availability(
    event_id: int = Path(ge=1, description="The event to show availability for."),
    include_inactive: bool = Query(
        default=False,
        description="Also return deactivated memberships and deactivated people.",
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> EventAvailabilityResponse:
    """Read-only. Every membership of this event's ministry -- active ones by
    default -- each with its stored answer for this event: ``null`` for no
    response, otherwise ``AVAILABLE``, ``BACKUP``, or ``UNAVAILABLE``.
    ``availability_locked_at`` says whether a write below would be refused.
    """
    event = _require_event(session, event_id)
    availability = list_event_availability(
        session, actor=actor, event=event, include_inactive=include_inactive,
    )
    return _availability_response(availability, actor=actor)


@router.put(
    "/events/{event_id}/availability/{ministry_membership_id}",
    response_model=MembershipAvailabilityResponse,
    summary="Record an availability answer for one membership at one event",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this event's ministry."},
        404: {"description": "No such event, or no such membership."},
        409: {
            "description": (
                "The event and membership belong to different ministries,"
                " this event's period has locked availability, or the"
                " requested answer would target a deactivated membership,"
                " person, or a cancelled event."
            )
        },
    },
)
def put_availability(
    event_id: int = Path(ge=1, description="The event being answered for."),
    ministry_membership_id: int = Path(ge=1, description="The membership being answered for."),
    body: SetAvailabilityRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MembershipAvailabilityResponse:
    """Record or change an explicit answer. Idempotent for a repeated answer
    that changes nothing -- the service's own rule, checked before the
    period-lock refusal, so re-sending the same answer never fails merely
    because the period has since locked.
    """
    event = _require_event(session, event_id)
    membership = _require_membership(session, ministry_membership_id)
    availability = set_availability(
        session, actor=actor, membership=membership, event=event,
        availability_state=body.availability_state, reason=body.reason,
    )
    # SetAvailabilityRequest.availability_state is always one of the three
    # explicit states (never None), and set_availability returns a row for
    # every explicit state -- None is only ever its clearing outcome.
    assert availability is not None
    return _membership_availability_response(membership, availability.availability_state)


@router.delete(
    "/events/{event_id}/availability/{ministry_membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear a membership's availability answer back to no response",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this event's ministry."},
        404: {"description": "No such event, or no such membership."},
        409: {"description": "This event's period has locked availability."},
    },
)
def delete_availability(
    event_id: int = Path(ge=1, description="The event to clear a response from."),
    ministry_membership_id: int = Path(ge=1, description="The membership to clear a response for."),
    reason: str | None = Query(
        default=None, description="Optional note for the audit trail."
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> None:
    """**204** whether or not a response existed to remove: clearing an
    already-absent response is the ordinary no-op the service already
    defines, not a 404 -- the event and membership both still exist, only the
    response between them does not. The period lock still applies to an
    actual removal (409), matching :func:`set_availability`'s own asymmetry.
    """
    event = _require_event(session, event_id)
    membership = _require_membership(session, ministry_membership_id)
    set_availability(
        session, actor=actor, membership=membership, event=event,
        availability_state=None, reason=reason,
    )


@router.post(
    "/scheduling-periods/{scheduling_period_id}/availability-lock",
    response_model=AvailabilityLockResponse,
    status_code=status.HTTP_200_OK,
    summary="Close availability collection for a scheduling period",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such scheduling period."},
    },
)
def post_availability_lock(
    scheduling_period_id: int = Path(
        ge=1, description="The period whose availability collection is closing."
    ),
    body: LockAvailabilityRequest = Body(
        default_factory=LockAvailabilityRequest,
        description="Optional note for the audit trail.",
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> AvailabilityLockResponse:
    """Freeze this period's availability, which is what starting a schedule
    requires.

    The transition itself is :func:`app.services.scheduling_period.lock_availability`
    and nothing about it is repeated here -- including its two properties that
    matter most to a caller:

    - **It is idempotent, and a repeat is 200, not 409.** Locking an
      already-locked period returns the original instant unchanged and writes
      no second audit row. A head who clicks twice, or two heads who click at
      once, both get the truth rather than an error about a state that is
      already what they asked for.
    - **It checks nothing about completeness.** No rule requires every member
      to have answered; "no response" is a valid permanent input state, and
      locking only says that ordinary availability edits stop being accepted.

    **There is no unlock, here or in the domain**, so this endpoint is one-way
    by omission rather than by a guard of its own. That is the existing
    model's decision, not a new one: reopening availability after a schedule
    has been built from it is a lifecycle question nobody has specified.
    """
    period = _require_period(session, scheduling_period_id)
    locked = lock_availability(
        session, actor=actor, period=period, reason=body.reason
    )
    # lock_availability either set the timestamp or found one already set.
    assert locked.availability_locked_at is not None
    return AvailabilityLockResponse(
        scheduling_period_id=locked.id,
        scheduling_period_name=locked.name,
        ministry_id=locked.ministry_id,
        availability_locked_at=locked.availability_locked_at,
    )


def _require_period(session: Session, scheduling_period_id: int) -> SchedulingPeriod:
    """The period the URL names, or 404, read on the request Session."""
    period = session.execute(
        select(SchedulingPeriod).where(SchedulingPeriod.id == scheduling_period_id)
    ).scalar_one_or_none()
    if period is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_PERIOD_NOT_FOUND
        )
    return period


def _require_event(session: Session, event_id: int) -> Event:
    """The event the URL names, or 404, read on the request Session."""
    event = session.execute(
        select(Event).where(Event.id == event_id)
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_EVENT_NOT_FOUND
        )
    return event


def _require_membership(session: Session, ministry_membership_id: int) -> MinistryMembership:
    """The membership the URL names, or 404, read on the request Session."""
    membership = session.execute(
        select(MinistryMembership).where(MinistryMembership.id == ministry_membership_id)
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_MEMBERSHIP_NOT_FOUND
        )
    return membership


def _membership_availability_response(
    membership: MinistryMembership, availability_state: str | None
) -> MembershipAvailabilityResponse:
    return MembershipAvailabilityResponse(
        ministry_membership_id=membership.id,
        person_id=membership.person_id,
        person_display_name=membership.person.display_name,
        membership_deactivated_at=membership.deactivated_at,
        person_deactivated_at=membership.person.deactivated_at,
        availability_state=availability_state,
    )


def _availability_response(
    availability: EventAvailability, *, actor: Person
) -> EventAvailabilityResponse:
    return EventAvailabilityResponse(
        event_id=availability.event_id,
        event_date=availability.event_date,
        event_name=availability.event_name,
        event_kind=availability.event_kind,
        ministry_id=availability.ministry_id,
        availability_locked_at=availability.availability_locked_at,
        memberships=[
            MembershipAvailabilityResponse(
                ministry_membership_id=m.ministry_membership_id,
                person_id=m.person_id,
                person_display_name=m.person_display_name,
                membership_deactivated_at=m.membership_deactivated_at,
                person_deactivated_at=m.person_deactivated_at,
                availability_state=m.availability_state,
            )
            for m in availability.memberships
        ],
        can_operate=can_operate_ministry(
            actor, ministry_id=availability.ministry_id
        ),
    )
