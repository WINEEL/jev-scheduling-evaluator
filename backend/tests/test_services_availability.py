"""Availability service tests.

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Tasks 14/15/16's precedent, for the same reasons:
this service needs both a ``session.flush()`` (to obtain a new row's identity
before the audit row referencing it) and a database lookup
(``session.execute(select(...))``, to find an existing answer for a
membership/event pair) and, for the clearing path, ``session.delete()`` on an
object this offline fixture cannot make genuinely "persistent" without a bound
engine.

- **The lookup's SQL** is tested directly and mechanically, with no session or
  database at all: ``_availability_lookup_statement`` returns a plain
  SQLAlchemy ``Select`` compiled with literal binds and inspected.
- **The lookup's use** (found vs. not-found) is exercised via ``monkeypatch``
  in every orchestration test.
- **The flush** and **the delete** use a real, unbound ``Session`` subclass
  whose ``flush()`` simulates identity assignment and whose ``delete()``
  tracks the request explicitly, exactly as Task 16 documented -- neither
  touches a database. ``commit()`` and ``rollback()`` remain forbidden.

What this cannot verify, same honest limitation as the earlier tasks: that
PostgreSQL actually assigns identities and rolls back a real transaction. What
it verifies instead is the same shape of property those tasks settled for --
the service never commits or rolls back, flushes only where the design says it
must, and every mutation and its audit row share one session.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_BACKUP,
    AVAILABILITY_UNAVAILABLE,
    EVENT_KIND_SUNDAY_SERVICE,
    Availability,
    Event,
    SchedulingPeriod,
)
from app.services import AuthorizationError, InvalidOperationError
from app.services.availability import (
    ACTION_AVAILABILITY_CHANGED,
    ACTION_AVAILABILITY_CLEARED,
    ACTION_AVAILABILITY_RECORDED,
    EventAvailability,
    MembershipAvailability,
    _availability_lookup_statement,
    _event_availability_statement,
    list_event_availability,
    set_availability,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class AvailabilitySession(Session):
    """A real, unbound Session for these tests.

    Like Tasks 14/15/16's Session subclasses, ``flush()`` is not forbidden --
    this service legitimately flushes once, when it records a new answer, to
    obtain the identity the audit row needs. ``delete()`` is overridden the
    same way Task 16 documented: the genuine ``Session.delete()`` requires a
    "persistent" object (loaded via query, or added and flushed against a real
    engine), which these hand-built fixtures cannot honestly produce without a
    bound database, so the request is tracked instead of executed. ``commit()``
    and ``rollback()`` remain forbidden -- the transaction still belongs to the
    caller.
    """

    def __init__(self, *, next_id: int = 900) -> None:
        super().__init__()
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self.deleted_objects: list = []
        self._next_id = next_id

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def flush(self, objects=None) -> None:
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1

    def delete(self, instance) -> None:
        self.deleted_objects.append(instance)


@pytest.fixture
def session() -> AvailabilitySession:
    return AvailabilitySession()


def _person(person_id: int, name: str, *, is_admin: bool = False,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return person


def _ministry(ministry_id: int, name: str) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


def _actor_membership(
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False,
) -> MinistryMembership:
    """A membership used purely for authorization -- the *actor's own*
    membership, distinct from the *target* membership fixtures below."""
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _target_membership(
    membership_id: int,
    *,
    person: Person,
    ministry: Ministry,
    deactivated: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(person_id=person.id, ministry_id=ministry.id)
    membership.id = membership_id
    membership.person = person
    membership.ministry = ministry
    if deactivated:
        membership.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return membership


def _period(period_id: int, *, ministry: Ministry, locked: bool = False) -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name="Setup Q4 2026",
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    if locked:
        period.availability_locked_at = datetime.datetime(
            2026, 9, 1, tzinfo=datetime.timezone.utc
        )
    return period


def _event(
    event_id: int,
    *,
    ministry: Ministry,
    period: SchedulingPeriod | None = None,
    event_date: datetime.date = datetime.date(2026, 10, 4),
    cancelled: bool = False,
) -> Event:
    if period is None:
        period = _period(1, ministry=ministry)
    event = Event(
        scheduling_period_id=period.id,
        ministry_id=ministry.id,
        event_date=event_date,
        event_kind=EVENT_KIND_SUNDAY_SERVICE,
    )
    event.id = event_id
    event.scheduling_period = period
    if cancelled:
        event.cancelled_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return event


def _existing_availability(
    availability_id: int, *, membership: MinistryMembership, event: Event, state: str,
) -> Availability:
    availability = Availability(
        ministry_membership_id=membership.id, event_id=event.id,
        ministry_id=event.ministry_id, availability_state=state,
    )
    availability.id = availability_id
    return availability


@pytest.fixture
def head(setup_ministry: Ministry) -> Person:
    """The actor every operational write in this module is performed by.

    **An active Ministry Head of the ministry being written to** -- which,
    since Task 80, is the only kind of person who may perform any of these
    operations. This fixture used to be an Admin who headed nothing; that
    person now gets 403 from every write here, and the tests that assert so
    say Admin in their own names.
    """
    person = _person(1, "Demo Admin")
    _actor_membership(9001, person=person, ministry=setup_ministry, is_head=True)
    return person


@pytest.fixture
def setup_ministry() -> Ministry:
    return _ministry(3, "Setup")


@pytest.fixture
def john_membership(setup_ministry: Ministry) -> MinistryMembership:
    return _target_membership(118, person=_person(42, "John"), ministry=setup_ministry)


@pytest.fixture
def sunday_event(setup_ministry: Ministry) -> Event:
    return _event(700, ministry=setup_ministry)


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _stub_lookup(monkeypatch, existing: Availability | None) -> None:
    import app.services.availability as module

    monkeypatch.setattr(
        module, "_find_existing_availability",
        lambda session, *, ministry_membership_id, event_id: existing,
    )


# --------------------------------------------------------------------------
# 1, 2, 3, 4, 5 -- Authorization
# --------------------------------------------------------------------------


def test_admin_can_record_available(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )

    assert result.availability_state == AVAILABILITY_AVAILABLE


def test_admin_can_record_unavailable(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_UNAVAILABLE,
    )

    assert result.availability_state == AVAILABILITY_UNAVAILABLE


def test_admin_can_record_backup(session, head, john_membership, sunday_event, monkeypatch):
    """Task 52: BACKUP is a third accepted, explicit answer -- not merely
    tolerated as a stray string, but a real recordable state like the other
    two.
    """
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_BACKUP,
    )

    assert result.availability_state == AVAILABILITY_BACKUP


def test_own_ministry_head_can_record_availability(
    session, setup_ministry, john_membership, sunday_event, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    head = _person(9, "Head Person")
    _actor_membership(200, person=head, ministry=setup_ministry, is_head=True)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )

    assert result.availability_state == AVAILABILITY_AVAILABLE


def test_other_ministry_head_is_rejected(session, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)
    av_ministry = _ministry(4, "AV")
    av_head = _person(9, "AV Head")
    _actor_membership(201, person=av_head, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        set_availability(
            session, actor=av_head, membership=john_membership, event=sunday_event,
            availability_state=AVAILABILITY_AVAILABLE,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_normal_user_is_rejected(session, setup_ministry, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)
    ordinary = _person(7, "Ada")
    _actor_membership(202, person=ordinary, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        set_availability(
            session, actor=ordinary, membership=john_membership, event=sunday_event,
            availability_state=AVAILABILITY_AVAILABLE,
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 6 -- Ministry mismatch
# --------------------------------------------------------------------------


def test_membership_event_ministry_mismatch_is_rejected(session, head, john_membership, monkeypatch):
    _stub_lookup(monkeypatch, None)
    av_ministry = _ministry(4, "AV")
    av_event = _event(701, ministry=av_ministry)
    # Authorization is scoped to the *event's* ministry, so the AV head is the
    # actor who reaches the mismatch rule: authorized for the event, and still
    # refused because the membership belongs to somebody else's ministry.
    av_head = _person(3, "AV Head")
    _actor_membership(9002, person=av_head, ministry=av_ministry, is_head=True)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=av_head, membership=john_membership, event=av_event,
            availability_state=AVAILABILITY_AVAILABLE,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 7 -- Invalid state string
# --------------------------------------------------------------------------


def test_invalid_availability_string_is_rejected(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=john_membership, event=sunday_event,
            availability_state="MAYBE",
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_no_unknown_or_no_response_string_is_ever_accepted(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    for forbidden in ("UNKNOWN", "NO_RESPONSE", "TENTATIVE", "available", "unavailable"):
        with pytest.raises(InvalidOperationError):
            set_availability(
                session, actor=head, membership=john_membership, event=sunday_event,
                availability_state=forbidden,
            )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 8, 9, 10 -- No row: create or true no-op
# --------------------------------------------------------------------------


def test_no_row_plus_available_creates_explicit_row(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )

    assert isinstance(result, Availability)
    assert result.availability_state == AVAILABILITY_AVAILABLE
    assert result.ministry_membership_id == 118
    assert result.event_id == 700
    assert result.ministry_id == 3


def test_no_row_plus_unavailable_creates_explicit_row(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_UNAVAILABLE,
    )

    assert result.availability_state == AVAILABILITY_UNAVAILABLE


def test_no_row_plus_none_is_idempotent_no_op(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=None,
    )

    assert result is None
    assert _audit_rows(session) == []
    assert session.flush_calls == 0
    assert session.deleted_objects == []


# --------------------------------------------------------------------------
# 11, 12 -- Idempotent same-explicit-state
# --------------------------------------------------------------------------


def test_available_plus_available_is_idempotent(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )

    assert result is existing
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_unavailable_plus_unavailable_is_idempotent(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_UNAVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_UNAVAILABLE,
    )

    assert result is existing
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 13, 14 -- Explicit-state transitions
# --------------------------------------------------------------------------


def test_available_to_unavailable_updates(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_UNAVAILABLE,
    )

    assert result is existing
    assert result.availability_state == AVAILABILITY_UNAVAILABLE


def test_unavailable_to_available_updates(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_UNAVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )

    assert result is existing
    assert result.availability_state == AVAILABILITY_AVAILABLE


# --------------------------------------------------------------------------
# 15 -- Removal
# --------------------------------------------------------------------------


def test_existing_row_plus_none_removes_it(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=None,
    )

    assert result is None
    assert existing in session.deleted_objects


# --------------------------------------------------------------------------
# 16, 17, 18 -- Target activity for create/change
# --------------------------------------------------------------------------


def test_create_requires_active_membership(session, head, setup_ministry, sunday_event, monkeypatch):
    inactive_membership = _target_membership(
        118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True
    )
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=inactive_membership, event=sunday_event,
            availability_state=AVAILABILITY_AVAILABLE,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_create_requires_active_target_person(session, head, setup_ministry, sunday_event, monkeypatch):
    departed = _person(42, "John", deactivated=True)
    membership = _target_membership(118, person=departed, ministry=setup_ministry)
    assert membership.deactivated_at is None  # membership itself is active
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=membership, event=sunday_event,
            availability_state=AVAILABILITY_AVAILABLE,
        )

    assert _audit_rows(session) == []


def test_create_or_change_rejected_for_cancelled_event(session, head, setup_ministry, john_membership, monkeypatch):
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=john_membership, event=cancelled_event,
            availability_state=AVAILABILITY_AVAILABLE,
        )

    assert _audit_rows(session) == []


def test_change_also_requires_active_target(session, head, setup_ministry, monkeypatch):
    """Not just brand-new rows: changing an existing state on a cancelled
    event must also be rejected."""
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)
    membership = _target_membership(118, person=_person(42, "John"), ministry=setup_ministry)
    existing = _existing_availability(500, membership=membership, event=cancelled_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=membership, event=cancelled_event,
            availability_state=AVAILABILITY_UNAVAILABLE,
        )

    assert existing.availability_state == AVAILABILITY_AVAILABLE  # untouched
    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 19, 20, 21 -- Removal is exempt from activity checks
# --------------------------------------------------------------------------


def test_removal_allowed_for_deactivated_membership(session, head, setup_ministry, sunday_event, monkeypatch):
    inactive_membership = _target_membership(
        118, person=_person(42, "John"), ministry=setup_ministry, deactivated=True
    )
    existing = _existing_availability(500, membership=inactive_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=inactive_membership, event=sunday_event,
        availability_state=None,
    )

    assert result is None
    assert existing in session.deleted_objects


def test_removal_allowed_for_deactivated_person(session, head, setup_ministry, sunday_event, monkeypatch):
    departed = _person(42, "John", deactivated=True)
    membership = _target_membership(118, person=departed, ministry=setup_ministry)
    existing = _existing_availability(500, membership=membership, event=sunday_event, state=AVAILABILITY_UNAVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=membership, event=sunday_event,
        availability_state=None,
    )

    assert result is None
    assert existing in session.deleted_objects


def test_removal_allowed_for_cancelled_event(session, head, setup_ministry, john_membership, monkeypatch):
    cancelled_event = _event(700, ministry=setup_ministry, cancelled=True)
    existing = _existing_availability(500, membership=john_membership, event=cancelled_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=john_membership, event=cancelled_event,
        availability_state=None,
    )

    assert result is None
    assert existing in session.deleted_objects


# --------------------------------------------------------------------------
# 22, 23, 24, 25 -- SchedulingPeriod lock
# --------------------------------------------------------------------------


def test_locked_period_rejects_create(session, head, setup_ministry, john_membership, monkeypatch):
    locked_period = _period(1, ministry=setup_ministry, locked=True)
    locked_event = _event(700, ministry=setup_ministry, period=locked_period)
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=john_membership, event=locked_event,
            availability_state=AVAILABILITY_AVAILABLE,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_locked_period_rejects_update(session, head, setup_ministry, john_membership, monkeypatch):
    locked_period = _period(1, ministry=setup_ministry, locked=True)
    locked_event = _event(700, ministry=setup_ministry, period=locked_period)
    existing = _existing_availability(500, membership=john_membership, event=locked_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=john_membership, event=locked_event,
            availability_state=AVAILABILITY_UNAVAILABLE,
        )

    assert existing.availability_state == AVAILABILITY_AVAILABLE  # untouched
    assert _audit_rows(session) == []


def test_locked_period_rejects_removal(session, head, setup_ministry, john_membership, monkeypatch):
    """The lock still protects the collected input set even for cleanup --
    unlike the membership/person/event activity checks, it is not exempted."""
    locked_period = _period(1, ministry=setup_ministry, locked=True)
    locked_event = _event(700, ministry=setup_ministry, period=locked_period)
    existing = _existing_availability(500, membership=john_membership, event=locked_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=john_membership, event=locked_event,
            availability_state=None,
        )

    assert existing not in session.deleted_objects
    assert _audit_rows(session) == []


def test_no_row_plus_none_under_locked_period_remains_a_true_no_op(
    session, head, setup_ministry, john_membership, monkeypatch
):
    """The preferred behavior stated explicitly in the Task 17 brief: this must
    not fail merely because the period is locked, since nothing is attempted."""
    locked_period = _period(1, ministry=setup_ministry, locked=True)
    locked_event = _event(700, ministry=setup_ministry, period=locked_period)
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=locked_event,
        availability_state=None,
    )

    assert result is None
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_idempotent_same_state_under_locked_period_also_remains_a_true_no_op(
    session, head, setup_ministry, john_membership, monkeypatch
):
    """Generalizing the brief's stated no-op exemption: the SchedulingPeriod
    lock section's own list of operations it must reject -- create, create,
    change, remove -- names only the mutating transitions and omits this shape
    of no-op exactly as it omits no-row+None. An unchanged request is not the
    kind of "availability change" the lock exists to prevent."""
    locked_period = _period(1, ministry=setup_ministry, locked=True)
    locked_event = _event(700, ministry=setup_ministry, period=locked_period)
    existing = _existing_availability(500, membership=john_membership, event=locked_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=john_membership, event=locked_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )

    assert result is existing
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 26, 27, 28 -- Flush discipline
# --------------------------------------------------------------------------


def test_create_uses_exactly_one_flush(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )

    assert session.flush_calls == 1


def test_update_uses_no_flush(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_UNAVAILABLE,
    )

    assert session.flush_calls == 0


def test_removal_uses_no_flush(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=None,
    )

    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 29, 30, 31, 32, 33 -- Audit correctness
# --------------------------------------------------------------------------


def test_recorded_audit_is_correct(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_AVAILABILITY_RECORDED
    assert audit.actor_type == "PERSON"
    assert audit.actor_person_id == 1
    assert audit.actor_label == "Demo Admin"
    assert audit.target_table == "availability"
    assert audit.target_id == result.id == 900
    assert audit.ministry_id == 3
    assert audit.summary == "Recorded John as available for Setup on 2026-10-04"
    assert audit.before_values is None
    assert audit.after_values == {
        "ministry_membership_id": 118, "event_id": 700, "availability_state": "AVAILABLE",
    }
    assert audit.reason is None


def test_backup_recorded_audit_is_correct_and_never_says_unavailable(
    session, head, john_membership, sunday_event, monkeypatch
):
    """Task 51 flagged the pre-Task-52 ``_meaning()`` as a latent bug: a third
    stored state would have been described as "unavailable" in the audit
    summary. This pins the fix: BACKUP gets its own wording, distinct from
    both AVAILABLE and UNAVAILABLE.
    """
    _stub_lookup(monkeypatch, None)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_BACKUP,
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_AVAILABILITY_RECORDED
    assert audit.target_id == result.id
    assert audit.summary == "Recorded John as available as backup for Setup on 2026-10-04"
    assert "unavailable" not in audit.summary
    assert audit.after_values == {
        "ministry_membership_id": 118, "event_id": 700, "availability_state": "BACKUP",
    }


def test_changed_to_backup_audit_is_correct_and_never_says_unavailable(
    session, head, john_membership, sunday_event, monkeypatch
):
    existing = _existing_availability(
        500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE
    )
    _stub_lookup(monkeypatch, existing)

    set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_BACKUP,
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_AVAILABILITY_CHANGED
    assert audit.summary == "Changed John to available as backup for Setup on 2026-10-04"
    assert "unavailable" not in audit.summary
    assert audit.after_values == {"availability_state": "BACKUP"}


def test_changed_audit_is_correct(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_UNAVAILABLE, reason="Family commitment.",
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_AVAILABILITY_CHANGED
    assert audit.target_id == 500
    assert audit.ministry_id == 3
    assert audit.summary == "Changed John to unavailable for Setup on 2026-10-04"
    assert audit.before_values == {"availability_state": "AVAILABLE"}
    assert audit.after_values == {"availability_state": "UNAVAILABLE"}
    assert audit.reason == "Family commitment."


def test_cleared_audit_is_correct(session, head, john_membership, sunday_event, monkeypatch):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_UNAVAILABLE)
    _stub_lookup(monkeypatch, existing)

    set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=None,
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_AVAILABILITY_CLEARED
    assert audit.target_id == 500
    assert audit.ministry_id == 3
    assert audit.summary == "Cleared John's Setup availability response for 2026-10-04"
    assert audit.before_values == {
        "ministry_membership_id": 118, "event_id": 700, "availability_state": "UNAVAILABLE",
    }
    assert audit.after_values is None


# --------------------------------------------------------------------------
# 34 -- Whitespace-only reason
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_whitespace_only_reason_is_rejected(session, head, john_membership, sunday_event, monkeypatch, blank):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        set_availability(
            session, actor=head, membership=john_membership, event=sunday_event,
            availability_state=AVAILABILITY_AVAILABLE, reason=blank,
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 35 -- Transaction discipline
# --------------------------------------------------------------------------


def test_service_never_commits_or_rolls_back(session, head, john_membership, sunday_event, monkeypatch):
    _stub_lookup(monkeypatch, None)
    set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=AVAILABILITY_AVAILABLE,
    )

    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)
    set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=None,
    )

    assert session.commit_calls == 0
    assert session.rollback_calls == 0


# --------------------------------------------------------------------------
# 36 -- Absence is never a stored value
# --------------------------------------------------------------------------


def test_absence_is_represented_by_no_row_never_a_stored_unknown_value(
    session, head, john_membership, sunday_event, monkeypatch
):
    existing = _existing_availability(500, membership=john_membership, event=sunday_event, state=AVAILABILITY_AVAILABLE)
    _stub_lookup(monkeypatch, existing)

    result = set_availability(
        session, actor=head, membership=john_membership, event=sunday_event,
        availability_state=None,
    )

    assert result is None  # not an object with availability_state="UNKNOWN"


# --------------------------------------------------------------------------
# Lookup statement, tested without any Session or database at all
# --------------------------------------------------------------------------


def test_the_lookup_statement_filters_on_the_integrity_columns():
    stmt = _availability_lookup_statement(118, 700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "FROM availability" in compiled
    assert "availability.ministry_membership_id = 118" in compiled
    assert "availability.event_id = 700" in compiled


def test_the_lookup_statement_selects_the_availability_model():
    stmt = _availability_lookup_statement(1, 1)

    assert stmt.column_descriptions[0]["type"] is Availability


# ==========================================================================
# list_event_availability (Task 56)
# ==========================================================================


class _Row:
    """A stand-in for one row of ``_event_availability_statement``'s result."""

    def __init__(
        self, ministry_membership_id, person_id, person_display_name,
        membership_deactivated_at, person_deactivated_at, availability_state,
    ):
        self.ministry_membership_id = ministry_membership_id
        self.person_id = person_id
        self.person_display_name = person_display_name
        self.membership_deactivated_at = membership_deactivated_at
        self.person_deactivated_at = person_deactivated_at
        self.availability_state = availability_state


def _stub_event_availability_rows(session, monkeypatch, rows: list[_Row]) -> None:
    """Bypass the query entirely: its SQL shape is pinned separately below,
    and here only orchestration (authorization, and the rows-to-dataclass
    mapping) is under test.
    """

    class _Result:
        def all(self):
            return rows

    monkeypatch.setattr(session, "execute", lambda stmt: _Result())


def test_admin_can_list_event_availability(session, head, setup_ministry, sunday_event, monkeypatch):
    _stub_event_availability_rows(
        session, monkeypatch,
        [
            _Row(118, 42, "Ann", None, None, AVAILABILITY_AVAILABLE),
            _Row(119, 43, "Ben", None, None, AVAILABILITY_BACKUP),
            _Row(120, 44, "Cam", None, None, AVAILABILITY_UNAVAILABLE),
            _Row(121, 45, "Dee", None, None, None),
        ],
    )

    result = list_event_availability(session, actor=head, event=sunday_event)

    assert isinstance(result, EventAvailability)
    assert result.event_id == 700
    assert result.ministry_id == 3
    assert result.availability_locked_at is None
    assert result.memberships == (
        MembershipAvailability(118, 42, "Ann", None, None, AVAILABILITY_AVAILABLE),
        MembershipAvailability(119, 43, "Ben", None, None, AVAILABILITY_BACKUP),
        MembershipAvailability(120, 44, "Cam", None, None, AVAILABILITY_UNAVAILABLE),
        MembershipAvailability(121, 45, "Dee", None, None, None),
    )


def test_own_ministry_head_can_list_event_availability(session, setup_ministry, sunday_event, monkeypatch):
    _stub_event_availability_rows(session, monkeypatch, [])
    head = _person(9, "Head Person")
    _actor_membership(200, person=head, ministry=setup_ministry, is_head=True)

    result = list_event_availability(session, actor=head, event=sunday_event)

    assert result.memberships == ()


def test_other_ministry_head_cannot_list_event_availability(session, sunday_event, monkeypatch):
    _stub_event_availability_rows(session, monkeypatch, [])
    other_ministry = _ministry(4, "AV")
    head = _person(9, "Head Person")
    _actor_membership(200, person=head, ministry=other_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        list_event_availability(session, actor=head, event=sunday_event)


def test_normal_member_cannot_list_event_availability(session, setup_ministry, sunday_event, monkeypatch):
    _stub_event_availability_rows(session, monkeypatch, [])
    member = _person(9, "Member Person")
    _actor_membership(200, person=member, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        list_event_availability(session, actor=member, event=sunday_event)


def test_no_response_reports_none_not_a_placeholder_string(
    session, head, sunday_event, monkeypatch
):
    _stub_event_availability_rows(
        session, monkeypatch, [_Row(118, 42, "Ann", None, None, None)],
    )

    result = list_event_availability(session, actor=head, event=sunday_event)

    assert result.memberships[0].availability_state is None


def test_locked_period_is_reported_on_the_listing(session, head, setup_ministry, monkeypatch):
    locked_event = _event(701, ministry=setup_ministry, period=_period(2, ministry=setup_ministry, locked=True))
    _stub_event_availability_rows(session, monkeypatch, [])

    result = list_event_availability(session, actor=head, event=locked_event)

    assert result.availability_locked_at is not None


def test_the_event_availability_statement_only_admits_active_memberships_of_this_ministry():
    stmt = _event_availability_statement(700, 3, include_inactive=False)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "ministry_membership.ministry_id = 3" in compiled
    assert "LEFT OUTER JOIN availability" in compiled
    assert "availability.event_id = 700" in compiled
    assert "ministry_membership.deactivated_at IS NULL" in compiled
    assert "person.deactivated_at IS NULL" in compiled


def test_the_event_availability_statement_includes_inactive_when_asked():
    stmt = _event_availability_statement(700, 3, include_inactive=True)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "deactivated_at IS NULL" not in compiled
