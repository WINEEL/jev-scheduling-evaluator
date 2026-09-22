"""Listing, creating, editing, and soft-deactivating a ministry's roles.

Implements the accepted design in ``docs/architecture/core-data-model.md``
§7 (``ministry_role``) and the authorization rules in the same document's
§4.2--§4.3. Section numbers below refer to the core document unless stated.

**[APPROVED] MinistryRole is generic, per-ministry configuration, not a fixed
vocabulary** (core §7): "Setup Lead", "Sound", "Slides" are data a Ministry
Head enters, never a name the application branches on. Nothing here, or
anywhere this module is called from, may special-case a role by name.

**Head-scoped writes, reader-scoped reads** (core §4.2--§4.3, §4.3.1).
Listing takes :func:`app.services.authorization.require_ministry_reader`
(an Admin overseeing the church, or this ministry's active Head); every
write takes :func:`app.services.authorization.require_ministry_operator`
(this ministry's active Head, and nobody else -- Task 80). An Admin who
heads nothing may read all of this and change none of it.

**Roles are deactivated, never deleted** (core §7's own docstring on
``MinistryRole.deactivated_at``): past ``StaffingRequirement``,
``RoleQualification`` and ``Assignment`` rows name a role by id and must
remain readable forever. Nothing in this module issues a ``DELETE`` against
``ministry_role``, and none is added by any operation here.

**Uniqueness spans active and inactive roles, on purpose.** The database's own
``uq_ministry_role_ministry_id_name_lower`` index carries no ``deactivated_at``
predicate -- exactly the same shape as ``Ministry``'s own name uniqueness,
whose comment states the reasoning this module inherits unchanged: reusing a
deactivated role's name means reactivating or renaming *that* role, both
deliberate acts, never a second row silently claiming a retired name. Creating
a role therefore checks for a name collision against every row, active or not,
and :func:`reactivate_ministry_role` can never collide with itself.

**Display order gets a simple, deterministic default, not an ordering UI**
(core §7's own docstring on ``MinistryRole.display_order``: presentation only,
never solver input). A newly created role is appended after every existing
role for its ministry -- ``max(display_order) + 1``, or ``0`` for a ministry's
first role -- so a configuration screen has a stable, predictable order with
no separate reordering operation to build. Changing an existing role's order
is explicitly out of this module's scope.

**Editing name/description takes the whole new state, not a partial patch.**
:func:`update_ministry_role` is a two-field setter in the same shape as
:func:`app.services.scheduling_period.create_scheduling_period` validates a
name -- the caller supplies the row's complete editable content, exactly as a
head's edit form would submit it, rather than this module inventing per-field
PATCH semantics nothing else here uses.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryRole, Person
from app.services.audit import (
    ACTION_MINISTRY_ROLE_CHANGED,
    ACTION_MINISTRY_ROLE_CREATED,
    ACTION_MINISTRY_ROLE_DEACTIVATED,
    ACTION_MINISTRY_ROLE_REACTIVATED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError

__all__ = [
    "create_ministry_role",
    "deactivate_ministry_role",
    "list_ministry_roles",
    "reactivate_ministry_role",
    "update_ministry_role",
]

_TARGET_TABLE = "ministry_role"


def list_ministry_roles(
    session: Session,
    *,
    actor: Person,
    ministry: Ministry,
    include_inactive: bool = False,
) -> tuple[MinistryRole, ...]:
    """``ministry``'s roles, for someone allowed to manage it.

    **Read-only.** Ordered by ``display_order``, then ``lower(name)``, then
    ``id`` -- exactly the tie-break the model's own docstring documents, so
    every caller of this module sees roles in the same stable order a
    configuration screen would want, without re-deriving it.

    ``include_inactive=False`` (the default) returns only roles a head could
    assign a member or a staffing requirement to today. Passing ``True``
    additionally returns deactivated roles, so a screen can show them
    clearly-marked rather than making them silently disappear.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``ministry``.
    """
    require_ministry_reader(actor, ministry_id=ministry.id)

    stmt = _roles_statement(ministry.id, include_inactive=include_inactive)
    return tuple(session.execute(stmt).scalars().all())


def create_ministry_role(
    session: Session,
    *,
    actor: Person,
    ministry: Ministry,
    name: str,
    description: str | None = None,
    reason: str | None = None,
) -> MinistryRole:
    """Create a new role for ``ministry``, as ``actor``.

    Validated, in order, before anything is mutated: actor authorization,
    ``ministry`` being active, ``name`` non-blank, and finally that no role in
    this ministry already has this name case-insensitively -- active or
    deactivated (module docstring) -- proactively, so the caller receives an
    :class:`InvalidOperationError` rather than the database's own
    ``uq_ministry_role_ministry_id_name_lower`` rejecting the insert as an
    ``IntegrityError`` far from its cause.

    ``display_order`` is never accepted as a parameter: it is computed here as
    one past this ministry's current maximum (module docstring), so a caller
    cannot create a role that jumps the queue or collides with another's
    position.

    The row is added to ``session`` and flushed once -- the minimum needed to
    obtain its identity before the audit row that must reference it can be
    built -- and is written by the caller's eventual commit, not here.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``ministry``.
    :raises InvalidOperationError: ``ministry`` is deactivated; ``name`` is
        blank; a role with this name already exists in this ministry; or
        ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=ministry.id)
    reason = _validate_optional_reason(reason)

    if ministry.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot create a role for a deactivated ministry"
        )

    name = _require_non_blank(name, "name")

    if _find_role_by_name(session, ministry_id=ministry.id, name=name) is not None:
        raise InvalidOperationError(
            f"a role named {name!r} already exists for this ministry"
        )

    role = MinistryRole(
        ministry_id=ministry.id,
        name=name,
        description=description,
        display_order=_next_display_order(session, ministry.id),
    )
    session.add(role)
    # The audit row below needs a real target_id; a new identity bigint does
    # not exist until the INSERT actually runs. Scoped to the one pending row
    # that needs it -- see app.services' module docstring on why this does not
    # compromise the caller-owned transaction.
    session.flush([role])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MINISTRY_ROLE_CREATED,
        target_table=_TARGET_TABLE,
        target_id=role.id,
        ministry_id=ministry.id,
        summary=f"Created role {name} for {ministry.name}",
        reason=reason,
        # Only meaningful business state -- never the whole ORM row
        # (audit §7.2).
        after_values={
            "name": name,
            "description": description,
            "display_order": role.display_order,
        },
    )
    return role


def update_ministry_role(
    session: Session,
    *,
    actor: Person,
    role: MinistryRole,
    name: str,
    description: str | None = None,
    reason: str | None = None,
) -> MinistryRole:
    """Set ``role``'s name and description to exactly what is given, as ``actor``.

    **Idempotent.** If both fields already equal what is given, nothing is
    changed and no audit row is written: no domain state changed, so there is
    no act to record, and ``updated_at`` does not move for an "edit" that
    edited nothing.

    Renaming to a name already used by a *different* role in this ministry --
    active or deactivated (module docstring) -- is rejected the same way
    :func:`create_ministry_role` rejects a duplicate at creation. Renaming a
    role to a case-different spelling of **its own current name** (``"Sound"``
    -> ``"SOUND"``) is not a collision with itself and is allowed.

    Editing is permitted regardless of whether ``role`` is currently active or
    deactivated: fixing a typo in a role nobody may assign to right now is not
    the same act as approving new work against it, and Task 53 draws no
    distinction the model does not already draw.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``role``'s ministry.
    :raises InvalidOperationError: ``name`` is blank; a *different* role in
        this ministry already has this name; or ``reason`` was supplied but
        blank.
    """
    require_ministry_operator(actor, ministry_id=role.ministry_id)
    reason = _validate_optional_reason(reason)
    name = _require_non_blank(name, "name")

    existing_with_name = _find_role_by_name(
        session, ministry_id=role.ministry_id, name=name
    )
    if existing_with_name is not None and existing_with_name.id != role.id:
        raise InvalidOperationError(
            f"a role named {name!r} already exists for this ministry"
        )

    if role.name == name and role.description == description:
        return role

    old_values = {"name": role.name, "description": role.description}
    role.name = name
    role.description = description
    # No flush: the row already has an id, so nothing the audit row needs is
    # missing.
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MINISTRY_ROLE_CHANGED,
        target_table=_TARGET_TABLE,
        target_id=role.id,
        ministry_id=role.ministry_id,
        summary=f"Changed role {old_values['name']} to {name} for {role.ministry.name}",
        reason=reason,
        # Only the two changed business fields (audit §7.2). display_order and
        # deactivated_at are untouched by this operation and are not this
        # audit row's business.
        before_values=old_values,
        after_values={"name": name, "description": description},
    )
    return role


def deactivate_ministry_role(
    session: Session, *, actor: Person, role: MinistryRole, reason: str | None = None,
) -> MinistryRole:
    """Deactivate ``role``, as ``actor``.

    **Idempotent.** If ``role`` is already deactivated, nothing is changed and
    no audit row is written.

    Never deletes anything, and never touches a ``StaffingRequirement``,
    ``RoleQualification`` or ``Assignment`` row: those keep naming this role by
    id, exactly as the model's own docstring intends (module docstring).
    Whether new staffing or a new qualification may still be recorded against
    a deactivated role is those services' own rule
    (:func:`app.services.staffing_requirement.set_staffing_requirement`,
    :func:`app.services.role_qualification.set_role_qualification`), already
    built and unaffected by this module.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``role``'s ministry.
    :raises InvalidOperationError: ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=role.ministry_id)
    reason = _validate_optional_reason(reason)

    if role.deactivated_at is not None:
        return role

    deactivated_at = _now_utc()
    role.deactivated_at = deactivated_at
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MINISTRY_ROLE_DEACTIVATED,
        target_table=_TARGET_TABLE,
        target_id=role.id,
        ministry_id=role.ministry_id,
        summary=f"Deactivated role {role.name} for {role.ministry.name}",
        reason=reason,
        # A JSON-compatible ISO-8601 string, not a Python datetime: JSONB has
        # no native timestamp type, and an audit payload is a historical
        # record read back as plain JSON (audit §7.2), matching
        # scheduling_period.lock_availability's own convention.
        before_values={"deactivated_at": None},
        after_values={"deactivated_at": deactivated_at.isoformat()},
    )
    return role


def reactivate_ministry_role(
    session: Session, *, actor: Person, role: MinistryRole, reason: str | None = None,
) -> MinistryRole:
    """Reactivate ``role``, as ``actor``.

    **Idempotent.** If ``role`` is already active, nothing is changed and no
    audit row is written.

    **Never a naming conflict with itself** (module docstring): the
    uniqueness index has no partial predicate, so this exact row already holds
    its name whether active or not, and reactivating changes no name. A
    genuine collision could only arise if some *other* row had since taken
    this name -- impossible, since the index already forbids that while this
    row still holds it, deactivated or not.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``role``'s ministry.
    :raises InvalidOperationError: ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=role.ministry_id)
    reason = _validate_optional_reason(reason)

    if role.deactivated_at is None:
        return role

    old_deactivated_at = role.deactivated_at
    role.deactivated_at = None
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MINISTRY_ROLE_REACTIVATED,
        target_table=_TARGET_TABLE,
        target_id=role.id,
        ministry_id=role.ministry_id,
        summary=f"Reactivated role {role.name} for {role.ministry.name}",
        reason=reason,
        before_values={"deactivated_at": old_deactivated_at.isoformat()},
        after_values={"deactivated_at": None},
    )
    return role


def _validate_optional_reason(reason: str | None) -> str | None:
    """A reason is optional here, but blank is not a reason.

    [REVIEWED] audit §9: no new universal reason requirement is invented for
    ordinary role configuration; a reason may be supplied and is recorded when
    it is.
    """
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


def _require_non_blank(value: str, field: str) -> str:
    if not value.strip():
        raise InvalidOperationError(f"{field} must not be blank")
    return value


def _roles_statement(ministry_id: int, *, include_inactive: bool) -> Select:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_ministry_role.py``).

    Ordered by ``display_order``, then ``lower(name)``, then ``id`` -- the
    model's own documented tie-break (core §7's ``MinistryRole.display_order``
    docstring).
    """
    stmt = select(MinistryRole).where(MinistryRole.ministry_id == ministry_id)
    if not include_inactive:
        stmt = stmt.where(MinistryRole.deactivated_at.is_(None))
    return stmt.order_by(
        MinistryRole.display_order, func.lower(MinistryRole.name), MinistryRole.id
    )


def _role_name_lookup_statement(
    ministry_id: int, name: str
) -> Select[tuple[MinistryRole]]:
    """Mirrors the database's own case-insensitive uniqueness index,
    ``uq_ministry_role_ministry_id_name_lower`` -- ``func.lower`` on both
    sides is what makes this a true case-insensitive match rather than an
    exact one. Deliberately **no** ``deactivated_at`` filter: the index it
    mirrors has none either (module docstring).
    """
    return select(MinistryRole).where(
        MinistryRole.ministry_id == ministry_id,
        func.lower(MinistryRole.name) == name.lower(),
    )


def _find_role_by_name(
    session: Session, *, ministry_id: int, name: str
) -> MinistryRole | None:
    stmt = _role_name_lookup_statement(ministry_id, name)
    return session.execute(stmt).scalar_one_or_none()


def _now_utc() -> datetime.datetime:
    """The current instant, timezone-aware in UTC (core §3.2).

    Matches :func:`app.services.scheduling_period._now_utc` exactly, for the
    same reason: a substitutable "now" for deterministic testing.
    """
    return datetime.datetime.now(datetime.timezone.utc)


def _next_display_order(session: Session, ministry_id: int) -> int:
    """One past this ministry's current maximum ``display_order``, across
    active and deactivated roles alike, or ``0`` for its first role.

    A plain ``MAX`` query, not a stored counter: this runs once per creation,
    which is rare enough that recomputing it costs nothing, and it can never
    drift from what the table actually holds.
    """
    current_max = session.execute(
        select(func.max(MinistryRole.display_order)).where(
            MinistryRole.ministry_id == ministry_id
        )
    ).scalar_one()
    return 0 if current_max is None else current_max + 1
