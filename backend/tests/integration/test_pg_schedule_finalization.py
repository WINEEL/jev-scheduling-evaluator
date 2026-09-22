"""REVIEW -> FINALIZED against real PostgreSQL rows (Task 27).

The offline suite proves the control flow with the gates stubbed. Only a real
database can prove the thing finalization actually exists to do: that after it
commits, **ADR 0003's authority genuinely transfers** -- Task 21's conflict
query, run unmocked over real rows, stops reading the old finalized version and
starts reading the new one, without anybody having mutated the old version or
copied an assignment anywhere.

Nothing is mocked here: Task 26's readiness gate, Task 21's ``DISTINCT ON``
resolution, the real JSONB audit payload and real transaction rollback are all
exercised as they ship. Every test is rollback-isolated; nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services.assignment import assign_member
from app.services.audit import ACTION_SCHEDULE_VERSION_FINALIZED
from app.services.errors import InvalidOperationError
from app.services.schedule_lifecycle import finalize_schedule_version
from app.services.sunday_conflict import get_person_sunday_conflicts
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)


class CallerBlewUp(Exception):
    """An application error raised by the caller after the service returned."""


class _Scenario:
    """A ready-to-finalize REVIEW version: snapshot matching current input,
    one qualified member assigned to a requirement of count 1.
    """

    def __init__(self, session, *, required_count: int = 1, members: int = 1, assign: bool = True):
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
        if assign:
            for membership in self.memberships[:required_count]:
                assign_member(
                    session, actor=self.head, requirement=self.requirement,
                    membership=membership,
                )
            session.flush()

    @property
    def membership(self):
        return self.memberships[0]

    def finalize(self, *, reason: str | None = None):
        version = finalize_schedule_version(
            self.session, actor=self.head, version=self.version, reason=reason,
        )
        self.session.flush()
        return version


def _stored_version(session, version_id: int):
    return session.execute(
        text("SELECT status, finalized_at FROM schedule_version WHERE id = :id"),
        {"id": version_id},
    ).one()


def _finalization_audits(session, version_id: int):
    return session.execute(
        text(
            "SELECT id, summary, reason, ministry_id, before_values, after_values"
            " FROM audit_event WHERE target_table = 'schedule_version'"
            "   AND target_id = :id AND action = :action ORDER BY id"
        ),
        {"id": version_id, "action": ACTION_SCHEDULE_VERSION_FINALIZED},
    ).all()


# --------------------------------------------------------------------------
# A -- a real ready REVIEW version finalizes and persists
# --------------------------------------------------------------------------


def test_a_a_ready_review_version_is_persisted_as_finalized_with_one_audit(db_session):
    scenario = _Scenario(db_session)

    version = scenario.finalize(reason="Approved at the elders' meeting.")

    stored = _stored_version(db_session, version.id)
    assert stored.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert stored.finalized_at is not None
    assert stored.finalized_at.tzinfo is not None
    assert stored.finalized_at.utcoffset() == datetime.timedelta(0)

    audits = _finalization_audits(db_session, version.id)
    assert len(audits) == 1
    audit = audits[0]
    assert audit.ministry_id == scenario.ministry.id
    assert audit.reason == "Approved at the elders' meeting."
    assert audit.before_values == {"status": "REVIEW", "finalized_at": None}
    assert audit.after_values["status"] == "FINALIZED"
    # The payload timestamp is the very value written to the column.
    assert audit.after_values["finalized_at"] == stored.finalized_at.isoformat()
    # Summary carries the ministry name and the period name independently.
    assert scenario.ministry.name in audit.summary
    assert scenario.period.name in audit.summary
    assert f"version {version.version_number}" in audit.summary


def test_a2_the_model_invariant_holds_in_the_database(db_session):
    """``status_finalized_at_agree`` is a two-way CHECK; PostgreSQL accepting
    the row is the proof that both fields moved together.
    """
    scenario = _Scenario(db_session)
    scenario.finalize()

    agreeing = db_session.execute(
        text(
            "SELECT (status = 'FINALIZED') = (finalized_at IS NOT NULL) AS ok"
            " FROM schedule_version WHERE id = :id"
        ),
        {"id": scenario.version.id},
    ).scalar_one()
    assert agreeing is True


# --------------------------------------------------------------------------
# B -- a not-ready version is refused and left alone
# --------------------------------------------------------------------------


def test_b_an_unfilled_version_is_refused_and_stays_in_review(db_session):
    scenario = _Scenario(db_session, required_count=2, members=2, assign=False)
    assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=scenario.memberships[0],
    )
    db_session.flush()  # one of two required

    with pytest.raises(InvalidOperationError, match="cannot finalize"):
        finalize_schedule_version(
            db_session, actor=scenario.head, version=scenario.version,
        )
    db_session.flush()

    stored = _stored_version(db_session, scenario.version.id)
    assert stored.status == SCHEDULE_VERSION_STATUS_REVIEW
    assert stored.finalized_at is None
    assert _finalization_audits(db_session, scenario.version.id) == []


def test_b2_nothing_is_repaired_or_mutated_by_the_refusal(db_session):
    scenario = _Scenario(db_session, required_count=2, members=2, assign=False)
    assign_member(
        db_session, actor=scenario.head, requirement=scenario.requirement,
        membership=scenario.memberships[0],
    )
    db_session.flush()
    before_assignments = db_session.execute(
        text("SELECT count(*) FROM assignment WHERE schedule_version_id = :v"),
        {"v": scenario.version.id},
    ).scalar_one()

    with pytest.raises(InvalidOperationError):
        finalize_schedule_version(db_session, actor=scenario.head, version=scenario.version)

    after_assignments = db_session.execute(
        text("SELECT count(*) FROM assignment WHERE schedule_version_id = :v"),
        {"v": scenario.version.id},
    ).scalar_one()
    assert after_assignments == before_assignments == 1
    assert db_session.execute(
        text(
            "SELECT count(*) FROM schedule_version_requirement"
            " WHERE schedule_version_id = :v"
        ),
        {"v": scenario.version.id},
    ).scalar_one() == 1


# --------------------------------------------------------------------------
# C -- caller-owned rollback discards the transition and its audit
# --------------------------------------------------------------------------


def test_c_rollback_discards_the_finalization_and_its_audit_row(db_session):
    scenario = _Scenario(db_session)
    version_id = scenario.version.id

    with pytest.raises(CallerBlewUp):
        # The SAVEPOINT stands in for the caller's transaction; rolling it back
        # is a genuine PostgreSQL ROLLBACK of everything written inside.
        with db_session.begin_nested():
            finalize_schedule_version(
                db_session, actor=scenario.head, version=scenario.version,
            )
            db_session.flush()

            # Both really reached PostgreSQL before the failure.
            assert _stored_version(db_session, version_id).status == "FINALIZED"
            assert len(_finalization_audits(db_session, version_id)) == 1

            raise CallerBlewUp("the caller failed after the service returned")

    # A fresh Session on the same connection: neither the transition nor its
    # audit row survived.
    fresh = Session(
        bind=db_session.get_bind(), autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        stored = _stored_version(fresh, version_id)
        audits = _finalization_audits(fresh, version_id)
    finally:
        fresh.close()

    assert stored.status == SCHEDULE_VERSION_STATUS_REVIEW
    assert stored.finalized_at is None
    assert audits == []


# --------------------------------------------------------------------------
# D -- authority actually transfers (ADR 0003), through the real service
# --------------------------------------------------------------------------


def test_d_finalizing_version_2_transfers_authority_away_from_version_1(db_session):
    """Version 1 is FINALIZED and commits a person in the AV ministry.
    Version 2 is a ready REVIEW version that does not assign them.

    Before: Task 21 still blocks the person on that date.
    After finalizing Version 2 through the real Task 27 service: it does not,
    because Version 1 is no longer the highest-numbered FINALIZED version --
    and Version 1's own row was never touched.
    """
    church = f.make_church(db_session)
    setup = f.make_ministry(db_session, church=church, name="Setup")  # the asking ministry
    av = f.make_ministry(db_session, church=church, name="AV")
    # AV's head, because AV's schedule is the one being finalized here.
    head = f.make_ministry_head(db_session, church=church, ministry=av, name="AV Head")
    volunteer = f.make_person(db_session, church=church, name="Volunteer")

    av_role = f.make_role(db_session, ministry=av, name="Sound")
    av_period = f.make_period(db_session, ministry=av)
    av_event = f.make_event(db_session, period=av_period, event_date=NOV_15)
    av_schedule = f.make_schedule(db_session, period=av_period)
    av_membership = f.make_membership(db_session, person=volunteer, ministry=av)

    # Version 1: finalized, with the volunteer committed.
    version_1 = f.make_version(
        db_session, schedule=av_schedule, period=av_period, version_number=1,
        status=SCHEDULE_VERSION_STATUS_FINALIZED,
        finalized_at=datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    requirement_1 = f.make_version_requirement(
        db_session, version=version_1, event=av_event, role=av_role,
    )
    assignment_1 = f.make_assignment(
        db_session, requirement=requirement_1, membership=av_membership,
    )
    version_1_finalized_at = version_1.finalized_at

    # Version 2: a ready REVIEW version of the same schedule, with the
    # volunteer NOT assigned. Its requirement is satisfied by someone else.
    replacement = f.make_person(db_session, church=church, name="Replacement")
    replacement_membership = f.make_membership(db_session, person=replacement, ministry=av)
    f.make_qualification(
        db_session, membership=replacement_membership, role=av_role,
        decided_by=head, is_qualified=True,
    )
    f.make_staffing_requirement(db_session, event=av_event, role=av_role, required_count=1)
    version_2 = f.make_version(
        db_session, schedule=av_schedule, period=av_period, version_number=2,
        status=SCHEDULE_VERSION_STATUS_REVIEW,
    )
    requirement_2 = f.make_version_requirement(
        db_session, version=version_2, event=av_event, role=av_role,
    )
    db_session.flush()
    assign_member(
        db_session, actor=head, requirement=requirement_2, membership=replacement_membership,
    )
    db_session.flush()

    def volunteer_is_blocked() -> bool:
        return get_person_sunday_conflicts(
            db_session, person_id=volunteer.id, conflict_date=NOV_15,
            target_ministry_id=setup.id,
        ).is_blocked

    # Before: Version 1 is authoritative, so the old commitment still stands.
    assert volunteer_is_blocked() is True

    finalize_schedule_version(db_session, actor=head, version=version_2)
    db_session.flush()

    # After: Version 2 is authoritative; Version 1's assignment is history.
    assert volunteer_is_blocked() is False

    # Version 1 was never mutated -- authority moved by query semantics alone.
    stored_v1 = _stored_version(db_session, version_1.id)
    assert stored_v1.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert stored_v1.finalized_at == version_1_finalized_at
    assert db_session.execute(
        text("SELECT count(*) FROM assignment WHERE id = :id"), {"id": assignment_1.id},
    ).scalar_one() == 1
    # And nothing was mirrored into existing_commitment (ADR 0002).
    assert db_session.execute(
        text("SELECT count(*) FROM existing_commitment WHERE person_id = :p"),
        {"p": volunteer.id},
    ).scalar_one() == 0


def test_d2_a_superseded_review_version_cannot_be_finalized(db_session):
    """Version 1 REVIEW with a newer Version 2 present is historical."""
    scenario = _Scenario(db_session)
    f.make_version(
        db_session, schedule=scenario.schedule, period=scenario.period,
        version_number=2, status=SCHEDULE_VERSION_STATUS_REVIEW,
    )
    db_session.flush()

    with pytest.raises(InvalidOperationError, match="superseded"):
        finalize_schedule_version(
            db_session, actor=scenario.head, version=scenario.version,
        )

    assert _stored_version(db_session, scenario.version.id).status == SCHEDULE_VERSION_STATUS_REVIEW


# --------------------------------------------------------------------------
# E -- idempotency over real rows
# --------------------------------------------------------------------------


def test_e_a_second_finalize_call_changes_nothing_and_adds_no_audit(db_session):
    scenario = _Scenario(db_session)
    scenario.finalize(reason="First time.")

    first = _stored_version(db_session, scenario.version.id)
    assert len(_finalization_audits(db_session, scenario.version.id)) == 1

    again = scenario.finalize(reason="Second time.")

    second = _stored_version(db_session, scenario.version.id)
    assert again is scenario.version
    assert second.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert second.finalized_at == first.finalized_at  # the moment is preserved
    assert len(_finalization_audits(db_session, scenario.version.id)) == 1


def test_e2_an_idempotent_call_is_unaffected_by_later_readiness_problems(db_session):
    """A published schedule stays published. Deactivating the assigned person
    would fail Task 26 readiness, but the idempotent path does not re-run it.
    """
    scenario = _Scenario(db_session)
    scenario.finalize()

    scenario.membership.person.deactivated_at = datetime.datetime(2026, 11, 1, tzinfo=UTC)
    db_session.flush()

    again = scenario.finalize()

    assert again.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert len(_finalization_audits(db_session, scenario.version.id)) == 1
