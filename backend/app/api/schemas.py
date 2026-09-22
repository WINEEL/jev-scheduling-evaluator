"""Transport models: what the API sends, defined apart from the ORM.

Deliberately hand-built rather than serialized from SQLAlchemy objects. An ORM
row carries everything the database knows -- ``person.email``, the audit
timestamps, and, one relationship away, the linked ``user_account`` and its
Google subject. A DTO that starts from the row and removes fields leaks the next
column somebody adds; one that names its fields explicitly cannot.

None of these models sets ``from_attributes``, so passing an ORM object where a
schema is expected is an error rather than an accidental disclosure.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AssignmentLoad",
    "MyScheduleAssignment",
    "MyScheduleResponse",
    "CreatedAssignment",
    "CurrentActorResponse",
    "GenerateDraftScheduleRequest",
    "GenerateDraftScheduleResponse",
    "GenerationMetrics",
    "HeadedMinistry",
    "UnfilledRequirementReport",
]


class HeadedMinistry(BaseModel):
    """One ministry the actor currently heads."""

    model_config = ConfigDict(extra="forbid")

    ministry_id: int
    name: str


class CurrentActorResponse(BaseModel):
    """Who the caller is, as the frontend needs to know it.

    Enough to render a header and decide which navigation to show, and nothing
    more: no email, no phone, no linked account, no audit fields.
    """

    model_config = ConfigDict(extra="forbid")

    person_id: int
    display_name: str
    #: The global application-role tier: an Admin may act in any ministry.
    is_admin: bool
    #: Ministries this Person actually heads, through an **active** membership
    #: carrying head authority. This is a statement about membership state, not
    #: about authority: an Admin may manage every ministry and will still have
    #: an empty list here unless they genuinely head one. A normal volunteer's
    #: list is legitimately empty.
    headed_ministries: list[HeadedMinistry] = Field(default_factory=list)


class MyScheduleAssignment(BaseModel):
    """One commitment on the signed-in person's own schedule (Task 77).

    Carries the ministry and role **by name** as well as by id, unlike the
    management DTOs. Those are read inside a ministry whose names the reader
    already knows; this one may be read by a volunteer who belongs to no team
    and has no screen on which to look either up.

    ``is_confirmed`` is the field that changes what this *means*. It is true
    only for a FINALIZED schedule -- one a ministry head has stood behind. A
    manager previewing their own draft sees false, and the UI must say so:
    a proposal shown as a commitment is how somebody ends up turning up on the
    wrong Sunday, or not turning up on the right one.

    No person id, no membership id, no schedule or version id. This is somebody
    reading their own list; none of those help them and each is a handle onto
    internal structure that need not be published.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: int
    event_id: int
    event_date: datetime.date
    #: e.g. SUNDAY or SPECIAL -- what kind of gathering this is.
    event_kind: str
    #: Only special events are named; a Sunday service is identified by its date.
    event_name: str | None = None
    ministry_id: int
    ministry_name: str
    ministry_role_id: int
    ministry_role_name: str
    #: True only for a FINALIZED schedule. See above.
    is_confirmed: bool
    #: DRAFT, REVIEW or FINALIZED -- so the UI can be specific rather than
    #: merely saying "not confirmed".
    schedule_version_status: str


class MyScheduleResponse(BaseModel):
    """The signed-in person's upcoming commitments, across every ministry.

    **Aggregated through the canonical Person**, not through a membership, so
    somebody who serves in two ministries gets one list rather than two halves
    (ADR 0001).

    ``as_of_date`` is the day the list was computed for, in the church's own
    timezone. Returned rather than assumed so a reader in another timezone --
    or a test -- can see which day "upcoming" was measured from.
    """

    model_config = ConfigDict(extra="forbid")

    person_id: int
    display_name: str
    as_of_date: datetime.date
    assignments: list[MyScheduleAssignment] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Draft schedule generation
# --------------------------------------------------------------------------


class GenerateDraftScheduleRequest(BaseModel):
    """The scheduling preferences to run this generation under.

    Mirrors :class:`app.scheduling.solver.SchedulingPolicy` field for field,
    and holds nothing else. It is a transport shape for an existing domain
    value, not a second place where scheduling behaviour is decided -- there
    are no ministry-specific constants here, and adding one would mean the
    policy meant different things depending on which layer read it.

    **Every field is optional, and the empty body ``{}`` is a valid request**
    producing the default policy: fill as many positions as possible, and
    distribute the work evenly among the people who could take it. The
    defaults are :class:`SchedulingPolicy`'s own.

    This is temporary in one respect: a policy belongs to a Ministry and should
    eventually be configured and stored rather than sent with each request.
    Until that exists, the caller supplies it per request.
    """

    model_config = ConfigDict(extra="forbid")

    #: Allow candidates who never answered to be scheduled. Off by default:
    #: silence is not consent.
    allow_no_response: bool = False
    #: The per-person assignment count to aim for. ``None`` means no target
    #: is optimized. It is a goal, never a cap, and it does not control
    #: whether workload is balanced -- see ``balance_candidate_loads``.
    target_assignments_per_candidate: int | None = None
    #: Spread work evenly across candidates. **On by default**, so a client
    #: that omits it -- including every client written before this field
    #: existed -- gets a sensibly distributed schedule rather than one that
    #: may pile every service onto one person. Independent of the target: a
    #: ministry with no numeric goal still gets balancing, and need not
    #: invent a number to obtain it.
    balance_candidate_loads: bool = True
    #: Roles across which to spread each person's work. ``None`` means the
    #: preference is not applied at all.
    role_variety_role_ids: list[int] | None = None


class CreatedAssignment(BaseModel):
    """One Assignment this request actually created.

    Assignments the version already carried are not listed: they were not
    created by this request, and reporting them would make the caller unable
    to tell what the run decided from what a person had decided earlier.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: int
    requirement_id: int
    membership_id: int
    event_id: int


class UnfilledRequirementReport(BaseModel):
    """A requirement the run could not fully staff.

    Not an error. An incomplete schedule is an approved outcome, and these
    rows are the part a human has to resolve by hand.
    """

    model_config = ConfigDict(extra="forbid")

    requirement_id: int
    #: How many positions are still missing, not how many requirements are.
    missing_count: int
    #: Short codes naming why. Several may apply to one requirement.
    diagnostic_codes: list[str] = Field(default_factory=list)


class AssignmentLoad(BaseModel):
    """How much one person is carrying after the run."""

    model_config = ConfigDict(extra="forbid")

    membership_id: int
    #: Existing assignments plus new ones -- the person's whole load for the
    #: period, not just what this run added.
    assignment_count: int


class GenerationMetrics(BaseModel):
    """What the soft preferences achieved.

    **The nulls are the honest part.** A cost is ``None`` when nothing
    optimized it -- no target configured, or no variety roles -- because
    reporting ``0`` would claim a preference had been evaluated and perfectly
    satisfied when it was never evaluated at all.
    """

    model_config = ConfigDict(extra="forbid")

    #: Ordered by membership id, so two runs of the same schedule compare.
    assignment_loads: list[AssignmentLoad] = Field(default_factory=list)
    #: Sum of each person's overshoot beyond the target. ``None`` without one.
    target_excess_total: int | None = None
    #: ``sum(load^2)``; lower is more evenly spread. ``None`` without a target.
    fairness_cost: int | None = None
    #: ``sum(role_load^2)`` over the variety roles. ``None`` without them.
    role_variety_cost: int | None = None


class GenerateDraftScheduleResponse(BaseModel):
    """The outcome of one generation request.

    Domain facts only: no ORM objects, no audit rows, and no solver internals.
    The caller learns what was created, what is still missing, and how evenly
    the work landed.
    """

    model_config = ConfigDict(extra="forbid")

    schedule_version_id: int
    #: True when every required position is now filled -- by an assignment
    #: this run created or one the version already had.
    is_complete: bool
    #: ``len(created_assignments)``, stated outright because it is the first
    #: thing a caller checks.
    created_count: int
    created_assignments: list[CreatedAssignment] = Field(default_factory=list)
    unfilled_requirements: list[UnfilledRequirementReport] = Field(
        default_factory=list
    )
    metrics: GenerationMetrics = Field(default_factory=GenerationMetrics)
