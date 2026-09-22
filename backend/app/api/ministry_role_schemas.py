"""Transport models for ministry role management (Task 53).

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes``, so an ORM row cannot be serialized by accident.

**Plain vocabulary on purpose.** A response reports ``deactivated_at``
directly, exactly as ``SchedulingPeriodSummaryResponse.availability_locked_at``
already does elsewhere in this API: a caller distinguishes active from
inactive with one ``is None`` check, and no second, redundant boolean field is
invented to say the same thing twice.

``display_order`` is reported for display only -- never solver input, exactly
as :class:`app.models.core.MinistryRole`'s own docstring insists -- and this
API accepts no way to set it: a new role's position is computed server-side
(:mod:`app.services.ministry_role`), and reordering an existing one is
explicitly out of Task 53's scope.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.api.capabilities import CAN_OPERATE_DESCRIPTION

__all__ = [
    "CreateMinistryRoleRequest",
    "MinistryRoleResponse",
    "MinistryRolesResponse",
    "ReasonRequest",
    "UpdateMinistryRoleRequest",
]


class MinistryRoleResponse(BaseModel):
    """One role, active or not."""

    model_config = ConfigDict(extra="forbid")

    ministry_role_id: int
    name: str
    description: str | None = None
    display_order: int
    #: ``null`` while the role is active. Non-null means deactivated, and
    #: since when -- never deleted (module docstring).
    deactivated_at: datetime.datetime | None = None


class MinistryRolesResponse(BaseModel):
    """A ministry's roles, for someone who manages it."""

    model_config = ConfigDict(extra="forbid")

    ministry_id: int
    ministry_name: str
    roles: list[MinistryRoleResponse] = Field(default_factory=list)
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class CreateMinistryRoleRequest(BaseModel):
    """Everything a caller may say when creating a role.

    ``display_order`` and ``deactivated_at`` are deliberately absent: the
    domain decides both (module docstring) -- a caller that could set either
    could create a role in a state no ordinary configuration action produces.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Must not be blank once trimmed.")
    description: str | None = None
    #: Free text for the audit trail. Omit it, or send ``null``; whitespace-only
    #: is rejected, because a blank reason is not a reason.
    reason: str | None = None


class UpdateMinistryRoleRequest(BaseModel):
    """The role's complete new name and description -- a full replace, not a
    per-field patch (:mod:`app.services.ministry_role`'s own reasoning).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, description="Must not be blank once trimmed.")
    description: str | None = None
    reason: str | None = None


class ReasonRequest(BaseModel):
    """The one thing a deactivate/reactivate call may say: an optional reason
    for the audit trail. Everything else about *which* role and *what*
    happens to it comes from the URL and the verb.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
