"""Successor ScheduleVersion creation against real PostgreSQL rows (Task 28).

The offline suite proves the control flow with the reads stubbed. What only a
real database can show is the point of the whole operation: that the successor
snapshot genuinely *diverges* from its source's -- Version 1 keeps the count
and date it was built against while Version 2 captures today's -- and that
creating a DRAFT successor moves no authority at all until it is itself
finalized.

Nothing is mocked: Task 23's staleness comparison and Task 21's ``DISTINCT ON``
conflict query are both run unmocked over real rows, and the source version's
immutability is checked by asking the real Task 24/27 lifecycle operations.

Every test is rollback-isolated by the shared harness; nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text

from app.models.schedule_output import (
    ScheduleVersionRequirement,
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services.assignment import assign_member, remove_assignment
from app.services.audit import ACTION_SCHEDULE_VERSION_CREATED
from app.services.errors import InvalidOperationError
from app.services.schedule_lifecycle import (
    finalize_schedule_version,
    submit_schedule_version_for_review,
)
from app.services.schedule_staleness import get_schedule_version_staleness
from app.services.schedule_version import create_successor_schedule_version
from app.services.sunday_conflict import get_person_sunday_conflicts
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)
V1_FINALIZED_AT = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _Scenario:
    """A FINALIZED Version 1 whose snapshot matches current input, with one
    qualified member assigned to its single requirement.
    """

    def __init__(self, session, *, required_count: int = 1, assign: bool = True):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
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
        self.version_1 = f.make_version(
            session, schedule=self.schedule, period=self.period, version_number=1,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=V1_FINALIZED_AT,
        )
        self.requirement_1 = f.make_version_requirement(
            session, version=self.version_1, event=self.event, role=self.role,
            required_count=required_count,
        )
        self.person = f.make_person(session, church=self.church, name="Volunteer")
        self.membership = f.make_membership(
            session, person=self.person, ministry=self.ministry,
        )
        f.make_qualification(
            session, membership=self.membership, role=self.role,
            decided_by=self.head, is_qualified=True,
        )
        session.flush()
        self.assignment_1 = None
        if assign:
            self.assignment_1 = f.make_assignment(
                session, requirement=self.requirement_1, membership=self.membership,
            )
            session.flush()

    def create_successor(self, *, reason: str = "Staffing requirements changed", notes=None):
        version = create_successor_schedule_version(
            self.session, actor=self.head, source_version=self.version_1,
            amendment_reason=reason, notes=notes,
        )
        self.session.flush()
        return version


def _stored_version(session, version_id: int):
    return session.execute(
        text(
            "SELECT status, finalized_at, version_number, amends_version_id,"
            "       amendment_reason, notes, schedule_id, scheduling_period_id"
            " FROM schedule_version WHERE id = :id"
        ),
        {"id": version_id},
    ).one()


def _snapshot(session, version_id: int):
    return session.execute(
        text(
            "SELECT event_id, event_date, ministry_role_id, required_count"
            " FROM schedule_version_requirement WHERE schedule_version_id = :v"
            " ORDER BY id"
        ),
        {"v": version_id},
    ).all()


def _assignment_count(session, version_id: int) -> int:
    return session.execute(
        text("SELECT count(*) FROM assignment WHERE schedule_version_id = :v"),
        {"v": version_id},
    ).scalar_one()


# --------------------------------------------------------------------------
# A -- a successor to a FINALIZED version
# --------------------------------------------------------------------------


def test_a_a_finalized_version_spawns_a_draft_successor_with_no_assignments(db_session):
    scenario = _Scenario(db_session)

    v2 = scenario.create_successor(reason="Correcting the November 15 assignment")

    stored_v2 = _stored_version(db_session, v2.id)
    assert stored_v2.status == SCHEDULE_VERSION_STATUS_DRAFT
    assert stored_v2.finalized_at is None
    assert stored_v2.version_number == 2
    assert stored_v2.amends_version_id == scenario.version_1.id
    assert stored_v2.amendment_reason == "Correcting the November 15 assignment"
    assert stored_v2.schedule_id == scenario.schedule.id
    assert stored_v2.scheduling_period_id == scenario.period.id

    # The successor starts empty: no assignment was carried forward.
    assert _assignment_count(db_session, v2.id) == 0
    assert _assignment_count(db_session, scenario.version_1.id) == 1

    # Version 1 is untouched in every respect.
    stored_v1 = _stored_version(db_session, scenario.version_1.id)
    assert stored_v1.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert stored_v1.finalized_at == V1_FINALIZED_AT
    assert stored_v1.amends_version_id is None
    assert stored_v1.amendment_reason is None


def test_a2_exactly_one_creation_audit_row_with_the_lineage_payload(db_session):
    scenario = _Scenario(db_session)

    v2 = scenario.create_successor(reason="Event date changed", notes="Second attempt.")

    rows = db_session.execute(
        text(
            "SELECT summary, reason, ministry_id, before_values, after_values"
            " FROM audit_event WHERE target_table = 'schedule_version'"
            "   AND target_id = :id AND action = :action"
        ),
        {"id": v2.id, "action": ACTION_SCHEDULE_VERSION_CREATED},
    ).all()
    assert len(rows) == 1
    audit = rows[0]
    assert audit.ministry_id == scenario.ministry.id
    assert audit.before_values is None
    # The standing amendment_reason is not duplicated into the audit reason.
    assert audit.reason is None
    assert audit.after_values["amends_version_id"] == scenario.version_1.id
    assert audit.after_values["amendment_reason"] == "Event date changed"
    assert audit.after_values["version_number"] == 2
    assert audit.after_values["status"] == "DRAFT"
    assert audit.after_values["notes"] == "Second attempt."
    assert audit.after_values["requirement_snapshot_count"] == 1
    assert scenario.ministry.name in audit.summary
    assert scenario.period.name in audit.summary
    assert audit.summary.startswith("Created draft version 2 for")


@pytest.mark.parametrize(
    "status,finalized_at",
    [
        (SCHEDULE_VERSION_STATUS_DRAFT, None),
        (SCHEDULE_VERSION_STATUS_REVIEW, None),
        (SCHEDULE_VERSION_STATUS_FINALIZED, V1_FINALIZED_AT),
    ],
)
def test_a3_any_recognized_source_status_spawns_a_successor(db_session, status, finalized_at):
    scenario = _Scenario(db_session, assign=False)
    scenario.version_1.status = status
    scenario.version_1.finalized_at = finalized_at
    db_session.flush()

    v2 = scenario.create_successor()

    assert _stored_version(db_session, v2.id).status == SCHEDULE_VERSION_STATUS_DRAFT
    # The source keeps the status it had.
    assert _stored_version(db_session, scenario.version_1.id).status == status


# --------------------------------------------------------------------------
# B -- a changed required_count, and what Task 23 then says about each version
# --------------------------------------------------------------------------


def test_b_the_successor_captures_the_new_count_while_version_1_keeps_the_old(db_session):
    scenario = _Scenario(db_session, required_count=1)
    assert get_schedule_version_staleness(
        db_session, version=scenario.version_1
    ).is_stale is False

    # Current staffing changes after Version 1 was built.
    scenario.staffing.required_count = 2
    db_session.flush()

    # Version 1 is now stale against current input -- which is the reason to
    # create a successor at all.
    assert get_schedule_version_staleness(
        db_session, version=scenario.version_1
    ).is_stale is True

    v2 = scenario.create_successor(reason="Staffing requirements changed")

    v1_snapshot = _snapshot(db_session, scenario.version_1.id)
    v2_snapshot = _snapshot(db_session, v2.id)
    assert [r.required_count for r in v1_snapshot] == [1]  # history preserved
    assert [r.required_count for r in v2_snapshot] == [2]  # current captured

    # And the successor is fresh, while Version 1 remains stale.
    assert get_schedule_version_staleness(db_session, version=v2).is_stale is False
    assert get_schedule_version_staleness(
        db_session, version=scenario.version_1
    ).is_stale is True


# --------------------------------------------------------------------------
# C -- a moved event
# --------------------------------------------------------------------------


def test_c_the_successor_captures_the_new_event_date(db_session):
    scenario = _Scenario(db_session)

    scenario.event.event_date = NOV_22
    db_session.flush()

    v2 = scenario.create_successor(reason="Event date changed")

    assert [r.event_date for r in _snapshot(db_session, scenario.version_1.id)] == [NOV_15]
    assert [r.event_date for r in _snapshot(db_session, v2.id)] == [NOV_22]


def test_c2_a_cancelled_events_requirement_is_absent_from_the_successor(db_session):
    scenario = _Scenario(db_session)
    other_role = f.make_role(db_session, ministry=scenario.ministry, name="Setup Assist")
    other_event = f.make_event(db_session, period=scenario.period, event_date=NOV_22)
    f.make_staffing_requirement(db_session, event=other_event, role=other_role)
    db_session.flush()

    scenario.event.cancelled_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()

    v2 = scenario.create_successor(reason="Event cancelled")

    v2_snapshot = _snapshot(db_session, v2.id)
    assert [r.event_id for r in v2_snapshot] == [other_event.id]
    # Version 1 still records the cancelled event, as history.
    assert [r.event_id for r in _snapshot(db_session, scenario.version_1.id)] == [
        scenario.event.id
    ]


def test_c3_zero_current_requirements_still_creates_the_successor(db_session):
    scenario = _Scenario(db_session, assign=False)
    db_session.delete(scenario.staffing)
    db_session.flush()

    v2 = scenario.create_successor(reason="All requirements removed")

    assert _stored_version(db_session, v2.id).version_number == 2
    assert _snapshot(db_session, v2.id) == []


# --------------------------------------------------------------------------
# D -- creating a successor moves no authority
# --------------------------------------------------------------------------


def test_d_a_draft_successor_does_not_supersede_the_finalized_version(db_session):
    """Version 1 FINALIZED commits the volunteer; another ministry asks whether
    they are free. Creating Version 2 as a DRAFT must change nothing --
    authority is the highest-numbered *FINALIZED* version (ADR 0003), and only
    finalizing Version 2 (Task 27) would move it.
    """
    scenario = _Scenario(db_session)
    asking_ministry = f.make_ministry(db_session, church=scenario.church, name="AV")
    db_session.flush()

    def volunteer_is_blocked() -> bool:
        return get_person_sunday_conflicts(
            db_session, person_id=scenario.person.id, conflict_date=NOV_15,
            target_ministry_id=asking_ministry.id,
        ).is_blocked

    assert volunteer_is_blocked() is True

    v2 = scenario.create_successor(reason="Amending")

    # Still blocked: Version 1 remains authoritative.
    assert volunteer_is_blocked() is True
    assert _assignment_count(db_session, v2.id) == 0

    # Submitting Version 2 for review changes nothing either.
    submit_schedule_version_for_review(db_session, actor=scenario.head, version=v2)
    db_session.flush()
    assert _stored_version(db_session, v2.id).status == SCHEDULE_VERSION_STATUS_REVIEW
    assert volunteer_is_blocked() is True

    # Version 2 must be *staffed* before it can be finalized (Task 26) -- by
    # someone else, since the point is that the volunteer is no longer on it.
    replacement = f.make_person(db_session, church=scenario.church, name="Replacement")
    replacement_membership = f.make_membership(
        db_session, person=replacement, ministry=scenario.ministry,
    )
    f.make_qualification(
        db_session, membership=replacement_membership, role=scenario.role,
        decided_by=scenario.head, is_qualified=True,
    )
    db_session.flush()
    requirement_2 = db_session.execute(
        select(ScheduleVersionRequirement).where(
            ScheduleVersionRequirement.schedule_version_id == v2.id
        )
    ).scalar_one()
    assign_member(
        db_session, actor=scenario.head, requirement=requirement_2,
        membership=replacement_membership,
    )
    db_session.flush()
    # Still Version 1's world until Version 2 is finalized.
    assert volunteer_is_blocked() is True

    # Only finalizing Version 2 -- which has no assignment for the volunteer --
    # transfers authority away from Version 1.
    finalize_schedule_version(db_session, actor=scenario.head, version=v2)
    db_session.flush()
    assert volunteer_is_blocked() is False

    # Version 1 was never mutated along the way.
    stored_v1 = _stored_version(db_session, scenario.version_1.id)
    assert stored_v1.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert stored_v1.finalized_at == V1_FINALIZED_AT
    assert _assignment_count(db_session, scenario.version_1.id) == 1


# --------------------------------------------------------------------------
# E -- the source becomes immutable once outnumbered
# --------------------------------------------------------------------------


def test_e_the_source_version_becomes_immutable_to_the_real_lifecycle_operations(db_session):
    """Nothing sets a flag on Version 1. The existing latest-version rule --
    asked here through the real Task 24 and Task 27 services -- is what makes
    it historical, purely because Version 2 now exists.
    """
    scenario = _Scenario(db_session, assign=False)
    scenario.version_1.status = SCHEDULE_VERSION_STATUS_REVIEW
    scenario.version_1.finalized_at = None
    db_session.flush()

    # Before the successor exists, Version 1 is the latest and can be acted on.
    submit_schedule_version_for_review(
        db_session, actor=scenario.head, version=scenario.version_1,
    )  # idempotent no-op, but it does not raise

    scenario.create_successor(reason="Amending")

    with pytest.raises(InvalidOperationError, match="superseded"):
        submit_schedule_version_for_review(
            db_session, actor=scenario.head, version=scenario.version_1,
        )
    with pytest.raises(InvalidOperationError, match="superseded"):
        finalize_schedule_version(
            db_session, actor=scenario.head, version=scenario.version_1,
        )


def test_e2_assignments_can_no_longer_be_changed_on_the_superseded_version(db_session):
    """Task 22's own mutability guard, over real rows: the source's
    assignments are frozen once a successor exists.
    """
    scenario = _Scenario(db_session, assign=False)
    scenario.version_1.status = SCHEDULE_VERSION_STATUS_DRAFT
    scenario.version_1.finalized_at = None
    db_session.flush()
    assignment = assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement_1,
        membership=scenario.membership,
    )
    db_session.flush()

    scenario.create_successor(reason="Amending")

    with pytest.raises(InvalidOperationError, match="superseded"):
        remove_assignment(db_session, actor=scenario.head, assignment=assignment)
    assert _assignment_count(db_session, scenario.version_1.id) == 1


def test_e3_a_superseded_version_cannot_spawn_a_second_successor(db_session):
    """No forking: once Version 2 exists, Version 1 is not a branch point."""
    scenario = _Scenario(db_session, assign=False)
    scenario.create_successor(reason="First successor")

    with pytest.raises(InvalidOperationError, match="superseded"):
        create_successor_schedule_version(
            db_session, actor=scenario.head, source_version=scenario.version_1,
            amendment_reason="Second successor",
        )


def test_e4_the_chain_continues_from_the_new_latest_version(db_session):
    """Version 3 is created from Version 2, and its lineage points there."""
    scenario = _Scenario(db_session, assign=False)
    v2 = scenario.create_successor(reason="Second version")

    v3 = create_successor_schedule_version(
        db_session, actor=scenario.head, source_version=v2,
        amendment_reason="Third version",
    )
    db_session.flush()

    stored_v3 = _stored_version(db_session, v3.id)
    assert stored_v3.version_number == 3
    assert stored_v3.amends_version_id == v2.id
    assert _stored_version(db_session, v2.id).amends_version_id == scenario.version_1.id


def test_the_database_enforces_unique_version_numbers_per_schedule(db_session):
    """The concurrency guard this task deliberately does not duplicate in
    Python: two Version 2s for one schedule are impossible.
    """
    from sqlalchemy.exc import IntegrityError

    scenario = _Scenario(db_session, assign=False)
    scenario.create_successor(reason="Second version")

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            f.make_version(
                db_session, schedule=scenario.schedule, period=scenario.period,
                version_number=2, status=SCHEDULE_VERSION_STATUS_DRAFT,
            )
    assert "uq_schedule_version_schedule_id_version_number" in str(exc_info.value.orig)
