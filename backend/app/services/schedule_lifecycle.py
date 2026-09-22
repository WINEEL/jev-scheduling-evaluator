"""The ScheduleVersion lifecycle: DRAFT -> REVIEW, and REVIEW -> FINALIZED.

Implements the accepted design in
``docs/architecture/schedule-output-data-model.md`` §6, §7, §13, §16 and the
authorization rules in ``docs/architecture/core-data-model.md`` §4.2--§4.3.1.
Section numbers below refer to the schedule-output document unless stated.

**Both transitions are operational writes, and the most consequential ones in
the product** (core §4.3.1, Task 80): each takes
:func:`app.services.authorization.require_ministry_operator` -- the active Head
of *that* ministry, and nobody else. An Admin who heads nothing may read the
version, its checks and its readiness diagnostics in full
(:mod:`app.services.schedule_version_detail`) and may neither submit it nor
finalize it. Finalization is the moment a schedule becomes binding on
volunteers and on every other ministry's conflict query; whose schedule that is
is not a question church-wide oversight answers.

**Two forward transitions, and no others** (§6):
:func:`submit_schedule_version_for_review` (DRAFT -> REVIEW) and
:func:`finalize_schedule_version` (REVIEW -> FINALIZED). REVIEW is a
human-review state, not the authoritative one -- a version may enter it with
unfilled requirements or none at all, and only finalization requires a
complete, currently-valid schedule (Task 26).

**There is deliberately no reverse transition of any kind**: no
FINALIZED -> REVIEW, no un-finalize, no DRAFT -> FINALIZED shortcut past human
review. Un-finalizing would retroactively free people that other ministries
have already scheduled around (ADR 0002/0003); a finalized version is
corrected by a *successor* version, which this module does not create.
Successor-version creation, amendments, assignment copying and the solver all
remain out of scope: this is two operations that share three helpers, not the
beginnings of a lifecycle framework.

**[APPROVED] Mutable only as the latest working version** (§7), reusing the
same shape :mod:`app.services.assignment` already established for Assignment
mutation: ``status`` is DRAFT or REVIEW *and* no newer version of the same
schedule exists. Whether a newer version exists is always queried fresh
(:func:`_newer_version_exists`), never inferred from a relationship
collection, because "latest" is a fact about the schedule, not about the row
in hand. This module keeps its own small copy of that check rather than
importing :mod:`app.services.assignment`'s private helper -- the two checks
happen to share a shape today, but they guard different operations for
different reasons, and reaching into another module's private function would
couple them for no real savings.

**[REVIEWED] The only scheduling-input freshness gate here is Task 23's exact
snapshot-staleness comparison** (§13):
:func:`app.services.schedule_staleness.get_schedule_version_staleness`, called
on the same session, before any mutation. A stale DRAFT is refused with
:class:`~app.services.errors.InvalidOperationError`; the remedy is a fresh
successor version (§13's own words), which this module does not create.
Nothing here revalidates RoleQualification, Availability, Sunday conflicts,
override reasons or requirement fill counts -- those are Assignment-time
rules, not REVIEW-transition rules, and finalization completeness is a
separate, later gate this task does not build.

**Idempotent, but only for a version already in the target state and still
latest.** Every other setter in this package treats "already there" as a
silent no-op (Tasks 13-19); both transitions here extend that convention with
one condition the others do not need: idempotency requires the version still
be the *latest* one. A superseded REVIEW, or a superseded FINALIZED version an
amendment has replaced, is historical and must not report success -- saying
"yes, done" for it would misstate which version is authoritative. Neither
idempotent path writes an audit row, re-runs its gate, or flushes: nothing
happened.

**Ministry is derived, never accepted from the caller.** ``ScheduleVersion``
carries no ``ministry_id`` column of its own (unlike
``ScheduleVersionRequirement``, which does) -- only ``scheduling_period_id``,
so the Ministry is resolved with one fresh query for the owning
``SchedulingPeriod``, whose ``ministry_id`` column is the authorization scope
and whose ``ministry`` relationship supplies the name for the audit summary.
A ``version`` without enough persisted identity to run that query (no id, no
``schedule_id``, no ``scheduling_period_id``) is rejected outright rather than
guessed at -- see :func:`_resolve_scheduling_period`.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.core import Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    ScheduleVersion,
)
from app.models.scheduling_input import SchedulingPeriod
from app.services.audit import (
    ACTION_SCHEDULE_VERSION_FINALIZED,
    ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW,
    record_audit_event,
)
from app.services.authorization import require_ministry_operator
from app.services.errors import InvalidOperationError
from app.services.finalization_readiness import get_finalization_readiness
from app.services.schedule_staleness import get_schedule_version_staleness

__all__ = ["finalize_schedule_version", "submit_schedule_version_for_review"]

_VERSION_TARGET_TABLE = "schedule_version"


def submit_schedule_version_for_review(
    session: Session,
    *,
    actor: Person,
    version: ScheduleVersion,
    reason: str | None = None,
) -> ScheduleVersion:
    """Submit ``version`` for review, as ``actor``.

    **Sequence, and why it is ordered this way:**

    1. Resolve the owning ``SchedulingPeriod`` (and therefore the Ministry) --
       everything below needs it, and a version without enough persisted
       identity to find it cannot be safely operated on at all.
    2. Authorize against the resolved ministry -- a gate on whether this call
       may proceed, checked before anything else, including ``reason``
       formatting.
    3. Validate ``reason``.
    4. Confirm the version is mutable at all (DRAFT or REVIEW; not FINALIZED,
       not an unrecognized status) and is still the *latest* version of its
       schedule -- both must hold whether the eventual outcome is a real
       transition or an idempotent no-op.
    5. If already REVIEW: return unchanged. No staleness check, no audit row,
       no flush -- nothing happened.
    6. Otherwise (DRAFT): check Task 23 staleness against the same session.
       A stale version is refused; its snapshot rows are never touched.
    7. Flip ``status`` to REVIEW and record exactly one AuditEvent.

    **No flush.** ``version`` already has its identity (step 1 requires
    ``version.id`` to be set), so nothing here needs one before building the
    audit row's ``target_id``.

    Everything -- the status change and the audit row -- is added to the same
    ``session`` and written by the caller's eventual commit, never here. This
    function never commits or rolls back; if ``record_audit_event`` raises
    after the status mutation, the exception propagates and the caller's
    transaction rolls back the whole attempt, including the status change --
    nothing here manually restores it.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of the version's Ministry.
    :raises InvalidOperationError: ``version`` lacks enough persisted context
        to resolve its Ministry; ``reason`` was supplied but blank; the
        version is FINALIZED or an unrecognized status; the version has been
        superseded by a newer one; or the version is DRAFT and its snapshot is
        stale against current scheduling input (Task 23).
    """
    period = _resolve_scheduling_period(session, version)
    require_ministry_operator(actor, ministry_id=period.ministry_id)
    reason = _validate_optional_text(reason, field="reason")

    _require_latest_working_version(session, version)

    if version.status == SCHEDULE_VERSION_STATUS_REVIEW:
        # Idempotent: already there, still latest, nothing to record.
        return version

    # Only DRAFT reaches here -- _require_latest_working_version has already
    # rejected FINALIZED and any unrecognized status.
    staleness = get_schedule_version_staleness(session, version=version)
    if staleness.is_stale:
        raise InvalidOperationError(
            "cannot submit this version for review: its requirement snapshot"
            " no longer matches current scheduling input (staffing"
            " requirements or event dates have changed since it was created);"
            " create a fresh version to pick up the current configuration"
        )

    version.status = SCHEDULE_VERSION_STATUS_REVIEW

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW,
        target_table=_VERSION_TARGET_TABLE,
        target_id=version.id,
        ministry_id=period.ministry_id,
        # period.name is not guaranteed to embed the Ministry's own name (a
        # period is commonly just "Q4 2026"), so the Ministry name is read
        # explicitly via period.ministry -- the existing relationship, not a
        # fresh query -- rather than assumed to already be present in
        # period.name.
        summary=(
            f"Submitted draft version {version.version_number} for"
            f" {period.ministry.name} {period.name} for review"
        ),
        reason=reason,
        before_values={"status": SCHEDULE_VERSION_STATUS_DRAFT},
        after_values={"status": SCHEDULE_VERSION_STATUS_REVIEW},
    )
    return version


def finalize_schedule_version(
    session: Session,
    *,
    actor: Person,
    version: ScheduleVersion,
    reason: str | None = None,
) -> ScheduleVersion:
    """Finalize ``version``, making it the authoritative schedule, as ``actor``.

    **This is the moment church-wide authority transfers** (ADR 0003, audit
    §14): from the instant this commits, every other ministry's conflict query
    reads this version's assignments as real commitments. It is the most
    consequential write in the system, which is why the readiness gate below
    is absolute rather than advisory.

    **Sequence, and why it is ordered this way:**

    1. Resolve the owning ``SchedulingPeriod`` (and therefore the Ministry) --
       everything below needs it, and a version without enough persisted
       identity to find it cannot be safely operated on at all.
    2. Authorize against the resolved ministry.
    3. Validate ``reason``.
    4. Confirm the status is one this operation recognizes (REVIEW, or an
       already-FINALIZED version) and that no newer version supersedes it.
       Both hold whether the outcome is a real transition or a no-op: a
       superseded FINALIZED version is history, and answering "yes, done"
       for it would be a lie about which version is authoritative.
    5. If already FINALIZED: return unchanged, keeping the original
       ``finalized_at`` exactly. **No readiness re-check** -- re-validating a
       published schedule would let a later, unrelated change (a revoked
       qualification, a cancelled event) make an idempotent call start
       failing, and finalization is one-way: what was published stays
       published, and correcting it is an amendment.
    6. Otherwise (REVIEW): run Task 26's readiness gate on the same session.
       Not ready means refused outright -- there is no partial finalization,
       and nothing is mutated or repaired on the way out.
    7. Set ``status`` and ``finalized_at`` **together**, then record exactly
       one AuditEvent.

    **DRAFT is refused, deliberately** (§6, §16): a version must pass human
    review before it can bind the church. There is no DRAFT -> FINALIZED
    shortcut and no reverse transition of any kind -- un-finalizing would
    retroactively free people other ministries have already scheduled around,
    so a finalized version is corrected by a successor version, which this
    module does not create.

    **Nothing else is touched.** No pointer to an "authoritative version" is
    stored -- ADR 0003 derives it as the highest-numbered FINALIZED version,
    so finalizing version 2 supersedes version 1 by query semantics alone, and
    version 1's own row is never modified. Assignments are not rewritten and
    never mirrored into ``existing_commitment`` (ADR 0002): the union happens
    at query time, in Task 21.

    **No flush.** ``version`` already has its identity, and both changed
    fields are plain columns; the caller's commit writes them with the audit
    row. This function never commits or rolls back -- if
    ``record_audit_event`` raises after the mutation, the exception propagates
    and the caller's transaction discards status and ``finalized_at``
    together, with nothing here restoring them by hand.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of the version's Ministry.
    :raises InvalidOperationError: ``version`` lacks enough persisted context
        to resolve its Ministry; ``reason`` was supplied but blank; the
        version is DRAFT or an unrecognized status; it has been superseded by
        a newer version; or it is not ready to finalize (Task 26).
    """
    period = _resolve_scheduling_period(session, version)
    require_ministry_operator(actor, ministry_id=period.ministry_id)
    reason = _validate_optional_text(reason, field="reason")

    if version.status not in (
        SCHEDULE_VERSION_STATUS_REVIEW,
        SCHEDULE_VERSION_STATUS_FINALIZED,
    ):
        raise InvalidOperationError(
            "only a schedule version in REVIEW can be finalized; a draft must"
            " be submitted for review first"
        )
    _require_not_superseded(session, version)

    if version.status == SCHEDULE_VERSION_STATUS_FINALIZED:
        # Idempotent: already authoritative and still latest. finalized_at is
        # left exactly as it was -- rewriting it would move the historical
        # moment authority transferred.
        return version

    readiness = get_finalization_readiness(session, version=version)
    if not readiness.is_ready:
        raise InvalidOperationError(
            "cannot finalize this version:"
            f" {len(readiness.issues)} readiness issue(s) remain"
            + (" and its requirement snapshot is stale" if readiness.staleness.is_stale else "")
            + "; see get_finalization_readiness for the details"
        )

    finalized_at = _now_utc()
    # Set together: the model's status_finalized_at_agree CHECK is two-way, so
    # a moment where one is set without the other would be an invalid row if
    # anything flushed in between.
    version.status = SCHEDULE_VERSION_STATUS_FINALIZED
    version.finalized_at = finalized_at

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SCHEDULE_VERSION_FINALIZED,
        target_table=_VERSION_TARGET_TABLE,
        target_id=version.id,
        ministry_id=period.ministry_id,
        # period.name need not embed the Ministry's own name (Task 24's
        # correction), so both are read explicitly.
        summary=(
            f"Finalized version {version.version_number} for"
            f" {period.ministry.name} {period.name}"
        ),
        reason=reason,
        before_values={
            "status": SCHEDULE_VERSION_STATUS_REVIEW,
            "finalized_at": None,
        },
        after_values={
            "status": SCHEDULE_VERSION_STATUS_FINALIZED,
            # Text, not a datetime: the payload is JSONB and must be
            # JSON-serializable (audit §7.2).
            "finalized_at": finalized_at.isoformat(),
        },
    )
    return version


def _now_utc() -> datetime.datetime:
    """The finalization moment, timezone-aware and in UTC.

    A named indirection rather than an inline ``datetime.now`` so tests can
    pin it: this value is written to a column, echoed into the audit payload,
    and is the historical record of when church-wide authority transferred.
    Never naive -- ``finalized_at`` is ``timestamptz``, and a naive value would
    be interpreted against the server's zone rather than stated in UTC.
    """
    return datetime.datetime.now(tz=datetime.timezone.utc)


def _resolve_scheduling_period(session: Session, version: ScheduleVersion) -> SchedulingPeriod:
    """The version's owning ``SchedulingPeriod``, fetched fresh.

    ``ScheduleVersion`` has no ``scheduling_period`` relationship and no
    ``ministry_id`` column of its own (model docstring) -- ``scheduling_period_id``
    is the only link, so this is the one query that makes authorization and
    the audit ministry/summary possible at all.

    Guards ``version.id``, ``version.schedule_id`` and
    ``version.scheduling_period_id`` explicitly rather than letting a missing
    one silently query ``NULL`` and fail confusingly later (id is needed for
    the audit ``target_id``; ``schedule_id`` for the latest-version check;
    ``scheduling_period_id`` for this lookup itself).
    """
    if version.id is None:
        raise InvalidOperationError("version must be persisted (id is None)")
    if version.schedule_id is None:
        raise InvalidOperationError("version must have a schedule_id")
    if version.scheduling_period_id is None:
        raise InvalidOperationError("version must have a scheduling_period_id")

    stmt = _scheduling_period_lookup_statement(version.scheduling_period_id)
    period = session.execute(stmt).scalar_one_or_none()
    if period is None:
        raise InvalidOperationError(
            "version's scheduling period could not be resolved"
        )
    return period


def _scheduling_period_lookup_statement(scheduling_period_id: int) -> Select[tuple[SchedulingPeriod]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_schedule_lifecycle.py``).
    """
    return select(SchedulingPeriod).where(SchedulingPeriod.id == scheduling_period_id)


def _require_latest_working_version(session: Session, version: ScheduleVersion) -> None:
    """Raise unless ``version`` may be submitted for review at all, and is
    still the latest version of its schedule.

    Deliberately the same two-part shape as
    :func:`app.services.assignment._require_mutable_working_version` --
    status must be DRAFT or REVIEW, and no newer version of the same schedule
    may exist -- kept as this module's own small copy rather than imported,
    per module scope: these are two different operations that happen to share
    a rule today, not one rule two callers should be coupled through.
    """
    if version.status not in (SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW):
        raise InvalidOperationError(
            "only a DRAFT or REVIEW schedule version can be submitted for review"
        )
    _require_not_superseded(session, version)


def _require_not_superseded(session: Session, version: ScheduleVersion) -> None:
    """Raise if any newer version of the same schedule exists.

    The one genuinely shared half of the two transitions' rules, so it is
    written once: "latest" means the same thing whether a version is being
    submitted for review or finalized, and the answer lives on *other* rows,
    never on the one in hand. The status half stays with each operation,
    because the statuses they accept differ -- submission wants a working
    version, finalization wants a reviewed one.

    **Any** newer version supersedes, whatever its own status: an unfinished
    DRAFT 2 still means DRAFT/REVIEW 1 is no longer what the ministry is
    working on (§7).
    """
    if _newer_version_exists(
        session, schedule_id=version.schedule_id, version_number=version.version_number,
    ):
        raise InvalidOperationError(
            "cannot change a schedule version that has been superseded by a"
            " newer version"
        )


def _newer_version_exists_statement(schedule_id: int, version_number: int) -> Select[tuple[int]]:
    """The query, split from its execution for the same testing reason as
    :func:`_scheduling_period_lookup_statement`.
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


def _validate_optional_text(value: str | None, *, field: str) -> str | None:
    """Supplied means meaningful -- the same optional-free-text convention
    every other service in this package follows (``override_reason``,
    ``reason``, ``notes`` throughout Tasks 13-22): ``None`` stays ``None``, a
    supplied value must not be whitespace-only.
    """
    if value is None:
        return None
    if not value.strip():
        raise InvalidOperationError(f"{field} must not be blank when supplied")
    return value
