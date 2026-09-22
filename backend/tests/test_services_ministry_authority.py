"""Ministry Head authority service tests.

Offline: no PostgreSQL, no Neon, no network.

**Test strategy, and its one honest limitation.**

These tests use *real* ORM objects and a *real* :class:`sqlalchemy.orm.Session`
that has no bind. That is a deliberate choice over a hand-written fake:

- ``session.add()`` behaves exactly as it does in production, so assertions
  about what the operation registered are assertions about real ORM state.
- Constructing a real :class:`AuditEvent` proves the service passes attribute
  names the model actually has -- a fake dict would not.
- An unbound Session raises ``UnboundExecutionError`` on ``commit()``. So if a
  service ever grew an internal commit, these tests would fail loudly rather
  than quietly passing.

What this **cannot** verify is that PostgreSQL rolls the transaction back, since
no database is involved. What it verifies instead is the property that makes
rollback the caller's to perform and impossible to get wrong by accident: the
services never commit, never flush, never roll back, and put the mutation and
the audit row in the *same* session. See
``test_the_operation_never_commits_flushes_or_rolls_back`` and
``test_mutation_and_audit_row_are_registered_on_the_same_session``.

The remaining gap -- an end-to-end commit/rollback against real PostgreSQL --
needs a deliberate database integration-test strategy, which does not exist yet
and is not invented here. `TestAuditValuesSatisfyTheDatabaseConstraints` narrows
that gap by checking every value the services produce against the actual CHECK
constraints defined on the model, so a value that would be rejected at commit
time is caught here instead.
"""

from __future__ import annotations

import datetime
import re

import pytest
from sqlalchemy.orm import Session

from app.models.audit import (
    AUDIT_ACTION_PATTERN,
    AUDIT_ACTOR_TYPE_PERSON,
    AUDIT_TARGET_TABLES,
    AuditEvent,
)
from app.models.core import Ministry, MinistryMembership, Person
from app.services import (
    AuthorizationError,
    InvalidOperationError,
    grant_ministry_head,
    record_audit_event,
    revoke_ministry_head,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class RecordingSession(Session):
    """A real Session that records whether transaction control was invoked.

    Subclassing rather than faking keeps ``add()`` and the identity map real.
    The session is never bound to an engine, so nothing reaches a database.
    """

    def __init__(self) -> None:
        super().__init__()
        self.commit_calls = 0
        self.flush_calls = 0
        self.rollback_calls = 0

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def flush(self, objects=None) -> None:  # pragma: no cover - must never run
        self.flush_calls += 1
        raise AssertionError("a service must never flush")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


def _person(person_id: int, name: str, *, is_admin: bool = False,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    if deactivated:
        person.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return person


def _membership(
    membership_id: int,
    *,
    person: Person,
    ministry: Ministry,
    is_head: bool = False,
    deactivated: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id,
        ministry_id=ministry.id,
        is_ministry_head=is_head,
    )
    membership.id = membership_id
    membership.person = person
    membership.ministry = ministry
    if deactivated:
        membership.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return membership


@pytest.fixture
def admin() -> Person:
    return _person(1, "Demo Admin", is_admin=True)


@pytest.fixture
def setup_ministry() -> Ministry:
    ministry = Ministry(name="Setup", church_id=1)
    ministry.id = 3
    return ministry


@pytest.fixture
def member(setup_ministry: Ministry) -> MinistryMembership:
    return _membership(118, person=_person(42, "Sheldon"), ministry=setup_ministry)


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


# --------------------------------------------------------------------------
# 1 & 2 — Admin grants, and the audit row is correct
# --------------------------------------------------------------------------


def test_admin_can_grant_ministry_head(session, admin, member):
    returned = grant_ministry_head(session, actor=admin, membership=member)

    assert member.is_ministry_head is True
    assert returned is member


def test_grant_records_a_correct_audit_row(session, admin, member):
    grant_ministry_head(session, actor=admin, membership=member)
    audit = _one_audit_row(session)

    assert audit.actor_type == AUDIT_ACTOR_TYPE_PERSON
    assert audit.actor_person_id == 1
    assert audit.actor_label == "Demo Admin"
    assert audit.action == "MINISTRY_HEAD_GRANTED"
    assert audit.target_table == "ministry_membership"
    assert audit.target_id == 118
    assert audit.ministry_id == 3
    assert audit.before_values == {"is_ministry_head": False}
    assert audit.after_values == {"is_ministry_head": True}
    assert audit.reason is None


def test_grant_summary_names_the_person_and_the_ministry(session, admin, member):
    """The row carries no target_label, so the summary must name the target."""
    grant_ministry_head(session, actor=admin, membership=member)
    audit = _one_audit_row(session)

    assert audit.summary == (
        "Granted Ministry Head authority to Sheldon for Setup"
    )


def test_grant_records_an_optional_reason_when_supplied(session, admin, member):
    grant_ministry_head(
        session, actor=admin, membership=member, reason="Stepping up as co-lead."
    )

    assert _one_audit_row(session).reason == "Stepping up as co-lead."


def test_grant_payload_contains_only_the_changed_field(session, admin, member):
    """Never a whole membership row (audit §7.2)."""
    grant_ministry_head(session, actor=admin, membership=member)
    audit = _one_audit_row(session)

    assert set(audit.before_values) == {"is_ministry_head"}
    assert set(audit.after_values) == {"is_ministry_head"}
    for forbidden in ("notes", "joined_on", "updated_at", "person_id"):
        assert forbidden not in audit.after_values


# --------------------------------------------------------------------------
# 3 — Revoke
# --------------------------------------------------------------------------


def test_admin_can_revoke_ministry_head(session, admin, setup_ministry):
    head = _membership(
        118, person=_person(42, "Sheldon"), ministry=setup_ministry, is_head=True
    )

    revoke_ministry_head(session, actor=admin, membership=head)

    assert head.is_ministry_head is False


def test_revoke_records_a_correct_audit_row(session, admin, setup_ministry):
    head = _membership(
        118, person=_person(42, "Sheldon"), ministry=setup_ministry, is_head=True
    )

    revoke_ministry_head(
        session, actor=admin, membership=head, reason="Stepping back this quarter."
    )
    audit = _one_audit_row(session)

    assert audit.action == "MINISTRY_HEAD_REVOKED"
    assert audit.actor_type == AUDIT_ACTOR_TYPE_PERSON
    assert audit.actor_person_id == 1
    assert audit.target_table == "ministry_membership"
    assert audit.target_id == 118
    assert audit.ministry_id == 3
    assert audit.summary == (
        "Revoked Ministry Head authority from Sheldon for Setup"
    )
    assert audit.reason == "Stepping back this quarter."
    # Reversed relative to grant.
    assert audit.before_values == {"is_ministry_head": True}
    assert audit.after_values == {"is_ministry_head": False}


# --------------------------------------------------------------------------
# 4, 5, 6 — Authorization
# --------------------------------------------------------------------------


def test_non_admin_cannot_grant(session, member):
    ordinary = _person(7, "Ada")

    with pytest.raises(AuthorizationError):
        grant_ministry_head(session, actor=ordinary, membership=member)

    assert member.is_ministry_head is False
    assert _audit_rows(session) == []


def test_non_admin_cannot_revoke(session, setup_ministry):
    ordinary = _person(7, "Ada")
    head = _membership(
        118, person=_person(42, "Sheldon"), ministry=setup_ministry, is_head=True
    )

    with pytest.raises(AuthorizationError):
        revoke_ministry_head(session, actor=ordinary, membership=head)

    assert head.is_ministry_head is True
    assert _audit_rows(session) == []


def test_a_ministry_head_cannot_promote_another_head(session, setup_ministry):
    """[APPROVED] core §4.3 — head authority propagates only from an Admin.

    The actor here heads the very ministry being changed, which is precisely the
    case that must still be refused: managing a ministry confers no say in who
    leads it.
    """
    head_actor_person = _person(9, "Existing Head")
    head_actor_membership = _membership(
        200, person=head_actor_person, ministry=setup_ministry, is_head=True
    )
    assert head_actor_membership.is_ministry_head is True
    assert head_actor_person.is_admin is False

    target = _membership(
        118, person=_person(42, "Sheldon"), ministry=setup_ministry
    )

    with pytest.raises(AuthorizationError):
        grant_ministry_head(
            session, actor=head_actor_person, membership=target
        )

    assert target.is_ministry_head is False
    assert _audit_rows(session) == []


def test_a_ministry_head_cannot_revoke_another_head(session, setup_ministry):
    head_actor_person = _person(9, "Existing Head")
    _membership(200, person=head_actor_person, ministry=setup_ministry, is_head=True)
    target = _membership(
        118, person=_person(42, "Sheldon"), ministry=setup_ministry, is_head=True
    )

    with pytest.raises(AuthorizationError):
        revoke_ministry_head(session, actor=head_actor_person, membership=target)

    assert target.is_ministry_head is True
    assert _audit_rows(session) == []


def test_a_deactivated_admin_may_not_act(session, member):
    """is_admin is not cleared on deactivation, so activity is checked too."""
    departed = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        grant_ministry_head(session, actor=departed, membership=member)

    assert member.is_ministry_head is False
    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 7 — Deactivated membership
# --------------------------------------------------------------------------


def test_cannot_grant_to_a_deactivated_membership(session, admin, setup_ministry):
    """core §5.1 — an inactive membership never carries head authority.

    The database CHECK would also refuse this, but only at commit, by which
    point the caller's whole transaction has aborted.
    """
    inactive = _membership(
        118, person=_person(42, "Sheldon"), ministry=setup_ministry, deactivated=True
    )

    with pytest.raises(InvalidOperationError):
        grant_ministry_head(session, actor=admin, membership=inactive)

    assert inactive.is_ministry_head is False
    assert _audit_rows(session) == []


def test_cannot_grant_to_a_globally_deactivated_person(
    session, admin, setup_ministry
):
    """A person who has left the church gets no authority, active membership or not.

    The membership here is deliberately **active**: this is the case the
    membership check alone does not catch. `person.deactivated_at` and
    `membership.deactivated_at` are separate facts (core §6) -- left the church
    versus left one ministry -- and neither implies the other.

    No database CHECK can express this, since it is a condition on the parent
    row, so the service is the only place it can be enforced.
    """
    departed = _person(42, "Sheldon", deactivated=True)
    membership = _membership(118, person=departed, ministry=setup_ministry)
    assert membership.deactivated_at is None, "membership itself must be active"

    with pytest.raises(InvalidOperationError):
        grant_ministry_head(session, actor=admin, membership=membership)

    assert membership.is_ministry_head is False
    assert _audit_rows(session) == []


def test_grant_to_a_deactivated_person_is_not_idempotent_success(
    session, admin, setup_ministry
):
    """Inconsistent state is reported, not silently confirmed.

    If a deactivated person somehow already holds the flag, the eligibility
    check must run *before* the idempotency return -- otherwise the operation
    would answer "already done" and hide the very state that should be raising
    an alarm.
    """
    departed = _person(42, "Sheldon", deactivated=True)
    membership = _membership(
        118, person=departed, ministry=setup_ministry, is_head=True
    )

    with pytest.raises(InvalidOperationError):
        grant_ministry_head(session, actor=admin, membership=membership)

    # Untouched, and no audit row claiming a promotion that did not happen.
    assert membership.is_ministry_head is True
    assert _audit_rows(session) == []


def test_revoke_can_repair_authority_held_by_a_deactivated_person(
    session, admin, setup_ministry
):
    """Revocation is deliberately permissive, unlike grant.

    Revoking is also the state-repair operation: if authority somehow exists for
    a person who has left, an Admin must still be able to take it away. Applying
    grant's eligibility rules here would block the only tool for fixing the
    problem behind the problem itself.
    """
    departed = _person(42, "Sheldon", deactivated=True)
    membership = _membership(
        118, person=departed, ministry=setup_ministry, is_head=True
    )

    revoke_ministry_head(session, actor=admin, membership=membership)

    assert membership.is_ministry_head is False
    audit = _one_audit_row(session)
    assert audit.action == "MINISTRY_HEAD_REVOKED"
    assert audit.before_values == {"is_ministry_head": True}
    assert audit.after_values == {"is_ministry_head": False}


def test_revoke_is_also_permissive_about_a_deactivated_membership(
    session, admin, setup_ministry
):
    """Same repair argument for the other deactivation flag."""
    membership = _membership(
        118,
        person=_person(42, "Sheldon"),
        ministry=setup_ministry,
        is_head=True,
        deactivated=True,
    )

    revoke_ministry_head(session, actor=admin, membership=membership)

    assert membership.is_ministry_head is False
    assert _one_audit_row(session).action == "MINISTRY_HEAD_REVOKED"


# --------------------------------------------------------------------------
# 8 & 9 — Idempotency
# --------------------------------------------------------------------------


def test_granting_an_existing_head_is_a_no_op_with_no_audit_row(
    session, admin, setup_ministry
):
    """No domain state changed, so there is no act to record."""
    head = _membership(
        118, person=_person(42, "Sheldon"), ministry=setup_ministry, is_head=True
    )

    returned = grant_ministry_head(session, actor=admin, membership=head)

    assert returned is head
    assert head.is_ministry_head is True
    assert _audit_rows(session) == []


def test_revoking_a_non_head_is_a_no_op_with_no_audit_row(session, admin, member):
    assert member.is_ministry_head is False

    returned = revoke_ministry_head(session, actor=admin, membership=member)

    assert returned is member
    assert member.is_ministry_head is False
    assert _audit_rows(session) == []


def test_repeated_grants_record_exactly_one_audit_row(session, admin, member):
    grant_ministry_head(session, actor=admin, membership=member)
    grant_ministry_head(session, actor=admin, membership=member)
    grant_ministry_head(session, actor=admin, membership=member)

    assert len(_audit_rows(session)) == 1


def test_a_grant_revoke_grant_cycle_records_three_rows(session, admin, member):
    """Idempotency must not suppress genuine repeat transitions."""
    grant_ministry_head(session, actor=admin, membership=member)
    revoke_ministry_head(session, actor=admin, membership=member)
    grant_ministry_head(session, actor=admin, membership=member)

    actions = [a.action for a in _audit_rows(session)]
    assert actions == [
        "MINISTRY_HEAD_GRANTED",
        "MINISTRY_HEAD_REVOKED",
        "MINISTRY_HEAD_GRANTED",
    ]


# --------------------------------------------------------------------------
# 10 — Reason validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_whitespace_only_reason_is_rejected_on_grant(session, admin, member, blank):
    with pytest.raises(InvalidOperationError):
        grant_ministry_head(session, actor=admin, membership=member, reason=blank)

    assert member.is_ministry_head is False
    assert _audit_rows(session) == []


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_whitespace_only_reason_is_rejected_on_revoke(
    session, admin, setup_ministry, blank
):
    head = _membership(
        118, person=_person(42, "Sheldon"), ministry=setup_ministry, is_head=True
    )

    with pytest.raises(InvalidOperationError):
        revoke_ministry_head(session, actor=admin, membership=head, reason=blank)

    assert head.is_ministry_head is True
    assert _audit_rows(session) == []


def test_a_reason_is_optional_not_required(session, admin, member):
    """audit §9 — no new universal reason requirement is invented for these."""
    grant_ministry_head(session, actor=admin, membership=member)

    assert _one_audit_row(session).reason is None


# --------------------------------------------------------------------------
# 11 & 12 — Transaction contract
# --------------------------------------------------------------------------


def test_the_operation_never_commits_flushes_or_rolls_back(session, admin, member):
    """The caller owns the transaction boundary.

    RecordingSession raises on commit/flush/rollback, so reaching this assertion
    at all already proves none was called; the counters make the intent explicit.
    """
    grant_ministry_head(session, actor=admin, membership=member)
    revoke_ministry_head(session, actor=admin, membership=member)

    assert session.commit_calls == 0
    assert session.flush_calls == 0
    assert session.rollback_calls == 0


def test_the_audit_helper_never_commits(session, admin):
    record_audit_event(
        session,
        actor=admin,
        action="MINISTRY_HEAD_GRANTED",
        target_table="ministry_membership",
        target_id=118,
        summary="Granted Ministry Head authority to Sheldon for Setup",
        ministry_id=3,
        after_values={"is_ministry_head": True},
    )

    assert session.commit_calls == 0
    assert session.flush_calls == 0


def test_mutation_and_audit_row_are_registered_on_the_same_session(
    session, admin, member
):
    """The atomicity contract, stated as the property that delivers it.

    Both the flag change and the audit row are pending in one Session, so one
    commit writes both and one rollback discards both. There is no code path in
    which the mutation could be committed while the audit row is not, because no
    service ever commits.
    """
    grant_ministry_head(session, actor=admin, membership=member)

    audit = _one_audit_row(session)
    assert audit in session.new
    assert member.is_ministry_head is True
    # Same Session instance -- not two, and not a nested one.
    assert Session.object_session(audit) is session


def test_a_failure_inside_audit_recording_propagates_and_commits_nothing(
    session, admin, setup_ministry, monkeypatch
):
    """If the audit write fails, the caller must see the exception.

    A service that swallowed it would produce exactly the state the design
    forbids: an authority change with no record that it happened. The caller's
    rollback -- not the service -- is what undoes the in-memory mutation, which
    is why ``session.begin()`` as a context manager is the documented form.
    """
    import app.services.ministry_authority as ministry_authority

    def exploding_record(*args, **kwargs):
        raise RuntimeError("audit backend unavailable")

    monkeypatch.setattr(ministry_authority, "record_audit_event", exploding_record)

    target = _membership(118, person=_person(42, "Sheldon"), ministry=setup_ministry)

    with pytest.raises(RuntimeError, match="audit backend unavailable"):
        grant_ministry_head(session, actor=admin, membership=target)

    # No audit row, and -- critically -- nothing was committed, so the caller's
    # rollback still governs the outcome.
    assert _audit_rows(session) == []
    assert session.commit_calls == 0
    assert session.rollback_calls == 0


def test_invalid_audit_input_raises_before_anything_is_added(session, admin):
    """Validation happens before session.add, so a bad call leaves no residue."""
    with pytest.raises(InvalidOperationError):
        record_audit_event(
            session,
            actor=admin,
            action="not a valid action",
            target_table="ministry_membership",
            target_id=118,
            summary="x",
            after_values={"a": 1},
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# The audit helper's own contract
# --------------------------------------------------------------------------


def test_audit_helper_rejects_an_unaudited_target_table(session, admin):
    with pytest.raises(InvalidOperationError):
        record_audit_event(
            session,
            actor=admin,
            action="SOMETHING_HAPPENED",
            target_table="user_account",
            target_id=1,
            summary="x",
            after_values={"a": 1},
        )


def test_audit_helper_requires_at_least_one_payload(session, admin):
    with pytest.raises(InvalidOperationError):
        record_audit_event(
            session,
            actor=admin,
            action="SOMETHING_HAPPENED",
            target_table="person",
            target_id=1,
            summary="x",
        )


def test_audit_helper_rejects_a_non_object_payload(session, admin):
    with pytest.raises(InvalidOperationError):
        record_audit_event(
            session,
            actor=admin,
            action="SOMETHING_HAPPENED",
            target_table="person",
            target_id=1,
            summary="x",
            after_values=[1, 2, 3],
        )


def test_audit_helper_requires_exactly_one_actor_form(session, admin):
    common = dict(
        action="SOMETHING_HAPPENED",
        target_table="person",
        target_id=1,
        summary="x",
        after_values={"a": 1},
    )

    with pytest.raises(InvalidOperationError):
        record_audit_event(session, **common)  # neither

    with pytest.raises(InvalidOperationError):
        record_audit_event(
            session, actor=admin, system_actor_label="Solver", **common
        )  # both


def test_audit_helper_supports_a_system_actor(session):
    """The schema supports SYSTEM; no service-account infrastructure exists."""
    audit = record_audit_event(
        session,
        system_actor_label="Scheduling engine",
        action="SCHEDULE_VERSION_CREATED",
        target_table="schedule_version",
        target_id=77,
        summary="Generated Version 2 of the Setup Q4 2026 schedule",
        after_values={"version_number": 2},
    )

    assert audit.actor_type == "SYSTEM"
    assert audit.actor_person_id is None
    assert audit.actor_label == "Scheduling engine"


def test_audit_helper_copies_the_payload_mapping(session, admin):
    """A caller mutating its own dict must not change a pending row."""
    payload = {"is_ministry_head": True}

    audit = record_audit_event(
        session,
        actor=admin,
        action="MINISTRY_HEAD_GRANTED",
        target_table="ministry_membership",
        target_id=118,
        summary="x",
        after_values=payload,
    )
    payload["is_ministry_head"] = False

    assert audit.after_values == {"is_ministry_head": True}


def test_audit_helper_rejects_an_unpersisted_target(session, admin):
    """target_id is None when the caller passes an object that was never saved."""
    with pytest.raises(InvalidOperationError):
        record_audit_event(
            session,
            actor=admin,
            action="SOMETHING_HAPPENED",
            target_table="ministry_membership",
            target_id=None,
            summary="x",
            after_values={"a": 1},
        )

    assert _audit_rows(session) == []


def test_audit_helper_rejects_an_unpersisted_actor(session):
    """A pending Person has no id, so the foreign key could not be written."""
    unsaved = Person(display_name="New Admin", is_admin=True, church_id=1)

    with pytest.raises(InvalidOperationError):
        record_audit_event(
            session,
            actor=unsaved,
            action="SOMETHING_HAPPENED",
            target_table="person",
            target_id=1,
            summary="x",
            after_values={"a": 1},
        )


# --------------------------------------------------------------------------
# Bridging to the database constraints that are not exercised offline
# --------------------------------------------------------------------------


class TestAuditValuesSatisfyTheDatabaseConstraints:
    """Every value the services produce must pass the model's CHECK constraints.

    No database is involved, so these re-apply the constraints declared in
    :mod:`app.models.audit` to the values the services actually generate. This
    is what stops a service producing a row that would only be rejected at
    commit time, in an environment these tests cannot reach.
    """

    def _rows(self, session, admin, setup_ministry) -> list[AuditEvent]:
        member = _membership(
            118, person=_person(42, "Sheldon"), ministry=setup_ministry
        )
        grant_ministry_head(
            session, actor=admin, membership=member, reason="Co-lead for Q4."
        )
        revoke_ministry_head(session, actor=admin, membership=member)
        return _audit_rows(session)

    def test_actions_match_the_database_shape_constraint(
        self, session, admin, setup_ministry
    ):
        for audit in self._rows(session, admin, setup_ministry):
            assert re.match(AUDIT_ACTION_PATTERN, audit.action), audit.action

    def test_target_tables_are_in_the_audited_set(
        self, session, admin, setup_ministry
    ):
        for audit in self._rows(session, admin, setup_ministry):
            assert audit.target_table in AUDIT_TARGET_TABLES

    def test_required_text_is_never_blank(self, session, admin, setup_ministry):
        for audit in self._rows(session, admin, setup_ministry):
            assert audit.summary.strip()
            assert audit.actor_label.strip()
            if audit.reason is not None:
                assert audit.reason.strip()

    def test_target_id_is_positive(self, session, admin, setup_ministry):
        for audit in self._rows(session, admin, setup_ministry):
            assert audit.target_id > 0

    def test_the_two_way_actor_invariant_holds(self, session, admin, setup_ministry):
        for audit in self._rows(session, admin, setup_ministry):
            assert (audit.actor_type == "PERSON") == (
                audit.actor_person_id is not None
            )

    def test_payloads_are_json_objects_and_at_least_one_is_present(
        self, session, admin, setup_ministry
    ):
        for audit in self._rows(session, admin, setup_ministry):
            assert audit.before_values is not None or audit.after_values is not None
            for payload in (audit.before_values, audit.after_values):
                if payload is not None:
                    assert isinstance(payload, dict)
