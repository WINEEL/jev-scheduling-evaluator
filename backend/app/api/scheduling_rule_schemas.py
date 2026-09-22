"""Transport models for a scheduling period's own scheduling rules.

Task 71 put the ministry event-gap rule here; Task 74 added the two generic
rules that stand beside it -- the per-event **member group limit** and the
**same-event support requirement**. One response carries all three, because a
period's rule configuration is one thing a head reads at once.

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes``, so an ORM row cannot be serialized by accident.

**``min_intervening_events`` is ``int | None``, never a stored zero.** Absence
of a rule means consecutive assignments are allowed
(:mod:`app.services.event_gap`); reporting that as ``0`` would invent a second
representation of the same fact, and ``0`` is explicitly *not* a valid value
(the model's own ``min_intervening_events > 0`` check).

**``PUT`` sets a positive gap; ``DELETE`` clears it.** The service's
:func:`~app.services.event_gap.set_min_intervening_events` collapses both into
one setter where ``None`` means "clear" -- a shape chosen for the service's own
reasons, not one this API repeats. Splitting them into two verbs keeps ``null``
out of the ``PUT`` body entirely, exactly as Task 57 kept it out of the
serving-limit contract and Task 56 out of the availability one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.api.capabilities import CAN_OPERATE_DESCRIPTION

__all__ = [
    "MemberGroupLimitEntry",
    "MemberGroupResponse",
    "MemberGroupsResponse",
    "PeriodEventGapRuleResponse",
    "SameEventSupportEntry",
    "SetMemberGroupLimitRequest",
    "SetMinInterveningEventsRequest",
    "SetSameEventSupportRequest",
    "CreateMemberGroupRequest",
]


class MemberGroupLimitEntry(BaseModel):
    """One member group of this period's ministry, and its cap for this period.

    A group with **no** cap appears with ``max_per_event`` of ``null``: that is
    the case a head configuring the rule most needs to see, and dropping it
    would hide the groups they might want to cap. ``null`` is never a stand-in
    for zero -- zero is not a representable cap (:mod:`app.services.member_group`).
    """

    model_config = ConfigDict(extra="forbid")

    member_group_id: int
    name: str
    #: How many memberships are in the group, whatever their activity.
    member_count: int
    #: ``null`` = no cap for this period; otherwise the positive maximum number
    #: of this group's members who may serve any one event.
    max_per_event: int | None = None


class SameEventSupportEntry(BaseModel):
    """One same-event support requirement configured for this period.

    Names the subject and the approved supporting members, because a head
    managing the rule needs to see who it involves. It carries **no reason,
    category or relationship**, because none is stored (requirements §4.7).
    """

    model_config = ConfigDict(extra="forbid")

    subject_membership_id: int
    subject_display_name: str
    #: How many approved supporters must serve the same event. Always positive.
    min_supporters: int
    supporter_membership_ids: list[int]
    supporter_display_names: list[str]


class SetMemberGroupLimitRequest(BaseModel):
    """Set (or change) how many of one group may serve one event.

    ``max_per_event`` must be at least 1: clearing the cap is expressed by
    ``DELETE``ing it, never by a body carrying a value that means "no cap" --
    the same split the event-gap rule and the serving limit already use.
    """

    model_config = ConfigDict(extra="forbid")

    max_per_event: int = Field(ge=1)


class SetSameEventSupportRequest(BaseModel):
    """Record or revise one member's same-event support requirement.

    **The approved set is replaced wholesale**, matching the service: a head
    editing the rule is stating the set as it now stands, and a merge would
    leave somebody approved who was meant to be removed.

    There is deliberately no ``reason`` field. The rule records that a condition
    exists and who satisfies it; why the ministry agreed it is not this system's
    business (requirements §4.7).
    """

    model_config = ConfigDict(extra="forbid")

    #: At least one, and never more than the approved set can supply -- the
    #: service refuses a requirement nobody could satisfy.
    min_supporters: int = Field(default=1, ge=1)
    supporter_membership_ids: list[int] = Field(min_length=1)


class CreateMemberGroupRequest(BaseModel):
    """Define a member group for a ministry.

    A plain name, because that is all a group is: nothing branches on it, and no
    meaning is attached to it anywhere in the application.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)


class MemberGroupResponse(BaseModel):
    """One ministry member group and who is in it."""

    model_config = ConfigDict(extra="forbid")

    member_group_id: int
    ministry_id: int
    name: str
    member_membership_ids: list[int]


class MemberGroupsResponse(BaseModel):
    """A ministry's member groups, in a stable order."""

    model_config = ConfigDict(extra="forbid")

    ministry_id: int
    member_groups: list[MemberGroupResponse]
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class PeriodEventGapRuleResponse(BaseModel):
    """One scheduling period's configured scheduling rules, for its head.

    Named for the rule it carried when it was the only one; the name is kept so
    the published contract does not churn, and the two lists added in Task 74
    default to empty so a period with neither rule reads exactly as it did
    before they existed.
    """

    model_config = ConfigDict(extra="forbid")

    scheduling_period_id: int
    scheduling_period_name: str
    ministry_id: int
    #: Every member group of this ministry, with its cap for this period if it
    #: has one. Empty when the ministry defines no groups at all.
    member_group_limits: list[MemberGroupLimitEntry] = Field(default_factory=list)
    #: Every same-event support requirement configured for this period. Empty
    #: is the ordinary case.
    same_event_support_requirements: list[SameEventSupportEntry] = Field(
        default_factory=list
    )
    #: ``null`` = no rule, and the same person may serve consecutive events;
    #: otherwise the positive number of this ministry's own events that must
    #: fall between two assignments of the same person in this period. Never
    #: ``0``.
    min_intervening_events: int | None = None
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class SetMinInterveningEventsRequest(BaseModel):
    """Set (or change) how many events must be skipped between assignments.

    ``min_intervening_events`` must be at least 1: clearing the rule is
    expressed by ``DELETE``ing it instead, never by a request body carrying a
    value that means "no rule" (:mod:`app.services.event_gap`).
    """

    model_config = ConfigDict(extra="forbid")

    min_intervening_events: int = Field(ge=1)
