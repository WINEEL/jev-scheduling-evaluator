"""ScheduleVersion requirement-snapshot staleness tests (schedule-output §13).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Task 21's precedent for a read-only query:

- **Each query's SQL** is compiled against the PostgreSQL dialect with literal
  binds and inspected as text. That is where this task's load-bearing claims
  are actually proven -- period scoping, cancelled-event exclusion, *which*
  ``event_date`` column each side reads, and the deliberate absence of a
  ``staffing_requirement.id``, an ``event`` join, or a ``cancelled_at``
  predicate on the snapshot side.
- **Result semantics** are exercised through the public function with both
  fetch helpers monkeypatched to return fixed fingerprint sets, so the exact
  set comparison is tested on its own, without inventing fake row execution.
- **Session discipline** is checked against a real, unbound ``Session``
  subclass that raises on ``add``/``delete``/``flush``/``commit``/``rollback``
  -- nothing is permitted, because this is a read-only query -- and the fakes
  record which session object they were handed, proving both queries use the
  one the caller supplied.

Honest limitation, as in every earlier task: this proves what the SQL *says*
and how the comparison behaves, not that PostgreSQL returns those rows.

The two regression guards §13 specifically warns about are covered explicitly:

- **BUG A** -- using the current ``event.event_date`` on *both* sides, which
  would let a version whose event has moved look fresh.
- **BUG B** -- filtering snapshot rows by the current ``event.cancelled_at``,
  which would make a cancelled event's rows vanish from both sides and report
  fresh.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    ScheduleVersion,
)
from app.services.errors import InvalidOperationError
from app.services.schedule_staleness import (
    RequirementFingerprint,
    ScheduleVersionStalenessResult,
    _current_requirements_statement,
    _fingerprints,
    _snapshot_requirements_statement,
    get_schedule_version_staleness,
)

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

OCT_4 = datetime.date(2026, 10, 4)
OCT_11 = datetime.date(2026, 10, 11)


class StalenessSession(Session):
    """A real, unbound Session that fails loudly on any mutation method.

    This is a read-only query, so ``add``, ``delete`` and ``flush`` join
    ``commit``/``rollback`` on the forbidden list (Task 21's precedent).
    """

    def __init__(self) -> None:
        super().__init__()
        self.add_calls = 0
        self.delete_calls = 0
        self.flush_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

    def add(self, instance, _warn=True) -> None:  # pragma: no cover - must never run
        self.add_calls += 1
        raise AssertionError("a read-only query must never add")

    def delete(self, instance) -> None:  # pragma: no cover - must never run
        self.delete_calls += 1
        raise AssertionError("a read-only query must never delete")

    def flush(self, objects=None) -> None:  # pragma: no cover - must never run
        self.flush_calls += 1
        raise AssertionError("a read-only query must never flush")

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a read-only query must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a read-only query must never roll back")


@pytest.fixture
def session() -> StalenessSession:
    return StalenessSession()


def _version(
    version_id: int = 500,
    *,
    scheduling_period_id: int = 11,
    status: str = SCHEDULE_VERSION_STATUS_DRAFT,
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=400,
        scheduling_period_id=scheduling_period_id,
        version_number=1,
        status=status,
    )
    version.id = version_id
    return version


@pytest.fixture
def draft_version() -> ScheduleVersion:
    return _version()


def _fp(
    *, event_id: int = 700, event_date: datetime.date = OCT_4,
    ministry_role_id: int = 12, required_count: int = 1,
) -> RequirementFingerprint:
    return RequirementFingerprint(
        event_id=event_id, event_date=event_date,
        ministry_role_id=ministry_role_id, required_count=required_count,
    )


def _stub_fetch(monkeypatch, *, current=(), snapshot=()):
    """Replace both execution helpers with fakes returning fixed sets, and
    record the session and keyword arguments each was called with -- which is
    how the "same supplied Session" and scoping-argument claims are proven.
    """
    import app.services.schedule_staleness as module

    calls: list[tuple[str, object, dict]] = []

    def fake_current(session, **kwargs):
        calls.append(("current", session, kwargs))
        return frozenset(current)

    def fake_snapshot(session, **kwargs):
        calls.append(("snapshot", session, kwargs))
        return frozenset(snapshot)

    monkeypatch.setattr(module, "_fetch_current_requirements", fake_current)
    monkeypatch.setattr(module, "_fetch_snapshot_requirements", fake_snapshot)
    return calls


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------
# 1-10 -- Result semantics: the exact set comparison
# --------------------------------------------------------------------------


def test_01_identical_sets_are_fresh(session, draft_version, monkeypatch):
    same = (_fp(event_id=700), _fp(event_id=701, ministry_role_id=13))
    _stub_fetch(monkeypatch, current=same, snapshot=same)

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is False
    assert result.current_only == frozenset()
    assert result.snapshot_only == frozenset()


def test_02_both_sides_empty_is_fresh(session, draft_version, monkeypatch):
    _stub_fetch(monkeypatch, current=(), snapshot=())

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is False
    assert result.current_requirements == frozenset()
    assert result.snapshot_requirements == frozenset()


def test_03_requirement_added_after_snapshot_is_stale_as_current_only(
    session, draft_version, monkeypatch
):
    kept = _fp(event_id=700)
    added = _fp(event_id=700, ministry_role_id=13)
    _stub_fetch(monkeypatch, current=(kept, added), snapshot=(kept,))

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is True
    assert result.current_only == frozenset({added})
    assert result.snapshot_only == frozenset()


def test_04_requirement_removed_after_snapshot_is_stale_as_snapshot_only(
    session, draft_version, monkeypatch
):
    kept = _fp(event_id=700)
    removed = _fp(event_id=700, ministry_role_id=13)
    _stub_fetch(monkeypatch, current=(kept,), snapshot=(kept, removed))

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is True
    assert result.snapshot_only == frozenset({removed})
    assert result.current_only == frozenset()


def test_05_required_count_change_appears_on_both_sides(session, draft_version, monkeypatch):
    old = _fp(required_count=1)
    new = _fp(required_count=2)
    _stub_fetch(monkeypatch, current=(new,), snapshot=(old,))

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is True
    assert result.snapshot_only == frozenset({old})
    assert result.current_only == frozenset({new})


def test_06_event_date_change_appears_on_both_sides(session, draft_version, monkeypatch):
    old = _fp(event_date=OCT_4)
    moved = _fp(event_date=OCT_11)
    _stub_fetch(monkeypatch, current=(moved,), snapshot=(old,))

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is True
    assert result.snapshot_only == frozenset({old})
    assert result.current_only == frozenset({moved})
    # BUG A would produce OCT_11 on both sides and report fresh.
    assert {f.event_date for f in result.snapshot_only} == {OCT_4}
    assert {f.event_date for f in result.current_only} == {OCT_11}


def test_07_cancelled_event_leaves_a_snapshot_only_difference(
    session, draft_version, monkeypatch
):
    # A cancelled event drops out of the current set entirely (the current
    # query excludes it); its snapshot rows stay. BUG B would drop them from
    # both sides and report fresh.
    cancelled_rows = (_fp(event_id=700), _fp(event_id=700, ministry_role_id=13))
    still_active = _fp(event_id=701, event_date=OCT_11)
    _stub_fetch(
        monkeypatch, current=(still_active,), snapshot=cancelled_rows + (still_active,)
    )

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is True
    assert result.snapshot_only == frozenset(cancelled_rows)
    assert result.current_only == frozenset()


def test_08_active_requirement_missing_from_snapshot_is_current_only(
    session, draft_version, monkeypatch
):
    # An event that was cancelled when the snapshot was taken and has since
    # become active again, with a current requirement: present now, absent
    # from the snapshot.
    revived = _fp(event_id=702, event_date=OCT_11)
    _stub_fetch(monkeypatch, current=(revived,), snapshot=())

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is True
    assert result.current_only == frozenset({revived})
    assert result.snapshot_only == frozenset()


def test_09_multiple_differences_are_all_retained(session, draft_version, monkeypatch):
    unchanged = _fp(event_id=700)
    added = _fp(event_id=700, ministry_role_id=14)
    removed = _fp(event_id=701, ministry_role_id=12)
    count_old = _fp(event_id=702, required_count=1)
    count_new = _fp(event_id=702, required_count=5)
    date_old = _fp(event_id=703, event_date=OCT_4)
    date_new = _fp(event_id=703, event_date=OCT_11)
    _stub_fetch(
        monkeypatch,
        current=(unchanged, added, count_new, date_new),
        snapshot=(unchanged, removed, count_old, date_old),
    )

    result = get_schedule_version_staleness(session, version=draft_version)

    assert result.is_stale is True
    assert result.current_only == frozenset({added, count_new, date_new})
    assert result.snapshot_only == frozenset({removed, count_old, date_old})
    # Nothing is collapsed into a "changed requirement" taxonomy: a count
    # change is genuinely one tuple on each side.
    assert len(result.current_only) == 3
    assert len(result.snapshot_only) == 3


def test_10_differences_are_unordered_frozensets(session, draft_version, monkeypatch):
    a = _fp(event_id=700)
    b = _fp(event_id=701)
    c = _fp(event_id=702)
    # Same members, supplied in two different orders -- the results must be
    # equal, so no caller can come to depend on an ordering.
    calls_one = _stub_fetch(monkeypatch, current=(a, b, c), snapshot=(a,))
    first = get_schedule_version_staleness(session, version=draft_version)
    _stub_fetch(monkeypatch, current=(c, a, b), snapshot=(a,))
    second = get_schedule_version_staleness(session, version=draft_version)

    assert isinstance(first.current_only, frozenset)
    assert isinstance(first.snapshot_only, frozenset)
    assert first.current_only == second.current_only
    assert first.current_requirements == second.current_requirements
    assert len(calls_one) == 2


def test_result_and_fingerprint_types_are_frozen():
    fingerprint = _fp()
    result = ScheduleVersionStalenessResult(
        current_requirements=frozenset({fingerprint}),
        snapshot_requirements=frozenset({fingerprint}),
    )

    with pytest.raises(Exception):
        result.current_requirements = frozenset()
    with pytest.raises(Exception):
        fingerprint.required_count = 9


def test_fingerprint_equality_ignores_row_identity():
    # Two rows that are different database rows but the same required
    # position compare equal -- a requirement deleted and recreated
    # identically is deliberately not a change (§13).
    assert _fp() == _fp()
    assert len({_fp(), _fp()}) == 1


# --------------------------------------------------------------------------
# The row -> fingerprint converter, on its own
# --------------------------------------------------------------------------


def _row(event_id, event_date, ministry_role_id, required_count):
    """A row stand-in exposing only the four labels, in a deliberately
    different attribute order from the SELECT lists -- so a converter that
    read positionally rather than by label could not pass."""
    return SimpleNamespace(
        required_count=required_count,
        ministry_role_id=ministry_role_id,
        event_date=event_date,
        event_id=event_id,
    )


def test_fingerprints_are_built_from_the_four_labelled_columns():
    result = _fingerprints([_row(700, OCT_4, 12, 2)])

    assert result == frozenset({_fp(event_id=700, event_date=OCT_4, ministry_role_id=12, required_count=2)})


def test_fingerprints_returns_a_frozenset_and_deduplicates_identical_rows():
    result = _fingerprints([_row(700, OCT_4, 12, 1), _row(700, OCT_4, 12, 1)])

    assert isinstance(result, frozenset)
    assert len(result) == 1


def test_fingerprints_keeps_rows_that_differ_in_any_one_field():
    rows = [
        _row(700, OCT_4, 12, 1),
        _row(701, OCT_4, 12, 1),
        _row(700, OCT_11, 12, 1),
        _row(700, OCT_4, 13, 1),
        _row(700, OCT_4, 12, 2),
    ]

    assert len(_fingerprints(rows)) == 5


def test_fingerprints_of_no_rows_is_the_empty_frozenset():
    assert _fingerprints([]) == frozenset()


# --------------------------------------------------------------------------
# 11-16 -- Current requirement query scope (compiled SQL)
# --------------------------------------------------------------------------


def test_11_current_query_restricts_event_scheduling_period(draft_version):
    compiled = _compile(_current_requirements_statement(11))

    assert "event.scheduling_period_id = 11" in compiled
    # Scoped by period, not by ministry.
    assert "ministry_id" not in compiled


def test_12_current_query_excludes_cancelled_events(draft_version):
    compiled = _compile(_current_requirements_statement(11))

    assert "event.cancelled_at IS NULL" in compiled


def test_13_current_query_reads_the_current_event_date(draft_version):
    compiled = _compile(_current_requirements_statement(11))

    assert "event.event_date AS event_date" in compiled
    # BUG A's mirror image: the current side must not read the snapshot's date.
    assert "schedule_version_requirement" not in compiled


def test_14_current_query_uses_staffing_requirement_ministry_role_id(draft_version):
    compiled = _compile(_current_requirements_statement(11))

    assert "staffing_requirement.ministry_role_id AS ministry_role_id" in compiled


def test_15_current_query_uses_staffing_requirement_required_count(draft_version):
    compiled = _compile(_current_requirements_statement(11))

    assert "staffing_requirement.required_count AS required_count" in compiled


def test_16_current_query_does_not_select_staffing_requirement_id(draft_version):
    compiled = _compile(_current_requirements_statement(11))

    assert "staffing_requirement.id AS" not in compiled
    # event_id is selected from staffing_requirement; the join predicate is
    # the only place staffing_requirement.id could appear, and it does not.
    assert "staffing_requirement.id" not in compiled


def test_current_query_joins_event_on_the_requirement_event_id(draft_version):
    compiled = _compile(_current_requirements_statement(11))

    assert "JOIN event ON event.id = staffing_requirement.event_id" in compiled
    assert "staffing_requirement.event_id AS event_id" in compiled


# --------------------------------------------------------------------------
# 17-21 -- Snapshot query scope (compiled SQL)
# --------------------------------------------------------------------------


def test_17_snapshot_query_restricts_schedule_version_id():
    compiled = _compile(_snapshot_requirements_statement(500))

    assert "schedule_version_requirement.schedule_version_id = 500" in compiled


def test_18_snapshot_query_uses_the_snapshot_event_date():
    compiled = _compile(_snapshot_requirements_statement(500))

    assert "schedule_version_requirement.event_date AS event_date" in compiled


def test_19_snapshot_query_never_reads_the_current_event_date():
    # BUG A: reading event.event_date here would make an old snapshot look
    # fresh the moment its event moved.
    compiled = _compile(_snapshot_requirements_statement(500))

    assert "event.event_date" not in compiled
    assert "JOIN event" not in compiled
    assert " event " not in f" {compiled} "  # no bare `event` table anywhere


def test_20_snapshot_query_does_not_filter_on_cancelled_at():
    # BUG B: filtering the snapshot by the current event's cancellation would
    # make both sides drop the rows and falsely report fresh.
    compiled = _compile(_snapshot_requirements_statement(500))

    assert "cancelled_at" not in compiled


def test_21_snapshot_query_does_not_reference_staffing_requirement():
    compiled = _compile(_snapshot_requirements_statement(500))

    assert "staffing_requirement" not in compiled


def test_snapshot_query_selects_exactly_the_four_compared_columns():
    compiled = _compile(_snapshot_requirements_statement(500))

    for column in ("event_id", "event_date", "ministry_role_id", "required_count"):
        assert f"schedule_version_requirement.{column} AS {column}" in compiled
    for absent in ("ministry_id", "scheduling_period_id", "created_at"):
        assert f"schedule_version_requirement.{absent}" not in compiled


def test_both_statements_label_their_columns_identically():
    # One converter builds fingerprints from both sides, reading by label --
    # so the labels must actually match.
    current = _compile(_current_requirements_statement(11))
    snapshot = _compile(_snapshot_requirements_statement(500))

    for label in ("event_id", "event_date", "ministry_role_id", "required_count"):
        assert f"AS {label}" in current
        assert f"AS {label}" in snapshot


# --------------------------------------------------------------------------
# 22-24 -- Isolation
# --------------------------------------------------------------------------


def test_22_another_period_is_excluded_by_the_current_query_scope(session, monkeypatch):
    version = _version(500, scheduling_period_id=11)
    calls = _stub_fetch(monkeypatch)

    get_schedule_version_staleness(session, version=version)

    current_call = next(c for c in calls if c[0] == "current")
    assert current_call[2] == {"scheduling_period_id": 11}
    # And the SQL that argument feeds is period-scoped, so another period's
    # requirements cannot enter the current set.
    assert "event.scheduling_period_id = 11" in _compile(_current_requirements_statement(11))
    assert "event.scheduling_period_id = 12" not in _compile(_current_requirements_statement(11))


def test_23_another_version_is_excluded_by_the_snapshot_query_scope(session, monkeypatch):
    version = _version(500)
    calls = _stub_fetch(monkeypatch)

    get_schedule_version_staleness(session, version=version)

    snapshot_call = next(c for c in calls if c[0] == "snapshot")
    assert snapshot_call[2] == {"schedule_version_id": 500}
    compiled = _compile(_snapshot_requirements_statement(500))
    assert "schedule_version_requirement.schedule_version_id = 500" in compiled
    assert "schedule_version_id = 501" not in compiled


@pytest.mark.parametrize(
    "status",
    [
        SCHEDULE_VERSION_STATUS_DRAFT,
        SCHEDULE_VERSION_STATUS_REVIEW,
        SCHEDULE_VERSION_STATUS_FINALIZED,
    ],
)
def test_24_version_status_does_not_alter_the_computation(session, monkeypatch, status):
    version = _version(500, status=status)
    snapshot = (_fp(required_count=1),)
    current = (_fp(required_count=2),)
    calls = _stub_fetch(monkeypatch, current=current, snapshot=snapshot)

    result = get_schedule_version_staleness(session, version=version)

    assert result.is_stale is True
    assert result.snapshot_only == frozenset(snapshot)
    assert result.current_only == frozenset(current)
    # Same two queries, same arguments, whatever the status.
    assert [c[0] for c in calls] == ["current", "snapshot"]
    assert calls[0][2] == {"scheduling_period_id": 11}
    assert calls[1][2] == {"schedule_version_id": 500}
    # Purely descriptive: a stale version is not touched.
    assert version.status == status


def test_unpersisted_version_is_rejected_rather_than_falsely_fresh(session, monkeypatch):
    _stub_fetch(monkeypatch)
    version = _version(500)
    version.id = None

    with pytest.raises(InvalidOperationError):
        get_schedule_version_staleness(session, version=version)


def test_version_without_a_period_is_rejected(session, monkeypatch):
    _stub_fetch(monkeypatch)
    version = _version(500)
    version.scheduling_period_id = None

    with pytest.raises(InvalidOperationError):
        get_schedule_version_staleness(session, version=version)


# --------------------------------------------------------------------------
# 25-31 -- Read-only / session discipline
# --------------------------------------------------------------------------


def test_25_both_queries_use_the_one_supplied_session(session, draft_version, monkeypatch):
    calls = _stub_fetch(monkeypatch, current=(_fp(),), snapshot=(_fp(),))

    get_schedule_version_staleness(session, version=draft_version)

    assert len(calls) == 2
    assert [c[1] for c in calls] == [session, session]


def test_26_never_adds(session, draft_version, monkeypatch):
    _stub_fetch(monkeypatch)
    get_schedule_version_staleness(session, version=draft_version)
    assert session.add_calls == 0


def test_27_never_deletes(session, draft_version, monkeypatch):
    _stub_fetch(monkeypatch)
    get_schedule_version_staleness(session, version=draft_version)
    assert session.delete_calls == 0


def test_28_never_flushes(session, draft_version, monkeypatch):
    _stub_fetch(monkeypatch)
    get_schedule_version_staleness(session, version=draft_version)
    assert session.flush_calls == 0


def test_29_never_commits(session, draft_version, monkeypatch):
    _stub_fetch(monkeypatch)
    get_schedule_version_staleness(session, version=draft_version)
    assert session.commit_calls == 0


def test_30_never_rolls_back(session, draft_version, monkeypatch):
    _stub_fetch(monkeypatch)
    get_schedule_version_staleness(session, version=draft_version)
    assert session.rollback_calls == 0


def test_31_creates_no_audit_event_and_nothing_pending(session, draft_version, monkeypatch):
    from app.models.audit import AuditEvent

    _stub_fetch(monkeypatch, current=(_fp(),), snapshot=())

    get_schedule_version_staleness(session, version=draft_version)

    assert [obj for obj in session.new if isinstance(obj, AuditEvent)] == []
    assert len(session.new) == 0
    assert len(session.dirty) == 0
    assert len(session.deleted) == 0


def test_module_imports_no_audit_vocabulary():
    import app.services.schedule_staleness as module

    assert not hasattr(module, "record_audit_event")
    assert not [name for name in vars(module) if name.startswith("ACTION_")]


# --------------------------------------------------------------------------
# Person-specific constraints are deliberately NOT part of staleness
# (Task 47's serving maximum; Task 50's linked-pair same-date exclusion)
# --------------------------------------------------------------------------


def test_the_fingerprint_covers_staffing_configuration_only():
    """Staleness answers one question: *does today's staffing configuration
    still match what this version was built against?*

    It compares four requirement fields against an immutable snapshot, and the
    snapshot contains nothing about candidates. That is why a revoked
    qualification, an availability answer changed after the fact, a
    deactivated person, a changed serving maximum (Task 47) and a newly
    configured linked-pair same-date exclusion (Task 50) are all invisible
    here: none of them is staffing configuration, and none is snapshotted.

    Those candidate-level facts are re-evaluated against **current** state by
    :mod:`app.services.finalization_readiness`, which is the mechanism this
    architecture uses for exactly that class of change. A serving maximum
    lowered below a version's assignment count blocks finalization
    (``EXCEEDS_SERVING_LIMIT``); a pair exclusion added after both members were
    assigned to one date blocks it too
    (``SAME_DATE_LINKED_MEMBER_CONFLICT``). Neither marks the version stale,
    and neither deletes an assignment.

    This test exists so that boundary is a recorded decision rather than an
    unnoticed gap: adding candidate state to the fingerprint would need a new
    snapshot table and would make these the only candidate-level facts that
    are snapshotted, which is a larger architectural change than either task
    was scoped for. Task 50 re-checked the architecture rather than assuming
    it, and found it unchanged since Task 47 -- which is why it invented no
    one-off snapshot of its own.
    """
    assert set(RequirementFingerprint.__dataclass_fields__) == {
        "event_id",
        "event_date",
        "ministry_role_id",
        "required_count",
    }


def test_the_requirement_snapshot_stores_no_candidate_state():
    """The other half of the same boundary, asserted on the schema.

    If a future task adds candidate state to this snapshot, this test should
    fail and the decision above should be revisited deliberately.
    """
    import app.models  # noqa: F401 -- registers every mapped class
    from app.db import Base

    columns = set(Base.metadata.tables["schedule_version_requirement"].columns.keys())

    for candidate_column in (
        "ministry_membership_id", "person_id", "max_assignments",
        "availability_state", "membership_a_id", "membership_b_id",
    ):
        assert candidate_column not in columns


def test_a_configured_pair_exclusion_is_not_snapshotted_anywhere(monkeypatch):
    """Task 50 added no snapshot of its own, deliberately.

    A pair exclusion belongs to (membership pair x SchedulingPeriod) and is
    read **current** everywhere it matters -- by the input builder at
    generation time, by manual assignment, and by finalization readiness. A
    version-scoped copy would be a second representation of the same rule, free
    to disagree with the one a head actually edits, and it would make pair
    rules the only candidate-level fact this project snapshots.
    """
    import app.models  # noqa: F401 -- registers every mapped class
    from app.db import Base

    exclusion = Base.metadata.tables["membership_same_date_exclusion"]
    # Scoped to the period, and to nothing narrower: no schedule_version_id
    # column exists for a snapshot to hang off.
    assert "schedule_version_id" not in exclusion.columns
    assert "scheduling_period_id" in exclusion.columns
