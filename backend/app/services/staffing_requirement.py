"""Setting, changing, and removing how many people a role needs at an event.

Implements the accepted design in
``docs/architecture/scheduling-input-data-model.md`` §6--§7 and the
authorization rules in ``docs/architecture/core-data-model.md`` §4.2--§4.3.
Section numbers below refer to the scheduling-input document unless stated.

**[APPROVED] StaffingRequirement is current, mutable configuration** (§6): it
is the authoritative per-event input the (future) solver reads, and it is
freely created, changed, and deleted right up until a schedule is produced
against it. It is emphatically **not** the historical record --
``ScheduleVersionRequirement`` is the immutable snapshot a schedule version
takes of it once one exists (schedule-output §8), and there is deliberately no
foreign key from that snapshot back to this table (schedule-output §8's
"why the snapshot has no FK to staffing_requirement" reasoning): a finalized
version must remain readable, and a staffing row must remain freely editable
or deletable, forever after. **Nothing here touches a schedule snapshot**,
and detecting that a version has gone stale against a later staffing change is
explicitly a later schedule service's job (schedule-output §13), not this
one's.

**[APPROVED] A required-count row asserts a real need; the absence of a row
is "not needed here"** (§6, §7): there is no persisted zero. Passing
``required_count=0`` to :func:`set_staffing_requirement` therefore means
*remove the requirement*, not *store a zero* -- the database's own
``CHECK (required_count > 0)`` would refuse a zero row outright, and this
service never attempts to write one.

**Head-scoped writes, reader-scoped reads** (core §4.2--§4.3, §4.3.1).
Listing takes :func:`app.services.authorization.require_ministry_reader`
(an Admin overseeing the church, or this ministry's active Head); every
write takes :func:`app.services.authorization.require_ministry_operator`
(this ministry's active Head, and nobody else -- Task 80). An Admin who
heads nothing may read all of this and change none of it.

**[Task 54] Listing is read-only and joins in every active role, required or
not.** :func:`list_event_staffing_requirements` answers "what does this event
need, and what *could* it need?" in one call: every currently active role in
the event's ministry, each paired with its current required count where one
exists and ``None`` where it does not (§6, §7's own "absence is not needed
here" reading, carried into the read side). A deactivated role's own
requirement history is not hidden by the model -- it simply is not this
question's business, the same way :func:`app.services.ministry_role.list_ministry_roles`
does not surface a deactivated role by default; a head who deactivated a role
after a schedule already needed it can still see the role itself, deactivated,
on the Task 53 role screen.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import MinistryRole, Person
from app.models.scheduling_input import Event, StaffingRequirement
from app.services.audit import (
    ACTION_STAFFING_REQUIREMENT_CHANGED,
    ACTION_STAFFING_REQUIREMENT_CREATED,
    ACTION_STAFFING_REQUIREMENT_REMOVED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError

__all__ = [
    "EventStaffing",
    "RoleStaffing",
    "list_event_staffing_requirements",
    "set_staffing_requirement",
]

_TARGET_TABLE = "staffing_requirement"


@dataclass(frozen=True, slots=True)
class RoleStaffing:
    """One active role, and this event's current demand for it, if any."""

    ministry_role_id: int
    name: str
    description: str | None
    display_order: int
    #: ``None`` means "not required here" (§6, §7) -- never a stored zero.
    required_count: int | None


@dataclass(frozen=True, slots=True)
class EventStaffing:
    """One event's staffing picture: itself, and every active role's demand."""

    event_id: int
    event_date: datetime.date
    event_name: str | None
    event_kind: str
    ministry_id: int
    roles: tuple[RoleStaffing, ...]


def list_event_staffing_requirements(
    session: Session, *, actor: Person, event: Event
) -> EventStaffing:
    """``event``'s active roles and their current required counts.

    **Read-only.** Ordered the same way :func:`app.services.ministry_role.list_ministry_roles`
    orders a ministry's roles -- ``display_order``, then ``lower(name)``, then
    ``id`` -- so a staffing screen and a role screen never disagree about role
    order.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``event.ministry_id``.
    """
    require_ministry_reader(actor, ministry_id=event.ministry_id)

    rows = session.execute(_event_staffing_statement(event.id, event.ministry_id)).all()
    return EventStaffing(
        event_id=event.id,
        event_date=event.event_date,
        event_name=event.name,
        event_kind=event.event_kind,
        ministry_id=event.ministry_id,
        roles=tuple(
            RoleStaffing(
                ministry_role_id=row.ministry_role_id,
                name=row.name,
                description=row.description,
                display_order=row.display_order,
                required_count=row.required_count,
            )
            for row in rows
        ),
    )


def set_staffing_requirement(
    session: Session,
    *,
    actor: Person,
    event: Event,
    role: MinistryRole,
    required_count: int,
    reason: str | None = None,
) -> StaffingRequirement | None:
    """Create, change, or remove the staffing requirement for ``role`` at ``event``.

    One explicit setter, not separate create/update/remove functions: the
    domain fact is a single non-negative number (§6), and ``required_count=0``
    already has an unambiguous meaning -- "not needed" -- so a setter states
    the whole rule in one signature instead of asking the caller to know which
    of three verbs applies to a count going from 1 to 0. This mirrors the
    reasoning behind :func:`app.services.role_qualification.set_role_qualification`,
    the one other setter-shaped service in this project.

    **``required_count > 0`` creates or changes the row; ``required_count == 0``
    removes it.** Negative counts are rejected outright, before anything else
    is checked.

    **Idempotent**, exactly as stated, with no further conditions:

    - an existing row whose count already equals the requested count is
      returned unchanged -- no ``AuditEvent``, and the row's ``required_count``
      is not reassigned even to the same value, so ``updated_at`` does not
      move for a "change" that changed nothing;
    - no row **and** a requested ``0`` returns ``None`` -- nothing to remove,
      nothing to audit.

    **Activity, and the one exempt path.** Every request whose result is a
    positive count -- a brand-new row, or an existing row's count changing to
    a different positive value -- requires the event to be **live**
    (``event.cancelled_at IS NULL``) and the role to be **active**
    (``role.deactivated_at IS NULL``): both are prerequisites for new or
    changed staffing demand, and are checked before the idempotency return so
    a stale "already this count" cannot mask a target that has since gone
    inactive. **Removing an existing requirement is exempt** -- reducing
    configuration to nothing is state repair, permitted even against a
    cancelled event or a deactivated role, on the same footing as
    :func:`~app.services.ministry_authority.revoke_ministry_head` and the
    ``True -> False`` path of ``set_role_qualification``. Unlike that
    function's asymmetry, there is no "brand-new negative assessment" case
    here to reason about separately: a StaffingRequirement's only "no demand"
    state is the absence of a row, which a ``required_count=0`` request against
    an already-absent row resolves as the ordinary no-op above, not as a new
    row to be checked for activity.

    The mutation and its audit row are added to the same ``session`` and are
    written by the caller's commit, so they land together or not at all
    (:mod:`app.services`). A brand-new row is flushed once, immediately after
    it is added, to obtain the identity its audit row must reference; an
    update or a removal needs no flush, since the existing row already has one.
    This function never commits or rolls back.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``event.ministry_id``.
    :raises InvalidOperationError: ``event`` and ``role`` belong to different
        ministries; ``required_count`` is negative; the requested count would
        create or change a positive requirement against a cancelled event or a
        deactivated role; or ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=event.ministry_id)
    _require_same_ministry(event, role)
    reason = _validate_optional_reason(reason)

    if required_count < 0:
        raise InvalidOperationError("required_count must not be negative")

    existing = _find_existing_requirement(
        session, event_id=event.id, ministry_role_id=role.id
    )

    if required_count > 0:
        if existing is not None and existing.required_count == required_count:
            return existing
        # A brand-new row, or an existing row's count changing to a different
        # positive value: both are new/changed staffing demand.
        _require_active_target(event, role)
        return _create_or_change(
            session, actor=actor, event=event, role=role,
            existing=existing, required_count=required_count, reason=reason,
        )

    # required_count == 0: remove, or no-op if there is nothing to remove.
    if existing is None:
        return None
    _remove(session, actor=actor, event=event, role=role, existing=existing, reason=reason)
    return None


def _create_or_change(
    session: Session,
    *,
    actor: Person,
    event: Event,
    role: MinistryRole,
    existing: StaffingRequirement | None,
    required_count: int,
    reason: str | None,
) -> StaffingRequirement:
    ministry_name = role.ministry.name
    when = event.event_date.isoformat()

    if existing is None:
        requirement = StaffingRequirement(
            event_id=event.id,
            ministry_role_id=role.id,
            # Shared by both composite foreign keys on the model; taken from
            # the event, which _require_same_ministry has already proven
            # agrees with the role.
            ministry_id=event.ministry_id,
            required_count=required_count,
        )
        session.add(requirement)
        # The audit row below needs a real target_id, and a new identity
        # bigint does not exist until the INSERT actually runs -- the minimum
        # flush that makes that true, scoped to the one pending row that needs
        # it. See app.services' module docstring on why this does not
        # compromise the caller-owned transaction.
        session.flush([requirement])

        record_audit_event(
            session,
            actor=actor,
            action=ACTION_STAFFING_REQUIREMENT_CREATED,
            target_table=_TARGET_TABLE,
            target_id=requirement.id,
            ministry_id=event.ministry_id,
            summary=f"Required {required_count} {role.name} for {ministry_name} on {when}",
            reason=reason,
            # Only meaningful business state, never the whole ORM row
            # (audit §7.2).
            after_values={
                "event_id": event.id,
                "ministry_role_id": role.id,
                "required_count": required_count,
            },
        )
        return requirement

    old_count = existing.required_count
    existing.required_count = required_count
    # No flush: the row already has an id, so nothing the audit row needs is
    # missing.
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_STAFFING_REQUIREMENT_CHANGED,
        target_table=_TARGET_TABLE,
        target_id=existing.id,
        ministry_id=event.ministry_id,
        summary=(
            f"Changed {role.name} staffing for {ministry_name} on {when}"
            f" from {old_count} to {required_count}"
        ),
        reason=reason,
        # Only the one changed business field (audit §7.2).
        before_values={"required_count": old_count},
        after_values={"required_count": required_count},
    )
    return existing


def _remove(
    session: Session,
    *,
    actor: Person,
    event: Event,
    role: MinistryRole,
    existing: StaffingRequirement,
    reason: str | None,
) -> None:
    ministry_name = role.ministry.name
    when = event.event_date.isoformat()

    # Captured before the delete, and the audit row is built before
    # session.delete() is called at all -- clearer than relying on the fact
    # that a pending-delete object's attributes remain readable until flush.
    # Both the audit insert and the delete land in the same transaction
    # regardless of which comes first in code.
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_STAFFING_REQUIREMENT_REMOVED,
        target_table=_TARGET_TABLE,
        target_id=existing.id,
        ministry_id=event.ministry_id,
        summary=f"Removed {role.name} staffing requirement for {ministry_name} on {when}",
        reason=reason,
        before_values={
            "event_id": existing.event_id,
            "ministry_role_id": existing.ministry_role_id,
            "required_count": existing.required_count,
        },
    )
    # Genuine removal, not a soft-delete flag: the schema deliberately has no
    # deactivated_at column here (module docstring). No flush -- nothing here
    # needs to prove the deletion happened before the transaction commits.
    session.delete(existing)


def _require_same_ministry(event: Event, role: MinistryRole) -> None:
    """The database enforces this too, via the composite foreign keys routed
    through ``staffing_requirement.ministry_id`` (core §7.2, scheduling-input
    §7). Checking here turns an ``IntegrityError`` at commit -- far from its
    cause, and aborting the caller's whole transaction -- into a meaningful
    domain error raised before anything is mutated.
    """
    if event.ministry_id != role.ministry_id:
        raise InvalidOperationError("event and role must belong to the same ministry")


def _require_active_target(event: Event, role: MinistryRole) -> None:
    """A new or changed positive requirement needs a live event and an active role.

    Not called on the removal path -- see the asymmetry documented on
    :func:`set_staffing_requirement`.
    """
    if event.cancelled_at is not None:
        raise InvalidOperationError(
            "cannot record a new or changed staffing requirement"
            " for a cancelled event"
        )
    if role.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot record a new or changed staffing requirement"
            " for a deactivated role"
        )


def _validate_optional_reason(reason: str | None) -> str | None:
    """A reason is optional here, but blank is not a reason.

    [REVIEWED] audit §9: no new universal reason requirement is invented for
    ordinary staffing changes; a reason may be supplied and is recorded when
    it is.
    """
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


def _requirement_lookup_statement(
    event_id: int, ministry_role_id: int
) -> Select[tuple[StaffingRequirement]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_staffing_requirement.py``).
    """
    return select(StaffingRequirement).where(
        StaffingRequirement.event_id == event_id,
        StaffingRequirement.ministry_role_id == ministry_role_id,
    )


def _find_existing_requirement(
    session: Session, *, event_id: int, ministry_role_id: int
) -> StaffingRequirement | None:
    """The standing requirement for this (event, role) pair, if one exists.

    Queried on the model's own integrity columns -- ``event_id`` and
    ``ministry_role_id``, exactly the pair
    ``uq_staffing_requirement_event_id_ministry_role_id`` is built from
    (scheduling-input §7) -- via a plain SQLAlchemy ``select()``, no
    repository abstraction. Absence is a real domain state, "not required
    here" (§6, §7), and is returned as ``None`` rather than assumed to mean a
    stored zero.
    """
    stmt = _requirement_lookup_statement(event_id, ministry_role_id)
    return session.execute(stmt).scalar_one_or_none()


def _event_staffing_statement(event_id: int, ministry_id: int) -> Select:
    """Every active role in ``ministry_id``, left-joined to any
    ``StaffingRequirement`` it has for ``event_id``.

    A ``LEFT OUTER JOIN``, not an inner one: a role with no requirement row
    for this event is exactly the case this listing exists to show ("not
    required here yet"), and an inner join would silently drop it. Ordered
    the same way :func:`app.services.ministry_role._roles_statement` orders a
    ministry's roles.
    """
    return (
        select(
            MinistryRole.id.label("ministry_role_id"),
            MinistryRole.name.label("name"),
            MinistryRole.description.label("description"),
            MinistryRole.display_order.label("display_order"),
            StaffingRequirement.required_count.label("required_count"),
        )
        .select_from(MinistryRole)
        .outerjoin(
            StaffingRequirement,
            (StaffingRequirement.ministry_role_id == MinistryRole.id)
            & (StaffingRequirement.event_id == event_id),
        )
        .where(
            MinistryRole.ministry_id == ministry_id,
            MinistryRole.deactivated_at.is_(None),
        )
        .order_by(
            MinistryRole.display_order, func.lower(MinistryRole.name), MinistryRole.id
        )
    )
