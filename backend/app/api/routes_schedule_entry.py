"""Entering the scheduling flow: find a period, start its first schedule.

The two endpoints a Ministry Head needs before Tasks 37 and 38 take over:

``GET  /api/v1/ministries/{ministry_id}/scheduling-periods``
``POST /api/v1/scheduling-periods/{scheduling_period_id}/schedule-versions``

Together with the two that already exist, that is the whole first-pass flow --
list the periods, start the first schedule, generate it, review it. There is
deliberately no endpoint here for creating a ministry, editing availability or
staffing, or creating a successor version: this is an entry path into an
already-configured ministry, not general CRUD.

**Both routes stay thin.** Resolve the actor, resolve the URL's resource, call
the service, map the result. Every domain rule -- who may act, whether
availability is locked, whether a first schedule already exists -- lives in
:mod:`app.services.schedule_entry` and, beneath it, Task 20. Neither route
commits: :func:`app.api.dependencies.get_session` is the transaction boundary,
so a successful request commits once on the way out and any failure rolls the
whole thing back.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.schedule_entry_schemas import (
    LatestScheduleSummaryResponse,
    MinistrySchedulingPeriodsResponse,
    SchedulingPeriodSummaryResponse,
    StartedScheduleResponse,
    StartFirstScheduleRequest,
)
from app.models.core import Ministry, Person
from app.models.scheduling_input import SchedulingPeriod
from app.services.authorization import can_operate_ministry
from app.services.schedule_entry import (
    MinistrySchedulingPeriods,
    StartedSchedule,
    list_ministry_scheduling_periods,
    start_first_schedule,
)

__all__ = ["router"]

router = APIRouter(tags=["scheduling entry"])

_MINISTRY_NOT_FOUND = "Ministry not found."
_PERIOD_NOT_FOUND = "Scheduling period not found."


@router.get(
    "/ministries/{ministry_id}/scheduling-periods",
    response_model=MinistrySchedulingPeriodsResponse,
    summary="The scheduling periods of a ministry you manage",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such ministry."},
    },
)
def read_ministry_scheduling_periods(
    ministry_id: int = Path(ge=1, description="The ministry to list periods for."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MinistrySchedulingPeriodsResponse:
    """Which periods exist, and whether each is ready or already scheduled.

    Read-only. For each period it answers three questions a head has before
    anything else: is availability locked yet, has scheduling started, and if
    so which schedule should be opened.

    Only the **newest** version of a started period is reported. A period's
    full history is not part of this answer, and this endpoint offers no way
    to ask for it.
    """
    ministry = _require_ministry(session, ministry_id)
    periods = list_ministry_scheduling_periods(session, actor=actor, ministry=ministry)
    return _periods_response(periods, actor=actor)


@router.post(
    "/scheduling-periods/{scheduling_period_id}/schedule-versions",
    response_model=StartedScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start this period's first schedule",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such scheduling period."},
        409: {
            "description": (
                "Availability is still open, or this period already has a"
                " schedule."
            )
        },
    },
)
def start_period_first_schedule(
    scheduling_period_id: int = Path(
        ge=1, description="The period to start scheduling."
    ),
    body: StartFirstScheduleRequest = Body(
        default_factory=StartFirstScheduleRequest,
        description="Optionally, a note for the head's own reference.",
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> StartedScheduleResponse:
    """Begin scheduling this period, and return where to go next.

    **201**, because it creates something: the period's schedule and its first
    DRAFT. It does not fill it -- generation is a separate, deliberate call to
    ``POST /api/v1/schedule-versions/{id}/generate`` -- and it never submits or
    finalizes anything.

    **This starts a first schedule only.** A period that already has one is
    **409**, not a quietly created second version: replacing an existing
    schedule is a different operation with different rules, and is not part of
    this flow. Availability still being open is **409** for the same reason —
    the domain refuses, and the refusal is reported rather than worked around.
    """
    period = _require_scheduling_period(session, scheduling_period_id)
    started = start_first_schedule(
        session, actor=actor, period=period, notes=body.notes
    )
    return _started_response(started, scheduling_period_id=scheduling_period_id)


def _require_ministry(session: Session, ministry_id: int) -> Ministry:
    """The ministry the URL names, or 404, read on the request Session."""
    ministry = session.execute(
        select(Ministry).where(Ministry.id == ministry_id)
    ).scalar_one_or_none()
    if ministry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_MINISTRY_NOT_FOUND
        )
    return ministry


def _require_scheduling_period(
    session: Session, scheduling_period_id: int
) -> SchedulingPeriod:
    """The period the URL names, or 404.

    404 rather than the domain's 409: a URL naming nothing is a different
    failure from one naming something that cannot be scheduled yet, and no
    amount of locking availability fixes the first.
    """
    period = session.execute(
        select(SchedulingPeriod).where(SchedulingPeriod.id == scheduling_period_id)
    ).scalar_one_or_none()
    if period is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_PERIOD_NOT_FOUND
        )
    return period


def _periods_response(
    periods: MinistrySchedulingPeriods, *, actor: Person
) -> MinistrySchedulingPeriodsResponse:
    """Field by field, so no ORM column can reach the response by accident."""
    return MinistrySchedulingPeriodsResponse(
        ministry_id=periods.ministry.id,
        ministry_name=periods.ministry.name,
        periods=[
            SchedulingPeriodSummaryResponse(
                scheduling_period_id=period.scheduling_period_id,
                name=period.name,
                start_date=period.start_date,
                end_date=period.end_date,
                availability_locked_at=period.availability_locked_at,
                schedule=(
                    None
                    if period.schedule is None
                    else LatestScheduleSummaryResponse(
                        schedule_id=period.schedule.schedule_id,
                        latest_version_id=period.schedule.latest_version_id,
                        latest_version_number=period.schedule.latest_version_number,
                        latest_version_status=period.schedule.latest_version_status,
                    )
                ),
            )
            for period in periods.periods
        ],
        can_operate=can_operate_ministry(
            actor, ministry_id=periods.ministry.id
        ),
    )


def _started_response(
    started: StartedSchedule, *, scheduling_period_id: int
) -> StartedScheduleResponse:
    version = started.version
    return StartedScheduleResponse(
        schedule_id=version.schedule_id,
        schedule_version_id=version.id,
        scheduling_period_id=scheduling_period_id,
        version_number=version.version_number,
        status=version.status,
        requirement_snapshot_count=started.requirement_snapshot_count,
    )
