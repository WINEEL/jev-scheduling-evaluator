"""Joined multi-ministry generation over real rows (Task 81).

Rollback-isolated; every row is synthetic and nothing is committed.

What only a database can prove, and what this file is therefore for:

- one joined run really does produce **assignments in N different ministries'
  DRAFT versions**, written through the ordinary batch writer with the
  ordinary rules;
- the church-wide rule survives the round trip -- a canonical Person reached
  through two ``MinistryMembership`` rows holds work in at most one ministry on
  any date, as stored, not merely as proposed;
- a refusal in **one** ministry leaves **no** ministry's rows behind, because
  nothing is committed until every placement has been accepted;
- the versions are still latest DRAFTs afterwards, so Task 80's lifecycle is
  untouched.

The engine's own behaviour is ``tests/test_scheduling_joined.py``; the
orchestration is ``tests/test_services_joined_schedule_generation.py``. Neither
is repeated here.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    Assignment,
)
from app.models.scheduling_input import AVAILABILITY_AVAILABLE
from app.scheduling.solver import SchedulingPolicy
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.joined_schedule_generation import (
    JoinedMinistryRequest,
    generate_joined_draft_schedules,
)
from tests.integration.factories import (
    make_availability,
    make_church,
    make_event,
    make_membership,
    make_ministry,
    make_ministry_head,
    make_period,
    make_person,
    make_qualification,
    make_role,
    make_schedule,
    make_staffing_requirement,
    make_version,
    make_version_requirement,
)

SUNDAY = datetime.date(2026, 10, 4)
NEXT_SUNDAY = datetime.date(2026, 10, 11)

POLICY = SchedulingPolicy(allow_no_response=False, balance_candidate_loads=True)


class Wing:
    """One ministry ready to be generated into: a DRAFT version, one role, one
    requirement per date, and whoever is qualified and available.
    """

    def __init__(self, session, *, church, name, head, dates):
        self.ministry = make_ministry(session, church=church, name=name)
        make_membership(session, person=head, ministry=self.ministry, is_head=True)
        self.role = make_role(session, ministry=self.ministry, name=f"{name} Role")
        self.period = make_period(session, ministry=self.ministry, name=f"{name} Q4")
        self.events = {
            date: make_event(session, period=self.period, event_date=date)
            for date in dates
        }
        self.schedule = make_schedule(session, period=self.period)
        self.version = make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.requirements = {}
        for date, event in self.events.items():
            make_staffing_requirement(session, event=event, role=self.role)
            self.requirements[date] = make_version_requirement(
                session, version=self.version, event=event, role=self.role,
            )
        self.memberships: dict[int, MinistryMembership] = {}

    def enrol(self, session, person: Person, *, head: Person) -> MinistryMembership:
        """Give ``person`` a membership here, qualified for the role and
        available on every date -- a second, separate membership row for a
        person who may already belong to another ministry.
        """
        membership = make_membership(
            session, person=person, ministry=self.ministry
        )
        make_qualification(
            session, membership=membership, role=self.role, decided_by=head,
        )
        for event in self.events.values():
            make_availability(
                session, membership=membership, event=event,
                state=AVAILABILITY_AVAILABLE,
            )
        session.flush()
        self.memberships[person.id] = membership
        return membership

    def request(self) -> JoinedMinistryRequest:
        return JoinedMinistryRequest(version=self.version, policy=POLICY)


def _assignments_for(session, wing: Wing) -> list[Assignment]:
    return list(
        session.execute(
            select(Assignment).where(
                Assignment.schedule_version_id == wing.version.id
            )
        ).scalars()
    )


@pytest.fixture
def world(db_session):
    """Three ministries, one head of all three, and one person shared by two
    of them. The shared person is the *only* candidate either shares.
    """
    church = make_church(db_session)
    head = make_person(db_session, church=church, name="Head")
    setup = Wing(
        db_session, church=church, name="SetupLike", head=head,
        dates=(SUNDAY, NEXT_SUNDAY),
    )
    av = Wing(db_session, church=church, name="AvLike", head=head, dates=(SUNDAY,))
    kids = Wing(
        db_session, church=church, name="KidsLike", head=head, dates=(SUNDAY,)
    )

    shared = make_person(db_session, church=church, name="Shared")
    setup.enrol(db_session, shared, head=head)
    av.enrol(db_session, shared, head=head)

    setup_only = make_person(db_session, church=church, name="SetupOnly")
    setup.enrol(db_session, setup_only, head=head)
    kids_only = make_person(db_session, church=church, name="KidsOnly")
    kids.enrol(db_session, kids_only, head=head)

    db_session.flush()
    return {
        "church": church, "head": head, "setup": setup, "av": av,
        "kids": kids, "shared": shared,
    }


def test_one_run_writes_assignments_into_three_ministries(db_session, world):
    result = generate_joined_draft_schedules(
        db_session,
        actor=world["head"],
        requests=[
            world["setup"].request(),
            world["av"].request(),
            world["kids"].request(),
        ],
    )
    db_session.flush()

    assert sorted(result.created_by_ministry) == sorted(
        wing.ministry.id for wing in (world["setup"], world["av"], world["kids"])
    )
    for wing in (world["setup"], world["av"], world["kids"]):
        rows = _assignments_for(db_session, wing)
        assert rows, f"{wing.ministry.name} got no assignments"
        for row in rows:
            assert row.ministry_id == wing.ministry.id
            assert row.is_override is False
            assert row.override_reason is None


def test_the_shared_person_serves_at_most_one_ministry_per_date(db_session, world):
    generate_joined_draft_schedules(
        db_session,
        actor=world["head"],
        requests=[
            world["setup"].request(),
            world["av"].request(),
            world["kids"].request(),
        ],
    )
    db_session.flush()

    # Read back through the stored rows, resolving each assignment to its
    # canonical Person -- never to a membership and never to a name.
    rows = db_session.execute(
        select(
            Person.id,
            Assignment.ministry_id,
            Assignment.event_id,
        )
        .join(
            MinistryMembership,
            MinistryMembership.id == Assignment.ministry_membership_id,
        )
        .join(Person, Person.id == MinistryMembership.person_id)
    ).all()

    events = {}
    for wing in (world["setup"], world["av"], world["kids"]):
        for date, event in wing.events.items():
            events[event.id] = date

    by_person_date: dict[tuple[int, datetime.date], set[int]] = {}
    for person_id, ministry_id, event_id in rows:
        key = (person_id, events[event_id])
        by_person_date.setdefault(key, set()).add(ministry_id)

    for (person_id, date), ministries in by_person_date.items():
        assert len(ministries) == 1, (
            f"person {person_id} serves {len(ministries)} ministries on {date}"
        )

    # And the case is real: the shared person was wanted by two ministries on
    # the one Sunday both of them meet.
    shared_dates = {
        date
        for (person_id, date), _ in by_person_date.items()
        if person_id == world["shared"].id
    }
    assert SUNDAY in shared_dates


def test_a_refusal_in_one_ministry_leaves_no_ministrys_rows(db_session, world):
    """All of it, or none of it -- proved on real rows.

    The head does not head the third ministry, so authorization refuses the
    run. Nothing was written for the two they *do* head, because the gates all
    run before any solving or writing.
    """
    outsider = make_ministry_head(
        db_session, church=world["church"], ministry=world["setup"].ministry,
        name="OtherHead",
    )
    with pytest.raises(AuthorizationError):
        generate_joined_draft_schedules(
            db_session,
            actor=outsider,
            requests=[world["setup"].request(), world["av"].request()],
        )

    for wing in (world["setup"], world["av"], world["kids"]):
        assert _assignments_for(db_session, wing) == []


def test_the_versions_are_still_latest_drafts_afterwards(db_session, world):
    generate_joined_draft_schedules(
        db_session,
        actor=world["head"],
        requests=[world["setup"].request(), world["av"].request()],
    )
    db_session.flush()

    for wing in (world["setup"], world["av"]):
        db_session.refresh(wing.version)
        assert wing.version.status == SCHEDULE_VERSION_STATUS_DRAFT
        assert wing.version.finalized_at is None


def test_a_second_run_proposes_nothing_new(db_session, world):
    """Repeatable without special machinery: the first run's rows are inputs
    to the second, so an already-complete schedule yields no further writes.
    """
    requests = [world["setup"].request(), world["av"].request()]
    first = generate_joined_draft_schedules(
        db_session, actor=world["head"], requests=requests
    )
    db_session.flush()
    before = {
        wing.ministry.id: len(_assignments_for(db_session, wing))
        for wing in (world["setup"], world["av"])
    }
    assert first.created_count > 0

    second = generate_joined_draft_schedules(
        db_session, actor=world["head"], requests=requests
    )
    db_session.flush()

    assert second.created_count == 0
    for wing in (world["setup"], world["av"]):
        assert len(_assignments_for(db_session, wing)) == before[wing.ministry.id]


def test_a_failure_on_a_later_ministry_leaves_no_ministrys_rows(db_session):
    """§6's "failure in one ministry means no partial joined result", on real
    rows and without faking anything.

    The joined solve succeeds for all three ministries. Between the solve and
    the writes, one volunteer the **second** ministry was given is
    disqualified, so the genuine Task 22 rules refuse that placement -- by
    which time the first ministry's rows have already been written and
    flushed. Nothing is caught and nothing is compensated; the caller's
    transaction discards the lot.

    The monkeypatch only *times* the change. Every write is a real one against
    PostgreSQL.
    """
    import app.services.joined_schedule_generation as module

    setup, av = world_fixture_ministries(db_session)
    version_ids = [setup.version.id, av.version.id]

    real_solve = module.solve_joined_schedule

    def solve_then_revoke(ministries):
        result = real_solve(ministries)
        # Whoever the *second* ministry was given is no longer qualified. The
        # state the solve reasoned about is now out of date, exactly as a
        # concurrent change would make it.
        proposal = result.results_by_ministry[av.ministry.id].proposed_assignments[0]
        db_session.execute(
            text(
                "UPDATE role_qualification SET is_qualified = false"
                " WHERE ministry_membership_id = :m"
            ),
            {"m": proposal.membership_id},
        )
        return result

    with pytest.raises(InvalidOperationError, match="not currently qualified"):
        with db_session.begin_nested():
            module.solve_joined_schedule = solve_then_revoke
            try:
                generate_joined_draft_schedules(
                    db_session,
                    actor=setup.head_person,
                    requests=[setup.request(), av.request()],
                )
            finally:
                module.solve_joined_schedule = real_solve

    # After the caller's rollback: the *first* ministry's rows are gone too,
    # even though those writes had succeeded.
    fresh = Session(
        bind=db_session.get_bind(), autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        surviving = list(
            fresh.execute(
                select(Assignment).where(
                    Assignment.schedule_version_id.in_(version_ids)
                )
            ).scalars()
        )
    finally:
        fresh.close()

    assert surviving == []


def world_fixture_ministries(db_session):
    """Two ministries sharing nobody, each with exactly one candidate.

    Built here rather than reusing ``world`` because this test needs the
    *second* ministry's placement to be the one that fails, which means
    knowing which ministry is written second -- ascending ministry id, and
    these two are created in that order.
    """
    church = make_church(db_session)
    head = make_person(db_session, church=church, name="Head")
    first = Wing(
        db_session, church=church, name="FirstLike", head=head, dates=(SUNDAY,)
    )
    second = Wing(
        db_session, church=church, name="SecondLike", head=head, dates=(SUNDAY,)
    )
    first.enrol(
        db_session, make_person(db_session, church=church, name="A"), head=head
    )
    second.enrol(
        db_session, make_person(db_session, church=church, name="B"), head=head
    )
    first.head_person = head
    second.head_person = head
    db_session.flush()
    assert first.ministry.id < second.ministry.id
    return first, second
