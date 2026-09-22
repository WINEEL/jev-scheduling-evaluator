"""Integration Tests D and E -- the transaction contract, against PostgreSQL.

Tasks 13-24 proved the *discipline* (a service never commits or rolls back)
with recording Sessions that raise if the forbidden method is called. What no
offline test could prove is the consequence that discipline exists for:

- **D** -- when the caller's transaction rolls back, the domain row **and** its
  AuditEvent both disappear, together, because they were only ever pending in
  one real PostgreSQL transaction.
- **E** -- ``session.delete()`` genuinely removes the row. The offline suites
  for Tasks 16-18 and 22 could only record that ``delete()`` was *called*: a
  real ``Session.delete()`` needs a persistent instance, which an unbound
  offline session cannot honestly produce.

Both tests verify through **raw SQL on the same connection**, never through
the ORM identity map -- after a rollback the map is not evidence, and a
``SELECT count(*)`` is.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from app.services.audit import (
    ACTION_EXISTING_COMMITMENT_RECORDED,
    ACTION_EXISTING_COMMITMENT_REMOVED,
)
from app.services.existing_commitment import (
    remove_existing_commitment,
    set_existing_commitment,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

COMMITMENT_DATE = datetime.date(2026, 11, 15)


class CallerBlewUp(Exception):
    """An application error raised by the *caller*, after the service returned
    and before it committed -- the exact situation the convention exists for.
    """


def _commitment_count(session, person_id: int) -> int:
    return session.execute(
        text("SELECT count(*) FROM existing_commitment WHERE person_id = :p"),
        {"p": person_id},
    ).scalar_one()


def _audit_count(session, *, actor_person_id: int, action: str) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM audit_event"
            " WHERE actor_person_id = :a AND action = :action"
        ),
        {"a": actor_person_id, "action": action},
    ).scalar_one()


# --------------------------------------------------------------------------
# Test D -- caller-owned rollback discards the domain row and its audit row
# --------------------------------------------------------------------------


def test_d1_rollback_discards_both_the_commitment_and_its_audit_event(db_session):
    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    person = f.make_person(db_session, church=church, name="Preacher")
    db_session.flush()

    with pytest.raises(CallerBlewUp):
        # The SAVEPOINT stands in for the caller's transaction: the service
        # writes into it exactly as it would into a real one, and rolling it
        # back is a genuine PostgreSQL ROLLBACK of everything written inside.
        # (The outer transaction is rolled back too when the test ends, so
        # this suite never commits anything at all.)
        with db_session.begin_nested():
            set_existing_commitment(
                db_session, actor=admin, person=person,
                commitment_date=COMMITMENT_DATE, reason="Preaching",
            )
            db_session.flush()

            # Both rows really reached PostgreSQL before the failure -- without
            # this, the rollback assertions below would prove nothing.
            assert _commitment_count(db_session, person.id) == 1
            assert _audit_count(
                db_session, actor_person_id=admin.id,
                action=ACTION_EXISTING_COMMITMENT_RECORDED,
            ) == 1

            raise CallerBlewUp("the caller failed after the service returned")

    # Fresh reads on the same connection, after the rollback.
    assert _commitment_count(db_session, person.id) == 0
    assert _audit_count(
        db_session, actor_person_id=admin.id, action=ACTION_EXISTING_COMMITMENT_RECORDED,
    ) == 0


def test_d2_a_fresh_session_also_sees_neither_row_after_the_rollback(db_session):
    """The same proof through a second Session, in case the first one's
    identity map were somehow flattering the result.
    """
    from sqlalchemy.orm import Session

    from app.models.audit import AuditEvent
    from app.models.scheduling_input import ExistingCommitment

    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    person = f.make_person(db_session, church=church, name="Preacher")
    db_session.flush()

    with pytest.raises(CallerBlewUp):
        with db_session.begin_nested():
            set_existing_commitment(
                db_session, actor=admin, person=person,
                commitment_date=COMMITMENT_DATE, reason="Preaching",
            )
            db_session.flush()
            raise CallerBlewUp("boom")

    fresh = Session(
        bind=db_session.get_bind(), autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    try:
        commitments = fresh.query(ExistingCommitment).filter_by(person_id=person.id).all()
        audits = fresh.query(AuditEvent).filter_by(actor_person_id=admin.id).all()
    finally:
        fresh.close()

    assert commitments == []
    assert audits == []


def test_d3_without_the_failure_both_rows_are_present_together(db_session):
    """The control case: the same service call, no exception. Both rows exist,
    so D1's emptiness is caused by the rollback and not by the service quietly
    doing nothing.
    """
    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    person = f.make_person(db_session, church=church, name="Preacher")

    set_existing_commitment(
        db_session, actor=admin, person=person,
        commitment_date=COMMITMENT_DATE, reason="Preaching",
    )
    db_session.flush()

    assert _commitment_count(db_session, person.id) == 1
    assert _audit_count(
        db_session, actor_person_id=admin.id, action=ACTION_EXISTING_COMMITMENT_RECORDED,
    ) == 1


# --------------------------------------------------------------------------
# Test E -- a real Session.delete() really removes the row
# --------------------------------------------------------------------------


def test_e1_remove_existing_commitment_actually_deletes_the_row(db_session):
    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    person = f.make_person(db_session, church=church, name="Preacher")

    commitment = set_existing_commitment(
        db_session, actor=admin, person=person,
        commitment_date=COMMITMENT_DATE, reason="Preaching",
    )
    db_session.flush()
    commitment_id = commitment.id
    assert _commitment_count(db_session, person.id) == 1

    remove_existing_commitment(db_session, actor=admin, commitment=commitment)
    db_session.flush()

    # The DELETE really executed: this is the assertion Tasks 16-18/22 could
    # only approximate by recording that delete() had been called.
    assert _commitment_count(db_session, person.id) == 0
    assert db_session.execute(
        text("SELECT count(*) FROM existing_commitment WHERE id = :id"),
        {"id": commitment_id},
    ).scalar_one() == 0


def test_e2_the_removal_audit_event_survives_the_delete(db_session):
    """The audit row is the only remaining record that the commitment ever
    existed, so it must outlive the row it describes -- and it must still be
    there after the domain row is gone.
    """
    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    person = f.make_person(db_session, church=church, name="Preacher")

    commitment = set_existing_commitment(
        db_session, actor=admin, person=person,
        commitment_date=COMMITMENT_DATE, reason="Preaching",
    )
    db_session.flush()
    commitment_id = commitment.id

    remove_existing_commitment(db_session, actor=admin, commitment=commitment)
    db_session.flush()

    removal = db_session.execute(
        text(
            "SELECT target_id, target_table, before_values, after_values"
            " FROM audit_event"
            " WHERE actor_person_id = :a AND action = :action"
        ),
        {"a": admin.id, "action": ACTION_EXISTING_COMMITMENT_REMOVED},
    ).one()

    assert removal.target_id == commitment_id
    assert removal.target_table == "existing_commitment"
    assert removal.before_values["person_id"] == person.id
    assert removal.after_values is None  # a removal has no resulting state


def test_e3_the_delete_and_its_audit_row_roll_back_together_as_well(db_session):
    """Removal obeys the same contract as creation: nothing is durable until
    the caller commits, so a rollback restores the row *and* discards the
    removal audit event.
    """
    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    person = f.make_person(db_session, church=church, name="Preacher")

    commitment = set_existing_commitment(
        db_session, actor=admin, person=person,
        commitment_date=COMMITMENT_DATE, reason="Preaching",
    )
    db_session.flush()

    with pytest.raises(CallerBlewUp):
        with db_session.begin_nested():
            remove_existing_commitment(db_session, actor=admin, commitment=commitment)
            db_session.flush()
            assert _commitment_count(db_session, person.id) == 0
            raise CallerBlewUp("the caller failed after the removal")

    assert _commitment_count(db_session, person.id) == 1
    assert _audit_count(
        db_session, actor_person_id=admin.id, action=ACTION_EXISTING_COMMITMENT_REMOVED,
    ) == 0
