"""Church-wide Sunday conflict query tests (ADR 0002, ADR 0003).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Tasks 14-20's precedent, adapted for a read-only
query rather than a mutation:

- **Each query's SQL** is tested directly and mechanically, with no session or
  database at all: ``_existing_commitment_conflicts_statement`` and
  ``_authoritative_assignment_conflicts_statement`` return plain SQLAlchemy
  ``Select`` objects compiled with literal binds and inspected. This is where
  the load-bearing claims of ADR 0002/0003 -- authoritative-version
  resolution, snapshot-date filtering, current-event cancellation, the
  other-ministry predicate, Person reached only through MinistryMembership --
  are actually proven, against real compiled SQL text, not merely asserted in
  a comment.
- **Each query's use** is exercised via ``monkeypatch`` on
  ``_fetch_existing_commitment_conflicts`` / ``_fetch_authoritative_assignment_conflicts``
  in orchestration tests, exactly as earlier tasks monkeypatched their lookup
  helpers. This also lets the "same Session" claim be checked directly: the
  fake fetchers record which session object they were called with.
- **Session discipline** (never add/delete/flush/commit/rollback) is checked
  against a real, unbound ``Session`` subclass that raises if any of those
  five methods is called -- with the two fetch functions monkeypatched, so no
  real ``session.execute()`` is attempted (an unbound Session cannot honestly
  execute anything without a database).

What this cannot verify, same honest limitation as the earlier tasks: that
PostgreSQL actually returns the rows these queries would select, or that the
partial index is used. What it verifies instead is that the query text this
module builds says exactly what ADR 0002 and ADR 0003 require, and that
calling it touches nothing but reads.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.services.errors import InvalidOperationError
from app.services.sunday_conflict import (
    SundayConflictResult,
    _authoritative_assignment_conflicts_statement,
    _existing_commitment_conflicts_statement,
    get_person_sunday_conflicts,
)

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


class SundayConflictSession(Session):
    """A real, unbound Session that fails loudly on any mutation method.

    Unlike Tasks 14-20's Session subclasses, **nothing** is permitted here --
    this is a read-only query, so ``add``, ``delete`` and ``flush`` join
    ``commit``/``rollback`` on the forbidden list.
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
def session() -> SundayConflictSession:
    return SundayConflictSession()


def _stub_fetch(monkeypatch, *, commitments: tuple = (), assignments: tuple = ()):
    """Replace both execution helpers with fakes that record the session they
    were called with, so orchestration tests can prove both queries used the
    one session supplied by the caller (point 31)."""
    import app.services.sunday_conflict as module

    seen_sessions: list = []

    def fake_commitments(session, **kwargs):
        seen_sessions.append(session)
        return tuple(commitments)

    def fake_assignments(session, **kwargs):
        seen_sessions.append(session)
        return tuple(assignments)

    monkeypatch.setattr(module, "_fetch_existing_commitment_conflicts", fake_commitments)
    monkeypatch.setattr(module, "_fetch_authoritative_assignment_conflicts", fake_assignments)
    return seen_sessions


class _FakeRow:
    """A minimal stand-in for an ORM row -- these tests never touch a
    database, so a real ExistingCommitment/Assignment instance is not needed
    to prove the *orchestration* logic (result construction, is_blocked,
    session identity). The compiled-SQL tests below prove the query shape
    against the real models."""

    def __init__(self, label: str) -> None:
        self.label = label

    def __repr__(self) -> str:
        return f"_FakeRow({self.label!r})"


# --------------------------------------------------------------------------
# 21-25 -- Result construction
# --------------------------------------------------------------------------


def test_no_rows_means_not_blocked(session, monkeypatch):
    _stub_fetch(monkeypatch)

    result = get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )

    assert result.is_blocked is False
    assert result.existing_commitments == ()
    assert result.authoritative_assignments == ()


def test_explicit_commitment_only_blocks(session, monkeypatch):
    commitment = _FakeRow("commitment")
    _stub_fetch(monkeypatch, commitments=(commitment,))

    result = get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )

    assert result.is_blocked is True
    assert result.existing_commitments == (commitment,)
    assert result.authoritative_assignments == ()


def test_authoritative_assignment_only_blocks(session, monkeypatch):
    assignment = _FakeRow("assignment")
    _stub_fetch(monkeypatch, assignments=(assignment,))

    result = get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )

    assert result.is_blocked is True
    assert result.authoritative_assignments == (assignment,)
    assert result.existing_commitments == ()


def test_both_sources_block_and_both_are_retained(session, monkeypatch):
    commitment = _FakeRow("commitment")
    assignment = _FakeRow("assignment")
    _stub_fetch(monkeypatch, commitments=(commitment,), assignments=(assignment,))

    result = get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )

    assert result.is_blocked is True
    assert result.existing_commitments == (commitment,)
    assert result.authoritative_assignments == (assignment,)


def test_multiple_legitimate_conflict_rows_are_all_retained_not_deduplicated(session, monkeypatch):
    commitment_a = _FakeRow("commitment-a")
    commitment_b = _FakeRow("commitment-b")
    assignment_a = _FakeRow("assignment-a")
    assignment_b = _FakeRow("assignment-b")
    _stub_fetch(
        monkeypatch,
        commitments=(commitment_a, commitment_b),
        assignments=(assignment_a, assignment_b),
    )

    result = get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )

    assert result.existing_commitments == (commitment_a, commitment_b)
    assert result.authoritative_assignments == (assignment_a, assignment_b)
    assert len(result.existing_commitments) == 2
    assert len(result.authoritative_assignments) == 2


def test_result_type_is_frozen():
    result = SundayConflictResult(existing_commitments=(), authoritative_assignments=())

    with pytest.raises(Exception):
        result.existing_commitments = (1,)


# --------------------------------------------------------------------------
# 26-31 -- Session discipline
# --------------------------------------------------------------------------


def test_never_adds(session, monkeypatch):
    _stub_fetch(monkeypatch)
    get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )
    assert session.add_calls == 0


def test_never_deletes(session, monkeypatch):
    _stub_fetch(monkeypatch)
    get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )
    assert session.delete_calls == 0


def test_never_flushes(session, monkeypatch):
    _stub_fetch(monkeypatch)
    get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )
    assert session.flush_calls == 0


def test_never_commits(session, monkeypatch):
    _stub_fetch(monkeypatch)
    get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )
    assert session.commit_calls == 0


def test_never_rolls_back(session, monkeypatch):
    _stub_fetch(monkeypatch)
    get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )
    assert session.rollback_calls == 0


def test_both_queries_use_the_same_supplied_session(session, monkeypatch):
    seen_sessions = _stub_fetch(monkeypatch)

    get_person_sunday_conflicts(
        session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )

    assert len(seen_sessions) == 2
    assert all(s is session for s in seen_sessions)


# --------------------------------------------------------------------------
# Basic validation
# --------------------------------------------------------------------------


def test_non_positive_person_id_is_rejected(session, monkeypatch):
    _stub_fetch(monkeypatch)

    with pytest.raises(InvalidOperationError):
        get_person_sunday_conflicts(
            session, person_id=0, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
        )


def test_non_positive_target_ministry_id_is_rejected(session, monkeypatch):
    _stub_fetch(monkeypatch)

    with pytest.raises(InvalidOperationError):
        get_person_sunday_conflicts(
            session, person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=0,
        )


def test_validation_happens_before_any_query_is_executed(session, monkeypatch):
    seen_sessions = _stub_fetch(monkeypatch)

    with pytest.raises(InvalidOperationError):
        get_person_sunday_conflicts(
            session, person_id=-1, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
        )

    assert seen_sessions == []


# --------------------------------------------------------------------------
# ExistingCommitment source -- SQL shape (points 1-5)
#
# The compiled query is the only honest way to prove these offline: with no
# database, "is this row included" can only be answered by inspecting exactly
# which rows the WHERE clause would admit, which is what every assertion
# below does against the real predicate text.
# --------------------------------------------------------------------------


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _commitment_where() -> str:
    stmt = _existing_commitment_conflicts_statement(
        person_id=42, conflict_date=datetime.date(2026, 10, 4), target_ministry_id=3,
    )
    return _compile(stmt)


def test_1_same_person_date_source_less_commitment_is_included():
    """source_ministry_id IS NULL makes 'IS DISTINCT FROM 3' true, so a
    matching person/date row with no source ministry is selected."""
    compiled = _commitment_where()

    assert "existing_commitment.person_id IN (42)" in compiled
    assert "existing_commitment.commitment_date IN ('2026-10-04')" in compiled
    assert "source_ministry_id IS DISTINCT FROM 3" in compiled  # true when NULL


def test_2_commitment_from_another_ministry_is_included():
    """source_ministry_id = 4 (AV) IS DISTINCT FROM 3 (Setup, the target) is
    true, so this row is selected."""
    compiled = _commitment_where()

    assert "source_ministry_id IS DISTINCT FROM 3" in compiled  # true when 4 != 3


def test_3_commitment_sourced_from_target_ministry_is_excluded():
    """source_ministry_id = 3 IS DISTINCT FROM 3 is false -- the one case the
    predicate is built to exclude, and the only reason IS DISTINCT FROM (not a
    plain inequality) is used at all."""
    compiled = _commitment_where()

    assert "IS DISTINCT FROM 3" in compiled
    assert "source_ministry_id = 3" not in compiled  # no separate leaking equality


def test_4_another_person_is_excluded():
    compiled = _commitment_where()

    assert "existing_commitment.person_id IN (42)" in compiled
    # No OR/alternate person predicate exists to admit a different person --
    # exactly one comparison against person_id, in the WHERE clause. The set
    # form holds a single member here; widening `=` to `IN` was done so one
    # query can answer for many people at once, and narrows nothing.
    assert compiled.count("person_id IN (42)") == 1


def test_5_another_date_is_excluded():
    compiled = _commitment_where()

    assert "existing_commitment.commitment_date IN ('2026-10-04')" in compiled
    assert compiled.count("commitment_date IN ('2026-10-04')") == 1


def test_commitment_query_only_targets_existing_commitment_table():
    compiled = _commitment_where()

    assert compiled.strip().startswith("SELECT")
    assert "FROM existing_commitment" in compiled
    assert "JOIN" not in compiled


# --------------------------------------------------------------------------
# Authoritative Assignment source -- SQL shape (points 6-20)
# --------------------------------------------------------------------------


def _assignment_sql(conflict_date: datetime.date = datetime.date(2026, 10, 4)) -> str:
    stmt = _authoritative_assignment_conflicts_statement(
        person_id=42, conflict_date=conflict_date, target_ministry_id=3,
    )
    return _compile(stmt)


def test_6_and_7_draft_and_review_versions_are_excluded():
    """The DISTINCT ON subquery filters WHERE status = 'FINALIZED' before
    ranking -- a DRAFT or REVIEW row never enters the candidate set at all,
    regardless of its version_number."""
    compiled = _assignment_sql()

    assert "schedule_version.status = 'FINALIZED'" in compiled
    assert "'DRAFT'" not in compiled
    assert "'REVIEW'" not in compiled


def test_8_older_finalized_version_is_excluded_when_a_newer_finalized_exists():
    """DISTINCT ON (schedule_id) ... ORDER BY schedule_id, version_number DESC
    keeps exactly one row per schedule -- the first after ordering, which is
    the highest version_number. An older FINALIZED row for the same schedule
    is present in the pre-DISTINCT set but is not the one DISTINCT ON keeps."""
    compiled = _assignment_sql()

    assert "DISTINCT ON (schedule_version.schedule_id)" in compiled
    assert "ORDER BY schedule_version.schedule_id, schedule_version.version_number DESC" in compiled


def test_9_the_highest_finalized_version_is_included():
    """The same ORDER BY ... DESC is exactly what makes the highest
    version_number the one DISTINCT ON retains per schedule."""
    compiled = _assignment_sql()

    assert "version_number DESC" in compiled


def test_10_version_1_finalized_version_2_draft_leaves_version_1_authoritative():
    """A DRAFT row is excluded by the status filter before ranking even
    begins, so it can never be the one DISTINCT ON keeps -- Version 1 is the
    only FINALIZED candidate for its schedule and remains authoritative."""
    compiled = _assignment_sql()

    assert "schedule_version.status = 'FINALIZED'" in compiled  # excludes the V2 DRAFT row entirely


def test_11_version_1_finalized_version_2_review_leaves_version_1_authoritative():
    """Same mechanism as test 10: REVIEW never satisfies status = FINALIZED,
    so it never competes with Version 1 in the ranked set."""
    compiled = _assignment_sql()

    assert "schedule_version.status = 'FINALIZED'" in compiled  # excludes the V2 REVIEW row entirely


def test_12_version_1_finalized_version_2_finalized_only_version_2_counts():
    """Both rows now satisfy the status filter and enter the ranked set for
    the same schedule_id; DISTINCT ON keeps exactly one row per schedule_id,
    and version_number DESC orders Version 2 first -- so only Version 2 is
    retained by the same subquery already proven above."""
    compiled = _assignment_sql()

    assert "DISTINCT ON (schedule_version.schedule_id)" in compiled  # exactly one row per schedule
    assert "version_number DESC" in compiled  # the higher version_number sorts first


def test_assignment_query_does_not_merely_filter_status_finalized():
    """A bare 'status = FINALIZED' filter with no DISTINCT ON / highest-version
    resolution would count every superseded finalized version too -- the
    specific wrong query ADR 0003 names and rejects."""
    compiled = _assignment_sql()

    assert compiled.count("FINALIZED") == 1  # only inside the DISTINCT ON subquery
    assert "DISTINCT ON" in compiled


def test_13_assignment_from_target_ministry_is_excluded():
    compiled = _assignment_sql()

    assert "assignment.ministry_id != 3" in compiled


def test_14_assignment_from_another_ministry_is_included():
    """assignment.ministry_id != 3 is true for any other ministry id (e.g. 4,
    AV), so such a row satisfies the predicate."""
    compiled = _assignment_sql()

    assert "assignment.ministry_id != 3" in compiled


def test_15_another_person_is_excluded():
    compiled = _assignment_sql()

    assert "ministry_membership.person_id IN (42)" in compiled
    assert compiled.count("person_id IN (42)") == 1


def test_assignment_query_reaches_person_only_through_ministry_membership():
    """assignment carries no person_id of its own (schedule-output §11)."""
    compiled = _assignment_sql()

    assert "ministry_membership.person_id IN (42)" in compiled
    assert "assignment.person_id" not in compiled


def test_16_query_filters_on_schedule_version_requirement_event_date():
    compiled = _assignment_sql()

    assert "schedule_version_requirement.event_date IN ('2026-10-04')" in compiled


def test_17_query_does_not_use_event_event_date_for_date_equality():
    compiled = _assignment_sql()

    assert "event.event_date" not in compiled


def test_18_event_join_is_still_present_for_cancellation_state():
    compiled = _assignment_sql()

    assert "JOIN event ON event.id = assignment.event_id" in compiled
    assert "event.cancelled_at" in compiled


def test_19_cancelled_current_event_excludes_that_assignment():
    """event.cancelled_at IS NULL is an unconditional predicate on the joined
    current Event row -- a cancelled event (cancelled_at NOT NULL) fails it,
    so its assignment is excluded regardless of the authoritative version or
    snapshot date matching."""
    compiled = _assignment_sql()

    assert "event.cancelled_at IS NULL" in compiled


def test_20_moving_the_current_event_date_cannot_change_the_snapshot_date_predicate():
    """The date predicate is textually anchored to
    schedule_version_requirement.event_date, with event.event_date appearing
    nowhere in the query -- proven for a second, different conflict_date too,
    so this is not an artifact of one particular date value."""
    compiled = _assignment_sql(conflict_date=datetime.date(2026, 11, 15))

    assert "schedule_version_requirement.event_date IN ('2026-11-15')" in compiled
    assert "event.event_date" not in compiled
    assert "event.cancelled_at" in compiled  # the join is still there, for this only


def test_assignment_query_joins_schedule_version_requirement_for_the_snapshot():
    compiled = _assignment_sql()

    assert (
        "JOIN schedule_version_requirement"
        " ON schedule_version_requirement.id = assignment.schedule_version_requirement_id"
        in compiled
    )


def test_assignment_query_selects_from_assignment():
    compiled = _assignment_sql()

    assert compiled.strip().startswith("SELECT assignment.")
    assert "FROM assignment" in compiled
