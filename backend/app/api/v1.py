"""The identity endpoints of version 1 of the product API.

Two endpoints, and both answer a question about **the caller themselves**:
``/me`` -- who are you? -- and ``/me/schedule`` -- where are you expected?

That shared shape is why they live together. Neither takes a person, a ministry
or an actor parameter, so neither has an authorization rule of its own: the
subject is the authenticated actor, and the only way to read somebody else's
answer would be to hold their session.

Resource endpoints live in their own modules beside this one (see
:mod:`app.api.routes_schedule_versions`) and are mounted at the same ``/api/v1``
prefix. They take the acting Person from
:func:`app.api.dependencies.get_current_actor`, never from the request.
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.schemas import (
    CurrentActorResponse,
    HeadedMinistry,
    MyScheduleAssignment,
    MyScheduleResponse,
)
from app.models.core import Ministry, MinistryMembership, Person
from app.services.church_calendar import church_today
from app.services.my_schedule import get_my_upcoming_schedule

__all__ = ["router"]

router = APIRouter(tags=["identity"])


@router.get(
    "/me",
    response_model=CurrentActorResponse,
    summary="The signed-in person's identity",
    responses={401: {"description": "No active actor could be established."}},
)
def read_current_actor(
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> CurrentActorResponse:
    """Who the caller is, and which ministries they head.

    The Session here is the same one the actor was resolved on: FastAPI caches
    a dependency per request, so ``get_session`` runs once and both this
    function and :func:`get_current_actor` share it.

    The response is built field by field from the ORM row rather than serialized
    from it, so nothing the row happens to carry -- email, phone, the linked
    account -- can escape by accident.
    """
    return CurrentActorResponse(
        person_id=actor.id,
        display_name=actor.display_name,
        is_admin=actor.is_admin,
        headed_ministries=_headed_ministries(session, person_id=actor.id),
    )


def _headed_ministries_statement(person_id: int) -> Select[tuple[int, str]]:
    """Ministries this Person heads through an active membership.

    Mirrors :func:`app.services.authorization.require_ministry_reader` exactly:
    an **active** membership carrying ``is_ministry_head``. Two deliberate
    omissions keep it honest --

    - **Admin is not folded in.** An Admin may manage every ministry, but this
      field reports membership state, and inventing head rows for them would
      make the response disagree with the database.
    - **The ministry's own ``deactivated_at`` is not filtered.** The
      authorization rule does not consult it either, so filtering here would
      understate what the actor can actually do and make the UI hide a ministry
      the API would still let them manage.

    Ordered by name then id, so a person heading several sees them in a stable
    order rather than whatever the database returns.
    """
    return (
        select(Ministry.id, Ministry.name)
        .join(MinistryMembership, MinistryMembership.ministry_id == Ministry.id)
        .where(
            MinistryMembership.person_id == person_id,
            MinistryMembership.is_ministry_head.is_(True),
            MinistryMembership.deactivated_at.is_(None),
        )
        .order_by(Ministry.name, Ministry.id)
    )


def _headed_ministries(session: Session, *, person_id: int) -> list[HeadedMinistry]:
    rows = session.execute(_headed_ministries_statement(person_id)).all()
    return [HeadedMinistry(ministry_id=row.id, name=row.name) for row in rows]


@router.get(
    "/me/schedule",
    response_model=MyScheduleResponse,
    summary="The signed-in person's own upcoming commitments",
    responses={401: {"description": "No active actor could be established."}},
)
def read_my_schedule(
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MyScheduleResponse:
    """Where this person is expected, from today onward, across every ministry.

    **Every authenticated person may call this, and it needs no permission
    check**, because the subject is not a parameter. There is no
    ``?person_id=``, no path segment and no header that could name somebody
    else: the actor comes from the session, and the service filters on that id
    alone. A volunteer who manages nothing gets their own list; so does an
    Admin.

    What differs by role is not *whose* schedule but *which version speaks* --
    a volunteer sees only FINALIZED schedules, a ministry head additionally sees
    their own ministry's working draft. That rule lives in
    :mod:`app.services.my_schedule`, next to the query it constrains.
    """
    today = _today_for(session, actor=actor)
    assignments = get_my_upcoming_schedule(session, actor=actor, on_or_after=today)

    return MyScheduleResponse(
        person_id=actor.id,
        display_name=actor.display_name,
        as_of_date=today,
        assignments=[
            MyScheduleAssignment(
                assignment_id=item.assignment_id,
                event_id=item.event_id,
                event_date=item.event_date,
                event_kind=item.event_kind,
                event_name=item.event_name,
                ministry_id=item.ministry_id,
                ministry_name=item.ministry_name,
                ministry_role_id=item.ministry_role_id,
                ministry_role_name=item.ministry_role_name,
                is_confirmed=item.is_confirmed,
                schedule_version_status=item.schedule_version_status,
            )
            for item in assignments
        ],
    )


def _today_for(session: Session, *, actor: Person) -> datetime.date:
    """Today's date in the church's own timezone.

    **Not UTC**, and no longer written here: the rule and its UTC fallback live
    in :func:`app.services.church_calendar.church_today`, because Task 79's
    serving-history counts need the very same boundary to decide what is
    *past*. Two definitions of "today" would let an event be neither upcoming
    nor already served.
    """
    return church_today(session, church_id=actor.church_id)
