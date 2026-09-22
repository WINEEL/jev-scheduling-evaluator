"""Creating Scheduling Periods and generating their ordinary Sunday events.

Implements the accepted design in
``docs/architecture/scheduling-input-data-model.md`` §3--§5 and the
authorization rules in ``docs/architecture/core-data-model.md`` §4.2--§4.3.
Section numbers below refer to the scheduling-input document unless stated.

**[APPROVED] This is a Ministry-Head-scoped pair of services** (core
§4.2--§4.3, §4.3.1), on the same footing as
:mod:`app.services.role_qualification`: an actor may write here only as an
active Ministry Head *of the ministry the period belongs to*, while listing a
period's events is an oversight read an Admin also gets. Both write operations
independently authorize -- ``generate_sunday_events`` does not trust that
whoever holds a ``SchedulingPeriod`` object was authorized to create it. The
checks are :func:`app.services.authorization.require_ministry_operator` and
:func:`app.services.authorization.require_ministry_reader`, shared with every
other ministry-scoped service since Task 16 and split by read/write in Task 80.

**[APPROVED] Availability is open by creation, not by a separate step**
(§3). A new period's ``availability_locked_at`` is ``NULL`` because nothing
here ever sets it; there is deliberately no status/workflow column alongside
it, and locking is out of this task's scope entirely.

**[APPROVED] Overlapping periods for one ministry are not rejected** (§3, §4).
Task 7 accepted this at the database level as a workflow concern rather than a
corruption, and no service-level prohibition has been approved since -- so
none is added here, even though it would be easy to.

**No recurrence is stored.** Generated Sundays are ordinary, independent
``event`` rows (§5); nothing here writes a rule that could regenerate or
reinterpret them later. Running the generator again is the only mechanism for
"more Sundays," and it is idempotent (see :func:`generate_sunday_events`).

**[APPROVED] The Availability lifecycle is exactly two states, both read off
one nullable timestamp** (§3, Task 19): ``availability_locked_at IS NULL`` is
open, non-null is locked. There is no Draft / Review / Finalized here --
those describe a *produced schedule* and belong to the future ScheduleVersion,
which is precisely why a single workflow column on this table could never have
expressed both at once. :func:`lock_availability` is the one operation this
slice adds, moving Open -> Locked. **There is deliberately no unlock/reopen
operation.** Once :class:`~app.models.scheduling_input.Availability` rows may
already have fed a produced schedule, reopening collection is not the reverse
of locking it -- it is a decision with consequences for whatever was built on
top, and designing that deliberately is explicitly out of this task's scope.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, Person
from app.models.scheduling_input import EVENT_KIND_SUNDAY_SERVICE, Event, SchedulingPeriod
from app.services.audit import (
    ACTION_AVAILABILITY_LOCKED,
    ACTION_EVENT_CREATED,
    ACTION_SCHEDULING_PERIOD_CREATED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError

_PERIOD_TARGET_TABLE = "scheduling_period"
_EVENT_TARGET_TABLE = "event"


def create_scheduling_period(
    session: Session,
    *,
    actor: Person,
    ministry: Ministry,
    name: str,
    start_date: datetime.date,
    end_date: datetime.date,
) -> SchedulingPeriod:
    """Create a new :class:`SchedulingPeriod` for ``ministry``, as ``actor``.

    Validated, in order, before anything is mutated: actor authorization,
    ``ministry`` being active, ``name`` non-blank once trimmed,
    ``start_date <= end_date``, and finally that no period in this ministry
    already has this name case-insensitively -- proactively, so the caller
    receives an :class:`InvalidOperationError` rather than the database's
    own ``uq_scheduling_period_ministry_id_name_lower`` rejecting the insert as
    an ``IntegrityError`` far from its cause.

    **Overlap with an existing period is deliberately not checked.** §3/§4 of
    the design accept overlap at the database level as a workflow concern, not
    a corruption, and no service-level rule against it has been approved --
    adding one here would be redesigning accepted scope.

    Availability starts open: ``availability_locked_at`` is left ``NULL``
    because nothing here sets it, and there is no separate lifecycle/status
    column to initialize (§3).

    The row is added to ``session`` and flushed once -- the minimum needed to
    obtain its identity before the audit row that must reference it can be
    built -- and is written by the caller's eventual commit, not here.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``ministry``.
    :raises InvalidOperationError: ``ministry`` is deactivated; ``name`` is
        blank; ``start_date`` is after ``end_date``; or a period with this
        name already exists in this ministry.
    """
    require_ministry_operator(actor, ministry_id=ministry.id)

    if ministry.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot create a scheduling period for a deactivated ministry"
        )

    name = _require_non_blank(name, "name")

    if start_date > end_date:
        raise InvalidOperationError("start_date must not be after end_date")

    if _find_period_by_name(session, ministry_id=ministry.id, name=name) is not None:
        raise InvalidOperationError(
            f"a scheduling period named {name!r} already exists for this ministry"
        )

    period = SchedulingPeriod(
        ministry_id=ministry.id,
        name=name,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(period)
    # The audit row below needs a real target_id; a new identity bigint does
    # not exist until the INSERT actually runs. Scoped to the one pending row
    # that needs it -- see app.services' module docstring on why this does not
    # compromise the caller-owned transaction.
    session.flush([period])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SCHEDULING_PERIOD_CREATED,
        target_table=_PERIOD_TARGET_TABLE,
        target_id=period.id,
        ministry_id=ministry.id,
        summary=f"Created scheduling period {name} for {ministry.name}",
        # Only meaningful business state -- never the whole ORM row (audit
        # §7.2). Dates are ISO strings: JSONB has no native date type, and an
        # audit payload is a historical record read back as plain JSON, not a
        # Python object.
        after_values={
            "name": name,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "availability_locked_at": None,
        },
    )
    return period


def generate_sunday_events(
    session: Session,
    *,
    actor: Person,
    period: SchedulingPeriod,
) -> list[Event]:
    """Create the ordinary Sunday-service events for ``period``, as ``actor``.

    One row per calendar Sunday in ``[period.start_date, period.end_date]``
    (both ends included; see :func:`_sundays_between`) that this period does
    not already have an ordinary Sunday-service event for.

    **Idempotent, as a property of this generator specifically -- not a
    database rule.** The schema deliberately allows several events for one
    ministry on one date (§4: a morning and an evening service each need their
    own crew), so nothing here adds a uniqueness constraint. Instead, before
    creating anything, this function looks up which Sundays this period
    already has an ``EVENT_KIND_SUNDAY_SERVICE`` event for -- **cancelled or
    not**, since a cancelled Sunday event still means "this Sunday was already
    generated" and must not be silently recreated -- and skips those dates. A
    ``SPECIAL`` event on the same Sunday is not a Sunday-service event and
    never counts as a duplicate; a special evening service and the ordinary
    Sunday morning service coexist as two rows, exactly as the schema intends.

    Running this twice with nothing changed in between creates **zero** rows,
    **zero** audit events, and calls ``session.flush()`` **zero** times: the
    duplicate check happens before anything is added to the session, so there
    is nothing pending to flush.

    All newly created ``Event`` rows are added first and flushed **once**, as
    a batch, so every new row's identity is available before any audit row is
    built -- not one flush per event. Each new event then gets exactly one
    ``AuditEvent``; there is no additional summary/batch row.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``period``'s ministry.
    """
    require_ministry_operator(actor, ministry_id=period.ministry_id)

    sundays = _sundays_between(period.start_date, period.end_date)
    if not sundays:
        return []

    already_generated = _existing_sunday_service_dates(session, period)
    missing_sundays = [d for d in sundays if d not in already_generated]
    if not missing_sundays:
        return []

    new_events = [
        Event(
            scheduling_period_id=period.id,
            ministry_id=period.ministry_id,
            event_date=sunday,
            event_kind=EVENT_KIND_SUNDAY_SERVICE,
        )
        for sunday in missing_sundays
    ]
    for event in new_events:
        session.add(event)
    # One batch flush for every new row this call created, not one per event
    # -- the identities are all that is needed before the audit rows below,
    # and SQLAlchemy can insert the whole batch in a single flush.
    session.flush(new_events)

    for event in new_events:
        record_audit_event(
            session,
            actor=actor,
            action=ACTION_EVENT_CREATED,
            target_table=_EVENT_TARGET_TABLE,
            target_id=event.id,
            ministry_id=period.ministry_id,
            summary=(
                f"Created Sunday service event for {period.ministry.name}"
                f" on {event.event_date.isoformat()}"
            ),
            # Only meaningful business fields (audit §7.2). ``name`` is
            # omitted rather than recorded as null: it is not applicable to a
            # SUNDAY_SERVICE event at all (it exists only for SPECIAL events),
            # which is different from a field that has no prior value yet.
            after_values={
                "scheduling_period_id": period.id,
                "event_date": event.event_date.isoformat(),
                "event_kind": event.event_kind,
            },
        )

    return new_events


def lock_availability(
    session: Session,
    *,
    actor: Person,
    period: SchedulingPeriod,
    reason: str | None = None,
) -> SchedulingPeriod:
    """Move ``period`` from Open to Locked, as ``actor``.

    **Idempotent.** If ``period.availability_locked_at`` is already set, it is
    returned unchanged: no ``AuditEvent``, the original lock instant is never
    rewritten, and nothing is flushed -- there is nothing pending, since the
    period already has its identity and no attribute was touched. Authorization
    is still checked first, even on this no-op path, matching every other
    service in this project: whether a call changes anything is a separate
    question from whether the caller was allowed to ask.

    **What locking does not require.** No completeness check of any kind runs
    here -- not every member having answered, not every Event having an
    ``Availability`` row, not staffing, not a minimum number of Events, not
    even that the period contains a Sunday. "No response" is itself a valid,
    permanent input state in the accepted model (scheduling-input §8); locking
    only freezes the moment ordinary Availability edits stop being accepted
    (:mod:`app.services.availability`), and says nothing about whether what was
    collected is complete. Any future policy for what an unanswered Sunday
    *means* is a solver-input transformation, not a precondition on locking.

    The timestamp is timezone-aware UTC, matching every other timestamp in
    this project (core §3.2). **No flush**: unlike creating a new row, this
    mutates an attribute on a period the caller already holds, which already
    has its identity -- there is nothing an identity-only flush would obtain
    here. This function never commits or rolls back; if recording the audit
    event fails after the timestamp has been set in memory, the exception
    propagates so the caller's transaction rolls back the whole thing,
    including the timestamp change -- this function does not attempt to
    manually undo it.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``period``'s ministry.
    :raises InvalidOperationError: ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=period.ministry_id)
    reason = _validate_optional_reason(reason)

    if period.availability_locked_at is not None:
        return period

    locked_at = _now_utc()
    period.availability_locked_at = locked_at

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_AVAILABILITY_LOCKED,
        target_table=_PERIOD_TARGET_TABLE,
        target_id=period.id,
        ministry_id=period.ministry_id,
        summary=f"Locked availability for {period.name}",
        reason=reason,
        # Only the one changed business field (audit §7.2). A JSON-compatible
        # ISO-8601 string, not a Python datetime: JSONB has no native
        # timestamp type, and an audit payload is a historical record read
        # back as plain JSON.
        before_values={"availability_locked_at": None},
        after_values={"availability_locked_at": locked_at.isoformat()},
    )
    return period


@dataclass(frozen=True, slots=True)
class EventSummary:
    """One event of a period, for a head choosing which one to staff.

    Not a general Event read model -- just enough to list and identify one
    (Task 54's own reason for existing: staffing management is per-event, and
    nothing before this task exposed a period's events at all). Creating,
    editing or cancelling an event is not this function's business.
    """

    event_id: int
    event_date: datetime.date
    #: ``None`` for an ordinary Sunday service; set for a SPECIAL event.
    event_name: str | None
    event_kind: str
    #: Shown, not filtered out: a head choosing an event should be able to
    #: see a cancelled one and understand why setting staffing on it will be
    #: refused, rather than have it silently vanish from the list.
    cancelled_at: datetime.datetime | None


def list_period_events(
    session: Session, *, actor: Person, period: SchedulingPeriod
) -> tuple[EventSummary, ...]:
    """``period``'s events, in date order, for someone allowed to manage it.

    **Read-only.** Ordered by ``event_date`` then ``id``, so two events on one
    Sunday still come back in a stable, deterministic order.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``period.ministry_id``.
    """
    require_ministry_reader(actor, ministry_id=period.ministry_id)

    rows = session.execute(_period_events_statement(period.id)).all()
    return tuple(
        EventSummary(
            event_id=row.id, event_date=row.event_date, event_name=row.name,
            event_kind=row.event_kind, cancelled_at=row.cancelled_at,
        )
        for row in rows
    )


def _period_events_statement(scheduling_period_id: int) -> Select:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_scheduling_period.py``).
    """
    return (
        select(
            Event.id, Event.event_date, Event.name, Event.event_kind,
            Event.cancelled_at,
        )
        .where(Event.scheduling_period_id == scheduling_period_id)
        .order_by(Event.event_date, Event.id)
    )


def _now_utc() -> datetime.datetime:
    """The current instant, timezone-aware in UTC (core §3.2).

    A one-line indirection, not a clock framework: this is the only function
    in the project so far that needs a substitutable "now" for deterministic
    testing (see ``tests/test_services_scheduling_period.py``), and one small
    function is all that need.
    """
    return datetime.datetime.now(datetime.timezone.utc)


def _validate_optional_reason(reason: str | None) -> str | None:
    """A reason is optional here, but blank is not a reason.

    [REVIEWED] audit §9: no new universal reason requirement is invented for
    locking availability; a reason may be supplied and is recorded when it is.
    """
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


def _sundays_between(
    start_date: datetime.date, end_date: datetime.date
) -> list[datetime.date]:
    """Every calendar Sunday in ``[start_date, end_date]``, both ends included.

    A small pure function, deliberately: it is tested directly, with no
    Session and no database, and it is the entire "recurrence rule" this
    slice has -- computed on demand, never stored (module docstring).

    ``date.weekday()`` is Monday=0 ... Sunday=6, so ``6 - start_date.weekday()``
    is the number of days forward to the first Sunday on or after
    ``start_date`` (0 if ``start_date`` is itself a Sunday); the ``% 7`` keeps
    that non-negative for every weekday.
    """
    if start_date > end_date:
        return []

    days_to_first_sunday = (6 - start_date.weekday()) % 7
    sunday = start_date + datetime.timedelta(days=days_to_first_sunday)

    sundays: list[datetime.date] = []
    while sunday <= end_date:
        sundays.append(sunday)
        sunday += datetime.timedelta(days=7)
    return sundays


def _require_non_blank(value: str, field: str) -> str:
    if not value.strip():
        raise InvalidOperationError(f"{field} must not be blank")
    return value


def _period_name_lookup_statement(
    ministry_id: int, name: str
) -> Select[tuple[SchedulingPeriod]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_scheduling_period.py``).

    Mirrors the database's own case-insensitive uniqueness index,
    ``uq_scheduling_period_ministry_id_name_lower`` -- ``func.lower`` on both
    sides is what makes this a true case-insensitive match rather than an
    exact one.
    """
    return select(SchedulingPeriod).where(
        SchedulingPeriod.ministry_id == ministry_id,
        func.lower(SchedulingPeriod.name) == name.lower(),
    )


def _find_period_by_name(
    session: Session, *, ministry_id: int, name: str
) -> SchedulingPeriod | None:
    stmt = _period_name_lookup_statement(ministry_id, name)
    return session.execute(stmt).scalar_one_or_none()


def _existing_sunday_service_dates_statement(
    scheduling_period_id: int,
) -> Select[tuple[datetime.date]]:
    """The query, split from its execution for the same testing reason as
    :func:`_period_name_lookup_statement`.

    Deliberately filters on ``event_kind`` alone, with **no** predicate on
    ``cancelled_at``: a cancelled Sunday-service event still means this Sunday
    was already generated (module docstring), and a ``SPECIAL`` event must
    never be mistaken for one.
    """
    return select(Event.event_date).where(
        Event.scheduling_period_id == scheduling_period_id,
        Event.event_kind == EVENT_KIND_SUNDAY_SERVICE,
    )


def _existing_sunday_service_dates(
    session: Session, period: SchedulingPeriod
) -> set[datetime.date]:
    stmt = _existing_sunday_service_dates_statement(period.id)
    return set(session.execute(stmt).scalars().all())
