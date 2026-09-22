"""Serving-limit management endpoints (Task 57).

``GET    /api/v1/scheduling-periods/{scheduling_period_id}/serving-limits``
``PUT    /api/v1/scheduling-periods/{scheduling_period_id}/serving-limits/{ministry_membership_id}``
``DELETE /api/v1/scheduling-periods/{scheduling_period_id}/serving-limits/{ministry_membership_id}``

The mutation service behind these, :mod:`app.services.serving_limit`, already
existed and was already tested (Task 47), including its solver, manual-
assignment and finalization enforcement. This module is the HTTP surface Task
51's inspection found missing -- nothing about the domain rule, the hard-and-
non-overridable nature of the limit, or the no-carry-forward behaviour changes
here. The read/list side (:func:`~app.services.serving_limit.list_serving_limits`)
is new for exactly this UI and is read-only.

**Every route stays thin.** Resolve the actor, resolve the URL's resources,
call the service, map the result. Every domain rule -- who may act, whether
membership and period agree on ministry, what a deactivated target means, that
zero is not a limit -- lives in the service or its own request schema. No
route commits: :func:`get_session` is the transaction boundary.

**``PUT`` sets a positive maximum; ``DELETE`` clears it.**
:func:`~app.services.serving_limit.set_serving_limit` collapses both into one
setter where ``max_assignments=None`` means "clear" -- a shape chosen for the
service's own reasons, not one this API repeats. Splitting them into two verbs
keeps ``null`` out of the ``PUT`` body entirely, and Pydantic's own ``ge=1``
refuses a non-positive maximum before the service is even reached.

**Cross-ministry management is refused by authorization and by the service's
own integrity check, not by URL shape.** The mutation routes take a period id
and a membership id with no ministry id to keep in agreement --
:func:`~app.services.authorization.require_ministry_operator`, scoped to
``scheduling_period.ministry_id``, makes a Head of a *different* ministry's
request 403, and :func:`set_serving_limit`'s own ``_require_same_ministry``
turns a period/membership pair from two different ministries into a 409.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.serving_limit_schemas import (
    MembershipServingLimitResponse,
    PeriodServingLimitsResponse,
    SetServingLimitRequest,
)
from app.models.core import MinistryMembership, Person
from app.models.scheduling_input import SchedulingPeriod
from app.services.authorization import can_operate_ministry
from app.services.serving_limit import (
    PeriodServingLimits,
    list_serving_limits,
    set_serving_limit,
)

__all__ = ["router"]

router = APIRouter(tags=["serving limits"])

_PERIOD_NOT_FOUND = "Scheduling period not found."
_MEMBERSHIP_NOT_FOUND = "Ministry membership not found."


@router.get(
    "/scheduling-periods/{scheduling_period_id}/serving-limits",
    response_model=PeriodServingLimitsResponse,
    summary="A scheduling period's ministry members and their serving maximums",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period."},
    },
)
def read_serving_limits(
    scheduling_period_id: int = Path(ge=1, description="The period to show serving limits for."),
    include_inactive: bool = Query(
        default=False,
        description="Also return deactivated memberships and deactivated people.",
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PeriodServingLimitsResponse:
    """Read-only. Every membership of this period's ministry -- active ones by
    default -- each with its hard serving maximum for this period:
    ``max_assignments`` is ``null`` when no maximum is recorded, otherwise the
    positive number.
    """
    period = _require_period(session, scheduling_period_id)
    limits = list_serving_limits(
        session, actor=actor, scheduling_period=period, include_inactive=include_inactive,
    )
    return _serving_limits_response(limits, actor=actor)


@router.put(
    "/scheduling-periods/{scheduling_period_id}/serving-limits/{ministry_membership_id}",
    response_model=MembershipServingLimitResponse,
    summary="Set this member's hard serving maximum for this period",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period, or no such membership."},
        409: {
            "description": (
                "The period and membership belong to different ministries, or"
                " the maximum would be recorded against a deactivated"
                " membership or person."
            )
        },
    },
)
def put_serving_limit(
    scheduling_period_id: int = Path(ge=1, description="The period the maximum applies to."),
    ministry_membership_id: int = Path(ge=1, description="The membership being limited."),
    body: SetServingLimitRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MembershipServingLimitResponse:
    """Record the maximum if none exists, or change it if one does.
    ``max_assignments`` is validated positive by the request schema before
    this ever runs; ``0`` and ``null`` are not values this endpoint accepts --
    see ``DELETE`` for clearing the maximum.

    Idempotent for a repeated maximum that changes nothing (the service's own
    rule). Lowering a maximum below what an existing draft already contains is
    permitted here and deliberately so -- nothing deletes an assignment;
    :mod:`app.services.finalization_readiness` reports the version as
    unfinalizable until a head repairs it.
    """
    period = _require_period(session, scheduling_period_id)
    membership = _require_membership(session, ministry_membership_id)
    set_serving_limit(
        session, actor=actor, membership=membership, scheduling_period=period,
        max_assignments=body.max_assignments,
    )
    return _membership_serving_limit_response(membership, body.max_assignments)


@router.delete(
    "/scheduling-periods/{scheduling_period_id}/serving-limits/{ministry_membership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear this member's serving maximum for this period",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period, or no such membership."},
        409: {
            "description": (
                "The period and membership belong to different ministries."
            )
        },
    },
)
def delete_serving_limit(
    scheduling_period_id: int = Path(ge=1, description="The period to clear a maximum from."),
    ministry_membership_id: int = Path(ge=1, description="The membership to no longer limit."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> None:
    """**204** whether or not a maximum existed to remove: clearing an
    already-absent maximum is the ordinary no-op the service already defines,
    not a 404 -- the period and membership both still exist, only the limit
    between them does not. Clearing is exempt from the active-target rule, so
    a departed member's stray maximum can always be tidied up.
    """
    period = _require_period(session, scheduling_period_id)
    membership = _require_membership(session, ministry_membership_id)
    set_serving_limit(
        session, actor=actor, membership=membership, scheduling_period=period,
        max_assignments=None,
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


def _membership_serving_limit_response(
    membership: MinistryMembership, max_assignments: int | None
) -> MembershipServingLimitResponse:
    return MembershipServingLimitResponse(
        ministry_membership_id=membership.id,
        person_id=membership.person_id,
        person_display_name=membership.person.display_name,
        membership_deactivated_at=membership.deactivated_at,
        person_deactivated_at=membership.person.deactivated_at,
        max_assignments=max_assignments,
    )


def _serving_limits_response(
    limits: PeriodServingLimits, *, actor: Person
) -> PeriodServingLimitsResponse:
    return PeriodServingLimitsResponse(
        scheduling_period_id=limits.scheduling_period_id,
        scheduling_period_name=limits.scheduling_period_name,
        ministry_id=limits.ministry_id,
        memberships=[
            MembershipServingLimitResponse(
                ministry_membership_id=m.ministry_membership_id,
                person_id=m.person_id,
                person_display_name=m.person_display_name,
                membership_deactivated_at=m.membership_deactivated_at,
                person_deactivated_at=m.person_deactivated_at,
                max_assignments=m.max_assignments,
            )
            for m in limits.memberships
        ],
        can_operate=can_operate_ministry(actor, ministry_id=limits.ministry_id),
    )
