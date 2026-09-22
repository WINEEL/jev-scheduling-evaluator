"""The church-wide hard rule, end to end (Task 79 §15).

> **One Person may serve at most ONE ministry on the same Sunday.**

This file exists to prove that foundation still holds after Task 79's identity
and governance changes, and to record -- in executable form -- **where the rule
is actually enforced**, because it is enforced in four different places and a
reader looking for "the check" would find only one of them.

The scenario is §15's own example, with synthetic names: one canonical Person
belongs to two ministries, is committed to the first on a Sunday by a finalized
schedule, and must not be placeable in the second on that date.

Where enforcement lives, and what each layer contributes
--------------------------------------------------------
1. :func:`app.services.sunday_conflict.get_person_sunday_conflicts` -- **the
   rule itself**, defined once (ADR 0002/0003). Every layer below calls it;
   none restates it. It resolves the authoritative version, reads the
   version's snapshot date, ignores cancelled events, and excludes the target
   ministry's own rows.
2. :mod:`app.services.scheduling_input_builder` -> ``MemberInput.blocked_dates``
   -- the solver's input. A blocked date is removed from the candidate's
   domain as a **hard constraint**, so no preference, fairness weight or
   balance pass can trade it away.
3. :func:`app.services.assignment.assign_member` -- manual placement. The
   conflict is an **absolute** rule
   (:func:`app.services.assignment_rules.require_absolute_rules`): refused with
   an ``override_reason`` and without, identically.
4. :mod:`app.services.finalization_readiness` -- the gate. The conflict is
   checked with the *absolutes*, before override history is consulted at all,
   so no audit row of any shape lets a contradictory version finalize.

**The gap this file used to pin is closed.** Until Task 79's final correction
the conflict was one of Task 22's five bounded *overridable* blockers: a
ministry manager supplying a reason could place the person anyway, and layer 4
then accepted the row because the override had been authorized. It is now
absolute at both layers, and the tests below assert the refusal -- with a
reason, without one, and for historical rows that were written while it was
still allowed.

**The rest of the override system is untouched**, and one test here proves it:
an explicit ``UNAVAILABLE`` on an unconflicted Sunday still accepts a reason,
is still flagged ``is_override``, and still records what it bypassed.

Rollback-isolated; every row is synthetic and nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest

from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
)
from app.models.scheduling_input import AVAILABILITY_UNAVAILABLE, Event
from app.scheduling.solver import SchedulingPolicy, solve_schedule
from app.services.assignment import assign_member
from app.services.assignment_policy import (
    BLOCKER_SUNDAY_CONFLICT,
    BLOCKER_UNAVAILABLE,
)
from app.services.assignment_rules import CROSS_MINISTRY_SUNDAY_CONFLICT
from app.services.audit import (
    ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
    record_audit_event,
)
from app.services.errors import InvalidOperationError
from app.services.schedule_lifecycle import finalize_schedule_version
from app.services.finalization_readiness import (
    ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT,
    ISSUE_INVALID_OVERRIDE_PAYLOAD,
    ISSUE_MISSING_OVERRIDE_AUDIT,
    get_finalization_readiness,
)
from app.services.scheduling_input_builder import build_scheduling_input
from app.services.serving_history import find_cross_ministry_conflicts
from app.services.sunday_conflict import get_person_sunday_conflicts
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
SUNDAY = datetime.date(2026, 10, 11)
OTHER_SUNDAY = datetime.date(2026, 10, 18)
FINALIZED_AT = datetime.datetime(2026, 9, 20, tzinfo=UTC)

#: Nobody in this file answers an availability question, so the run has to
#: treat a missing answer as "available" for anybody to be schedulable at all.
#: That keeps the only reason a candidate can be excluded here the one under
#: test.
POLICY = SchedulingPolicy(allow_no_response=True)


class World:
    """§15's example: one Person, two ministries, one contested Sunday.

    "Setup" and "AV" here are synthetic ministries built by the factories with
    random name suffixes -- nothing in this file reads or writes production
    data, and no real ministry is imported.
    """

    def __init__(self, session):
        self.session = session
        self.church = f.make_church(session)
        self.setup = f.make_ministry(session, church=self.church, name="SetupMin")
        self.av = f.make_ministry(session, church=self.church, name="AvMin")

        # The whole point of ADR 0001: **one** Person, two memberships.
        self.person = f.make_person(session, church=self.church, name="Shared Person")
        self.setup_membership = f.make_membership(
            session, person=self.person, ministry=self.setup
        )
        self.av_membership = f.make_membership(
            session, person=self.person, ministry=self.av
        )

        # Somebody AV can use instead, so a solver run has a feasible answer
        # and "nobody was scheduled" cannot be mistaken for "the rule worked".
        self.spare = f.make_person(session, church=self.church, name="AV Spare")
        self.spare_membership = f.make_membership(
            session, person=self.spare, ministry=self.av
        )

        self.av_head = f.make_person(session, church=self.church, name="AV Head")
        f.make_membership(
            session, person=self.av_head, ministry=self.av, is_head=True
        )

        self.setup_period = f.make_period(
            session=session, ministry=self.setup,
            start=datetime.date(2026, 10, 1), end=datetime.date(2026, 12, 31),
        )
        self.av_period = f.make_period(
            session=session, ministry=self.av,
            start=datetime.date(2026, 10, 1), end=datetime.date(2026, 12, 31),
        )
        self.setup_role = f.make_role(session=session, ministry=self.setup)
        self.av_role = f.make_role(session=session, ministry=self.av)

    def commit_setup_on(self, date, *, cancelled=False):
        """Finalize a Setup schedule placing the shared Person on ``date``.

        This is what makes the date "already spoken for" church-wide: an
        authoritative assignment, not a draft and not a note somebody typed.
        """
        event = f.make_event(
            session=self.session, period=self.setup_period, event_date=date,
            cancelled=cancelled,
        )
        schedule = f.make_schedule(session=self.session, period=self.setup_period)
        version = f.make_version(
            session=self.session, schedule=schedule, period=self.setup_period,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT,
        )
        requirement = f.make_version_requirement(
            session=self.session, version=version, event=event, role=self.setup_role
        )
        return f.make_assignment(
            session=self.session, requirement=requirement,
            membership=self.setup_membership,
        )

    def av_draft_for(self, dates, *, required_count=1):
        """A mutable AV working version with one requirement per date."""
        schedule = f.make_schedule(session=self.session, period=self.av_period)
        version = f.make_version(
            session=self.session, schedule=schedule, period=self.av_period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        requirements = {}
        for date in dates:
            event = f.make_event(
                session=self.session, period=self.av_period, event_date=date
            )
            # The live staffing configuration as well as the snapshot: the
            # builder refuses to schedule a version whose snapshot no longer
            # matches current configuration (Task 23's staleness check), and a
            # fixture that wrote only the snapshot would be testing that rule
            # rather than this one.
            f.make_staffing_requirement(
                session=self.session, event=event, role=self.av_role,
                required_count=required_count,
            )
            requirements[date] = f.make_version_requirement(
                session=self.session, version=version, event=event,
                role=self.av_role, required_count=required_count,
            )
        return version, requirements

    def qualify_av(self):
        """Both AV candidates approved for the AV role, so qualification is
        never what a refusal is really about."""
        for membership in (self.av_membership, self.spare_membership):
            f.make_qualification(
                session=self.session, membership=membership, role=self.av_role,
                decided_by=self.av_head,
            )
        self.session.flush()


@pytest.fixture
def world(db_session):
    return World(db_session)


# --------------------------------------------------------------------------
# Layer 1: the rule itself
# --------------------------------------------------------------------------


class TestTheRuleQuery:
    """:mod:`app.services.sunday_conflict` -- the single definition."""

    def test_a_finalized_commitment_elsewhere_blocks_the_date(self, world):
        world.commit_setup_on(SUNDAY)
        world.session.flush()

        result = get_person_sunday_conflicts(
            world.session,
            person_id=world.person.id,
            conflict_date=SUNDAY,
            target_ministry_id=world.av.id,
        )
        assert result.is_blocked is True

    def test_it_is_the_canonical_person_that_is_blocked_not_a_membership(self, world):
        """§15: the rule applies through Person identity. The AV membership is
        a different row from the Setup one, and the block still reaches it --
        which is exactly what ADR 0001's one-human-one-Person buys."""
        world.commit_setup_on(SUNDAY)
        world.session.flush()

        blocked = get_person_sunday_conflicts(
            world.session, person_id=world.person.id, conflict_date=SUNDAY,
            target_ministry_id=world.av.id,
        )
        spare = get_person_sunday_conflicts(
            world.session, person_id=world.spare.id, conflict_date=SUNDAY,
            target_ministry_id=world.av.id,
        )
        assert blocked.is_blocked is True
        assert spare.is_blocked is False

    def test_another_sunday_is_not_blocked(self, world):
        world.commit_setup_on(SUNDAY)
        world.session.flush()

        result = get_person_sunday_conflicts(
            world.session, person_id=world.person.id, conflict_date=OTHER_SUNDAY,
            target_ministry_id=world.av.id,
        )
        assert result.is_blocked is False

    def test_a_cancelled_event_blocks_nothing(self, world):
        """Nobody serves a service that did not happen."""
        world.commit_setup_on(SUNDAY, cancelled=True)
        world.session.flush()

        result = get_person_sunday_conflicts(
            world.session, person_id=world.person.id, conflict_date=SUNDAY,
            target_ministry_id=world.av.id,
        )
        assert result.is_blocked is False


# --------------------------------------------------------------------------
# Layer 2: the solver's input -- a hard constraint, not a preference
# --------------------------------------------------------------------------


class TestTheSolverCannotTradeItAway:
    def test_the_date_arrives_at_the_solver_as_a_blocked_date(self, world):
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, _ = world.av_draft_for([SUNDAY])
        world.session.flush()

        scheduling_input = build_scheduling_input(world.session, version=version)
        shared = next(
            candidate
            for candidate in scheduling_input.candidates
            if candidate.person_id == world.person.id
        )
        assert shared.is_blocked_on(SUNDAY) is True

    def test_the_solver_schedules_the_other_candidate_instead(self, world):
        """**The regression §15 asks for.** AV needs one person on the
        contested Sunday, two are qualified and available, and the one already
        committed to Setup is not chosen -- not because they scored worse, but
        because the date is not in their domain at all."""
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, _ = world.av_draft_for([SUNDAY])
        world.session.flush()

        result = solve_schedule(
            build_scheduling_input(world.session, version=version), policy=POLICY
        )
        assigned = {
            placement.membership_id for placement in result.proposed_assignments
        }
        assert world.spare_membership.id in assigned
        assert world.av_membership.id not in assigned

    def test_the_shared_person_is_still_used_on_an_unconstested_sunday(self, world):
        """The contrast that makes the test above mean something: the rule
        blocks one date, not the person."""
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, _ = world.av_draft_for([SUNDAY, OTHER_SUNDAY], required_count=2)
        world.session.flush()

        result = solve_schedule(
            build_scheduling_input(world.session, version=version), policy=POLICY
        )
        by_date: dict[datetime.date, set[int]] = {}
        requirements = {r.id: r for r in version.requirements}
        for placement in result.proposed_assignments:
            date = requirements[placement.requirement_id].event_date
            by_date.setdefault(date, set()).add(placement.membership_id)

        assert world.av_membership.id not in by_date.get(SUNDAY, set())
        assert world.av_membership.id in by_date.get(OTHER_SUNDAY, set())


# --------------------------------------------------------------------------
# Layer 3: manual assignment
# --------------------------------------------------------------------------


class TestManualAssignment:
    def test_a_plain_manual_assignment_is_refused(self, world):
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([SUNDAY])
        world.session.flush()

        with pytest.raises(InvalidOperationError) as caught:
            assign_member(
                world.session,
                actor=world.av_head,
                requirement=requirements[SUNDAY],
                membership=world.av_membership,
            )
        assert "conflict" in str(caught.value).lower()

    def test_the_same_person_is_accepted_on_a_free_sunday(self, world):
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([SUNDAY, OTHER_SUNDAY])
        world.session.flush()

        assignment = assign_member(
            world.session,
            actor=world.av_head,
            requirement=requirements[OTHER_SUNDAY],
            membership=world.av_membership,
        )
        assert assignment.id is not None

    def test_an_explicit_override_is_still_refused(self, world):
        """**The correction.** This test asserted the opposite until Task 79's
        review: a ministry manager supplying an ``override_reason`` could place
        the person anyway, and the row was flagged ``is_override`` with an
        audit event naming ``sunday_conflict``.

        One Person serves at most one ministry per Sunday is a hard rule of
        this church. A rule a reason can talk its way past is not hard, so the
        conflict left the bounded overridable catalogue and became an absolute
        rule. The refusal says so, because a head who has just been refused
        needs to know that a better reason is not the remedy.
        """
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([SUNDAY])
        world.session.flush()

        with pytest.raises(InvalidOperationError) as caught:
            assign_member(
                world.session,
                actor=world.av_head,
                requirement=requirements[SUNDAY],
                membership=world.av_membership,
                override_reason="Nobody else could cover it",
            )
        message = str(caught.value)
        assert CROSS_MINISTRY_SUNDAY_CONFLICT in message
        assert "not overridable" in message

    def test_nothing_is_written_when_the_override_is_refused(self, world):
        """No assignment, and no override audit row -- so the contradictory
        state cannot even be built, let alone finalized."""
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([SUNDAY])
        world.session.flush()

        with pytest.raises(InvalidOperationError):
            assign_member(
                world.session,
                actor=world.av_head,
                requirement=requirements[SUNDAY],
                membership=world.av_membership,
                override_reason="Nobody else could cover it",
            )

        assert world.session.execute(
            select(func.count())
            .select_from(Assignment)
            .where(Assignment.schedule_version_id == version.id)
        ).scalar_one() == 0
        assert world.session.execute(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == ACTION_ASSIGNMENT_OVERRIDE_APPLIED)
        ).scalar_one() == 0

    def test_an_unrelated_overridable_blocker_still_accepts_an_override(self, world):
        """**The global override mechanism was not disabled.**

        The same head, the same ministry, the same person -- on a Sunday with
        no cross-ministry conflict, and with an explicit UNAVAILABLE answer
        that *is* one of the bounded overridable blockers. The reason is
        honoured, the row is flagged, and the audit payload names exactly what
        was bypassed.
        """
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([OTHER_SUNDAY])
        requirement = requirements[OTHER_SUNDAY]
        f.make_availability(
            session=world.session,
            membership=world.av_membership,
            event=world.session.get(Event, requirement.event_id),
            state=AVAILABILITY_UNAVAILABLE,
        )
        world.session.flush()

        assignment = assign_member(
            world.session,
            actor=world.av_head,
            requirement=requirement,
            membership=world.av_membership,
            override_reason="They offered to swap",
        )
        world.session.flush()

        assert assignment.is_override is True
        stored = world.session.execute(
            select(AuditEvent.after_values).where(
                AuditEvent.action == ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
                AuditEvent.target_id == assignment.id,
            )
        ).scalar_one()
        assert stored["overridden_blockers"] == [BLOCKER_UNAVAILABLE]

    def test_the_finalization_gate_rejects_a_legacy_audited_override(self, world):
        """**Historical contradictory data: reported, never normalized.**

        The service will not create such a row any more, so one is
        reconstructed exactly as it would have been written while the rule was
        overridable -- ``is_override`` set, with a real audit payload naming
        ``sunday_conflict``. The version must not finalize, and the intact
        payload must not be reported as corrupt: it truthfully records a
        decision that is no longer allowed.
        """
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([SUNDAY])
        world.session.flush()

        assignment = f.make_assignment(
            session=world.session,
            requirement=requirements[SUNDAY],
            membership=world.av_membership,
            is_override=True,
            override_reason="Approved before the rule was locked",
        )
        world.session.flush()
        record_audit_event(
            world.session,
            actor=world.av_head,
            action=ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
            target_table="assignment",
            target_id=assignment.id,
            ministry_id=world.av.id,
            summary="Legacy override",
            reason="Approved before the rule was locked",
            after_values={"overridden_blockers": [BLOCKER_SUNDAY_CONFLICT]},
        )
        world.session.flush()

        readiness = get_finalization_readiness(world.session, version=version)
        codes = {issue.code for issue in readiness.issues}
        assert readiness.is_ready is False
        assert ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT in codes
        assert ISSUE_INVALID_OVERRIDE_PAYLOAD not in codes

    def test_the_finalization_gate_rejects_an_unaudited_conflict(self, world):
        """A row claiming ``is_override=True`` with no audit authorizing it --
        a hand-edit or a bad migration -- is refused twice over: for the hard
        rule, and for the missing authorization."""
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([SUNDAY])
        world.session.flush()

        f.make_assignment(
            session=world.session,
            requirement=requirements[SUNDAY],
            membership=world.av_membership,
            is_override=True,
            override_reason="Typed straight into the database",
        )
        world.session.flush()

        readiness = get_finalization_readiness(world.session, version=version)
        codes = {issue.code for issue in readiness.issues}
        assert readiness.is_ready is False
        assert ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT in codes
        assert ISSUE_MISSING_OVERRIDE_AUDIT in codes

    def test_a_conflicting_version_cannot_actually_be_finalized(self, world):
        """**The end of the line, asserted rather than inferred.**

        Readiness reporting an issue is only useful because
        :func:`app.services.schedule_lifecycle.finalize_schedule_version`
        refuses on it. This drives the real transition: the version stays
        REVIEW, ``finalized_at`` stays ``NULL``, and no other ministry's
        conflict query will ever read these assignments as commitments.
        """
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([SUNDAY])
        f.make_assignment(
            session=world.session,
            requirement=requirements[SUNDAY],
            membership=world.av_membership,
        )
        version.status = SCHEDULE_VERSION_STATUS_REVIEW
        world.session.flush()

        with pytest.raises(InvalidOperationError) as caught:
            finalize_schedule_version(
                world.session, actor=world.av_head, version=version
            )
        assert "readiness issue" in str(caught.value)

        world.session.refresh(version)
        assert version.status == SCHEDULE_VERSION_STATUS_REVIEW
        assert version.finalized_at is None

    def test_a_clean_version_still_finalizes(self, world):
        """The contrast that makes the test above mean something: the same
        ministry, the same person, an uncontested Sunday -- and finalization
        works exactly as it always did."""
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([OTHER_SUNDAY])
        f.make_assignment(
            session=world.session,
            requirement=requirements[OTHER_SUNDAY],
            membership=world.av_membership,
        )
        version.status = SCHEDULE_VERSION_STATUS_REVIEW
        world.session.flush()

        finalized = finalize_schedule_version(
            world.session, actor=world.av_head, version=version
        )
        assert finalized.status == SCHEDULE_VERSION_STATUS_FINALIZED
        assert finalized.finalized_at is not None

    def test_a_plain_conflicting_row_is_rejected_by_the_gate_too(self, world):
        """No override claimed at all -- the most ordinary shape contradictory
        historical data takes, and the one an import would produce."""
        world.commit_setup_on(SUNDAY)
        world.qualify_av()
        version, requirements = world.av_draft_for([SUNDAY])
        world.session.flush()

        f.make_assignment(
            session=world.session,
            requirement=requirements[SUNDAY],
            membership=world.av_membership,
        )
        world.session.flush()

        readiness = get_finalization_readiness(world.session, version=version)
        codes = {issue.code for issue in readiness.issues}
        assert readiness.is_ready is False
        assert ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT in codes


# --------------------------------------------------------------------------
# The state §13 must never turn into a count
# --------------------------------------------------------------------------


class TestServingHistoryReportsItRatherThanCountingIt:
    def test_two_finalized_ministries_on_one_sunday_are_a_reported_conflict(
        self, world
    ):
        """If such a state ever exists -- through an import, a direct database
        write, or the override above followed by finalization -- the serving
        summary reports it as a hard conflict instead of quietly counting two
        services (Task 79 §13)."""
        world.commit_setup_on(SUNDAY)

        # A second authoritative commitment, in AV, on the same date.
        event = f.make_event(
            session=world.session, period=world.av_period, event_date=SUNDAY
        )
        schedule = f.make_schedule(session=world.session, period=world.av_period)
        version = f.make_version(
            session=world.session, schedule=schedule, period=world.av_period,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT,
        )
        requirement = f.make_version_requirement(
            session=world.session, version=version, event=event, role=world.av_role
        )
        f.make_assignment(
            session=world.session, requirement=requirement,
            membership=world.av_membership,
        )
        world.session.flush()

        conflicts = find_cross_ministry_conflicts(
            world.session,
            person_ids=[world.person.id],
            church_id=world.church.id,
            as_of=SUNDAY + datetime.timedelta(days=1),
        )
        assert len(conflicts) == 1
        assert conflicts[0].event_date == SUNDAY
        assert set(conflicts[0].ministry_names) == {world.setup.name, world.av.name}
