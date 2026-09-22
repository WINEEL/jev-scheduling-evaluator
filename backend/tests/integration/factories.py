"""Row builders for the PostgreSQL integration suite.

Plain functions that build real ORM rows on a real Session and flush them, so
the caller gets database-assigned identities back. Everything they write lives
inside the test's transaction and is rolled back with it (see ``conftest``).

Names are suffixed with a random token because the integration branch is a
real database that may already hold rows: ``ministry.name`` and
``ministry_role.name`` carry case-insensitive unique indexes, and a fixed
"Setup" would collide with any pre-existing row rather than testing anything.
Nothing here deletes or truncates to make room.
"""

from __future__ import annotations

import datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models.core import (
    Church,
    Ministry,
    MinistryMembership,
    MinistryRole,
    Person,
    RoleQualification,
)
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    Assignment,
    Schedule,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import (
    EVENT_KIND_SUNDAY_SERVICE,
    Availability,
    Event,
    MembershipServingLimit,
    SchedulingPeriod,
    StaffingRequirement,
)

UTC = datetime.timezone.utc


def unique(label: str) -> str:
    """A name no pre-existing row can collide with."""
    return f"{label}-it-{uuid4().hex[:10]}"


def make_church(session: Session, *, name: str = "Church") -> Church:
    church = Church(name=unique(name), timezone="Asia/Kolkata")
    session.add(church)
    session.flush()
    return church


def make_person(
    session: Session, *, church: Church, name: str = "Person",
    is_admin: bool = False, deactivated: bool = False,
) -> Person:
    person = Person(
        church_id=church.id, display_name=unique(name), is_admin=is_admin,
        deactivated_at=datetime.datetime(2026, 1, 1, tzinfo=UTC) if deactivated else None,
    )
    session.add(person)
    session.flush()
    return person


def make_ministry(session: Session, *, church: Church, name: str = "Ministry") -> Ministry:
    ministry = Ministry(church_id=church.id, name=unique(name))
    session.add(ministry)
    session.flush()
    return ministry


def make_membership(
    session: Session, *, person: Person, ministry: Ministry, is_head: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    session.add(membership)
    session.flush()
    return membership


def make_ministry_head(
    session: Session, *, church: Church, ministry: Ministry, name: str = "Head",
    is_admin: bool = False,
) -> Person:
    """A Person who actively heads ``ministry`` -- the actor for any
    operational write.

    **Added by Task 80, and it replaced an Admin almost everywhere.** Until
    then these scenarios acted as a church-wide Admin, because an Admin passed
    every ministry's gate; that is no longer true for a single configuration,
    scheduling or lifecycle write in the product, so a scenario built around
    one would test nothing but the refusal.

    ``is_admin`` is here for the handful of tests whose subject is an actor who
    is *both* -- they must pass, and pass through the head membership rather
    than through the flag.
    """
    person = make_person(session, church=church, name=name, is_admin=is_admin)
    make_membership(session, person=person, ministry=ministry, is_head=True)
    return person


def make_role(
    session: Session, *, ministry: Ministry, name: str = "Role", deactivated: bool = False,
) -> MinistryRole:
    role = MinistryRole(
        ministry_id=ministry.id, name=unique(name),
        deactivated_at=datetime.datetime(2026, 1, 1, tzinfo=UTC) if deactivated else None,
    )
    session.add(role)
    session.flush()
    return role


def make_qualification(
    session: Session, *, membership: MinistryMembership, role: MinistryRole,
    decided_by: Person, is_qualified: bool = True, ministry_id: int | None = None,
) -> RoleQualification:
    """``ministry_id`` is settable on purpose.

    It is the single shared column both composite foreign keys route through,
    so a test proving the cross-ministry protection needs to be able to supply
    a deliberately wrong one. Callers that just want a valid row omit it.
    """
    qualification = RoleQualification(
        ministry_membership_id=membership.id,
        ministry_role_id=role.id,
        ministry_id=membership.ministry_id if ministry_id is None else ministry_id,
        is_qualified=is_qualified,
        decided_at=datetime.datetime.now(tz=UTC),
        decided_by_person_id=decided_by.id,
    )
    session.add(qualification)
    return qualification


def make_period(
    session: Session, *, ministry: Ministry, name: str = "Q4 2026",
    start: datetime.date = datetime.date(2026, 10, 4),
    end: datetime.date = datetime.date(2026, 12, 27),
    availability_locked: bool = True,
) -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=unique(name), start_date=start, end_date=end,
        availability_locked_at=datetime.datetime.now(tz=UTC) if availability_locked else None,
    )
    session.add(period)
    session.flush()
    return period


def make_event(
    session: Session, *, period: SchedulingPeriod, event_date: datetime.date,
    cancelled: bool = False,
) -> Event:
    event = Event(
        scheduling_period_id=period.id, ministry_id=period.ministry_id,
        event_date=event_date, event_kind=EVENT_KIND_SUNDAY_SERVICE,
        cancelled_at=datetime.datetime(2026, 1, 1, tzinfo=UTC) if cancelled else None,
    )
    session.add(event)
    session.flush()
    return event


def make_staffing_requirement(
    session: Session, *, event: Event, role: MinistryRole, required_count: int = 1,
) -> StaffingRequirement:
    requirement = StaffingRequirement(
        event_id=event.id, ministry_role_id=role.id, ministry_id=event.ministry_id,
        required_count=required_count,
    )
    session.add(requirement)
    session.flush()
    return requirement


def make_availability(
    session: Session, *, membership: MinistryMembership, event: Event, state: str,
) -> Availability:
    """``ministry_id`` is supplied explicitly: it is the shared integrity-spine
    column both composite foreign keys route through, and no relationship
    manages it (scheduling-input §7.2), so PostgreSQL rejects a row without it.
    """
    availability = Availability(
        ministry_membership_id=membership.id, event_id=event.id,
        ministry_id=membership.ministry_id, availability_state=state,
    )
    session.add(availability)
    session.flush()
    return availability


def make_serving_limit(
    session: Session, *, membership: MinistryMembership, period: SchedulingPeriod,
    max_assignments: int = 2, ministry_id: int | None = None,
) -> MembershipServingLimit:
    """``ministry_id`` is settable for the same reason as :func:`make_qualification`:
    it is the single shared column both composite foreign keys route through.
    """
    limit = MembershipServingLimit(
        ministry_membership_id=membership.id,
        scheduling_period_id=period.id,
        ministry_id=membership.ministry_id if ministry_id is None else ministry_id,
        max_assignments=max_assignments,
    )
    session.add(limit)
    session.flush()
    return limit


def make_schedule(session: Session, *, period: SchedulingPeriod) -> Schedule:
    schedule = Schedule(scheduling_period_id=period.id)
    session.add(schedule)
    session.flush()
    return schedule


def make_version(
    session: Session, *, schedule: Schedule, period: SchedulingPeriod,
    version_number: int = 1, status: str = SCHEDULE_VERSION_STATUS_DRAFT,
    finalized_at: datetime.datetime | None = None,
) -> ScheduleVersion:
    """``finalized_at`` is the caller's responsibility.

    ``status_finalized_at_agree`` is a two-way CHECK -- FINALIZED implies a
    timestamp and a timestamp implies FINALIZED -- so a fixture that sets one
    without the other is rejected by PostgreSQL, correctly.
    """
    version = ScheduleVersion(
        schedule_id=schedule.id, scheduling_period_id=period.id,
        version_number=version_number, status=status, finalized_at=finalized_at,
    )
    session.add(version)
    session.flush()
    return version


def make_version_requirement(
    session: Session, *, version: ScheduleVersion, event: Event, role: MinistryRole,
    event_date: datetime.date | None = None, required_count: int = 1,
) -> ScheduleVersionRequirement:
    """``event_date`` defaults to the event's current date -- the snapshot as
    it would have been taken today. Tests that care about divergence pass it
    explicitly and then move the Event.
    """
    requirement = ScheduleVersionRequirement(
        schedule_version_id=version.id,
        event_id=event.id,
        event_date=event.event_date if event_date is None else event_date,
        ministry_role_id=role.id,
        scheduling_period_id=event.scheduling_period_id,
        ministry_id=event.ministry_id,
        required_count=required_count,
    )
    session.add(requirement)
    session.flush()
    return requirement


def make_assignment(
    session: Session, *, requirement: ScheduleVersionRequirement,
    membership: MinistryMembership, is_override: bool = False,
    override_reason: str | None = None,
) -> Assignment:
    assignment = Assignment(
        schedule_version_requirement_id=requirement.id,
        ministry_membership_id=membership.id,
        schedule_version_id=requirement.schedule_version_id,
        event_id=requirement.event_id,
        ministry_id=requirement.ministry_id,
        is_override=is_override,
        override_reason=override_reason,
    )
    session.add(assignment)
    session.flush()
    return assignment
