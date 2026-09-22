"""Assignment carry-forward against real PostgreSQL rows (Task 29).

The offline suite proves the orchestration with the lookups stubbed. What only
a real database can show is the claim this module exists to make: that an
override granted on version 1 -- a genuine Task 22 override, with its real
JSONB ``overridden_blockers`` audit payload sitting in the table -- confers
**nothing** on version 2. The same carry is rejected while the blocker stands
and succeeds as an ordinary assignment once it clears.

Nothing is mocked: the real ``assign_member``, the real Task 21 ``DISTINCT ON``
conflict query and the real Task 28 successor creation all run over real rows.

Every test is rollback-isolated by the shared harness; nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import AVAILABILITY_UNAVAILABLE
from app.services.assignment import assign_member
from app.services.assignment_carry_forward import carry_forward_assignment
from app.services.assignment_policy import BLOCKER_SUNDAY_CONFLICT, BLOCKER_UNAVAILABLE
from app.services.assignment_rules import CROSS_MINISTRY_SUNDAY_CONFLICT
from app.services.audit import ACTION_ASSIGNMENT_ADDED, ACTION_ASSIGNMENT_OVERRIDE_APPLIED
from app.services.errors import InvalidOperationError
from app.services.schedule_version import create_successor_schedule_version
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)
V1_FINALIZED_AT = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _Scenario:
    """A FINALIZED version 1 with one qualified member, ready to be assigned
    and then carried into a successor.
    """

    def __init__(self, session, *, required_count: int = 1):
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
            status=SCHEDULE_VERSION_STATUS_DRAFT,
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

    def assign_v1(self, *, override_reason: str | None = None) -> Assignment:
        assignment = assign_member(
            self.session, actor=self.head, requirement=self.requirement_1,
            membership=self.membership, override_reason=override_reason,
        )
        self.session.flush()
        return assignment

    def finalize_v1_row(self) -> None:
        """Put version 1 into FINALIZED state directly.

        Direct fixture state, not the Task 27 service: this suite is about
        carry-forward, and driving the full readiness gate here would test
        Task 26/27 again rather than anything new.
        """
        self.version_1.status = SCHEDULE_VERSION_STATUS_FINALIZED
        self.version_1.finalized_at = V1_FINALIZED_AT
        self.session.flush()

    def create_v2(self, *, reason: str = "Amending"):
        version = create_successor_schedule_version(
            self.session, actor=self.head, source_version=self.version_1,
            amendment_reason=reason,
        )
        self.session.flush()
        return version

    def carry(self, assignment, target_version):
        result = carry_forward_assignment(
            self.session, actor=self.head, source_assignment=assignment,
            target_version=target_version,
        )
        self.session.flush()
        return result

    def add_foreign_sunday_commitment(self, *, on: datetime.date = NOV_15):
        """Another ministry authoritatively claims this person on ``on`` --
        a real ADR 0003 conflict built from real rows.
        """
        other_ministry = f.make_ministry(self.session, church=self.church, name="AV")
        other_role = f.make_role(self.session, ministry=other_ministry, name="Sound")
        other_period = f.make_period(self.session, ministry=other_ministry)
        other_event = f.make_event(self.session, period=other_period, event_date=on)
        other_schedule = f.make_schedule(self.session, period=other_period)
        other_version = f.make_version(
            self.session, schedule=other_schedule, period=other_period,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=V1_FINALIZED_AT,
        )
        other_requirement = f.make_version_requirement(
            self.session, version=other_version, event=other_event, role=other_role,
        )
        other_membership = f.make_membership(
            self.session, person=self.person, ministry=other_ministry,
        )
        f.make_assignment(
            self.session, requirement=other_requirement, membership=other_membership,
        )
        self.session.flush()


def _assignments_of(session, version_id: int):
    return session.execute(
        text(
            "SELECT id, ministry_membership_id, schedule_version_requirement_id,"
            "       is_override, override_reason"
            " FROM assignment WHERE schedule_version_id = :v ORDER BY id"
        ),
        {"v": version_id},
    ).all()


def _audits_for(session, assignment_id: int, action: str):
    return session.execute(
        text(
            "SELECT id, after_values FROM audit_event"
            " WHERE target_table = 'assignment' AND target_id = :id"
            "   AND action = :action ORDER BY id"
        ),
        {"id": assignment_id, "action": action},
    ).all()


def _requirement_in(session, version_id: int) -> ScheduleVersionRequirement:
    return session.execute(
        select(ScheduleVersionRequirement).where(
            ScheduleVersionRequirement.schedule_version_id == version_id
        )
    ).scalars().first()


# --------------------------------------------------------------------------
# A -- the happy path
# --------------------------------------------------------------------------


def test_a_a_finalized_versions_assignment_carries_into_its_draft_successor(db_session):
    scenario = _Scenario(db_session)
    source = scenario.assign_v1()
    scenario.finalize_v1_row()
    v2 = scenario.create_v2()
    assert _assignments_of(db_session, v2.id) == []

    carried = scenario.carry(source, v2)

    v2_rows = _assignments_of(db_session, v2.id)
    assert len(v2_rows) == 1
    row = v2_rows[0]
    assert row.id == carried.id
    assert row.ministry_membership_id == scenario.membership.id
    assert row.schedule_version_requirement_id == _requirement_in(db_session, v2.id).id
    # A new decision, not a copy: normal, with no inherited override state.
    assert row.is_override is False
    assert row.override_reason is None

    # Version 1's assignment is untouched, and it is a different row.
    v1_rows = _assignments_of(db_session, scenario.version_1.id)
    assert len(v1_rows) == 1
    assert v1_rows[0].id == source.id != carried.id
    assert v1_rows[0].is_override is False

    # Exactly one creation audit for the new row, and none added to the old.
    assert len(_audits_for(db_session, carried.id, ACTION_ASSIGNMENT_ADDED)) == 1
    assert len(_audits_for(db_session, source.id, ACTION_ASSIGNMENT_ADDED)) == 1


# --------------------------------------------------------------------------
# B -- an override does not transfer while its blocker stands
# --------------------------------------------------------------------------


def test_b_a_real_override_does_not_authorize_the_carry_while_the_blocker_remains(db_session):
    """Version 1's assignment was a genuine Task 22 override for an explicit
    UNAVAILABLE. The member is still unavailable, so the carry is refused --
    the stored authorization belongs to version 1's decision, not this one.
    """
    scenario = _Scenario(db_session)
    f.make_availability(
        db_session, membership=scenario.membership, event=scenario.event,
        state=AVAILABILITY_UNAVAILABLE,
    )
    db_session.flush()
    source = scenario.assign_v1(override_reason="Head approved despite unavailability.")
    assert source.is_override is True

    # The real override audit payload exists and names the blocker.
    override_audits = _audits_for(db_session, source.id, ACTION_ASSIGNMENT_OVERRIDE_APPLIED)
    assert len(override_audits) == 1
    assert override_audits[0].after_values["overridden_blockers"] == [BLOCKER_UNAVAILABLE]

    scenario.finalize_v1_row()
    v2 = scenario.create_v2()

    with pytest.raises(InvalidOperationError, match="unavailable"):
        scenario.carry(source, v2)

    assert _assignments_of(db_session, v2.id) == []
    # And version 1's override row and its audit are untouched.
    assert _assignments_of(db_session, scenario.version_1.id)[0].is_override is True
    assert len(_audits_for(db_session, source.id, ACTION_ASSIGNMENT_OVERRIDE_APPLIED)) == 1


# --------------------------------------------------------------------------
# C -- an override whose blocker has cleared carries as an ordinary assignment
# --------------------------------------------------------------------------


def test_c_an_override_whose_blocker_has_gone_carries_as_a_normal_assignment(db_session):
    scenario = _Scenario(db_session)
    availability = f.make_availability(
        db_session, membership=scenario.membership, event=scenario.event,
        state=AVAILABILITY_UNAVAILABLE,
    )
    db_session.flush()
    source = scenario.assign_v1(override_reason="Head approved despite unavailability.")
    assert source.is_override is True

    # The member withdraws the UNAVAILABLE answer: the blocker is gone.
    db_session.delete(availability)
    db_session.flush()

    scenario.finalize_v1_row()
    v2 = scenario.create_v2()

    carried = scenario.carry(source, v2)

    row = _assignments_of(db_session, v2.id)[0]
    assert row.id == carried.id
    assert row.is_override is False
    assert row.override_reason is None
    # No override audit was written for the new row, and none was copied.
    assert _audits_for(db_session, carried.id, ACTION_ASSIGNMENT_OVERRIDE_APPLIED) == []
    added = _audits_for(db_session, carried.id, ACTION_ASSIGNMENT_ADDED)
    assert len(added) == 1
    assert "overridden_blockers" not in added[0].after_values
    # Version 1 keeps its override and its history, unchanged.
    assert _assignments_of(db_session, scenario.version_1.id)[0].override_reason == (
        "Head approved despite unavailability."
    )


# --------------------------------------------------------------------------
# D -- a moved event refuses rather than relocating the person
# --------------------------------------------------------------------------


def test_d_a_changed_snapshot_date_rejects_the_carry(db_session):
    scenario = _Scenario(db_session)
    source = scenario.assign_v1()
    scenario.finalize_v1_row()

    # The event moves before the successor is created, so Task 28 correctly
    # snapshots the new date into version 2.
    scenario.event.event_date = NOV_22
    db_session.flush()
    v2 = scenario.create_v2(reason="Event date changed")

    v1_requirement_date = db_session.execute(
        text("SELECT event_date FROM schedule_version_requirement WHERE id = :id"),
        {"id": scenario.requirement_1.id},
    ).scalar_one()
    v2_requirement = _requirement_in(db_session, v2.id)
    assert v1_requirement_date == NOV_15
    assert v2_requirement.event_date == NOV_22

    with pytest.raises(InvalidOperationError, match="has moved from 2026-11-15 to 2026-11-22"):
        scenario.carry(source, v2)

    assert _assignments_of(db_session, v2.id) == []


def test_d2_a_requirement_absent_from_the_successor_rejects_the_carry(db_session):
    """The event was cancelled, so Task 28's snapshot omits it entirely."""
    scenario = _Scenario(db_session)
    source = scenario.assign_v1()
    scenario.finalize_v1_row()

    scenario.event.cancelled_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()
    v2 = scenario.create_v2(reason="Event cancelled")
    assert _requirement_in(db_session, v2.id) is None

    with pytest.raises(InvalidOperationError, match="no requirement for this event and role"):
        scenario.carry(source, v2)


# --------------------------------------------------------------------------
# E -- a conflict that arose after version 1
# --------------------------------------------------------------------------


def test_e_a_new_cross_ministry_conflict_rejects_the_carry(db_session):
    """The source assignment was perfectly valid when made. Another ministry
    has since finalized a schedule claiming this person for the same Sunday --
    found by the real Task 21 query, through the real Task 22 check.

    Since Task 79's correction the refusal comes from the **absolute** rule
    rather than the overridable catalogue. Carry-forward has no override
    argument at all, so the outcome was always a refusal; what changed is that
    it now names the hard rule, and that no route exists to force it.
    """
    scenario = _Scenario(db_session)
    source = scenario.assign_v1()
    assert source.is_override is False
    scenario.finalize_v1_row()
    v2 = scenario.create_v2()

    scenario.add_foreign_sunday_commitment(on=NOV_15)

    with pytest.raises(InvalidOperationError) as caught:
        scenario.carry(source, v2)
    assert CROSS_MINISTRY_SUNDAY_CONFLICT in str(caught.value)
    assert "not overridable" in str(caught.value)

    assert _assignments_of(db_session, v2.id) == []


def test_e2_a_now_deactivated_person_rejects_the_carry(db_session):
    scenario = _Scenario(db_session)
    source = scenario.assign_v1()
    scenario.finalize_v1_row()
    v2 = scenario.create_v2()

    scenario.person.deactivated_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="deactivated person"):
        scenario.carry(source, v2)
    assert _assignments_of(db_session, v2.id) == []


# --------------------------------------------------------------------------
# F -- idempotency
# --------------------------------------------------------------------------


def test_f_a_repeated_carry_returns_the_same_row_with_no_duplicate_audit(db_session):
    scenario = _Scenario(db_session)
    source = scenario.assign_v1()
    scenario.finalize_v1_row()
    v2 = scenario.create_v2()

    first = scenario.carry(source, v2)
    second = scenario.carry(source, v2)

    assert second.id == first.id
    rows = _assignments_of(db_session, v2.id)
    assert len(rows) == 1
    assert len(_audits_for(db_session, first.id, ACTION_ASSIGNMENT_ADDED)) == 1


# --------------------------------------------------------------------------
# Lineage and target lifecycle, over real rows
# --------------------------------------------------------------------------


def test_a_review_target_is_rejected(db_session):
    """Stricter than Task 22, which would accept REVIEW."""
    scenario = _Scenario(db_session)
    source = scenario.assign_v1()
    scenario.finalize_v1_row()
    v2 = scenario.create_v2()
    v2.status = SCHEDULE_VERSION_STATUS_REVIEW
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="only be carried into a DRAFT"):
        scenario.carry(source, v2)


def test_a_superseded_target_is_rejected(db_session):
    scenario = _Scenario(db_session)
    source = scenario.assign_v1()
    scenario.finalize_v1_row()
    v2 = scenario.create_v2()
    v3 = create_successor_schedule_version(
        db_session, actor=scenario.head, source_version=v2, amendment_reason="Third",
    )
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="superseded"):
        scenario.carry(source, v2)
    # And V1 -> V3 is refused as non-adjacent, not silently allowed.
    with pytest.raises(InvalidOperationError, match="does not amend"):
        scenario.carry(source, v3)


def test_a_changed_required_count_still_permits_the_carry(db_session):
    """A count that rose from 1 to 2 leaves this person's place intact; the
    successor is simply underfilled until someone else is assigned.
    """
    scenario = _Scenario(db_session, required_count=1)
    source = scenario.assign_v1()
    scenario.finalize_v1_row()

    scenario.staffing.required_count = 2
    db_session.flush()
    v2 = scenario.create_v2(reason="Staffing requirements changed")
    assert _requirement_in(db_session, v2.id).required_count == 2

    carried = scenario.carry(source, v2)

    assert carried.is_override is False
    assert len(_assignments_of(db_session, v2.id)) == 1
