"""Integration Test I -- Task 22's assign/remove happy path over real rows.

Value here comes from running Task 22's *many* SQL helpers together against
one relational state: the exact-assignment lookup, the duplicate-in-event
probe, the capacity count, the qualification and availability lookups, the
newer-version probe, and Task 21's church-wide conflict query -- all real,
all on the same connection, none monkeypatched.

The fixture is the fully clean case (§7, §10): latest DRAFT version, active
membership and person, active role, approved qualification, no explicit
UNAVAILABLE row, no church-wide conflict, and capacity to spare -- so no
override should be needed or recorded.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from app.models.schedule_output import SCHEDULE_VERSION_STATUS_DRAFT
from app.services.assignment import (
    ACTION_ASSIGNMENT_ADDED,
    ACTION_ASSIGNMENT_REMOVED,
    assign_member,
    remove_assignment,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)


class _Scenario:
    def __init__(self, session, *, required_count: int = 1):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.person = f.make_person(session, church=self.church, name="Volunteer")
        self.membership = f.make_membership(
            session, person=self.person, ministry=self.ministry,
        )
        self.role = f.make_role(session, ministry=self.ministry, name="Setup Lead")
        f.make_qualification(
            session, membership=self.membership, role=self.role,
            decided_by=self.head, is_qualified=True,
        )
        self.period = f.make_period(session, ministry=self.ministry)
        self.event = f.make_event(session, period=self.period, event_date=NOV_15)
        f.make_staffing_requirement(
            session, event=self.event, role=self.role, required_count=required_count,
        )
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.requirement = f.make_version_requirement(
            session, version=self.version, event=self.event, role=self.role,
            required_count=required_count,
        )
        session.flush()


def _assignment_count(session, requirement_id: int) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM assignment"
            " WHERE schedule_version_requirement_id = :r"
        ),
        {"r": requirement_id},
    ).scalar_one()


def _audit_count(session, *, actor_person_id: int, action: str) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM audit_event"
            " WHERE actor_person_id = :a AND action = :action"
        ),
        {"a": actor_person_id, "action": action},
    ).scalar_one()


def test_i1_a_clean_assignment_is_persisted_without_an_override(db_session):
    scenario = _Scenario(db_session)

    assignment = assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=scenario.membership,
    )
    db_session.flush()

    assert assignment.id is not None
    assert assignment.is_override is False
    assert assignment.override_reason is None
    assert _assignment_count(db_session, scenario.requirement.id) == 1

    stored = db_session.execute(
        text(
            "SELECT ministry_membership_id, schedule_version_id, event_id,"
            " ministry_id, is_override FROM assignment WHERE id = :id"
        ),
        {"id": assignment.id},
    ).one()
    assert stored.ministry_membership_id == scenario.membership.id
    assert stored.schedule_version_id == scenario.version.id
    assert stored.event_id == scenario.event.id
    assert stored.ministry_id == scenario.ministry.id
    assert stored.is_override is False


def test_i2_the_assignment_audit_event_is_written(db_session):
    scenario = _Scenario(db_session)

    assignment = assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=scenario.membership,
    )
    db_session.flush()

    row = db_session.execute(
        text(
            "SELECT action, target_table, target_id, ministry_id, after_values"
            " FROM audit_event WHERE actor_person_id = :a"
        ),
        {"a": scenario.head.id},
    ).one()
    assert row.action == ACTION_ASSIGNMENT_ADDED
    assert row.target_table == "assignment"
    assert row.target_id == assignment.id
    assert row.ministry_id == scenario.ministry.id
    assert row.after_values["is_override"] is False
    assert "overridden_blockers" not in row.after_values


def test_i3_assigning_the_same_member_twice_is_idempotent(db_session):
    scenario = _Scenario(db_session)

    first = assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=scenario.membership,
    )
    db_session.flush()
    second = assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=scenario.membership,
    )
    db_session.flush()

    # The real exact-assignment lookup found the existing row.
    assert second.id == first.id
    assert _assignment_count(db_session, scenario.requirement.id) == 1
    assert _audit_count(
        db_session, actor_person_id=scenario.head.id, action=ACTION_ASSIGNMENT_ADDED,
    ) == 1


def test_i4_removal_actually_deletes_the_row_and_audits_it(db_session):
    scenario = _Scenario(db_session)
    assignment = assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=scenario.membership,
    )
    db_session.flush()
    assignment_id = assignment.id

    remove_assignment(db_session, actor=scenario.head, assignment=assignment)
    db_session.flush()

    assert _assignment_count(db_session, scenario.requirement.id) == 0
    assert db_session.execute(
        text("SELECT count(*) FROM assignment WHERE id = :id"), {"id": assignment_id},
    ).scalar_one() == 0

    removal = db_session.execute(
        text(
            "SELECT target_id, before_values FROM audit_event"
            " WHERE actor_person_id = :a AND action = :action"
        ),
        {"a": scenario.head.id, "action": ACTION_ASSIGNMENT_REMOVED},
    ).one()
    assert removal.target_id == assignment_id
    assert removal.before_values["ministry_membership_id"] == scenario.membership.id


def test_i5_a_full_requirement_refuses_a_second_member_without_an_override(db_session):
    """The real capacity count, over real rows: required_count is 1 and it is
    already filled, so a *different* member is refused rather than silently
    over-staffing.
    """
    from app.services.errors import InvalidOperationError

    scenario = _Scenario(db_session, required_count=1)
    assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=scenario.membership,
    )
    db_session.flush()

    other_person = f.make_person(db_session, church=scenario.church, name="Second")
    other_membership = f.make_membership(
        db_session, person=other_person, ministry=scenario.ministry,
    )
    f.make_qualification(
        db_session, membership=other_membership, role=scenario.role,
        decided_by=scenario.head, is_qualified=True,
    )
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="fully staffed"):
        assign_member(
            db_session, actor=scenario.head, requirement=scenario.requirement,
            membership=other_membership,
        )
