"""Granting and revoking Ministry Head authority.

Implements the approved authorization rules in
``docs/architecture/core-data-model.md`` §4.3, §5 and §5.1, and records each
change as an audit event per ``docs/architecture/audit-event-data-model.md``
§22.1. Section numbers below refer to the core document unless stated.

**[APPROVED] Only an Admin may promote or revoke a Ministry Head** (§4.3). A
Ministry Head cannot promote another Ministry Head -- not in their own ministry
and not in any other. Head authority propagates only from an Admin, which is why
these two functions test ``person.is_admin`` and nothing else: managing a
ministry confers no authority over who leads it.

Head authority is a column on ``ministry_membership`` rather than a table (§5),
so promotion and revocation leave no trace of their own. **The audit row is the
only record that either happened** -- core §5 withdrew a
``ministry_head_assignment`` history table on exactly that understanding. Losing
the audit write would therefore lose the history entirely, which is why it
shares the mutation's transaction rather than being best-effort.

Not implemented here, deliberately
----------------------------------
**Membership deactivation.** §5.1 established that deactivating a membership
which currently holds head authority must clear ``is_ministry_head`` **in the
same domain operation**, and that doing so requires **Admin** authorization
because it necessarily removes head authority. The database enforces the
resulting invariant -- ``CHECK (NOT is_ministry_head OR deactivated_at IS
NULL)`` -- so a deactivation that forgets to clear the flag is rejected rather
than discovered later as a stale permission.

That operation belongs to the membership-lifecycle service, which is a later
task. It is recorded here so the rule is not rediscovered from scratch: when it
is built, it emits **two** audit rows in one transaction --
``MINISTRY_HEAD_REVOKED`` and the deactivation event -- because two things
happened and both are worth seeing (audit §22.1).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import MinistryMembership, Person
from app.services.audit import (
    ACTION_MINISTRY_HEAD_GRANTED,
    ACTION_MINISTRY_HEAD_REVOKED,
    record_audit_event,
)
from app.services.authorization import require_active_admin
from app.services.errors import InvalidOperationError

#: The audited table these operations target (audit §5.2). A literal, because
#: the reference is generic and unenforced: nothing in the database ties
#: ``target_id`` to ``ministry_membership.id``, so the service is what makes it
#: true (ADR 0004).
_TARGET_TABLE = "ministry_membership"


def grant_ministry_head(
    session: Session,
    *,
    actor: Person,
    membership: MinistryMembership,
    reason: str | None = None,
) -> MinistryMembership:
    """Give ``membership`` Ministry Head authority, as ``actor``.

    Both the flag change and its audit row are added to ``session`` and are
    written by the caller's commit, so they land together or not at all.

    **Idempotent.** If the membership already holds head authority, nothing is
    changed and **no audit row is written**: no domain state changed, so there
    is no act to record. Writing one anyway would put events in the history that
    a reader could not distinguish from real promotions, and would inflate any
    later "how often has this happened?" answer.

    :raises AuthorizationError: the actor is not an active Admin.
    :raises InvalidOperationError: the membership is deactivated, the person is
        globally deactivated, or ``reason`` was supplied but blank.
    """
    require_active_admin(actor)
    reason = _validate_optional_reason(reason)

    # §5.1: head authority may be conferred only through an active membership,
    # and the database enforces it. Checking here turns an IntegrityError at
    # commit -- which aborts the caller's whole transaction, far from its cause
    # -- into a meaningful domain error raised before anything is mutated.
    if membership.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot grant Ministry Head authority to a deactivated membership"
        )

    # A globally deactivated Person must not be given authority even through a
    # membership that is itself still active. The two deactivations are separate
    # facts (core §6): `person.deactivated_at` means the person has left the
    # church, `membership.deactivated_at` means they left one ministry, and
    # neither implies the other. The database cannot express this one -- it is a
    # condition on the parent row, which no CHECK can reach -- so unlike the
    # membership rule above, the service is the only place it can be enforced.
    #
    # Deliberately checked *before* the idempotency return below: if a
    # deactivated person somehow already holds the flag, that is inconsistent
    # state to be reported, not a success to be confirmed silently.
    if membership.person.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot grant Ministry Head authority to a deactivated person"
        )

    if membership.is_ministry_head:
        return membership

    membership.is_ministry_head = True
    _record_authority_change(
        session,
        actor=actor,
        membership=membership,
        action=ACTION_MINISTRY_HEAD_GRANTED,
        summary=(
            f"Granted Ministry Head authority to {_person_name(membership)}"
            f" for {_ministry_name(membership)}"
        ),
        granted=True,
        reason=reason,
    )
    return membership


def revoke_ministry_head(
    session: Session,
    *,
    actor: Person,
    membership: MinistryMembership,
    reason: str | None = None,
) -> MinistryMembership:
    """Remove ``membership``'s Ministry Head authority, as ``actor``.

    **Idempotent**, for the same reason as :func:`grant_ministry_head`: if the
    membership does not hold head authority, nothing changed and no audit row is
    written.

    **Deliberately permissive about deactivation, unlike**
    :func:`grant_ministry_head`. Neither the membership's nor the person's
    deactivation is checked here, and that asymmetry is the point: revocation is
    also the **state-repair** operation. If authority somehow exists for a person
    who has left the church -- through an ordering mistake, a data fix, or a
    write that predates this validation -- an Admin must still be able to take it
    away. Refusing to revoke would leave the only tool for correcting the problem
    blocked by the problem itself.

    Granting asks "should this person have authority?", so it applies every
    eligibility rule. Revoking only ever reduces authority, so it needs none of
    them.

    :raises AuthorizationError: the actor is not an active Admin.
    :raises InvalidOperationError: ``reason`` was supplied but blank.
    """
    require_active_admin(actor)
    reason = _validate_optional_reason(reason)

    if not membership.is_ministry_head:
        return membership

    membership.is_ministry_head = False
    _record_authority_change(
        session,
        actor=actor,
        membership=membership,
        action=ACTION_MINISTRY_HEAD_REVOKED,
        summary=(
            f"Revoked Ministry Head authority from {_person_name(membership)}"
            f" for {_ministry_name(membership)}"
        ),
        granted=False,
        reason=reason,
    )
    return membership


def _validate_optional_reason(reason: str | None) -> str | None:
    """A reason is optional for these operations, but blank is not a reason.

    **[REVIEWED]** audit §9: for V1 a reason is mandatory only where accepted
    product behaviour already requires one -- a manual assignment override and a
    schedule amendment. Head promotion and revocation may record one when the
    Admin supplies it, and no new universal requirement is invented here.
    """
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


def _record_authority_change(
    session: Session,
    *,
    actor: Person,
    membership: MinistryMembership,
    action: str,
    summary: str,
    granted: bool,
    reason: str | None,
) -> AuditEvent:
    """Write the audit row for one head-authority change.

    ``target_id`` and ``ministry_id`` are taken from the membership itself and
    are never accepted as parameters: the generic target reference has no
    foreign key (ADR 0004), so a caller-supplied ministry id could silently
    scope the row to the wrong ministry -- and ``ministry_id`` is what a later
    Ministry Head's audit view filters on (audit §8, §17).

    Only the one changed business field goes into the payload (audit §7.2).
    Never the whole membership row: ``notes`` and ``joined_on`` did not change,
    and ``updated_at`` changing tells a reader nothing.
    """
    return record_audit_event(
        session,
        actor=actor,
        action=action,
        target_table=_TARGET_TABLE,
        target_id=membership.id,
        ministry_id=membership.ministry_id,
        summary=summary,
        reason=reason,
        before_values={"is_ministry_head": not granted},
        after_values={"is_ministry_head": granted},
    )


def _person_name(membership: MinistryMembership) -> str:
    """The person's name for the summary sentence.

    Reached through the relationship, which lazy-loads if it is not already
    loaded. That is one small query in an operation that is rare and
    Admin-driven, and the readable summary is worth it: since the audit row
    carries no ``target_label``, the summary is what names the people involved
    once the membership itself is gone (audit §14).
    """
    return membership.person.display_name


def _ministry_name(membership: MinistryMembership) -> str:
    return membership.ministry.name
