"""The Admin's church-wide ministry list (Task 79 §4).

``GET /api/v1/ministries``

One endpoint, and it closes a gap the product had been reporting to its own
users: the Admin home screen said, in as many words, that no view listed every
ministry an administrator could manage, because nothing in the API did. This is
that list.

**Admin only, and read-only.** The authorization decision is made inside
:func:`app.services.ministry_directory.list_church_ministries`, not here -- this
module makes no authorization decision of its own, exactly as
:mod:`app.api.routes_people` does not.

- A **Ministry Head** does not get this. The ministries they lead already reach
  them through ``/api/v1/me``'s ``headed_ministries``, which reports their own
  memberships; a church-wide inventory is a different thing and is not theirs.
- A **volunteer** gets 403.

**Being able to see a ministry is not being able to run it.** The list links an
Admin to each ministry's existing screens, and those screens' reads already
admit an Admin. What this endpoint deliberately does not do is hand anybody
operational write authority: Task 79 narrowed the roster writes to the
ministry's own active Head
(:func:`app.services.authorization.require_ministry_operator`), and the task
report names every older endpoint where ``is_admin=True`` alone still grants an
operational write, so the lifecycle task can decide about each on purpose.

**There is no create, edit or archive here.** ``Ministry`` rows are made by the
import scripts today, and the domain carries no reviewed product rule for
creating one through the API -- what happens to its periods, its roster and its
past schedules when it is archived has never been decided. Adding the verb
before the rule exists would be building the dangerous half of the feature
first.

No route commits: :func:`app.api.dependencies.get_session` is the transaction
boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.ministry_schemas import (
    MinistryHeadResponse,
    MinistryListResponse,
    MinistryOverviewResponse,
    MinistryPeriodResponse,
)
from app.models.core import Person
from app.services.ministry_directory import (
    MinistryOverview,
    list_church_ministries,
)

__all__ = ["router"]

router = APIRouter(tags=["ministries"])


@router.get(
    "/ministries",
    response_model=MinistryListResponse,
    summary="Every ministry in the church, for an administrator",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor is not an active Admin."},
    },
)
def read_ministries(
    include_inactive: bool = Query(
        default=True,
        description=(
            "Include archived ministries. On by default: an archived ministry"
            " is part of what an overseer oversees, and omitting it silently"
            " would look like deletion."
        ),
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MinistryListResponse:
    """Name, head(s), team size, current period and schedule status.

    Scoped to the actor's own church by the service, which takes
    ``church_id`` from the actor and never from the request -- so there is no
    parameter through which one church's Admin could read another's ministries.

    Bounded: five queries whatever the number of ministries, never one per
    ministry (see the service).
    """
    ministries = list_church_ministries(
        session, actor=actor, include_inactive=include_inactive
    )
    return MinistryListResponse(
        ministries=[_overview_response(row) for row in ministries],
        total=len(ministries),
    )


def _overview_response(row: MinistryOverview) -> MinistryOverviewResponse:
    """Field by field from the value object, never serialized from an ORM row."""
    return MinistryOverviewResponse(
        ministry_id=row.ministry_id,
        name=row.name,
        description=row.description,
        deactivated_at=row.deactivated_at,
        heads=[
            MinistryHeadResponse(
                person_id=head.person_id, display_name=head.display_name
            )
            for head in row.heads
        ],
        active_member_count=row.active_member_count,
        period=(
            None
            if row.period is None
            else MinistryPeriodResponse(
                scheduling_period_id=row.period.scheduling_period_id,
                name=row.period.name,
                start_date=row.period.start_date,
                end_date=row.period.end_date,
                is_current=row.period.is_current,
                latest_version_number=row.period.latest_version_number,
                latest_version_status=row.period.latest_version_status,
            )
        ),
    )
