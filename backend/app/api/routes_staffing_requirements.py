"""Staffing-requirement management endpoints (Task 54).

``GET    /api/v1/scheduling-periods/{scheduling_period_id}/events``
``GET    /api/v1/events/{event_id}/staffing-requirements``
``PUT    /api/v1/events/{event_id}/staffing-requirements/{ministry_role_id}``
``DELETE /api/v1/events/{event_id}/staffing-requirements/{ministry_role_id}``

The service behind the last three, :mod:`app.services.staffing_requirement`,
already existed and was already tested (Tasks 15-16). This module is the HTTP
surface Task 51's inspection found missing -- nothing about the domain rule
changes here.

**The first route is a necessary enabler, not scope creep.** Staffing is
managed per event, and nothing before Task 54 exposed a scheduling period's
events over HTTP at all -- a Ministry Head choosing "which Sunday" had no way
to see the choices. :func:`app.services.scheduling_period.list_period_events`
is new for exactly this reason and is read-only: it lists events, and creates,
edits, or cancels none.

**Every route stays thin.** Resolve the actor, resolve the URL's resources,
call the service, map the result. Every domain rule -- who may act, whether
event and role agree on ministry, whether the role is active, what a
non-positive count means -- lives in the service or its own request schema.
No route commits: :func:`get_session` is the transaction boundary.

**``PUT`` sets a positive count; ``DELETE`` clears it.** The service's own
:func:`~app.services.staffing_requirement.set_staffing_requirement` collapses
both into one setter where ``required_count=0`` means "remove" -- a shape
chosen for the service's own reasons (its docstring), not one this API
repeats. Splitting them into two verbs here keeps ``0`` out of the wire
contract entirely: a client that wants "not required" calls ``DELETE``, never
``PUT`` with a magic number, and Pydantic's own ``ge=1`` on the request body
refuses a non-positive count before the service is even reached.

**Cross-ministry management is refused by authorization, not by URL shape.**
Both single-role routes take an event id and a role id with no ministry id to
keep in agreement -- the service's own
:func:`~app.services.authorization.require_ministry_operator` check, scoped to
``event.ministry_id``, already makes a Head of a *different* ministry's
request 403, and :func:`~app.services.staffing_requirement.set_staffing_requirement`'s
own ``_require_same_ministry`` check turns an event/role pair from two
different ministries into a 409, never a silent cross-ministry write.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.staffing_requirement_schemas import (
    EventStaffingResponse,
    EventSummaryResponse,
    PeriodEventsResponse,
    RoleStaffingResponse,
    SetStaffingRequirementRequest,
)
from app.models.core import MinistryRole, Person
from app.models.scheduling_input import Event, SchedulingPeriod
from app.services.authorization import can_operate_ministry
from app.services.scheduling_period import EventSummary, list_period_events
from app.services.staffing_requirement import (
    EventStaffing,
    list_event_staffing_requirements,
    set_staffing_requirement,
)

__all__ = ["router"]

router = APIRouter(tags=["staffing requirements"])

_PERIOD_NOT_FOUND = "Scheduling period not found."
_EVENT_NOT_FOUND = "Event not found."
_ROLE_NOT_FOUND = "Ministry role not found."


@router.get(
    "/scheduling-periods/{scheduling_period_id}/events",
    response_model=PeriodEventsResponse,
    summary="A scheduling period's events, to choose one for staffing",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period."},
    },
)
def read_period_events(
    scheduling_period_id: int = Path(ge=1, description="The period to list events for."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PeriodEventsResponse:
    """Read-only. Every event of this period, cancelled ones included and
    clearly marked, in date order.
    """
    period = _require_period(session, scheduling_period_id)
    events = list_period_events(session, actor=actor, period=period)
    return _period_events_response(
        scheduling_period_id, events, ministry_id=period.ministry_id, actor=actor
    )


@router.get(
    "/events/{event_id}/staffing-requirements",
    response_model=EventStaffingResponse,
    summary="An event's active roles and their current required counts",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this event's ministry."},
        404: {"description": "No such event."},
    },
)
def read_event_staffing(
    event_id: int = Path(ge=1, description="The event to show staffing for."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> EventStaffingResponse:
    """Read-only. Every currently active role in the event's ministry, each
    with its current required count (``null`` where none is set).
    """
    event = _require_event(session, event_id)
    staffing = list_event_staffing_requirements(session, actor=actor, event=event)
    return _staffing_response(staffing, actor=actor)


@router.put(
    "/events/{event_id}/staffing-requirements/{ministry_role_id}",
    response_model=RoleStaffingResponse,
    summary="Set how many people this role needs at this event",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this event's ministry."},
        404: {"description": "No such event, or no such role."},
        409: {
            "description": (
                "The event and role belong to different ministries, the"
                " event is cancelled, or the role is deactivated."
            )
        },
    },
)
def put_staffing_requirement(
    event_id: int = Path(ge=1, description="The event to staff."),
    ministry_role_id: int = Path(ge=1, description="The role to require."),
    body: SetStaffingRequirementRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> RoleStaffingResponse:
    """Create the requirement if none exists, or change its count if one
    does. ``required_count`` is validated positive by the request schema
    before this ever runs; ``0`` is not a value this endpoint accepts at all
    -- see ``DELETE`` for clearing a requirement.
    """
    event = _require_event(session, event_id)
    role = _require_role(session, ministry_role_id)
    requirement = set_staffing_requirement(
        session, actor=actor, event=event, role=role,
        required_count=body.required_count, reason=body.reason,
    )
    # required_count >= 1 is enforced by the request schema, and
    # set_staffing_requirement returns a row (new or existing) for every
    # positive count -- None is only ever its removal outcome.
    assert requirement is not None
    return _role_staffing_response(role, requirement.required_count)


@router.delete(
    "/events/{event_id}/staffing-requirements/{ministry_role_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear this role's requirement for this event",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this event's ministry."},
        404: {"description": "No such event, or no such role."},
    },
)
def delete_staffing_requirement(
    event_id: int = Path(ge=1, description="The event to clear a requirement from."),
    ministry_role_id: int = Path(ge=1, description="The role to no longer require."),
    reason: str | None = Query(
        default=None, description="Optional note for the audit trail."
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> None:
    """**204** whether or not a requirement existed to remove: clearing an
    already-absent requirement is the ordinary no-op the service already
    defines, not a 404 -- the role and event both still exist, only the
    requirement between them does not. Never touches any other event's or
    role's requirement (:func:`~app.services.staffing_requirement.set_staffing_requirement`
    is scoped to exactly this one event/role pair).
    """
    event = _require_event(session, event_id)
    role = _require_role(session, ministry_role_id)
    set_staffing_requirement(
        session, actor=actor, event=event, role=role, required_count=0, reason=reason,
    )


def _require_period(session: Session, scheduling_period_id: int) -> SchedulingPeriod:
    """The scheduling period the URL names, or 404, read on the request Session."""
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


def _require_role(session: Session, ministry_role_id: int) -> MinistryRole:
    """The role the URL names, or 404, read on the request Session."""
    role = session.execute(
        select(MinistryRole).where(MinistryRole.id == ministry_role_id)
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_ROLE_NOT_FOUND
        )
    return role


def _role_staffing_response(
    role: MinistryRole, required_count: int | None
) -> RoleStaffingResponse:
    return RoleStaffingResponse(
        ministry_role_id=role.id,
        name=role.name,
        description=role.description,
        display_order=role.display_order,
        required_count=required_count,
    )


def _period_events_response(
    scheduling_period_id: int,
    events: tuple[EventSummary, ...],
    *,
    ministry_id: int,
    actor: Person,
) -> PeriodEventsResponse:
    return PeriodEventsResponse(
        scheduling_period_id=scheduling_period_id,
        events=[
            EventSummaryResponse(
                event_id=event.event_id,
                event_date=event.event_date,
                event_name=event.event_name,
                event_kind=event.event_kind,
                cancelled_at=event.cancelled_at,
            )
            for event in events
        ],
        can_operate=can_operate_ministry(actor, ministry_id=ministry_id),
    )


def _staffing_response(
    staffing: EventStaffing, *, actor: Person
) -> EventStaffingResponse:
    return EventStaffingResponse(
        event_id=staffing.event_id,
        event_date=staffing.event_date,
        event_name=staffing.event_name,
        event_kind=staffing.event_kind,
        ministry_id=staffing.ministry_id,
        roles=[
            RoleStaffingResponse(
                ministry_role_id=role.ministry_role_id,
                name=role.name,
                description=role.description,
                display_order=role.display_order,
                required_count=role.required_count,
            )
            for role in staffing.roles
        ],
        can_operate=can_operate_ministry(actor, ministry_id=staffing.ministry_id),
    )
