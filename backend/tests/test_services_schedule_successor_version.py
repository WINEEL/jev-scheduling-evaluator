"""Successor ScheduleVersion creation tests (schedule-output §8, §13, §14).

Offline: no PostgreSQL, no Neon, no network.

A separate module from ``test_services_schedule_version.py`` (Task 20's
initial-version tests) so that suite stays exactly as it was; both exercise the
same service module through their own operation's front door.

**Test strategy** follows Tasks 20-27's precedent: the module's own read
helpers (``_resolve_source_period``, ``_newer_version_exists``,
``_current_requirement_snapshot_source``) are monkeypatched and record the
session they were handed, while the real ORM objects are built so the rows this
service constructs are genuine ``ScheduleVersion`` /
``ScheduleVersionRequirement`` instances. Session discipline uses a real,
unbound ``Session`` subclass that counts flushes and forbids
commit/rollback/delete.

What only a real database can show -- that the successor's snapshot really
diverges from its source's, and that authority does not move until the
successor is finalized -- is proven in
``tests/integration/test_pg_successor_version.py``.
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
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import SchedulingPeriod
from app.services.audit import ACTION_SCHEDULE_VERSION_CREATED
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.schedule_version import (
    _newer_version_exists_statement,
    _scheduling_period_lookup_statement,
    create_successor_schedule_version,
)

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)
FINALIZED_AT = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


class SuccessorSession(Session):
    """A real, unbound Session. ``flush()`` is permitted (this service needs
    exactly one, for the new version's identity) and counted; ``commit``,
    ``rollback`` and ``delete`` are forbidden.
    """

    def __init__(self, *, next_id: int = 900) -> None:
        super().__init__(autoflush=False)
        self.commit_calls = 0
        self.rollback_calls = 0
        self.delete_calls = 0
        self.flush_calls = 0
        self.flushed_batches: list = []
        self._next_id = next_id

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def delete(self, instance) -> None:  # pragma: no cover - must never run
        self.delete_calls += 1
        raise AssertionError("this operation must never delete")

    def flush(self, objects=None) -> None:
        self.flush_calls += 1
        self.flushed_batches.append(list(objects) if objects is not None else None)
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


@pytest.fixture
def session() -> SuccessorSession:
    return SuccessorSession()


def _person(person_id: int, name: str, *, is_admin: bool = False, deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    return person


def _ministry(ministry_id: int, name: str) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


def _actor_membership(
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _period(period_id: int, *, ministry: Ministry, name: str = "Q4 2026") -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=name,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    period.ministry = ministry
    return period


def _version(
    version_id: int = 500, *, schedule_id: int = 400, scheduling_period_id: int = 11,
    version_number: int = 1, status: str = SCHEDULE_VERSION_STATUS_FINALIZED,
    finalized_at: datetime.datetime | None = FINALIZED_AT, notes: str | None = None,
    amends_version_id: int | None = None, amendment_reason: str | None = None,
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=schedule_id, scheduling_period_id=scheduling_period_id,
        version_number=version_number, status=status,
        finalized_at=finalized_at if status == SCHEDULE_VERSION_STATUS_FINALIZED else None,
        notes=notes, amends_version_id=amends_version_id,
        amendment_reason=amendment_reason,
    )
    version.id = version_id
    return version


def _current_row(
    *, event_id: int = 700, event_date: datetime.date = NOV_15, ministry_role_id: int = 12,
    scheduling_period_id: int = 11, ministry_id: int = 3, required_count: int = 1,
) -> SimpleNamespace:
    """One row as ``_current_requirement_snapshot_source`` returns it -- the
    join of current StaffingRequirement to its non-cancelled Event.
    """
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
    _actor_membership(9001, person=person, ministry=setup_ministry, is_head=True)
    return person


@pytest.fixture
def setup_ministry() -> Ministry:
    return _ministry(3, "Setup")


@pytest.fixture
def setup_period(setup_ministry: Ministry) -> SchedulingPeriod:
    return _period(11, ministry=setup_ministry)


@pytest.fixture
def finalized_v1() -> ScheduleVersion:
    return _version()


def _stub(
    monkeypatch, *, period: SchedulingPeriod, newer_version_exists: bool = False,
    current_rows=(),
) -> list[tuple[str, object, dict]]:
    """Patch the three reads, recording each call so call order, argument
    scoping and "same session" claims can be asserted.
    """
    import app.services.schedule_version as module

    calls: list[tuple[str, object, dict]] = []

    def fake_period(session, source_version):
        calls.append(("resolve_period", session, {"source_version": source_version}))
        return period

    def fake_newer(session, *, schedule_id, version_number):
        calls.append(("newer_version_exists", session, {
            "schedule_id": schedule_id, "version_number": version_number,
        }))
        return newer_version_exists

    def fake_current(session, *, scheduling_period_id):
        calls.append(("snapshot_source", session, {
            "scheduling_period_id": scheduling_period_id,
        }))
        return list(current_rows)

    monkeypatch.setattr(module, "_resolve_source_period", fake_period)
    monkeypatch.setattr(module, "_newer_version_exists", fake_newer)
    monkeypatch.setattr(module, "_current_requirement_snapshot_source", fake_current)
    return calls


def _new_versions(session: Session) -> list[ScheduleVersion]:
    return [o for o in session.new if isinstance(o, ScheduleVersion)]


def _snapshot_rows(session: Session) -> list[ScheduleVersionRequirement]:
    return [o for o in session.new if isinstance(o, ScheduleVersionRequirement)]


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [o for o in session.new if isinstance(o, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _snapshot_of(version: ScheduleVersion) -> dict:
    """A comparable view of a ScheduleVersion's business fields."""
    return {
        "status": version.status,
        "finalized_at": version.finalized_at,
        "notes": version.notes,
        "amendment_reason": version.amendment_reason,
        "amends_version_id": version.amends_version_id,
        "version_number": version.version_number,
        "schedule_id": version.schedule_id,
        "scheduling_period_id": version.scheduling_period_id,
    }


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------
# 1-7 -- Authorization / context
# --------------------------------------------------------------------------


def test_01_admin_can_create_a_successor(session, head, setup_period, finalized_v1, monkeypatch):
    _stub(monkeypatch, period=setup_period)

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1,
        amendment_reason="Staffing requirements changed",
    )

    assert isinstance(version, ScheduleVersion)
    assert version.version_number == 2


def test_02_own_ministry_head_can_create_a_successor(
    session, setup_ministry, setup_period, finalized_v1, monkeypatch
):
    head = _person(2, "Head")
    _actor_membership(200, person=head, ministry=setup_ministry, is_head=True)
    _stub(monkeypatch, period=setup_period)

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert version.version_number == 2


def test_03_head_of_another_ministry_is_rejected(
    session, setup_period, finalized_v1, monkeypatch
):
    other_head = _person(5, "AV Head")
    _actor_membership(201, person=other_head, ministry=_ministry(4, "AV"), is_head=True)
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        create_successor_schedule_version(
            session, actor=other_head, source_version=finalized_v1, amendment_reason="No",
        )
    assert _new_versions(session) == []
    assert _audit_rows(session) == []


def test_04_normal_member_is_rejected(
    session, setup_ministry, setup_period, finalized_v1, monkeypatch
):
    ordinary = _person(6, "Ordinary")
    _actor_membership(202, person=ordinary, ministry=setup_ministry, is_head=False)
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        create_successor_schedule_version(
            session, actor=ordinary, source_version=finalized_v1, amendment_reason="No",
        )


def test_05_deactivated_admin_is_rejected(session, setup_period, finalized_v1, monkeypatch):
    former = _person(7, "Former Admin", is_admin=True, deactivated=True)
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        create_successor_schedule_version(
            session, actor=former, source_version=finalized_v1, amendment_reason="No",
        )


def test_06_ministry_is_derived_from_the_persisted_period(session, finalized_v1, monkeypatch):
    """No ministry_id parameter exists; authorization and the audit ministry
    both come from whatever the resolved period says.
    """
    kids = _ministry(9, "Kids")
    kids_period = _period(12, ministry=kids)
    head = _person(8, "Kids Head")
    _actor_membership(300, person=head, ministry=kids, is_head=True)
    _stub(monkeypatch, period=kids_period)

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert _one_audit_row(session).ministry_id == kids.id


@pytest.mark.parametrize(
    "missing", ["id", "schedule_id", "scheduling_period_id", "version_number"]
)
def test_07_insufficient_source_context_is_rejected(session, head, missing):
    source = _version()
    setattr(source, missing, None)

    with pytest.raises(InvalidOperationError):
        create_successor_schedule_version(
            session, actor=head, source_version=source, amendment_reason="Amending",
        )


def test_07b_an_unresolvable_period_is_rejected(session, head, finalized_v1, monkeypatch):
    """The real ``_resolve_source_period`` runs here; only ``session.execute``
    is stubbed to say "no row", which an unbound offline session cannot
    honestly determine on its own.
    """
    class _NoRow:
        def scalar_one_or_none(self):
            return None

    monkeypatch.setattr(session, "execute", lambda stmt: _NoRow())

    with pytest.raises(InvalidOperationError):
        create_successor_schedule_version(
            session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
        )


# --------------------------------------------------------------------------
# 8-15 -- Source: status, latest, and immutability
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW, SCHEDULE_VERSION_STATUS_FINALIZED],
)
def test_08_09_10_any_recognized_source_status_may_spawn_a_successor(
    session, head, setup_period, monkeypatch, status
):
    """A stale DRAFT/REVIEW needs replacing rather than patching; a FINALIZED
    version needs a successor to amend it.
    """
    source = _version(status=status)
    _stub(monkeypatch, period=setup_period)

    version = create_successor_schedule_version(
        session, actor=head, source_version=source, amendment_reason="Amending",
    )

    assert version.status == SCHEDULE_VERSION_STATUS_DRAFT
    assert version.version_number == 2


def test_11_a_superseded_source_is_rejected(session, head, setup_period, finalized_v1, monkeypatch):
    """Branching from an older historical version would fork the schedule."""
    _stub(monkeypatch, period=setup_period, newer_version_exists=True)

    with pytest.raises(InvalidOperationError, match="superseded"):
        create_successor_schedule_version(
            session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
        )
    assert _new_versions(session) == []
    assert _audit_rows(session) == []


def test_12_an_unknown_source_status_is_rejected(session, head, setup_period, monkeypatch):
    weird = _version(status="ARCHIVED")
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError):
        create_successor_schedule_version(
            session, actor=head, source_version=weird, amendment_reason="Amending",
        )
    assert weird.status == "ARCHIVED"


@pytest.mark.parametrize(
    "status",
    [SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW, SCHEDULE_VERSION_STATUS_FINALIZED],
)
def test_13_the_source_version_is_left_completely_unchanged(
    session, head, setup_period, monkeypatch, status
):
    source = _version(status=status, notes="Original notes.")
    before = _snapshot_of(source)
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    create_successor_schedule_version(
        session, actor=head, source_version=source, amendment_reason="Amending",
    )

    assert _snapshot_of(source) == before
    # Nothing marks the source as amended or superseded -- being outnumbered is
    # what makes it historical.
    assert source.amends_version_id is None
    assert source.amendment_reason is None


def test_14_the_sources_snapshot_rows_are_never_touched(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """The new snapshot rows all belong to the new version; not one of them
    points at the source, and none of the source's own rows is read or copied.
    """
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row(), _current_row(event_id=701)])

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    rows = _snapshot_rows(session)
    assert len(rows) == 2
    assert {r.schedule_version_id for r in rows} == {version.id}
    assert finalized_v1.id not in {r.schedule_version_id for r in rows}


def test_15_no_assignment_is_read_created_or_copied(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert [o for o in session.new if isinstance(o, Assignment)] == []


# --------------------------------------------------------------------------
# 16-25 -- The new version's own state
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source_number,expected", [(1, 2), (2, 3), (7, 8)])
def test_16_the_version_number_increments_by_exactly_one(
    session, head, setup_period, monkeypatch, source_number, expected
):
    source = _version(version_number=source_number)
    _stub(monkeypatch, period=setup_period)

    version = create_successor_schedule_version(
        session, actor=head, source_version=source, amendment_reason="Amending",
    )

    assert version.version_number == expected


def test_17_18_19_20_21_the_new_version_carries_the_expected_state(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert version.schedule_id == finalized_v1.schedule_id == 400
    assert version.scheduling_period_id == finalized_v1.scheduling_period_id == 11
    assert version.status == SCHEDULE_VERSION_STATUS_DRAFT == "DRAFT"
    assert version.finalized_at is None
    assert version.amends_version_id == finalized_v1.id == 500


@pytest.mark.parametrize("blank", [None, "", "   ", "\t\n"])
def test_22_a_blank_amendment_reason_is_rejected_before_anything_is_created(
    session, head, setup_period, finalized_v1, monkeypatch, blank
):
    """Mirrors the database's own ``amendment_reason_required`` CHECK, which
    fires whenever ``amends_version_id`` is set.
    """
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError, match="amendment_reason"):
        create_successor_schedule_version(
            session, actor=head, source_version=finalized_v1, amendment_reason=blank,
        )
    assert _new_versions(session) == []
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_23_the_amendment_reason_is_stored_on_the_version(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1,
        amendment_reason="Correcting the October 18 assignment",
    )

    assert version.amendment_reason == "Correcting the October 18 assignment"


@pytest.mark.parametrize("blank", ["", "   "])
def test_24_blank_notes_are_rejected_exactly_as_for_the_initial_version(
    session, head, setup_period, finalized_v1, monkeypatch, blank
):
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError, match="notes"):
        create_successor_schedule_version(
            session, actor=head, source_version=finalized_v1,
            amendment_reason="Amending", notes=blank,
        )


def test_24b_supplied_notes_are_stored_and_omitted_notes_stay_none(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    with_notes = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1,
        amendment_reason="Amending", notes="Second attempt.",
    )
    assert with_notes.notes == "Second attempt."

    session2 = SuccessorSession()
    without = create_successor_schedule_version(
        session2, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )
    assert without.notes is None


def test_25_source_notes_are_never_inherited(session, head, setup_period, monkeypatch):
    """Notes are one head's commentary on one version; carrying them forward
    would attribute them to a version they were not written about.
    """
    source = _version(notes="Notes about version 1.")
    _stub(monkeypatch, period=setup_period)

    version = create_successor_schedule_version(
        session, actor=head, source_version=source, amendment_reason="Amending",
    )

    assert version.notes is None
    assert source.notes == "Notes about version 1."


# --------------------------------------------------------------------------
# 26-34 -- The fresh snapshot
# --------------------------------------------------------------------------


def test_26_the_snapshot_is_read_from_current_requirements_for_this_period(
    session, head, setup_period, finalized_v1, monkeypatch
):
    calls = _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    source_call = next(c for c in calls if c[0] == "snapshot_source")
    assert source_call[2] == {"scheduling_period_id": finalized_v1.scheduling_period_id}


def test_27_a_changed_required_count_is_captured_by_the_successor(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """Version 1's snapshot said 1; current input now says 2. The successor
    captures 2, and version 1 is untouched.
    """
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row(required_count=2)])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    rows = _snapshot_rows(session)
    assert [r.required_count for r in rows] == [2]


def test_28_a_changed_event_date_is_captured_by_the_successor(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row(event_date=NOV_22)])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Event date changed",
    )

    assert [r.event_date for r in _snapshot_rows(session)] == [NOV_22]


def test_29_a_cancelled_events_requirement_is_absent(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """Cancellation is expressed by the row simply not being in the current
    source -- the shared query already excludes cancelled events.
    """
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row(event_id=701)])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Event cancelled",
    )

    assert {r.event_id for r in _snapshot_rows(session)} == {701}


def test_30_31_added_and_removed_requirements_follow_current_state(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(
        monkeypatch, period=setup_period,
        current_rows=[_current_row(ministry_role_id=12), _current_row(ministry_role_id=14)],
    )

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    rows = _snapshot_rows(session)
    assert {r.ministry_role_id for r in rows} == {12, 14}  # 14 added, 13 gone
    assert len(rows) == 2


def test_32_zero_current_requirements_still_creates_the_version(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """Not a completeness rule -- Task 26 owns that at finalization time."""
    _stub(monkeypatch, period=setup_period, current_rows=[])

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert version.version_number == 2
    assert _snapshot_rows(session) == []
    assert _one_audit_row(session).after_values["requirement_snapshot_count"] == 0


def test_33_no_snapshot_row_references_a_staffing_requirement(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """The snapshot is deliberately not foreign-keyed back to the mutable
    input row it was copied from (§8).
    """
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    row = _snapshot_rows(session)[0]
    assert not hasattr(row, "staffing_requirement_id")
    assert "staffing_requirement_id" not in ScheduleVersionRequirement.__table__.c


def test_34_snapshot_rows_are_never_flushed_individually(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(
        monkeypatch, period=setup_period,
        current_rows=[_current_row(event_id=700 + i) for i in range(20)],
    )

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert len(_snapshot_rows(session)) == 20
    assert session.flush_calls == 1  # one, regardless of how many rows


def test_snapshot_rows_carry_the_full_integrity_spine(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    row = _snapshot_rows(session)[0]
    assert row.schedule_version_id == version.id
    assert row.event_id == 700
    assert row.event_date == NOV_15
    assert row.ministry_role_id == 12
    assert row.scheduling_period_id == 11
    assert row.ministry_id == 3
    assert row.required_count == 1


# --------------------------------------------------------------------------
# 35-38 -- No Assignment carry-forward
# --------------------------------------------------------------------------


def test_35_36_37_38_nothing_about_assignments_is_touched(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """Carrying people forward needs current re-validation and its own honest
    audit history -- Task 29's job, not an implicit side effect here.
    """
    import app.services.schedule_version as module

    _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    # No Assignment row was created...
    assert [o for o in session.new if isinstance(o, Assignment)] == []
    # ...and the module cannot reach the machinery that would create one, so
    # no override flag, reason or audit could have been copied either.
    assert not hasattr(module, "assign_member")
    assert not hasattr(module, "Assignment")
    assert not [n for n in vars(module) if "assignment" in n.lower()]
    assert not [n for n in vars(module) if "override" in n.lower()]
    # The only audit row is the version's own.
    assert [a.action for a in _audit_rows(session)] == [ACTION_SCHEDULE_VERSION_CREATED]


# --------------------------------------------------------------------------
# 39-44 -- Audit
# --------------------------------------------------------------------------


def test_39_exactly_one_version_created_audit_event(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """The same action as the initial version -- this *is* a version being
    created; amends_version_id in the payload is what distinguishes them.
    """
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row(), _current_row(event_id=701)])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert len(_audit_rows(session)) == 1
    assert _one_audit_row(session).action == ACTION_SCHEDULE_VERSION_CREATED


def test_40_audit_target_and_ministry_are_correct(
    session, head, setup_ministry, setup_period, finalized_v1, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    audit = _one_audit_row(session)
    assert audit.target_table == "schedule_version"
    assert audit.target_id == version.id
    assert audit.ministry_id == setup_ministry.id == 3
    assert audit.actor_type == "PERSON"
    assert audit.actor_person_id == head.id
    assert audit.actor_label == head.display_name
    assert audit.before_values is None


def test_41_the_after_payload_carries_the_lineage_and_snapshot_count(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(
        monkeypatch, period=setup_period,
        current_rows=[_current_row(), _current_row(event_id=701), _current_row(event_id=702)],
    )

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1,
        amendment_reason="Staffing requirements changed", notes="Second attempt.",
    )

    assert _one_audit_row(session).after_values == {
        "schedule_id": 400,
        "scheduling_period_id": 11,
        "version_number": 2,
        "status": "DRAFT",
        "notes": "Second attempt.",
        "amends_version_id": 500,
        "amendment_reason": "Staffing requirements changed",
        "requirement_snapshot_count": 3,
    }


def test_42_the_amendment_reason_is_not_copied_into_the_audit_reason(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """It is standing state on the version, readable there forever. Copying it
    would present one fact as two and invite them to disagree.
    """
    _stub(monkeypatch, period=setup_period)

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1,
        amendment_reason="Staffing requirements changed",
    )

    audit = _one_audit_row(session)
    assert audit.reason is None
    assert audit.after_values["amendment_reason"] == "Staffing requirements changed"


def test_43_the_summary_uses_the_new_number_ministry_name_and_period_name(
    session, head, setup_ministry, setup_period, monkeypatch
):
    """setup_ministry.name == "Setup", setup_period.name == "Q4 2026" -- the
    period name deliberately does not embed the ministry name (Task 24's
    correction), so both must appear independently.
    """
    source = _version(version_number=1)
    _stub(monkeypatch, period=setup_period)

    create_successor_schedule_version(
        session, actor=head, source_version=source, amendment_reason="Amending",
    )

    summary = _one_audit_row(session).summary
    assert setup_ministry.name in summary
    assert setup_period.name in summary
    assert summary == "Created draft version 2 for Setup Q4 2026"


def test_44_no_per_snapshot_audit_rows_are_written(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(
        monkeypatch, period=setup_period,
        current_rows=[_current_row(event_id=700 + i) for i in range(30)],
    )

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert len(_snapshot_rows(session)) == 30
    assert len(_audit_rows(session)) == 1


# --------------------------------------------------------------------------
# 45-48 -- Transaction and session behavior
# --------------------------------------------------------------------------


def test_45_exactly_one_identity_flush_for_the_new_version(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """No Schedule is created here (it already exists), so unlike initial
    creation there is exactly one flush, scoped to the version alone.
    """
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    version = create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert session.flush_calls == 1
    assert session.flushed_batches == [[version]]


def test_46_47_never_commits_or_rolls_back(
    session, head, setup_period, finalized_v1, monkeypatch
):
    _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert session.commit_calls == 0
    assert session.rollback_calls == 0
    assert session.delete_calls == 0


def test_48_every_read_uses_the_one_supplied_session(
    session, head, setup_period, finalized_v1, monkeypatch
):
    calls = _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    create_successor_schedule_version(
        session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
    )

    assert [c[0] for c in calls] == ["resolve_period", "newer_version_exists", "snapshot_source"]
    assert all(call[1] is session for call in calls)


def test_a_failure_after_the_flush_propagates_without_cleanup(
    session, head, setup_period, finalized_v1, monkeypatch
):
    """The caller's rollback discards the version, its snapshot and its audit
    row together; a hand-rolled cleanup here would be a second, untested code
    path doing what ROLLBACK already does correctly.
    """
    import app.services.schedule_version as module

    _stub(monkeypatch, period=setup_period, current_rows=[_current_row()])

    def boom(*args, **kwargs):
        raise RuntimeError("audit write exploded")

    monkeypatch.setattr(module, "record_audit_event", boom)

    with pytest.raises(RuntimeError, match="audit write exploded"):
        create_successor_schedule_version(
            session, actor=head, source_version=finalized_v1, amendment_reason="Amending",
        )

    # Still pending in the session, deliberately left for the caller to discard.
    assert len(_new_versions(session)) == 1
    assert session.rollback_calls == 0


# --------------------------------------------------------------------------
# 49-50 -- Existing behavior, and what a successor does to its source
# --------------------------------------------------------------------------


def test_49_the_latest_version_query_is_scoped_and_strictly_greater():
    compiled = _compile(_newer_version_exists_statement(400, 3))

    assert "schedule_version.schedule_id = 400" in compiled
    assert "schedule_version.version_number > 3" in compiled
    assert ">= 3" not in compiled
    assert "LIMIT 1" in compiled


def test_49b_the_period_lookup_is_scoped_to_the_given_id():
    compiled = _compile(_scheduling_period_lookup_statement(11))

    assert "scheduling_period.id = 11" in compiled


def test_50_after_a_successor_exists_the_source_reads_as_superseded(
    session, head, setup_period, monkeypatch
):
    """The successor is created by *this* module, but the rule that makes the
    source immutable belongs to Tasks 22/24/27 -- all of which ask the same
    question ("is there a higher version_number for this schedule?"). Here that
    question is asked with the successor present, and the answer flips.
    """
    import app.services.schedule_lifecycle as lifecycle
    from app.services.errors import InvalidOperationError as LifecycleError

    source = _version(status=SCHEDULE_VERSION_STATUS_REVIEW)
    _stub(monkeypatch, period=setup_period)
    create_successor_schedule_version(
        session, actor=head, source_version=source, amendment_reason="Amending",
    )

    # Now ask Task 24/27's own guard, with a newer version present.
    monkeypatch.setattr(
        lifecycle, "_newer_version_exists",
        lambda session, *, schedule_id, version_number: True,
    )
    monkeypatch.setattr(
        lifecycle, "_resolve_scheduling_period", lambda session, version: setup_period,
    )

    with pytest.raises(LifecycleError, match="superseded"):
        lifecycle.submit_schedule_version_for_review(session, actor=head, version=source)
    with pytest.raises(LifecycleError, match="superseded"):
        lifecycle.finalize_schedule_version(session, actor=head, version=source)


def test_49c_initial_version_creation_is_untouched_by_this_task():
    """Its signature and its refusal to make a second version both stand."""
    import inspect

    from app.services.schedule_version import create_initial_schedule_version

    params = inspect.signature(create_initial_schedule_version).parameters
    assert list(params) == ["session", "actor", "period", "notes"]
