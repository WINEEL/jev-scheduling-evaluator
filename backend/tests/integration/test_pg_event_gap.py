"""The ministry event-gap rule against real PostgreSQL rows (Task 71).

The offline suite proves the rule's meaning with the reads stubbed. What only a
real database can show is that the pieces meet:

- the ``min_intervening_events > 0`` CHECK really refuses a stored zero, so
  "consecutive is allowed" keeps one representation;
- the surrounding-event queries really return the ministry's *nearest*
  non-cancelled events on each side, and really read *authoritative*
  assignments (ADR 0003) rather than any finalized row or any draft;
- and, end to end, that somebody who served the last event of a finalized
  previous quarter is genuinely not proposed for the first event of the next
  one -- and that somebody already published on the first event of the quarter
  *after* is not proposed for this one's last -- through the real builder, the
  real solver and the real writer, with nothing mocked.

Every test is rollback-isolated by the shared harness; nothing is committed.
Every person, ministry and date is synthetic.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
)
from app.models.scheduling_input import SchedulingPeriod
from app.scheduling.solver import SchedulingPolicy
from app.services.assignment import assign_member
from app.services.errors import InvalidOperationError
from app.services.event_gap import (
    MIN_EVENT_GAP_CONFLICT,
    get_min_intervening_events,
    load_adjacent_ministry_events,
    set_min_intervening_events,
)
from app.services.finalization_readiness import get_finalization_readiness
from app.services.schedule_generation import generate_draft_schedule
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
FINALIZED_AT = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

SEP_20 = datetime.date(2026, 9, 20)
SEP_27 = datetime.date(2026, 9, 27)
OCT_4 = datetime.date(2026, 10, 4)
OCT_11 = datetime.date(2026, 10, 11)
OCT_18 = datetime.date(2026, 10, 18)

LENIENT = SchedulingPolicy(allow_no_response=True)


# ==========================================================================
# The database's own guarantee
# ==========================================================================


def test_a_non_positive_gap_is_refused_by_the_database(db_session):
    """Zero would mean "consecutive assignments are allowed", which ``NULL``
    already says -- and one fact with two spellings is two things free to
    disagree.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="Setup")

    for bad in (0, -1):
        with pytest.raises(IntegrityError) as exc_info:
            with db_session.begin_nested():
                db_session.add(
                    SchedulingPeriod(
                        ministry_id=ministry.id,
                        name=f.unique("Q4 2026"),
                        start_date=OCT_4,
                        end_date=OCT_18,
                        min_intervening_events=bad,
                    )
                )
                db_session.flush()
        assert "min_intervening_events_positive" in str(exc_info.value.orig)


def test_an_unconfigured_period_stores_null(db_session):
    """Every period created before this rule existed reads as ``NULL``, and a
    new one does too unless a head asks otherwise.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="Setup")
    period = f.make_period(db_session, ministry=ministry)

    assert period.min_intervening_events is None
    assert get_min_intervening_events(
        db_session, scheduling_period_id=period.id
    ) is None


def test_setting_and_clearing_the_rule_round_trips(db_session):
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="Setup")
    head = f.make_ministry_head(
        db_session, church=church, ministry=ministry, name="Head"
    )
    period = f.make_period(db_session, ministry=ministry)

    set_min_intervening_events(
        db_session, actor=head, scheduling_period=period, min_intervening_events=2
    )
    db_session.flush()
    assert get_min_intervening_events(
        db_session, scheduling_period_id=period.id
    ) == 2

    set_min_intervening_events(
        db_session, actor=head, scheduling_period=period, min_intervening_events=None
    )
    db_session.flush()
    assert get_min_intervening_events(
        db_session, scheduling_period_id=period.id
    ) is None


def test_the_setter_refuses_a_zero_before_the_database_does(db_session):
    """A domain error naming the problem, rather than an ``IntegrityError`` at
    commit time from a constraint whose name means nothing to a caller.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="Setup")
    head = f.make_ministry_head(
        db_session, church=church, ministry=ministry, name="Head"
    )
    period = f.make_period(db_session, ministry=ministry)

    with pytest.raises(InvalidOperationError, match="must be positive"):
        set_min_intervening_events(
            db_session, actor=head, scheduling_period=period,
            min_intervening_events=0,
        )


# ==========================================================================
# The history query, against real rows
# ==========================================================================


class _History:
    """A previous quarter with three events, finalized, plus a new quarter.

    Everything the history loader has to get right is in here: order, the
    ``LIMIT``, cancellation, and whose assignments count.
    """

    def __init__(self, db_session):
        self.session = db_session
        self.church = f.make_church(db_session)
        self.ministry = f.make_ministry(db_session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            db_session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.role = f.make_role(db_session, ministry=self.ministry, name="Lead")

        self.previous = f.make_period(
            db_session, ministry=self.ministry, name="Q3 2026",
            start=SEP_20, end=SEP_27,
        )
        self.old_schedule = f.make_schedule(db_session, period=self.previous)
        self.old_version = f.make_version(
            db_session, schedule=self.old_schedule, period=self.previous,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT,
        )

        self.current = f.make_period(
            db_session, ministry=self.ministry, name="Q4 2026",
            start=OCT_4, end=OCT_18,
        )

    def member(self, name: str):
        person = f.make_person(self.session, church=self.church, name=name)
        membership = f.make_membership(
            self.session, person=person, ministry=self.ministry
        )
        f.make_qualification(
            self.session, membership=membership, role=self.role, decided_by=self.head
        )
        self.session.flush()
        return membership

    def previous_event(self, event_date, *, served_by=None, cancelled=False):
        event = f.make_event(
            self.session, period=self.previous, event_date=event_date,
            cancelled=cancelled,
        )
        requirement = f.make_version_requirement(
            self.session, version=self.old_version, event=event, role=self.role
        )
        if served_by is not None:
            f.make_assignment(
                self.session, requirement=requirement, membership=served_by
            )
        self.session.flush()
        return event

    def load(self, *, gap: int, before=OCT_4, after=OCT_18):
        """Both sides, against boundaries wide enough that the ids the database
        happened to assign cannot matter: ``0`` is below every real id and
        ``2**40`` is above every one.
        """
        return load_adjacent_ministry_events(
            self.session,
            ministry_id=self.ministry.id,
            min_intervening_events=gap,
            first_event_date=before,
            first_event_id=2**40,
            last_event_date=after,
            last_event_id=0,
        )

    def preceding(self, *, gap: int, before=OCT_4):
        return self.load(gap=gap, before=before)[0]


def test_the_history_returns_only_as_many_events_as_the_gap_needs(db_session):
    history = _History(db_session)
    history.previous_event(SEP_20)
    history.previous_event(SEP_27)

    assert [e.event_date for e in history.preceding(gap=1)] == [SEP_27]
    assert [e.event_date for e in history.preceding(gap=2)] == [SEP_20, SEP_27]
    # More than exists is not an error; there simply is no more.
    assert [e.event_date for e in history.preceding(gap=9)] == [SEP_20, SEP_27]


def test_the_history_carries_who_served_from_the_authoritative_version(db_session):
    history = _History(db_session)
    john = history.member("John")
    history.previous_event(SEP_27, served_by=john)

    (event,) = history.preceding(gap=1)

    assert event.assigned_membership_ids == frozenset({john.id})


def test_a_cancelled_event_is_neither_a_blocker_nor_an_intervening_event(db_session):
    """Consistent with every other scheduling path: a cancelled event means
    nobody is serving, so it drops out of the sequence entirely.
    """
    history = _History(db_session)
    history.previous_event(SEP_20)
    history.previous_event(SEP_27, cancelled=True)

    assert [e.event_date for e in history.preceding(gap=1)] == [SEP_20]


def test_a_draft_assignment_in_the_previous_period_blocks_nobody(db_session):
    """ADR 0003: a version nobody has agreed to does not speak for the church.
    The event still counts as intervening -- it simply carries no names.
    """
    history = _History(db_session)
    john = history.member("John")
    draft_version = f.make_version(
        db_session, schedule=history.old_schedule, period=history.previous,
        version_number=2, status=SCHEDULE_VERSION_STATUS_DRAFT,
    )
    event = f.make_event(db_session, period=history.previous, event_date=SEP_27)
    requirement = f.make_version_requirement(
        db_session, version=draft_version, event=event, role=history.role
    )
    f.make_assignment(db_session, requirement=requirement, membership=john)
    db_session.flush()

    (loaded,) = history.preceding(gap=1)

    assert loaded.event_id == event.id
    assert loaded.assigned_membership_ids == frozenset()


def test_another_ministrys_events_are_a_different_sequence(db_session):
    """The rule is ministry-scoped, and this is where that is a query rather
    than a promise.
    """
    history = _History(db_session)
    other_ministry = f.make_ministry(db_session, church=history.church, name="AV")
    other_period = f.make_period(db_session, ministry=other_ministry, name="AV Q3")
    f.make_event(db_session, period=other_period, event_date=SEP_27)
    db_session.flush()

    assert history.load(gap=2) == ((), ())


def test_the_following_side_returns_the_nearest_later_events(db_session):
    """The mirror of the preceding-side tests, against real rows: nearest
    first, limited by the gap, and ministry-scoped.
    """
    history = _History(db_session)
    later = f.make_period(
        db_session, ministry=history.ministry, name="Q1 2027",
        start=OCT_18 + datetime.timedelta(days=7),
        end=OCT_18 + datetime.timedelta(days=28),
    )
    for offset in (7, 14, 21):
        f.make_event(
            db_session, period=later,
            event_date=OCT_18 + datetime.timedelta(days=offset),
        )
    db_session.flush()

    _, following_one = history.load(gap=1)
    _, following_two = history.load(gap=2)

    assert [e.event_date for e in following_one] == [
        OCT_18 + datetime.timedelta(days=7)
    ]
    assert [e.event_date for e in following_two] == [
        OCT_18 + datetime.timedelta(days=7),
        OCT_18 + datetime.timedelta(days=14),
    ]


def test_a_cancelled_later_event_drops_out_of_the_following_side(db_session):
    history = _History(db_session)
    later = f.make_period(
        db_session, ministry=history.ministry, name="Q1 2027",
        start=OCT_18 + datetime.timedelta(days=7),
        end=OCT_18 + datetime.timedelta(days=28),
    )
    f.make_event(
        db_session, period=later, event_date=OCT_18 + datetime.timedelta(days=7),
        cancelled=True,
    )
    f.make_event(
        db_session, period=later, event_date=OCT_18 + datetime.timedelta(days=14)
    )
    db_session.flush()

    _, following = history.load(gap=1)

    assert [e.event_date for e in following] == [
        OCT_18 + datetime.timedelta(days=14)
    ]


# ==========================================================================
# End to end: generation across a period boundary
# ==========================================================================


class _Quarter:
    """A new quarter with three Sundays and one position each, preceded by a
    finalized event the gap rule has to see.
    """

    def __init__(self, db_session, *, gap: int | None = 1):
        self.session = db_session
        self.church = f.make_church(db_session)
        self.ministry = f.make_ministry(db_session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            db_session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.role = f.make_role(db_session, ministry=self.ministry, name="Lead")

        # The previous quarter: one finalized event on 27 September.
        self.previous = f.make_period(
            db_session, ministry=self.ministry, name="Q3 2026",
            start=SEP_20, end=SEP_27,
        )
        previous_schedule = f.make_schedule(db_session, period=self.previous)
        self.previous_version = f.make_version(
            db_session, schedule=previous_schedule, period=self.previous,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT,
        )
        self.previous_event = f.make_event(
            db_session, period=self.previous, event_date=SEP_27
        )
        self.previous_requirement = f.make_version_requirement(
            db_session, version=self.previous_version,
            event=self.previous_event, role=self.role,
        )

        # The quarter being scheduled.
        self.period = f.make_period(
            db_session, ministry=self.ministry, name="Q4 2026",
            start=OCT_4, end=OCT_18,
        )
        self.period.min_intervening_events = gap
        self.events = [
            f.make_event(db_session, period=self.period, event_date=date)
            for date in (OCT_4, OCT_11, OCT_18)
        ]
        for event in self.events:
            f.make_staffing_requirement(
                db_session, event=event, role=self.role, required_count=1
            )

        self.schedule = f.make_schedule(db_session, period=self.period)
        self.version = f.make_version(
            db_session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.requirements = [
            f.make_version_requirement(
                db_session, version=self.version, event=event, role=self.role
            )
            for event in self.events
        ]
        db_session.flush()

    def member(self, name: str):
        person = f.make_person(self.session, church=self.church, name=name)
        membership = f.make_membership(
            self.session, person=person, ministry=self.ministry
        )
        f.make_qualification(
            self.session, membership=membership, role=self.role, decided_by=self.head
        )
        self.session.flush()
        return membership

    def served_previous_quarter(self, membership):
        f.make_assignment(
            self.session, requirement=self.previous_requirement, membership=membership
        )
        self.session.flush()

    def _next_quarter(self, *, finalized: bool):
        """The quarter after this one, holding a single event one week past the
        window's last. Built lazily, so the tests that never mention it pay
        nothing and the forward side stays genuinely absent for them.
        """
        if getattr(self, "_next_requirement", None) is None:
            period = f.make_period(
                self.session, ministry=self.ministry, name="Q1 2027",
                start=OCT_18 + datetime.timedelta(days=7),
                end=OCT_18 + datetime.timedelta(days=14),
            )
            event = f.make_event(
                self.session, period=period,
                event_date=OCT_18 + datetime.timedelta(days=7),
            )
            schedule = f.make_schedule(self.session, period=period)
            version = f.make_version(
                self.session, schedule=schedule, period=period,
                status=(
                    SCHEDULE_VERSION_STATUS_FINALIZED if finalized
                    else SCHEDULE_VERSION_STATUS_DRAFT
                ),
                finalized_at=FINALIZED_AT if finalized else None,
            )
            self._next_requirement = f.make_version_requirement(
                self.session, version=version, event=event, role=self.role
            )
            self.next_event_date = event.event_date
        return self._next_requirement

    def published_next_quarter(self, membership):
        """An **authoritative** commitment at the event after this window."""
        f.make_assignment(
            self.session, requirement=self._next_quarter(finalized=True),
            membership=membership,
        )
        self.session.flush()

    def drafted_next_quarter(self, membership):
        """A commitment nobody has agreed to yet, at the same event."""
        f.make_assignment(
            self.session, requirement=self._next_quarter(finalized=False),
            membership=membership,
        )
        self.session.flush()

    def generate(self):
        return generate_draft_schedule(
            self.session, actor=self.head, version=self.version, policy=LENIENT
        )

    def dates_for(self, membership_id: int, result) -> list[datetime.date]:
        by_id = {r.id: r for r in self.requirements}
        return sorted(
            by_id[a.schedule_version_requirement_id].event_date
            for a in result.created_assignments
            if a.ministry_membership_id == membership_id
        )


def test_generation_never_places_one_person_on_consecutive_events(db_session):
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")

    result = quarter.generate()

    assert quarter.dates_for(john.id, result) == [OCT_4, OCT_18]
    assert result.is_complete is False


def test_the_previous_quarters_last_event_blocks_the_new_quarters_first(db_session):
    """The whole reason the history is loaded at all, proven with real rows: a
    finalized assignment in Q3 keeps John off the first Sunday of Q4.
    """
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    quarter.served_previous_quarter(john)

    result = quarter.generate()

    assert OCT_4 not in quarter.dates_for(john.id, result)


def test_without_the_rule_the_same_schedule_fills_completely(db_session):
    """The legacy guarantee, against the same rows: an unconfigured period
    behaves exactly as it did before Task 71.
    """
    quarter = _Quarter(db_session, gap=None)
    john = quarter.member("John")
    quarter.served_previous_quarter(john)

    result = quarter.generate()

    assert quarter.dates_for(john.id, result) == [OCT_4, OCT_11, OCT_18]
    assert result.is_complete is True


def test_enough_people_fill_every_position_under_the_rule(db_session):
    quarter = _Quarter(db_session, gap=1)
    quarter.member("John")
    quarter.member("Mary")

    result = quarter.generate()

    assert result.is_complete is True
    assert result.created_count == 3


def test_a_manual_assignment_on_the_next_event_is_refused(db_session):
    """Manual assignment applies the same rule from its own reads, and the
    refusal names it rather than quietly succeeding.
    """
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    assign_member(
        db_session, actor=quarter.head,
        requirement=quarter.requirements[0], membership=john,
    )
    db_session.flush()

    with pytest.raises(InvalidOperationError, match=MIN_EVENT_GAP_CONFLICT):
        assign_member(
            db_session, actor=quarter.head,
            requirement=quarter.requirements[1], membership=john,
        )


def test_a_manual_assignment_two_events_later_is_allowed(db_session):
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    assign_member(
        db_session, actor=quarter.head,
        requirement=quarter.requirements[0], membership=john,
    )
    db_session.flush()

    assignment = assign_member(
        db_session, actor=quarter.head,
        requirement=quarter.requirements[2], membership=john,
    )

    assert assignment.ministry_membership_id == john.id


def test_manual_assignment_is_refused_across_the_period_boundary(db_session):
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    quarter.served_previous_quarter(john)

    with pytest.raises(InvalidOperationError, match=MIN_EVENT_GAP_CONFLICT):
        assign_member(
            db_session, actor=quarter.head,
            requirement=quarter.requirements[0], membership=john,
        )


def test_tightening_the_rule_after_a_draft_blocks_finalization_without_deleting(
    db_session,
):
    """The safety property that makes the rule changeable mid-draft: a schedule
    built with no rule keeps every assignment, and simply stops being
    finalizable until a head acts.
    """
    quarter = _Quarter(db_session, gap=None)
    john = quarter.member("John")
    result = quarter.generate()
    assert quarter.dates_for(john.id, result) == [OCT_4, OCT_11, OCT_18]

    set_min_intervening_events(
        db_session, actor=quarter.head, scheduling_period=quarter.period,
        min_intervening_events=1,
    )
    db_session.flush()

    readiness = get_finalization_readiness(db_session, version=quarter.version)

    assert MIN_EVENT_GAP_CONFLICT in {issue.code for issue in readiness.issues}
    # Nothing was deleted to make the new rule true.
    assert len(result.created_assignments) == 3


def test_a_schedule_that_respects_the_rule_reports_no_gap_issue(db_session):
    quarter = _Quarter(db_session, gap=1)
    quarter.member("John")
    quarter.member("Mary")
    quarter.generate()
    db_session.flush()

    readiness = get_finalization_readiness(db_session, version=quarter.version)

    assert MIN_EVENT_GAP_CONFLICT not in {issue.code for issue in readiness.issues}


# ==========================================================================
# End to end: the forward boundary
# ==========================================================================


def test_the_next_quarters_first_event_blocks_this_quarters_last(db_session):
    """The correction this continuation exists for, proven with real rows: a
    finalized assignment in Q1 keeps John off the last Sunday of Q4.
    """
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    quarter.published_next_quarter(john)

    result = quarter.generate()

    placed = quarter.dates_for(john.id, result)
    assert OCT_18 not in placed
    # The first two Sundays are adjacent to each other, so exactly one of them
    # is reachable; which one is a tie the solver may break either way.
    assert placed in ([OCT_4], [OCT_11])


def test_a_draft_in_the_next_quarter_blocks_nobody(db_session):
    """ADR 0003 on the forward side, which is where it matters most: a version
    nobody has agreed to must not constrain the quarter being scheduled.
    """
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    quarter.drafted_next_quarter(john)

    result = quarter.generate()

    assert OCT_18 in quarter.dates_for(john.id, result)


def test_no_schedule_at_all_in_the_next_quarter_blocks_nobody(db_session):
    """The event exists and is part of the sequence; it simply carries no
    names, so it separates without blocking.
    """
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    quarter._next_quarter(finalized=True)  # the event, with nobody on it
    db_session.flush()

    result = quarter.generate()

    assert OCT_18 in quarter.dates_for(john.id, result)


def test_manual_assignment_is_refused_across_the_forward_boundary(db_session):
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    quarter.published_next_quarter(john)

    with pytest.raises(InvalidOperationError, match=MIN_EVENT_GAP_CONFLICT):
        assign_member(
            db_session, actor=quarter.head,
            requirement=quarter.requirements[-1], membership=john,
        )


def test_manual_assignment_two_events_before_the_boundary_is_allowed(db_session):
    """The forward reach is the configured gap, not "the rest of the period"."""
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    quarter.published_next_quarter(john)

    assignment = assign_member(
        db_session, actor=quarter.head,
        requirement=quarter.requirements[1], membership=john,
    )

    assert assignment.ministry_membership_id == john.id


def test_readiness_reports_a_forward_boundary_conflict(db_session):
    """A schedule built before the next quarter was published becomes
    unfinalizable once it is -- reported, with nothing deleted.
    """
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    assign_member(
        db_session, actor=quarter.head,
        requirement=quarter.requirements[-1], membership=john,
    )
    db_session.flush()

    quarter.published_next_quarter(john)

    readiness = get_finalization_readiness(db_session, version=quarter.version)

    assert MIN_EVENT_GAP_CONFLICT in {issue.code for issue in readiness.issues}


def test_both_boundaries_bind_in_one_generation_run(db_session):
    """Committed on both sides with a gap of one: John may take only the middle
    Sunday of the three.
    """
    quarter = _Quarter(db_session, gap=1)
    john = quarter.member("John")
    quarter.served_previous_quarter(john)
    quarter.published_next_quarter(john)

    result = quarter.generate()

    assert quarter.dates_for(john.id, result) == [OCT_11]


def test_without_the_rule_the_forward_boundary_is_inert(db_session):
    """The legacy guarantee on this side too."""
    quarter = _Quarter(db_session, gap=None)
    john = quarter.member("John")
    quarter.published_next_quarter(john)

    result = quarter.generate()

    assert quarter.dates_for(john.id, result) == [OCT_4, OCT_11, OCT_18]
