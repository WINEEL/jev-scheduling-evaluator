"""Finalization readiness against real PostgreSQL rows (Task 26).

The offline suite proves the control flow with the reads stubbed. What only a
real database can prove is that the pieces compose: that Task 21's
``DISTINCT ON`` conflict query, Task 23's staleness comparison, the
requirement/assignment reads and the **JSONB override payload Task 22 actually
wrote** all agree about one version.

Nothing is mocked here. Overrides are created by calling the real
:func:`app.services.assignment.assign_member`, so the
``after_values["overridden_blockers"]`` this module reads back is the genuine
audit row rather than a fixture's idea of one -- which is the whole point of
the covered/uncovered rule.

Every test is rollback-isolated by the shared harness; nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services.assignment import assign_member
from app.services.availability import set_availability
from app.services.role_qualification import set_role_qualification
from app.services.assignment_policy import (
    BLOCKER_CAPACITY_FULL,
    BLOCKER_SUNDAY_CONFLICT,
    BLOCKER_UNAVAILABLE,
)
from app.services.assignment_rules import CROSS_MINISTRY_SUNDAY_CONFLICT
from app.services.audit import (
    ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
    record_audit_event,
)
from app.services.errors import InvalidOperationError
from app.services.finalization_readiness import (
    ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT,
    ISSUE_INVALID_OVERRIDE_PAYLOAD,
    ISSUE_MISSING_OVERRIDE_AUDIT,
    ISSUE_UNAUTHORIZED_BLOCKER,
    ISSUE_UNAUTHORIZED_OVERFILL,
    ISSUE_UNFILLED_REQUIREMENT,
    get_finalization_readiness,
)
from app.models.scheduling_input import AVAILABILITY_BACKUP, AVAILABILITY_UNAVAILABLE
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
FINALIZED_AT = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _Scenario:
    """A REVIEW version of one ministry, with a snapshot matching current
    input so the staleness gate starts satisfied."""

    def __init__(self, session, *, required_count: int = 1, members: int = 1):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.role = f.make_role(session, ministry=self.ministry, name="Setup Lead")
        self.period = f.make_period(session, ministry=self.ministry)
        self.event = f.make_event(session, period=self.period, event_date=NOV_15)
        f.make_staffing_requirement(
            session, event=self.event, role=self.role, required_count=required_count,
        )
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_REVIEW,
        )
        self.requirement = f.make_version_requirement(
            session, version=self.version, event=self.event, role=self.role,
            required_count=required_count,
        )
        self.memberships = []
        for index in range(members):
            person = f.make_person(session, church=self.church, name=f"Member{index}")
            membership = f.make_membership(session, person=person, ministry=self.ministry)
            f.make_qualification(
                session, membership=membership, role=self.role,
                decided_by=self.head, is_qualified=True,
            )
            self.memberships.append(membership)
        session.flush()

    @property
    def membership(self):
        return self.memberships[0]

    def assign(self, membership, *, override_reason: str | None = None):
        assignment = assign_member(
            self.session, actor=self.head, requirement=self.requirement,
            membership=membership, override_reason=override_reason,
        )
        self.session.flush()
        return assignment

    def readiness(self):
        return get_finalization_readiness(self.session, version=self.version)

    def add_foreign_sunday_commitment(self, membership):
        """Make another ministry authoritatively claim this person on the
        snapshot date -- a real ADR 0003 conflict, built from real rows.
        """
        other_ministry = f.make_ministry(self.session, church=self.church, name="AV")
        other_role = f.make_role(self.session, ministry=other_ministry, name="Sound")
        other_period = f.make_period(self.session, ministry=other_ministry)
        other_event = f.make_event(self.session, period=other_period, event_date=NOV_15)
        other_schedule = f.make_schedule(self.session, period=other_period)
        other_version = f.make_version(
            self.session, schedule=other_schedule, period=other_period,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT,
        )
        other_requirement = f.make_version_requirement(
            self.session, version=other_version, event=other_event, role=other_role,
        )
        other_membership = f.make_membership(
            self.session, person=membership.person, ministry=other_ministry,
        )
        f.make_assignment(
            self.session, requirement=other_requirement, membership=other_membership,
        )
        self.session.flush()


def _codes(result):
    return [issue.code for issue in result.issues]


# --------------------------------------------------------------------------
# A / B -- the baseline gates over real rows
# --------------------------------------------------------------------------


def test_a_fresh_fully_filled_review_version_is_ready(db_session):
    scenario = _Scenario(db_session)
    scenario.assign(scenario.membership)

    result = scenario.readiness()

    assert result.staleness.is_stale is False
    assert result.issues == ()
    assert result.is_ready is True


# --------------------------------------------------------------------------
# Task 55 -- revoking qualification through the real service, after the
# assignment already exists, is seen by readiness immediately: qualification
# is candidate-level live state, never part of the version's own snapshot.
# --------------------------------------------------------------------------


def test_revoking_qualification_through_the_real_service_breaks_readiness(db_session):
    """A / B above prove the baseline is ready. Here the exact same
    assignment is made not-ready by calling
    :func:`set_role_qualification` -- the same function the new
    ``PUT /ministry-roles/{id}/qualifications/{id}`` endpoint calls -- with
    no change to the assignment, the snapshot, or the schedule version
    itself.
    """
    scenario = _Scenario(db_session)
    scenario.assign(scenario.membership)
    assert scenario.readiness().is_ready is True

    set_role_qualification(
        db_session, actor=scenario.head, membership=scenario.membership,
        role=scenario.role, is_qualified=False,
    )
    db_session.flush()

    result = scenario.readiness()
    assert result.is_ready is False
    assert ISSUE_UNAUTHORIZED_BLOCKER in _codes(result)


def test_granting_qualification_through_the_real_service_makes_a_candidate_eligible(
    db_session,
):
    """The other direction: a membership never assessed for this role
    (Task 55's "never assessed" state) becomes usable the moment
    :func:`set_role_qualification` approves it -- reflected in
    :func:`build_scheduling_input`'s ``qualified_role_ids`` with no code
    change anywhere in the builder or solver.
    """
    from app.services.scheduling_input_builder import build_scheduling_input

    scenario = _Scenario(db_session, members=0)
    person = f.make_person(db_session, church=scenario.church, name="New Member")
    membership = f.make_membership(db_session, person=person, ministry=scenario.ministry)
    db_session.flush()

    before = build_scheduling_input(db_session, version=scenario.version)
    before_candidate = before.candidate_by_membership_id(membership.id)
    assert before_candidate is not None
    assert before_candidate.qualified_role_ids == frozenset()

    set_role_qualification(
        db_session, actor=scenario.head, membership=membership, role=scenario.role,
        is_qualified=True,
    )
    db_session.flush()

    after = build_scheduling_input(db_session, version=scenario.version)
    after_candidate = after.candidate_by_membership_id(membership.id)
    assert after_candidate is not None
    assert after_candidate.qualified_role_ids == frozenset({scenario.role.id})


def test_recording_unavailable_through_the_real_service_breaks_readiness(db_session):
    """Task 56's analogue of the qualification-revocation test above: an
    assignment that was ready becomes not-ready the moment
    :func:`set_availability` records ``UNAVAILABLE`` for the already-assigned
    membership -- the same function
    ``PUT /events/{id}/availability/{id}`` calls -- with no change to the
    assignment, the snapshot, or the schedule version itself. Availability is
    live candidate-state, never part of the version's own snapshot.
    """
    scenario = _Scenario(db_session)
    scenario.period.availability_locked_at = None  # factories default to locked
    scenario.assign(scenario.membership)
    assert scenario.readiness().is_ready is True

    set_availability(
        db_session, actor=scenario.head, membership=scenario.membership,
        event=scenario.event, availability_state=AVAILABILITY_UNAVAILABLE,
    )
    db_session.flush()

    result = scenario.readiness()
    assert result.is_ready is False
    assert ISSUE_UNAUTHORIZED_BLOCKER in _codes(result)


def test_backup_recorded_through_the_real_service_never_breaks_readiness(db_session):
    """BACKUP is feasible, never a blocker (Task 52): recording it for an
    already-assigned membership through the real service leaves readiness
    untouched, unlike ``UNAVAILABLE`` above.
    """
    scenario = _Scenario(db_session)
    scenario.period.availability_locked_at = None  # factories default to locked
    scenario.assign(scenario.membership)
    assert scenario.readiness().is_ready is True

    set_availability(
        db_session, actor=scenario.head, membership=scenario.membership,
        event=scenario.event, availability_state=AVAILABILITY_BACKUP,
    )
    db_session.flush()

    assert scenario.readiness().is_ready is True


def test_b_an_unfilled_requirement_is_not_ready(db_session):
    scenario = _Scenario(db_session, required_count=2, members=2)
    scenario.assign(scenario.memberships[0])  # one of two

    result = scenario.readiness()

    assert result.is_ready is False
    assert _codes(result) == [ISSUE_UNFILLED_REQUIREMENT]
    assert result.issues[0].schedule_version_requirement_id == scenario.requirement.id
    assert "needs 2 and has 1" in result.issues[0].message


def test_b2_a_stale_snapshot_is_detected_over_real_rows(db_session):
    """Task 23's comparison, reached through this service: raising the current
    staffing requirement after the snapshot was taken.
    """
    scenario = _Scenario(db_session)
    scenario.assign(scenario.membership)
    assert scenario.readiness().is_ready is True

    staffing = scenario.event.staffing_requirements[0]
    staffing.required_count = 3
    db_session.flush()

    result = scenario.readiness()
    assert result.staleness.is_stale is True
    assert result.is_ready is False


# --------------------------------------------------------------------------
# C / D / E -- Cross-ministry conflicts and what an override actually authorizes
# --------------------------------------------------------------------------


def test_c_a_cross_ministry_conflict_arising_after_assignment_is_not_ready(db_session):
    """The assignment was clean when it was made; another ministry finalized a
    schedule afterwards. Real ``DISTINCT ON`` resolution, no mocking.

    Since Task 79's correction this is reported as the hard-rule issue rather
    than as an unauthorized overridable blocker -- the version is equally
    unfinalizable either way, and now it says which rule was broken.
    """
    scenario = _Scenario(db_session)
    scenario.assign(scenario.membership)
    assert scenario.readiness().is_ready is True

    scenario.add_foreign_sunday_commitment(scenario.membership)

    result = scenario.readiness()
    assert result.is_ready is False
    assert _codes(result) == [ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT]
    assert "one ministry on the same day" in result.issues[0].message


def test_d_a_cross_ministry_conflict_cannot_be_overridden_at_all(db_session):
    """**This test asserted the opposite until Task 79's final correction.**

    The conflict exists before assignment. Manual assignment used to require an
    override and then accept one, recording ``sunday_conflict`` in the audit
    JSONB, and readiness read that payload back and passed the version. It now
    refuses the assignment outright -- with the reason supplied and without --
    so the audit row never exists and the contradictory schedule cannot be
    built in the first place.
    """
    scenario = _Scenario(db_session)
    scenario.add_foreign_sunday_commitment(scenario.membership)

    with pytest.raises(InvalidOperationError) as with_reason:
        scenario.assign(scenario.membership, override_reason="Head approved.")
    assert CROSS_MINISTRY_SUNDAY_CONFLICT in str(with_reason.value)
    assert "not overridable" in str(with_reason.value)

    db_session.rollback()
    scenario = _Scenario(db_session)
    scenario.add_foreign_sunday_commitment(scenario.membership)

    with pytest.raises(InvalidOperationError) as without_reason:
        scenario.assign(scenario.membership)
    assert CROSS_MINISTRY_SUNDAY_CONFLICT in str(without_reason.value)

    # Nothing was written on either path.
    from sqlalchemy import text

    overrides = db_session.execute(
        text(
            "SELECT count(*) FROM audit_event"
            " WHERE action = 'ASSIGNMENT_OVERRIDE_APPLIED'"
            "   AND after_values->'overridden_blockers' @> '[\"sunday_conflict\"]'"
        )
    ).scalar_one()
    assert overrides == 0


def test_d2_a_legacy_sunday_conflict_override_row_no_longer_finalizes(db_session):
    """**Historical contradictory data, reported rather than normalized.**

    A row written while the rule *was* overridable is reconstructed directly
    -- the assignment, the ``is_override`` flag and the real audit payload
    naming ``sunday_conflict`` -- because the service will no longer create
    one. Readiness must refuse it, and must not call the intact payload
    corrupt: it is a truthful record of a decision that is no longer allowed.
    """
    scenario = _Scenario(db_session)
    assignment = scenario.assign(scenario.membership)
    assignment.is_override = True
    assignment.override_reason = "Approved before the rule was locked."
    db_session.flush()

    record_audit_event(
        db_session,
        actor=scenario.head,
        action=ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
        target_table="assignment",
        target_id=assignment.id,
        ministry_id=scenario.ministry.id,
        summary="Legacy override",
        reason="Approved before the rule was locked.",
        after_values={"overridden_blockers": [BLOCKER_SUNDAY_CONFLICT]},
    )
    scenario.add_foreign_sunday_commitment(scenario.membership)
    db_session.flush()

    result = scenario.readiness()
    codes = _codes(result)
    assert result.is_ready is False
    assert ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT in codes
    # Intact history, not corruption -- the payload parsed fine.
    assert ISSUE_INVALID_OVERRIDE_PAYLOAD not in codes
    assert ISSUE_MISSING_OVERRIDE_AUDIT not in codes


def test_e_a_different_override_does_not_cover_a_newly_arisen_conflict(db_session):
    """The load-bearing rule, end to end on real data: the override was
    granted for an explicit UNAVAILABLE, and a cross-ministry conflict appears
    later.

    **It also proves the override mechanism is intact.** The unavailability
    override is written, is honoured, and keeps the version ready -- right up
    until the hard rule is broken by something nobody authorized. Making one
    rule absolute did not disable the rest.
    """
    scenario = _Scenario(db_session)
    f.make_availability(
        db_session, membership=scenario.membership, event=scenario.event,
        state=AVAILABILITY_UNAVAILABLE,
    )

    assignment = scenario.assign(scenario.membership, override_reason="Head approved.")
    from sqlalchemy import text

    stored = db_session.execute(
        text(
            "SELECT after_values->'overridden_blockers' AS blockers FROM audit_event"
            " WHERE target_table = 'assignment' AND target_id = :id"
            "   AND action = 'ASSIGNMENT_OVERRIDE_APPLIED'"
        ),
        {"id": assignment.id},
    ).scalar_one()
    assert stored == [BLOCKER_UNAVAILABLE]
    assert scenario.readiness().is_ready is True  # covered, so far

    scenario.add_foreign_sunday_commitment(scenario.membership)

    result = scenario.readiness()
    assert result.is_ready is False
    # The hard rule alone. The unavailability really was covered, which is what
    # shows the global override system still works.
    assert _codes(result) == [ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT]
    assert "one ministry on the same day" in result.issues[0].message


def test_e2_a_historical_blocker_that_no_longer_applies_is_not_a_failure(db_session):
    """Overridden for UNAVAILABLE; the member has since withdrawn that answer.
    History stays true, and readiness is restored.
    """
    scenario = _Scenario(db_session)
    availability = f.make_availability(
        db_session, membership=scenario.membership, event=scenario.event,
        state=AVAILABILITY_UNAVAILABLE,
    )
    scenario.assign(scenario.membership, override_reason="Head approved.")

    db_session.delete(availability)
    db_session.flush()

    result = scenario.readiness()
    assert result.is_ready is True


# --------------------------------------------------------------------------
# F -- overfill authorized through real Task 22 capacity overrides
# --------------------------------------------------------------------------


def test_f_overfill_is_accepted_only_with_enough_real_capacity_overrides(db_session):
    """required_count=1, three people assigned. The second and third each need
    a real capacity override, and Task 22 records ``capacity_full`` for them.
    """
    scenario = _Scenario(db_session, required_count=1, members=3)

    first = scenario.assign(scenario.memberships[0])
    assert first.is_override is False
    assert scenario.readiness().is_ready is True

    second = scenario.assign(scenario.memberships[1], override_reason="Extra hands.")
    assert second.is_override is True
    from sqlalchemy import text

    stored = db_session.execute(
        text(
            "SELECT after_values->'overridden_blockers' AS blockers FROM audit_event"
            " WHERE target_table = 'assignment' AND target_id = :id"
            "   AND action = 'ASSIGNMENT_OVERRIDE_APPLIED'"
        ),
        {"id": second.id},
    ).scalar_one()
    assert BLOCKER_CAPACITY_FULL in stored

    # excess = 1, authorized = 1 -> accepted.
    assert scenario.readiness().is_ready is True

    third = scenario.assign(scenario.memberships[2], override_reason="More hands.")
    assert third.is_override is True
    # excess = 2, authorized = 2 -> still accepted.
    assert scenario.readiness().is_ready is True


def test_f2_overfill_without_capacity_authorization_is_rejected(db_session):
    """The same overfilled shape, but the extra assignment's audit does not
    carry the capacity blocker -- so nothing authorized the third person.
    """
    scenario = _Scenario(db_session, required_count=1, members=2)
    scenario.assign(scenario.memberships[0])
    extra = scenario.assign(scenario.memberships[1], override_reason="Extra hands.")

    # Rewrite that assignment's audit payload to an override for something
    # else, leaving the capacity overfill unauthorized. (Editing history is
    # obviously not a real operation -- it is how this test reproduces a row
    # whose authorization does not cover what it is being used for.)
    from sqlalchemy import text

    db_session.execute(
        text(
            "UPDATE audit_event SET after_values ="
            " jsonb_set(after_values, '{overridden_blockers}', CAST(:blockers AS jsonb))"
            " WHERE target_table = 'assignment' AND target_id = :id"
            "   AND action = 'ASSIGNMENT_OVERRIDE_APPLIED'"
        ),
        {"id": extra.id, "blockers": '["unavailable"]'},
    )
    db_session.flush()

    result = scenario.readiness()
    assert result.is_ready is False
    assert ISSUE_UNAUTHORIZED_OVERFILL in _codes(result)


# --------------------------------------------------------------------------
# Absolute state, and read-only behavior, over real rows
# --------------------------------------------------------------------------


def test_a_deactivated_person_is_not_ready_even_with_a_full_override(db_session):
    """The override is for an explicit UNAVAILABLE -- a blocker that really is
    overridable. The cross-ministry conflict this test used to rely on is now
    absolute, and using it here would refuse the assignment before the
    deactivation under test could be reached.
    """
    scenario = _Scenario(db_session)
    f.make_availability(
        db_session, membership=scenario.membership, event=scenario.event,
        state=AVAILABILITY_UNAVAILABLE,
    )
    scenario.assign(scenario.membership, override_reason="Head approved.")
    assert scenario.readiness().is_ready is True

    scenario.membership.person.deactivated_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()

    result = scenario.readiness()
    assert result.is_ready is False
    assert "INACTIVE_PERSON" in _codes(result)


def test_readiness_writes_nothing_to_the_database(db_session):
    from sqlalchemy import text

    scenario = _Scenario(db_session)
    scenario.assign(scenario.membership)
    before = db_session.execute(text("SELECT count(*) FROM audit_event")).scalar_one()

    scenario.readiness()
    scenario.readiness()

    after = db_session.execute(text("SELECT count(*) FROM audit_event")).scalar_one()
    assert after == before
    assert db_session.execute(
        text("SELECT count(*) FROM assignment WHERE schedule_version_id = :v"),
        {"v": scenario.version.id},
    ).scalar_one() == 1


@pytest.mark.parametrize(
    "status",
    [SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW],
)
def test_any_working_status_can_be_inspected(db_session, status):
    scenario = _Scenario(db_session)
    scenario.assign(scenario.membership)
    scenario.version.status = status
    db_session.flush()

    assert scenario.readiness().is_ready is True
