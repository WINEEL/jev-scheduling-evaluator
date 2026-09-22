"""What actually protects a generated schedule from a concurrent writer.

Task 63 replaced a per-row writer, which re-read every fact just before each
INSERT, with a batch writer that prefetches once and writes once. "A narrower
window" is not a safety argument, so this module establishes what the
guarantees really are -- **per rule, from the database rather than from
reasoning about it**.

The findings these tests pin, in one paragraph, because the matrix is the
point:

- **One-person-per-event is the only assignment rule the database enforces**,
  through ``uq_assignment_version_event_membership``. It is genuinely
  path-independent: a conflicting row from anywhere makes the write fail.
- **The version's writability is enforced at write time by the application**,
  by a re-check taken one round trip before the INSERT.
- **Every other rule** -- staffing capacity, the serving maximum, linked-member
  same-date exclusions, qualification, availability, the church-wide conflict
  -- has **no** write-time protection, in either the old writer or the new
  one, and never did. Their guarantee is
  :func:`~app.services.finalization_readiness.get_finalization_readiness`,
  which :func:`~app.services.schedule_lifecycle.finalize_schedule_version`
  runs in the finalizing transaction and which refuses to publish a version
  that breaks any of them. A draft holding such a row is not corruption: it is
  the same state a head produces by lowering a serving limit the day after
  generating, which the readiness gate exists to catch.

**How the races are made deterministic.** A fact change is timed to land
*after* the prefetch and *before* the writes, by wrapping the prefetch. Those
changes happen on the test's own session, which is the strongest possible form
of the test: a change inside the writer's own transaction is the most visible
a change can be, so a writer that does not notice one certainly would not
notice another transaction's commit at the same instant.

**Two genuinely separate connections** are used where cross-transaction
visibility is the thing in question. Neither commits -- the harness's outer
transactions are rolled back as always, and nothing here truncates or deletes.

Every church, person, ministry and date is synthetic.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_UNAVAILABLE,
    ExistingCommitment,
    MembershipSameDateExclusion,
    MembershipServingLimit,
)
from app.scheduling.solver import SchedulingPolicy
from app.services import generated_assignment as writer
from app.services.errors import InvalidOperationError
from app.services.finalization_readiness import (
    ISSUE_EXCEEDS_SERVING_LIMIT,
    ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT,
    ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT,
    ISSUE_UNAUTHORIZED_BLOCKER,
    ISSUE_UNAUTHORIZED_OVERFILL,
    get_finalization_readiness,
)
from app.services.schedule_generation import (
    _resolve_scheduling_period,
    generate_draft_schedule,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
FIRST_SUNDAY = datetime.date(2026, 11, 15)
LENIENT = SchedulingPolicy(allow_no_response=True)


# --------------------------------------------------------------------------
# Scenario
# --------------------------------------------------------------------------


class _Scenario:
    """A latest DRAFT version, fully staffable by interchangeable volunteers."""

    def __init__(self, session, *, sundays: int = 1, roles: int = 1,
                 volunteers: int = 3, required_count: int = 1):
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
        for index in range(sundays):
            event = f.make_event(
                session, period=self.period,
                event_date=FIRST_SUNDAY + datetime.timedelta(days=7 * index),
            )
            self.events.append(event)
            for role in self.roles:
                f.make_staffing_requirement(
                    session, event=event, role=role, required_count=required_count,
                )
                f.make_version_requirement(
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

    @property
    def ministry_id(self) -> int:
        return _resolve_scheduling_period(self.session, self.version).ministry_id

    def requirements(self):
        return list(self.session.execute(
            select(ScheduleVersionRequirement)
            .where(ScheduleVersionRequirement.schedule_version_id == self.version.id)
            .order_by(ScheduleVersionRequirement.id)
        ).scalars())

    def generate(self):
        result = generate_draft_schedule(
            self.session, actor=self.head, version=self.version, policy=LENIENT,
        )
        self.session.flush()
        return result

    def assignment_count(self) -> int:
        return self.session.execute(
            text("SELECT count(*) FROM assignment WHERE schedule_version_id = :v"),
            {"v": self.version.id},
        ).scalar_one()


def _interpose(monkeypatch, change):
    """Run ``change`` between the writer's prefetch and its writes.

    The only way to land a change in that window deterministically. The real
    prefetch still runs, so the facts the rules see are the genuine ones as of
    a moment before the change.
    """
    real = writer._BatchFacts.prefetch.__func__

    def patched(cls, session, *, version, ministry_id, requirements, memberships):
        facts = real(cls, session, version=version, ministry_id=ministry_id,
                     requirements=requirements, memberships=memberships)
        # The rows the run actually resolved are handed to ``change``, so a
        # test can act on -- or deliberately avoid -- the very people the
        # solver picked.
        change(session, requirements=requirements, memberships=memberships)
        session.flush()
        return facts

    monkeypatch.setattr(writer._BatchFacts, "prefetch", classmethod(patched))


# ==========================================================================
# The ground the whole analysis stands on
# ==========================================================================


def test_the_application_runs_at_read_committed(db_session):
    """Every claim below depends on this. At REPEATABLE READ or SERIALIZABLE
    the analysis changes completely -- so if someone sets an isolation level on
    the Engine one day, this test is where they find out it matters.
    """
    level = db_session.execute(text("SHOW transaction_isolation")).scalar_one()

    assert level == "read committed"


def test_a_concurrent_session_cannot_see_this_run_s_uncommitted_rows(
    db_session, integration_engine
):
    """Why the overfill race below is real rather than theoretical.

    Two generations into one version, running at once, each prefetch a
    requirement they both believe is empty -- because under READ COMMITTED
    neither can see the other's uncommitted rows. Nothing in the database
    makes the second one notice.
    """
    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=3)
    scenario.generate()
    assert scenario.assignment_count() >= 1

    other = integration_engine.connect()
    transaction = other.begin()
    try:
        seen = other.execute(
            text("SELECT count(*) FROM assignment WHERE schedule_version_id = :v"),
            {"v": scenario.version.id},
        ).scalar_one()
    finally:
        transaction.rollback()
        other.close()

    assert seen == 0


def test_the_only_assignment_rule_the_database_encodes_is_one_per_event(db_session):
    """Stated from ``pg_constraint``, not from memory.

    The unique key covers one membership at most once per event per version.
    It says nothing about ``required_count``, serving maximums, linked pairs,
    qualification, availability or church-wide conflicts -- so no claim that it
    protects any of those can be made.
    """
    rows = db_session.execute(text("""
        SELECT con.conname, con.contype, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE rel.relname = 'assignment' AND con.contype IN ('u', 'c')
    """)).all()

    unique = [definition for _, contype, definition in rows if contype == "u"]
    assert unique == [
        "UNIQUE (schedule_version_id, event_id, ministry_membership_id)"
    ]
    # The one CHECK is about override bookkeeping, not about any scheduling
    # rule.
    checks = " ".join(d for _, contype, d in rows if contype == "c")
    assert "is_override" in checks
    for absent in ("required_count", "max_assignments", "availability", "qualified"):
        assert absent not in checks

    # And nothing enforces rules behind the application's back.
    assert db_session.execute(text(
        "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal"
    )).scalar_one() == 0


# ==========================================================================
# Rules with a real write-time guarantee
# ==========================================================================


def test_one_person_per_event_is_refused_by_the_database_itself(
    db_session, monkeypatch
):
    """A conflicting row appearing after the prefetch is not caught by any
    rule -- the writer cannot see it -- and is refused by the unique index when
    the INSERT runs. This is the one rule where that is true.
    """
    scenario = _Scenario(db_session, sundays=1, roles=2, volunteers=2)
    all_requirements = scenario.requirements()

    def take_the_same_person_in_this_event(session, *, requirements, memberships):
        # The person the run is about to place, taken for the *other* position
        # in the same event by somebody else.
        other = next(r for r in all_requirements if r.id != requirements[0].id)
        session.add(Assignment(
            schedule_version_requirement_id=other.id,
            ministry_membership_id=memberships[0].id,
            schedule_version_id=scenario.version.id,
            event_id=other.event_id,
            ministry_id=other.ministry_id,
            is_override=False,
        ))

    _interpose(monkeypatch, take_the_same_person_in_this_event)

    with pytest.raises(IntegrityError) as excinfo:
        scenario.generate()

    assert "uq_assignment_version_event_membership" in str(excinfo.value)
    db_session.rollback()


def test_a_version_finalized_after_the_prefetch_is_refused_before_the_write(
    db_session, monkeypatch
):
    """**The one race with no downstream backstop.** Rows added to a FINALIZED
    version are rows added to published history, and readiness would report the
    version ready, because each individual row really is valid. The pre-write
    re-check is what stops it.
    """
    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=3)

    def finalize_it(session, *, requirements, memberships):
        session.execute(
            text("UPDATE schedule_version SET status = :s, finalized_at = :t"
                 " WHERE id = :v"),
            {"s": SCHEDULE_VERSION_STATUS_FINALIZED,
             "t": datetime.datetime.now(UTC), "v": scenario.version.id},
        )

    _interpose(monkeypatch, finalize_it)

    with pytest.raises(InvalidOperationError, match="finalized schedule version"):
        scenario.generate()

    assert scenario.assignment_count() == 0


def test_a_successor_created_after_the_prefetch_is_refused_before_the_write(
    db_session, monkeypatch
):
    """Task 34 re-probed this before every row, so a successor appearing
    mid-run aborted the whole generation. Batching replaced N probes with one
    on entry; the pre-write re-check is what restores the parity.
    """
    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=3)

    def create_successor(session, *, requirements, memberships):
        session.add(ScheduleVersion(
            schedule_id=scenario.version.schedule_id,
            scheduling_period_id=scenario.version.scheduling_period_id,
            version_number=scenario.version.version_number + 1,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        ))

    _interpose(monkeypatch, create_successor)

    with pytest.raises(InvalidOperationError, match="superseded by a newer version"):
        scenario.generate()

    assert scenario.assignment_count() == 0


def test_the_re_check_reads_the_status_the_database_has_not_the_one_in_memory(
    db_session, monkeypatch
):
    """``flush()`` never expires an ORM attribute, so the version object still
    says DRAFT throughout. Only a fresh read can tell the truth.
    """
    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=3)

    def finalize_it(session, *, requirements, memberships):
        session.execute(
            text("UPDATE schedule_version SET status = :s, finalized_at = :t"
                 " WHERE id = :v"),
            {"s": SCHEDULE_VERSION_STATUS_FINALIZED,
             "t": datetime.datetime.now(UTC), "v": scenario.version.id},
        )

    _interpose(monkeypatch, finalize_it)

    with pytest.raises(InvalidOperationError):
        scenario.generate()

    # The in-memory object never noticed; the re-check did.
    assert scenario.version.status == SCHEDULE_VERSION_STATUS_DRAFT


def test_ministry_integrity_is_a_foreign_key_not_a_check_anyone_performs(db_session):
    """A membership from another ministry cannot be written into this
    version's requirement, whatever any writer believes, because both
    composite foreign keys route through the single ``ministry_id``.
    """
    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=2)
    other_ministry = f.make_ministry(db_session, church=scenario.church, name="AV")
    outsider = f.make_membership(
        db_session,
        person=f.make_person(db_session, church=scenario.church, name="Outsider"),
        ministry=other_ministry,
    )
    db_session.flush()
    requirement = scenario.requirements()[0]

    db_session.add(Assignment(
        schedule_version_requirement_id=requirement.id,
        ministry_membership_id=outsider.id,
        schedule_version_id=scenario.version.id,
        event_id=requirement.event_id,
        ministry_id=requirement.ministry_id,
        is_override=False,
    ))
    with pytest.raises(IntegrityError) as excinfo:
        db_session.flush()

    assert "fk_assignment_membership_ministry" in str(excinfo.value)
    db_session.rollback()


# ==========================================================================
# Rules with no write-time guarantee -- and the gate that actually holds them
# ==========================================================================


def _assert_written_then_caught_at_finalization(scenario, expected_code):
    """The shape every rule below shares: the run completes, and the schedule
    cannot be published until somebody fixes it.
    """
    assert scenario.assignment_count() >= 1
    readiness = get_finalization_readiness(scenario.session, version=scenario.version)
    assert expected_code in {issue.code for issue in readiness.issues}
    assert readiness.is_ready is False


def test_capacity_filled_by_another_writer_overfills_and_is_caught_at_finalization(
    db_session, monkeypatch
):
    """Over-filling is an aggregate compared against another table, so no
    single-row constraint can express it -- the model says so, and
    ``pg_constraint`` above confirms it. Neither writer ever prevented this.
    """
    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=3)
    requirement = scenario.requirements()[0]

    def someone_else_fills_it(session, *, requirements, memberships):
        # Deliberately somebody the run did *not* pick, so this is genuinely an
        # over-fill of a ``required_count=1`` position rather than the
        # one-per-event rule the unique index already covers.
        taken = {m.id for m in memberships}
        spare = next(m for m in scenario.memberships if m.id not in taken)
        session.add(Assignment(
            schedule_version_requirement_id=requirement.id,
            ministry_membership_id=spare.id,
            schedule_version_id=scenario.version.id,
            event_id=requirement.event_id,
            ministry_id=requirement.ministry_id,
            is_override=False,
        ))

    _interpose(monkeypatch, someone_else_fills_it)
    scenario.generate()

    _assert_written_then_caught_at_finalization(scenario, ISSUE_UNAUTHORIZED_OVERFILL)


def test_a_serving_limit_lowered_after_the_prefetch_is_caught_at_finalization(
    db_session, monkeypatch
):
    """The same end state a head reaches with no concurrency at all, by
    lowering a maximum the day after generating. Gate 4 exists for exactly it.
    """
    scenario = _Scenario(db_session, sundays=2, roles=1, volunteers=1)

    def lower_the_limit(session, *, requirements, memberships):
        session.add(MembershipServingLimit(
            ministry_membership_id=scenario.memberships[0].id,
            scheduling_period_id=scenario.period.id,
            ministry_id=scenario.ministry.id,
            max_assignments=1,
        ))

    _interpose(monkeypatch, lower_the_limit)
    scenario.generate()

    _assert_written_then_caught_at_finalization(scenario, ISSUE_EXCEEDS_SERVING_LIMIT)


def test_a_linked_pair_recorded_after_the_prefetch_is_caught_at_finalization(
    db_session, monkeypatch
):
    scenario = _Scenario(db_session, sundays=1, roles=2, volunteers=2)

    def link_them(session, *, requirements, memberships):
        low, high = sorted(
            (scenario.memberships[0].id, scenario.memberships[1].id)
        )
        session.add(MembershipSameDateExclusion(
            membership_a_id=low, membership_b_id=high,
            scheduling_period_id=scenario.period.id,
            ministry_id=scenario.ministry.id,
        ))

    _interpose(monkeypatch, link_them)
    scenario.generate()

    _assert_written_then_caught_at_finalization(
        scenario, ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT
    )


@pytest.mark.parametrize("label", ["qualification", "availability"])
def test_an_overridable_fact_changed_after_the_prefetch_is_caught_at_finalization(
    db_session, monkeypatch, label
):
    """Qualification and availability land in gate 3, which compares the
    blockers that apply *now* against the ones an override actually authorized
    -- and a generated row authorized none.

    The church-wide conflict used to be a third case here. It moved to its own
    test below when Task 79 made it absolute: it is no longer one of the
    blockers gate 3 compares, so asserting ``UNAUTHORIZED_BLOCKER`` for it
    would be asserting the wrong gate caught it.
    """
    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=1)
    membership = scenario.memberships[0]

    def change_it(session, *, requirements, memberships):
        if label == "qualification":
            session.execute(
                text("UPDATE role_qualification SET is_qualified = false"
                     " WHERE ministry_membership_id = :m"),
                {"m": membership.id},
            )
        else:
            session.execute(
                text("UPDATE availability SET availability_state = :s"
                     " WHERE ministry_membership_id = :m"),
                {"s": AVAILABILITY_UNAVAILABLE, "m": membership.id},
            )

    _interpose(monkeypatch, change_it)
    scenario.generate()

    _assert_written_then_caught_at_finalization(scenario, ISSUE_UNAUTHORIZED_BLOCKER)


def test_a_church_conflict_recorded_after_the_prefetch_is_caught_at_finalization(
    db_session, monkeypatch
):
    """**The hard rule's backstop against a race.**

    Another ministry claims this person *after* the run's prefetch read the
    conflicts and *before* it writes. The run cannot see it, so the row is
    written -- and finalization refuses the version, reporting the hard rule
    by name rather than as an unauthorized overridable blocker (Task 79's
    correction). No override exists that could excuse it, because a generated
    row authorizes nothing and the rule admits no authorization at all.
    """
    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=1)
    membership = scenario.memberships[0]

    def claim_them_elsewhere(session, *, requirements, memberships):
        session.add(ExistingCommitment(
            person_id=membership.person_id,
            commitment_date=scenario.events[0].event_date,
            source_ministry_id=None,
            reason="Synthetic cross-ministry commitment",
        ))

    _interpose(monkeypatch, claim_them_elsewhere)
    scenario.generate()

    _assert_written_then_caught_at_finalization(
        scenario, ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT
    )


def test_finalization_is_the_gate_that_actually_refuses_such_a_version(db_session):
    """The backstop is only a backstop if finalizing consults it. It does, in
    the finalizing transaction, against current state.
    """
    from app.services.schedule_lifecycle import (
        finalize_schedule_version,
        submit_schedule_version_for_review,
    )

    scenario = _Scenario(db_session, sundays=2, roles=1, volunteers=1)
    scenario.generate()
    submit_schedule_version_for_review(
        db_session, actor=scenario.head, version=scenario.version
    )
    # The limit is agreed downwards *after* the draft was built and reviewed --
    # exactly the state a mid-run race would also produce.
    db_session.add(MembershipServingLimit(
        ministry_membership_id=scenario.memberships[0].id,
        scheduling_period_id=scenario.period.id,
        ministry_id=scenario.ministry.id,
        max_assignments=1,
    ))
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="readiness issue"):
        finalize_schedule_version(
            db_session, actor=scenario.head, version=scenario.version
        )


def test_a_superseded_version_can_never_be_published(db_session):
    """Why rows written into a version that a successor overtakes are
    unpublishable rather than dangerous -- the remaining consequence of any
    supersession race the re-check does not catch.
    """
    from app.services.schedule_lifecycle import submit_schedule_version_for_review

    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=2)
    scenario.generate()
    db_session.add(ScheduleVersion(
        schedule_id=scenario.version.schedule_id,
        scheduling_period_id=scenario.version.scheduling_period_id,
        version_number=scenario.version.version_number + 1,
        status=SCHEDULE_VERSION_STATUS_DRAFT,
    ))
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="superseded"):
        submit_schedule_version_for_review(
            db_session, actor=scenario.head, version=scenario.version
        )


# ==========================================================================
# Manual assignment is untouched
# ==========================================================================


def test_manual_assignment_still_re_reads_every_fact_for_its_one_row(db_session):
    """Task 63 moved the rules, not the reads. ``assign_member`` still issues
    its own query per fact, so a change committed a moment before the call is
    seen -- which is the whole behaviour a single considered edit wants.
    """
    from app.services.assignment import assign_member

    scenario = _Scenario(db_session, sundays=1, roles=1, volunteers=2)
    requirement = scenario.requirements()[0]
    membership = scenario.memberships[0]

    # A fact changed after everything was loaded, and before the call.
    db_session.execute(
        text("UPDATE role_qualification SET is_qualified = false"
             " WHERE ministry_membership_id = :m"),
        {"m": membership.id},
    )
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="not currently qualified"):
        assign_member(
            db_session, actor=scenario.head, requirement=requirement,
            membership=membership, override_reason=None,
        )


def test_manual_assignment_takes_no_writability_re_check_of_its_own(db_session):
    """The pre-write re-check belongs to the batch writer, which has a prefetch
    to protect. ``assign_member`` checks the version once and writes
    immediately, exactly as it always has -- there is no window for a second
    check to close.
    """
    import app.services.assignment as manual

    assert not hasattr(manual, "_require_still_writable")
    assert not hasattr(manual, "_writability_statement")
