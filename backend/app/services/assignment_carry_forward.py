"""Carrying one Assignment forward into a version's direct successor.

Task 28 creates a successor with **zero** assignments, deliberately. This
module is the explicit, one-at-a-time way to bring a person forward -- and its
central claim is that doing so is **not a copy**. The result is a new
scheduling decision, made now, against current facts, by a named actor.

**[REVIEWED] ``assign_member`` is the only mutation path**
(:mod:`app.services.assignment`). This module resolves lineage and finds the
corresponding requirement in the successor; every rule about whether the person
may actually serve is Task 22's, unchanged and un-duplicated: authorization,
the target version's mutability, Ministry integrity, active Membership and
Person, a cancelled Event, one-position-per-event, qualification, role
activity, explicit ``UNAVAILABLE``, the church-wide Sunday conflict, and
capacity. Re-implementing any of them here would create a second, quietly
divergent definition of "may serve"; instead, a rejection from Task 22
propagates untouched. **Nothing is caught and skipped** -- one assignment per
call exists precisely so the caller sees each failure and decides.

**[REVIEWED] Historical authorization never transfers.**
``override_reason`` is always ``None``, and there is deliberately no such
parameter on the public function. ``is_override``, the old reason, the stored
``overridden_blockers`` and the source's override AuditEvent are neither read
nor copied: a head's decision to override a conflict *in one version, on one
day* is not standing permission for a new row in a new version. Two honest
consequences follow, and both are correct:

- a source assignment that *was* an override, whose blocker has since cleared,
  carries forward as an ordinary assignment (``is_override=False``);
- a source assignment whose blocker still applies is **rejected**, and a head
  who still wants it must make a fresh Task 22 assignment with a new,
  currently-justified ``override_reason``.

**[REVIEWED] Only the direct successor, and only a matching Sunday.** The
target must be the version that amends the source's own version -- same
schedule, same period, ``amends_version_id`` pointing at it, and exactly one
version number higher. And the two immutable snapshot ``event_date`` values
must agree: if the event moved from 15 to 22 November, Task 28 correctly
snapshotted the new date, but carrying the old row forward would silently
commit that person to a different Sunday than the one they were scheduled for.
That needs a person's decision, not an inference, so it is refused here. The
comparison is between the two snapshots -- never the current ``Event.event_date``,
which by then may agree with neither.

Not implemented here, deliberately: bulk or best-effort carry-forward, the
solver, any automatic override, successor-version creation, finalization, and
any new audit action. A successful call is recorded by the ``ASSIGNMENT_ADDED``
row Task 22 already writes for the real mutation; a second audit row for the
same event would be history describing itself twice, and no schema field
models assignment provenance for it to carry.
"""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.services.assignment import assign_member
from app.services.errors import InvalidOperationError

__all__ = ["carry_forward_assignment"]


def carry_forward_assignment(
    session: Session,
    *,
    actor: Person,
    source_assignment: Assignment,
    target_version: ScheduleVersion,
) -> Assignment:
    """Re-make ``source_assignment``'s decision in ``target_version``, as
    ``actor``.

    **Sequence, and why it is ordered this way:**

    1. Check that both objects carry the persisted context this needs. A
       half-built transient object cannot establish lineage, and guessing at
       one would be worse than refusing.
    2. Resolve the source's own version and require ``target_version`` to be
       its **direct** successor.
    3. Require the target to be a DRAFT that is still the latest version of
       its schedule. Task 22 would also refuse a superseded or FINALIZED
       target, but it accepts REVIEW, which this operation does not: carrying
       people into a version already under human review would change what the
       reviewers are looking at underneath them.
    4. Find the requirement in the target that corresponds to the source's --
       same event, same role, same snapshot date.
    5. Hand the real decision to :func:`~app.services.assignment.assign_member`
       with ``override_reason=None``.

    Steps 1-4 are structural only: they establish *which* requirement this is
    about. Whether the person may fill it -- including whether ``actor`` is
    allowed to ask -- is entirely step 5's answer.

    **Idempotent, through Task 22.** If this membership already fills the
    corresponding target requirement, ``assign_member`` returns that existing
    row unchanged, with no second audit row and no flush. This module adds no
    duplicate-detection of its own; one definition of "already assigned" is
    enough.

    **Nothing here mutates anything.** The source assignment, its version, its
    snapshot rows and its audit history are all read-only to this operation --
    several are not even loaded. The only write is the new row Task 22
    creates, in the caller's transaction, flushed once by Task 22 for its own
    audit row. This function never commits, rolls back, or flushes on its own.

    :raises AuthorizationError: raised by ``assign_member`` -- the actor is
        not an active Admin and not an active Ministry Head of the target
        requirement's Ministry.
    :raises InvalidOperationError: missing persisted context; ``target_version``
        is not the direct successor of the source's version; the target is not
        a latest DRAFT; the corresponding requirement does not exist in the
        target or is snapshotted for a different date; or ``assign_member``
        rejects the assignment on current facts.
    """
    _require_persisted_context(source_assignment, target_version)

    source_version = _resolve_version(session, source_assignment.schedule_version_id)
    _require_direct_successor(source_version=source_version, target_version=target_version)
    _require_latest_draft_target(session, target_version)

    source_requirement = _resolve_requirement(
        session, source_assignment.schedule_version_requirement_id
    )
    target_requirement = _resolve_corresponding_requirement(
        session, source_requirement=source_requirement, target_version=target_version
    )
    membership = _resolve_membership(session, source_assignment.ministry_membership_id)

    # The one mutation, through the one service that owns every rule about
    # whether it is allowed. override_reason is None and has no parameter to
    # come from: historical authorization does not travel (module docstring).
    return assign_member(
        session,
        actor=actor,
        requirement=target_requirement,
        membership=membership,
        override_reason=None,
    )


# --------------------------------------------------------------------------
# Lineage
# --------------------------------------------------------------------------


def _require_persisted_context(
    source_assignment: Assignment, target_version: ScheduleVersion
) -> None:
    """Every id this operation reasons about, checked explicitly.

    A missing one would otherwise become a query against ``NULL`` that matches
    nothing, and "no corresponding requirement" is a very misleading way to
    report "you passed an unsaved object".
    """
    for field in (
        "id",
        "schedule_version_id",
        "schedule_version_requirement_id",
        "ministry_membership_id",
    ):
        if getattr(source_assignment, field) is None:
            raise InvalidOperationError(
                f"source assignment must be persisted: {field} is None"
            )
    for field in ("id", "schedule_id", "scheduling_period_id", "version_number"):
        if getattr(target_version, field) is None:
            raise InvalidOperationError(
                f"target version must be persisted: {field} is None"
            )


def _require_direct_successor(
    *, source_version: ScheduleVersion, target_version: ScheduleVersion
) -> None:
    """The target must be the version that amends the source's version.

    All four conditions are checked, not just the lineage pointer: the shared
    schedule and period rule out carrying between unrelated schedules, and the
    adjacent version number rules out reaching across an intermediate version
    (V1 -> V3), whose own decisions would be skipped entirely. Anything less
    direct is a real scheduling decision someone should make deliberately.
    """
    if target_version.schedule_id != source_version.schedule_id:
        raise InvalidOperationError(
            "cannot carry an assignment between different schedules"
        )
    if target_version.scheduling_period_id != source_version.scheduling_period_id:
        raise InvalidOperationError(
            "cannot carry an assignment between different scheduling periods"
        )
    if target_version.amends_version_id != source_version.id:
        raise InvalidOperationError(
            "the target version does not amend the assignment's own version;"
            " an assignment can only be carried into its version's direct"
            " successor"
        )
    if target_version.version_number != source_version.version_number + 1:
        raise InvalidOperationError(
            "the target version is not the next version after the"
            f" assignment's own (version {source_version.version_number});"
            " carry forward one version at a time"
        )


def _require_latest_draft_target(session: Session, target_version: ScheduleVersion) -> None:
    """DRAFT, and still the latest version of its schedule.

    Stricter than Task 22's own mutability rule on purpose. Task 22 accepts
    DRAFT *or* REVIEW, because a head editing a version under review is making
    a deliberate change they can see; carrying rows in automatically would
    alter a version while others are reviewing it. FINALIZED and superseded
    targets would be refused by Task 22 too -- checked here as well so the
    error names the real problem instead of arriving as a generic
    immutability complaint after several irrelevant lookups.
    """
    if target_version.status != SCHEDULE_VERSION_STATUS_DRAFT:
        raise InvalidOperationError(
            "assignments can only be carried into a DRAFT version, and this"
            f" one is {target_version.status}"
        )
    if _newer_version_exists(
        session,
        schedule_id=target_version.schedule_id,
        version_number=target_version.version_number,
    ):
        raise InvalidOperationError(
            "cannot carry an assignment into a schedule version that has been"
            " superseded by a newer version"
        )


# --------------------------------------------------------------------------
# Requirement mapping
# --------------------------------------------------------------------------


def _resolve_corresponding_requirement(
    session: Session,
    *,
    source_requirement: ScheduleVersionRequirement,
    target_version: ScheduleVersion,
) -> ScheduleVersionRequirement:
    """The target's requirement for the same event, role and snapshot date.

    ``(schedule_version_id, event_id, ministry_role_id)`` is unique, so the
    lookup is unambiguous. The date is compared *after* the lookup rather than
    folded into it, so a moved event produces its own explicit error instead
    of the generic "no such requirement" -- the difference matters, because
    one means "this position no longer exists" and the other means "this
    position is now on a different Sunday".

    Both dates read are immutable snapshots. The current ``Event.event_date``
    is never consulted: by the time an event has moved it agrees with neither
    version, and the question here is whether the two versions describe the
    same commitment.

    A missing requirement covers every way a position can stop existing --
    the requirement was removed, the role's requirement was dropped, or the
    event was cancelled and so is absent from the successor's snapshot
    entirely. None of them is something to create on the fly.

    **``required_count`` is deliberately not compared.** A count that rose
    from 1 to 2 leaves this person's place intact and the requirement merely
    underfilled; one that fell from 2 to 1 is a capacity question, and Task 22
    already answers it exactly once, for whichever carry happens to be second.
    """
    target_requirement = _find_requirement_by_scheduling_identity(
        session,
        schedule_version_id=target_version.id,
        event_id=source_requirement.event_id,
        ministry_role_id=source_requirement.ministry_role_id,
    )
    if target_requirement is None:
        raise InvalidOperationError(
            "the target version has no requirement for this event and role,"
            " so this assignment has no position to carry into"
        )
    if target_requirement.event_date != source_requirement.event_date:
        raise InvalidOperationError(
            "this event has moved from"
            f" {source_requirement.event_date.isoformat()} to"
            f" {target_requirement.event_date.isoformat()} since the"
            " assignment was made; assign the member to the new date"
            " deliberately rather than carrying the old decision forward"
        )
    return target_requirement


# --------------------------------------------------------------------------
# Queries -- plain selects, each split from its execution so the SQL is
# testable with no database (see tests/test_services_assignment_carry_forward.py)
# --------------------------------------------------------------------------


def _version_lookup_statement(version_id: int) -> Select[tuple[ScheduleVersion]]:
    return select(ScheduleVersion).where(ScheduleVersion.id == version_id)


def _resolve_version(session: Session, version_id: int) -> ScheduleVersion:
    """The source assignment's own version, fetched fresh.

    Read from ``assignment.schedule_version_id`` rather than through the
    requirement's relationship: the assignment's four-column composite key
    already pins the two together, and this is the column the lineage question
    is actually about.
    """
    version = session.execute(_version_lookup_statement(version_id)).scalar_one_or_none()
    if version is None:
        raise InvalidOperationError(
            "the assignment's own schedule version could not be resolved"
        )
    return version


def _requirement_lookup_statement(
    requirement_id: int,
) -> Select[tuple[ScheduleVersionRequirement]]:
    return select(ScheduleVersionRequirement).where(
        ScheduleVersionRequirement.id == requirement_id
    )


def _resolve_requirement(
    session: Session, requirement_id: int
) -> ScheduleVersionRequirement:
    requirement = session.execute(
        _requirement_lookup_statement(requirement_id)
    ).scalar_one_or_none()
    if requirement is None:
        raise InvalidOperationError(
            "the assignment's own requirement snapshot could not be resolved"
        )
    return requirement


def _requirement_by_scheduling_identity_statement(
    schedule_version_id: int, event_id: int, ministry_role_id: int
) -> Select[tuple[ScheduleVersionRequirement]]:
    """The stable scheduling identity of a required position within one
    version: which event, and which role. Exactly the columns
    ``uq_schedule_version_requirement_version_event_role`` is built from.
    """
    return select(ScheduleVersionRequirement).where(
        ScheduleVersionRequirement.schedule_version_id == schedule_version_id,
        ScheduleVersionRequirement.event_id == event_id,
        ScheduleVersionRequirement.ministry_role_id == ministry_role_id,
    )


def _find_requirement_by_scheduling_identity(
    session: Session, *, schedule_version_id: int, event_id: int, ministry_role_id: int
) -> ScheduleVersionRequirement | None:
    stmt = _requirement_by_scheduling_identity_statement(
        schedule_version_id, event_id, ministry_role_id
    )
    return session.execute(stmt).scalar_one_or_none()


def _membership_lookup_statement(membership_id: int) -> Select[tuple[MinistryMembership]]:
    return select(MinistryMembership).where(MinistryMembership.id == membership_id)


def _resolve_membership(session: Session, membership_id: int) -> MinistryMembership:
    """The exact membership the source assignment named.

    Never a search for an equivalent person or a different membership of the
    same person: carrying an assignment forward means *this* person, and
    anything else would be the service quietly choosing someone.
    """
    membership = session.execute(
        _membership_lookup_statement(membership_id)
    ).scalar_one_or_none()
    if membership is None:
        raise InvalidOperationError(
            "the assignment's ministry membership could not be resolved"
        )
    return membership


def _newer_version_exists_statement(schedule_id: int, version_number: int) -> Select[tuple[int]]:
    """A single-column ``LIMIT 1`` existence probe -- the same shape Tasks
    22/24/27/28 use, kept local rather than imported from another module's
    private namespace. "Latest" is a fact about the schedule, living on other
    rows, so it is always queried fresh.
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
