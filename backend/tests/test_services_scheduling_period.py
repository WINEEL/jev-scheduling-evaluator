"""Scheduling Period / Sunday-generation service tests.

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Task 14's precedent exactly, for the same reasons:

- Both operations here need a ``session.flush()`` (to obtain a new row's
  identity before the audit row referencing it can be built) and a database
  lookup (``session.execute(select(...))``, to find an existing period name or
  existing Sunday-service dates). Neither can be exercised against a real,
  unbound :class:`~sqlalchemy.orm.Session` -- both would try to issue real SQL
  and raise ``UnboundExecutionError``.
- **The lookups' SQL** is tested directly and mechanically, with no session or
  database at all: ``_period_name_lookup_statement`` and
  ``_existing_sunday_service_dates_statement`` return plain SQLAlchemy
  ``Select`` objects that are compiled with literal binds and inspected.
- **The lookups' use** (found vs. not-found, and which dates are "already
  generated") is exercised via ``monkeypatch`` in every orchestration test,
  exactly as ``_find_existing_qualification`` was monkeypatched in
  ``test_services_role_qualification.py``.
- **The flush** uses a real, unbound ``Session`` subclass whose ``flush()``
  simulates identity assignment without touching a database. ``commit()`` and
  ``rollback()`` remain forbidden.

What this cannot verify, same honest limitation as Tasks 13 and 14: that
PostgreSQL actually assigns identities and rolls back a real transaction. What
it verifies instead is the same shape of property those tasks settled for --
the service never commits or rolls back, flushes only where the design says it
must (once per call, only when there is something new to flush), and every
mutation and its audit row share one session.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import EVENT_KIND_SPECIAL, EVENT_KIND_SUNDAY_SERVICE, Event, SchedulingPeriod
from app.services import AuthorizationError, InvalidOperationError
from app.services.scheduling_period import (
    ACTION_AVAILABILITY_LOCKED,
    ACTION_EVENT_CREATED,
    ACTION_SCHEDULING_PERIOD_CREATED,
    EventSummary,
    _existing_sunday_service_dates_statement,
    _period_events_statement,
    _period_name_lookup_statement,
    _sundays_between,
    create_scheduling_period,
    generate_sunday_events,
    list_period_events,
    lock_availability,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class SchedulingPeriodSession(Session):
    """A real, unbound Session for these tests.

    Like Task 14's ``RoleQualificationSession``, ``flush()`` is not forbidden
    here -- both services in this module legitimately flush once, when they
    create new rows, to obtain identities for the audit rows that follow.
    ``commit()`` and ``rollback()`` remain forbidden.
    """

    def __init__(self, *, next_id: int = 900) -> None:
        super().__init__()
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self._next_id = next_id

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def flush(self, objects=None) -> None:
        """Assign an id to any pending new row that does not already have one."""
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


@pytest.fixture
def session() -> SchedulingPeriodSession:
    return SchedulingPeriodSession()


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


def _ministry(ministry_id: int, name: str, *, deactivated: bool = False) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    if deactivated:
        ministry.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return ministry


def _membership(
    membership_id: int,
    *,
    person: Person,
    ministry: Ministry,
    is_head: bool = False,
    deactivated: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    membership.person = person
    membership.ministry = ministry
    if deactivated:
        membership.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    person.ministry_memberships.append(membership)
    return membership


def _period(
    period_id: int,
    *,
    ministry: Ministry,
    name: str = "Setup Q4 2026",
    start_date: datetime.date = datetime.date(2026, 10, 4),
    end_date: datetime.date = datetime.date(2026, 12, 27),
    locked_at: datetime.datetime | None = None,
) -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=name, start_date=start_date, end_date=end_date,
    )
    period.id = period_id
    period.ministry = ministry
    period.availability_locked_at = locked_at
    return period


def _existing_event(
    event_id: int,
    *,
    period: SchedulingPeriod,
    event_date: datetime.date,
    event_kind: str = EVENT_KIND_SUNDAY_SERVICE,
    name: str | None = None,
    cancelled: bool = False,
) -> Event:
    event = Event(
        scheduling_period_id=period.id,
        ministry_id=period.ministry_id,
        event_date=event_date,
        event_kind=event_kind,
        name=name,
    )
    event.id = event_id
    if cancelled:
        event.cancelled_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return event


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
    _membership(9001, person=person, ministry=setup_ministry, is_head=True)
    return person


@pytest.fixture
def setup_ministry() -> Ministry:
    return _ministry(3, "Setup")


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _stub_period_lookup(monkeypatch, existing: SchedulingPeriod | None) -> None:
    import app.services.scheduling_period as module

    monkeypatch.setattr(
        module, "_find_period_by_name",
        lambda session, *, ministry_id, name: existing,
    )


def _stub_existing_dates(monkeypatch, dates: set[datetime.date]) -> None:
    import app.services.scheduling_period as module

    monkeypatch.setattr(
        module, "_existing_sunday_service_dates",
        lambda session, period: dates,
    )


# ==========================================================================
# create_scheduling_period
# ==========================================================================


# --------------------------------------------------------------------------
# Admin / Ministry Head / authorization
# --------------------------------------------------------------------------


def test_admin_can_create_scheduling_period(session, head, setup_ministry, monkeypatch):
    _stub_period_lookup(monkeypatch, None)

    period = create_scheduling_period(
        session, actor=head, ministry=setup_ministry, name="Setup Q4 2026",
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )

    assert period.ministry_id == 3
    assert period.name == "Setup Q4 2026"
    assert period.start_date == datetime.date(2026, 10, 4)
    assert period.end_date == datetime.date(2026, 12, 27)
    assert period.availability_locked_at is None
    assert period.id == 900


def test_ministry_head_can_create_a_period_for_their_own_ministry(
    session, setup_ministry, monkeypatch
):
    _stub_period_lookup(monkeypatch, None)
    head_person = _person(9, "Head Person")
    _membership(200, person=head_person, ministry=setup_ministry, is_head=True)

    period = create_scheduling_period(
        session, actor=head_person, ministry=setup_ministry, name="Setup Q4 2026",
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )

    assert period.ministry_id == 3


def test_ministry_head_cannot_create_a_period_for_another_ministry(
    session, monkeypatch
):
    _stub_period_lookup(monkeypatch, None)
    setup_ministry = _ministry(3, "Setup")
    av_ministry = _ministry(4, "AV")
    av_head = _person(9, "AV Head")
    _membership(201, person=av_head, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        create_scheduling_period(
            session, actor=av_head, ministry=setup_ministry, name="Setup Q4 2026",
            start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_normal_user_cannot_create_a_period(session, setup_ministry, monkeypatch):
    _stub_period_lookup(monkeypatch, None)
    ordinary = _person(7, "Ada")
    _membership(202, person=ordinary, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        create_scheduling_period(
            session, actor=ordinary, ministry=setup_ministry, name="Setup Q4 2026",
            start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
        )

    assert _audit_rows(session) == []


def test_deactivated_actor_cannot_create_a_period(session, setup_ministry, monkeypatch):
    _stub_period_lookup(monkeypatch, None)
    departed_admin = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        create_scheduling_period(
            session, actor=departed_admin, ministry=setup_ministry, name="Setup Q4 2026",
            start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
        )

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def test_deactivated_ministry_is_rejected(session, head, monkeypatch):
    _stub_period_lookup(monkeypatch, None)
    inactive_ministry = _ministry(3, "Setup", deactivated=True)

    with pytest.raises(InvalidOperationError):
        create_scheduling_period(
            session, actor=head, ministry=inactive_ministry, name="Setup Q4 2026",
            start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_blank_period_name_is_rejected(session, head, setup_ministry, monkeypatch, blank):
    _stub_period_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        create_scheduling_period(
            session, actor=head, ministry=setup_ministry, name=blank,
            start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_start_after_end_is_rejected(session, head, setup_ministry, monkeypatch):
    _stub_period_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        create_scheduling_period(
            session, actor=head, ministry=setup_ministry, name="Setup Q4 2026",
            start_date=datetime.date(2026, 12, 27), end_date=datetime.date(2026, 10, 4),
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_duplicate_period_name_in_same_ministry_is_rejected_case_insensitively(
    session, head, setup_ministry, monkeypatch
):
    existing = _period(500, ministry=setup_ministry, name="setup q4 2026")
    _stub_period_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        create_scheduling_period(
            session, actor=head, ministry=setup_ministry, name="Setup Q4 2026",
            start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
        )

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# Flush / transaction / audit
# --------------------------------------------------------------------------


def test_create_period_flushes_exactly_once(session, head, setup_ministry, monkeypatch):
    _stub_period_lookup(monkeypatch, None)

    create_scheduling_period(
        session, actor=head, ministry=setup_ministry, name="Setup Q4 2026",
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )

    assert session.flush_calls == 1
    assert session.commit_calls == 0
    assert session.rollback_calls == 0


def test_create_period_records_a_correct_audit_row(session, head, setup_ministry, monkeypatch):
    _stub_period_lookup(monkeypatch, None)

    period = create_scheduling_period(
        session, actor=head, ministry=setup_ministry, name="Setup Q4 2026",
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )

    rows = _audit_rows(session)
    assert len(rows) == 1
    audit = rows[0]
    assert audit.action == ACTION_SCHEDULING_PERIOD_CREATED
    assert audit.actor_type == "PERSON"
    assert audit.actor_person_id == 1
    assert audit.actor_label == "Demo Admin"
    assert audit.target_table == "scheduling_period"
    assert audit.target_id == period.id == 900
    assert audit.ministry_id == 3
    assert audit.summary == "Created scheduling period Setup Q4 2026 for Setup"
    assert audit.before_values is None
    assert audit.after_values == {
        "name": "Setup Q4 2026",
        "start_date": "2026-10-04",
        "end_date": "2026-12-27",
        "availability_locked_at": None,
    }
    assert audit.reason is None


def test_create_period_never_commits_or_rolls_back(session, head, setup_ministry, monkeypatch):
    _stub_period_lookup(monkeypatch, None)

    create_scheduling_period(
        session, actor=head, ministry=setup_ministry, name="Setup Q4 2026",
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )

    assert session.commit_calls == 0
    assert session.rollback_calls == 0


# ==========================================================================
# _sundays_between (pure helper)
# ==========================================================================


def test_sundays_between_includes_both_sunday_endpoints():
    result = _sundays_between(datetime.date(2026, 10, 4), datetime.date(2026, 10, 11))

    assert result == [datetime.date(2026, 10, 4), datetime.date(2026, 10, 11)]


def test_sundays_between_excludes_non_sundays_at_the_endpoints():
    """Monday start, Saturday end: only the Sundays strictly inside count."""
    result = _sundays_between(datetime.date(2026, 10, 5), datetime.date(2026, 10, 17))

    assert result == [datetime.date(2026, 10, 11)]


def test_sundays_between_returns_empty_for_a_range_with_no_sunday():
    result = _sundays_between(datetime.date(2026, 10, 5), datetime.date(2026, 10, 10))

    assert result == []


def test_sundays_between_returns_empty_for_an_inverted_range():
    result = _sundays_between(datetime.date(2026, 10, 11), datetime.date(2026, 10, 4))

    assert result == []


def test_sundays_between_a_single_sunday_day():
    result = _sundays_between(datetime.date(2026, 10, 4), datetime.date(2026, 10, 4))

    assert result == [datetime.date(2026, 10, 4)]


def test_sundays_between_a_full_thirteen_week_period():
    result = _sundays_between(datetime.date(2026, 10, 4), datetime.date(2026, 12, 27))

    assert len(result) == 13
    assert result[0] == datetime.date(2026, 10, 4)
    assert result[-1] == datetime.date(2026, 12, 27)
    assert all(d.weekday() == 6 for d in result)


# ==========================================================================
# generate_sunday_events
# ==========================================================================


# --------------------------------------------------------------------------
# Authorization -- same rules as create_scheduling_period
# --------------------------------------------------------------------------


def test_admin_can_generate_sunday_events(session, head, setup_ministry, monkeypatch):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 4),
    )
    _stub_existing_dates(monkeypatch, set())

    events = generate_sunday_events(session, actor=head, period=period)

    assert len(events) == 1
    assert events[0].event_date == datetime.date(2026, 10, 4)


def test_ministry_head_can_generate_events_for_their_own_ministry(
    session, setup_ministry, monkeypatch
):
    head_person = _person(9, "Head Person")
    _membership(200, person=head_person, ministry=setup_ministry, is_head=True)
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 4),
    )
    _stub_existing_dates(monkeypatch, set())

    events = generate_sunday_events(session, actor=head_person, period=period)

    assert len(events) == 1


def test_ministry_head_cannot_generate_events_for_another_ministry(session, monkeypatch):
    setup_ministry = _ministry(3, "Setup")
    av_ministry = _ministry(4, "AV")
    av_head = _person(9, "AV Head")
    _membership(201, person=av_head, ministry=av_ministry, is_head=True)
    period = _period(700, ministry=setup_ministry)
    _stub_existing_dates(monkeypatch, set())

    with pytest.raises(AuthorizationError):
        generate_sunday_events(session, actor=av_head, period=period)

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_normal_user_cannot_generate_events(session, setup_ministry, monkeypatch):
    ordinary = _person(7, "Ada")
    _membership(202, person=ordinary, ministry=setup_ministry, is_head=False)
    period = _period(700, ministry=setup_ministry)
    _stub_existing_dates(monkeypatch, set())

    with pytest.raises(AuthorizationError):
        generate_sunday_events(session, actor=ordinary, period=period)

    assert _audit_rows(session) == []


def test_deactivated_actor_cannot_generate_events(session, setup_ministry, monkeypatch):
    departed_admin = _person(5, "Departed Admin", is_admin=True, deactivated=True)
    period = _period(700, ministry=setup_ministry)
    _stub_existing_dates(monkeypatch, set())

    with pytest.raises(AuthorizationError):
        generate_sunday_events(session, actor=departed_admin, period=period)

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# Generated event correctness
# --------------------------------------------------------------------------


def test_generated_events_have_correct_ministry_period_date_and_kind(
    session, head, setup_ministry, monkeypatch
):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 11),
    )
    _stub_existing_dates(monkeypatch, set())

    events = generate_sunday_events(session, actor=head, period=period)

    assert len(events) == 2
    for event in events:
        assert event.scheduling_period_id == 700
        assert event.ministry_id == 3
        assert event.event_kind == EVENT_KIND_SUNDAY_SERVICE
        assert event.name is None
    assert {e.event_date for e in events} == {
        datetime.date(2026, 10, 4), datetime.date(2026, 10, 11)
    }


def test_zero_sunday_period_generates_nothing(session, head, setup_ministry, monkeypatch):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 5), end_date=datetime.date(2026, 10, 10),
    )
    _stub_existing_dates(monkeypatch, set())

    events = generate_sunday_events(session, actor=head, period=period)

    assert events == []
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_an_existing_ordinary_sunday_event_is_skipped(session, head, setup_ministry, monkeypatch):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 11),
    )
    _stub_existing_dates(monkeypatch, {datetime.date(2026, 10, 4)})

    events = generate_sunday_events(session, actor=head, period=period)

    assert len(events) == 1
    assert events[0].event_date == datetime.date(2026, 10, 11)


def test_a_cancelled_ordinary_sunday_event_is_also_skipped(
    session, head, setup_ministry, monkeypatch
):
    """The cancelled event's date is already in the stubbed 'existing' set --
    proving the lookup, not just the branching, includes cancelled rows is the
    job of test_the_existing_dates_statement_ignores_cancelled_at below."""
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 4),
    )
    _stub_existing_dates(monkeypatch, {datetime.date(2026, 10, 4)})

    events = generate_sunday_events(session, actor=head, period=period)

    assert events == []
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_a_special_event_on_the_same_sunday_does_not_block_generation(
    session, head, setup_ministry, monkeypatch
):
    """A SPECIAL event's date must never appear in the 'already generated' set
    -- proven at the query level in
    test_the_existing_dates_statement_only_matches_sunday_service below. Here
    we prove the orchestration: an empty 'existing' set (as it would be with
    only a SPECIAL event on this Sunday) still generates the ordinary event."""
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 4),
    )
    _stub_existing_dates(monkeypatch, set())  # the SPECIAL event never appears here

    events = generate_sunday_events(session, actor=head, period=period)

    assert len(events) == 1
    assert events[0].event_kind == EVENT_KIND_SUNDAY_SERVICE


def test_running_again_with_every_sunday_already_represented_creates_nothing(
    session, head, setup_ministry, monkeypatch
):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 11),
    )
    _stub_existing_dates(
        monkeypatch, {datetime.date(2026, 10, 4), datetime.date(2026, 10, 11)}
    )

    events = generate_sunday_events(session, actor=head, period=period)

    assert events == []


def test_no_op_generation_creates_no_audit_and_no_flush(
    session, head, setup_ministry, monkeypatch
):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 11),
    )
    _stub_existing_dates(
        monkeypatch, {datetime.date(2026, 10, 4), datetime.date(2026, 10, 11)}
    )

    generate_sunday_events(session, actor=head, period=period)

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# Batch flush and audit
# --------------------------------------------------------------------------


def test_a_batch_of_new_events_uses_exactly_one_flush(
    session, head, setup_ministry, monkeypatch
):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    _stub_existing_dates(monkeypatch, set())

    events = generate_sunday_events(session, actor=head, period=period)

    assert len(events) == 13
    assert session.flush_calls == 1


def test_each_created_event_gets_exactly_one_audit_event_no_batch_row(
    session, head, setup_ministry, monkeypatch
):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 11),
    )
    _stub_existing_dates(monkeypatch, set())

    events = generate_sunday_events(session, actor=head, period=period)

    rows = _audit_rows(session)
    assert len(rows) == 2  # exactly one per event, no extra summary row
    assert {r.target_id for r in rows} == {e.id for e in events}


def test_event_audit_rows_are_correct(session, head, setup_ministry, monkeypatch):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 4),
    )
    _stub_existing_dates(monkeypatch, set())

    events = generate_sunday_events(session, actor=head, period=period)
    audit = _audit_rows(session)[0]

    assert audit.action == ACTION_EVENT_CREATED
    assert audit.actor_type == "PERSON"
    assert audit.actor_person_id == 1
    assert audit.actor_label == "Demo Admin"
    assert audit.target_table == "event"
    assert audit.target_id == events[0].id == 900
    assert audit.ministry_id == 3
    assert audit.summary == "Created Sunday service event for Setup on 2026-10-04"
    assert audit.before_values is None
    assert audit.after_values == {
        "scheduling_period_id": 700,
        "event_date": "2026-10-04",
        "event_kind": "SUNDAY_SERVICE",
    }


def test_audit_date_values_are_json_compatible_strings(
    session, head, setup_ministry, monkeypatch
):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 4),
    )
    _stub_existing_dates(monkeypatch, set())

    generate_sunday_events(session, actor=head, period=period)
    audit = _audit_rows(session)[0]

    assert isinstance(audit.after_values["event_date"], str)
    assert audit.after_values["event_date"] == "2026-10-04"
    import json
    json.dumps(audit.after_values)  # must not raise


def test_generate_events_never_commits_or_rolls_back(
    session, head, setup_ministry, monkeypatch
):
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 10, 11),
    )
    _stub_existing_dates(monkeypatch, set())

    generate_sunday_events(session, actor=head, period=period)

    assert session.commit_calls == 0
    assert session.rollback_calls == 0


# ==========================================================================
# Lookup statements, tested without any Session or database at all
# ==========================================================================


def test_the_period_name_lookup_statement_filters_case_insensitively():
    stmt = _period_name_lookup_statement(3, "Setup Q4 2026")
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "FROM scheduling_period" in compiled
    assert "scheduling_period.ministry_id = 3" in compiled
    assert "lower(scheduling_period.name) = 'setup q4 2026'" in compiled


def test_the_existing_dates_statement_filters_on_period_and_sunday_service_kind():
    stmt = _existing_sunday_service_dates_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "FROM event" in compiled
    assert "event.scheduling_period_id = 700" in compiled
    assert "event.event_kind = 'SUNDAY_SERVICE'" in compiled


def test_the_existing_dates_statement_ignores_cancelled_at():
    """No predicate on cancelled_at at all -- a cancelled Sunday event must
    still count as 'already generated'."""
    stmt = _existing_sunday_service_dates_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "cancelled_at" not in compiled


def test_the_existing_dates_statement_only_matches_sunday_service():
    """A SPECIAL event's date is structurally excluded by the WHERE clause
    itself, not merely by application-level filtering after the fact."""
    stmt = _existing_sunday_service_dates_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert EVENT_KIND_SPECIAL not in compiled
    assert compiled.count("event_kind") == 1


# ==========================================================================
# lock_availability
# ==========================================================================


#: A fixed instant so tests can assert the exact stored/audited timestamp
#: without sleeping or fragile timing ranges -- monkeypatched over
#: ``scheduling_period._now_utc`` rather than reaching for a generic clock
#: framework, matching the module's own reasoning for why that one-line
#: indirection exists at all.
_FIXED_NOW = datetime.datetime(2026, 9, 15, 12, 30, 0, tzinfo=datetime.timezone.utc)


def _freeze_now(monkeypatch, instant: datetime.datetime = _FIXED_NOW) -> None:
    import app.services.scheduling_period as module

    monkeypatch.setattr(module, "_now_utc", lambda: instant)


# --------------------------------------------------------------------------
# 1, 2, 3, 4, 5 -- Authorization
# --------------------------------------------------------------------------


def test_admin_can_lock_availability(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    result = lock_availability(session, actor=head, period=period)

    assert result.availability_locked_at == _FIXED_NOW


def test_own_ministry_head_can_lock_availability(session, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=setup_ministry, is_head=True)

    result = lock_availability(session, actor=head, period=period)

    assert result.availability_locked_at == _FIXED_NOW


def test_head_of_another_ministry_is_rejected(session, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)
    av_ministry = _ministry(4, "AV")
    av_head = _person(9, "AV Head")
    _membership(201, person=av_head, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        lock_availability(session, actor=av_head, period=period)

    assert period.availability_locked_at is None
    assert _audit_rows(session) == []


def test_normal_user_is_rejected(session, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)
    ordinary = _person(7, "Ada")
    _membership(202, person=ordinary, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        lock_availability(session, actor=ordinary, period=period)

    assert period.availability_locked_at is None
    assert _audit_rows(session) == []


def test_globally_deactivated_actor_is_rejected(session, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)
    departed_admin = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        lock_availability(session, actor=departed_admin, period=period)

    assert period.availability_locked_at is None
    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 6 -- Timestamp
# --------------------------------------------------------------------------


def test_open_period_receives_a_timezone_aware_lock_timestamp(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)
    assert period.availability_locked_at is None

    result = lock_availability(session, actor=head, period=period)

    assert result.availability_locked_at == _FIXED_NOW
    assert result.availability_locked_at.tzinfo is not None


# --------------------------------------------------------------------------
# 7, 8, 9, 10 -- Idempotency
# --------------------------------------------------------------------------


def test_existing_lock_is_idempotent(session, head, setup_ministry, monkeypatch):
    original_lock = datetime.datetime(2026, 8, 1, 9, 0, 0, tzinfo=datetime.timezone.utc)
    period = _period(700, ministry=setup_ministry, locked_at=original_lock)
    _freeze_now(monkeypatch, datetime.datetime(2026, 9, 15, tzinfo=datetime.timezone.utc))

    result = lock_availability(session, actor=head, period=period)

    # `result is period` alone would pass even if the no-op branch were
    # missing, since the mutating path also returns `period` -- the
    # distinguishing assertion is that nothing was recorded.
    assert result is period
    assert _audit_rows(session) == []


def test_idempotent_lock_preserves_the_exact_original_timestamp(session, head, setup_ministry, monkeypatch):
    original_lock = datetime.datetime(2026, 8, 1, 9, 0, 0, tzinfo=datetime.timezone.utc)
    period = _period(700, ministry=setup_ministry, locked_at=original_lock)
    _freeze_now(monkeypatch, datetime.datetime(2026, 9, 15, tzinfo=datetime.timezone.utc))

    lock_availability(session, actor=head, period=period)

    assert period.availability_locked_at == original_lock


def test_idempotent_lock_creates_no_audit_event(session, head, setup_ministry, monkeypatch):
    original_lock = datetime.datetime(2026, 8, 1, 9, 0, 0, tzinfo=datetime.timezone.utc)
    period = _period(700, ministry=setup_ministry, locked_at=original_lock)
    _freeze_now(monkeypatch)

    lock_availability(session, actor=head, period=period)

    assert _audit_rows(session) == []


def test_idempotent_lock_does_not_flush(session, head, setup_ministry, monkeypatch):
    original_lock = datetime.datetime(2026, 8, 1, 9, 0, 0, tzinfo=datetime.timezone.utc)
    period = _period(700, ministry=setup_ministry, locked_at=original_lock)
    _freeze_now(monkeypatch)

    lock_availability(session, actor=head, period=period)

    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# 11, 12, 13, 14, 15, 16 -- Audit correctness
# --------------------------------------------------------------------------


def test_successful_lock_creates_exactly_one_audit_event(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period)

    rows = _audit_rows(session)
    assert len(rows) == 1
    assert rows[0].action == ACTION_AVAILABILITY_LOCKED


def test_audit_target_table_and_id_are_correct(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period)
    audit = _audit_rows(session)[0]

    assert audit.target_table == "scheduling_period"
    assert audit.target_id == 700


def test_audit_ministry_id_derives_from_period(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period)
    audit = _audit_rows(session)[0]

    assert audit.ministry_id == setup_ministry.id == 3


def test_before_payload_contains_null_lock_timestamp(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period)
    audit = _audit_rows(session)[0]

    assert audit.before_values == {"availability_locked_at": None}


def test_after_payload_contains_json_compatible_iso_timestamp(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period)
    audit = _audit_rows(session)[0]

    assert audit.after_values == {"availability_locked_at": _FIXED_NOW.isoformat()}
    assert isinstance(audit.after_values["availability_locked_at"], str)
    import json
    json.dumps(audit.after_values)  # must not raise


def test_optional_reason_is_copied_to_audit_event_reason(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period, reason="Availability window closed.")
    audit = _audit_rows(session)[0]

    assert audit.reason == "Availability window closed."


def test_audit_summary_uses_the_actual_period_name(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry, name="Setup Q4 2026")

    lock_availability(session, actor=head, period=period)
    audit = _audit_rows(session)[0]

    assert audit.summary == "Locked availability for Setup Q4 2026"


# --------------------------------------------------------------------------
# 17 -- Reason validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_whitespace_only_reason_is_rejected(session, head, setup_ministry, monkeypatch, blank):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    with pytest.raises(InvalidOperationError):
        lock_availability(session, actor=head, period=period, reason=blank)

    assert period.availability_locked_at is None
    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 18 -- No completeness checks introduced
# --------------------------------------------------------------------------


def test_lock_succeeds_with_no_events_and_no_availability_rows_at_all(session, head, setup_ministry, monkeypatch):
    """No minimum-Events check, no Sunday-containment check, no Availability
    completeness check of any kind -- 'no response' is a valid, permanent
    input state, and locking must not require anyone to have answered."""
    _freeze_now(monkeypatch)
    # A period whose range contains no Sunday at all, and which this test
    # never populates with any Event or Availability row.
    period = _period(
        700, ministry=setup_ministry,
        start_date=datetime.date(2026, 10, 5), end_date=datetime.date(2026, 10, 10),
    )
    assert _sundays_between(period.start_date, period.end_date) == []

    result = lock_availability(session, actor=head, period=period)

    assert result.availability_locked_at == _FIXED_NOW


# --------------------------------------------------------------------------
# 19, 20, 21 -- Transaction discipline
# --------------------------------------------------------------------------


def test_lock_never_commits(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period)

    assert session.commit_calls == 0


def test_lock_never_rolls_back(session, head, setup_ministry, monkeypatch):
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period)

    assert session.rollback_calls == 0


def test_lock_does_not_flush(session, head, setup_ministry, monkeypatch):
    """No new row is created -- the period already has its identity -- so
    there is nothing an identity-only flush would obtain."""
    _freeze_now(monkeypatch)
    period = _period(700, ministry=setup_ministry)

    lock_availability(session, actor=head, period=period)

    assert session.flush_calls == 0


# --------------------------------------------------------------------------
# No unlock operation exists
# --------------------------------------------------------------------------


def test_no_unlock_reopen_or_clear_lock_function_exists():
    import app.services.scheduling_period as module

    for forbidden in ("unlock_availability", "reopen_availability", "clear_availability_lock"):
        assert not hasattr(module, forbidden)


# ==========================================================================
# list_period_events (Task 54)
# ==========================================================================


class _EventRow:
    """A stand-in for one row of ``_period_events_statement``'s result."""

    def __init__(self, id, event_date, name, event_kind, cancelled_at=None):
        self.id = id
        self.event_date = event_date
        self.name = name
        self.event_kind = event_kind
        self.cancelled_at = cancelled_at


def _stub_period_event_rows(session, monkeypatch, rows: list[_EventRow]) -> None:
    """Bypass the query entirely: its SQL shape is pinned separately below,
    and here only orchestration (authorization, and the rows-to-dataclass
    mapping) is under test.
    """

    class _Result:
        def all(self):
            return rows

    monkeypatch.setattr(session, "execute", lambda stmt: _Result())


def test_admin_can_list_period_events(session, head, setup_ministry, monkeypatch):
    period = _period(700, ministry=setup_ministry)
    _stub_period_event_rows(
        session, monkeypatch,
        [
            _EventRow(100, datetime.date(2026, 10, 4), None, EVENT_KIND_SUNDAY_SERVICE),
            _EventRow(101, datetime.date(2026, 10, 11), "Harvest Party", EVENT_KIND_SPECIAL),
        ],
    )

    result = list_period_events(session, actor=head, period=period)

    assert result == (
        EventSummary(
            event_id=100, event_date=datetime.date(2026, 10, 4), event_name=None,
            event_kind=EVENT_KIND_SUNDAY_SERVICE, cancelled_at=None,
        ),
        EventSummary(
            event_id=101, event_date=datetime.date(2026, 10, 11), event_name="Harvest Party",
            event_kind=EVENT_KIND_SPECIAL, cancelled_at=None,
        ),
    )


def test_own_ministry_head_can_list_period_events(session, setup_ministry, monkeypatch):
    period = _period(700, ministry=setup_ministry)
    _stub_period_event_rows(session, monkeypatch, [])
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=setup_ministry, is_head=True)

    assert list_period_events(session, actor=head, period=period) == ()


def test_other_ministry_head_cannot_list_period_events(session, monkeypatch):
    other_ministry = _ministry(4, "AV")
    period = _period(700, ministry=other_ministry)
    _stub_period_event_rows(session, monkeypatch, [])
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=_ministry(3, "Setup"), is_head=True)

    with pytest.raises(AuthorizationError):
        list_period_events(session, actor=head, period=period)


def test_normal_member_cannot_list_period_events(session, setup_ministry, monkeypatch):
    period = _period(700, ministry=setup_ministry)
    _stub_period_event_rows(session, monkeypatch, [])
    member = _person(9, "Member Person")
    _membership(200, person=member, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        list_period_events(session, actor=member, period=period)


def test_a_cancelled_event_is_shown_not_hidden(session, head, setup_ministry, monkeypatch):
    period = _period(700, ministry=setup_ministry)
    cancelled_at = datetime.datetime(2026, 10, 1, tzinfo=datetime.timezone.utc)
    _stub_period_event_rows(
        session, monkeypatch,
        [_EventRow(100, datetime.date(2026, 10, 4), None, EVENT_KIND_SUNDAY_SERVICE, cancelled_at)],
    )

    result = list_period_events(session, actor=head, period=period)

    assert result[0].cancelled_at == cancelled_at


def test_the_period_events_statement_filters_and_orders_by_date_then_id():
    stmt = _period_events_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "event.scheduling_period_id = 700" in compiled
    assert "ORDER BY event.event_date, event.id" in compiled
