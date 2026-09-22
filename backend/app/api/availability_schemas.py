"""Transport models for availability management (Task 56).

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes``, so an ORM row cannot be serialized by accident.

**``availability_state`` is ``str | None``, never a fourth stored value.**
``null`` reports "no response" (scheduling-input §8); the three explicit
values are exactly ``app.models.scheduling_input``'s
``AVAILABILITY_AVAILABLE`` / ``AVAILABILITY_BACKUP`` / ``AVAILABILITY_UNAVAILABLE``
strings, echoed as-is rather than translated into a client-side enum this
layer would have to keep in sync by hand.

**No way to set the ``NO_RESPONSE`` string.** It is not a value this API
accepts -- clearing a response is a separate operation (``DELETE``), never a
``PUT`` carrying a sentinel, mirroring exactly how Task 54 kept ``0`` out of
the staffing-requirement wire contract.
"""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.capabilities import CAN_OPERATE_DESCRIPTION

__all__ = [
    "AvailabilityLockResponse",
    "EventAvailabilityResponse",
    "LockAvailabilityRequest",
    "MembershipAvailabilityResponse",
    "SetAvailabilityRequest",
]

#: The exact strings ``app.models.scheduling_input`` stores
#: (``AVAILABILITY_AVAILABLE`` / ``_BACKUP`` / ``_UNAVAILABLE``), spelled out
#: as a ``Literal`` rather than imported: Pydantic turns this into both a
#: 422 on any other value and a proper enum in the generated OpenAPI schema,
#: which a plain tuple check would not.
_SettableState = Literal["AVAILABLE", "BACKUP", "UNAVAILABLE"]


class MembershipAvailabilityResponse(BaseModel):
    """One membership of the event's ministry, and its stored answer for
    this event, if any.
    """

    model_config = ConfigDict(extra="forbid")

    ministry_membership_id: int
    person_id: int
    person_display_name: str
    #: `null` while the membership is active.
    membership_deactivated_at: datetime.datetime | None = None
    #: `null` while the person is active church-wide -- a separate fact from
    #: the membership's own activity (core §6).
    person_deactivated_at: datetime.datetime | None = None
    #: `null` = no response; otherwise ``"AVAILABLE"``, ``"BACKUP"``, or
    #: ``"UNAVAILABLE"``.
    availability_state: str | None = None


class EventAvailabilityResponse(BaseModel):
    """One event's whole availability picture, for the head recording it."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    event_date: datetime.date
    event_name: str | None = None
    event_kind: str
    ministry_id: int
    #: `null` while availability is still open for this event's period. Once
    #: set, every mutation below is refused (`409`).
    availability_locked_at: datetime.datetime | None = None
    memberships: list[MembershipAvailabilityResponse] = Field(default_factory=list)
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class SetAvailabilityRequest(BaseModel):
    """Record an explicit answer: available, backup, or unavailable.

    To clear a response back to "no response", call ``DELETE`` instead --
    there is no sentinel value for that here.
    """

    model_config = ConfigDict(extra="forbid")

    availability_state: _SettableState
    reason: str | None = None


class LockAvailabilityRequest(BaseModel):
    """Close availability collection for one scheduling period.

    ``reason`` is the only field, and it is optional -- the same shape every
    other mutation here uses. There is deliberately no ``locked`` boolean: the
    domain has no unlock, so a flag would offer a value the service cannot
    honour.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None


class AvailabilityLockResponse(BaseModel):
    """A period's lock state after the call.

    Carries the period's identity as well as the timestamp, so a client that
    holds several periods can update the right one without matching on
    anything it had to remember.
    """

    model_config = ConfigDict(extra="forbid")

    scheduling_period_id: int
    scheduling_period_name: str
    ministry_id: int
    #: Never ``null`` in this response: the endpoint either locked the period
    #: or found it already locked, and both outcomes have an instant.
    availability_locked_at: datetime.datetime
