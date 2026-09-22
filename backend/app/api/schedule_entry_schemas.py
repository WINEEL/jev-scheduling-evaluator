"""Transport models for entering the scheduling flow.

The two shapes a Ministry Head needs before the schedule itself exists: the
list of periods they could schedule, and the confirmation that one has been
started.

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes``, so an ORM row cannot be serialized by accident.

**Plain vocabulary on purpose.** "Start the first schedule" is what a head is
doing; ``ScheduleVersion`` is how it is stored. Nothing in this contract asks
the caller to understand successor versions, carry-forward, or which version is
authoritative -- none of which is part of this flow.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.api.capabilities import CAN_OPERATE_DESCRIPTION

__all__ = [
    "LatestScheduleSummaryResponse",
    "MinistrySchedulingPeriodsResponse",
    "SchedulingPeriodSummaryResponse",
    "StartFirstScheduleRequest",
    "StartedScheduleResponse",
]


class LatestScheduleSummaryResponse(BaseModel):
    """How far this period's scheduling has got.

    Only the newest version, never a history: enough to decide whether to
    offer "start scheduling" or "open the schedule", and which one to open.
    """

    model_config = ConfigDict(extra="forbid")

    schedule_id: int
    #: The version to open -- the highest-numbered one, whatever its status.
    latest_version_id: int | None = None
    latest_version_number: int | None = None
    #: ``DRAFT``, ``REVIEW`` or ``FINALIZED``, exactly as stored.
    latest_version_status: str | None = None


class SchedulingPeriodSummaryResponse(BaseModel):
    """One scheduling period this ministry has configured."""

    model_config = ConfigDict(extra="forbid")

    scheduling_period_id: int
    name: str
    start_date: datetime.date
    end_date: datetime.date
    #: ``null`` while availability is still open. A schedule cannot be started
    #: until it is set.
    availability_locked_at: datetime.datetime | None = None
    #: ``null`` when scheduling has not been started for this period yet.
    schedule: LatestScheduleSummaryResponse | None = None


class MinistrySchedulingPeriodsResponse(BaseModel):
    """A ministry's scheduling periods, for someone who manages it."""

    model_config = ConfigDict(extra="forbid")

    ministry_id: int
    ministry_name: str
    periods: list[SchedulingPeriodSummaryResponse] = Field(default_factory=list)
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class StartFirstScheduleRequest(BaseModel):
    """Everything a caller may say when starting a schedule: an optional note.

    Deliberately tiny. The acting person comes from the authenticated request,
    the ministry and period come from the URL, and the version number, status
    and requirement snapshot are the domain's to decide -- a caller that could
    name any of them could create a schedule that never went through the rules
    that make one meaningful.
    """

    model_config = ConfigDict(extra="forbid")

    #: Free text for the head's own reference. Omit it, or send ``null``;
    #: whitespace-only is rejected, because a blank note is not a note.
    notes: str | None = None


class StartedScheduleResponse(BaseModel):
    """Confirmation that scheduling has begun, and where to go next.

    Navigation-sized on purpose: the caller follows up with
    ``GET /api/v1/schedule-versions/{schedule_version_id}`` for the contents.
    """

    model_config = ConfigDict(extra="forbid")

    schedule_id: int
    schedule_version_id: int
    scheduling_period_id: int
    #: Always ``1``: this endpoint starts a period's first schedule and
    #: nothing else.
    version_number: int
    #: Always ``DRAFT``. Starting a schedule never submits or finalizes it.
    status: str
    #: How many required positions the new schedule froze in place. ``0`` means
    #: the period has no staffing requirements configured yet.
    requirement_snapshot_count: int
