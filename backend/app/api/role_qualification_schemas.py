"""Transport models for role-qualification management (Task 55).

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes``, so an ORM row cannot be serialized by accident.

**``is_qualified`` is ``bool | None``, never a stored default.** ``None``
reports "never assessed" (core §8); a client testing truthiness would be
quietly wrong the day someone tried to distinguish "never assessed" from
"explicitly not qualified" -- two different facts this contract keeps apart on
purpose (module docstring, :mod:`app.services.role_qualification`).

**No clear/delete endpoint exists, and none is added here.** Once a decision
exists it is only ever updated to ``True`` or ``False``
(:func:`app.services.role_qualification.set_role_qualification`); the model
itself never deletes a ``RoleQualification`` row, so there is no "revert to
never assessed" operation for this API to expose.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.api.capabilities import CAN_OPERATE_DESCRIPTION

__all__ = [
    "MembershipQualificationResponse",
    "RoleQualificationsResponse",
    "SetRoleQualificationRequest",
]


class MembershipQualificationResponse(BaseModel):
    """One membership of the role's ministry, and its standing decision for
    that role, if any.
    """

    model_config = ConfigDict(extra="forbid")

    ministry_membership_id: int
    person_id: int
    person_display_name: str
    #: `null` while the membership is active.
    membership_deactivated_at: datetime.datetime | None = None
    #: `null` while the person is active church-wide. A separate fact from
    #: the membership's own activity (core §6).
    person_deactivated_at: datetime.datetime | None = None
    #: `null` = never assessed; otherwise the standing decision.
    is_qualified: bool | None = None
    #: `null` exactly when ``is_qualified`` is `null`.
    decided_at: datetime.datetime | None = None


class RoleQualificationsResponse(BaseModel):
    """One role's whole qualification picture, for the head deciding it."""

    model_config = ConfigDict(extra="forbid")

    ministry_role_id: int
    role_name: str
    ministry_id: int
    memberships: list[MembershipQualificationResponse] = Field(default_factory=list)
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class SetRoleQualificationRequest(BaseModel):
    """Record a qualification decision: qualified, or explicitly not."""

    model_config = ConfigDict(extra="forbid")

    is_qualified: bool
    reason: str | None = None
