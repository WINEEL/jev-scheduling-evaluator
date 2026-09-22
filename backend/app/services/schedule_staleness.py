"""Is a ScheduleVersion's requirement snapshot still current? (§13)

Answers exactly one question, read-only: *does today's staffing configuration
still match what this version was built against?* Section numbers refer to
``docs/architecture/schedule-output-data-model.md``.

**[REVIEWED] Staleness is an exact set comparison, never a timestamp
heuristic** (§13, decision 14). The compared identity is the same business
tuple on both sides::

    (event_id, event_date, ministry_role_id, required_count)

The two sides are:

1. **Current** -- ``staffing_requirement`` rows joined to the *currently
   schedulable* events of the version's scheduling period: ``event`` rows with
   ``cancelled_at IS NULL``, read at their **current** ``event.event_date``.
2. **Snapshot** -- the version's own immutable ``schedule_version_requirement``
   rows, read at the ``event_date`` **stored on the snapshot row** (§8).

A version is fresh only when the two sets are exactly equal, so the symmetric
difference catches every case the design lists: a requirement added, a
requirement removed, ``required_count`` changed, an event's date moved, an
event cancelled, an event returning to the schedulable set, and a role
requirement removed and recreated. An earlier revision's attempt to detect
this by comparing ``updated_at`` could not see deletions at all, which is why
nothing here reads a timestamp, a ``staffing_requirement.id``, or a name.

**The two sides deliberately read different date columns**, and confusing them
is the one bug this module exists to avoid:

- The **current** side asks "what is the ministry scheduling *now*?", so it
  reads the live ``event.event_date``.
- The **snapshot** side asks "what was this version built against?", so it
  reads ``schedule_version_requirement.event_date``, which is immutable
  historical state (model docstring, §8). Reading the current event's date on
  both sides would make an old version look fresh the moment its event moved,
  which is precisely the failure including ``event_date`` in the comparison
  was meant to prevent (§13).

That is also why this module differs from Task 21's conflict query
(:mod:`app.services.sunday_conflict`), which reads the snapshot date on *both*
sides: it asks a different question -- "what historical date does an
authoritative assignment occupy?" -- not "does current configuration still
match?".

**Cancellation is asymmetric, on purpose.** Cancelled events are excluded from
the current set only. Snapshot rows are *never* filtered by the current
``event.cancelled_at``: filtering them too would make a cancelled event's rows
vanish from both sides at once and report the version fresh, when cancelling
an event is exactly the kind of change that should make it stale. A cancelled
event's snapshot rows therefore remain, and surface as ``snapshot_only``.

**Descriptive only.** This helper computes; it decides nothing. It applies to
a version of *any* status (§13 runs the same comparison against a DRAFT that
must not yet be finalized and against an authoritative FINALIZED version that
may need an amendment), it never mutates a version because it is stale, it
persists no diagnostics, and it writes no AuditEvent -- staleness is derived
state, and re-deriving it is a query (ADR 0003's reasoning). It performs no
authorization check either: whether a caller may ask this question belongs to
the operation that exposes the answer.

Not implemented here, deliberately: the REVIEW/FINALIZED transitions and their
staleness gate (§16), successor-version creation, assignment copying, the
solver, and any API or UI over the result.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.schedule_output import ScheduleVersion, ScheduleVersionRequirement
from app.models.scheduling_input import Event, StaffingRequirement
from app.services.errors import InvalidOperationError

__all__ = [
    "RequirementFingerprint",
    "ScheduleVersionStalenessResult",
    "get_schedule_version_staleness",
]


@dataclass(frozen=True, slots=True)
class RequirementFingerprint:
    """One required position, reduced to the four fields §13 compares.

    Deliberately carries **no** ``staffing_requirement.id``, no
    ``schedule_version_requirement.id``, no ministry or period id, no name and
    no timestamp: a requirement deleted and recreated with the same event,
    date, role and count is *the same requirement* for this purpose, and one
    whose count changed is a *different* one. Adding an id would make the
    first look like a change and is the reason the design compares a business
    tuple rather than rows.

    Frozen (and therefore hashable) because the whole comparison is set
    algebra; it is a value, not an entity.
    """

    event_id: int
    event_date: datetime.date
    ministry_role_id: int
    required_count: int


@dataclass(frozen=True, slots=True)
class ScheduleVersionStalenessResult:
    """Both compared sets, kept whole rather than collapsed to a boolean.

    A finalization gate needs only :attr:`is_stale`, but the operation that
    *refuses* to finalize -- and the UI that later explains why -- needs to say
    which requirements differ. Keeping both sides costs nothing (~65 tuples)
    and is not persisted anywhere: this is a computed answer, recomputed on
    demand.

    The two directional differences are deliberately left as raw sets rather
    than classified into a change taxonomy ("count changed", "event moved").
    A count change legitimately appears as the old tuple in
    :attr:`snapshot_only` *and* the new tuple in :attr:`current_only`, and
    pairing those back up is a presentation concern with more than one
    reasonable answer -- not something this query should decide for every
    caller.
    """

    current_requirements: frozenset[RequirementFingerprint]
    snapshot_requirements: frozenset[RequirementFingerprint]

    @property
    def current_only(self) -> frozenset[RequirementFingerprint]:
        """Required now, but not recorded identically in the snapshot."""
        return self.current_requirements - self.snapshot_requirements

    @property
    def snapshot_only(self) -> frozenset[RequirementFingerprint]:
        """Recorded in the snapshot, but not required identically now."""
        return self.snapshot_requirements - self.current_requirements

    @property
    def is_stale(self) -> bool:
        """Exact set inequality (§13) -- nothing weaker.

        Two empty sets are equal, so a period with no requirements at all
        yields a *fresh* version rather than a stale one; a snapshot that has
        rows the current configuration no longer has (or vice versa) is stale
        in either direction.
        """
        return self.current_requirements != self.snapshot_requirements


def get_schedule_version_staleness(
    session: Session,
    *,
    version: ScheduleVersion,
) -> ScheduleVersionStalenessResult:
    """Compare ``version``'s snapshot against current staffing configuration.

    **Read-only.** This function only calls ``session.execute()`` -- never
    ``add()``, ``delete()``, ``flush()``, ``commit()`` or ``rollback()`` -- and
    opens no session of its own: both queries run on the one supplied, so they
    see the same transactional state as each other and as the caller.

    **Status-agnostic** (§13): a DRAFT, REVIEW, FINALIZED or amended version
    is compared the same way. Whether a given status *may* be stale is a rule
    for the operation applying the gate, not for this description of the
    facts.

    :raises InvalidOperationError: ``version`` is not persisted, or carries no
        ``scheduling_period_id`` -- either would silently query ``NULL`` and
        return an empty, falsely-fresh answer.
    """
    if version.id is None:
        raise InvalidOperationError("version must be persisted (id is None)")
    if version.scheduling_period_id is None:
        raise InvalidOperationError("version must have a scheduling_period_id")

    current = _fetch_current_requirements(
        session, scheduling_period_id=version.scheduling_period_id
    )
    snapshot = _fetch_snapshot_requirements(session, schedule_version_id=version.id)

    return ScheduleVersionStalenessResult(
        current_requirements=current, snapshot_requirements=snapshot
    )


def _fetch_current_requirements(
    session: Session, *, scheduling_period_id: int
) -> frozenset[RequirementFingerprint]:
    """Execution split from statement construction so it is testable with no
    database: orchestration tests monkeypatch this function directly, while
    :func:`_current_requirements_statement` is compiled and inspected on its
    own (see ``tests/test_services_schedule_staleness.py``).
    """
    stmt = _current_requirements_statement(scheduling_period_id)
    return _fingerprints(session.execute(stmt).all())


def _fetch_snapshot_requirements(
    session: Session, *, schedule_version_id: int
) -> frozenset[RequirementFingerprint]:
    """Execution split from statement construction, for the same testing
    reason as :func:`_fetch_current_requirements`.
    """
    stmt = _snapshot_requirements_statement(schedule_version_id)
    return _fingerprints(session.execute(stmt).all())


def _fingerprints(rows: Iterable) -> frozenset[RequirementFingerprint]:
    """Both statements label their four columns identically, so one converter
    serves both sides -- and, more importantly, neither side can drift into
    building a differently-shaped tuple than the other.

    Reading by label rather than by position is what makes that safe: the two
    queries select from different tables in different orders, and a positional
    read would compare correctly right up until someone reordered one of them.
    """
    return frozenset(
        RequirementFingerprint(
            event_id=row.event_id,
            event_date=row.event_date,
            ministry_role_id=row.ministry_role_id,
            required_count=row.required_count,
        )
        for row in rows
    )


def _current_requirements_statement(
    scheduling_period_id: int,
) -> Select[tuple[int, datetime.date, int, int]]:
    """The current side of §13's comparison: ``staffing_requirement`` joined
    to the period's non-cancelled events.

    Scoped by ``Event.scheduling_period_id``, never by ministry alone -- the
    comparison is against *this version's period*, and a ministry's other
    periods are a different schedule's business. ``cancelled_at IS NULL``
    excludes events that will not happen, exactly as the snapshot copy does
    when a version is created (§8), so a version created today against this
    query is fresh against it a moment later.

    ``Event.event_date`` -- the **current** date -- is deliberate here; see the
    module docstring. ``StaffingRequirement.id`` is not selected: the
    comparison is on the business tuple, not on row identity.
    """
    return (
        select(
            StaffingRequirement.event_id.label("event_id"),
            Event.event_date.label("event_date"),
            StaffingRequirement.ministry_role_id.label("ministry_role_id"),
            StaffingRequirement.required_count.label("required_count"),
        )
        .join(Event, Event.id == StaffingRequirement.event_id)
        .where(
            Event.scheduling_period_id == scheduling_period_id,
            Event.cancelled_at.is_(None),
        )
    )


def _snapshot_requirements_statement(
    schedule_version_id: int,
) -> Select[tuple[int, datetime.date, int, int]]:
    """The snapshot side: this version's immutable requirement rows, and
    nothing else.

    **No join to ``event``**, and no ``cancelled_at`` predicate. The snapshot
    is self-sufficient (§8) -- it carries its own ``event_date`` precisely so
    the current row need not be consulted -- and joining ``event`` here would
    either overwrite that historical date with the live one or make cancelled
    events' snapshot rows disappear. Both are the bugs §13 warns about; see
    the module docstring.

    **No reference to ``staffing_requirement``** either: there is deliberately
    no foreign key from a snapshot row back to the mutable input row it was
    copied from, and reaching for one would defeat the point of the copy.
    """
    return select(
        ScheduleVersionRequirement.event_id.label("event_id"),
        ScheduleVersionRequirement.event_date.label("event_date"),
        ScheduleVersionRequirement.ministry_role_id.label("ministry_role_id"),
        ScheduleVersionRequirement.required_count.label("required_count"),
    ).where(ScheduleVersionRequirement.schedule_version_id == schedule_version_id)
