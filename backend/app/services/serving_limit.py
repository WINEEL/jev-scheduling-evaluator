"""Setting and clearing a member's serving maximum for one scheduling period.

The first *person-specific structured scheduling constraint*
(requirements §4.4.1). It answers a question availability cannot: not "can
this person serve on this date?" but "how many times, at most, during this
period?"

**Scope is (MinistryMembership x SchedulingPeriod).** A limit recorded here
binds one ministry's schedule for one period and nothing else. The same human
may hold four AV Sundays and two Setup Sundays in one quarter, and an AV head
setting a limit can no more affect Setup's schedule than they can edit Setup's
availability. A church-wide total across all ministries is a different concept
with different ownership, and is deliberately not this service.

**Absence means no maximum.** ``max_assignments=None`` clears: a genuine
delete for an existing row, a pure no-op for an absent one. There is no stored
value meaning "unlimited", so "no limit" keeps one representation rather than
two free to disagree -- the same choice :mod:`app.services.availability` makes
about no-response.

**The limit is hard, and this service is the only way to move it**
(requirements §6). Every other scheduling blocker a head may bypass with an
``override_reason`` describes a fact about the world; this one records what a
volunteer said they could manage. So there is no override path: a head who
needs a fifth assignment agrees a new number with the person and raises the
limit here, which is audited. That extra step is the feature. It makes the
volunteer's agreement a precondition rather than something sought afterwards,
and it leaves a record of a limit that changed rather than a rule that was
worked around.

**No reason field, deliberately.** ``set_serving_limit`` takes no reason and
stores none. A serving limit can imply illness, family strain or a
relationship, and the schedule needs the number, not the circumstance
(requirements §4.7). The audit records who changed what and when, which is
what accountability actually requires.

**V1 scope: only this ministry's active Head may call this** (Task 80).
Self-service -- a volunteer setting or editing their own limit -- is explicitly
not part of first-pass V1; this module has no notion of "the acting person is
also the subject." A volunteer *viewing* their own limit is a designed read
path that does not exist yet either.

Not implemented here, deliberately: the soft "prefer about N" preference,
linked-volunteer rules, any HTTP endpoint, any copy-forward between periods,
and any repair of assignments that a lowered limit has put over the line --
that last one is reported by finalization readiness and fixed by a head, never
by this service deleting somebody's assignment.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, Person
from app.models.scheduling_input import MembershipServingLimit, SchedulingPeriod
from app.services.audit import (
    ACTION_SERVING_LIMIT_CHANGED,
    ACTION_SERVING_LIMIT_CLEARED,
    ACTION_SERVING_LIMIT_RECORDED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError

__all__ = [
    "MembershipServingLimitEntry",
    "PeriodServingLimits",
    "get_serving_limit",
    "get_serving_limits_for",
    "list_serving_limits",
    "set_serving_limit",
]

_TARGET_TABLE = "membership_serving_limit"


@dataclass(frozen=True, slots=True)
class MembershipServingLimitEntry:
    """One membership of the period's ministry, and its serving maximum for
    that period, if any.

    ``max_assignments`` is ``None`` for "no maximum" (the module docstring's
    absence rule) -- never a stored sentinel, and never collapsed with a
    number. ``membership_deactivated_at`` and ``person_deactivated_at`` are
    reported separately, exactly as
    :class:`app.services.role_qualification.MembershipQualification` keeps
    them: a person may leave one ministry without leaving the church.
    """

    ministry_membership_id: int
    person_id: int
    person_display_name: str
    membership_deactivated_at: datetime.datetime | None
    person_deactivated_at: datetime.datetime | None
    #: ``None`` = no maximum; otherwise the positive hard maximum.
    max_assignments: int | None


@dataclass(frozen=True, slots=True)
class PeriodServingLimits:
    """One scheduling period's whole serving-limit picture, for the head
    managing it.
    """

    scheduling_period_id: int
    scheduling_period_name: str
    ministry_id: int
    memberships: tuple[MembershipServingLimitEntry, ...]


def list_serving_limits(
    session: Session,
    *,
    actor: Person,
    scheduling_period: SchedulingPeriod,
    include_inactive: bool = False,
) -> PeriodServingLimits:
    """``scheduling_period``'s ministry's memberships, each with its serving
    maximum for that period.

    **Read-only.** Ordered by ``lower(person.display_name)`` then
    ``ministry_membership_id``, matching
    :func:`app.services.role_qualification.list_role_qualifications` and
    :func:`app.services.availability.list_event_availability`, so two reads of
    unchanged data return the same list.

    ``include_inactive=False`` (the default) returns only active memberships
    of currently active people. Passing ``True`` additionally returns
    deactivated memberships and memberships of deactivated people, so a head
    can still see (and, per :func:`set_serving_limit`'s own clearing
    exemption, still clear) a stray limit against someone who has since left.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``scheduling_period.ministry_id``.
    """
    require_ministry_reader(actor, ministry_id=scheduling_period.ministry_id)

    rows = session.execute(
        _period_serving_limits_statement(
            scheduling_period.id,
            scheduling_period.ministry_id,
            include_inactive=include_inactive,
        )
    ).all()
    return PeriodServingLimits(
        scheduling_period_id=scheduling_period.id,
        scheduling_period_name=scheduling_period.name,
        ministry_id=scheduling_period.ministry_id,
        memberships=tuple(
            MembershipServingLimitEntry(
                ministry_membership_id=row.ministry_membership_id,
                person_id=row.person_id,
                person_display_name=row.person_display_name,
                membership_deactivated_at=row.membership_deactivated_at,
                person_deactivated_at=row.person_deactivated_at,
                max_assignments=row.max_assignments,
            )
            for row in rows
        ),
    )


def set_serving_limit(
    session: Session,
    *,
    actor: Person,
    membership: MinistryMembership,
    scheduling_period: SchedulingPeriod,
    max_assignments: int | None,
) -> MembershipServingLimit | None:
    """Record, change, or clear ``membership``'s serving maximum for
    ``scheduling_period``.

    One explicit setter, matching the shape of
    :func:`app.services.availability.set_availability` and
    :func:`app.services.role_qualification.set_role_qualification`: a positive
    integer records or revises the maximum, and ``None`` clears it back to no
    maximum -- a delete for an existing row, a no-op for an absent one.

    **A request that changes nothing does nothing**, and is not audited: an
    absent limit cleared again, or an existing limit set to the value it
    already has, is not a change to record. Writing a history row for a
    non-event would make the audit trail claim a head acted when they did not.

    **Lowering a limit below what the schedule already contains is allowed**,
    and deliberately so. A head may agree a smaller number with a volunteer
    after a draft is built. Nothing here deletes an assignment to make the new
    number true -- that would destroy a decision a person made, possibly one
    the volunteer is counting on. The version simply becomes unfinalizable
    until a head repairs it, which
    :mod:`app.services.finalization_readiness` reports.

    **Target activity, and the removal asymmetry.** Recording or changing a
    maximum requires the membership and the person behind it to be active: a
    person who has left the ministry should not receive new scheduling
    constraints. **Clearing is exempt from both**, so a departed member's stray
    limit can always be tidied up -- the same asymmetry
    :func:`~app.services.availability.set_availability` and
    :func:`~app.services.ministry_authority.revoke_ministry_head` apply.

    **The SchedulingPeriod availability lock does not apply here.** That lock
    freezes *collected availability* so a draft is built against a stable set
    of answers. A serving maximum is not an availability answer, it is read
    live at generation time like a qualification, and a head must be able to
    agree a new number with a volunteer while a draft is in progress -- which
    is precisely the workflow that replaces overriding the rule.

    The mutation and its audit row are added to the same ``session`` and are
    written by the caller's commit, so they land together or not at all
    (:mod:`app.services`). A brand-new row is flushed once, immediately after
    being added, to obtain the identity its audit row must reference. This
    function never commits or rolls back.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``scheduling_period.ministry_id``.
    :raises InvalidOperationError: ``membership`` and ``scheduling_period``
        belong to different ministries; ``max_assignments`` is not ``None`` and
        not a positive integer; or the requested change would record a maximum
        against a deactivated membership or a deactivated person.
    """
    require_ministry_operator(actor, ministry_id=scheduling_period.ministry_id)
    _require_same_ministry(membership, scheduling_period)
    _require_valid_maximum(max_assignments)

    existing = _find_existing_limit(
        session,
        ministry_membership_id=membership.id,
        scheduling_period_id=scheduling_period.id,
    )

    if max_assignments is None:
        if existing is None:
            return None  # true no-op: nothing to remove
        _clear(
            session, actor=actor, membership=membership,
            scheduling_period=scheduling_period, existing=existing,
        )
        return None

    if existing is not None and existing.max_assignments == max_assignments:
        return existing  # idempotent no-op

    _require_active_target(membership)

    if existing is None:
        return _record(
            session, actor=actor, membership=membership,
            scheduling_period=scheduling_period,
            max_assignments=max_assignments,
        )
    return _change(
        session, actor=actor, membership=membership,
        scheduling_period=scheduling_period, existing=existing,
        max_assignments=max_assignments,
    )


def get_serving_limit(
    session: Session,
    *,
    ministry_membership_id: int,
    scheduling_period_id: int,
) -> int | None:
    """The configured maximum, or ``None`` when there is none.

    Read-only and unauthorized on purpose: it answers a question about
    scheduling input, and every caller that exposes the answer to a human --
    the input builder, manual assignment, finalization readiness -- has already
    established who may see it. Adding a check here would duplicate theirs and
    make the solver path depend on an actor it does not have.
    """
    limit = _find_existing_limit(
        session,
        ministry_membership_id=ministry_membership_id,
        scheduling_period_id=scheduling_period_id,
    )
    return None if limit is None else limit.max_assignments


def get_serving_limits_for(
    session: Session,
    *,
    ministry_membership_ids: Sequence[int],
    scheduling_period_id: int,
) -> dict[int, int]:
    """:func:`get_serving_limit` for many memberships at once, in one query.

    Same rule, same rows, built from the very statement builder the
    one-membership form uses, widened from ``=`` to ``IN``. A caller that would
    otherwise ask once per assignment gets identical answers from one round
    trip -- which against a hosted database is the whole cost (Task 63).

    **A missing key means "no maximum configured"**, exactly as ``None`` does
    from the one-membership form. It deliberately does *not* mean zero, and
    entries are not invented for memberships with no row: absence of a limit is
    the ordinary case, and materializing it as a number would be the one
    mistake that turns a volunteer with no stated maximum into one who may
    never serve.

    Read-only and unauthorized, for the same reason
    :func:`get_serving_limit` is.
    """
    if not ministry_membership_ids:
        return {}
    return {
        row.ministry_membership_id: row.max_assignments
        for row in session.execute(
            _limits_query(
                ministry_membership_ids=sorted(set(ministry_membership_ids)),
                scheduling_period_id=scheduling_period_id,
            )
        ).scalars()
    }


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------


def _record(
    session: Session, *, actor: Person, membership: MinistryMembership,
    scheduling_period: SchedulingPeriod, max_assignments: int,
) -> MembershipServingLimit:
    limit = MembershipServingLimit(
        ministry_membership_id=membership.id,
        scheduling_period_id=scheduling_period.id,
        # Shared by both composite foreign keys on the model; taken from the
        # period, which _require_same_ministry has already proven agrees with
        # the membership.
        ministry_id=scheduling_period.ministry_id,
        max_assignments=max_assignments,
    )
    session.add(limit)
    # The audit row below needs a real target_id, and a new identity bigint
    # does not exist until the INSERT actually runs -- the minimum flush that
    # makes that true, scoped to the one pending row that needs it.
    session.flush([limit])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SERVING_LIMIT_RECORDED,
        target_table=_TARGET_TABLE,
        target_id=limit.id,
        ministry_id=scheduling_period.ministry_id,
        summary=_summary(
            membership=membership, scheduling_period=scheduling_period,
            max_assignments=max_assignments,
        ),
        # Only meaningful business state, never the whole ORM row (audit §7.2),
        # and never why the person asked for it (requirements §4.7).
        after_values=_values(
            membership=membership, scheduling_period=scheduling_period,
            max_assignments=max_assignments,
        ),
    )
    return limit


def _change(
    session: Session, *, actor: Person, membership: MinistryMembership,
    scheduling_period: SchedulingPeriod, existing: MembershipServingLimit,
    max_assignments: int,
) -> MembershipServingLimit:
    previous = existing.max_assignments
    existing.max_assignments = max_assignments

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SERVING_LIMIT_CHANGED,
        target_table=_TARGET_TABLE,
        target_id=existing.id,
        ministry_id=scheduling_period.ministry_id,
        summary=(
            f"Changed {membership.person.display_name}'s"
            f" {membership.ministry.name} serving maximum for"
            f" {scheduling_period.name} from {previous} to {max_assignments}"
        ),
        before_values=_values(
            membership=membership, scheduling_period=scheduling_period,
            max_assignments=previous,
        ),
        after_values=_values(
            membership=membership, scheduling_period=scheduling_period,
            max_assignments=max_assignments,
        ),
    )
    return existing


def _clear(
    session: Session, *, actor: Person, membership: MinistryMembership,
    scheduling_period: SchedulingPeriod, existing: MembershipServingLimit,
) -> None:
    previous = existing.max_assignments
    # Read before the delete: the audit row must name the row that existed,
    # and an expired instance cannot be interrogated afterwards.
    target_id = existing.id
    session.delete(existing)

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SERVING_LIMIT_CLEARED,
        target_table=_TARGET_TABLE,
        target_id=target_id,
        ministry_id=scheduling_period.ministry_id,
        summary=(
            f"Cleared {membership.person.display_name}'s"
            f" {membership.ministry.name} serving maximum for"
            f" {scheduling_period.name}"
        ),
        before_values=_values(
            membership=membership, scheduling_period=scheduling_period,
            max_assignments=previous,
        ),
        # No after_values: the row is gone, and "no maximum" is exactly the
        # absence the empty side represents.
    )


# --------------------------------------------------------------------------
# Validation and helpers
# --------------------------------------------------------------------------


def _require_same_ministry(
    membership: MinistryMembership, scheduling_period: SchedulingPeriod
) -> None:
    """A Setup membership may not carry a limit for an AV period.

    The database enforces this too, through the row's composite foreign keys.
    It is checked here as well so the caller gets a domain error naming the
    problem, rather than an ``IntegrityError`` at commit time from a
    constraint whose name means nothing to them -- and so the rule holds even
    for a caller that never reaches a flush.
    """
    if membership.ministry_id != scheduling_period.ministry_id:
        raise InvalidOperationError(
            "membership and scheduling period must belong to the same ministry"
        )


def _require_valid_maximum(max_assignments: int | None) -> None:
    if max_assignments is None:
        return
    # bool is an int subclass, and True would silently become a maximum of 1.
    if isinstance(max_assignments, bool) or not isinstance(max_assignments, int):
        raise InvalidOperationError("max_assignments must be an integer or None")
    if max_assignments <= 0:
        raise InvalidOperationError(
            "max_assignments must be positive; use None to clear the maximum."
            " Zero would mean 'never schedule this person', which deactivating"
            " the membership or answering UNAVAILABLE already says"
        )


def _require_active_target(membership: MinistryMembership) -> None:
    if membership.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot set a serving limit on a deactivated membership"
        )
    if membership.person.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot set a serving limit for a deactivated person"
        )


def _values(
    *,
    membership: MinistryMembership,
    scheduling_period: SchedulingPeriod,
    max_assignments: int,
) -> dict:
    """The structured context a history reader needs, and nothing more.

    Enough to answer "whose limit, in which period, and what was the number",
    without the person's reason for asking (requirements §4.7).
    """
    return {
        "ministry_membership_id": membership.id,
        "scheduling_period_id": scheduling_period.id,
        "max_assignments": max_assignments,
    }


def _summary(
    *,
    membership: MinistryMembership,
    scheduling_period: SchedulingPeriod,
    max_assignments: int,
) -> str:
    return (
        f"Set {membership.person.display_name}'s {membership.ministry.name}"
        f" serving maximum for {scheduling_period.name} to {max_assignments}"
    )


def _period_serving_limits_statement(
    scheduling_period_id: int, ministry_id: int, *, include_inactive: bool
) -> Select:
    """Every membership of ``ministry_id``, left-joined to any
    ``MembershipServingLimit`` it has for ``scheduling_period_id``.

    A ``LEFT OUTER JOIN``, not an inner one: a membership with no recorded
    limit is exactly the case this listing exists to show, and an inner join
    would silently drop it. Joined to ``Person`` for the display name and
    that person's own ``deactivated_at`` -- two different activity facts,
    neither derivable from the other. Mirrors
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
            MembershipServingLimit.max_assignments.label("max_assignments"),
        )
        .select_from(MinistryMembership)
        .join(Person, Person.id == MinistryMembership.person_id)
        .outerjoin(
            MembershipServingLimit,
            (MembershipServingLimit.ministry_membership_id == MinistryMembership.id)
            & (MembershipServingLimit.scheduling_period_id == scheduling_period_id),
        )
        .where(MinistryMembership.ministry_id == ministry_id)
    )
    if not include_inactive:
        stmt = stmt.where(
            MinistryMembership.deactivated_at.is_(None),
            Person.deactivated_at.is_(None),
        )
    return stmt.order_by(func.lower(Person.display_name), MinistryMembership.id)


def _limit_statement(
    ministry_membership_id: int, scheduling_period_id: int
) -> Select:
    """The one-membership form, delegating to the set-based builder with a
    one-element set so the predicate cannot drift between them.
    """
    return _limits_query(
        ministry_membership_ids=(ministry_membership_id,),
        scheduling_period_id=scheduling_period_id,
    )


def _limits_query(
    *, ministry_membership_ids: Sequence[int], scheduling_period_id: int
) -> Select:
    """**This is the single definition.** Widening ``=`` to ``IN`` changes
    nothing about which rows qualify: a row comes back only when its own
    membership is in the set asked about and its period matches, so keying the
    results by ``ministry_membership_id`` reconstructs exactly the per-member
    answers.
    """
    return select(MembershipServingLimit).where(
        MembershipServingLimit.ministry_membership_id.in_(ministry_membership_ids),
        MembershipServingLimit.scheduling_period_id == scheduling_period_id,
    )


def _find_existing_limit(
    session: Session, *, ministry_membership_id: int, scheduling_period_id: int
) -> MembershipServingLimit | None:
    stmt = _limit_statement(ministry_membership_id, scheduling_period_id)
    return session.execute(stmt).scalar_one_or_none()
