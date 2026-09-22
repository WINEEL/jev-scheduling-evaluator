"""Transport models for serving-limit management (Task 57).

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes``, so an ORM row cannot be serialized by accident.

**``max_assignments`` is ``int | None``, never a stored zero or sentinel.**
Absence of a limit is "no hard maximum" (:mod:`app.services.serving_limit`);
reporting it as ``0`` would invent a second representation of the same fact,
and ``0`` is explicitly *not* a valid limit (the model's own
``max_assignments > 0`` check).

**``PUT`` sets a positive maximum; ``DELETE`` clears it.** The service's
:func:`~app.services.serving_limit.set_serving_limit` collapses both into one
setter where ``max_assignments=None`` means "clear" -- a shape chosen for the
service's own reasons, not one this API repeats. Splitting them into two verbs
keeps ``null`` out of the ``PUT`` body entirely, exactly as Task 54 kept ``0``
out of the staffing-requirement contract and Task 56 kept ``null`` out of the
availability one.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.api.capabilities import CAN_OPERATE_DESCRIPTION

__all__ = [
    "MembershipServingLimitResponse",
    "PeriodServingLimitsResponse",
    "SetServingLimitRequest",
]


class MembershipServingLimitResponse(BaseModel):
    """One membership of the period's ministry, and its serving maximum for
    that period, if any.
    """

    model_config = ConfigDict(extra="forbid")

    ministry_membership_id: int
    person_id: int
    person_display_name: str
    #: ``null`` while the membership is active.
    membership_deactivated_at: datetime.datetime | None = None
    #: ``null`` while the person is active church-wide -- a separate fact from
    #: the membership's own activity.
    person_deactivated_at: datetime.datetime | None = None
    #: ``null`` = no hard maximum; otherwise the positive maximum number of
    #: assignments this membership may hold in this period, in this ministry.
    max_assignments: int | None = None


class PeriodServingLimitsResponse(BaseModel):
    """One scheduling period's whole serving-limit picture, for the head
    managing it.
    """

    model_config = ConfigDict(extra="forbid")

    scheduling_period_id: int
    scheduling_period_name: str
    ministry_id: int
    memberships: list[MembershipServingLimitResponse] = Field(default_factory=list)
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class SetServingLimitRequest(BaseModel):
    """Set (or change) a membership's hard serving maximum for this period.

    ``max_assignments`` must be at least 1: clearing the maximum is expressed
    by ``DELETE``ing it instead, never by a request body carrying a value
    that means "no limit" (:mod:`app.services.serving_limit`).
    """

    model_config = ConfigDict(extra="forbid")

    max_assignments: int = Field(ge=1)
