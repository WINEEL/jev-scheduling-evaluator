"""Recorded-serving service tests (Task 79 §12--§14).

Offline: no PostgreSQL, no Neon, no network. Two techniques, the same split
``tests/test_services_person_directory.py`` uses:

- **What the queries say** is tested mechanically -- each statement is compiled
  for the PostgreSQL dialect and inspected. That is how the *rule* is pinned:
  the authoritative-version join, the snapshot date, the cancelled-event
  exclusion, the strict "before today" boundary and the DISTINCT that collapses
  several role rows into one occurrence.
- **What the service does with the answers** is tested with a scripted Session,
  so the totals, the breakdown, the sum-agrees-with-breakdown property and the
  cross-ministry conflict reporting can be exercised without rows existing
  anywhere.

**What those queries actually return against real data** is not a thing this
file can know, and it is exactly what
``tests/integration/test_pg_serving_history.py`` runs against PostgreSQL: the
future/DRAFT/REVIEW/cancelled exclusions, two roles on one Sunday counting
once, several Sundays summing, the per-ministry breakdown, no leakage between
people, church-timezone date semantics, and the same-Sunday cross-ministry
regression.

Every person and ministry name here is fictional.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.serving_history import (
    RECORDED_SERVING_LABEL,
    CrossMinistryConflict,
    MinistryServingCount,
    PersonServingSummary,
    _by_ministry_statement,
    _cross_ministry_conflict_statement,
    _totals_statement,
    _unique_ids,
    find_cross_ministry_conflicts,
    recorded_serving_summaries,
    recorded_serving_totals,
)

TODAY = datetime.date(2026, 9, 19)
CHURCH = 1


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class ServingSession:
    """A stand-in Session that answers the three statements this module issues.

    Dispatches on the compiled SQL rather than on call order, so a test that
    adds a query somewhere else does not silently receive another's canned
    answer. ``commit`` and ``rollback`` are forbidden: this module is
    read-only, and a write of any kind here would be a defect.
    """

    def __init__(self, *, totals=(), by_ministry=(), conflicts=()) -> None:
        self.totals = totals
        self.by_ministry = by_ministry
        self.conflicts = conflicts
        self.executed_sql: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.executed_sql.append(sql)
        if "array_agg" in sql:
            return _Result(self.conflicts)
        if "ministry.name" in sql:
            return _Result(self.by_ministry)
        return _Result(self.totals)

    def commit(self):  # pragma: no cover - must never run
        raise AssertionError("a read-only service must never commit")

    def rollback(self):  # pragma: no cover - must never run
        raise AssertionError("a read-only service must never roll back")

    def add(self, obj):  # pragma: no cover - must never run
        raise AssertionError("a read-only service must never add")

    def flush(self, objects=None):  # pragma: no cover - must never run
        raise AssertionError("a read-only service must never flush")


class _Result:
    def __init__(self, rows) -> None:
        self._rows = tuple(rows)

    def all(self):
        return list(self._rows)


def _total_row(person_id: int, count: int):
    return SimpleNamespace(person_id=person_id, recorded_serving=count)


def _ministry_row(person_id: int, ministry_id: int, name: str, count: int):
    return SimpleNamespace(
        person_id=person_id,
        ministry_id=ministry_id,
        ministry_name=name,
        recorded_serving=count,
    )


def _conflict_row(person_id: int, date: datetime.date, names: list[str]):
    return SimpleNamespace(person_id=person_id, event_date=date, ministry_names=names)


def _sql(statement) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


# --------------------------------------------------------------------------
# The term itself
# --------------------------------------------------------------------------


class TestTerminology:
    """§13's truthfulness requirement, pinned as a test rather than a comment.

    The domain records who was *scheduled*. Nothing in it records who turned
    up, so a label claiming attendance would be a statement about somebody's
    conduct made on evidence that cannot support it.
    """

    def test_the_label_is_recorded_serving(self):
        assert RECORDED_SERVING_LABEL == "Recorded serving"

    def test_the_label_never_claims_attendance(self):
        lowered = RECORDED_SERVING_LABEL.lower()
        assert "attend" not in lowered
        assert "actually served" not in lowered


# --------------------------------------------------------------------------
# What the queries say
# --------------------------------------------------------------------------


class TestServingOccurrenceRule:
    """One place defines what counts, and every statement is built on it."""

    @pytest.fixture
    def statements(self):
        return {
            "totals": _sql(_totals_statement(person_ids=[1, 2], as_of=TODAY)),
            "by_ministry": _sql(
                _by_ministry_statement(person_ids=[1, 2], as_of=TODAY)
            ),
            "conflicts": _sql(
                _cross_ministry_conflict_statement(person_ids=[1, 2], as_of=TODAY)
            ),
        }

    def test_only_authoritative_versions_are_counted(self, statements):
        """ADR 0003: the highest-numbered FINALIZED version per schedule --
        which is what excludes DRAFT, REVIEW *and* a version an amendment has
        superseded, in one predicate rather than three."""
        for sql in statements.values():
            assert "DISTINCT ON (schedule_version.schedule_id)" in sql
            assert "schedule_version.status = 'FINALIZED'" in sql
            assert "schedule_version.version_number DESC" in sql

    def test_the_date_is_the_version_snapshot_not_the_live_event(self, statements):
        """A finalized version committed people to a date. Moving the event
        afterwards must not silently relocate the history."""
        for sql in statements.values():
            assert "schedule_version_requirement.event_date" in sql
            assert "event.event_date" not in sql

    def test_cancelled_events_are_excluded(self, statements):
        for sql in statements.values():
            assert "event.cancelled_at IS NULL" in sql

    def test_future_dates_are_excluded_by_a_strict_comparison(self, statements):
        """Strictly before today: a service happening today has not been served
        yet, and one that has not happened is not history."""
        for sql in statements.values():
            assert "schedule_version_requirement.event_date < '2026-09-19'" in sql
            assert "event_date <= " not in sql

    def test_the_person_is_reached_through_the_membership(self, statements):
        """``assignment`` carries no ``person_id`` of its own."""
        for sql in statements.values():
            assert "ministry_membership.person_id IN (1, 2)" in sql

    def test_occurrences_are_distinct_by_person_ministry_and_date(self, statements):
        """The line that stops two role rows on one Sunday counting twice."""
        for sql in statements.values():
            assert "SELECT DISTINCT" in sql

    def test_the_totals_query_aggregates_in_the_database(self):
        sql = _sql(_totals_statement(person_ids=[1], as_of=TODAY))
        assert "count(*)" in sql
        assert "GROUP BY" in sql

    def test_the_breakdown_joins_the_ministry_name(self):
        """So a caller never looks a name up per row -- the per-ministry N+1
        this module exists to avoid."""
        sql = _sql(_by_ministry_statement(person_ids=[1], as_of=TODAY))
        assert "JOIN ministry" in sql
        assert "ministry.name" in sql

    def test_the_breakdown_is_ordered_for_a_stable_reading(self):
        sql = _sql(_by_ministry_statement(person_ids=[1], as_of=TODAY))
        assert "ORDER BY" in sql
        assert "lower(ministry.name)" in sql

    def test_the_conflict_query_asks_for_more_than_one_ministry_on_a_date(self):
        """The church-wide hard rule, stated as a query."""
        sql = _sql(_cross_ministry_conflict_statement(person_ids=[1], as_of=TODAY))
        assert "HAVING count(DISTINCT" in sql
        assert "> 1" in sql


# --------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------


class TestRecordedServingTotals:
    def test_it_is_one_query_for_a_whole_page(self):
        """§14: bounded aggregate querying, never one per person."""
        session = ServingSession(totals=(_total_row(1, 4),))
        recorded_serving_totals(
            session, person_ids=list(range(1, 101)), church_id=CHURCH, as_of=TODAY
        )
        assert len(session.executed_sql) == 1

    def test_every_requested_person_is_present(self):
        """Somebody with no history reads ``0``, never a missing key -- a
        caller must not have to treat absence and zero as two facts."""
        session = ServingSession(totals=(_total_row(1, 4),))
        totals = recorded_serving_totals(
            session, person_ids=[1, 2, 3], church_id=CHURCH, as_of=TODAY
        )
        assert totals == {1: 4, 2: 0, 3: 0}

    def test_no_people_issues_no_query_at_all(self):
        session = ServingSession()
        assert (
            recorded_serving_totals(
                session, person_ids=[], church_id=CHURCH, as_of=TODAY
            )
            == {}
        )
        assert session.executed_sql == []

    def test_a_repeated_id_does_not_produce_two_entries(self):
        session = ServingSession(totals=(_total_row(1, 4),))
        totals = recorded_serving_totals(
            session, person_ids=[1, 1, 1], church_id=CHURCH, as_of=TODAY
        )
        assert totals == {1: 4}


# --------------------------------------------------------------------------
# Summaries
# --------------------------------------------------------------------------


class TestRecordedServingSummaries:
    def test_it_is_two_queries_whatever_the_number_of_people(self):
        session = ServingSession()
        recorded_serving_summaries(
            session, person_ids=list(range(1, 51)), church_id=CHURCH, as_of=TODAY
        )
        assert len(session.executed_sql) == 2

    def test_the_total_is_the_sum_of_the_breakdown(self):
        """By construction, not by a second query -- so a screen showing both
        can never show two numbers that disagree."""
        session = ServingSession(
            by_ministry=(
                _ministry_row(1, 100, "AV", 9),
                _ministry_row(1, 200, "Setup", 12),
                _ministry_row(1, 300, "Kids", 6),
            )
        )
        summary = recorded_serving_summaries(
            session, person_ids=[1], church_id=CHURCH, as_of=TODAY
        )[1]
        assert summary.total == 27
        assert summary.total == sum(item.count for item in summary.by_ministry)

    def test_only_ministries_with_history_appear(self):
        """§12: a row reading "AV: 0" would assert an absence that the absence
        of the row already states."""
        session = ServingSession(by_ministry=(_ministry_row(1, 100, "AV", 3),))
        summary = recorded_serving_summaries(
            session, person_ids=[1], church_id=CHURCH, as_of=TODAY
        )[1]
        assert [item.ministry_name for item in summary.by_ministry] == ["AV"]

    def test_one_persons_rows_never_reach_another(self):
        session = ServingSession(
            by_ministry=(
                _ministry_row(1, 100, "AV", 3),
                _ministry_row(2, 200, "Setup", 5),
            )
        )
        summaries = recorded_serving_summaries(
            session, person_ids=[1, 2], church_id=CHURCH, as_of=TODAY
        )
        assert summaries[1].total == 3
        assert summaries[2].total == 5
        assert [item.ministry_name for item in summaries[1].by_ministry] == ["AV"]
        assert [item.ministry_name for item in summaries[2].by_ministry] == ["Setup"]

    def test_somebody_with_no_history_gets_a_zero_summary(self):
        session = ServingSession()
        summary = recorded_serving_summaries(
            session, person_ids=[9], church_id=CHURCH, as_of=TODAY
        )[9]
        assert summary == PersonServingSummary(person_id=9, total=0)
        assert summary.has_conflicts is False


# --------------------------------------------------------------------------
# The invalid state, reported rather than counted
# --------------------------------------------------------------------------


class TestCrossMinistryConflicts:
    """§13: two ministries on one Sunday is a hard-rule violation, not two
    legitimate serving occurrences."""

    def test_a_conflict_is_reported_on_the_summary(self):
        session = ServingSession(
            by_ministry=(
                _ministry_row(22, 100, "AV", 1),
                _ministry_row(22, 200, "Setup", 1),
            ),
            conflicts=(
                _conflict_row(22, datetime.date(2026, 10, 11), ["AV", "Setup"]),
            ),
        )
        summary = recorded_serving_summaries(
            session, person_ids=[22], church_id=CHURCH, as_of=TODAY
        )[22]
        assert summary.has_conflicts is True
        assert summary.same_date_conflicts[0].event_date == datetime.date(2026, 10, 11)
        assert set(summary.same_date_conflicts[0].ministry_names) == {"AV", "Setup"}

    def test_a_healthy_record_reports_nothing(self):
        session = ServingSession(by_ministry=(_ministry_row(1, 100, "AV", 5),))
        summary = recorded_serving_summaries(
            session, person_ids=[1], church_id=CHURCH, as_of=TODAY
        )[1]
        assert summary.same_date_conflicts == ()
        assert summary.has_conflicts is False

    def test_the_conflict_names_the_ministries_involved(self):
        """"Person 22 is in two ministries on 11 October" is actionable;
        "Person 22 has a conflict" is not."""
        session = ServingSession(
            conflicts=(
                _conflict_row(22, datetime.date(2026, 10, 11), ["AV", "Setup"]),
            )
        )
        found = find_cross_ministry_conflicts(
            session, person_ids=[22], church_id=CHURCH, as_of=TODAY
        )
        assert found == (
            CrossMinistryConflict(
                person_id=22,
                event_date=datetime.date(2026, 10, 11),
                ministry_names=("AV", "Setup"),
            ),
        )

    def test_no_people_issues_no_query(self):
        session = ServingSession()
        assert (
            find_cross_ministry_conflicts(
                session, person_ids=[], church_id=CHURCH, as_of=TODAY
            )
            == ()
        )
        assert session.executed_sql == []


# --------------------------------------------------------------------------
# Small guarantees
# --------------------------------------------------------------------------


class TestDiscipline:
    def test_nothing_here_writes(self):
        """The Session forbids every write method; reaching here means none
        was called."""
        session = ServingSession(by_ministry=(_ministry_row(1, 100, "AV", 1),))
        recorded_serving_totals(
            session, person_ids=[1], church_id=CHURCH, as_of=TODAY
        )
        recorded_serving_summaries(
            session, person_ids=[1], church_id=CHURCH, as_of=TODAY
        )

    def test_ids_are_deduplicated_and_sorted(self):
        assert _unique_ids([3, 1, 3, 2]) == [1, 2, 3]

    def test_non_positive_ids_are_dropped(self):
        """A caller passing 0 or a negative id is asking about nobody, and
        widening the ``IN`` list with it would only slow the query down."""
        assert _unique_ids([0, -1, 5]) == [5]

    def test_the_value_objects_are_frozen(self):
        count = MinistryServingCount(ministry_id=1, ministry_name="AV", count=2)
        with pytest.raises(Exception):
            count.count = 3
