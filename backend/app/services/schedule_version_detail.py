"""One authorized, read-only view of everything a ScheduleVersion contains.

**Why this is a service and not route code.** The question "may this person
look at this version?" is a domain question with an existing answer
(:func:`app.services.authorization.require_ministry_reader`), and the version
carries no ``ministry_id`` of its own -- resolving one takes a query. Putting
that in the route would put a domain rule and a domain query in the HTTP layer,
where a second endpoint would eventually reimplement it slightly differently.

**What it does not do.** It computes no diagnostics. Staleness is Task 23's and
readiness is Task 26's; this module calls them and passes their answers along
untouched. It is also strictly read-only: only ``session.execute()`` and
attribute access, never ``add``, ``delete``, ``flush``, ``commit`` or
``rollback``.

**Snapshot over current state.** Every scheduling fact here -- the date a
position is needed, which role, how many -- is read from the immutable
``ScheduleVersionRequirement`` snapshot, never from the mutable
``StaffingRequirement`` the snapshot was taken from. Current ``Event`` and
``MinistryRole`` rows are consulted **only for display labels**. If the two
have drifted apart, that is what the staleness result is for; rewriting the
snapshot to agree with today's configuration would destroy the evidence.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.models.schedule_output import (
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import Event, SchedulingPeriod
from app.services.authorization import require_ministry_reader
from app.services.errors import InvalidOperationError
from app.services.finalization_readiness import (
    FinalizationReadinessResult,
    get_finalization_readiness,
)

__all__ = [
    "AssignmentDetail",
    "RequirementDetail",
    "ScheduleVersionDetail",
    "get_schedule_version_detail",
]


@dataclass(frozen=True, slots=True)
class RequirementDetail:
    """One snapshot requirement, with today's labels attached.

    ``event_date``, ``required_count`` and ``ministry_role_id`` come from the
    snapshot. ``event_name``, ``event_kind`` and ``role_name`` come from the
    current rows and are ``None`` when those rows no longer exist -- a label is
    a convenience, and its absence must not hide a requirement.
    """

    requirement_id: int
    event_id: int
    event_date: datetime.date
    event_name: str | None
    event_kind: str | None
    role_id: int
    role_name: str | None
    required_count: int
    assigned_count: int


@dataclass(frozen=True, slots=True)
class AssignmentDetail:
    """One Assignment, resolved to the person a reviewer needs to see."""

    assignment_id: int
    requirement_id: int
    event_id: int
    membership_id: int
    person_id: int
    person_display_name: str
    role_id: int
    role_name: str | None
    is_override: bool
    override_reason: str | None


@dataclass(frozen=True, slots=True)
class ScheduleVersionDetail:
    """Everything the review screen needs, in one consistent read.

    The counts are derived here rather than left to the caller so that every
    consumer agrees on what "unfilled" means -- in particular that an
    authorized overfill does not produce a negative shortfall.
    """

    version: ScheduleVersion
    period: SchedulingPeriod
    ministry: Ministry
    requirements: tuple[RequirementDetail, ...]
    assignments: tuple[AssignmentDetail, ...]
    readiness: FinalizationReadinessResult

    @property
    def required_positions(self) -> int:
        return sum(r.required_count for r in self.requirements)

    @property
    def assigned_positions(self) -> int:
        """Actual Assignment rows, not the capacity they were meant to fill."""
        return len(self.assignments)

    @property
    def unfilled_positions(self) -> int:
        """Shortfall summed per requirement, floored at zero **per row**.

        Summing ``required - assigned`` across the version would let an
        overfilled Sunday quietly cancel out an unstaffed one and report a
        fully staffed schedule that is nothing of the kind.
        """
        return sum(
            max(r.required_count - r.assigned_count, 0) for r in self.requirements
        )

    @property
    def is_fully_staffed(self) -> bool:
        """Every requirement has at least its required count. Deliberately not
        ``assigned == required``: an authorized overfill is still fully
        staffed.
        """
        return self.unfilled_positions == 0


def get_schedule_version_detail(
    session: Session,
    *,
    actor: Person,
    version: ScheduleVersion,
) -> ScheduleVersionDetail:
    """Read ``version`` in full, for an actor allowed to *read* its ministry.

    An oversight read (core §4.3.1): an Admin sees any ministry's version
    exactly as its Head does, diagnostics included. Whether they may then
    change anything is a separate question, asked separately -- the HTTP
    layer reports it as ``can_operate`` and every write re-checks it.

    **Read-only**, and every query runs on the supplied Session -- including
    Task 23's and Task 26's, which are reached through
    :func:`get_finalization_readiness` -- so the whole answer describes one
    transactional moment rather than several.

    :raises AuthorizationError: the actor is not an active Admin, nor an active
        Ministry Head of this version's ministry.
    :raises InvalidOperationError: the version is not persisted, or its period
        or ministry cannot be resolved.
    """
    if version.id is None:
        raise InvalidOperationError("version must be persisted (id is None)")
    if version.scheduling_period_id is None:
        raise InvalidOperationError("version must have a scheduling_period_id")

    period, ministry = _resolve_period_and_ministry(session, version)

    # Authorization first: nothing about the version's contents is read, let
    # alone returned, until the actor has been allowed to see it.
    require_ministry_reader(actor, ministry_id=ministry.id)

    requirements = _load_requirements(session, schedule_version_id=version.id)
    assignments = _load_assignments(session, schedule_version_id=version.id)

    # Task 26 computes Task 23 on the way and carries the result whole, so one
    # call answers both questions. Calling staleness separately would repeat
    # two queries and, worse, allow the two halves of the response to disagree
    # if a concurrent write landed between them.
    readiness = get_finalization_readiness(session, version=version)

    return ScheduleVersionDetail(
        version=version,
        period=period,
        ministry=ministry,
        requirements=requirements,
        assignments=assignments,
        readiness=readiness,
    )


def _resolve_period_and_ministry(
    session: Session, version: ScheduleVersion
) -> tuple[SchedulingPeriod, Ministry]:
    """One joined read, because authorization cannot begin without the ministry
    and the response cannot be built without the period.
    """
    row = session.execute(
        select(SchedulingPeriod, Ministry)
        .join(Ministry, Ministry.id == SchedulingPeriod.ministry_id)
        .where(SchedulingPeriod.id == version.scheduling_period_id)
    ).one_or_none()
    if row is None:
        raise InvalidOperationError(
            "version's scheduling period could not be resolved"
        )
    return row[0], row[1]


def _requirements_statement(schedule_version_id: int) -> Select:
    """Snapshot rows, their labels, and their assignment counts in one pass.

    The count is a correlated scalar subquery rather than a second round trip
    per requirement -- the N+1 this endpoint would otherwise obviously have.
    ``LEFT OUTER JOIN`` for the label tables: a requirement whose event or role
    row has since been deleted must still appear, unlabelled.

    Ordered by snapshot ``event_date`` first, then event id, then the role's
    ``display_order`` (its purpose), then role name and id, then the
    requirement id -- fully determined, with no reliance on how PostgreSQL
    happens to return rows.
    """
    assigned = (
        select(func.count(Assignment.id))
        .where(Assignment.schedule_version_requirement_id == ScheduleVersionRequirement.id)
        .correlate(ScheduleVersionRequirement)
        .scalar_subquery()
    )
    return (
        select(
            ScheduleVersionRequirement.id,
            ScheduleVersionRequirement.event_id,
            ScheduleVersionRequirement.event_date,
            ScheduleVersionRequirement.ministry_role_id,
            ScheduleVersionRequirement.required_count,
            Event.name.label("event_name"),
            Event.event_kind.label("event_kind"),
            MinistryRole.name.label("role_name"),
            MinistryRole.display_order.label("role_display_order"),
            assigned.label("assigned_count"),
        )
        .select_from(ScheduleVersionRequirement)
        .outerjoin(Event, Event.id == ScheduleVersionRequirement.event_id)
        .outerjoin(
            MinistryRole, MinistryRole.id == ScheduleVersionRequirement.ministry_role_id
        )
        .where(ScheduleVersionRequirement.schedule_version_id == schedule_version_id)
        .order_by(
            ScheduleVersionRequirement.event_date,
            ScheduleVersionRequirement.event_id,
            MinistryRole.display_order,
            MinistryRole.name,
            ScheduleVersionRequirement.ministry_role_id,
            ScheduleVersionRequirement.id,
        )
    )


def _load_requirements(
    session: Session, *, schedule_version_id: int
) -> tuple[RequirementDetail, ...]:
    rows = session.execute(_requirements_statement(schedule_version_id)).all()
    return tuple(
        RequirementDetail(
            requirement_id=row.id,
            event_id=row.event_id,
            event_date=row.event_date,
            event_name=row.event_name,
            event_kind=row.event_kind,
            role_id=row.ministry_role_id,
            role_name=row.role_name,
            required_count=row.required_count,
            assigned_count=row.assigned_count,
        )
        for row in rows
    )


def _assignments_statement(schedule_version_id: int) -> Select:
    """Assignment, its snapshot requirement, and the person behind it.

    Person is reached through ``MinistryMembership`` -- the assignment names a
    membership, and a membership names a person, which is the only path that
    keeps "who is serving" tied to "in which ministry".

    Ordered by the **snapshot's** date, not the current event's, so the list
    stays in the order the version was built for even if an event has since
    been moved.
    """
    return (
        select(
            Assignment.id,
            Assignment.schedule_version_requirement_id,
            Assignment.event_id,
            Assignment.ministry_membership_id,
            Assignment.is_override,
            Assignment.override_reason,
            Person.id.label("person_id"),
            Person.display_name.label("person_display_name"),
            ScheduleVersionRequirement.event_date.label("snapshot_event_date"),
            ScheduleVersionRequirement.ministry_role_id.label("role_id"),
            MinistryRole.name.label("role_name"),
            MinistryRole.display_order.label("role_display_order"),
        )
        .select_from(Assignment)
        .join(
            ScheduleVersionRequirement,
            ScheduleVersionRequirement.id
            == Assignment.schedule_version_requirement_id,
        )
        .join(
            MinistryMembership,
            MinistryMembership.id == Assignment.ministry_membership_id,
        )
        .join(Person, Person.id == MinistryMembership.person_id)
        .outerjoin(
            MinistryRole,
            MinistryRole.id == ScheduleVersionRequirement.ministry_role_id,
        )
        .where(Assignment.schedule_version_id == schedule_version_id)
        .order_by(
            ScheduleVersionRequirement.event_date,
            Assignment.event_id,
            MinistryRole.display_order,
            MinistryRole.name,
            ScheduleVersionRequirement.ministry_role_id,
            Person.display_name,
            Person.id,
            Assignment.id,
        )
    )


def _load_assignments(
    session: Session, *, schedule_version_id: int
) -> tuple[AssignmentDetail, ...]:
    rows = session.execute(_assignments_statement(schedule_version_id)).all()
    return tuple(
        AssignmentDetail(
            assignment_id=row.id,
            requirement_id=row.schedule_version_requirement_id,
            event_id=row.event_id,
            membership_id=row.ministry_membership_id,
            person_id=row.person_id,
            person_display_name=row.person_display_name,
            role_id=row.role_id,
            role_name=row.role_name,
            is_override=row.is_override,
            override_reason=row.override_reason,
        )
        for row in rows
    )
