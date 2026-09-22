"""Recording, changing the reason on, and removing explicit ExistingCommitment rows.

Implements the accepted design in
``docs/architecture/scheduling-input-data-model.md`` §10 and
``docs/adr/0002-sunday-conflict-sources-of-truth.md``, and the authorization
rules in ``docs/architecture/core-data-model.md`` §4.2--§4.3. Section numbers
below refer to the scheduling-input document unless stated.

**[APPROVED] ExistingCommitment is the explicit half of the church-wide Sunday
conflict rule** (ADR 0002): a known commitment this system does not manage --
already serving AV, Admin duty, preaching, another known responsibility --
entered so the scheduler can treat it as a hard block. **The block is
church-wide** and references the canonical Person, never a
MinistryMembership: it blocks every ministry equally, not just the one that
sourced it. ``source_ministry`` is *provenance, not scope* -- it records why
the person is blocked, and this service deliberately never requires the
target Person to hold a membership in it (§10's own reasoning, restated).

**[APPROVED] Never a mirror of Assignment** (ADR 0002, §10, this task). There
is no ``assignment_id`` on the model and none is added here. Future
authoritative, finalized assignments are the *other* source of Sunday-conflict
information, computed at query time as
``existing_commitment UNION authoritative_assignment`` -- this service manages
only the explicit side and touches nothing else.

**[APPROVED] "Not ordinary availability."** A person's own "I can't make the
12th" belongs to :mod:`app.services.availability`, in the ministry context
where it was given (§10). Filing it here instead would create a second,
competing representation of the same fact with no rule for which one wins.

**Two authorization shapes, driven by whether there is a source ministry**
(this task): with one, the check is
:func:`app.services.authorization.require_ministry_operator` for that specific
ministry -- provenance implies who may manage the record, and recording that a
ministry has committed somebody is that ministry's Head's act, not an
overseer's (Task 80). Without one --
preaching, or any responsibility that does not cleanly belong to one ministry
-- only :func:`app.services.authorization.require_active_admin` will do, since
there is no ministry to derive management from.

**The logical identity -- person, date, source ministry -- is not an in-place
field.** :func:`set_existing_commitment` finds or creates a row keyed on
exactly the columns the database's own
``uq_existing_commitment_person_date_source`` unique index is built from
(``NULLS NOT DISTINCT``, so a missing source ministry participates in
uniqueness rather than escaping it), and the only thing it will ever change on
an existing row is ``reason``. Correcting the person, the date, or the source
ministry is explicitly a remove-then-create, deliberately, so that what a row
represents never changes silently underneath its own history -- see
:func:`remove_existing_commitment` and :func:`set_existing_commitment`.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, Person
from app.models.scheduling_input import ExistingCommitment
from app.services.audit import (
    ACTION_EXISTING_COMMITMENT_CHANGED,
    ACTION_EXISTING_COMMITMENT_RECORDED,
    ACTION_EXISTING_COMMITMENT_REMOVED,
    record_audit_event,
)
from app.services.authorization import require_active_admin, require_ministry_operator
from app.services.errors import InvalidOperationError

_TARGET_TABLE = "existing_commitment"


def set_existing_commitment(
    session: Session,
    *,
    actor: Person,
    person: Person,
    commitment_date: datetime.date,
    source_ministry: Ministry | None = None,
    reason: str | None = None,
) -> ExistingCommitment:
    """Record a commitment, or update its standing reason, as ``actor``.

    The logical key is ``(person, commitment_date, source_ministry)`` --
    exactly the database's own ``NULLS NOT DISTINCT`` unique index, so a
    missing ``source_ministry`` still identifies one logical row rather than
    escaping the uniqueness rule. **Only ``reason`` is ever changed on an
    existing row.** If the person, the date, or the source ministry needs
    correcting, that is a different logical commitment: call
    :func:`remove_existing_commitment` and then this function again, rather
    than silently repointing a row at a different identity. That keeps
    authorization (which depends on the source ministry) and audit history
    honest -- an in-place identity change would let a row's whole provenance
    change without ever being individually authorized or recorded as the
    distinct acts they are.

    **Provenance is required** (the database's own ``provenance_required``
    CHECK, restated here so a violation is a domain error rather than an
    ``IntegrityError``): a commitment needs a source ministry, a reason, or
    both. ``source_ministry=None`` therefore makes ``reason`` mandatory. A
    supplied reason is trimmed before it is stored or compared -- so
    ``" Preaching "`` and ``"Preaching"`` are the same reason, and neither an
    idempotency check nor the stored value should see them as different.

    **Idempotent.** An existing row whose (normalized) reason already matches
    the request is returned unchanged -- no ``AuditEvent``, and **no target
    activity is re-validated**, exactly as the brief specifies: nothing is
    changing, so there is nothing to validate against.

    **Target activity.** Both creating a new row and changing an existing
    row's reason require the target Person to be active, and the source
    ministry (when given) to be active. Neither check runs for the idempotent
    no-op above, and neither applies to removal -- see
    :func:`remove_existing_commitment`.

    The mutation and its audit row are added to the same ``session`` and are
    written by the caller's commit, so they land together or not at all
    (:mod:`app.services`). A brand-new row is flushed once, immediately after
    it is added, to obtain the identity its audit row must reference; a reason
    change needs no flush, since the existing row already has one. This
    function never commits or rolls back.

    :raises AuthorizationError: with a source ministry, the actor is not an
        active Admin and not an active Ministry Head of it; without one, the
        actor is not an active Admin.
    :raises InvalidOperationError: no source ministry and no reason were
        given; a supplied reason is blank; the requested row would be newly
        recorded or reasoned against a deactivated target Person or a
        deactivated source ministry.
    """
    _require_authorized_actor(actor, source_ministry=source_ministry)
    reason = _validate_reason_and_provenance(source_ministry, reason)

    source_ministry_id = source_ministry.id if source_ministry is not None else None
    existing = _find_existing_commitment(
        session, person_id=person.id, commitment_date=commitment_date,
        source_ministry_id=source_ministry_id,
    )

    if existing is not None and existing.reason == reason:
        return existing

    _require_active_target_person(person)
    if source_ministry is not None:
        _require_active_source_ministry(source_ministry)

    if existing is None:
        return _record(
            session, actor=actor, person=person, commitment_date=commitment_date,
            source_ministry=source_ministry, reason=reason,
        )
    return _change_reason(
        session, actor=actor, person=person, commitment_date=commitment_date,
        source_ministry=source_ministry, existing=existing, reason=reason,
    )


def remove_existing_commitment(
    session: Session, *, actor: Person, commitment: ExistingCommitment
) -> None:
    """Remove ``commitment``, as ``actor``.

    Authorization is derived from ``commitment.source_ministry_id`` itself,
    never a caller-supplied ministry id: with one, the active Admin-or-Head
    rule for that specific ministry; without one, Admin only -- the same rule
    :func:`set_existing_commitment` used to create the row, now read from the
    row rather than asked of the caller.

    **Removal is exempt from target activity.** Unlike creating a row or
    changing its reason, this is cleanup -- it only ever *reduces* current
    blocking state -- so it remains usable even when the target Person or the
    source ministry has since been deactivated, on the same footing as
    :func:`~app.services.ministry_authority.revoke_ministry_head`. The acting
    Person must still be active; only the *target*'s activity is exempt.

    The audit row is built from the commitment's values before
    ``session.delete()`` is called, and both land in the same transaction
    regardless of order. No flush: the row already has an id, and nothing here
    needs to prove the deletion happened before the caller commits.

    :raises AuthorizationError: the actor is not authorized per the rule
        above.
    """
    if commitment.source_ministry_id is not None:
        require_ministry_operator(actor, ministry_id=commitment.source_ministry_id)
    else:
        require_active_admin(actor)

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_EXISTING_COMMITMENT_REMOVED,
        target_table=_TARGET_TABLE,
        target_id=commitment.id,
        ministry_id=commitment.source_ministry_id,
        summary=_summary(
            "Removed", person=commitment.person, commitment_date=commitment.commitment_date,
            source_ministry=commitment.source_ministry,
        ),
        before_values={
            "person_id": commitment.person_id,
            "commitment_date": commitment.commitment_date.isoformat(),
            "source_ministry_id": commitment.source_ministry_id,
            "reason": commitment.reason,
        },
    )
    # Genuine removal, not a soft-delete flag: the schema has no
    # deactivated_at column here.
    session.delete(commitment)


def _record(
    session: Session, *, actor: Person, person: Person,
    commitment_date: datetime.date, source_ministry: Ministry | None,
    reason: str | None,
) -> ExistingCommitment:
    source_ministry_id = source_ministry.id if source_ministry is not None else None
    commitment = ExistingCommitment(
        person_id=person.id,
        commitment_date=commitment_date,
        source_ministry_id=source_ministry_id,
        reason=reason,
    )
    session.add(commitment)
    # The audit row below needs a real target_id, and a new identity bigint
    # does not exist until the INSERT actually runs -- the minimum flush that
    # makes that true, scoped to the one pending row that needs it.
    session.flush([commitment])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_EXISTING_COMMITMENT_RECORDED,
        target_table=_TARGET_TABLE,
        target_id=commitment.id,
        # Provenance/management scope, not the block's own reach -- the
        # commitment blocks every ministry regardless (module docstring).
        ministry_id=source_ministry_id,
        summary=_summary(
            "Recorded", person=person, commitment_date=commitment_date,
            source_ministry=source_ministry,
        ),
        # Only meaningful business state, never the whole ORM row (audit
        # §7.2).
        after_values={
            "person_id": person.id,
            "commitment_date": commitment_date.isoformat(),
            "source_ministry_id": source_ministry_id,
            "reason": reason,
        },
    )
    return commitment


def _change_reason(
    session: Session, *, actor: Person, person: Person,
    commitment_date: datetime.date, source_ministry: Ministry | None,
    existing: ExistingCommitment, reason: str | None,
) -> ExistingCommitment:
    old_reason = existing.reason
    existing.reason = reason
    # No flush: the row already has an id, so nothing the audit row needs is
    # missing.
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_EXISTING_COMMITMENT_CHANGED,
        target_table=_TARGET_TABLE,
        target_id=existing.id,
        ministry_id=existing.source_ministry_id,
        summary=_summary(
            "Changed", person=person, commitment_date=commitment_date,
            source_ministry=source_ministry, changing_reason=True,
        ),
        # Only the one changed standing field (audit §7.2). person, date and
        # source ministry are the logical identity and never change here.
        before_values={"reason": old_reason},
        after_values={"reason": reason},
    )
    return existing


def _summary(
    verb: str, *, person: Person, commitment_date: datetime.date,
    source_ministry: Ministry | None, changing_reason: bool = False,
) -> str:
    """Names the person, the scope, and the date (audit §7.3, §14): the audit
    row carries no ``target_label``, so the summary is what stays readable
    once the commitment's context has to be reconstructed from history alone.
    """
    scope = source_ministry.name if source_ministry is not None else "church-wide"
    when = commitment_date.isoformat()
    if changing_reason:
        return f"Changed {person.display_name}'s {scope} commitment details for {when}"
    return f"{verb} {scope} commitment for {person.display_name} on {when}"


def _require_authorized_actor(actor: Person, *, source_ministry: Ministry | None) -> None:
    """With a source ministry, provenance implies who may manage the record
    (module docstring); without one, only an Admin can, since there is no
    ministry to derive management from.
    """
    if source_ministry is not None:
        require_ministry_operator(actor, ministry_id=source_ministry.id)
    else:
        require_active_admin(actor)


def _validate_reason_and_provenance(
    source_ministry: Ministry | None, reason: str | None
) -> str | None:
    """Every commitment must have meaningful provenance (the database's own
    ``provenance_required`` CHECK): a source ministry, a reason, or both.
    """
    normalized = _normalize_reason(reason)
    if source_ministry is None and normalized is None:
        raise InvalidOperationError(
            "reason is required when no source ministry is given"
        )
    return normalized


def _normalize_reason(reason: str | None) -> str | None:
    """Blank is not a reason, and interior/edge whitespace is trimmed so the
    value used for both storage and the idempotency comparison is stable.
    """
    if reason is None:
        return None
    normalized = reason.strip()
    if not normalized:
        raise InvalidOperationError("reason must not be blank when supplied")
    return normalized


def _require_active_target_person(person: Person) -> None:
    """Not called on the removal path -- see the asymmetry documented on
    :func:`remove_existing_commitment`.
    """
    if person.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot record or change an existing commitment"
            " for a deactivated person"
        )


def _require_active_source_ministry(source_ministry: Ministry) -> None:
    """Not called on the removal path, and not called at all when there is no
    source ministry.
    """
    if source_ministry.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot record or change an existing commitment"
            " for a deactivated source ministry"
        )


def _commitment_lookup_statement(
    person_id: int, commitment_date: datetime.date, source_ministry_id: int | None
) -> Select[tuple[ExistingCommitment]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_existing_commitment.py``).

    ``source_ministry_id == None`` deliberately relies on SQLAlchemy's
    standard behaviour of compiling a Python ``None`` comparison as
    ``IS NULL`` rather than ``= NULL`` -- the latter would never match in real
    SQL and would silently defeat the whole lookup for source-less
    commitments, exactly the case
    ``uq_existing_commitment_person_date_source``'s ``NULLS NOT DISTINCT``
    exists to protect.
    """
    return select(ExistingCommitment).where(
        ExistingCommitment.person_id == person_id,
        ExistingCommitment.commitment_date == commitment_date,
        ExistingCommitment.source_ministry_id == source_ministry_id,
    )


def _find_existing_commitment(
    session: Session, *, person_id: int, commitment_date: datetime.date,
    source_ministry_id: int | None,
) -> ExistingCommitment | None:
    """The standing row for this logical key, if one exists.

    Queried on exactly the three columns
    ``uq_existing_commitment_person_date_source`` is built from, via a plain
    SQLAlchemy ``select()``, no repository abstraction. Absence is a real
    domain state -- no block recorded -- and is returned as ``None`` rather
    than assumed to mean anything.
    """
    stmt = _commitment_lookup_statement(person_id, commitment_date, source_ministry_id)
    return session.execute(stmt).scalar_one_or_none()
