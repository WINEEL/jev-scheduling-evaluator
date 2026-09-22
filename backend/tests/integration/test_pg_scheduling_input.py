"""Scheduling-input building against real PostgreSQL rows (Task 30).

The offline suite proves the orchestration with the reads stubbed. What only a
real database can show is that these queries return the rows the pure model
claims: that an inactive person really drops out, that a qualification matrix
really lands per candidate, that a stored answer and a missing row really
become two different states, and that a genuine ADR 0003 conflict -- resolved
by Task 21's own ``DISTINCT ON`` query, unmocked -- really lands in
``blocked_dates``.

Every test is rollback-isolated by the shared harness; nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.models.scheduling_input import AVAILABILITY_AVAILABLE, AVAILABILITY_UNAVAILABLE
from app.scheduling.input import AvailabilityState
from app.services.assignment import assign_member
from app.services.errors import InvalidOperationError
from app.services.schedule_staleness import get_schedule_version_staleness
from app.services.scheduling_input_builder import build_scheduling_input
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)
FINALIZED_AT = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _Scenario:
    """A DRAFT version with one requirement, whose snapshot matches current
    input so the staleness gate starts satisfied.
    """

    def __init__(self, session, *, required_count: int = 1, event_date=NOV_15):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        # The head is a member of the ministry like anybody else, so they are
        # a candidate too (Task 80 gave these scenarios a head where they used
        # to act as an Admin, who was a member of nothing). Their membership
        # is named here so the tests below can say what they mean -- "this
        # person is a candidate, that one is not" -- rather than depend on the
        # roster containing exactly one row.
        self.head_membership = self.head.ministry_memberships[0]
        self.role = f.make_role(session, ministry=self.ministry, name="Setup Lead")
        self.period = f.make_period(session, ministry=self.ministry)
        self.event = f.make_event(session, period=self.period, event_date=event_date)
        self.staffing = f.make_staffing_requirement(
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

    def add_member(self, name: str, *, qualified: bool | None = True,
                   membership_deactivated: bool = False, person_deactivated: bool = False):
        person = f.make_person(
            self.session, church=self.church, name=name, deactivated=person_deactivated,
        )
        membership = f.make_membership(
            self.session, person=person, ministry=self.ministry,
        )
        if membership_deactivated:
            membership.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        if qualified is not None:
            f.make_qualification(
                self.session, membership=membership, role=self.role,
                decided_by=self.head, is_qualified=qualified,
            )
        self.session.flush()
        return membership

    def build(self):
        return build_scheduling_input(self.session, version=self.version)

    def add_foreign_sunday_commitment(self, person, *, on=NOV_15):
        """Another ministry authoritatively claims this person -- real rows,
        resolved by Task 21's own query.
        """
        other_ministry = f.make_ministry(self.session, church=self.church, name="AV")
        other_role = f.make_role(self.session, ministry=other_ministry, name="Sound")
        other_period = f.make_period(self.session, ministry=other_ministry)
        other_event = f.make_event(self.session, period=other_period, event_date=on)
        other_schedule = f.make_schedule(self.session, period=other_period)
        other_version = f.make_version(
            self.session, schedule=other_schedule, period=other_period,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT,
        )
        other_requirement = f.make_version_requirement(
            self.session, version=other_version, event=other_event, role=other_role,
        )
        other_membership = f.make_membership(
            self.session, person=person, ministry=other_ministry,
        )
        f.make_assignment(
            self.session, requirement=other_requirement, membership=other_membership,
        )
        self.session.flush()


# --------------------------------------------------------------------------
# A -- a real fresh DRAFT version
# --------------------------------------------------------------------------


def test_a_builds_snapshot_values_and_active_candidates_from_real_rows(db_session):
    scenario = _Scenario(db_session, required_count=2)
    active = scenario.add_member("Active")
    scenario.add_member("GoneMembership", membership_deactivated=True)
    scenario.add_member("GonePerson", person_deactivated=True)

    result = scenario.build()

    assert result.schedule_version_id == scenario.version.id
    assert result.scheduling_period_id == scenario.period.id
    assert result.ministry_id == scenario.ministry.id

    assert len(result.requirements) == 1
    requirement = result.requirements[0]
    assert requirement.requirement_id == scenario.requirement.id
    assert requirement.event_id == scenario.event.id
    assert requirement.event_date == NOV_15
    assert requirement.ministry_role_id == scenario.role.id
    assert requirement.required_count == 2
    assert requirement.role_is_active is True
    assert result.total_required_positions == 2

    # Only the active membership of an active person is a candidate. The head
    # is one too -- heading a ministry is a membership of it -- so the claim
    # worth making is about which of the three added members got through.
    candidate_ids = {c.membership_id for c in result.candidates}
    assert active.id in candidate_ids
    assert candidate_ids == {active.id, scenario.head_membership.id}
    found = next(c for c in result.candidates if c.membership_id == active.id)
    assert found.person_id == active.person_id
    assert found.display_name.startswith("Active-it-")


def test_a2_another_ministrys_members_are_not_candidates(db_session):
    scenario = _Scenario(db_session)
    ours = scenario.add_member("Ours")
    other_ministry = f.make_ministry(db_session, church=scenario.church, name="AV")
    outsider = f.make_person(db_session, church=scenario.church, name="Outsider")
    f.make_membership(db_session, person=outsider, ministry=other_ministry)
    db_session.flush()

    result = scenario.build()

    candidate_ids = {c.membership_id for c in result.candidates}
    assert candidate_ids == {ours.id, scenario.head_membership.id}


def test_a3_a_version_with_no_requirements_still_builds_with_its_ministry(db_session):
    scenario = _Scenario(db_session)
    db_session.delete(scenario.requirement)
    db_session.delete(scenario.staffing)
    db_session.flush()

    result = scenario.build()

    assert result.requirements == ()
    assert result.ministry_id == scenario.ministry.id


# --------------------------------------------------------------------------
# B -- the qualification matrix
# --------------------------------------------------------------------------


def test_b_qualification_truth_is_reflected_per_candidate(db_session):
    scenario = _Scenario(db_session)
    qualified = scenario.add_member("Qualified", qualified=True)
    declined = scenario.add_member("Declined", qualified=False)
    unassessed = scenario.add_member("Unassessed", qualified=None)

    result = scenario.build()
    by_id = {c.membership_id: c for c in result.candidates}

    assert by_id[qualified.id].qualified_role_ids == frozenset({scenario.role.id})
    # An explicit False and a missing row are one outcome, as in Task 22.
    assert by_id[declined.id].qualified_role_ids == frozenset()
    assert by_id[unassessed.id].qualified_role_ids == frozenset()

    eligible = result.eligible_candidates(result.requirements[0])
    assert [c.membership_id for c in eligible] == [qualified.id]


def test_b2_a_deactivated_role_leaves_the_requirement_with_no_eligible_candidates(db_session):
    """Role activity is current state and is not in Task 23's fingerprint, so
    the version is still fresh -- the builder states the fact instead.
    """
    scenario = _Scenario(db_session)
    scenario.add_member("Qualified", qualified=True)
    scenario.role.deactivated_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()

    assert get_schedule_version_staleness(
        db_session, version=scenario.version
    ).is_stale is False

    result = scenario.build()

    assert result.requirements[0].role_is_active is False
    assert result.eligible_candidates(result.requirements[0]) == ()


# --------------------------------------------------------------------------
# C -- the availability tri-state
# --------------------------------------------------------------------------


def test_c_all_three_availability_states_are_distinct_over_real_rows(db_session):
    scenario = _Scenario(db_session)
    yes = scenario.add_member("Yes")
    no = scenario.add_member("No")
    silent = scenario.add_member("Silent")
    f.make_availability(
        db_session, membership=yes, event=scenario.event, state=AVAILABILITY_AVAILABLE,
    )
    f.make_availability(
        db_session, membership=no, event=scenario.event, state=AVAILABILITY_UNAVAILABLE,
    )
    db_session.flush()

    result = scenario.build()
    by_id = {c.membership_id: c for c in result.candidates}

    assert by_id[yes.id].availability_for(scenario.event.id) is AvailabilityState.AVAILABLE
    assert by_id[no.id].availability_for(scenario.event.id) is AvailabilityState.UNAVAILABLE
    assert by_id[silent.id].availability_for(scenario.event.id) is AvailabilityState.NO_RESPONSE

    # The third state exists only in the pure model: no row was written for it.
    stored = db_session.execute(
        text("SELECT count(*) FROM availability WHERE event_id = :e"),
        {"e": scenario.event.id},
    ).scalar_one()
    assert stored == 2


# --------------------------------------------------------------------------
# D -- a real church-wide conflict
# --------------------------------------------------------------------------


def test_d_an_authoritative_foreign_assignment_blocks_the_snapshot_sunday(db_session):
    scenario = _Scenario(db_session)
    committed = scenario.add_member("Committed")
    free = scenario.add_member("Free")
    scenario.add_foreign_sunday_commitment(committed.person, on=NOV_15)

    result = scenario.build()
    by_id = {c.membership_id: c for c in result.candidates}

    assert by_id[committed.id].blocked_dates == frozenset({NOV_15})
    assert by_id[committed.id].is_blocked_on(NOV_15) is True
    assert by_id[free.id].blocked_dates == frozenset()
    # The conflicting rows themselves never reach the solver input.
    assert all(
        isinstance(d, datetime.date) for d in by_id[committed.id].blocked_dates
    )


def test_d2_a_conflict_on_another_date_does_not_block_this_sunday(db_session):
    scenario = _Scenario(db_session, event_date=NOV_15)
    member = scenario.add_member("Member")
    scenario.add_foreign_sunday_commitment(member.person, on=NOV_22)

    result = scenario.build()

    assert result.candidates[0].blocked_dates == frozenset()


# --------------------------------------------------------------------------
# E -- existing assignments
# --------------------------------------------------------------------------


def test_e_a_real_manual_assignment_appears_as_an_existing_assignment(db_session):
    scenario = _Scenario(db_session)
    member = scenario.add_member("Member")
    assignment = assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=member,
    )
    db_session.flush()

    result = scenario.build()

    assert len(result.existing_assignments) == 1
    existing = result.existing_assignments[0]
    assert existing.assignment_id == assignment.id
    assert existing.requirement_id == scenario.requirement.id
    assert existing.membership_id == member.id
    assert existing.event_id == scenario.event.id
    assert existing.is_override is False

    # Building the input changed nothing about it.
    stored = db_session.execute(
        text(
            "SELECT is_override, override_reason, ministry_membership_id"
            " FROM assignment WHERE id = :id"
        ),
        {"id": assignment.id},
    ).one()
    assert stored.is_override is False
    assert stored.override_reason is None
    assert stored.ministry_membership_id == member.id


def test_e2_an_override_assignment_keeps_its_flag_without_its_reason(db_session):
    """The flag is enough to say a decision exists; the justification and its
    audit stay in the database, where Task 26 reads them.
    """
    scenario = _Scenario(db_session)
    member = scenario.add_member("Member")
    f.make_availability(
        db_session, membership=member, event=scenario.event,
        state=AVAILABILITY_UNAVAILABLE,
    )
    db_session.flush()
    assignment = assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=member, override_reason="Head approved.",
    )
    db_session.flush()
    assert assignment.is_override is True

    result = scenario.build()

    existing = result.existing_assignments[0]
    assert existing.is_override is True
    assert not hasattr(existing, "override_reason")


def test_e3_another_versions_assignments_are_excluded(db_session):
    scenario = _Scenario(db_session)
    member = scenario.add_member("Member")
    assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=member,
    )
    db_session.flush()

    # A second, unrelated schedule and version with its own assignment.
    other_period = f.make_period(db_session, ministry=scenario.ministry, name="Q1 2027")
    other_event = f.make_event(
        db_session, period=other_period, event_date=datetime.date(2027, 1, 3),
    )
    other_schedule = f.make_schedule(db_session, period=other_period)
    other_version = f.make_version(
        db_session, schedule=other_schedule, period=other_period,
        status=SCHEDULE_VERSION_STATUS_DRAFT,
    )
    other_requirement = f.make_version_requirement(
        db_session, version=other_version, event=other_event, role=scenario.role,
    )
    f.make_assignment(db_session, requirement=other_requirement, membership=member)
    db_session.flush()

    result = scenario.build()

    assert len(result.existing_assignments) == 1
    assert result.existing_assignments[0].requirement_id == scenario.requirement.id


# --------------------------------------------------------------------------
# F -- the staleness gate over real drift
# --------------------------------------------------------------------------


def test_f_a_moved_event_makes_the_version_stale_and_the_build_is_refused(db_session):
    """The builder refuses rather than silently substituting the new date --
    the version committed to 15 November, and a successor is the repair.
    """
    scenario = _Scenario(db_session)
    scenario.add_member("Member")
    assert scenario.build().requirements[0].event_date == NOV_15

    scenario.event.event_date = NOV_22
    db_session.flush()

    assert get_schedule_version_staleness(
        db_session, version=scenario.version
    ).is_stale is True
    with pytest.raises(InvalidOperationError, match="no longer matches current"):
        scenario.build()

    # And the snapshot was not rewritten on the way out.
    assert db_session.execute(
        text("SELECT event_date FROM schedule_version_requirement WHERE id = :id"),
        {"id": scenario.requirement.id},
    ).scalar_one() == NOV_15


def test_f2_a_changed_required_count_also_refuses(db_session):
    scenario = _Scenario(db_session, required_count=1)
    scenario.staffing.required_count = 3
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="no longer matches current"):
        scenario.build()


def test_f3_a_cancelled_event_is_caught_by_the_same_staleness_gate(db_session):
    """No second, competing cancelled-event rule: the cancelled event drops out
    of current requirements, so the version simply reads as stale.
    """
    scenario = _Scenario(db_session)
    scenario.event.cancelled_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="no longer matches current"):
        scenario.build()


def test_f4_a_review_version_builds_and_a_finalized_one_does_not(db_session):
    scenario = _Scenario(db_session)
    scenario.add_member("Member")

    scenario.version.status = SCHEDULE_VERSION_STATUS_REVIEW
    db_session.flush()
    assert scenario.build().schedule_version_id == scenario.version.id

    scenario.version.status = SCHEDULE_VERSION_STATUS_FINALIZED
    scenario.version.finalized_at = FINALIZED_AT
    db_session.flush()
    with pytest.raises(InvalidOperationError, match="DRAFT or REVIEW"):
        scenario.build()


def test_f5_a_superseded_version_is_refused(db_session):
    scenario = _Scenario(db_session)
    f.make_version(
        db_session, schedule=scenario.schedule, period=scenario.period,
        version_number=2, status=SCHEDULE_VERSION_STATUS_DRAFT,
    )
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="superseded"):
        scenario.build()


def test_building_the_input_writes_nothing_at_all(db_session):
    scenario = _Scenario(db_session)
    member = scenario.add_member("Member")
    assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=member,
    )
    db_session.flush()

    def counts():
        return db_session.execute(
            text(
                "SELECT (SELECT count(*) FROM audit_event) AS audits,"
                "       (SELECT count(*) FROM assignment) AS assignments,"
                "       (SELECT count(*) FROM availability) AS availability,"
                "       (SELECT count(*) FROM schedule_version_requirement) AS requirements"
            )
        ).one()

    before = counts()
    scenario.build()
    scenario.build()

    assert counts() == before
