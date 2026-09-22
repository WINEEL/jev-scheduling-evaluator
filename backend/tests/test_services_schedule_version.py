"""Initial Schedule / Version 1 creation service tests.

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Tasks 14-19's precedent, for the same reasons: this
service needs up to two ``session.flush()`` calls (to obtain a new Schedule's
and/or a new ScheduleVersion's identity before the audit rows referencing them
can be built) and three database lookups/queries -- finding an existing
Schedule, checking whether it already has any version, and reading the current
requirement-snapshot source. None of these can be exercised against a real,
unbound :class:`~sqlalchemy.orm.Session`.

- **Each query's SQL** is tested directly and mechanically, with no session or
  database at all: ``_schedule_lookup_statement``, ``_any_version_exists_statement``
  and ``_requirement_snapshot_source_statement`` all return plain SQLAlchemy
  ``Select`` objects compiled with literal binds and inspected.
- **Each query's use** is exercised via ``monkeypatch`` in every orchestration
  test.
- **The flushes** use a real, unbound ``Session`` subclass whose ``flush()``
  simulates identity assignment via one shared counter -- so a Schedule and a
  ScheduleVersion created in the same call always get *different* ids, never
  colliding across object types, exactly as a real flush against PostgreSQL
  would guarantee (two different identity sequences, two different values).
  ``commit()`` and ``rollback()`` remain forbidden.

What this cannot verify, same honest limitation as the earlier tasks: that
PostgreSQL actually assigns identities and rolls back a real transaction. What
it verifies instead is the same shape of property those tasks settled for --
the service never commits or rolls back, flushes only where the design says it
must, and every mutation and its audit rows share one session.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.schedule_output import (
    Assignment,
    Schedule,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import SchedulingPeriod
from app.services import AuthorizationError, InvalidOperationError
from app.services.schedule_version import (
    ACTION_SCHEDULE_CREATED,
    ACTION_SCHEDULE_VERSION_CREATED,
    _any_version_exists_statement,
    _requirement_snapshot_source_statement,
    _schedule_lookup_statement,
    create_initial_schedule_version,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class ScheduleVersionSession(Session):
    """A real, unbound Session for these tests.

    Like Tasks 14-19's Session subclasses, ``flush()`` is not forbidden --
    this service legitimately flushes up to twice per call, to obtain
    identities for the audit rows that follow. **One shared counter** assigns
    ids across every object type, so a Schedule and a ScheduleVersion created
    in the same call never collide -- the specific hazard the Task 20 brief
    warns about. ``commit()`` and ``rollback()`` remain forbidden -- the
    transaction still belongs to the caller.
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
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


@pytest.fixture
def session() -> ScheduleVersionSession:
    return ScheduleVersionSession()


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


def _membership(
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _period(
    period_id: int,
    *,
    ministry: Ministry,
    name: str = "Setup Q4 2026",
    locked_at: datetime.datetime | None = datetime.datetime(
        2026, 9, 1, tzinfo=datetime.timezone.utc
    ),
) -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=name,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    period.availability_locked_at = locked_at
    return period


def _schedule(schedule_id: int, *, period: SchedulingPeriod) -> Schedule:
    schedule = Schedule(scheduling_period_id=period.id)
    schedule.id = schedule_id
    return schedule


def _snapshot_row(
    *, event_id: int, event_date: datetime.date, ministry_role_id: int,
    scheduling_period_id: int, ministry_id: int, required_count: int,
) -> SimpleNamespace:
    """Stands in for one row of the raw ``Select`` result the snapshot source
    query returns -- attribute access only, exactly what a SQLAlchemy ``Row``
    supports for a multi-column ``select()``."""
    return SimpleNamespace(
        event_id=event_id, event_date=event_date, ministry_role_id=ministry_role_id,
        scheduling_period_id=scheduling_period_id, ministry_id=ministry_id,
        required_count=required_count,
    )


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


@pytest.fixture
def locked_period(setup_ministry: Ministry) -> SchedulingPeriod:
    return _period(700, ministry=setup_ministry)


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _new_of_type(session: Session, cls: type) -> list:
    return [obj for obj in session.new if isinstance(obj, cls)]


def _stub_schedule_lookup(monkeypatch, existing: Schedule | None) -> None:
    import app.services.schedule_version as module

    monkeypatch.setattr(
        module, "_find_existing_schedule",
        lambda session, *, scheduling_period_id: existing,
    )


def _stub_version_exists(monkeypatch, exists: bool) -> None:
    import app.services.schedule_version as module

    monkeypatch.setattr(
        module, "_schedule_has_any_version",
        lambda session, *, schedule_id: exists,
    )


def _stub_snapshot_source(monkeypatch, rows: list) -> None:
    import app.services.schedule_version as module

    monkeypatch.setattr(
        module, "_current_requirement_snapshot_source",
        lambda session, *, scheduling_period_id: rows,
    )


def _stub_all(monkeypatch, *, schedule=None, version_exists=False, snapshot_rows=()):
    _stub_schedule_lookup(monkeypatch, schedule)
    _stub_version_exists(monkeypatch, version_exists)
    _stub_snapshot_source(monkeypatch, list(snapshot_rows))


# --------------------------------------------------------------------------
# 1-6 -- Authorization / lifecycle gates
# --------------------------------------------------------------------------


def test_admin_can_create_initial_version(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert isinstance(version, ScheduleVersion)
    assert version.version_number == 1


def test_own_ministry_head_can_create_initial_version(session, setup_ministry, locked_period, monkeypatch):
    _stub_all(monkeypatch)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=setup_ministry, is_head=True)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.version_number == 1


def test_head_of_another_ministry_is_rejected(session, locked_period, monkeypatch):
    _stub_all(monkeypatch)
    av_ministry = _ministry(4, "AV")
    av_head = _person(9, "AV Head")
    _membership(201, person=av_head, ministry=av_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        create_initial_schedule_version(session, actor=av_head, period=locked_period)

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_normal_user_is_rejected(session, setup_ministry, locked_period, monkeypatch):
    _stub_all(monkeypatch)
    ordinary = _person(7, "Ada")
    _membership(202, person=ordinary, ministry=setup_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        create_initial_schedule_version(session, actor=ordinary, period=locked_period)

    assert _audit_rows(session) == []


def test_deactivated_actor_is_rejected(session, locked_period, monkeypatch):
    _stub_all(monkeypatch)
    departed = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        create_initial_schedule_version(session, actor=departed, period=locked_period)

    assert _audit_rows(session) == []


def test_unlocked_period_is_rejected_before_mutation(session, head, setup_ministry, monkeypatch):
    _stub_all(monkeypatch)
    open_period = _period(700, ministry=setup_ministry, locked_at=None)

    with pytest.raises(InvalidOperationError):
        create_initial_schedule_version(session, actor=head, period=open_period)

    assert _audit_rows(session) == []
    assert session.flush_calls == 0
    assert _new_of_type(session, Schedule) == []
    assert _new_of_type(session, ScheduleVersion) == []


# --------------------------------------------------------------------------
# 7-11 -- Schedule create / reuse behavior
# --------------------------------------------------------------------------


def test_no_existing_schedule_creates_one(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    new_schedules = _new_of_type(session, Schedule)
    assert len(new_schedules) == 1
    assert new_schedules[0].scheduling_period_id == 700
    assert version.schedule_id == new_schedules[0].id


def test_existing_schedule_is_reused(session, head, locked_period, monkeypatch):
    existing_schedule = _schedule(500, period=locked_period)
    _stub_all(monkeypatch, schedule=existing_schedule, version_exists=False)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.schedule_id == 500
    assert _new_of_type(session, Schedule) == []  # no second Schedule object


def test_never_creates_a_second_schedule_for_the_same_period(session, head, locked_period, monkeypatch):
    existing_schedule = _schedule(500, period=locked_period)
    _stub_all(monkeypatch, schedule=existing_schedule, version_exists=False)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    assert _new_of_type(session, Schedule) == []


def test_existing_schedule_with_any_version_is_rejected(session, head, locked_period, monkeypatch):
    existing_schedule = _schedule(500, period=locked_period)
    _stub_all(monkeypatch, schedule=existing_schedule, version_exists=True)

    with pytest.raises(InvalidOperationError):
        create_initial_schedule_version(session, actor=head, period=locked_period)

    assert _audit_rows(session) == []
    assert _new_of_type(session, ScheduleVersion) == []
    assert session.flush_calls == 0


def test_does_not_create_version_2(session, head, locked_period, monkeypatch):
    existing_schedule = _schedule(500, period=locked_period)
    _stub_all(monkeypatch, schedule=existing_schedule, version_exists=True)

    with pytest.raises(InvalidOperationError):
        create_initial_schedule_version(session, actor=head, period=locked_period)

    assert _new_of_type(session, ScheduleVersion) == []


# --------------------------------------------------------------------------
# 12-18 -- Version fields
# --------------------------------------------------------------------------


def test_version_number_is_1(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.version_number == 1


def test_status_is_draft(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.status == "DRAFT"


def test_finalized_at_is_none(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.finalized_at is None


def test_amends_version_id_is_none(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.amends_version_id is None


def test_amendment_reason_is_none(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.amendment_reason is None


def test_notes_are_stored_when_supplied(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(
        session, actor=head, period=locked_period, notes="Generated ahead of the holiday season."
    )

    assert version.notes == "Generated ahead of the holiday season."


def test_notes_default_to_none(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.notes is None


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
def test_whitespace_only_notes_are_rejected(session, head, locked_period, monkeypatch, blank):
    _stub_all(monkeypatch)

    with pytest.raises(InvalidOperationError):
        create_initial_schedule_version(session, actor=head, period=locked_period, notes=blank)

    assert _audit_rows(session) == []
    assert _new_of_type(session, ScheduleVersion) == []


# --------------------------------------------------------------------------
# 19-27 -- Snapshot copy
# --------------------------------------------------------------------------


def test_one_snapshot_row_per_relevant_staffing_requirement(session, head, locked_period, monkeypatch):
    rows = [
        _snapshot_row(
            event_id=100 + i, event_date=datetime.date(2026, 10, 4 + 7 * i),
            ministry_role_id=12, scheduling_period_id=700, ministry_id=3,
            required_count=1,
        )
        for i in range(3)
    ]
    _stub_all(monkeypatch, snapshot_rows=rows)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    snapshots = _new_of_type(session, ScheduleVersionRequirement)
    assert len(snapshots) == 3


def test_event_date_is_copied_from_the_source_row(session, head, locked_period, monkeypatch):
    rows = [_snapshot_row(
        event_id=100, event_date=datetime.date(2026, 11, 15), ministry_role_id=12,
        scheduling_period_id=700, ministry_id=3, required_count=1,
    )]
    _stub_all(monkeypatch, snapshot_rows=rows)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    snapshot = _new_of_type(session, ScheduleVersionRequirement)[0]
    assert snapshot.event_date == datetime.date(2026, 11, 15)


def test_required_count_is_copied(session, head, locked_period, monkeypatch):
    rows = [_snapshot_row(
        event_id=100, event_date=datetime.date(2026, 10, 4), ministry_role_id=12,
        scheduling_period_id=700, ministry_id=3, required_count=5,
    )]
    _stub_all(monkeypatch, snapshot_rows=rows)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    snapshot = _new_of_type(session, ScheduleVersionRequirement)[0]
    assert snapshot.required_count == 5


def test_role_event_period_ministry_ids_are_correct(session, head, locked_period, monkeypatch):
    rows = [_snapshot_row(
        event_id=101, event_date=datetime.date(2026, 10, 4), ministry_role_id=12,
        scheduling_period_id=700, ministry_id=3, required_count=2,
    )]
    _stub_all(monkeypatch, snapshot_rows=rows)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    snapshot = _new_of_type(session, ScheduleVersionRequirement)[0]
    assert snapshot.schedule_version_id == version.id
    assert snapshot.event_id == 101
    assert snapshot.ministry_role_id == 12
    assert snapshot.scheduling_period_id == 700
    assert snapshot.ministry_id == 3


def test_zero_requirement_period_still_creates_version_1_with_zero_snapshots(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch, snapshot_rows=[])

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert version.version_number == 1
    assert _new_of_type(session, ScheduleVersionRequirement) == []


def test_no_staffing_requirement_reference_is_invented(session, head, locked_period, monkeypatch):
    rows = [_snapshot_row(
        event_id=100, event_date=datetime.date(2026, 10, 4), ministry_role_id=12,
        scheduling_period_id=700, ministry_id=3, required_count=1,
    )]
    _stub_all(monkeypatch, snapshot_rows=rows)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    snapshot = _new_of_type(session, ScheduleVersionRequirement)[0]
    assert not hasattr(snapshot, "staffing_requirement_id")


def test_no_assignment_rows_are_created(session, head, locked_period, monkeypatch):
    rows = [_snapshot_row(
        event_id=100, event_date=datetime.date(2026, 10, 4), ministry_role_id=12,
        scheduling_period_id=700, ministry_id=3, required_count=1,
    )]
    _stub_all(monkeypatch, snapshot_rows=rows)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    assert _new_of_type(session, Assignment) == []


# --------------------------------------------------------------------------
# 28-35 -- Audit
# --------------------------------------------------------------------------


def test_new_schedule_gets_schedule_created_audit(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    actions = [a.action for a in _audit_rows(session)]
    assert ACTION_SCHEDULE_CREATED in actions


def test_schedule_created_audit_fields_are_correct(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    schedule_audit = next(a for a in _audit_rows(session) if a.action == ACTION_SCHEDULE_CREATED)
    new_schedule = _new_of_type(session, Schedule)[0]
    assert schedule_audit.target_table == "schedule"
    assert schedule_audit.target_id == new_schedule.id
    assert schedule_audit.ministry_id == 3
    assert schedule_audit.summary == "Created schedule for Setup Q4 2026"
    assert schedule_audit.before_values is None
    assert schedule_audit.after_values == {"scheduling_period_id": 700}


def test_reused_schedule_does_not_get_a_duplicate_schedule_created_audit(session, head, locked_period, monkeypatch):
    existing_schedule = _schedule(500, period=locked_period)
    _stub_all(monkeypatch, schedule=existing_schedule, version_exists=False)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    actions = [a.action for a in _audit_rows(session)]
    assert ACTION_SCHEDULE_CREATED not in actions
    assert actions.count(ACTION_SCHEDULE_VERSION_CREATED) == 1


def test_version_gets_schedule_version_created_audit(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    version_audit = next(a for a in _audit_rows(session) if a.action == ACTION_SCHEDULE_VERSION_CREATED)
    assert version_audit.target_table == "schedule_version"
    assert version_audit.target_id == version.id
    assert version_audit.ministry_id == 3
    assert version_audit.summary == "Created draft version 1 for Setup Q4 2026"
    assert version_audit.before_values is None


def test_version_audit_includes_correct_snapshot_count(session, head, locked_period, monkeypatch):
    rows = [
        _snapshot_row(
            event_id=100 + i, event_date=datetime.date(2026, 10, 4 + 7 * i),
            ministry_role_id=12, scheduling_period_id=700, ministry_id=3,
            required_count=1,
        )
        for i in range(4)
    ]
    _stub_all(monkeypatch, snapshot_rows=rows)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    version_audit = next(a for a in _audit_rows(session) if a.action == ACTION_SCHEDULE_VERSION_CREATED)
    assert version_audit.after_values == {
        "schedule_id": version.schedule_id,
        "scheduling_period_id": 700,
        "version_number": 1,
        "status": "DRAFT",
        "notes": None,
        "requirement_snapshot_count": 4,
    }


def test_no_per_requirement_audit_events_are_created(session, head, locked_period, monkeypatch):
    rows = [
        _snapshot_row(
            event_id=100 + i, event_date=datetime.date(2026, 10, 4), ministry_role_id=12,
            scheduling_period_id=700, ministry_id=3, required_count=1,
        )
        for i in range(65)  # a full realistic period's worth
    ]
    _stub_all(monkeypatch, snapshot_rows=rows)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    # Exactly two audit rows total (Schedule + Version), never one per snapshot.
    assert len(_audit_rows(session)) == 2


def test_audit_target_ids_are_the_real_post_flush_ids(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    schedule_audit = next(a for a in _audit_rows(session) if a.action == ACTION_SCHEDULE_CREATED)
    version_audit = next(a for a in _audit_rows(session) if a.action == ACTION_SCHEDULE_VERSION_CREATED)
    assert schedule_audit.target_id == version.schedule_id
    assert version_audit.target_id == version.id
    assert schedule_audit.target_id != version_audit.target_id  # never colliding


def test_audit_ministry_id_derives_from_period(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    for audit in _audit_rows(session):
        assert audit.ministry_id == locked_period.ministry_id == 3


def test_notes_are_not_copied_into_audit_event_reason(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    create_initial_schedule_version(
        session, actor=head, period=locked_period, notes="Generated ahead of schedule."
    )

    for audit in _audit_rows(session):
        assert audit.reason is None


# --------------------------------------------------------------------------
# 36-40 -- Transaction / flush discipline
# --------------------------------------------------------------------------


def test_never_commits(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    assert session.commit_calls == 0


def test_never_rolls_back(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    assert session.rollback_calls == 0


def test_same_session_owns_everything(session, head, locked_period, monkeypatch):
    rows = [_snapshot_row(
        event_id=100, event_date=datetime.date(2026, 10, 4), ministry_role_id=12,
        scheduling_period_id=700, ministry_id=3, required_count=1,
    )]
    _stub_all(monkeypatch, snapshot_rows=rows)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    assert Session.object_session(version) is session
    for obj in _new_of_type(session, Schedule) + _audit_rows(session) + _new_of_type(session, ScheduleVersionRequirement):
        assert Session.object_session(obj) is session


def test_new_schedule_and_new_version_use_exactly_two_flushes(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    assert session.flush_calls == 2


def test_reused_schedule_uses_exactly_one_flush(session, head, locked_period, monkeypatch):
    existing_schedule = _schedule(500, period=locked_period)
    _stub_all(monkeypatch, schedule=existing_schedule, version_exists=False)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    assert session.flush_calls == 1


def test_no_flush_per_snapshot_row(session, head, locked_period, monkeypatch):
    rows = [
        _snapshot_row(
            event_id=100 + i, event_date=datetime.date(2026, 10, 4), ministry_role_id=12,
            scheduling_period_id=700, ministry_id=3, required_count=1,
        )
        for i in range(65)
    ]
    _stub_all(monkeypatch, snapshot_rows=rows)

    create_initial_schedule_version(session, actor=head, period=locked_period)

    # Still only the (at most) two identity flushes -- Schedule and Version --
    # regardless of how many snapshot rows were created.
    assert session.flush_calls == 2


def test_schedule_and_version_ids_never_collide(session, head, locked_period, monkeypatch):
    _stub_all(monkeypatch)

    version = create_initial_schedule_version(session, actor=head, period=locked_period)

    new_schedule = _new_of_type(session, Schedule)[0]
    assert new_schedule.id != version.id


# --------------------------------------------------------------------------
# 41-44 -- Query statements, tested without any Session or database at all
# --------------------------------------------------------------------------


def test_schedule_lookup_statement_targets_scheduling_period_id():
    stmt = _schedule_lookup_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "FROM schedule" in compiled
    assert "schedule.scheduling_period_id = 700" in compiled


def test_version_exists_statement_targets_schedule_id():
    stmt = _any_version_exists_statement(500)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "FROM schedule_version" in compiled
    assert "schedule_version.schedule_id = 500" in compiled
    assert "LIMIT 1" in compiled.upper()


def test_snapshot_statement_restricts_to_the_target_period():
    stmt = _requirement_snapshot_source_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "event.scheduling_period_id = 700" in compiled


def test_snapshot_statement_excludes_cancelled_events():
    stmt = _requirement_snapshot_source_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "event.cancelled_at IS NULL" in compiled


def test_snapshot_statement_joins_staffing_requirement_to_event():
    stmt = _requirement_snapshot_source_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "FROM staffing_requirement JOIN event" in compiled
    assert "event.id = staffing_requirement.event_id" in compiled


def test_snapshot_statement_selects_ministry_id_from_staffing_requirement_not_event():
    """The reviewed design selects sr.ministry_id, not e.ministry_id -- both
    are guaranteed equal by StaffingRequirement's own composite FK, and the
    reviewed SQL is explicit about which one it reads."""
    stmt = _requirement_snapshot_source_statement(700)
    compiled = str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )

    assert "staffing_requirement.ministry_id" in compiled
    assert "event.ministry_id" not in compiled
