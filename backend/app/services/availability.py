"""Recording, changing, clearing, and listing a MinistryMembership's
Availability for an Event.

Implements the accepted design in
``docs/architecture/scheduling-input-data-model.md`` §8 and the authorization
rules in ``docs/architecture/core-data-model.md`` §4.2--§4.3. Section numbers
below refer to the scheduling-input document unless stated.

**[APPROVED] Four situations, and only three of them are rows** (§8, widened
by Task 52):

- no row                               -> no response
- row, ``availability_state=AVAILABLE``   -> they said they can serve
- row, ``availability_state=BACKUP``      -> they can serve, but only if an
  ordinarily AVAILABLE candidate cannot fill the position (Task 52)
- row, ``availability_state=UNAVAILABLE`` -> they said they cannot

**Absence is not a fourth stored state, and it is not any explicit state.**
There is no ``UNKNOWN`` / ``NO_RESPONSE`` value in the schema (§8), and this
service introduces none either: passing ``availability_state=None`` to
:func:`set_availability` means *return to no response*, which for an existing
row is a genuine delete and for an absent row is a pure no-op -- never a row
that reads ``NO_RESPONSE``.

**[Task 56] Listing is read-only and never invents a fourth stored state.**
:func:`list_event_availability` reports ``availability_state`` as ``None``
for "no response", exactly mirroring the write side; it never returns a
placeholder string for absence, and it changes no scheduling, staleness, or
snapshot behavior -- it only reads the same rows :func:`set_availability`
already writes.

**[APPROVED] "Blank means available" is not this service's business** (§9).
Setup's historical spreadsheet-import policy about what a blank cell means is
applied later, when solver input is built from these rows; it must never be
encoded as a stored ``AVAILABLE`` row here, which would fabricate a decision
the person never made.

**V1 scope: only this ministry's Head records availability** (this task,
narrowed by Task 80). Self-service -- a member editing their own row -- is
explicitly deferred; this module has no notion of "the acting person is also
the subject."

**Head-scoped writes, reader-scoped reads** (core §4.2--§4.3, §4.3.1).
Listing takes :func:`app.services.authorization.require_ministry_reader`
(an Admin overseeing the church, or this ministry's active Head); every
write takes :func:`app.services.authorization.require_ministry_operator`
(this ministry's active Head, and nobody else -- Task 80). An Admin who
heads nothing may read all of this and change none of it.

**The SchedulingPeriod availability lock is a cross-row lifecycle rule, not a
database CHECK** (§3, this task): once
``event.scheduling_period.availability_locked_at`` is set, the collected input
set must stop changing through ordinary availability operations. No database
constraint can see a period through an event through an availability row, so
this service is where the rule lives -- see :func:`_require_period_not_locked`.
Unlocking, and any override mechanism, is explicitly out of scope here.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, Person
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_BACKUP,
    AVAILABILITY_UNAVAILABLE,
    Availability,
    Event,
)
from app.services.audit import (
    ACTION_AVAILABILITY_CHANGED,
    ACTION_AVAILABILITY_CLEARED,
    ACTION_AVAILABILITY_RECORDED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError

_TARGET_TABLE = "availability"
_VALID_STATES = (AVAILABILITY_AVAILABLE, AVAILABILITY_BACKUP, AVAILABILITY_UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class MembershipAvailability:
    """One membership of the event's ministry, and its stored answer for
    this event, if any.

    ``availability_state`` is ``None`` for "no response" (§8) -- never a
    stored default, and never a placeholder string. A head choosing who to
    ask needs to see the real distinction between "said nothing" and any of
    the three explicit answers.
    """

    ministry_membership_id: int
    person_id: int
    person_display_name: str
    membership_deactivated_at: datetime.datetime | None
    person_deactivated_at: datetime.datetime | None
    #: ``None`` = no response; otherwise one of ``_VALID_STATES``.
    availability_state: str | None


@dataclass(frozen=True, slots=True)
class EventAvailability:
    """One event's whole availability picture, for the head recording it."""

    event_id: int
    event_date: datetime.date
    event_name: str | None
    event_kind: str
    ministry_id: int
    #: ``None`` while availability is still open for this event's period.
    #: Mirrors ``SchedulingPeriod.availability_locked_at`` exactly -- reported
    #: here so a caller does not have to fetch the period separately just to
    #: know whether a change would be refused.
    availability_locked_at: datetime.datetime | None
    memberships: tuple[MembershipAvailability, ...]


def list_event_availability(
    session: Session,
    *,
    actor: Person,
    event: Event,
    include_inactive: bool = False,
) -> EventAvailability:
    """``event``'s ministry's memberships, each with its availability state
    for ``event``.

    **Read-only.** Ordered by ``lower(person.display_name)`` then
    ``ministry_membership_id``, matching
    :func:`app.services.role_qualification.list_role_qualifications` and
    :func:`app.services.staffing_requirement.list_event_staffing_requirements`.

    ``include_inactive=False`` (the default) returns only active memberships
    of currently active people. Passing ``True`` additionally returns
    deactivated memberships and memberships of deactivated people, so a head
    can still see (and, per :func:`set_availability`'s own removal
    exemption, still clear) a stray answer against someone who has since
    left.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``event.ministry_id``.
    """
    require_ministry_reader(actor, ministry_id=event.ministry_id)

    rows = session.execute(
        _event_availability_statement(
            event.id, event.ministry_id, include_inactive=include_inactive
        )
    ).all()
    return EventAvailability(
        event_id=event.id,
        event_date=event.event_date,
        event_name=event.name,
        event_kind=event.event_kind,
        ministry_id=event.ministry_id,
        availability_locked_at=event.scheduling_period.availability_locked_at,
        memberships=tuple(
            MembershipAvailability(
                ministry_membership_id=row.ministry_membership_id,
                person_id=row.person_id,
                person_display_name=row.person_display_name,
                membership_deactivated_at=row.membership_deactivated_at,
                person_deactivated_at=row.person_deactivated_at,
                availability_state=row.availability_state,
            )
            for row in rows
        ),
    )


def set_availability(
    session: Session,
    *,
    actor: Person,
    membership: MinistryMembership,
    event: Event,
    availability_state: str | None,
    reason: str | None = None,
) -> Availability | None:
    """Record, change, or clear ``membership``'s availability for ``event``.

    One explicit setter, matching the shape of
    :func:`app.services.role_qualification.set_role_qualification` and
    :func:`app.services.staffing_requirement.set_staffing_requirement`:
    ``availability_state="AVAILABLE"`` or ``"UNAVAILABLE"`` records or changes
    the explicit answer; ``availability_state=None`` clears it back to no
    response, which is a delete for an existing row and a no-op for an absent
    one.

    **A request that changes nothing is resolved before the SchedulingPeriod
    lock is even consulted.** Two shapes of no-op exist, and both are exempt:

    - no row **and** ``availability_state=None`` -- nothing to remove;
    - an existing row whose state already equals the requested one -- nothing
      to change.

    Both return immediately after authorization, ministry integrity, and input
    validation, without treating the call as an attempted availability change
    at all -- so a locked period never rejects a call that would not have
    changed anything. This is stated explicitly for the first shape in the
    Task 17 brief ("it should not fail merely because the period is locked")
    and is applied here to the second for the same reason: the lock section's
    own list of operations it must reject -- creating AVAILABLE, creating
    UNAVAILABLE, changing AVAILABLE <-> UNAVAILABLE, and removing a row --
    is precisely the set of **mutating** transitions, and deliberately omits
    both no-op shapes. The rule the lock exists to enforce is that "the
    collected input set must stop changing"; a call that would not change it
    is not the case that rule is protecting against, whichever no-op shape it
    takes.

    **Target activity, and the removal asymmetry.** Recording a new answer, or
    changing an existing one, requires the membership to be active, the person
    behind it to be active, and the event to be live (not cancelled) -- a
    person who has left the church or the ministry should not receive new
    availability decisions, and neither should a cancelled event. **Clearing an
    existing row is exempt from all three**: cleanup of a departed member's or
    a cancelled event's stray answer must remain possible, on the same footing
    as :func:`~app.services.ministry_authority.revoke_ministry_head` and the
    removal path of ``set_staffing_requirement``. **The SchedulingPeriod lock
    still applies to removal, however** -- unlike the activity checks, the lock
    protects the collected input set itself, and cleanup is not an exemption
    from that; unlocking is a separate, not-yet-built administrative operation
    (module docstring).

    The mutation and its audit row are added to the same ``session`` and are
    written by the caller's commit, so they land together or not at all
    (:mod:`app.services`). A brand-new row is flushed once, immediately after
    it is added, to obtain the identity its audit row must reference; a change
    or a removal needs no flush, since the existing row already has one. This
    function never commits or rolls back.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``event.ministry_id``.
    :raises InvalidOperationError: ``membership`` and ``event`` belong to
        different ministries; ``availability_state`` is neither ``"AVAILABLE"``,
        ``"UNAVAILABLE"``, nor ``None``; the scheduling period's availability is
        locked and this call would actually change something; the requested
        state would record or change an explicit answer against a deactivated
        membership, a deactivated person, or a cancelled event; or ``reason``
        was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=event.ministry_id)
    _require_same_ministry(membership, event)
    reason = _validate_optional_reason(reason)
    _require_valid_state(availability_state)

    existing = _find_existing_availability(
        session, ministry_membership_id=membership.id, event_id=event.id
    )

    if availability_state is None:
        if existing is None:
            return None  # true no-op: nothing to remove, lock not consulted
        _require_period_not_locked(event)
        _clear(session, actor=actor, membership=membership, event=event,
               existing=existing, reason=reason)
        return None

    if existing is not None and existing.availability_state == availability_state:
        return existing  # idempotent no-op, lock not consulted

    _require_period_not_locked(event)
    _require_active_target(membership, event)

    if existing is None:
        return _record(
            session, actor=actor, membership=membership, event=event,
            availability_state=availability_state, reason=reason,
        )
    return _change(
        session, actor=actor, membership=membership, event=event,
        existing=existing, availability_state=availability_state, reason=reason,
    )


def _record(
    session: Session, *, actor: Person, membership: MinistryMembership,
    event: Event, availability_state: str, reason: str | None,
) -> Availability:
    availability = Availability(
        ministry_membership_id=membership.id,
        event_id=event.id,
        # Shared by both composite foreign keys on the model; taken from the
        # event, which _require_same_ministry has already proven agrees with
        # the membership.
        ministry_id=event.ministry_id,
        availability_state=availability_state,
    )
    session.add(availability)
    # The audit row below needs a real target_id, and a new identity bigint
    # does not exist until the INSERT actually runs -- the minimum flush that
    # makes that true, scoped to the one pending row that needs it.
    session.flush([availability])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_AVAILABILITY_RECORDED,
        target_table=_TARGET_TABLE,
        target_id=availability.id,
        ministry_id=event.ministry_id,
        summary=(
            f"Recorded {membership.person.display_name} as"
            f" {_meaning(availability_state)} for {membership.ministry.name}"
            f" on {event.event_date.isoformat()}"
        ),
        reason=reason,
        # Only meaningful business state, never the whole ORM row (audit §7.2).
        after_values={
            "ministry_membership_id": membership.id,
            "event_id": event.id,
            "availability_state": availability_state,
        },
    )
    return availability


def _change(
    session: Session, *, actor: Person, membership: MinistryMembership,
    event: Event, existing: Availability, availability_state: str,
    reason: str | None,
) -> Availability:
    old_state = existing.availability_state
    existing.availability_state = availability_state
    # No flush: the row already has an id, so nothing the audit row needs is
    # missing.
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_AVAILABILITY_CHANGED,
        target_table=_TARGET_TABLE,
        target_id=existing.id,
        ministry_id=event.ministry_id,
        summary=(
            f"Changed {membership.person.display_name} to"
            f" {_meaning(availability_state)} for {membership.ministry.name}"
            f" on {event.event_date.isoformat()}"
        ),
        reason=reason,
        # Only the one changed business field (audit §7.2).
        before_values={"availability_state": old_state},
        after_values={"availability_state": availability_state},
    )
    return existing


def _clear(
    session: Session, *, actor: Person, membership: MinistryMembership,
    event: Event, existing: Availability, reason: str | None,
) -> None:
    # Captured, and the audit row built, before session.delete() is called --
    # clearer than relying on a pending-delete object's attributes staying
    # readable until flush. Both the audit insert and the delete land in the
    # same transaction regardless of which comes first in code.
    record_audit_event(
        session,
        actor=actor,
        action=ACTION_AVAILABILITY_CLEARED,
        target_table=_TARGET_TABLE,
        target_id=existing.id,
        ministry_id=event.ministry_id,
        summary=(
            f"Cleared {membership.person.display_name}'s"
            f" {membership.ministry.name} availability response"
            f" for {event.event_date.isoformat()}"
        ),
        reason=reason,
        before_values={
            "ministry_membership_id": existing.ministry_membership_id,
            "event_id": existing.event_id,
            "availability_state": existing.availability_state,
        },
    )
    # Genuine removal, not a soft-delete flag: the schema has no
    # deactivated_at column here, matching the model's own three-state design
    # (module docstring). No flush -- nothing here needs to prove the deletion
    # happened before the transaction commits.
    session.delete(existing)


#: Human-reading phrase for each stored state, for audit summaries only.
#: Exhaustive by construction -- ``_require_valid_state`` has already refused
#: anything outside ``_VALID_STATES`` by the time this is called, and a new
#: stored state added here without an entry raises rather than silently
#: reusing another state's wording (Task 52: a lower-preference answer must
#: never be described as "unavailable").
_MEANINGS = {
    AVAILABILITY_AVAILABLE: "available",
    AVAILABILITY_BACKUP: "available as backup",
    AVAILABILITY_UNAVAILABLE: "unavailable",
}


def _meaning(availability_state: str) -> str:
    return _MEANINGS[availability_state]


def _require_same_ministry(membership: MinistryMembership, event: Event) -> None:
    """The database enforces this too, via the composite foreign keys routed
    through ``availability.ministry_id`` (core §7.2, scheduling-input §8).
    Checking here turns an ``IntegrityError`` at commit -- far from its cause,
    and aborting the caller's whole transaction -- into a meaningful domain
    error raised before anything is mutated.
    """
    if membership.ministry_id != event.ministry_id:
        raise InvalidOperationError(
            "membership and event must belong to the same ministry"
        )


def _require_valid_state(availability_state: str | None) -> None:
    """Only the two explicit states, or ``None`` for "no response" (§8).

    There is no ``UNKNOWN`` / ``NO_RESPONSE`` string, here or in the database;
    any other value is rejected before the lookup even runs, so an invalid
    string can never be mistaken for one of the three real situations.
    """
    if availability_state is not None and availability_state not in _VALID_STATES:
        raise InvalidOperationError(
            f"availability_state must be one of {_VALID_STATES!r} or None,"
            f" got {availability_state!r}"
        )


def _require_period_not_locked(event: Event) -> None:
    """Once a period's availability is locked, ordinary availability
    operations must stop changing the collected input set (module docstring).
    This is a cross-row lifecycle rule -- period, through event, through
    availability -- that no database CHECK can see, so it is enforced here,
    reached through ``Event.scheduling_period``.
    """
    if event.scheduling_period.availability_locked_at is not None:
        raise InvalidOperationError(
            "cannot change availability: this scheduling period's"
            " availability has been locked"
        )


def _require_active_target(membership: MinistryMembership, event: Event) -> None:
    """A new or changed explicit answer needs an active membership, an active
    person, and a live event.

    Not called on the clearing path -- see the asymmetry documented on
    :func:`set_availability`.
    """
    if membership.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot record availability for a deactivated membership"
        )
    if membership.person.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot record availability for a deactivated person"
        )
    if event.cancelled_at is not None:
        raise InvalidOperationError(
            "cannot record availability for a cancelled event"
        )


def _validate_optional_reason(reason: str | None) -> str | None:
    """A reason is optional here, but blank is not a reason.

    [REVIEWED] audit §9: no new universal reason requirement is invented for
    ordinary availability changes; a reason may be supplied and is recorded
    when it is.
    """
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


def _event_availability_statement(
    event_id: int, ministry_id: int, *, include_inactive: bool
) -> Select:
    """Every membership of ``ministry_id``, left-joined to any
    ``Availability`` it has for ``event_id``.

    A ``LEFT OUTER JOIN``, not an inner one: a membership with no response
    for this event is exactly the case this listing exists to show, and an
    inner join would silently drop it. Joined to ``Person`` for the display
    name and that person's own ``deactivated_at`` -- two different activity
    facts (core §6), neither derivable from the other. Mirrors
    :func:`app.services.role_qualification._role_qualifications_statement`
    exactly.
    """
    stmt = (
        select(
            MinistryMembership.id.label("ministry_membership_id"),
            Person.id.label("person_id"),
            Person.display_name.label("person_display_name"),
            MinistryMembership.deactivated_at.label("membership_deactivated_at"),
            Person.deactivated_at.label("person_deactivated_at"),
            Availability.availability_state.label("availability_state"),
        )
        .select_from(MinistryMembership)
        .join(Person, Person.id == MinistryMembership.person_id)
        .outerjoin(
            Availability,
            (Availability.ministry_membership_id == MinistryMembership.id)
            & (Availability.event_id == event_id),
        )
        .where(MinistryMembership.ministry_id == ministry_id)
    )
    if not include_inactive:
        stmt = stmt.where(
            MinistryMembership.deactivated_at.is_(None),
            Person.deactivated_at.is_(None),
        )
    return stmt.order_by(
        func.lower(Person.display_name), MinistryMembership.id
    )


def _availability_lookup_statement(
    ministry_membership_id: int, event_id: int
) -> Select[tuple[Availability]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_availability.py``).
    """
    return select(Availability).where(
        Availability.ministry_membership_id == ministry_membership_id,
        Availability.event_id == event_id,
    )


def _find_existing_availability(
    session: Session, *, ministry_membership_id: int, event_id: int
) -> Availability | None:
    """The standing answer for this (membership, event) pair, if one exists.

    Queried on the model's own integrity columns -- ``ministry_membership_id``
    and ``event_id``, exactly the pair
    ``uq_availability_ministry_membership_id_event_id`` is built from
    (scheduling-input §8) -- via a plain SQLAlchemy ``select()``, no repository
    abstraction. Absence is a real domain state, "no response" (§8), and is
    returned as ``None`` rather than assumed to mean either explicit answer.
    """
    stmt = _availability_lookup_statement(ministry_membership_id, event_id)
    return session.execute(stmt).scalar_one_or_none()
