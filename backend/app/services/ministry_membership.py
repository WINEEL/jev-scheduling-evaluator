"""Who is on which team: the membership lifecycle (Task 79).

The ministry-scoped half of people management. :mod:`app.services
.person_directory` owns the church-wide Person; this module owns the
``ministry_membership`` rows hanging off it -- adding somebody to a ministry,
removing them from one, and editing the participation details that are scoped
to that one ministry.

**This is the service :mod:`app.services.ministry_authority` said was coming.**
Its docstring recorded the rule this module now implements, so that it would
not have to be rediscovered: core §5.1 requires that deactivating a membership
which currently holds head authority clears ``is_ministry_head`` **in the same
domain operation**, that doing so needs **Admin** authorization because it
necessarily removes head authority, and that the operation emits **two** audit
rows -- ``MINISTRY_HEAD_REVOKED`` and the removal -- because two things
happened. :func:`remove_person_from_ministry` does exactly that, and delegates
the revocation to :func:`app.services.ministry_authority.revoke_ministry_head`
rather than clearing the flag itself, so there is still only one piece of code
that takes head authority away.

Authorization, in one sentence each
-----------------------------------
- :func:`add_person_to_ministry`, :func:`remove_person_from_ministry`,
  :func:`update_membership` -- :func:`~app.services.authorization
  .require_ministry_operator` against **the membership's own ministry**: the
  active Ministry Head of that specific ministry. A Head of a different
  ministry is refused however active, which is what makes cross-ministry
  management impossible rather than merely undesirable.
- **A church-wide Admin who does not head the ministry is refused too**, and
  that is Task 79's governance rule rather than an oversight (§1, §18).
  Rostering a team is the job of whoever runs it; an Admin is an overseer, who
  may read every ministry in the church and may appoint somebody to lead one,
  but does not silently become every ministry's head. An Admin who *is* an
  active Head of this ministry passes -- through that membership, not through
  ``is_admin``.
- Removing a membership that **holds head authority** additionally requires an
  active Admin (core §5.1, above). So a Head cannot quietly depose a co-head by
  removing them from the team -- and, because both checks apply, neither can an
  Admin who does not lead that ministry. Taking authority away is
  :func:`app.services.ministry_authority.revoke_ministry_head`, which is
  Admin-only and needs no ministry membership at all; removing the person from
  the team afterwards is the ministry's own business.

**The ministry is always explicit.** Every function here takes the ministry, or
a membership that names one; none of them infers "the actor's ministry" from
the actor. A Head who leads three ministries therefore cannot change one of
them by accident, and the authorization check always has a specific id to test
against rather than a set to search.

Removal is deactivation, never deletion
---------------------------------------
:func:`remove_person_from_ministry` writes one timestamp. It issues no
``DELETE``, and there is no operation in this module that does. Every
assignment, availability answer, serving limit, qualification and audit row
referencing the membership survives -- which is required, because every foreign
key into ``ministry_membership`` is ``ON DELETE RESTRICT`` (core §6) and a
delete would be refused by the database anyway.

**Rejoining reactivates, it never inserts a second row.** The database's
``uq_ministry_membership_person_id_ministry_id`` constraint spans active and
inactive rows, so :func:`add_person_to_ministry` looks for an existing
membership first and clears its ``deactivated_at``. That is what keeps somebody
who left and came back attached to their own serving history rather than
starting again as a stranger.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryMembership, Person
from app.services.audit import (
    ACTION_MINISTRY_MEMBERSHIP_ADDED,
    ACTION_MINISTRY_MEMBERSHIP_CHANGED,
    ACTION_MINISTRY_MEMBERSHIP_REMOVED,
    record_audit_event,
)
from app.services.authorization import require_active_admin, require_ministry_operator
from app.services.errors import InvalidOperationError
from app.services.ministry_authority import (
    grant_ministry_head,
    revoke_ministry_head,
)

__all__ = [
    "add_person_to_ministry",
    "find_membership",
    "remove_person_from_ministry",
    "set_ministry_head_authority",
    "update_membership",
]

_TARGET_TABLE = "ministry_membership"


def find_membership(
    session: Session, *, person_id: int, ministry_id: int
) -> MinistryMembership | None:
    """The one membership joining this person and this ministry, or ``None``.

    **Active or not.** The unique constraint spans both states, so "do they
    already have a membership here?" has exactly one answer and a deactivated
    row is still that answer -- it is the row a rejoin must reactivate rather
    than duplicate.

    No authorization check: this is a lookup a caller performs *before*
    deciding which authorized operation to run, and every one of those
    operations checks for itself.
    """
    return session.execute(
        _membership_lookup_statement(person_id=person_id, ministry_id=ministry_id)
    ).scalar_one_or_none()


def add_person_to_ministry(
    session: Session,
    *,
    actor: Person,
    person: Person,
    ministry: Ministry,
    notes: str | None = None,
    joined_on: datetime.date | None = None,
    reason: str | None = None,
) -> MinistryMembership:
    """Put ``person`` on ``ministry``'s team, as ``actor``.

    Authorized against **this ministry** with
    :func:`~app.services.authorization.require_ministry_operator`, so a Head
    may add to their own team and to no other -- and an Admin who does not head
    it may not add to it at all (module docstring).

    **Three outcomes, one of which writes nothing:**

    - *No membership exists* -- a row is created and
      ``MINISTRY_MEMBERSHIP_ADDED`` is recorded.
    - *A deactivated membership exists* -- it is **reactivated**, and the same
      action is recorded. A second row is never inserted: the unique constraint
      forbids it, and more importantly the old row is what every past
      assignment and qualification of theirs points at (module docstring).
      ``notes`` and ``joined_on`` are applied if supplied and otherwise left
      exactly as they were, so rejoining does not silently erase what the
      previous head wrote about them.
    - *An active membership already exists* -- **idempotent**: it is returned
      unchanged and **no audit row is written**, because no domain state
      changed. Adding somebody who is already on the team is a duplicate
      request, not an error; the caller's screen is simply out of date.

    Reactivation deliberately does **not** restore head authority. The flag was
    cleared when the membership was removed, and giving it back is an Admin's
    explicit act through :func:`app.services.ministry_authority
    .grant_ministry_head` -- never a side effect of somebody rejoining a team.

    :raises AuthorizationError: the actor is not an active Ministry Head of
        ``ministry``.
    :raises InvalidOperationError: ``ministry`` is deactivated, ``person`` is
        deactivated church-wide, or ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=ministry.id)
    reason = _validate_optional_reason(reason)
    return _add_or_restore_membership(
        session,
        actor=actor,
        person=person,
        ministry=ministry,
        notes=notes,
        joined_on=joined_on,
        reason=reason,
    )


def _add_or_restore_membership(
    session: Session,
    *,
    actor: Person,
    person: Person,
    ministry: Ministry,
    notes: str | None,
    joined_on: datetime.date | None,
    reason: str | None,
) -> MinistryMembership:
    """The membership write itself, with **no authorization check of its own**.

    Extracted because two differently-authorized operations perform exactly
    this write and must not drift apart: :func:`add_person_to_ministry`, which
    is the ministry's Head rostering their team, and
    :func:`set_ministry_head_authority`, which is an Admin appointing somebody
    to lead a ministry that may have no members at all yet.

    Private, and deliberately so: every caller must have answered "who may do
    this?" before reaching it, and there is no path to it from outside this
    module.
    """
    if ministry.deactivated_at is not None:
        raise InvalidOperationError("cannot add a person to a deactivated ministry")

    # A person who has left the church must not be quietly put back on a rota
    # through a ministry-scoped action -- that would let a Head undo an Admin's
    # church-wide decision without the authority to make it. Reactivating them
    # first is the Admin's call (:func:`person_directory.reactivate_person`).
    if person.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot add a person who is deactivated church-wide;"
            " an Admin must reactivate them first"
        )

    notes = _optional_text(notes)
    existing = find_membership(
        session, person_id=person.id, ministry_id=ministry.id
    )

    if existing is not None and existing.deactivated_at is None:
        return existing

    if existing is not None:
        membership = existing
        membership.deactivated_at = None
        if notes is not None:
            membership.notes = notes
        if joined_on is not None:
            membership.joined_on = joined_on
        rejoined = True
    else:
        membership = MinistryMembership(
            person_id=person.id,
            ministry_id=ministry.id,
            notes=notes,
            joined_on=joined_on,
            # Never accepted as a parameter, here or anywhere else in this
            # module. Head authority is granted only by an Admin through
            # :mod:`app.services.ministry_authority`; a Head who could pass it
            # to this function would be promoting people by adding them.
            is_ministry_head=False,
        )
        # Both relationships are populated explicitly on the new row. In
        # production they would lazy-load on first access -- the audit summary
        # below reads both names -- so this saves two round trips; more
        # importantly, a caller that hands the fresh membership to another
        # operation in the same transaction (as
        # :func:`set_ministry_head_authority` does) finds them already there
        # rather than on a row the Session has not yet associated with either
        # parent.
        membership.person = person
        membership.ministry = ministry
        session.add(membership)
        rejoined = False

    # One flush, on both paths, and it is doing two jobs. The new row needs an
    # identity before the audit row that references it can be built -- and the
    # *reactivated* row needs to be in the database before any caller reads
    # this person's memberships back in the same transaction. The application's
    # Session runs with ``autoflush=False``, so without this a read-back would
    # answer with the pre-reactivation state and a screen would show somebody
    # still removed a moment after they were restored.
    #
    # A flush, never a commit: the caller still owns the transaction
    # (see :mod:`app.services`).
    session.flush()

    verb = "Restored" if rejoined else "Added"
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MINISTRY_MEMBERSHIP_ADDED,
        target_table=_TARGET_TABLE,
        target_id=membership.id,
        ministry_id=membership.ministry_id,
        summary=(
            f"{verb} {person.display_name} to {ministry.name}"
        ),
        reason=reason,
        before_values={"member": False},
        after_values={"member": True},
    )
    return membership


def remove_person_from_ministry(
    session: Session,
    *,
    actor: Person,
    membership: MinistryMembership,
    reason: str | None = None,
    now: datetime.datetime | None = None,
) -> MinistryMembership:
    """Take ``membership``'s person off that one ministry's team, as ``actor``.

    **This changes one ministry and nothing else.** It is *not* the church-wide
    act -- that is
    :func:`app.services.person_directory.deactivate_person`, and it is
    Admin-only. Somebody removed from AV is still in the church, still in every
    other ministry they belong to, and still on every past AV rota.

    **Nothing is deleted.** One timestamp is written. Every assignment,
    availability answer, serving limit, qualification and audit row naming this
    membership is untouched, and the database would refuse a delete in any case
    (module docstring).

    **Removing a head requires an Admin** (core §5.1). Head authority cannot
    outlive the participation it came from, so a removal necessarily revokes
    it -- and revoking head authority is Admin-only. A Ministry Head attempting
    to remove a co-head is therefore refused **before anything is mutated**,
    rather than discovering the problem halfway through. When an Admin does it,
    :func:`app.services.ministry_authority.revoke_ministry_head` performs the
    revocation, so **two** audit rows land in one transaction:
    ``MINISTRY_HEAD_REVOKED`` and ``MINISTRY_MEMBERSHIP_REMOVED``. Two things
    happened, and both are worth seeing.

    **Idempotent**: an already-removed membership is returned unchanged, with
    no audit row and its original timestamp preserved rather than refreshed.

    :raises AuthorizationError: the actor is not an active Ministry Head of
        this membership's ministry, or the membership holds head authority and
        the actor is not also an active Admin.
    :raises InvalidOperationError: ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=membership.ministry_id)
    reason = _validate_optional_reason(reason)

    if membership.deactivated_at is not None:
        return membership

    if membership.is_ministry_head:
        # `revoke_ministry_head` applies this same check itself, and the
        # duplication is deliberate: core §5.1's rule is a rule *about
        # removal*, and a reader of this function should be able to see why a
        # Head is refused here without first going to read another module.
        require_active_admin(actor)
        revoke_ministry_head(
            session, actor=actor, membership=membership, reason=reason
        )

    membership.deactivated_at = now if now is not None else _now()

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MINISTRY_MEMBERSHIP_REMOVED,
        target_table=_TARGET_TABLE,
        target_id=membership.id,
        ministry_id=membership.ministry_id,
        summary=(
            f"Removed {_person_name(membership)} from"
            f" {_ministry_name(membership)}"
        ),
        reason=reason,
        before_values={"member": True},
        after_values={"member": False},
    )
    return membership


def update_membership(
    session: Session,
    *,
    actor: Person,
    membership: MinistryMembership,
    notes: str | None = None,
    joined_on: datetime.date | None = None,
    reason: str | None = None,
) -> MinistryMembership:
    """Replace the participation details scoped to this one ministry.

    Authorized against the membership's own ministry, so this is the
    ministry-specific membership information a Head may manage for a ministry
    they lead.

    **Two fields, and both are genuinely ministry-scoped**: free-prose ``notes``
    that this ministry's head keeps about this person's participation, and the
    ``joined_on`` date they joined *this* team. Nothing church-wide is reachable
    from here -- a Head editing a membership cannot rename the person, change
    their contact details, alter their sign-in address or touch their active
    state, because none of those is a field on this row. That is ADR 0001's
    separation doing its job rather than a rule this function has to remember.

    ``is_ministry_head`` is deliberately not among them: authority is Admin-only
    and has its own two operations in
    :mod:`app.services.ministry_authority`.

    A full replace of both fields, not a per-field patch -- the same shape
    :func:`app.services.ministry_role.update_ministry_role` uses.

    **Idempotent**: if neither field differs, nothing changes and no audit row
    is written.

    :raises AuthorizationError: the actor may not manage this membership's
        ministry.
    :raises InvalidOperationError: ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=membership.ministry_id)
    reason = _validate_optional_reason(reason)

    notes = _optional_text(notes)

    before = {"notes": membership.notes, "joined_on": _iso_or_none(membership.joined_on)}
    after = {"notes": notes, "joined_on": _iso_or_none(joined_on)}
    if before == after:
        return membership

    membership.notes = notes
    membership.joined_on = joined_on

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MINISTRY_MEMBERSHIP_CHANGED,
        target_table=_TARGET_TABLE,
        target_id=membership.id,
        ministry_id=membership.ministry_id,
        summary=(
            f"Updated {_person_name(membership)}'s membership of"
            f" {_ministry_name(membership)}"
        ),
        reason=reason,
        before_values=before,
        after_values=after,
    )
    return membership


def set_ministry_head_authority(
    session: Session,
    *,
    actor: Person,
    person: Person,
    ministry: Ministry,
    is_ministry_head: bool,
    reason: str | None = None,
) -> MinistryMembership:
    """Appoint ``person`` to lead ``ministry``, or take that authority away.

    **Church governance, and therefore active Admin only** (core §4.3,
    Task 79 §10). A Ministry Head cannot promote anybody -- not in their own
    ministry, not in another, and not themselves. Head authority propagates
    only from an Admin.

    **Person and ministry are both explicit**, which is what §10 asks for: an
    Admin chooses the human, chooses the team, and says grant or revoke. There
    is no "the actor's ministry" and nothing is inferred.

    **Granting creates the membership if there is not one**, and reactivates a
    removed one. That is not a back door into
    :func:`add_person_to_ministry`'s authorization -- it is the only way a
    ministry can ever acquire its *first* head. A newly created ministry has no
    members, so nobody heads it, so under
    :func:`~app.services.authorization.require_ministry_operator` nobody may
    add anyone to it. Appointing its leader is precisely the governance act
    that breaks that circle, and it is an Admin's to make. Two audit rows land
    in one transaction when a membership was created or restored --
    ``MINISTRY_MEMBERSHIP_ADDED`` and ``MINISTRY_HEAD_GRANTED`` -- because two
    things happened.

    **Revoking leaves the ordinary membership exactly where it is.** One
    boolean is cleared: they stay on the team, keep every qualification and
    every past assignment, and simply no longer lead it (Task 79 §10). Revoking
    from somebody who has no membership at all is a no-op rather than an error,
    since there is no authority to take away -- and it therefore never creates
    a membership, which would be an odd way to demote somebody.

    Both directions are **idempotent**, inheriting that from
    :mod:`app.services.ministry_authority`: restating the authority somebody
    already has or lacks writes no audit row.

    :raises AuthorizationError: the actor is not an active Admin.
    :raises InvalidOperationError: granting into a deactivated ministry or to a
        person deactivated church-wide, revoking where no membership exists, or
        ``reason`` supplied but blank.
    """
    require_active_admin(actor)
    reason = _validate_optional_reason(reason)

    if not is_ministry_head:
        membership = find_membership(
            session, person_id=person.id, ministry_id=ministry.id
        )
        if membership is None:
            raise InvalidOperationError(
                "that person has no membership of this ministry, so there is"
                " no Ministry Head authority to revoke"
            )
        return revoke_ministry_head(
            session, actor=actor, membership=membership, reason=reason
        )

    membership = _add_or_restore_membership(
        session,
        actor=actor,
        person=person,
        ministry=ministry,
        notes=None,
        joined_on=None,
        reason=reason,
    )
    return grant_ministry_head(
        session, actor=actor, membership=membership, reason=reason
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _membership_lookup_statement(
    *, person_id: int, ministry_id: int
) -> Select[tuple[MinistryMembership]]:
    """Split out from its execution so it can be tested without a database --
    a compiled ``Select`` is inspectable on its own, the same convention
    :mod:`app.services.role_qualification` uses.
    """
    return select(MinistryMembership).where(
        MinistryMembership.person_id == person_id,
        MinistryMembership.ministry_id == ministry_id,
    )


def _iso_or_none(value: datetime.date | None) -> str | None:
    """Dates are rendered for the JSONB audit payload, which has no date type.

    ISO 8601, so a reader gets ``"2026-03-01"`` rather than an opaque number,
    and so two payloads written months apart compare as equal strings.
    """
    return None if value is None else value.isoformat()


def _optional_text(value: str | None) -> str | None:
    """Trimmed, with whitespace-only collapsing to ``None`` -- an empty text
    box means "no note", not "a note made of spaces".
    """
    if value is None:
        return None
    candidate = value.strip()
    return candidate if candidate else None


def _validate_optional_reason(reason: str | None) -> str | None:
    """Optional, but blank is not a reason. Audit §9: no new universal
    requirement is invented here (see
    :func:`app.services.ministry_authority._validate_optional_reason`).
    """
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


def _person_name(membership: MinistryMembership) -> str:
    """Reached through the relationship, which lazy-loads if it is not already
    loaded -- one small query in a rare, human-driven operation, and the
    readable summary is what names the people involved once the membership
    itself is gone (audit §14). The same trade
    :mod:`app.services.ministry_authority` already makes.
    """
    return membership.person.display_name


def _ministry_name(membership: MinistryMembership) -> str:
    return membership.ministry.name


def _now() -> datetime.datetime:
    """Timezone-aware UTC, matching every other ``deactivated_at`` writer."""
    return datetime.datetime.now(datetime.timezone.utc)
