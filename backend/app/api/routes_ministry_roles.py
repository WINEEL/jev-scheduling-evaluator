"""Ministry role management endpoints (Task 53).

``GET   /api/v1/ministries/{ministry_id}/roles``
``POST  /api/v1/ministries/{ministry_id}/roles``
``PATCH /api/v1/ministry-roles/{ministry_role_id}``
``POST  /api/v1/ministry-roles/{ministry_role_id}/deactivate``
``POST  /api/v1/ministry-roles/{ministry_role_id}/reactivate``

The first head-facing management surface over a ministry's own configuration
-- everything before this task exists only in the service layer with no HTTP
path at all (Task 51's inspection).

**Every route stays thin.** Resolve the actor, resolve the URL's resource,
call the service, map the result. Every domain rule -- who may act, whether a
name collides, whether a ministry accepts new roles -- lives in
:mod:`app.services.ministry_role`. No route commits: :func:`get_session` is
the transaction boundary, so a successful request commits once on the way out
and any failure rolls the whole thing back.

**Cross-ministry management is refused by authorization, not by URL shape.**
The single-role endpoints take only a role id -- there is no
``/ministries/{ministry_id}/roles/{role_id}`` nesting to keep two ids in
agreement, because the service's own
:func:`~app.services.authorization.require_ministry_operator` check, scoped to
``role.ministry_id``, already makes a Head of a *different* ministry's request
403 regardless of which role id they name.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.ministry_role_schemas import (
    CreateMinistryRoleRequest,
    MinistryRoleResponse,
    MinistryRolesResponse,
    ReasonRequest,
    UpdateMinistryRoleRequest,
)
from app.models.core import Ministry, MinistryRole, Person
from app.services.authorization import can_operate_ministry
from app.services.ministry_role import (
    create_ministry_role,
    deactivate_ministry_role,
    list_ministry_roles,
    reactivate_ministry_role,
    update_ministry_role,
)

__all__ = ["router"]

router = APIRouter(tags=["ministry roles"])

_MINISTRY_NOT_FOUND = "Ministry not found."
_ROLE_NOT_FOUND = "Ministry role not found."


@router.get(
    "/ministries/{ministry_id}/roles",
    response_model=MinistryRolesResponse,
    summary="The roles of a ministry you manage",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such ministry."},
    },
)
def read_ministry_roles(
    ministry_id: int = Path(ge=1, description="The ministry to list roles for."),
    include_inactive: bool = Query(
        default=False, description="Also return deactivated roles."
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MinistryRolesResponse:
    """Read-only. Active roles only unless ``include_inactive=true``."""
    ministry = _require_ministry(session, ministry_id)
    roles = list_ministry_roles(
        session, actor=actor, ministry=ministry, include_inactive=include_inactive
    )
    return _roles_response(ministry, roles, actor=actor)


@router.post(
    "/ministries/{ministry_id}/roles",
    response_model=MinistryRoleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a role for a ministry you manage",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such ministry."},
        409: {
            "description": (
                "The ministry is deactivated, or a role with this name"
                " already exists in it."
            )
        },
    },
)
def create_role(
    ministry_id: int = Path(ge=1, description="The ministry to create a role for."),
    body: CreateMinistryRoleRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MinistryRoleResponse:
    """**201.** The new role's ``display_order`` is computed by the service,
    never accepted from the caller.
    """
    ministry = _require_ministry(session, ministry_id)
    role = create_ministry_role(
        session, actor=actor, ministry=ministry, name=body.name,
        description=body.description, reason=body.reason,
    )
    return _role_response(role)


@router.patch(
    "/ministry-roles/{ministry_role_id}",
    response_model=MinistryRoleResponse,
    summary="Rename or re-describe a role",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this role's ministry."},
        404: {"description": "No such role."},
        409: {"description": "Another role in this ministry already has this name."},
    },
)
def update_role(
    ministry_role_id: int = Path(ge=1, description="The role to edit."),
    body: UpdateMinistryRoleRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MinistryRoleResponse:
    """A full replace of name and description, active or deactivated role
    alike -- editing is not the same act as approving new work against it
    (:mod:`app.services.ministry_role`).
    """
    role = _require_ministry_role(session, ministry_role_id)
    role = update_ministry_role(
        session, actor=actor, role=role, name=body.name,
        description=body.description, reason=body.reason,
    )
    return _role_response(role)


@router.post(
    "/ministry-roles/{ministry_role_id}/deactivate",
    response_model=MinistryRoleResponse,
    summary="Deactivate a role, without deleting it",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this role's ministry."},
        404: {"description": "No such role."},
    },
)
def deactivate_role(
    ministry_role_id: int = Path(ge=1, description="The role to deactivate."),
    body: ReasonRequest = Body(default_factory=ReasonRequest),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MinistryRoleResponse:
    """Idempotent: deactivating an already-deactivated role changes nothing
    and returns it unchanged. Every historical staffing requirement,
    qualification and assignment naming this role remains exactly as it was.
    """
    role = _require_ministry_role(session, ministry_role_id)
    role = deactivate_ministry_role(session, actor=actor, role=role, reason=body.reason)
    return _role_response(role)


@router.post(
    "/ministry-roles/{ministry_role_id}/reactivate",
    response_model=MinistryRoleResponse,
    summary="Reactivate a previously deactivated role",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this role's ministry."},
        404: {"description": "No such role."},
    },
)
def reactivate_role(
    ministry_role_id: int = Path(ge=1, description="The role to reactivate."),
    body: ReasonRequest = Body(default_factory=ReasonRequest),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MinistryRoleResponse:
    """Idempotent: reactivating an already-active role changes nothing and
    returns it unchanged. Can never collide with its own name
    (:mod:`app.services.ministry_role`).
    """
    role = _require_ministry_role(session, ministry_role_id)
    role = reactivate_ministry_role(session, actor=actor, role=role, reason=body.reason)
    return _role_response(role)


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


def _require_ministry_role(session: Session, ministry_role_id: int) -> MinistryRole:
    """The role the URL names, or 404, read on the request Session."""
    role = session.execute(
        select(MinistryRole).where(MinistryRole.id == ministry_role_id)
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_ROLE_NOT_FOUND
        )
    return role


def _role_response(role: MinistryRole) -> MinistryRoleResponse:
    """Field by field, so no ORM column can reach the response by accident."""
    return MinistryRoleResponse(
        ministry_role_id=role.id,
        name=role.name,
        description=role.description,
        display_order=role.display_order,
        deactivated_at=role.deactivated_at,
    )


def _roles_response(
    ministry: Ministry, roles: tuple[MinistryRole, ...], *, actor: Person
) -> MinistryRolesResponse:
    return MinistryRolesResponse(
        ministry_id=ministry.id,
        ministry_name=ministry.name,
        roles=[_role_response(role) for role in roles],
        can_operate=can_operate_ministry(actor, ministry_id=ministry.id),
    )
