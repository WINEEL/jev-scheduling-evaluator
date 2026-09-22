"""The fictional dataset the demo seed writes, and the rules for writing it.

**Everything here is invented.** The church, the ministry, the people and the
dates exist only to exercise the scheduling screens. No real congregation,
ministry or member is represented, and nothing in this file came from one.

**Why some rows are written directly.** Most of the seed goes through the real
domain services -- periods, Sunday events, staffing, qualifications,
availability and the availability lock are all created exactly as the
application creates them, so their validation and audit history are the real
ones. Four kinds of row are inserted directly, because no public service
creates them yet: Church, Person, Ministry and MinistryRole. That is the
bootstrap problem rather than a shortcut -- every service takes an ``actor``,
and the first Person cannot be created by an actor who does not exist. Those
inserts live here, in demo tooling, and no production code was changed or
weakened to allow them.

The dataset is deliberately small and legible: seven volunteers, five roles,
four Sundays, and one Sunday where enough people are away that a position
genuinely cannot be filled -- so the screens show both a filled schedule and an
unresolved position without anybody having to break something to see it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import Church, Ministry, MinistryMembership, MinistryRole, Person
from app.models.schedule_output import Schedule
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_UNAVAILABLE,
    Event,
    SchedulingPeriod,
)
from app.services.availability import set_availability
from app.services.ministry_authority import grant_ministry_head
from app.services.role_qualification import set_role_qualification
from app.services.scheduling_period import (
    create_scheduling_period,
    generate_sunday_events,
    lock_availability,
)
from app.services.staffing_requirement import set_staffing_requirement

# -- The dataset, as data --------------------------------------------------

CHURCH_NAME = "Demo Church"
CHURCH_TIMEZONE = "UTC"
MINISTRY_NAME = "Setup Demo"
PERIOD_NAME = "October 2026 Demo"

#: Stamped on rows whose model already has a free-text field for it. No column
#: was added for demo provenance -- the schema is untouched.
DEMO_NOTE = "Local demo data. Fictional people and events for UI testing only."

LEAD_ROLE = "Setup Lead"
ROLE_NAMES = (LEAD_ROLE, "Setup 2", "Setup 3", "Setup 4", "Setup 5")

#: Four Sundays. Checked at seed time rather than trusted.
PERIOD_START = datetime.date(2026, 10, 4)
PERIOD_END = datetime.date(2026, 10, 25)
EXPECTED_SUNDAYS = (
    datetime.date(2026, 10, 4),
    datetime.date(2026, 10, 11),
    datetime.date(2026, 10, 18),
    datetime.date(2026, 10, 25),
)

REQUIRED_PER_ROLE = 1

ADMIN_NAME = "Dana Admin"
HEAD_NAME = "Jordan Lee"


@dataclass(frozen=True, slots=True)
class DemoVolunteer:
    """One fictional volunteer.

    ``lead_qualified`` is deliberately false for most of them: a ministry where
    everyone can lead would hide the scarcity rule the scheduler exists to
    respect. ``unavailable_on`` is the small number of "no" answers that make
    the generated schedule visibly obey availability.
    """

    display_name: str
    email: str
    lead_qualified: bool
    unavailable_on: tuple[datetime.date, ...] = ()


#: `.invalid` is reserved by RFC 2606 and can never resolve or be delivered to,
#: so these addresses cannot collide with or reach a real person.
VOLUNTEERS: tuple[DemoVolunteer, ...] = (
    DemoVolunteer("Jordan Lee", "jordan.lee@demo.invalid", lead_qualified=True),
    DemoVolunteer("Alex Johnson", "alex.johnson@demo.invalid", lead_qualified=True),
    DemoVolunteer("Taylor Smith", "taylor.smith@demo.invalid", lead_qualified=False,
                  unavailable_on=(datetime.date(2026, 10, 11), datetime.date(2026, 10, 25))),
    DemoVolunteer("Morgan Davis", "morgan.davis@demo.invalid", lead_qualified=False,
                  unavailable_on=(datetime.date(2026, 10, 18),)),
    DemoVolunteer("Casey Brown", "casey.brown@demo.invalid", lead_qualified=False,
                  unavailable_on=(datetime.date(2026, 10, 18),)),
    DemoVolunteer("Riley Wilson", "riley.wilson@demo.invalid", lead_qualified=False,
                  unavailable_on=(datetime.date(2026, 10, 25),)),
    DemoVolunteer("Jamie Miller", "jamie.miller@demo.invalid", lead_qualified=False,
                  unavailable_on=(datetime.date(2026, 10, 25),)),
)

#: 4 Sundays x 5 roles. On 25 October only four volunteers are free, so one
#: position cannot be filled -- which is the point: the review screen should
#: show an unresolved position, not hide it.
EXPECTED_REQUIRED_POSITIONS = len(EXPECTED_SUNDAYS) * len(ROLE_NAMES)


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What a caller needs afterwards, and nothing else."""

    church_id: int
    ministry_id: int
    period_id: int
    demo_actor_person_id: int
    admin_person_id: int
    already_existed: bool


class DemoDataError(RuntimeError):
    """The demo data on this database is not in a state the seed can work with.

    Raised rather than repaired. A half-seeded database is a situation with no
    single correct fix, and guessing at one risks deleting rows somebody
    wanted; recreating the demo branch is both simpler and safer.
    """


# -- Detection -------------------------------------------------------------


def find_demo_church(session: Session) -> Church | None:
    """The demo church, or ``None``. Raises if there is more than one."""
    churches = (
        session.execute(select(Church).where(Church.name == CHURCH_NAME)).scalars().all()
    )
    if len(churches) > 1:
        raise DemoDataError(
            f"found {len(churches)} churches named {CHURCH_NAME!r}; expected at most"
            " one. Recreate the demo branch rather than seeding again."
        )
    return churches[0] if churches else None


def describe_existing(session: Session, church: Church) -> SeedResult:
    """Report an already-seeded database, refusing anything half-built.

    Every piece is checked. A database carrying the demo church but missing its
    ministry, roles, people or period is **not** re-seeded on top: partial data
    means something went wrong, and writing more rows over it would turn a
    clear problem into a confusing one.
    """
    ministry = session.execute(
        select(Ministry).where(
            Ministry.church_id == church.id, Ministry.name == MINISTRY_NAME
        )
    ).scalar_one_or_none()
    if ministry is None:
        raise DemoDataError(
            f"{CHURCH_NAME!r} exists but its {MINISTRY_NAME!r} ministry does not."
            " The demo data is incomplete; recreate the demo branch."
        )

    roles = (
        session.execute(select(MinistryRole).where(MinistryRole.ministry_id == ministry.id))
        .scalars()
        .all()
    )
    if {role.name for role in roles} != set(ROLE_NAMES):
        raise DemoDataError(
            f"{MINISTRY_NAME!r} does not have exactly the expected"
            f" {len(ROLE_NAMES)} demo roles. The demo data is incomplete;"
            " recreate the demo branch."
        )

    period = session.execute(
        select(SchedulingPeriod).where(
            SchedulingPeriod.ministry_id == ministry.id,
            SchedulingPeriod.name == PERIOD_NAME,
        )
    ).scalar_one_or_none()
    if period is None:
        raise DemoDataError(
            f"{MINISTRY_NAME!r} exists but its {PERIOD_NAME!r} period does not."
            " The demo data is incomplete; recreate the demo branch."
        )

    people = (
        session.execute(select(Person).where(Person.church_id == church.id)).scalars().all()
    )
    by_name = {person.display_name: person for person in people}
    expected_names = {volunteer.display_name for volunteer in VOLUNTEERS} | {ADMIN_NAME}
    if not expected_names.issubset(by_name):
        raise DemoDataError(
            "the demo church is missing some of its people."
            " The demo data is incomplete; recreate the demo branch."
        )

    return SeedResult(
        church_id=church.id,
        ministry_id=ministry.id,
        period_id=period.id,
        demo_actor_person_id=by_name[HEAD_NAME].id,
        admin_person_id=by_name[ADMIN_NAME].id,
        already_existed=True,
    )


# -- Seeding ---------------------------------------------------------------


def seed_demo_data(session: Session) -> SeedResult:
    """Create the demo dataset, or report that it is already there.

    Idempotent at the level that matters: run twice against the same branch and
    the second run creates nothing and returns the same ids. It does **not**
    achieve that by deleting anything -- there is no reset here, by design. A
    clean slate comes from recreating the demo branch.

    The caller owns the transaction, exactly as with every domain service in
    this project: nothing here commits or rolls back.
    """
    for sunday in EXPECTED_SUNDAYS:
        if sunday.weekday() != 6:
            raise DemoDataError(f"{sunday.isoformat()} is not a Sunday")

    existing = find_demo_church(session)
    if existing is not None:
        return describe_existing(session, existing)

    church = Church(name=CHURCH_NAME, timezone=CHURCH_TIMEZONE)
    session.add(church)
    session.flush([church])

    # The bootstrap actor. Every service below requires one, and an Admin is
    # the only actor permitted to grant Ministry Head authority.
    admin = Person(
        church_id=church.id,
        display_name=ADMIN_NAME,
        email="dana.admin@demo.invalid",
        is_admin=True,
    )
    session.add(admin)
    session.flush([admin])

    ministry = Ministry(
        church_id=church.id, name=MINISTRY_NAME, description=DEMO_NOTE
    )
    session.add(ministry)
    session.flush([ministry])

    roles: dict[str, MinistryRole] = {}
    for order, role_name in enumerate(ROLE_NAMES):
        role = MinistryRole(
            ministry_id=ministry.id,
            name=role_name,
            description=DEMO_NOTE,
            display_order=order,
        )
        session.add(role)
        roles[role_name] = role
    session.flush(list(roles.values()))

    memberships: dict[str, MinistryMembership] = {}
    for volunteer in VOLUNTEERS:
        person = Person(
            church_id=church.id,
            display_name=volunteer.display_name,
            email=volunteer.email,
        )
        session.add(person)
        session.flush([person])

        membership = MinistryMembership(person_id=person.id, ministry_id=ministry.id)
        session.add(membership)
        session.flush([membership])
        memberships[volunteer.display_name] = membership

    # From here on, the real services do the work -- including their audit rows.
    head_membership = memberships[HEAD_NAME]
    grant_ministry_head(
        session, actor=admin, membership=head_membership, reason="Demo data setup"
    )
    head = head_membership.person

    for volunteer in VOLUNTEERS:
        membership = memberships[volunteer.display_name]
        for role_name in ROLE_NAMES:
            if role_name == LEAD_ROLE and not volunteer.lead_qualified:
                continue
            set_role_qualification(
                session,
                actor=head,
                membership=membership,
                role=roles[role_name],
                is_qualified=True,
                reason="Demo data setup",
            )

    period = create_scheduling_period(
        session,
        actor=head,
        ministry=ministry,
        name=PERIOD_NAME,
        start_date=PERIOD_START,
        end_date=PERIOD_END,
    )
    events = generate_sunday_events(session, actor=head, period=period)
    session.flush()

    dates = sorted(event.event_date for event in events)
    if tuple(dates) != EXPECTED_SUNDAYS:
        raise DemoDataError(
            f"expected {len(EXPECTED_SUNDAYS)} Sundays in {PERIOD_NAME!r},"
            f" got {len(dates)}"
        )

    for event in events:
        for role_name in ROLE_NAMES:
            set_staffing_requirement(
                session,
                actor=head,
                event=event,
                role=roles[role_name],
                required_count=REQUIRED_PER_ROLE,
                reason="Demo data setup",
            )

    # Every member answers for every Sunday: this first demo does not rely on
    # "no response" to work, so the checkbox in the UI changes nothing here.
    for volunteer in VOLUNTEERS:
        membership = memberships[volunteer.display_name]
        for event in events:
            state = (
                AVAILABILITY_UNAVAILABLE
                if event.event_date in volunteer.unavailable_on
                else AVAILABILITY_AVAILABLE
            )
            set_availability(
                session,
                actor=head,
                membership=membership,
                event=event,
                availability_state=state,
                reason="Demo data setup",
            )

    lock_availability(session, actor=head, period=period, reason="Demo data setup")
    session.flush()

    return SeedResult(
        church_id=church.id,
        ministry_id=ministry.id,
        period_id=period.id,
        demo_actor_person_id=head.id,
        admin_person_id=admin.id,
        already_existed=False,
    )


# -- Verification ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemoVerification:
    """Counts only. No names, no emails, no personal data of any kind."""

    churches: int
    ministries: int
    people: int
    roles: int
    events: int
    staffing_requirements: int
    availability_rows: int
    unavailable_answers: int
    availability_locked: bool
    schedules: int

    @property
    def problems(self) -> tuple[str, ...]:
        expected_availability = len(VOLUNTEERS) * len(EXPECTED_SUNDAYS)
        expected_unavailable = sum(len(v.unavailable_on) for v in VOLUNTEERS)
        checks = (
            (self.churches == 1, "expected exactly one demo church"),
            (self.ministries == 1, "expected exactly one demo ministry"),
            (self.people == len(VOLUNTEERS) + 1, f"expected {len(VOLUNTEERS) + 1} people"),
            (self.roles == len(ROLE_NAMES), f"expected {len(ROLE_NAMES)} roles"),
            (self.events == len(EXPECTED_SUNDAYS), f"expected {len(EXPECTED_SUNDAYS)} Sundays"),
            (
                self.staffing_requirements == EXPECTED_REQUIRED_POSITIONS,
                f"expected {EXPECTED_REQUIRED_POSITIONS} staffing requirements",
            ),
            (
                self.availability_rows == expected_availability,
                f"expected {expected_availability} availability answers",
            ),
            (
                self.unavailable_answers == expected_unavailable,
                f"expected {expected_unavailable} 'unavailable' answers",
            ),
            (self.availability_locked, "expected availability to be locked"),
            (self.schedules == 0, "expected no schedule yet -- the UI starts it"),
        )
        return tuple(message for ok, message in checks if not ok)


def verify_demo_data(session: Session) -> DemoVerification:
    """Read back what was seeded, counting rows and looking at nothing else."""
    church = find_demo_church(session)
    if church is None:
        raise DemoDataError(f"no church named {CHURCH_NAME!r} was found")

    ministries = (
        session.execute(select(Ministry).where(Ministry.church_id == church.id))
        .scalars()
        .all()
    )
    ministry_ids = [ministry.id for ministry in ministries]

    people = session.execute(
        select(Person).where(Person.church_id == church.id)
    ).scalars().all()
    roles = session.execute(
        select(MinistryRole).where(MinistryRole.ministry_id.in_(ministry_ids))
    ).scalars().all()
    periods = session.execute(
        select(SchedulingPeriod).where(SchedulingPeriod.ministry_id.in_(ministry_ids))
    ).scalars().all()
    period_ids = [period.id for period in periods]

    events = session.execute(
        select(Event).where(Event.scheduling_period_id.in_(period_ids))
    ).scalars().all()

    from app.models.scheduling_input import Availability, StaffingRequirement

    event_ids = [event.id for event in events]
    staffing = session.execute(
        select(StaffingRequirement).where(StaffingRequirement.event_id.in_(event_ids))
    ).scalars().all()
    availability = session.execute(
        select(Availability).where(Availability.event_id.in_(event_ids))
    ).scalars().all()
    schedules = session.execute(
        select(Schedule).where(Schedule.scheduling_period_id.in_(period_ids))
    ).scalars().all()

    return DemoVerification(
        churches=1,
        ministries=len(ministries),
        people=len(people),
        roles=len(roles),
        events=len(events),
        staffing_requirements=len(staffing),
        availability_rows=len(availability),
        unavailable_answers=sum(
            1 for row in availability if row.availability_state == AVAILABILITY_UNAVAILABLE
        ),
        availability_locked=all(
            period.availability_locked_at is not None for period in periods
        )
        and len(periods) == 1,
        schedules=len(schedules),
    )
