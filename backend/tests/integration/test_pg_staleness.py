"""Integration Test H -- Task 23's staleness comparison over real rows.

The offline suite compiled the two SELECTs and compared hand-built
fingerprint sets. What it could not do is run both queries against one real
database and watch a version go stale when someone edits the current staffing
configuration underneath it.

Nothing here mutates a snapshot row to "fix" staleness -- the snapshot is
immutable historical state, and the whole point is that it stays put while
current input moves.
"""

from __future__ import annotations

import datetime

import pytest

from app.models.schedule_output import SCHEDULE_VERSION_STATUS_DRAFT
from app.services.schedule_staleness import (
    RequirementFingerprint,
    get_schedule_version_staleness,
)
from app.services.staffing_requirement import set_staffing_requirement
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)


class _Scenario:
    """One event, one role, one current requirement, and a snapshot that
    matches it exactly."""

    def __init__(self, session, *, required_count: int = 1):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        # The actor for the three tests below that change staffing through the
        # real service: since Task 80 only this ministry's head may.
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.role = f.make_role(session, ministry=self.ministry, name="Setup Lead")
        self.period = f.make_period(session, ministry=self.ministry)
        self.event = f.make_event(session, period=self.period, event_date=NOV_15)
        self.staffing = f.make_staffing_requirement(
            session, event=self.event, role=self.role, required_count=required_count,
        )
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.snapshot = f.make_version_requirement(
            session, version=self.version, event=self.event, role=self.role,
            required_count=required_count,
        )
        session.flush()

    def staleness(self):
        return get_schedule_version_staleness(self.session, version=self.version)

    def fingerprint(self, *, event_date=NOV_15, required_count=1):
        return RequirementFingerprint(
            event_id=self.event.id, event_date=event_date,
            ministry_role_id=self.role.id, required_count=required_count,
        )


def test_h1_a_snapshot_matching_current_input_is_fresh(db_session):
    scenario = _Scenario(db_session)

    result = scenario.staleness()

    assert result.is_stale is False
    assert result.current_only == frozenset()
    assert result.snapshot_only == frozenset()
    assert result.current_requirements == frozenset({scenario.fingerprint()})
    assert result.snapshot_requirements == result.current_requirements


def test_h2_changing_required_count_makes_the_version_stale(db_session):
    scenario = _Scenario(db_session, required_count=1)
    assert scenario.staleness().is_stale is False

    scenario.staffing.required_count = 2
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is True
    assert result.snapshot_only == frozenset({scenario.fingerprint(required_count=1)})
    assert result.current_only == frozenset({scenario.fingerprint(required_count=2)})


def test_h3_the_snapshot_row_is_untouched_by_the_current_change(db_session):
    scenario = _Scenario(db_session, required_count=1)
    scenario.staffing.required_count = 5
    db_session.flush()

    db_session.refresh(scenario.snapshot)
    assert scenario.snapshot.required_count == 1  # history, not current config
    assert scenario.staleness().is_stale is True


def test_h4_moving_the_event_makes_the_version_stale_with_both_dates(db_session):
    scenario = _Scenario(db_session)
    assert scenario.staleness().is_stale is False

    scenario.event.event_date = NOV_22
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is True
    # The old date survives only in the snapshot; the new one only in current.
    assert result.snapshot_only == frozenset({scenario.fingerprint(event_date=NOV_15)})
    assert result.current_only == frozenset({scenario.fingerprint(event_date=NOV_22)})


def test_h5_restoring_the_date_makes_it_fresh_again(db_session):
    """Staleness is a comparison, not a latch -- undoing the change restores
    freshness, because nothing was recorded anywhere.
    """
    scenario = _Scenario(db_session)
    scenario.event.event_date = NOV_22
    db_session.flush()
    assert scenario.staleness().is_stale is True

    scenario.event.event_date = NOV_15
    db_session.flush()

    assert scenario.staleness().is_stale is False


def test_h6_deleting_the_current_requirement_leaves_a_snapshot_only_difference(db_session):
    scenario = _Scenario(db_session)

    db_session.delete(scenario.staffing)
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is True
    assert result.snapshot_only == frozenset({scenario.fingerprint()})
    assert result.current_only == frozenset()


def test_h7_adding_a_current_requirement_leaves_a_current_only_difference(db_session):
    scenario = _Scenario(db_session)
    second_role = f.make_role(db_session, ministry=scenario.ministry, name="Setup Assist")

    f.make_staffing_requirement(
        db_session, event=scenario.event, role=second_role, required_count=2,
    )
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is True
    assert result.snapshot_only == frozenset()
    assert result.current_only == frozenset({
        RequirementFingerprint(
            event_id=scenario.event.id, event_date=NOV_15,
            ministry_role_id=second_role.id, required_count=2,
        )
    })


def test_h8_cancelling_the_event_drops_it_from_current_but_not_from_the_snapshot(db_session):
    """The asymmetry Task 23 was careful about: cancelled events leave the
    current set only. If the snapshot side were filtered by ``cancelled_at``
    too, both sides would empty out and this version would report *fresh*.
    """
    scenario = _Scenario(db_session)

    scenario.event.cancelled_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is True
    assert result.current_requirements == frozenset()
    assert result.snapshot_only == frozenset({scenario.fingerprint()})


def test_h9_another_periods_requirements_are_not_counted(db_session):
    """Scoping, over real rows: a different period's staffing must not leak
    into this version's current set.
    """
    scenario = _Scenario(db_session)
    other_period = f.make_period(db_session, ministry=scenario.ministry, name="Q1 2027")
    other_event = f.make_event(
        db_session, period=other_period, event_date=datetime.date(2027, 1, 3),
    )
    f.make_staffing_requirement(
        db_session, event=other_event, role=scenario.role, required_count=4,
    )
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is False
    assert result.current_requirements == frozenset({scenario.fingerprint()})


# --------------------------------------------------------------------------
# Task 54 -- the same behavior, reached through the real service the new
# staffing-requirement API calls, not by mutating the row directly.
# --------------------------------------------------------------------------


def test_h10_a_real_service_call_to_change_the_count_makes_the_version_stale(db_session):
    """H2, but through :func:`set_staffing_requirement` end to end -- the same
    function ``PUT /events/{id}/staffing-requirements/{role_id}`` calls. Task
    54 added no staleness logic of its own; this pins that the existing
    architecture already sees a service-made change without any change here.
    """
    scenario = _Scenario(db_session, required_count=1)
    assert scenario.staleness().is_stale is False

    set_staffing_requirement(
        db_session, actor=scenario.head, event=scenario.event, role=scenario.role,
        required_count=3,
    )
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is True
    assert result.snapshot_only == frozenset({scenario.fingerprint(required_count=1)})
    assert result.current_only == frozenset({scenario.fingerprint(required_count=3)})


def test_h11_a_real_service_call_to_add_a_new_role_requirement_makes_the_version_stale(
    db_session,
):
    """H7, but through the service: a brand-new requirement for a role the
    snapshot never knew about, created via ``set_staffing_requirement``
    rather than the factory helper.
    """
    scenario = _Scenario(db_session)
    new_role = f.make_role(db_session, ministry=scenario.ministry, name="Slides")

    set_staffing_requirement(
        db_session, actor=scenario.head, event=scenario.event, role=new_role, required_count=1,
    )
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is True
    assert result.current_only == frozenset(
        {
            RequirementFingerprint(
                event_id=scenario.event.id, event_date=NOV_15,
                ministry_role_id=new_role.id, required_count=1,
            )
        }
    )


def test_h12_clearing_a_requirement_through_the_service_makes_the_version_stale(db_session):
    """H6, but through the service's ``required_count=0`` removal path -- the
    same operation ``DELETE /events/{id}/staffing-requirements/{role_id}``
    performs.
    """
    scenario = _Scenario(db_session, required_count=1)
    assert scenario.staleness().is_stale is False

    set_staffing_requirement(
        db_session, actor=scenario.head, event=scenario.event, role=scenario.role,
        required_count=0,
    )
    db_session.flush()

    result = scenario.staleness()
    assert result.is_stale is True
    assert result.current_only == frozenset()
    assert result.snapshot_only == frozenset({scenario.fingerprint(required_count=1)})
