"""Integration Tests F and G -- ADR 0002/0003 against real relational rows.

Task 21's offline tests compiled the conflict query and read its SQL text.
They could not prove that PostgreSQL's ``DISTINCT ON`` actually resolves the
authoritative version, nor that the snapshot date really wins over the current
Event row. These tests run the real query over real rows and watch the answer
change as versions are finalized and the event is moved and cancelled.

Fixture state is set directly (statuses, ``finalized_at``, dates). No
finalization service exists yet, and inventing one to prepare a fixture would
be building production code for a test.
"""

from __future__ import annotations

import datetime

import pytest

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services.sunday_conflict import get_person_sunday_conflicts
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)
FINALIZED_AT = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _Scenario:
    """One person committed by an authoritative AV version, and a separate
    Setup ministry asking whether they are free."""

    def __init__(self, session, *, event_date=NOV_15):
        self.session = session
        self.church = f.make_church(session)
        self.av = f.make_ministry(session, church=self.church, name="AV")
        self.setup = f.make_ministry(session, church=self.church, name="Setup")
        self.person = f.make_person(session, church=self.church, name="Volunteer")
        self.membership = f.make_membership(session, person=self.person, ministry=self.av)
        self.role = f.make_role(session, ministry=self.av, name="Sound")
        self.period = f.make_period(session, ministry=self.av)
        self.event = f.make_event(session, period=self.period, event_date=event_date)
        self.schedule = f.make_schedule(session, period=self.period)
        self.version_1 = f.make_version(
            session, schedule=self.schedule, period=self.period, version_number=1,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT,
        )
        self.requirement_1 = f.make_version_requirement(
            session, version=self.version_1, event=self.event, role=self.role,
        )
        self.assignment_1 = f.make_assignment(
            session, requirement=self.requirement_1, membership=self.membership,
        )
        session.flush()

    def add_version_2(self, status, finalized_at=None):
        version = f.make_version(
            self.session, schedule=self.schedule, period=self.period,
            version_number=2, status=status, finalized_at=finalized_at,
        )
        self.session.flush()
        return version

    def is_blocked(self, conflict_date=NOV_15) -> bool:
        return get_person_sunday_conflicts(
            self.session, person_id=self.person.id, conflict_date=conflict_date,
            target_ministry_id=self.setup.id,
        ).is_blocked


# --------------------------------------------------------------------------
# Test F -- authoritative-version selection as versions succeed one another
# --------------------------------------------------------------------------


def test_f0_a_finalized_version_1_blocks_the_person(db_session):
    """The baseline: with only Version 1, finalized, the assignment counts."""
    scenario = _Scenario(db_session)

    result = get_person_sunday_conflicts(
        db_session, person_id=scenario.person.id, conflict_date=NOV_15,
        target_ministry_id=scenario.setup.id,
    )

    assert result.is_blocked is True
    assert len(result.authoritative_assignments) == 1
    assert result.authoritative_assignments[0].id == scenario.assignment_1.id
    assert result.existing_commitments == ()


def test_f1_a_newer_draft_does_not_displace_the_finalized_version(db_session):
    """Case 1 -- Version 2 is DRAFT and does not assign the person. Version 1
    is still the authoritative version, so the commitment still stands. A
    query that took "the latest version" would wrongly free the person here.
    """
    scenario = _Scenario(db_session)
    scenario.add_version_2(SCHEDULE_VERSION_STATUS_DRAFT)

    assert scenario.is_blocked() is True


def test_f2_a_newer_review_version_does_not_displace_it_either(db_session):
    """Case 2 -- REVIEW is still not agreed; Version 1 remains authoritative."""
    scenario = _Scenario(db_session)
    version_2 = scenario.add_version_2(SCHEDULE_VERSION_STATUS_DRAFT)

    version_2.status = SCHEDULE_VERSION_STATUS_REVIEW
    db_session.flush()

    assert scenario.is_blocked() is True


def test_f3_finalizing_version_2_makes_version_1s_assignment_stop_counting(db_session):
    """Case 3 -- Version 2 is FINALIZED and does not assign the person, so it
    supersedes Version 1. The old assignment is history, not a current claim.

    A query filtering merely on ``status = 'FINALIZED'`` would still find
    Version 1's row and keep blocking; only the ``DISTINCT ON (schedule_id)
    ... ORDER BY version_number DESC`` resolution gets this right, and this is
    where PostgreSQL actually proves it.
    """
    scenario = _Scenario(db_session)
    version_2 = scenario.add_version_2(SCHEDULE_VERSION_STATUS_DRAFT)

    version_2.status = SCHEDULE_VERSION_STATUS_FINALIZED
    version_2.finalized_at = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    db_session.flush()

    assert scenario.is_blocked() is False


def test_f4_the_transition_sequence_end_to_end_on_one_scenario(db_session):
    """All three cases in order on a single set of rows, so the *change* in
    the answer is what is being observed, not three unrelated fixtures.
    """
    scenario = _Scenario(db_session)
    assert scenario.is_blocked() is True  # only v1, finalized

    version_2 = scenario.add_version_2(SCHEDULE_VERSION_STATUS_DRAFT)
    assert scenario.is_blocked() is True  # v2 DRAFT

    version_2.status = SCHEDULE_VERSION_STATUS_REVIEW
    db_session.flush()
    assert scenario.is_blocked() is True  # v2 REVIEW

    version_2.status = SCHEDULE_VERSION_STATUS_FINALIZED
    version_2.finalized_at = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    db_session.flush()
    assert scenario.is_blocked() is False  # v2 authoritative, person unassigned


def test_f5_a_conflict_in_the_same_ministry_is_not_a_cross_ministry_conflict(db_session):
    """ADR 0002's scope check, against real rows: asking on behalf of the AV
    ministry itself must not report AV's own assignment as a conflict.
    """
    scenario = _Scenario(db_session)

    same_ministry = get_person_sunday_conflicts(
        db_session, person_id=scenario.person.id, conflict_date=NOV_15,
        target_ministry_id=scenario.av.id,
    )

    assert same_ministry.is_blocked is False


# --------------------------------------------------------------------------
# Test G -- the snapshot date wins; current cancellation still applies
# --------------------------------------------------------------------------


def test_g1_moving_the_current_event_does_not_move_the_historical_commitment(db_session):
    """schedule-output §13 / ADR 0003: the version committed people to
    15 November. Moving the Event row to the 22nd must not silently relocate
    that commitment to a date nobody agreed to.
    """
    scenario = _Scenario(db_session, event_date=NOV_15)
    assert scenario.requirement_1.event_date == NOV_15

    scenario.event.event_date = NOV_22
    db_session.flush()

    # The snapshot date still blocks...
    assert scenario.is_blocked(conflict_date=NOV_15) is True
    # ...and the new current date does not, merely because the Event moved.
    assert scenario.is_blocked(conflict_date=NOV_22) is False
    # The snapshot row itself was never touched.
    db_session.refresh(scenario.requirement_1)
    assert scenario.requirement_1.event_date == NOV_15


def test_g2_cancelling_the_current_event_stops_the_conflict(db_session):
    """The deliberate asymmetry: the *date* comes from the immutable snapshot,
    but ``cancelled_at`` is current state and is read live. A cancelled event
    means nobody is serving, so the commitment stops blocking.
    """
    scenario = _Scenario(db_session, event_date=NOV_15)
    assert scenario.is_blocked(conflict_date=NOV_15) is True

    scenario.event.cancelled_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()

    assert scenario.is_blocked(conflict_date=NOV_15) is False


def test_g3_both_rules_together_moved_and_cancelled(db_session):
    """Moved *and* cancelled: still no conflict, and the snapshot still says
    what it always said.
    """
    scenario = _Scenario(db_session, event_date=NOV_15)

    scenario.event.event_date = NOV_22
    db_session.flush()
    assert scenario.is_blocked(conflict_date=NOV_15) is True

    scenario.event.cancelled_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()
    assert scenario.is_blocked(conflict_date=NOV_15) is False
    assert scenario.is_blocked(conflict_date=NOV_22) is False

    db_session.refresh(scenario.requirement_1)
    assert scenario.requirement_1.event_date == NOV_15
