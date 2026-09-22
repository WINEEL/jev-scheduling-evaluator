"""Creating ScheduleVersions: the initial Version 1, and its successors.

Implements the accepted design in
``docs/architecture/schedule-output-data-model.md`` §4, §5, §6, §8 and the
authorization rules in ``docs/architecture/core-data-model.md`` §4.2--§4.3.
Section numbers below refer to the schedule-output document unless stated.

**[APPROVED] Schedule and ScheduleVersion are distinct** (§2, §3, §4): a
Schedule is the ongoing identity for one period's scheduling artifact --
**zero-or-one per period** -- and carries no state of its own; a ScheduleVersion
is a meaningful, preserved state of it. This service establishes both for the
first time: the Schedule if none exists yet, and always exactly one
ScheduleVersion -- **Version 1, status DRAFT**.

**[APPROVED] Availability must be locked first** (Task 19, this task). A
version's requirement snapshot is meaningful only against a stable, closed
input set; creating one while ``scheduling_period.availability_locked_at`` is
still ``NULL`` would let ordinary Availability edits keep changing underneath
a version that is supposed to be a preserved state. Locking is
:mod:`app.services.scheduling_period`'s own operation and is never triggered
here.

**[APPROVED] The requirement snapshot is immutable and self-sufficient** (§8):
written once, in the same transaction as the version, copying
``staffing_requirement`` rows for the period's **non-cancelled** events. There
is deliberately **no foreign key from the snapshot back to
``staffing_requirement``**, and this service does not add one by accident --
the whole point is that the mutable input row stays editable and deletable
forever after, whatever versions have already been built from it.

**Creating a version is an operational write** (core §4.2--§4.3, §4.3.1):
:func:`app.services.authorization.require_ministry_operator` -- this ministry's
active Head, and nobody else. An Admin who heads nothing may read every version
this creates (:func:`app.services.authorization.require_ministry_reader`) and
may create none of them.

**Two creation operations, sharing one snapshot rule.**
:func:`create_initial_schedule_version` establishes a Schedule and its Version
1; :func:`create_successor_schedule_version` creates Version N+1 from the
current latest version, for a stale DRAFT/REVIEW that needs replacing or a
FINALIZED version that needs amending (§13, §14). Both take their snapshot
through :func:`_copy_requirement_snapshot`, so there is exactly one definition
of "current requirement state" here -- two subtly different ones would be a
genuine hazard, since Task 23 compares versions against precisely this rule.

**Neither operation ever rewrites an existing version.** The initial one
refuses outright if the schedule already has a version; the successor leaves
its source completely untouched and relies on the latest-version rule (Tasks
22/24/27) to make that source immutable from then on. Assignment
carry-forward, the solver and finalization are all deliberately elsewhere.
"""

from __future__ import annotations

import datetime
from typing import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.core import Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Schedule,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import Event, SchedulingPeriod, StaffingRequirement
from app.services.audit import (
    ACTION_SCHEDULE_CREATED,
    ACTION_SCHEDULE_VERSION_CREATED,
    record_audit_event,
)
from app.services.authorization import require_ministry_operator
from app.services.errors import InvalidOperationError

_SCHEDULE_TARGET_TABLE = "schedule"
_VERSION_TARGET_TABLE = "schedule_version"

#: The statuses a version may legitimately hold (§6). A successor may be
#: created from any of them; anything else is data this service refuses to
#: reason about rather than guess at.
_RECOGNIZED_VERSION_STATUSES = (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_REVIEW,
    SCHEDULE_VERSION_STATUS_FINALIZED,
)


def create_initial_schedule_version(
    session: Session,
    *,
    actor: Person,
    period: SchedulingPeriod,
    notes: str | None = None,
) -> ScheduleVersion:
    """Create ``period``'s Schedule if needed, and its Version 1, as ``actor``.

    **Not idempotent -- deliberately.** Unlike every setter elsewhere in this
    package, a second call against a Schedule that already has a version is a
    genuine error (:class:`InvalidOperationError`), not a no-op: there is no
    well-defined "unchanged" request here, only "create the first version" or
    "don't". Reusing an existing, version-less Schedule is fine and expected
    (§4) -- only a schedule with an existing version refuses.

    **Sequence, and why it is ordered this way:**

    1. Authorize, then confirm availability is locked -- both are gates on
       whether this call may proceed at all, checked before anything else,
       including input formatting.
    2. Validate ``notes``.
    3. Find or create the Schedule. A newly created Schedule is flushed
       immediately (its id is needed for the version) and audited on the spot,
       before anything about the version is decided -- if the schedule already
       has a version, the error is raised for a schedule that is otherwise
       untouched, and the audit trail for a freshly created Schedule reflects
       reality even if version creation goes on to fail for some other reason.
       **When the Schedule already exists**, this is also where its version
       count is checked -- deliberately not checked at all for a schedule this
       call just created, since a schedule with no rows written to it yet
       cannot possibly already have one.
    4. Create Version 1 and flush -- its id is needed by every snapshot row.
    5. Copy the current requirement snapshot (§8) -- read and write inside this
       same transaction, so the snapshot corresponds to exactly the database
       state this call is running against.
    6. Audit the version, including the snapshot row count as context.

    **Flushes: at most two** -- one for a newly created Schedule, one for the
    new Version -- never combined into one call. The brief this was built
    against explicitly asks not to invent flush choreography to shave one off;
    two independent, unconditional-looking flushes read more clearly than a
    conditional batch that only sometimes includes the Schedule. **Snapshot
    rows are never flushed here at all** -- nothing in this function needs
    their identities, and the owning transaction will flush them when it
    commits.

    Everything -- the Schedule (if new), the Version, every snapshot row, and
    both audit rows -- is added to the same ``session`` and written by the
    caller's eventual commit, never here. This function never commits or rolls
    back; if anything after the Schedule's own audit fails, the exception
    propagates and the caller's transaction rolls back the whole attempt,
    Schedule included.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``period.ministry_id``.
    :raises InvalidOperationError: ``period``'s availability is still open;
        ``notes`` was supplied but blank; or the Schedule for this period
        already has a version.
    """
    require_ministry_operator(actor, ministry_id=period.ministry_id)

    if period.availability_locked_at is None:
        raise InvalidOperationError(
            "cannot create the initial schedule version while this period's"
            " availability is still open -- lock it first"
        )

    notes = _validate_optional_notes(notes)

    schedule = _find_existing_schedule(session, scheduling_period_id=period.id)
    if schedule is None:
        schedule = Schedule(scheduling_period_id=period.id)
        session.add(schedule)
        # The version below needs a real schedule_id; a new identity bigint
        # does not exist until the INSERT actually runs. Scoped to the one
        # pending row that needs it.
        session.flush([schedule])

        record_audit_event(
            session,
            actor=actor,
            action=ACTION_SCHEDULE_CREATED,
            target_table=_SCHEDULE_TARGET_TABLE,
            target_id=schedule.id,
            ministry_id=period.ministry_id,
            summary=f"Created schedule for {period.name}",
            after_values={"scheduling_period_id": period.id},
        )
    elif _schedule_has_any_version(session, schedule_id=schedule.id):
        raise InvalidOperationError(
            "this schedule already has a version;"
            " successor-version creation is a separate operation"
        )

    version = ScheduleVersion(
        schedule_id=schedule.id,
        scheduling_period_id=period.id,
        version_number=1,
        status=SCHEDULE_VERSION_STATUS_DRAFT,
        finalized_at=None,
        amends_version_id=None,
        amendment_reason=None,
        notes=notes,
    )
    session.add(version)
    # The snapshot rows below don't need the version's identity -- they only
    # need to be added to the same session -- but the audit row that follows
    # does need target_id, and a new identity bigint does not exist until the
    # INSERT runs.
    session.flush([version])

    snapshot_rows = _copy_requirement_snapshot(
        session, scheduling_period_id=period.id, version=version
    )

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SCHEDULE_VERSION_CREATED,
        target_table=_VERSION_TARGET_TABLE,
        target_id=version.id,
        ministry_id=period.ministry_id,
        summary=f"Created draft version 1 for {period.name}",
        # Only meaningful creation context, never the whole ORM row (audit
        # §7.2). requirement_snapshot_count is not a ScheduleVersion column --
        # it is useful history about what this version was built against, and
        # belongs in the payload rather than nowhere.
        after_values={
            "schedule_id": schedule.id,
            "scheduling_period_id": period.id,
            "version_number": 1,
            "status": SCHEDULE_VERSION_STATUS_DRAFT,
            "notes": notes,
            "requirement_snapshot_count": len(snapshot_rows),
        },
    )
    return version


def create_successor_schedule_version(
    session: Session,
    *,
    actor: Person,
    source_version: ScheduleVersion,
    amendment_reason: str,
    notes: str | None = None,
) -> ScheduleVersion:
    """Create the next DRAFT version of ``source_version``'s schedule.

    **[APPROVED] History is never rewritten; it is superseded** (§8, §13, §14).
    When staffing changes, an event moves, or a finalized schedule needs
    amending, the answer is a *new* version with a *fresh* snapshot -- never an
    edit to a version that already exists. Rewriting a draft's snapshot would
    change what the head was looking at underneath them; rewriting a finalized
    one would change what the church was told. So this function touches
    ``source_version`` in no way at all: its status, ``finalized_at``, notes,
    snapshot rows and assignments are all exactly as they were afterwards.
    What makes it historical is simply that a higher-numbered version now
    exists -- the latest-version rule Tasks 22/24/27 already enforce turns that
    into immutability without anything here setting a flag.

    **Any recognized source status may spawn a successor**, for different but
    equally real reasons: a stale DRAFT or REVIEW needs replacing rather than
    patching, and a FINALIZED version needs a successor to amend it (§14).

    **[APPROVED] The snapshot is taken from current input, not copied** (§8).
    This is the whole point of creating a version: the successor captures
    ``staffing_requirement`` joined to the period's non-cancelled events *as
    they stand now*, through the same
    :func:`_copy_requirement_snapshot` the initial version uses. A count
    changed from 1 to 2 is 1 in version 1 and 2 in version 2, forever; a moved
    event keeps its old date in the old snapshot and takes the new one here; a
    cancelled event simply does not appear.

    **The successor starts with zero Assignments, deliberately.** Carrying
    people forward is not a copy operation -- every carried row would need
    re-validating against current qualification, availability and church-wide
    conflicts, and would need its own honest audit history rather than an
    inherited ``is_override`` and someone else's override reason. That is a
    separate, deliberate operation (Task 29); doing it implicitly here would
    silently reproduce authorizations nobody granted for this version.

    **Creating a successor does not move authority** (ADR 0003). The
    authoritative version is the highest-numbered *FINALIZED* one, so a new
    DRAFT changes nothing for any other ministry's conflict query: version 1
    stays authoritative until version 2 is itself finalized (Task 27). No
    pointer is stored and no status is touched to make that true.

    **Flushes: exactly one**, for the new version, because both the snapshot
    rows' ``schedule_version_id`` and the audit row's ``target_id`` need its
    identity. The snapshot rows are never flushed individually -- they are
    added and left for the caller's commit, like every other batch in this
    project. This function never commits or rolls back; if anything after the
    flush fails, the exception propagates and the caller's transaction
    discards the version, its snapshot and its audit row together, with no
    hand-rolled cleanup here.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of the source version's Ministry.
    :raises InvalidOperationError: ``source_version`` lacks the persisted
        context needed to resolve its Ministry; ``amendment_reason`` is blank;
        ``notes`` was supplied but blank; the source's status is
        unrecognized; or a newer version of the same schedule already exists.
    """
    period = _resolve_source_period(session, source_version)
    require_ministry_operator(actor, ministry_id=period.ministry_id)

    # Standing domain state on the new row, not an explanation of this audit
    # operation -- so it is required, and it is never mirrored into
    # AuditEvent.reason (see the audit call below).
    amendment_reason = _validate_amendment_reason(amendment_reason)
    notes = _validate_optional_notes(notes)

    if source_version.status not in _RECOGNIZED_VERSION_STATUSES:
        raise InvalidOperationError(
            f"cannot create a successor to a version with status"
            f" {source_version.status!r}"
        )
    if _newer_version_exists(
        session,
        schedule_id=source_version.schedule_id,
        version_number=source_version.version_number,
    ):
        raise InvalidOperationError(
            "cannot create a successor to a schedule version that has already"
            " been superseded by a newer version; branch from the latest one"
        )

    version_number = source_version.version_number + 1
    version = ScheduleVersion(
        schedule_id=source_version.schedule_id,
        scheduling_period_id=source_version.scheduling_period_id,
        version_number=version_number,
        status=SCHEDULE_VERSION_STATUS_DRAFT,
        finalized_at=None,
        amends_version_id=source_version.id,
        amendment_reason=amendment_reason,
        # Never inherited: notes are one head's commentary on one version, and
        # silently carrying them forward would attribute them to a version
        # they were not written about.
        notes=notes,
    )
    session.add(version)
    # The one identity-producing flush: the snapshot rows below and the audit
    # row both need this version's id, and a new identity bigint does not
    # exist until the INSERT runs.
    session.flush([version])

    snapshot_rows = _copy_requirement_snapshot(
        session, scheduling_period_id=source_version.scheduling_period_id, version=version
    )

    record_audit_event(
        session,
        actor=actor,
        # The same action as the initial version: this *is* a version being
        # created, and a separate "successor created" name would split one
        # concept across two vocabularies for no reader's benefit. The
        # payload's amends_version_id is what distinguishes the two.
        action=ACTION_SCHEDULE_VERSION_CREATED,
        target_table=_VERSION_TARGET_TABLE,
        target_id=version.id,
        ministry_id=period.ministry_id,
        # period.name need not embed the Ministry's own name, so both are read
        # explicitly (Task 24's correction).
        summary=(
            f"Created draft version {version_number} for"
            f" {period.ministry.name} {period.name}"
        ),
        # Deliberately None. amendment_reason is standing state on the version
        # itself, readable there forever; copying it here would present one
        # fact as two and invite them to disagree.
        reason=None,
        after_values={
            "schedule_id": source_version.schedule_id,
            "scheduling_period_id": source_version.scheduling_period_id,
            "version_number": version_number,
            "status": SCHEDULE_VERSION_STATUS_DRAFT,
            "notes": notes,
            "amends_version_id": source_version.id,
            "amendment_reason": amendment_reason,
            "requirement_snapshot_count": len(snapshot_rows),
        },
    )
    return version


def _resolve_source_period(
    session: Session, source_version: ScheduleVersion
) -> SchedulingPeriod:
    """The source version's owning ``SchedulingPeriod``, fetched fresh.

    ``ScheduleVersion`` carries no ``ministry_id`` of its own and has no
    ``scheduling_period`` relationship, so this one query is what makes
    authorization and the audit ministry/summary possible.

    Every field the operation depends on is guarded explicitly rather than
    left to fail confusingly later: ``id`` becomes ``amends_version_id``,
    ``schedule_id`` and ``version_number`` drive the latest-version check and
    the new number, and ``scheduling_period_id`` is both this lookup and the
    snapshot's scope.

    :mod:`app.services.schedule_lifecycle` has a similar local helper. It is
    deliberately not imported across modules: it is that module's private
    detail, and this one guards a different field set for different reasons.
    """
    if source_version.id is None:
        raise InvalidOperationError("source version must be persisted (id is None)")
    if source_version.schedule_id is None:
        raise InvalidOperationError("source version must have a schedule_id")
    if source_version.scheduling_period_id is None:
        raise InvalidOperationError("source version must have a scheduling_period_id")
    if source_version.version_number is None:
        raise InvalidOperationError("source version must have a version_number")

    stmt = _scheduling_period_lookup_statement(source_version.scheduling_period_id)
    period = session.execute(stmt).scalar_one_or_none()
    if period is None:
        raise InvalidOperationError(
            "source version's scheduling period could not be resolved"
        )
    return period


def _scheduling_period_lookup_statement(
    scheduling_period_id: int,
) -> Select[tuple[SchedulingPeriod]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_schedule_version.py``).
    """
    return select(SchedulingPeriod).where(SchedulingPeriod.id == scheduling_period_id)


def _newer_version_exists_statement(schedule_id: int, version_number: int) -> Select[tuple[int]]:
    """A single-column ``LIMIT 1`` existence probe -- the same shape Tasks
    22/24/27 use, kept local rather than imported from another module's
    private namespace (module scope). "Latest" is a fact about the schedule,
    living on other rows, so it is always queried fresh and never read off a
    relationship collection the caller may hold stale.
    """
    return (
        select(ScheduleVersion.id)
        .where(
            ScheduleVersion.schedule_id == schedule_id,
            ScheduleVersion.version_number > version_number,
        )
        .limit(1)
    )


def _newer_version_exists(session: Session, *, schedule_id: int, version_number: int) -> bool:
    stmt = _newer_version_exists_statement(schedule_id, version_number)
    return session.execute(stmt).scalar_one_or_none() is not None


def _validate_amendment_reason(amendment_reason: str) -> str:
    """Required and meaningful.

    Mirrors the database's own ``amendment_reason_required`` CHECK -- which
    fires whenever ``amends_version_id`` is set -- restated here so a caller
    gets a domain error instead of an ``IntegrityError`` at commit time, far
    from its cause and with the whole transaction already aborted (§14).
    """
    if amendment_reason is None or not amendment_reason.strip():
        raise InvalidOperationError("amendment_reason must not be blank")
    return amendment_reason


def _copy_requirement_snapshot(
    session: Session, *, scheduling_period_id: int, version: ScheduleVersion
) -> list[ScheduleVersionRequirement]:
    """Write §8's immutable snapshot: one row per current, non-cancelled
    ``staffing_requirement`` for this period's events.

    **The one definition of "current requirement state" in this module**, used
    by both the initial version and every successor. Two subtly different
    copies would be the worst possible outcome here: a successor built from a
    slightly different rule than Task 23 compares against would be born stale,
    or -- worse -- look fresh while carrying something else.

    Takes the period's id rather than the object because a successor has only
    ``source_version.scheduling_period_id`` to work from, and nothing in here
    ever needed more than the id.

    **No per-row audit event.** ~65 rows for one internal copy operation would
    add volume, not history a human would ever want to read (module scope).
    **No flush.** Nothing in this function needs a snapshot row's identity;
    the caller's eventual commit is what needs them to exist.
    """
    source_rows = _current_requirement_snapshot_source(
        session, scheduling_period_id=scheduling_period_id
    )
    snapshot_rows = [
        ScheduleVersionRequirement(
            schedule_version_id=version.id,
            event_id=row.event_id,
            # The event's date *as it stood right now* -- copied as a plain
            # value, never ORM-linked back to Event.event_date. Divergence
            # later is the entire point of this column (model docstring).
            event_date=row.event_date,
            ministry_role_id=row.ministry_role_id,
            scheduling_period_id=row.scheduling_period_id,
            # sr.ministry_id, not a fresh lookup: StaffingRequirement's own
            # composite foreign key already guarantees it agrees with the
            # event's ministry, so re-deriving it here would be a redundant
            # query proving a fact the database already enforces.
            ministry_id=row.ministry_id,
            required_count=row.required_count,
        )
        for row in source_rows
    ]
    for snapshot_row in snapshot_rows:
        session.add(snapshot_row)
    return snapshot_rows


def _validate_optional_notes(notes: str | None) -> str | None:
    """A note is optional, but blank is not a note.

    Not a database rule -- ``schedule_version.notes`` carries no CHECK -- but
    the same service-layer convention every other optional free-text field in
    this project follows (``reason`` throughout Tasks 13--19): supplied means
    meaningful, so a caller cannot store whitespace that looks like content
    but carries none.
    """
    if notes is None:
        return None
    if not notes.strip():
        raise InvalidOperationError("notes must not be blank when supplied")
    return notes


def _schedule_lookup_statement(scheduling_period_id: int) -> Select[tuple[Schedule]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_schedule_version.py``).
    """
    return select(Schedule).where(Schedule.scheduling_period_id == scheduling_period_id)


def _find_existing_schedule(
    session: Session, *, scheduling_period_id: int
) -> Schedule | None:
    """The standing Schedule for this period, if one exists.

    Queried on ``scheduling_period_id`` -- the exact column
    ``uq_schedule_scheduling_period_id`` is built from (§4) -- via a plain
    SQLAlchemy ``select()``, no repository abstraction.
    """
    stmt = _schedule_lookup_statement(scheduling_period_id)
    return session.execute(stmt).scalar_one_or_none()


def _any_version_exists_statement(schedule_id: int) -> Select[tuple[int]]:
    """The query, split from its execution for the same testing reason as
    :func:`_schedule_lookup_statement`. A single-column, ``LIMIT 1`` existence
    probe -- there is never a reason to load a whole ``ScheduleVersion`` row
    just to learn whether one exists.
    """
    return select(ScheduleVersion.id).where(
        ScheduleVersion.schedule_id == schedule_id
    ).limit(1)


def _schedule_has_any_version(session: Session, *, schedule_id: int) -> bool:
    """Whether ``schedule_id`` already has at least one version.

    A fresh query, not an inference from ``schedule.versions`` -- this service
    cannot honestly guarantee that collection is loaded or current for a
    Schedule the caller may have held onto from elsewhere.
    """
    stmt = _any_version_exists_statement(schedule_id)
    return session.execute(stmt).scalar_one_or_none() is not None


def _requirement_snapshot_source_statement(
    scheduling_period_id: int,
) -> Select[tuple[int, datetime.date, int, int, int, int]]:
    """The exact join the design document specifies (§8, "How it is written"):
    current ``staffing_requirement`` rows for this period's **non-cancelled**
    events, restricted by the query shape itself rather than by re-querying
    each row's integrity afterward.

    Columns, in order: ``event_id``, ``event_date``, ``ministry_role_id``,
    ``scheduling_period_id``, ``ministry_id``, ``required_count`` -- exactly
    what :func:`_copy_requirement_snapshot` needs to build one
    :class:`ScheduleVersionRequirement` per row.
    """
    return (
        select(
            StaffingRequirement.event_id,
            Event.event_date,
            StaffingRequirement.ministry_role_id,
            Event.scheduling_period_id,
            StaffingRequirement.ministry_id,
            StaffingRequirement.required_count,
        )
        .join(Event, Event.id == StaffingRequirement.event_id)
        .where(
            Event.scheduling_period_id == scheduling_period_id,
            Event.cancelled_at.is_(None),
        )
    )


def _current_requirement_snapshot_source(
    session: Session, *, scheduling_period_id: int
) -> Sequence:
    stmt = _requirement_snapshot_source_statement(scheduling_period_id)
    return session.execute(stmt).all()
