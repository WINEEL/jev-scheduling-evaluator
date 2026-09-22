"""Recorded serving against real PostgreSQL rows (Task 79 §13--§15).

Rollback-isolated by the shared harness; nothing is committed. Every identity
is synthetic, and no production data is read or written.

**Real PostgreSQL is not optional for this file.** What is being tested is the
*meaning* of three queries built on ``DISTINCT ON`` (the authoritative-version
subquery, ADR 0003), a ``SELECT DISTINCT`` over a three-column tuple, and a
``HAVING count(DISTINCT ...)``. None of those can be exercised against a fake
session: an offline test can prove the SQL says what it should
(``tests/test_services_serving_history.py`` does exactly that) but only a real
database can prove that what it says produces the right number.

The matrix below is Task 79 §14's list, one test each, plus §15's regression:
that an authoritative state placing one Person in two ministries on one Sunday
is reported as a **hard conflict**, never counted as two valid services.
"""

from __future__ import annotations

import datetime

import pytest

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services.church_calendar import church_today
from app.services.serving_history import (
    find_cross_ministry_conflicts,
    recorded_serving_summaries,
    recorded_serving_totals,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc

#: The boundary every test measures against. Passed explicitly rather than
#: letting the service read the clock, so these tests do not start failing on
#: a particular calendar day.
TODAY = datetime.date(2026, 10, 1)
FINALIZED_AT = datetime.datetime(2026, 9, 20, tzinfo=UTC)

PAST_1 = datetime.date(2026, 9, 6)
PAST_2 = datetime.date(2026, 9, 13)
PAST_3 = datetime.date(2026, 9, 20)
FUTURE = datetime.date(2026, 10, 11)


class World:
    """One synthetic church with two ministries and two people.

    The second person exists in every test for one reason: to prove their rows
    never reach the first person's number. A leak between people is the kind of
    defect a single-subject fixture cannot see.
    """

    def __init__(self, session, *, timezone: str | None = None):
        self.session = session
        self.church = f.make_church(session)
        if timezone is not None:
            self.church.timezone = timezone
            session.flush()

        self.setup = f.make_ministry(session, church=self.church, name="SetupMin")
        self.av = f.make_ministry(session, church=self.church, name="AvMin")

        self.person = f.make_person(session, church=self.church, name="Subject")
        self.other = f.make_person(session, church=self.church, name="Bystander")

        self.setup_membership = f.make_membership(
            session, person=self.person, ministry=self.setup
        )
        self.av_membership = f.make_membership(
            session, person=self.person, ministry=self.av
        )
        self.other_setup_membership = f.make_membership(
            session, person=self.other, ministry=self.setup
        )

        self._periods: dict[int, object] = {}
        self._roles: dict[tuple[int, str], object] = {}
        self._versions: dict[tuple[int, int], object] = {}

    # -- fixture plumbing ---------------------------------------------------

    def _period(self, ministry):
        """One period per ministry, wide enough for every date in this file."""
        if ministry.id not in self._periods:
            self._periods[ministry.id] = f.make_period(
                session=self.session,
                ministry=ministry,
                start=datetime.date(2026, 1, 1),
                end=datetime.date(2026, 12, 31),
            )
        return self._periods[ministry.id]

    def _role(self, ministry, name):
        key = (ministry.id, name)
        if key not in self._roles:
            self._roles[key] = f.make_role(
                session=self.session, ministry=ministry, name=name
            )
        return self._roles[key]

    def _version(self, ministry, *, status, version_number=1):
        """One schedule per ministry, and versions numbered within it.

        Several versions of one schedule is how the "an amendment supersedes
        the earlier finalized version" case is built, which is why the schedule
        is cached per ministry rather than created per version.
        """
        key = (ministry.id, version_number)
        if key in self._versions:
            return self._versions[key]

        period = self._period(ministry)
        schedule = getattr(self, f"_schedule_{ministry.id}", None)
        if schedule is None:
            schedule = f.make_schedule(session=self.session, period=period)
            setattr(self, f"_schedule_{ministry.id}", schedule)

        version = f.make_version(
            session=self.session,
            schedule=schedule,
            period=period,
            version_number=version_number,
            status=status,
            finalized_at=(
                FINALIZED_AT if status == SCHEDULE_VERSION_STATUS_FINALIZED else None
            ),
        )
        self._versions[key] = version
        return version

    def serve(
        self,
        membership,
        ministry,
        date,
        *,
        status=SCHEDULE_VERSION_STATUS_FINALIZED,
        version_number=1,
        role_name="Role",
        cancelled=False,
        snapshot_date=None,
    ):
        """One assignment: event, requirement and assignment row.

        ``snapshot_date`` overrides the requirement's frozen date, which is how
        the "the event moved after finalization" case is built.
        """
        period = self._period(ministry)
        event = f.make_event(
            session=self.session, period=period, event_date=date, cancelled=cancelled
        )
        role = self._role(ministry, role_name)
        version = self._version(
            ministry, status=status, version_number=version_number
        )
        requirement = f.make_version_requirement(
            session=self.session,
            version=version,
            event=event,
            role=role,
            event_date=snapshot_date,
        )
        return f.make_assignment(
            session=self.session, requirement=requirement, membership=membership
        )

    # -- the questions under test -------------------------------------------

    def total(self):
        return recorded_serving_totals(
            self.session,
            person_ids=[self.person.id, self.other.id],
            church_id=self.church.id,
            as_of=TODAY,
        )[self.person.id]

    def summary(self, person=None):
        subject = self.person if person is None else person
        return recorded_serving_summaries(
            self.session,
            person_ids=[self.person.id, self.other.id],
            church_id=self.church.id,
            as_of=TODAY,
        )[subject.id]

    def breakdown(self):
        return {
            entry.ministry_name: entry.count for entry in self.summary().by_ministry
        }


@pytest.fixture
def world(db_session):
    return World(db_session)


# --------------------------------------------------------------------------
# §14's matrix, one test each
# --------------------------------------------------------------------------


class TestWhatCounts:
    def test_no_historical_assignments_is_zero(self, world):
        assert world.total() == 0
        assert world.summary().by_ministry == ()

    def test_one_authoritative_past_sunday_is_one(self, world):
        world.serve(world.setup_membership, world.setup, PAST_1)
        assert world.total() == 1

    def test_two_roles_in_one_ministry_on_one_sunday_is_one(self, world):
        """**ADR 0002's rule, read as a count.** The hard rule is one ministry
        per Sunday, not one event per Sunday, so somebody covering two roles at
        one service served that ministry on that date once. Counting Assignment
        rows would inflate exactly the ministries that need two people.
        """
        world.serve(world.setup_membership, world.setup, PAST_1, role_name="Lead")
        world.serve(world.setup_membership, world.setup, PAST_1, role_name="Chairs")
        assert world.total() == 1

    def test_two_events_of_one_ministry_on_one_sunday_is_one(self, world):
        """A ministry running an early and a late service on the same date is
        still one Sunday's serving for that person -- two Events, one
        occurrence."""
        world.serve(world.setup_membership, world.setup, PAST_1, role_name="Early")
        world.serve(world.setup_membership, world.setup, PAST_1, role_name="Late")
        assert world.total() == 1

    def test_several_past_sundays_sum(self, world):
        for date in (PAST_1, PAST_2, PAST_3):
            world.serve(world.setup_membership, world.setup, date)
        assert world.total() == 3

    def test_a_future_date_is_not_counted(self, world):
        """It has not happened. A commitment is not history."""
        world.serve(world.setup_membership, world.setup, FUTURE)
        assert world.total() == 0

    def test_today_itself_is_not_counted(self, world):
        """Strictly before today: a service happening this morning has not been
        served yet as far as this number is concerned, and a boundary that
        flipped mid-day would make the figure move without anything changing.
        """
        world.serve(world.setup_membership, world.setup, TODAY)
        assert world.total() == 0

    def test_a_draft_version_is_not_counted(self, world):
        """Nobody has agreed to a draft."""
        world.serve(
            world.setup_membership,
            world.setup,
            PAST_1,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        assert world.total() == 0

    def test_a_review_version_is_not_counted(self, world):
        """REVIEW is not authoritative: it is a draft somebody is looking at.
        """
        world.serve(
            world.setup_membership,
            world.setup,
            PAST_1,
            status=SCHEDULE_VERSION_STATUS_REVIEW,
        )
        assert world.total() == 0

    def test_a_cancelled_event_is_not_counted(self, world):
        """Nobody served a service that did not happen. ``cancelled_at`` is
        current state and is deliberately read live, unlike the date."""
        world.serve(world.setup_membership, world.setup, PAST_1, cancelled=True)
        assert world.total() == 0

    def test_a_superseded_finalized_version_is_not_counted(self, world):
        """ADR 0003: **the highest-numbered** FINALIZED version, not any of
        them. An amendment replaced what version 1 said, and counting both
        would credit somebody for an arrangement the schedule no longer shows.
        """
        world.serve(world.setup_membership, world.setup, PAST_1, version_number=1)
        world.serve(
            world.setup_membership, world.setup, PAST_2, version_number=2
        )
        assert world.total() == 1
        assert world.breakdown() == {world.setup.name: 1}

    def test_the_snapshot_date_decides_not_the_live_event(self, world):
        """A finalized version committed people to a date. Moving the event
        afterwards must not silently relocate that history -- here the event
        now sits in the future while the commitment was to a past Sunday.
        """
        world.serve(
            world.setup_membership,
            world.setup,
            FUTURE,
            snapshot_date=PAST_1,
        )
        assert world.total() == 1


class TestBreakdownAndIsolation:
    def test_two_ministries_on_different_sundays_give_a_total_and_a_breakdown(
        self, world
    ):
        world.serve(world.setup_membership, world.setup, PAST_1)
        world.serve(world.setup_membership, world.setup, PAST_2)
        world.serve(world.av_membership, world.av, PAST_3)

        summary = world.summary()
        assert summary.total == 3
        assert world.breakdown() == {world.setup.name: 2, world.av.name: 1}
        assert summary.same_date_conflicts == ()

    def test_only_ministries_with_history_appear(self, world):
        """The person is a member of both; only one has history."""
        world.serve(world.av_membership, world.av, PAST_1)
        assert world.breakdown() == {world.av.name: 1}

    def test_the_total_equals_the_sum_of_the_breakdown(self, world):
        world.serve(world.setup_membership, world.setup, PAST_1)
        world.serve(world.av_membership, world.av, PAST_2)
        summary = world.summary()
        assert summary.total == sum(entry.count for entry in summary.by_ministry)

    def test_another_persons_history_never_reaches_this_one(self, world):
        """The leak a single-subject fixture cannot see."""
        world.serve(world.other_setup_membership, world.setup, PAST_1)
        world.serve(world.other_setup_membership, world.setup, PAST_2)
        world.serve(world.setup_membership, world.setup, PAST_3)

        assert world.total() == 1
        assert world.summary(world.other).total == 2

    def test_somebody_with_no_history_is_still_in_the_answer(self, world):
        """Zero, never a missing key."""
        world.serve(world.setup_membership, world.setup, PAST_1)
        assert world.summary(world.other).total == 0


class TestChurchTimezone:
    """§13: the church's local date, never a naive UTC one."""

    def test_the_boundary_is_read_from_the_church_row(self, db_session):
        world = World(db_session, timezone="Pacific/Kiritimati")
        assert church_today(
            db_session, church_id=world.church.id
        ) == datetime.datetime.now(
            tz=__import__("zoneinfo").ZoneInfo("Pacific/Kiritimati")
        ).date()

    def test_two_churches_in_different_zones_can_disagree_about_today(
        self, db_session
    ):
        """Which is the entire reason the zone is read rather than assumed: a
        date is a statement about the reader's calendar.

        **Two days apart is legal, and the assertion has to allow it.**
        Kiritimati is UTC+14 and Niue is UTC-11 -- a spread of *twenty-five*
        hours, not twenty-four -- so for one hour of every UTC day (10:00 to
        10:59) Kiritimati has already reached tomorrow while Niue is still on
        yesterday. This used to assert ``in (0, 1)`` and therefore failed
        during exactly that hour, whatever the code did. The bound is the
        offset spread, and there is no wall-clock time at which these two zones
        are three days apart.
        """
        east = World(db_session, timezone="Pacific/Kiritimati")
        west = World(db_session, timezone="Pacific/Niue")
        east_today = church_today(db_session, church_id=east.church.id)
        west_today = church_today(db_session, church_id=west.church.id)
        assert (east_today - west_today).days in (0, 1, 2)

    def test_an_unrecognised_zone_falls_back_rather_than_failing(self, db_session):
        """A figure a few hours out at a midnight boundary is a far better
        failure than a 500 on a screen somebody opened."""
        world = World(db_session, timezone="Not/AZone")
        assert church_today(db_session, church_id=world.church.id) == (
            datetime.datetime.now(tz=UTC).date()
        )


# --------------------------------------------------------------------------
# §15: the church-wide hard rule, read backwards
# --------------------------------------------------------------------------


class TestSameSundayCrossMinistryIsAConflict:
    """**The regression §14 and §15 both ask for.**

    One canonical Person, memberships in two ministries, and an authoritative
    record placing them in both on one Sunday. That state violates the church's
    hard rule -- one Person serves at most one ministry on a Sunday -- and must
    therefore be reported as a conflict rather than normalized into two
    legitimate serving occurrences.

    Nothing here asserts the count is 1 *or* 2: the point is that the number
    alone is not the answer, and that the summary carries the evidence the
    record is wrong.
    """

    def test_it_is_reported_as_a_conflict(self, world):
        world.serve(world.setup_membership, world.setup, PAST_1)
        world.serve(world.av_membership, world.av, PAST_1)

        summary = world.summary()
        assert summary.has_conflicts is True
        assert len(summary.same_date_conflicts) == 1
        conflict = summary.same_date_conflicts[0]
        assert conflict.event_date == PAST_1
        assert set(conflict.ministry_names) == {world.setup.name, world.av.name}

    def test_the_conflict_names_both_ministries(self, world):
        """"They are in two ministries on 6 September" is actionable;
        "they have a conflict" is not."""
        world.serve(world.setup_membership, world.setup, PAST_1)
        world.serve(world.av_membership, world.av, PAST_1)

        found = find_cross_ministry_conflicts(
            world.session,
            person_ids=[world.person.id],
            church_id=world.church.id,
            as_of=TODAY,
        )
        assert len(found) == 1
        assert sorted(found[0].ministry_names) == sorted(
            [world.av.name, world.setup.name]
        )

    def test_a_healthy_two_ministry_record_reports_nothing(self, world):
        """The contrast that makes the test above mean something: the same
        person in the same two ministries on *different* Sundays is entirely
        legitimate and reports no conflict at all."""
        world.serve(world.setup_membership, world.setup, PAST_1)
        world.serve(world.av_membership, world.av, PAST_2)

        summary = world.summary()
        assert summary.has_conflicts is False
        assert summary.total == 2

    def test_a_draft_cross_ministry_overlap_is_not_reported(self, world):
        """Only the authoritative record can contradict itself. A draft that
        happens to double-book somebody is a draft, and the generation and
        finalization gates are what stop it becoming authoritative."""
        world.serve(world.setup_membership, world.setup, PAST_1)
        world.serve(
            world.av_membership,
            world.av,
            PAST_1,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        assert world.summary().has_conflicts is False

    def test_a_cancelled_cross_ministry_overlap_is_not_reported(self, world):
        """Nobody served the cancelled one, so there is nothing to conflict
        with."""
        world.serve(world.setup_membership, world.setup, PAST_1)
        world.serve(world.av_membership, world.av, PAST_1, cancelled=True)
        assert world.summary().has_conflicts is False


# --------------------------------------------------------------------------
# §14: the query budget, measured rather than asserted in a comment
# --------------------------------------------------------------------------


class TestQueryBudget:
    def test_a_page_of_people_costs_one_totals_query(self, db_session):
        """Not one per person, and not one per ministry."""
        world = World(db_session)
        extra = [
            f.make_person(db_session, church=world.church, name=f"Extra{i}")
            for i in range(20)
        ]
        world.serve(world.setup_membership, world.setup, PAST_1)

        statements: list[str] = []
        from sqlalchemy import event as sa_event

        connection = db_session.connection()

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        sa_event.listen(connection.engine, "before_cursor_execute", _record)
        try:
            recorded_serving_totals(
                db_session,
                person_ids=[world.person.id, *[p.id for p in extra]],
                church_id=world.church.id,
                as_of=TODAY,
            )
        finally:
            sa_event.remove(connection.engine, "before_cursor_execute", _record)

        assert len(statements) == 1

    def test_a_summary_costs_two_queries(self, db_session):
        world = World(db_session)
        world.serve(world.setup_membership, world.setup, PAST_1)
        world.serve(world.av_membership, world.av, PAST_2)

        statements: list[str] = []
        from sqlalchemy import event as sa_event

        connection = db_session.connection()

        def _record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        sa_event.listen(connection.engine, "before_cursor_execute", _record)
        try:
            recorded_serving_summaries(
                db_session,
                person_ids=[world.person.id, world.other.id],
                church_id=world.church.id,
                as_of=TODAY,
            )
        finally:
            sa_event.remove(connection.engine, "before_cursor_execute", _record)

        assert len(statements) == 2
