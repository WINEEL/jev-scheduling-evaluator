"""Transport models for the ScheduleVersion detail endpoint.

Split out of :mod:`app.api.schemas` because this one response has a dozen
nested shapes and would otherwise dominate the module every other endpoint
shares. Same rules apply: hand-built, ``extra="forbid"``, and no
``from_attributes`` anywhere -- an ORM row passed where one of these is
expected is an error, not a silent disclosure.

**Two things a reader of this contract should understand.**

*The snapshot is authoritative.* ``event_date``, ``role_id`` and
``required_count`` are the version's own immutable record of what was needed.
``event_name``, ``event_kind`` and ``role_name`` are labels read from today's
rows purely so a screen can render something human, and are ``null`` when those
rows are gone. When the two disagree, ``staleness`` says so; the snapshot is
never rewritten to match.

*Readiness is a diagnosis, not a permission.* See
:class:`FinalizationReadiness`.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.api.capabilities import CAN_OPERATE_DESCRIPTION

__all__ = [
    "AssignmentDetailResponse",
    "LifecycleTransitionRequest",
    "FinalizationIssueResponse",
    "FinalizationReadiness",
    "PeriodSummary",
    "RequirementDetailResponse",
    "RequirementFingerprintResponse",
    "ScheduleVersionDetailResponse",
    "ScheduleVersionStaleness",
    "ScheduleVersionSummary",
    "StaffingSummary",
]


class ScheduleVersionSummary(BaseModel):
    """The version's own identity and lifecycle state, exactly as persisted."""

    model_config = ConfigDict(extra="forbid")

    id: int
    schedule_id: int
    scheduling_period_id: int
    version_number: int
    #: ``DRAFT``, ``REVIEW`` or ``FINALIZED``, reported as stored. This
    #: endpoint never advances it.
    status: str
    #: Set only once the version reached FINALIZED.
    finalized_at: datetime.datetime | None = None
    #: The version this one amends, when it was created as a successor.
    amends_version_id: int | None = None
    amendment_reason: str | None = None
    notes: str | None = None


class PeriodSummary(BaseModel):
    """The scheduling period this version belongs to, and whose ministry it is."""

    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    ministry_id: int
    ministry_name: str
    start_date: datetime.date
    end_date: datetime.date
    #: When availability was locked. ``null`` means it is still open, which is
    #: why a reviewer may be looking at shifting ground.
    availability_locked_at: datetime.datetime | None = None


class RequirementDetailResponse(BaseModel):
    """One required position from the immutable snapshot."""

    model_config = ConfigDict(extra="forbid")

    requirement_id: int
    event_id: int
    #: **From the snapshot.** If the Event has since been moved, this is still
    #: the date this version was built around, and ``staleness`` reports the
    #: difference.
    event_date: datetime.date
    #: A current label, or ``null`` if the Event row is gone.
    event_name: str | None = None
    event_kind: str | None = None
    role_id: int
    role_name: str | None = None
    #: **From the snapshot**, never from the current StaffingRequirement.
    required_count: int
    #: Actual Assignment rows filling this requirement. May legitimately exceed
    #: ``required_count`` where a capacity override was authorized; it is not
    #: clamped, because hiding an overfill would hide the thing a reviewer most
    #: needs to see.
    assigned_count: int


class AssignmentDetailResponse(BaseModel):
    """One assignment, resolved to the person serving.

    Carries no membership notes, no qualification state, no linked account and
    no audit payload. ``override_reason`` is present deliberately: a reviewer
    approving a schedule has to be able to read why a placement was forced.
    """

    model_config = ConfigDict(extra="forbid")

    assignment_id: int
    requirement_id: int
    event_id: int
    membership_id: int
    person_id: int
    person_display_name: str
    role_id: int
    role_name: str | None = None
    is_override: bool
    override_reason: str | None = None


class StaffingSummary(BaseModel):
    """The version's staffing totals."""

    model_config = ConfigDict(extra="forbid")

    #: Sum of the snapshot's required counts.
    required_positions: int
    #: Actual Assignment rows, which may exceed ``required_positions``.
    assigned_positions: int
    #: Summed **per requirement** and floored at zero, so an overfilled Sunday
    #: cannot cancel out an unstaffed one.
    unfilled_positions: int
    #: ``unfilled_positions == 0``. An authorized overfill is still fully
    #: staffed.
    is_fully_staffed: bool


class RequirementFingerprintResponse(BaseModel):
    """One required position as the staleness comparison sees it.

    Four fields and no ids of its own -- that is the comparison's design (Task
    23): a requirement deleted and recreated identically is the same
    requirement, and one whose count changed is a different one.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: int
    event_date: datetime.date
    ministry_role_id: int
    required_count: int


class ScheduleVersionStaleness(BaseModel):
    """Whether current staffing configuration still matches the snapshot.

    The two difference lists are given as they are compared, not paired into a
    change taxonomy: a changed count legitimately appears as the old tuple in
    ``snapshot_only`` *and* the new tuple in ``current_only``, and deciding how
    to pair them is a presentation choice this contract does not make for its
    callers.
    """

    model_config = ConfigDict(extra="forbid")

    #: Exact set inequality. A stale version is still returned with 200.
    is_stale: bool
    #: Required now, but not recorded identically in the snapshot.
    current_only: list[RequirementFingerprintResponse] = Field(default_factory=list)
    #: Recorded in the snapshot, but not required identically now.
    snapshot_only: list[RequirementFingerprintResponse] = Field(default_factory=list)


class FinalizationIssueResponse(BaseModel):
    """One reason this version would not finalize cleanly.

    ``code`` and ``message`` are the domain's own, passed through unchanged.
    The two ids are populated when the issue is about a specific row and
    ``null`` for a version-wide one such as a stale snapshot.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    assignment_id: int | None = None
    schedule_version_requirement_id: int | None = None


class FinalizationReadiness(BaseModel):
    """Domain diagnostics -- **not** a statement about what may happen next.

    ``is_ready`` means "nothing about this version's contents would block
    finalization": the snapshot is fresh and no assignment has a problem. It
    does **not** mean the version may be finalized right now. Finalization also
    requires the version to be in REVIEW and to be the latest version, and
    those lifecycle rules belong to the endpoints that perform the transitions,
    not to this read.

    So a DRAFT with ``is_ready: true`` is a draft that looks clean, not a draft
    that can be finalized. The distinction is why this field is reported for
    every status: a head checking a draft before submitting it wants exactly
    these diagnostics, and refusing to compute them outside REVIEW would make
    the endpoint less useful without making it safer.
    """

    model_config = ConfigDict(extra="forbid")

    is_ready: bool
    issues: list[FinalizationIssueResponse] = Field(default_factory=list)


class ScheduleVersionDetailResponse(BaseModel):
    """Everything a review screen needs about one version, in one read."""

    model_config = ConfigDict(extra="forbid")

    schedule_version: ScheduleVersionSummary
    period: PeriodSummary
    requirements: list[RequirementDetailResponse] = Field(default_factory=list)
    assignments: list[AssignmentDetailResponse] = Field(default_factory=list)
    summary: StaffingSummary
    staleness: ScheduleVersionStaleness
    finalization_readiness: FinalizationReadiness
    #: Whether this caller may write to this ministry, never whether
    #: anybody may -- see :mod:`app.api.capabilities`. Advisory: the
    #: server authorizes every write again on its own.
    can_operate: bool = Field(description=CAN_OPERATE_DESCRIPTION)


class LifecycleTransitionRequest(BaseModel):
    """The body both lifecycle transitions accept, and all it may carry.

    One optional field, on purpose. ``reason`` is free text a head may attach
    to explain a submission or a finalization; it reaches the AuditEvent
    unchanged and is validated by the domain, which refuses a value that was
    supplied but is blank (the same optional-free-text convention every other
    service in this product follows).

    **Nothing here names the target status.** A body that said
    ``{"status": "FINALIZED"}`` would invite a client to ask for a transition
    the domain does not offer, and would make the endpoint's meaning depend on
    its payload rather than on its URL. Each transition has its own path, and
    the legal graph is the service's (:mod:`app.services.schedule_lifecycle`),
    never the caller's.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(
        default=None,
        description=(
            "Optional note recorded on the audit event for this transition."
            " Must not be blank if supplied."
        ),
    )
