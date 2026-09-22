"""Readiness asks its questions once per version, not once per assignment (Task 49).

Offline: no PostgreSQL, no network.

The endpoint that serves the schedule review screen was issuing 112 SQL
statements for a four-Sunday schedule -- four per assignment, because each
check queried for one membership at a time. Against a hosted database, where a
round trip costs tens of milliseconds, query *count* is the response time.

These tests pin the two properties that fix depended on, without asserting the
exact number of statements a future refactor might legitimately change:

- the per-assignment lookups are answered from one prefetch, so adding
  assignments does not add queries;
- the batched church-wide conflict query returns exactly what asking
  per-person returned, because both are built from the same statement.

Every person and ministry here is synthetic.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

from app.services import finalization_readiness as readiness
from app.services import sunday_conflict as conflict

SUNDAY = datetime.date(2026, 10, 4)
NEXT_SUNDAY = datetime.date(2026, 10, 11)


def _compile(stmt) -> str:
    from sqlalchemy.dialects import postgresql

    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


# ==========================================================================
# The prefetch: three set-based reads, whatever the assignment count
# ==========================================================================


class _RecordingSession:
    """Counts statements and returns nothing, so only the shape is measured."""

    def __init__(self) -> None:
        self.statements: list[object] = []

    def execute(self, statement):
        self.statements.append(statement)
        # ``scalar_one_or_none`` answers None for the Task 71 event-gap rule
        # lookup, which is "this period configures no gap" -- the world these
        # tests describe, and the one that loads no ministry history.
        return SimpleNamespace(
            scalars=lambda: iter(()),
            all=lambda: [],
            scalar_one_or_none=lambda: None,
        )


def _assignment(membership_id: int, event_id: int, person_id: int):
    return SimpleNamespace(
        ministry_membership_id=membership_id,
        event_id=event_id,
        ministry_membership=SimpleNamespace(person_id=person_id),
    )


def _requirement(
    role_id: int,
    event_date: datetime.date,
    ministry_id: int = 3,
    event_id: int = 700,
):
    return SimpleNamespace(
        ministry_role_id=role_id,
        event_date=event_date,
        ministry_id=ministry_id,
        # Task 71 derives the version's event sequence from these same rows
        # rather than re-reading the snapshot, so a requirement here carries
        # the event it belongs to.
        event_id=event_id,
    )


def test_the_prefetch_issues_a_fixed_number_of_queries(monkeypatch):
    """Seven set-based reads plus the batched conflict lookup -- and the count
    does not grow with the number of assignments, which is the whole point."""
    seen: list[dict] = []
    monkeypatch.setattr(
        conflict,
        "get_sunday_conflicts_for",
        lambda session, **kw: seen.append(kw) or {},
    )
    monkeypatch.setattr(
        readiness, "get_sunday_conflicts_for", lambda session, **kw: seen.append(kw) or {}
    )

    session = _RecordingSession()
    readiness._fetch_current_facts(
        session,
        scheduling_period_id=11,
        schedule_version_id=900,
        assignments=[_assignment(100 + i, 700 + (i % 2), 40 + i) for i in range(19)],
        requirements=[_requirement(12, SUNDAY), _requirement(13, NEXT_SUNDAY)],
    )

    # Qualifications, availability, serving limits, (Task 50) linked-pair
    # same-date exclusions, (Task 71) the period's event-gap rule and (Task 74)
    # the member-group caps and same-event support requirements: one query
    # each, whatever the assignment count. All three rules are unconfigured
    # here, so each costs the one lookup and loads no history, no group members
    # and no supporters at all.
    assert len(session.statements) == 7
    # The conflict rule is asked once for the whole set, not once per pair.
    assert len(seen) == 1
    assert len(seen[0]["person_ids"]) == 19
    assert sorted(seen[0]["conflict_dates"]) == [SUNDAY, NEXT_SUNDAY]


def test_query_count_does_not_grow_with_assignments(monkeypatch):
    """The N+1 regression guard, expressed as a property rather than a number."""
    monkeypatch.setattr(readiness, "get_sunday_conflicts_for", lambda session, **kw: {})

    counts = []
    for size in (1, 5, 25):
        session = _RecordingSession()
        readiness._fetch_current_facts(
            session,
            scheduling_period_id=11,
            schedule_version_id=900,
            assignments=[_assignment(100 + i, 700, 40 + i) for i in range(size)],
            requirements=[_requirement(12, SUNDAY)],
        )
        counts.append(len(session.statements))

    assert counts[0] == counts[1] == counts[2]


def test_no_assignments_means_no_queries_at_all(monkeypatch):
    monkeypatch.setattr(readiness, "get_sunday_conflicts_for", lambda session, **kw: {})
    session = _RecordingSession()
    facts = readiness._fetch_current_facts(
        session,
        scheduling_period_id=11,
        schedule_version_id=900,
        assignments=[],
        requirements=[],
    )

    assert session.statements == []
    assert facts.qualifications == {}
    assert facts.availability == {}
    assert facts.serving_limits == {}
    assert facts.conflicts == {}
    assert facts.same_date_pairs == ()


# ==========================================================================
# The batched conflict query is the same rule as the per-person one
# ==========================================================================


def test_the_batch_and_single_statements_come_from_one_definition():
    """Every predicate the ADR specifies appears in both, because the
    one-person builder delegates to the set-based one."""
    single = _compile(
        conflict._existing_commitment_conflicts_statement(
            person_id=42, conflict_date=SUNDAY, target_ministry_id=3
        )
    )
    batch = _compile(
        conflict._existing_commitment_conflicts_query(
            person_ids=(42,), conflict_dates=(SUNDAY,), target_ministry_id=3
        )
    )
    assert single == batch


def test_the_authoritative_assignment_rule_is_also_one_definition():
    single = _compile(
        conflict._authoritative_assignment_conflicts_statement(
            person_id=42, conflict_date=SUNDAY, target_ministry_id=3
        )
    )
    batch = _compile(
        conflict._authoritative_assignment_conflicts_query(
            person_ids=(42,), conflict_dates=(SUNDAY,), target_ministry_id=3
        )
    )
    assert single == batch
    # The ADR's own constructs survive the generalisation.
    assert "DISTINCT ON" in batch
    assert "schedule_version.status = 'FINALIZED'" in batch
    assert "schedule_version_requirement.event_date" in batch
    assert "event.cancelled_at IS NULL" in batch
    assert "assignment.ministry_id != 3" in batch


def test_the_batch_widens_only_the_person_and_date_predicates():
    batch = _compile(
        conflict._existing_commitment_conflicts_query(
            person_ids=(42, 43), conflict_dates=(SUNDAY, NEXT_SUNDAY),
            target_ministry_id=3,
        )
    )
    assert "existing_commitment.person_id IN (42, 43)" in batch
    assert "commitment_date IN ('2026-10-04', '2026-10-11')" in batch
    # The cross-ministry rule is untouched and still a single scalar test.
    assert "source_ministry_id IS DISTINCT FROM 3" in batch


def test_the_batch_answers_for_every_requested_pair(monkeypatch):
    """A pair with no conflict gets an explicit empty result, so a caller
    never has to read a missing key as 'probably fine'."""
    monkeypatch.setattr(
        conflict, "_existing_commitment_conflicts_query",
        lambda **kw: "commitments",
    )
    monkeypatch.setattr(
        conflict, "_authoritative_assignment_conflicts_query",
        lambda **kw: SimpleNamespace(options=lambda *a: "assignments"),
    )

    class _Session:
        def execute(self, statement):
            return SimpleNamespace(scalars=lambda: iter(()))

    result = conflict.get_sunday_conflicts_for(
        _Session(), person_ids=[42, 43], conflict_dates=[SUNDAY, NEXT_SUNDAY],
        target_ministry_id=3,
    )

    assert set(result) == {
        (42, SUNDAY), (42, NEXT_SUNDAY), (43, SUNDAY), (43, NEXT_SUNDAY),
    }
    for value in result.values():
        assert value.is_blocked is False


def test_the_batch_rejects_the_same_bad_input_the_single_form_does():
    import pytest

    from app.services.errors import InvalidOperationError

    class _Session:
        def execute(self, statement):  # pragma: no cover - must not run
            raise AssertionError("validation must happen before any query")

    with pytest.raises(InvalidOperationError, match="target_ministry_id"):
        conflict.get_sunday_conflicts_for(
            _Session(), person_ids=[42], conflict_dates=[SUNDAY], target_ministry_id=0
        )
    with pytest.raises(InvalidOperationError, match="person_id"):
        conflict.get_sunday_conflicts_for(
            _Session(), person_ids=[0], conflict_dates=[SUNDAY], target_ministry_id=3
        )
