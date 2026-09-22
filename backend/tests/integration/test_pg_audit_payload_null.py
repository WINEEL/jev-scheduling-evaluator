"""Regression: an absent audit payload is stored as SQL NULL, not JSON null.

The D/E/I groups already prove this end-to-end through real services, so this
module stays deliberately small: it pins the *storage form* directly, which is
the one thing the offline `none_as_null` assertion cannot show.

The defect this guards against was invisible offline for a precise reason --
``record_audit_event`` built a perfectly correct object, and no offline
session ever issued the INSERT that PostgreSQL would refuse.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.models.audit import AuditEvent
from tests.integration import factories as f

pytestmark = pytest.mark.integration


def _payload_storage(session, audit_id: int) -> tuple[bool, bool, str | None, str | None]:
    """``(before_is_sql_null, after_is_sql_null, before_type, after_type)``.

    ``jsonb_typeof`` returns SQL NULL for a SQL NULL input and the string
    ``'null'`` for the JSON scalar -- the exact distinction at issue.
    """
    row = session.execute(
        text(
            "SELECT before_values IS NULL AS before_null,"
            "       after_values IS NULL AS after_null,"
            "       jsonb_typeof(before_values) AS before_type,"
            "       jsonb_typeof(after_values) AS after_type"
            " FROM audit_event WHERE id = :id"
        ),
        {"id": audit_id},
    ).one()
    return row.before_null, row.after_null, row.before_type, row.after_type


def _actor(session):
    church = f.make_church(session)
    return f.make_person(session, church=church, name="Admin", is_admin=True)


def test_creation_audit_stores_the_absent_before_side_as_sql_null(db_session):
    actor = _actor(db_session)

    event = AuditEvent(
        actor_type="PERSON", actor_person_id=actor.id, actor_label=actor.display_name,
        action="EXISTING_COMMITMENT_RECORDED", target_table="existing_commitment",
        target_id=1, summary="created", before_values=None,
        after_values={"person_id": 1},
    )
    db_session.add(event)
    db_session.flush()  # would raise CheckViolation without none_as_null=True

    before_null, after_null, before_type, after_type = _payload_storage(db_session, event.id)
    assert before_null is True
    assert before_type is None  # SQL NULL, not the JSON scalar 'null'
    assert after_null is False
    assert after_type == "object"


def test_removal_audit_stores_the_absent_after_side_as_sql_null(db_session):
    actor = _actor(db_session)

    event = AuditEvent(
        actor_type="PERSON", actor_person_id=actor.id, actor_label=actor.display_name,
        action="EXISTING_COMMITMENT_REMOVED", target_table="existing_commitment",
        target_id=1, summary="removed", before_values={"person_id": 1},
        after_values=None,
    )
    db_session.add(event)
    db_session.flush()

    before_null, after_null, before_type, after_type = _payload_storage(db_session, event.id)
    assert after_null is True
    assert after_type is None
    assert before_null is False
    assert before_type == "object"


def test_a_row_with_both_payloads_still_stores_two_objects(db_session):
    """The unaffected case, so the fix is shown to change only the None path."""
    actor = _actor(db_session)

    event = AuditEvent(
        actor_type="PERSON", actor_person_id=actor.id, actor_label=actor.display_name,
        action="EXISTING_COMMITMENT_CHANGED", target_table="existing_commitment",
        target_id=1, summary="changed", before_values={"reason": "old"},
        after_values={"reason": "new"},
    )
    db_session.add(event)
    db_session.flush()

    _, _, before_type, after_type = _payload_storage(db_session, event.id)
    assert (before_type, after_type) == ("object", "object")


def test_the_object_or_null_checks_are_still_enforced(db_session):
    """The fix must not have loosened anything: a non-object payload is still
    refused, and a row with neither payload is still refused.
    """
    from sqlalchemy.exc import IntegrityError

    actor = _actor(db_session)

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "INSERT INTO audit_event (actor_type, actor_person_id,"
                    " actor_label, action, target_table, target_id, summary,"
                    " after_values) VALUES ('PERSON', :a, 'x', 'X_DONE',"
                    " 'existing_commitment', 1, 's', '[1,2]'::jsonb)"
                ),
                {"a": actor.id},
            )
    assert "after_values_is_object" in str(exc_info.value.orig)

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "INSERT INTO audit_event (actor_type, actor_person_id,"
                    " actor_label, action, target_table, target_id, summary)"
                    " VALUES ('PERSON', :a, 'x', 'X_DONE',"
                    " 'existing_commitment', 1, 's')"
                ),
                {"a": actor.id},
            )
    assert "payload_present" in str(exc_info.value.orig)
