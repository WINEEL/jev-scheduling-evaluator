"""Transport models for staffing-requirement management (Task 54).

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes``, so an ORM row cannot be serialized by accident.

**``required_count`` is ``int | None``, never a stored zero.** Absence of a
requirement is "not needed here" (scheduling-input §6, §7); reporting it as
``0`` would invent a second representation of the same fact, and a client
testing ``required_count > 0`` to mean "required" would be quietly wrong the
day someone reused ``0`` for something else.

**No way to set ``display_order``.** It is presentation-only server state
(:class:`app.models.core.MinistryRole`'s own docstring), reported here for
context and accepted from no request body.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.api.capabilities import CAN_OPERATE_DESCRIPTION

__all__ = [
    "EventStaffingResponse",
    "EventSummaryResponse",
    "PeriodEventsResponse",
    "RoleStaffingResponse",
    "SetStaffingRequirementRequest",
]


class EventSummaryResponse(BaseModel):
    """One event of a scheduling period -- enough to identify it and choose
    it for staffing. Not a general event read model: creating, editing or
    cancelling an event has no endpoint here (Task 54 §3, "do not redesign").
    """

    model_config = ConfigDict(extra="forbid")

    event_id: int
    event_date: datetime.date
    #: ``null`` for an ordinary Sunday service; set for a SPECIAL event.
    event_name: str | None = None
    event_kind: str
    #: ``null`` unless the event has been cancelled. Shown, not filtered out,
    #: so a head understands why staffing a cancelled event is refused.
    cancelled_at: datetime.datetime | None = None


class PeriodEventsResponse(BaseModel):
    """A scheduling period's events, for someone choosing which one to staff."""

    model_config = ConfigDict(extra="forbid")

    scheduling_period_id: int
    events: list[EventSummaryResponse] = Field(default_factory=list)
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class RoleStaffingResponse(BaseModel):
    """One active role, and this event's current demand for it, if any."""

    model_config = ConfigDict(extra="forbid")

    ministry_role_id: int
    name: str
    description: str | None = None
    display_order: int
    #: ``null`` means this role is not required for this event.
    required_count: int | None = None


class EventStaffingResponse(BaseModel):
    """One event's staffing picture: itself, and every active role's demand."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    event_date: datetime.date
    event_name: str | None = None
    event_kind: str
    ministry_id: int
    roles: list[RoleStaffingResponse] = Field(default_factory=list)
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class SetStaffingRequirementRequest(BaseModel):
    """Set (or create) how many people this role needs at this event.

    ``required_count`` must be at least 1: setting a role's demand to zero is
    expressed by ``DELETE``ing the requirement instead, never by a request
    body carrying the number that means "remove" (:mod:`app.services.staffing_requirement`).
    """

    model_config = ConfigDict(extra="forbid")

    required_count: int = Field(ge=1)
    reason: str | None = None
