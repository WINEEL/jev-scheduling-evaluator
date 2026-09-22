"""Configuring and clearing a linked-pair same-date exclusion.

The second *person-specific structured scheduling constraint*, and the first
one about a **pair** (requirements §4.4.2). It answers a question neither
availability nor a serving limit can: not "may this person serve on this
date?" and not "how many times this period?", but *"may these two both be on
the roster for the same day?"*

**What the rule means.** If a same-date exclusion exists for two memberships
in a scheduling period, then for every calendar date in that period at most
one of them may hold *any* assignment. Not one per event -- one per **date**:
a period may hold two events on one Sunday, and putting one linked member at
each would break the rule exactly as squarely as putting both in one event.
The rule is symmetric, and the scheduler never needs to know why the two
people are linked.

**Neutral language, and nothing personal stored.** "Linked volunteers",
"same-date exclusion", "paired scheduling constraint". There is no
relationship type, no spouse/household/sibling column and no reason field:
the schedule needs the exclusion, not the circumstance behind it
(requirements §4.4.2, §4.7). Nothing is inferred either -- not from surnames,
not from addresses, not from who has historically served together. Absence of
a row *is* "no rule", and clearing it deletes the row rather than storing a
disabled one.

**Scope is (MinistryMembership x MinistryMembership x SchedulingPeriod).**
Both memberships and the period must belong to one ministry, so a Kids
Ministry head configuring an exclusion cannot reach either person's Setup, AV
or Nursery schedule -- the same boundary
:mod:`app.services.availability` and :mod:`app.services.serving_limit` draw. A
church-wide linked-person rule is a different concept with different
ownership and is deliberately not this service.

**Period-scoped, and it expires with the period.** An exclusion is never
consulted for, nor copied into, a later period, and there is no "copy last
period" behaviour: two people coordinating this quarter have said nothing
about the next one.

**The pair is unordered.** ``set``/``clear`` take the two memberships in
either order and canonicalize before touching the database, so A+B and B+A
are one rule. The database enforces the same thing independently through
``membership_a_id < membership_b_id``, which also makes pairing a membership
with itself unrepresentable.

**Hard, and non-overridable in first-pass V1** (requirements §6). Every
``override_reason`` in this system bypasses a bounded blocker describing a
*fact about the world*; this rule records an arrangement two volunteers made,
so there is no override path and no one-time exception. A head who needs the
pairing removes or changes the constraint here -- which is audited -- and then
makes the assignment.

**V1 scope: only this ministry's active Head may call this** (Task 80). Being
one of the two linked volunteers confers no configuration authority whatsoever;
volunteer self-service is out of scope, exactly as it is for serving limits.

Not implemented here, deliberately: the soft "prefer different dates"
preference and the "prefer same date" rule (both still design --
requirements §4.4.2), informational-only links, church-wide pair rules,
inferred spouse/household relationships, any HTTP endpoint, any frontend, any
copy-forward between periods, and any repair of assignments a newly added
exclusion has put in conflict -- that last one is reported by
:mod:`app.services.finalization_readiness` and fixed by a head, never by this
service deleting somebody's assignment.
"""

from __future__ import annotations

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, Person
from app.models.scheduling_input import (
    MembershipSameDateExclusion,
    SchedulingPeriod,
)
from app.services.audit import (
    ACTION_SAME_DATE_EXCLUSION_CLEARED,
    ACTION_SAME_DATE_EXCLUSION_RECORDED,
    record_audit_event,
)
from app.services.authorization import require_ministry_operator
from app.services.errors import InvalidOperationError

__all__ = [
    "SAME_DATE_LINKED_MEMBER_CONFLICT",
    "canonical_pair",
    "clear_same_date_exclusion",
    "get_linked_membership_ids",
    "get_same_date_exclusion_pairs",
    "set_same_date_exclusion",
]

#: The one code naming this rule wherever it is reported: the domain error
#: :func:`app.services.assignment.assign_member` raises, and the readiness
#: issue :mod:`app.services.finalization_readiness` emits.
#:
#: **Deliberately not a member of**
#: :data:`app.services.assignment_policy.OVERRIDABLE_BLOCKERS`. That catalogue
#: is the bounded set of checks an ``override_reason`` may bypass; this rule is
#: not one of them and adding it there would make it bypassable by definition.
#: The name says "linked member", never "spouse" or "partner" -- the scheduler
#: does not know, and must not record, why the two are linked.
SAME_DATE_LINKED_MEMBER_CONFLICT = "SAME_DATE_LINKED_MEMBER_CONFLICT"

_TARGET_TABLE = "membership_same_date_exclusion"


def canonical_pair(
    membership_a_id: int, membership_b_id: int
) -> tuple[int, int]:
    """The unordered pair as a canonically ordered tuple, lower id first.

    The single place ordering is decided. Every read, write and lookup in this
    module goes through it, so A+B and B+A can never become two rows -- and
    the database's ``membership_a_id < membership_b_id`` CHECK enforces the
    same rule independently, for any writer that does not.

    :raises InvalidOperationError: the two ids are equal. A membership linked
        to itself would mean "this person may never serve", which deactivating
        the membership already says, once, in the place that means it.
    """
    if membership_a_id == membership_b_id:
        raise InvalidOperationError(
            "a membership cannot be linked to itself; a same-date exclusion"
            " is a rule about two different members"
        )
    if membership_a_id < membership_b_id:
        return membership_a_id, membership_b_id
    return membership_b_id, membership_a_id


def set_same_date_exclusion(
    session: Session,
    *,
    actor: Person,
    membership_a: MinistryMembership,
    membership_b: MinistryMembership,
    scheduling_period: SchedulingPeriod,
) -> MembershipSameDateExclusion:
    """Record that these two memberships must not both serve on one date in
    ``scheduling_period``.

    **Order-insensitive and idempotent.** The pair is canonicalized first, so
    passing the memberships the other way round addresses the same rule.
    Setting an exclusion that already exists returns the existing row
    unchanged -- no second row, no flush and **no audit event**: nothing
    changed, and writing a history row for a non-event would make the trail
    claim a head acted when they did not.

    **Both memberships and the period must belong to one ministry**, and the
    actor must manage that ministry. The database guarantees the same thing
    through the row's three composite foreign keys; it is checked here as well
    so the caller gets a domain error naming the problem rather than an
    ``IntegrityError`` at commit time.

    **Target activity is required**, exactly as for a serving limit: someone
    who has left the ministry should not receive new scheduling constraints.
    :func:`clear_same_date_exclusion` is deliberately exempt, so a departed
    member's stray rule can always be tidied up.

    **The SchedulingPeriod availability lock does not apply.** That lock
    freezes *collected availability* so a draft is built against a stable set
    of answers. A pair exclusion is not an availability answer; it is read
    live at generation time like a qualification, and a head must be able to
    record one while a draft is in progress.

    **Nothing is repaired.** If the draft already has both members on a date,
    this call does not delete either assignment -- it records the rule, and
    :mod:`app.services.finalization_readiness` reports the conflict until a
    head fixes the schedule or clears the rule.

    The new row and its audit row are added to the same ``session`` and are
    written by the caller's commit, so they land together or not at all
    (:mod:`app.services`). A new row is flushed once, immediately after being
    added, to obtain the identity its audit row must reference. This function
    never commits or rolls back.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``scheduling_period.ministry_id``.
    :raises InvalidOperationError: the two memberships are the same one; a
        membership or the period belongs to a different ministry; or either
        membership, or the person behind it, is deactivated.
    """
    require_ministry_operator(actor, ministry_id=scheduling_period.ministry_id)
    _require_persisted(membership_a, membership_b, scheduling_period)
    _require_same_ministry(membership_a, membership_b, scheduling_period)
    low_id, high_id = canonical_pair(membership_a.id, membership_b.id)

    existing = _find_existing_exclusion(
        session,
        membership_a_id=low_id,
        membership_b_id=high_id,
        scheduling_period_id=scheduling_period.id,
    )
    if existing is not None:
        return existing  # idempotent no-op

    _require_active_target(membership_a)
    _require_active_target(membership_b)

    exclusion = MembershipSameDateExclusion(
        membership_a_id=low_id,
        membership_b_id=high_id,
        scheduling_period_id=scheduling_period.id,
        # Shared by all three composite foreign keys on the model; taken from
        # the period, which _require_same_ministry has already proven agrees
        # with both memberships.
        ministry_id=scheduling_period.ministry_id,
    )
    session.add(exclusion)
    # The audit row below needs a real target_id, and a new identity bigint
    # does not exist until the INSERT actually runs -- the minimum flush that
    # makes that true, scoped to the one pending row that needs it.
    session.flush([exclusion])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SAME_DATE_EXCLUSION_RECORDED,
        target_table=_TARGET_TABLE,
        target_id=exclusion.id,
        ministry_id=scheduling_period.ministry_id,
        summary=_summary(
            verb="Linked",
            membership_a=membership_a,
            membership_b=membership_b,
            scheduling_period=scheduling_period,
        ),
        # Only meaningful business state, never the whole ORM row (audit
        # §7.2), and never why the two are linked (requirements §4.7).
        after_values=_values(
            membership_a_id=low_id,
            membership_b_id=high_id,
            scheduling_period=scheduling_period,
        ),
    )
    return exclusion


def clear_same_date_exclusion(
    session: Session,
    *,
    actor: Person,
    membership_a: MinistryMembership,
    membership_b: MinistryMembership,
    scheduling_period: SchedulingPeriod,
) -> None:
    """Remove the same-date exclusion for these two memberships, if there is one.

    **Order-insensitive**, through the same canonicalization, and a **pure
    no-op when no rule exists** -- not an error, and not audited: there was
    nothing to remove, so nothing happened.

    **Exempt from the activity checks** ``set`` applies, deliberately. A rule
    left behind by a member who has since left the ministry must always be
    removable -- the same asymmetry
    :func:`app.services.serving_limit.set_serving_limit` and
    :func:`app.services.ministry_authority.revoke_ministry_head` apply.

    Deleting the row is the whole of "no rule": there is no disabled state and
    no tombstone, so absence keeps one representation rather than two free to
    disagree. Assignments the rule was constraining are left exactly as they
    are; clearing a rule never edits a schedule.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``scheduling_period.ministry_id``.
    :raises InvalidOperationError: the two memberships are the same one, or a
        membership or the period belongs to a different ministry.
    """
    require_ministry_operator(actor, ministry_id=scheduling_period.ministry_id)
    _require_persisted(membership_a, membership_b, scheduling_period)
    _require_same_ministry(membership_a, membership_b, scheduling_period)
    low_id, high_id = canonical_pair(membership_a.id, membership_b.id)

    existing = _find_existing_exclusion(
        session,
        membership_a_id=low_id,
        membership_b_id=high_id,
        scheduling_period_id=scheduling_period.id,
    )
    if existing is None:
        return  # true no-op: nothing to remove

    # Read before the delete: the audit row must name the row that existed,
    # and an expired instance cannot be interrogated afterwards.
    target_id = existing.id
    session.delete(existing)

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SAME_DATE_EXCLUSION_CLEARED,
        target_table=_TARGET_TABLE,
        target_id=target_id,
        ministry_id=scheduling_period.ministry_id,
        summary=_summary(
            verb="Unlinked",
            membership_a=membership_a,
            membership_b=membership_b,
            scheduling_period=scheduling_period,
        ),
        before_values=_values(
            membership_a_id=low_id,
            membership_b_id=high_id,
            scheduling_period=scheduling_period,
        ),
        # No after_values: the row is gone, and "no rule" is exactly the
        # absence the empty side represents.
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_same_date_exclusion_pairs(
    session: Session, *, scheduling_period_id: int
) -> tuple[tuple[int, int], ...]:
    """Every configured pair in this period, canonically ordered and sorted.

    Read-only and unauthorized on purpose, like
    :func:`app.services.serving_limit.get_serving_limit`: it answers a question
    about scheduling input, and every caller that exposes the answer to a
    human -- the input builder, finalization readiness -- has already
    established who may see it.

    Scoped by period alone, never by membership: the row's composite foreign
    keys already guarantee the period and both memberships share a ministry,
    so scoping by period scopes by ministry for free.
    """
    rows = session.execute(_period_exclusions_statement(scheduling_period_id))
    return tuple(
        sorted(
            (row.membership_a_id, row.membership_b_id) for row in rows.scalars()
        )
    )


def get_linked_membership_ids(
    session: Session, *, ministry_membership_id: int, scheduling_period_id: int
) -> frozenset[int]:
    """Which memberships this one is linked to in this period.

    Asked from *either* side of the pair -- a membership may be stored as
    either half -- which is why the query is an ``OR`` over both columns
    rather than a lookup on the canonical low id. Read-only and unauthorized,
    for the same reason as above.
    """
    rows = session.execute(
        _member_exclusions_statement(ministry_membership_id, scheduling_period_id)
    )
    linked: set[int] = set()
    for row in rows.scalars():
        # ``other`` rather than an if/else on one column: a row the query
        # could not have returned yields nothing instead of silently reporting
        # its ``membership_a_id`` as a partner.
        if row.membership_a_id == ministry_membership_id:
            linked.add(row.membership_b_id)
        elif row.membership_b_id == ministry_membership_id:
            linked.add(row.membership_a_id)
    return frozenset(linked)


# --------------------------------------------------------------------------
# Validation and helpers
# --------------------------------------------------------------------------


def _require_persisted(
    membership_a: MinistryMembership,
    membership_b: MinistryMembership,
    scheduling_period: SchedulingPeriod,
) -> None:
    """A transient object has no id, and canonicalizing ``None`` against
    ``None`` would silently make two unsaved memberships look like one pair.
    """
    if membership_a.id is None or membership_b.id is None:
        raise InvalidOperationError(
            "both memberships must be persisted (id is None)"
        )
    if scheduling_period.id is None:
        raise InvalidOperationError(
            "scheduling period must be persisted (id is None)"
        )


def _require_same_ministry(
    membership_a: MinistryMembership,
    membership_b: MinistryMembership,
    scheduling_period: SchedulingPeriod,
) -> None:
    """One ministry for both memberships and the period.

    A Kids head may configure a rule for two Kids memberships and nothing
    else: not a pair drawn from two ministries, and not a Kids pair hung off
    another ministry's period. The database enforces this too, through the
    row's three composite foreign keys; it is checked here so the caller gets
    a domain error naming the problem, and so the rule holds even for a caller
    that never reaches a flush.
    """
    ministry_id = scheduling_period.ministry_id
    if (
        membership_a.ministry_id != ministry_id
        or membership_b.ministry_id != ministry_id
    ):
        raise InvalidOperationError(
            "both memberships and the scheduling period must belong to the"
            " same ministry"
        )


def _require_active_target(membership: MinistryMembership) -> None:
    if membership.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot link a deactivated membership"
        )
    if membership.person.deactivated_at is not None:
        raise InvalidOperationError("cannot link a deactivated person")


def _values(
    *,
    membership_a_id: int,
    membership_b_id: int,
    scheduling_period: SchedulingPeriod,
) -> dict:
    """The structured context a history reader needs, and nothing more.

    Enough to answer "which two memberships, in which period, in which
    ministry" -- and deliberately not *why* they are linked, which this system
    never learns and never stores (requirements §4.7).
    """
    return {
        "membership_a_id": membership_a_id,
        "membership_b_id": membership_b_id,
        "scheduling_period_id": scheduling_period.id,
        "ministry_id": scheduling_period.ministry_id,
    }


def _summary(
    *,
    verb: str,
    membership_a: MinistryMembership,
    membership_b: MinistryMembership,
    scheduling_period: SchedulingPeriod,
) -> str:
    """Names both people, the ministry and the period (audit §7.3).

    Says what the rule *does* -- they must not serve on the same date -- and
    never what the two are to each other.
    """
    names = sorted(
        (membership_a.person.display_name, membership_b.person.display_name)
    )
    return (
        f"{verb} {names[0]} and {names[1]} for"
        f" {scheduling_period.ministry.name} {scheduling_period.name}:"
        " they must not both serve on the same date"
    )


# --------------------------------------------------------------------------
# Queries -- each split from its execution so the SQL is testable with no
# database (see tests/test_services_same_date_exclusion.py)
# --------------------------------------------------------------------------


def _exclusion_statement(
    membership_a_id: int, membership_b_id: int, scheduling_period_id: int
) -> Select:
    """The one row for a canonically ordered pair in one period."""
    return select(MembershipSameDateExclusion).where(
        MembershipSameDateExclusion.membership_a_id == membership_a_id,
        MembershipSameDateExclusion.membership_b_id == membership_b_id,
        MembershipSameDateExclusion.scheduling_period_id == scheduling_period_id,
    )


def _find_existing_exclusion(
    session: Session,
    *,
    membership_a_id: int,
    membership_b_id: int,
    scheduling_period_id: int,
) -> MembershipSameDateExclusion | None:
    stmt = _exclusion_statement(
        membership_a_id, membership_b_id, scheduling_period_id
    )
    return session.execute(stmt).scalar_one_or_none()


def _period_exclusions_statement(scheduling_period_id: int) -> Select:
    return (
        select(MembershipSameDateExclusion)
        .where(
            MembershipSameDateExclusion.scheduling_period_id
            == scheduling_period_id
        )
        .order_by(
            MembershipSameDateExclusion.membership_a_id,
            MembershipSameDateExclusion.membership_b_id,
        )
    )


def _member_exclusions_statement(
    ministry_membership_id: int, scheduling_period_id: int
) -> Select:
    """Rules naming this membership on **either** side, in one period."""
    return select(MembershipSameDateExclusion).where(
        MembershipSameDateExclusion.scheduling_period_id == scheduling_period_id,
        or_(
            MembershipSameDateExclusion.membership_a_id == ministry_membership_id,
            MembershipSameDateExclusion.membership_b_id == ministry_membership_id,
        ),
    )
