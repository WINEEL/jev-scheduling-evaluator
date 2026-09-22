"""The reusable church-wide Sunday conflict query (ADR 0002, ADR 0003).

Answers exactly one question: *does Person P have a church-wide scheduling
conflict on date D when scheduling Ministry M?* This is read-only domain
logic, not a service that mutates anything -- it exists to be called by later
Assignment and solver services, and by nothing that changes state.

**[APPROVED] The hard rule is one Ministry per Sunday, not one Event per
Sunday** (ADR 0002). The same Person serving two Events of the *same* Ministry
on one date is not, by itself, a conflict this query reports -- Assignment's
own uniqueness already prevents one membership filling two positions in one
Event, and that is a different, narrower rule than this one. What this query
reports is a *different* Ministry's claim on the person for that date.

**[APPROVED] Two sources, unioned at query time, never mirrored** (ADR 0002):

1. **``existing_commitment``** -- explicit, church-wide blocks a person or an
   importer typed in for a responsibility this system does not manage.
   ``source_ministry_id`` is *provenance*, never scope: a commitment sourced
   from AV blocks Setup exactly as it blocks anything else, which is why the
   only source-ministry rows this query excludes are ones sourced from the
   *target* ministry itself -- the one already being scheduled, which cannot
   conflict with itself.
2. **Authoritative ``assignment`` rows** -- see below.

Finalized assignments are never copied into ``existing_commitment``, and
nothing here writes to either table.

**[REVIEWED] "Authoritative" is derived, never stored** (ADR 0003): *the
highest-numbered ``schedule_version`` whose status is ``FINALIZED``, for that
schedule.* Not "any ``FINALIZED`` version" -- that would keep counting a
version an amendment has superseded, blocking people on the strength of an
arrangement the schedule no longer shows. Not "the latest version" -- that
would let an unreviewed draft block another ministry before anyone has agreed
to it. Both wrong readings look plausible and produce a schedule that is
quietly incorrect rather than an error, which is exactly why ADR 0003 insists
this query live in one shared place rather than being rewritten at each call
site.

**[REVIEWED] The conflict date is the version's own snapshot, never the
current Event row** (ADR 0003): ``schedule_version_requirement.event_date``,
not ``event.event_date``. If a finalized version committed people to 15
November and the event is later moved to 22 November, this query must keep
asserting 15 November -- reading the current row would silently relocate every
one of those commitments to a date nobody agreed to. The current ``event`` row
is still consulted, but for exactly one fact: ``cancelled_at``. A cancelled
event means nobody is serving, which is current state and is the right thing
to read live; the *date* people were committed to is not.

Not implemented here, deliberately: Assignment mutation, the solver,
finalization, amendments, authorization, and any persisted diagnostic or audit
trail of a conflict check. This function only answers the question; every
consequence of the answer belongs to its caller.
"""

from __future__ import annotations

import datetime
from typing import Sequence
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, joinedload

from app.models.core import MinistryMembership
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_FINALIZED,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import Event, ExistingCommitment
from app.services.errors import InvalidOperationError

__all__ = [
    "authoritative_version_subquery",
    "get_sunday_conflicts_for","SundayConflictResult", "get_person_sunday_conflicts"]


def authoritative_version_subquery():
    """The ids of the **authoritative** schedule versions, one per schedule.

    ADR 0003's own reviewed SQL: the highest ``version_number`` among
    ``FINALIZED`` rows for each schedule -- ``SELECT DISTINCT ON (schedule_id)
    ... ORDER BY schedule_id, version_number DESC``, expressed here as
    ``Select.distinct(schedule_id)`` ordered the same way, which SQLAlchemy
    compiles to PostgreSQL's ``DISTINCT ON``. That is the exact construct the
    ADR specifies and the one the partial index ``(schedule_id, version_number
    DESC) WHERE status = 'FINALIZED'`` exists to serve.

    **Public because a second question now needs the same answer.** The
    event-gap rule (:mod:`app.services.event_gap`) asks who is committed at a
    ministry's events in the periods either side of the one being scheduled,
    and "committed" means the same authoritative reading "served" means here --
    which matters most looking *forward*, where a draft for the next quarter
    may exist and must not block anybody. ADR 0003's whole point is that this
    definition lives in one place rather than being rewritten at each call
    site: not "any FINALIZED version", which keeps counting one an amendment
    has superseded, and not "the latest version", which lets an unreviewed
    draft speak for the church. Both wrong readings look plausible and produce
    a schedule that is quietly incorrect rather than an error.
    """
    return (
        select(ScheduleVersion.id)
        .distinct(ScheduleVersion.schedule_id)
        .where(ScheduleVersion.status == SCHEDULE_VERSION_STATUS_FINALIZED)
        .order_by(ScheduleVersion.schedule_id, ScheduleVersion.version_number.desc())
        .subquery()
    )


@dataclass(frozen=True, slots=True)
class SundayConflictResult:
    """The rows behind one person/date/ministry conflict answer.

    Deliberately not collapsed to a boolean: a caller explaining *why* someone
    is blocked needs the actual rows, and there can legitimately be more than
    one -- two explicit commitments, or authoritative assignments at two
    events in the same other ministry. Nothing here deduplicates them; every
    matching row is retained.

    Frozen by design: this is a query result, not a domain entity with its
    own identity or lifecycle, and there is nothing about it a caller should
    ever mutate.
    """

    existing_commitments: tuple[ExistingCommitment, ...]
    authoritative_assignments: tuple[Assignment, ...]

    @property
    def is_blocked(self) -> bool:
        """Whether *any* conflict exists, from either source.

        ``True`` the moment either tuple is non-empty -- ADR 0002's rule is a
        union (``OR``), not a preference for one source over the other.
        """
        return bool(self.existing_commitments) or bool(self.authoritative_assignments)


def get_person_sunday_conflicts(
    session: Session,
    *,
    person_id: int,
    conflict_date: datetime.date,
    target_ministry_id: int,
) -> SundayConflictResult:
    """Is ``person_id`` already spoken for on ``conflict_date``, for ministries
    other than ``target_ministry_id``?

    **Read-only.** This function only calls ``session.execute()`` -- never
    ``add()``, ``delete()``, ``flush()``, ``commit()``, or ``rollback()`` --
    and uses no session but the one supplied. It also performs no
    authorization check: whether the caller is entitled to ask this question
    is the calling operation's responsibility, not this low-level query's
    (module scope).

    Validation here is deliberately minimal -- ``person_id`` and
    ``target_ministry_id`` must be positive -- and does **not** confirm either
    id actually names a row. A normal caller already holds the domain objects
    this query is being asked about; re-querying to prove they exist would
    make this a second, redundant existence check rather than a conflict
    query.

    :raises InvalidOperationError: ``person_id`` or ``target_ministry_id`` is
        not positive.
    """
    if person_id <= 0:
        raise InvalidOperationError("person_id must be positive")
    if target_ministry_id <= 0:
        raise InvalidOperationError("target_ministry_id must be positive")

    commitments = _fetch_existing_commitment_conflicts(
        session, person_id=person_id, conflict_date=conflict_date,
        target_ministry_id=target_ministry_id,
    )
    assignments = _fetch_authoritative_assignment_conflicts(
        session, person_id=person_id, conflict_date=conflict_date,
        target_ministry_id=target_ministry_id,
    )

    return SundayConflictResult(
        existing_commitments=commitments, authoritative_assignments=assignments,
    )


def get_sunday_conflicts_for(
    session: Session,
    *,
    person_ids: Sequence[int],
    conflict_dates: Sequence[datetime.date],
    target_ministry_id: int,
) -> dict[tuple[int, datetime.date], SundayConflictResult]:
    """:func:`get_person_sunday_conflicts` for many people and dates at once.

    **Same rule, same two sources, one round trip each.** Both halves are built
    by the very statement builders the one-person function uses, widened from
    ``=`` to ``IN``; nothing about which rows count as a conflict is restated
    here. A caller that would otherwise ask the same question once per
    assignment gets identical answers from two queries instead of two per
    assignment, which against a hosted database is the whole cost.

    The result is keyed by ``(person_id, conflict_date)`` and contains an entry
    for **every** requested pair, including the pairs with no conflict at all --
    so a caller reads the answer the same way for every pair and never has to
    treat a missing key as "probably fine".

    Read-only, and no authorization check, exactly as the one-person function:
    whether the caller may ask is the calling operation's business.

    :raises InvalidOperationError: ``target_ministry_id`` is not positive, or
        any supplied person id is not positive.
    """
    if target_ministry_id <= 0:
        raise InvalidOperationError("target_ministry_id must be positive")
    for person_id in person_ids:
        if person_id <= 0:
            raise InvalidOperationError("person_id must be positive")

    people = sorted(set(person_ids))
    dates = sorted(set(conflict_dates))
    results: dict[tuple[int, datetime.date], SundayConflictResult] = {
        (person_id, date): SundayConflictResult(
            existing_commitments=(), authoritative_assignments=()
        )
        for person_id in people
        for date in dates
    }
    if not people or not dates:
        return results

    commitments: dict[tuple[int, datetime.date], list[ExistingCommitment]] = {}
    for row in session.execute(
        _existing_commitment_conflicts_query(
            person_ids=people, conflict_dates=dates,
            target_ministry_id=target_ministry_id,
        )
    ).scalars():
        commitments.setdefault((row.person_id, row.commitment_date), []).append(row)

    # An Assignment carries neither the person nor the snapshot date directly
    # (schedule-output §11), so both are read back through the rows the query
    # already joined -- not re-queried, and not re-derived from current state.
    assignments: dict[tuple[int, datetime.date], list[Assignment]] = {}
    conflict_assignments = _authoritative_assignment_conflicts_query(
        person_ids=people, conflict_dates=dates,
        target_ministry_id=target_ministry_id,
    ).options(
        # Both are read immediately below to key the result. Left lazy they
        # would be a round trip per conflicting row -- reintroducing, inside
        # the batch, the very per-row querying it exists to remove.
        joinedload(Assignment.ministry_membership),
        joinedload(Assignment.schedule_version_requirement),
    )
    for row in session.execute(conflict_assignments).scalars():
        key = (
            row.ministry_membership.person_id,
            row.schedule_version_requirement.event_date,
        )
        assignments.setdefault(key, []).append(row)

    for key in results:
        results[key] = SundayConflictResult(
            existing_commitments=tuple(commitments.get(key, ())),
            authoritative_assignments=tuple(assignments.get(key, ())),
        )
    return results


def _fetch_existing_commitment_conflicts(
    session: Session, *, person_id: int, conflict_date: datetime.date,
    target_ministry_id: int,
) -> tuple[ExistingCommitment, ...]:
    """Execution split from statement construction so it is testable with no
    database: orchestration tests monkeypatch this function directly, while
    :func:`_existing_commitment_conflicts_statement` is compiled and inspected
    on its own (see ``tests/test_services_sunday_conflict.py``).
    """
    stmt = _existing_commitment_conflicts_statement(
        person_id=person_id, conflict_date=conflict_date, target_ministry_id=target_ministry_id,
    )
    return tuple(session.execute(stmt).scalars().all())


def _fetch_authoritative_assignment_conflicts(
    session: Session, *, person_id: int, conflict_date: datetime.date,
    target_ministry_id: int,
) -> tuple[Assignment, ...]:
    """Execution split from statement construction, for the same testing
    reason as :func:`_fetch_existing_commitment_conflicts`.
    """
    stmt = _authoritative_assignment_conflicts_statement(
        person_id=person_id, conflict_date=conflict_date, target_ministry_id=target_ministry_id,
    )
    return tuple(session.execute(stmt).scalars().all())


def _existing_commitment_conflicts_statement(
    *, person_id: int, conflict_date: datetime.date, target_ministry_id: int
) -> Select[tuple[ExistingCommitment]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_sunday_conflict.py``).

    ``source_ministry_id.is_distinct_from(target_ministry_id)`` is one
    predicate covering both cases ADR 0002 describes: a source-less
    (church-wide) commitment is always distinct from a real ministry id, so it
    always blocks; a commitment sourced from a *different* ministry is
    distinct too. Only a commitment sourced from the target ministry itself is
    **not** distinct from it, and is correctly excluded -- provenance equal to
    scope is not a cross-ministry conflict.
    """
    return _existing_commitment_conflicts_query(
        person_ids=(person_id,),
        conflict_dates=(conflict_date,),
        target_ministry_id=target_ministry_id,
    )


def _existing_commitment_conflicts_query(
    *,
    person_ids: Sequence[int],
    conflict_dates: Sequence[datetime.date],
    target_ministry_id: int,
) -> Select[tuple[ExistingCommitment]]:
    """The commitment half of the rule, for a *set* of people and dates.

    **This is the single definition**; the one-person form above delegates to
    it with a one-element set, so the predicate cannot drift between the two.
    Widening ``=`` to ``IN`` changes nothing about which rows qualify: a row is
    returned only when its own person and its own date are both in the sets
    asked about, so grouping the results by ``(person_id, commitment_date)``
    reconstructs exactly the per-person answers, one round trip instead of one
    per pair.
    """
    return select(ExistingCommitment).where(
        ExistingCommitment.person_id.in_(person_ids),
        ExistingCommitment.commitment_date.in_(conflict_dates),
        ExistingCommitment.source_ministry_id.is_distinct_from(target_ministry_id),
    )


def _authoritative_assignment_conflicts_statement(
    *, person_id: int, conflict_date: datetime.date, target_ministry_id: int
) -> Select[tuple[Assignment]]:
    """The query, split from its execution for the same testing reason as
    :func:`_existing_commitment_conflicts_statement`.

    **Authoritative versions** come from
    :func:`authoritative_version_subquery`, which holds ADR 0003's own reviewed
    SQL -- the highest ``version_number`` among ``FINALIZED`` rows, one per
    schedule. Joining ``Assignment`` to this subquery on
    ``schedule_version_id`` is what restricts the result to assignments
    belonging to an authoritative version and nothing else -- not merely
    ``status = 'FINALIZED'`` alone, which would also count a version an
    amendment has already superseded.

    **The conflict date is ``ScheduleVersionRequirement.event_date``**, the
    immutable per-version snapshot, reached through
    ``Assignment.schedule_version_requirement_id`` -- never
    ``Event.event_date``. ``Event`` is still joined, through
    ``Assignment.event_id``, but is read for exactly one fact:
    ``cancelled_at``. Both joins rely on the model's own composite foreign
    keys to guarantee agreement (``assignment``'s four-column key already
    proves its ``event_id`` matches its requirement's; nothing here re-proves
    that with an extra predicate).

    **Person is reached only through ``MinistryMembership``** -- ``assignment``
    carries no ``person_id`` of its own (schedule-output §11).
    """
    return _authoritative_assignment_conflicts_query(
        person_ids=(person_id,),
        conflict_dates=(conflict_date,),
        target_ministry_id=target_ministry_id,
    )


def _authoritative_assignment_conflicts_query(
    *,
    person_ids: Sequence[int],
    conflict_dates: Sequence[datetime.date],
    target_ministry_id: int,
) -> Select[tuple[Assignment]]:
    """The assignment half of the rule, for a *set* of people and dates.

    **This is the single definition**; the one-person form above delegates to
    it with a one-element set. Every join and every predicate the ADR
    specifies -- the ``DISTINCT ON`` authoritative-version subquery, the
    snapshot ``event_date``, the cross-ministry test, the cancelled-event
    exclusion -- lives here once and is shared by both callers.
    """
    authoritative_versions = authoritative_version_subquery()

    return (
        select(Assignment)
        .join(
            ScheduleVersionRequirement,
            ScheduleVersionRequirement.id == Assignment.schedule_version_requirement_id,
        )
        .join(
            MinistryMembership,
            MinistryMembership.id == Assignment.ministry_membership_id,
        )
        .join(Event, Event.id == Assignment.event_id)
        .join(
            authoritative_versions,
            authoritative_versions.c.id == Assignment.schedule_version_id,
        )
        .where(
            MinistryMembership.person_id.in_(person_ids),
            ScheduleVersionRequirement.event_date.in_(conflict_dates),
            Assignment.ministry_id != target_ministry_id,
            Event.cancelled_at.is_(None),
        )
    )
