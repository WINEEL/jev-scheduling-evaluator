"""Draft schedule generation against real PostgreSQL rows (Task 34).

This is where the whole chain is exercised end to end: real requirements and
volunteers, the real Task 30 builder, the real CP-SAT solver, and real Task 22
writes producing real Assignment and AuditEvent rows.

Nothing about the persistence path is mocked. The one place a monkeypatch
appears is test E, purely to *time* a state change between building the input
and writing the second row -- the writes themselves are genuine
``assign_member`` calls against PostgreSQL, and the rejection comes from Task
22's own rules.

Rollback-isolated by the shared harness; nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.models.scheduling_input import AVAILABILITY_AVAILABLE, AVAILABILITY_UNAVAILABLE
from app.scheduling.solver import SchedulingPolicy
from app.services.assignment import assign_member
from app.services.audit import ACTION_ASSIGNMENT_ADDED
from app.services.errors import InvalidOperationError
from app.services.schedule_generation import generate_draft_schedule
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)

LENIENT = SchedulingPolicy(allow_no_response=True)


class CallerBlewUp(Exception):
    """An application error raised by the caller, after generation returned."""


class _Scenario:
    """A latest DRAFT version with ``sundays`` Sundays of one role, and a
    roster of interchangeable volunteers.
    """

    def __init__(self, session, *, sundays: int = 2, volunteers: int = 2,
                 required_count: int = 1, roles: int = 1):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.roles = [
            f.make_role(session, ministry=self.ministry, name=f"Position{index}")
            for index in range(roles)
        ]
        self.period = f.make_period(session, ministry=self.ministry)
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.events = []
        self.requirements = {}
        for index in range(sundays):
            event = f.make_event(
                session, period=self.period,
                event_date=NOV_15 + datetime.timedelta(days=7 * index),
            )
            self.events.append(event)
            for role in self.roles:
                f.make_staffing_requirement(
                    session, event=event, role=role, required_count=required_count,
                )
                self.requirements[(index, role.id)] = f.make_version_requirement(
                    session, version=self.version, event=event, role=role,
                    required_count=required_count,
                )
        self.memberships = []
        for index in range(volunteers):
            person = f.make_person(session, church=self.church, name=f"Volunteer{index}")
            membership = f.make_membership(session, person=person, ministry=self.ministry)
            for role in self.roles:
                f.make_qualification(
                    session, membership=membership, role=role,
                    decided_by=self.head, is_qualified=True,
                )
            for event in self.events:
                f.make_availability(
                    session, membership=membership, event=event,
                    state=AVAILABILITY_AVAILABLE,
                )
            self.memberships.append(membership)
        session.flush()

    def generate(self, policy=LENIENT, actor=None):
        result = generate_draft_schedule(
            self.session, actor=actor or self.head, version=self.version,
            policy=policy,
        )
        self.session.flush()
        return result


def _assignment_rows(session, version_id: int):
    return session.execute(
        text(
            "SELECT id, schedule_version_requirement_id, ministry_membership_id,"
            "       is_override, override_reason"
            " FROM assignment WHERE schedule_version_id = :v ORDER BY id"
        ),
        {"v": version_id},
    ).all()


def _added_audit_count(session) -> int:
    return session.execute(
        text("SELECT count(*) FROM audit_event WHERE action = :a"),
        {"a": ACTION_ASSIGNMENT_ADDED},
    ).scalar_one()


def _version_status(session, version_id: int) -> str:
    return session.execute(
        text("SELECT status FROM schedule_version WHERE id = :id"), {"id": version_id},
    ).scalar_one()


# --------------------------------------------------------------------------
# A -- a complete generation
# --------------------------------------------------------------------------


def test_a_proposals_become_real_assignment_rows_with_their_audit(db_session):
    scenario = _Scenario(db_session, sundays=2, volunteers=2)

    result = scenario.generate()

    rows = _assignment_rows(db_session, scenario.version.id)
    assert len(rows) == 2
    assert result.created_count == 2
    assert result.is_complete is True

    # Every created row corresponds to one proposal from this run.
    proposals = {
        (p.requirement_id, p.membership_id)
        for p in result.scheduling_result.proposed_assignments
    }
    assert {
        (r.schedule_version_requirement_id, r.ministry_membership_id) for r in rows
    } == proposals
    assert {r.id for r in rows} == {a.id for a in result.created_assignments}

    # Automatic work never overrides.
    assert all(r.is_override is False for r in rows)
    assert all(r.override_reason is None for r in rows)

    # One Task 22 audit row per created assignment, and no generation row.
    assert _added_audit_count(db_session) == 2
    assert db_session.execute(
        text("SELECT count(*) FROM audit_event WHERE action LIKE '%GENERAT%'")
    ).scalar_one() == 0

    # And the lifecycle is untouched.
    assert _version_status(db_session, scenario.version.id) == SCHEDULE_VERSION_STATUS_DRAFT


def test_a2_generation_is_refused_for_a_review_version(db_session):
    scenario = _Scenario(db_session)
    scenario.version.status = SCHEDULE_VERSION_STATUS_REVIEW
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="DRAFT"):
        scenario.generate()

    assert _assignment_rows(db_session, scenario.version.id) == []


# --------------------------------------------------------------------------
# B -- an existing manual assignment
# --------------------------------------------------------------------------


def test_b_a_manual_assignment_is_left_alone_and_generation_fills_around_it(db_session):
    scenario = _Scenario(db_session, sundays=2, volunteers=2)
    manual_requirement = scenario.requirements[(0, scenario.roles[0].id)]
    manual = assign_member(
        db_session, actor=scenario.head, requirement=manual_requirement,
        membership=scenario.memberships[0],
    )
    db_session.flush()
    manual_id = manual.id

    result = scenario.generate()

    rows = _assignment_rows(db_session, scenario.version.id)
    assert len(rows) == 2  # the manual one plus one generated
    assert result.created_count == 1  # only this run's work

    # The manual row is untouched and not duplicated.
    manual_row = next(r for r in rows if r.id == manual_id)
    assert manual_row.ministry_membership_id == scenario.memberships[0].id
    assert manual_row.schedule_version_requirement_id == manual_requirement.id
    assert manual_row.is_override is False
    assert manual_id not in {a.id for a in result.created_assignments}
    assert [r.schedule_version_requirement_id for r in rows].count(
        manual_requirement.id
    ) == 1


# --------------------------------------------------------------------------
# C -- an incomplete generation is still a success
# --------------------------------------------------------------------------


def test_c_an_incomplete_schedule_persists_what_it_can_without_raising(db_session):
    """Three Sundays, one volunteer: only one Sunday each can be staffed by
    that person, so two positions stay open. Best effort plus explicit
    unresolved slots.
    """
    scenario = _Scenario(db_session, sundays=3, volunteers=1)

    result = scenario.generate()

    assert result.created_count == 3  # one per Sunday; the same person may serve each
    assert result.is_complete is True

    # Now a genuinely infeasible shape: two positions per Sunday, one person.
    scarce = _Scenario(db_session, sundays=2, volunteers=1, required_count=2)
    scarce_result = scarce.generate()

    assert scarce_result.created_count == 2  # one per Sunday
    assert scarce_result.is_complete is False
    unfilled = scarce_result.scheduling_result.unfilled_requirements
    assert len(unfilled) == 2
    assert all(u.missing_count == 1 for u in unfilled)
    assert all(u.diagnostic_codes for u in unfilled)
    # Persisted what it could, and did not raise.
    assert len(_assignment_rows(db_session, scarce.version.id)) == 2


# --------------------------------------------------------------------------
# D -- repeated generation
# --------------------------------------------------------------------------


def test_d_a_second_run_on_unchanged_state_creates_nothing(db_session):
    """Repeatability with no provenance column: the second run's builder sees
    the first run's rows as existing assignments, so the solver proposes
    nothing.
    """
    scenario = _Scenario(db_session, sundays=2, volunteers=2)

    first = scenario.generate()
    assert first.created_count == 2
    audits_after_first = _added_audit_count(db_session)

    second = scenario.generate()

    assert second.created_count == 0
    assert second.scheduling_result.proposed_assignments == ()
    assert second.is_complete is True
    assert len(_assignment_rows(db_session, scenario.version.id)) == 2
    assert _added_audit_count(db_session) == audits_after_first == 2


def test_d2_a_second_run_after_a_genuinely_unfillable_shortfall_adds_nothing(db_session):
    scenario = _Scenario(db_session, sundays=2, volunteers=1, required_count=2)

    first = scenario.generate()
    assert first.is_complete is False
    before = len(_assignment_rows(db_session, scenario.version.id))

    second = scenario.generate()

    assert second.created_count == 0
    assert second.is_complete is False  # still short, still reported
    assert len(_assignment_rows(db_session, scenario.version.id)) == before


# --------------------------------------------------------------------------
# E -- atomic failure and caller rollback
# --------------------------------------------------------------------------


def test_e_a_failure_part_way_through_leaves_nothing_behind_after_rollback(db_session):
    """Two proposals. Between building the input and writing the second row,
    that volunteer's qualification is revoked -- so the real Task 22 refuses
    the second write while the first has already succeeded.

    The monkeypatch only *times* the change; both writes are genuine
    ``assign_member`` calls against PostgreSQL.
    """
    import app.services.schedule_generation as module

    scenario = _Scenario(db_session, sundays=2, volunteers=2)
    version_id = scenario.version.id

    real_solve = module.solve_schedule

    def solve_then_revoke(scheduling_input, *, policy):
        result = real_solve(scheduling_input, policy=policy)
        # The state the solver reasoned about is now out of date: revoke the
        # qualification of whoever the second proposal names.
        second = result.proposed_assignments[1]
        db_session.execute(
            text(
                "UPDATE role_qualification SET is_qualified = false"
                " WHERE ministry_membership_id = :m"
            ),
            {"m": second.membership_id},
        )
        return result

    with pytest.raises(InvalidOperationError, match="not currently qualified"):
        with db_session.begin_nested():
            module.solve_schedule = solve_then_revoke
            try:
                generate_draft_schedule(
                    db_session, actor=scenario.head, version=scenario.version,
                    policy=LENIENT,
                )
            finally:
                module.solve_schedule = real_solve

    # After the caller's rollback: neither the first assignment nor its audit
    # row survived, even though that write had succeeded.
    fresh = Session(
        bind=db_session.get_bind(), autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        rows = _assignment_rows(fresh, version_id)
        audits = _added_audit_count(fresh)
    finally:
        fresh.close()

    assert rows == []
    assert audits == 0


def test_e2_the_service_neither_commits_nor_compensates(db_session):
    """The same shape, without the outer savepoint: the exception propagates
    and the service has neither committed nor cleaned up after itself.

    **Task 63 changed what "the partial work" is.** Task 34 flushed each
    accepted row immediately, so after a refusal the first row sat pending in
    the transaction, left for the caller to discard. The batch writer accepts
    placements in memory and flushes the whole run at once, so a refused run
    has written nothing -- and, exactly as before, deleted nothing and rolled
    back nothing of its own. That is the same contract reached a stronger way:
    the caller's transaction is still the only thing that ends the run.
    """
    import app.services.schedule_generation as module

    scenario = _Scenario(db_session, sundays=2, volunteers=2)
    real_solve = module.solve_schedule

    def solve_then_block(scheduling_input, *, policy):
        result = real_solve(scheduling_input, policy=policy)
        second = result.proposed_assignments[1]
        membership_id = second.membership_id
        event_id = next(
            r.event_id for r in scheduling_input.requirements
            if r.requirement_id == second.requirement_id
        )
        db_session.execute(
            text(
                "UPDATE availability SET availability_state = :state"
                " WHERE ministry_membership_id = :m AND event_id = :e"
            ),
            {"state": AVAILABILITY_UNAVAILABLE, "m": membership_id, "e": event_id},
        )
        return result

    module.solve_schedule = solve_then_block
    try:
        with pytest.raises(InvalidOperationError, match="unavailable"):
            generate_draft_schedule(
                db_session, actor=scenario.head, version=scenario.version,
                policy=LENIENT,
            )
    finally:
        module.solve_schedule = real_solve

    # Nothing was written: the run was refused before its single flush, so
    # there is no partial schedule pending -- and equally nothing was deleted
    # or rolled back by the service to achieve that.
    db_session.flush()
    assert _assignment_rows(db_session, scenario.version.id) == []


# --------------------------------------------------------------------------
# F -- the policy bridge
# --------------------------------------------------------------------------


def test_f_the_supplied_policy_reaches_the_solver_and_shapes_real_rows(db_session):
    """Six Sundays, three volunteers, two roles, with the target and role
    variety both configured: the persisted rows match the solver's own answer
    and reflect the policy.
    """
    scenario = _Scenario(db_session, sundays=3, volunteers=3, roles=2)
    policy = SchedulingPolicy(
        allow_no_response=True,
        target_assignments_per_candidate=3,
        role_variety_role_ids=frozenset(role.id for role in scenario.roles),
    )

    result = scenario.generate(policy=policy)

    rows = _assignment_rows(db_session, scenario.version.id)
    assert len(rows) == result.created_count == 6  # 3 Sundays x 2 roles
    assert result.is_complete is True

    # The persisted rows are exactly the solver's proposals.
    assert {
        (r.schedule_version_requirement_id, r.ministry_membership_id) for r in rows
    } == {
        (p.requirement_id, p.membership_id)
        for p in result.scheduling_result.proposed_assignments
    }

    # And the policy really was applied: loads are balanced and roles varied.
    metrics = result.scheduling_result.metrics
    assert sorted(metrics.load_by_membership.values()) == [2, 2, 2]
    assert metrics.target_excess_total == 0
    assert metrics.role_variety_cost is not None
    assert all(r.is_override is False for r in rows)
