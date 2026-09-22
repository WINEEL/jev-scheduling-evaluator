"""Role-qualification management endpoints (Task 55).

``GET /api/v1/ministry-roles/{ministry_role_id}/qualifications``
``PUT /api/v1/ministry-roles/{ministry_role_id}/qualifications/{ministry_membership_id}``

The service behind these, :mod:`app.services.role_qualification`, already
existed and was already tested (Tasks 13-14). This module is the HTTP surface
Task 51's inspection found missing -- nothing about the domain rule changes
here.

**Every route stays thin.** Resolve the actor, resolve the URL's resources,
call the service, map the result. Every domain rule -- who may act, whether
membership and role agree on ministry, whether a new grant needs an active
target -- lives in the service.

**There is deliberately no ``DELETE``.** Unlike a staffing requirement (Task
54), a ``RoleQualification`` row is never deleted once created -- the model's
own docstring is explicit that revoking is an UPDATE, so the row keeps a
stable id for the audit history. This API therefore offers exactly one
mutation, ``PUT`` with a boolean, and no way to return a decision to "never
assessed".

**Cross-ministry management is refused by authorization and by the service's
own integrity check, not by URL shape.** The mutation route takes a role id
and a membership id with no ministry id to keep in agreement --
:func:`~app.services.authorization.require_ministry_operator`, scoped to
``membership.ministry_id``, makes a Head of a *different* ministry's request
403, and :func:`~app.services.role_qualification.set_role_qualification`'s
own ``_require_same_ministry`` turns a role/membership pair from two different
ministries into a 409.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.role_qualification_schemas import (
    MembershipQualificationResponse,
    RoleQualificationsResponse,
    SetRoleQualificationRequest,
)
from app.models.core import MinistryMembership, MinistryRole, Person
from app.services.authorization import can_operate_ministry
from app.services.role_qualification import (
    RoleQualifications,
    list_role_qualifications,
    set_role_qualification,
)

__all__ = ["router"]

router = APIRouter(tags=["role qualifications"])

_ROLE_NOT_FOUND = "Ministry role not found."
_MEMBERSHIP_NOT_FOUND = "Ministry membership not found."


@router.get(
    "/ministry-roles/{ministry_role_id}/qualifications",
    response_model=RoleQualificationsResponse,
    summary="A role's ministry, with each membership's qualification state",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this role's ministry."},
        404: {"description": "No such role."},
    },
)
def read_role_qualifications(
    ministry_role_id: int = Path(ge=1, description="The role to show qualifications for."),
    include_inactive: bool = Query(
        default=False,
        description="Also return deactivated memberships and deactivated people.",
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> RoleQualificationsResponse:
    """Read-only. Every membership of this role's ministry -- active ones by
    default -- each with its qualification state for this role: `null` for
    never assessed, otherwise the standing decision.
    """
    role = _require_role(session, ministry_role_id)
    qualifications = list_role_qualifications(
        session, actor=actor, role=role, include_inactive=include_inactive,
    )
    return _qualifications_response(qualifications, actor=actor)


@router.put(
    "/ministry-roles/{ministry_role_id}/qualifications/{ministry_membership_id}",
    response_model=MembershipQualificationResponse,
    summary="Record a qualification decision for one membership and this role",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this role's ministry."},
        404: {"description": "No such role, or no such membership."},
        409: {
            "description": (
                "The role and membership belong to different ministries, or"
                " granting/renewing standing would target a deactivated"
                " membership, person, or role."
            )
        },
    },
)
def put_role_qualification(
    ministry_role_id: int = Path(ge=1, description="The role being decided."),
    ministry_membership_id: int = Path(ge=1, description="The membership being decided."),
    body: SetRoleQualificationRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MembershipQualificationResponse:
    """Approve (``is_qualified=true``) or explicitly not approve
    (``is_qualified=false``) this membership for this role.

    Idempotent for a repeated decision that changes nothing (the service's
    own rule); a new or renewed ``true`` still requires the membership, its
    person, and the role to all be active, while turning an existing ``true``
    to ``false`` is permitted as state repair even against one that is not.
    """
    role = _require_role(session, ministry_role_id)
    membership = _require_membership(session, ministry_membership_id)
    qualification = set_role_qualification(
        session, actor=actor, membership=membership, role=role,
        is_qualified=body.is_qualified, reason=body.reason,
    )
    return _membership_qualification_response(membership, qualification)


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


def _membership_qualification_response(
    membership: MinistryMembership, qualification
) -> MembershipQualificationResponse:
    return MembershipQualificationResponse(
        ministry_membership_id=membership.id,
        person_id=membership.person_id,
        person_display_name=membership.person.display_name,
        membership_deactivated_at=membership.deactivated_at,
        person_deactivated_at=membership.person.deactivated_at,
        is_qualified=qualification.is_qualified,
        decided_at=qualification.decided_at,
    )


def _qualifications_response(
    qualifications: RoleQualifications, *, actor: Person
) -> RoleQualificationsResponse:
    return RoleQualificationsResponse(
        ministry_role_id=qualifications.ministry_role_id,
        role_name=qualifications.role_name,
        ministry_id=qualifications.ministry_id,
        memberships=[
            MembershipQualificationResponse(
                ministry_membership_id=m.ministry_membership_id,
                person_id=m.person_id,
                person_display_name=m.person_display_name,
                membership_deactivated_at=m.membership_deactivated_at,
                person_deactivated_at=m.person_deactivated_at,
                is_qualified=m.is_qualified,
                decided_at=m.decided_at,
            )
            for m in qualifications.memberships
        ],
        can_operate=can_operate_ministry(
            actor, ministry_id=qualifications.ministry_id
        ),
    )
