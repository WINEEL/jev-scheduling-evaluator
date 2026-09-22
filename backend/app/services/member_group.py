"""Member groups, and the per-event cap a period puts on one.

The fourth *structured hard scheduling constraint*, and the first one about
**an event's composition** (Task 74). It answers a question none of the others
can: not "may this person serve?" (availability), not "how many times this
period?" (:mod:`app.services.serving_limit`), not "may these two share a day?"
(:mod:`app.services.same_date_exclusion`) and not "how soon again?"
(:mod:`app.services.event_gap`), but *"how many of these people may be on one
crew at once?"*

**What the rule means, stated once.** A ministry defines its own **member
groups** -- plain named categories of its own members. A scheduling period may
then configure, for any of those groups, a **maximum number of that group's
members who may serve one event**. When a group has a cap of ``N``, no event in
that period may carry more than ``N`` of its members, counted **once per person
whatever role they serve**: somebody leading counts exactly as much as somebody
not.

**Generic, and deliberately so.** Nothing here knows what a group *is*. A group
is a name a head chose, exactly as a :class:`~app.models.core.MinistryRole` is;
no group name appears in application logic, nothing branches on one, and no
group is created by code. A ministry that wants a category has one, a ministry
that wants none has none, and two ministries' categories have nothing to do
with each other.

**Nothing is inferred.** Group membership is recorded because a head said so --
never derived from an age, a roster column this system does not hold,
historical assignments, or anything else. Absence of a row *is* "not in this
group", and clearing membership deletes the row rather than storing a disabled
one.

**Two scopes, and the split is the point.** The *group* and who is in it are
ministry-scoped standing facts, like a role and its qualifications: a category
does not expire at the end of a quarter, and re-declaring it every period would
be work with no decision in it. The *cap* is period-scoped and expires with the
period, like a serving limit and a pair exclusion: it is a rule a ministry
adopted for one quarter, and a ministry that wants it again adopts it again --
one audited row, rather than a rule that quietly outlives the decision.

**Absence means no cap.** ``max_per_event=None`` clears: a genuine delete for an
existing row, a pure no-op for an absent one. There is no stored value meaning
"unlimited", and the database's ``max_per_event > 0`` CHECK makes zero
unrepresentable -- "no member of this group may serve at all" is what
withholding a qualification or deactivating a membership already says, once, in
the place that means it.

**Hard, and deliberately not overridable** (requirements §6). It is applied as a
solver constraint before any objective, as an absolute check in manual
assignment, and as a finalization gate. A head who needs a third member of the
group on one event raises the number here -- which is audited -- rather than
working around the rule for one Sunday, exactly as they would raise a serving
maximum.

**V1 scope: only this ministry's active Head may configure any of this** (Task
80). Being in a group confers no authority over it, exactly as for serving
limits, same-date exclusions and the event-gap rule.

Not implemented here, deliberately: a soft "prefer fewer" preference, a
church-wide group, per-role caps, a cap on a *date* rather than an event, any
carry-forward between periods, and any repair of assignments that a newly
configured cap has put over the line -- that last one is reported by
:mod:`app.services.finalization_readiness` and fixed by a head, never by this
service deleting somebody's assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Sequence

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import (
    MemberGroup,
    MemberGroupEventLimit,
    MemberGroupMember,
    SchedulingPeriod,
)
from app.services.audit import (
    ACTION_MEMBER_GROUP_CREATED,
    ACTION_MEMBER_GROUP_LIMIT_CHANGED,
    ACTION_MEMBER_GROUP_LIMIT_CLEARED,
    ACTION_MEMBER_GROUP_LIMIT_RECORDED,
    ACTION_MEMBER_GROUP_MEMBER_ADDED,
    ACTION_MEMBER_GROUP_MEMBER_REMOVED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError

__all__ = [
    "MEMBER_GROUP_EVENT_LIMIT_CONFLICT",
    "MemberGroupCapConfig",
    "MemberGroupSummary",
    "PeriodMemberGroupLimits",
    "count_group_members_present",
    "create_member_group",
    "describe_member_group_cap",
    "list_member_groups",
    "list_period_member_group_limits",
    "load_member_group_caps",
    "set_member_group_event_limit",
    "set_member_group_membership",
]

#: The one code naming this rule wherever it is reported to a person: the
#: domain error :func:`app.services.assignment.assign_member` raises, and the
#: readiness issue :mod:`app.services.finalization_readiness` emits.
#:
#: **Deliberately not a member of**
#: :data:`app.services.assignment_policy.OVERRIDABLE_BLOCKERS`, for the same
#: reason ``SAME_DATE_LINKED_MEMBER_CONFLICT`` and ``MIN_EVENT_GAP_CONFLICT``
#: are not: that catalogue is the bounded set of checks an ``override_reason``
#: may bypass, and adding this one would make it bypassable by definition.
#:
#: The solver has its own vocabulary and its own code
#: (``ALL_AT_GROUP_EVENT_LIMIT``), because a solver diagnostic names *why a
#: position is open* while this names *why one placement is refused* -- the same
#: split every other hard rule keeps.
MEMBER_GROUP_EVENT_LIMIT_CONFLICT = "MEMBER_GROUP_EVENT_LIMIT_CONFLICT"

_GROUP_TABLE = "member_group"
_GROUP_MEMBER_TABLE = "member_group_member"
_LIMIT_TABLE = "member_group_event_limit"


@dataclass(frozen=True, slots=True)
class MemberGroupCapConfig:
    """One group's cap for one scheduling period, with the group's members.

    The shape every *enforcement* path reads: the solver input builder, manual
    assignment, the batch writer behind generation, and the finalization gate.
    One definition, so none of them can quietly disagree about who is in a
    group or what its number is.

    Carries the group's **name** as well as its id, because every message a
    person reads has to say *which* rule refused a placement -- and a group's
    name is a neutral label the head chose, never a fact about the people in it.
    The pure solver value (:class:`~app.scheduling.input.MemberGroupCap`) drops
    the name again: the engine counts, and has no use for it.
    """

    member_group_id: int
    member_group_name: str
    max_per_event: int
    member_membership_ids: frozenset[int]

    def contains(self, ministry_membership_id: int) -> bool:
        return ministry_membership_id in self.member_membership_ids


@dataclass(frozen=True, slots=True)
class MemberGroupSummary:
    """One ministry's member group, and who is in it, for the head managing it."""

    member_group_id: int
    ministry_id: int
    name: str
    member_membership_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PeriodMemberGroupLimits:
    """Every one of a ministry's groups, with its cap for one period, if any.

    A group with no cap is *included*, with ``max_per_event=None`` -- that is
    the case the management screen exists to show, and dropping it would hide
    the groups a head might want to cap.
    """

    scheduling_period_id: int
    scheduling_period_name: str
    ministry_id: int
    groups: tuple["PeriodMemberGroupLimit", ...]


@dataclass(frozen=True, slots=True)
class PeriodMemberGroupLimit:
    """One group's entry in :class:`PeriodMemberGroupLimits`."""

    member_group_id: int
    name: str
    member_count: int
    #: ``None`` = no cap for this period; otherwise the positive maximum.
    max_per_event: int | None


def describe_member_group_cap(name: str, max_per_event: int) -> str:
    """Plain wording for one configured cap, accurate for any value.

    Written once and shared by every message a person reads -- the manual
    assignment refusal and the finalization issue -- so the rule cannot be
    described two different ways by two different screens.
    """
    if max_per_event == 1:
        return f"at most one member of {name} may serve any one event"
    return (
        f"at most {max_per_event} members of {name} may serve any one event"
    )


def count_group_members_present(
    cap: MemberGroupCapConfig, membership_ids: Collection[int]
) -> int:
    """How many of ``cap``'s members appear in ``membership_ids``.

    The rule's whole arithmetic, in one place. ``membership_ids`` is who is on
    an event's roster, so the count is *people present from the group* -- which
    is why the caller passes a set of memberships rather than a list of
    assignments: two positions held by one person at one event cannot exist
    (schedule-output §10), and counting rows instead of people would be a
    different rule.
    """
    return len(cap.member_membership_ids.intersection(membership_ids))


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def load_member_group_caps(
    session: Session, *, scheduling_period_id: int
) -> tuple[MemberGroupCapConfig, ...]:
    """Every **capped** group for this period, with its members.

    :returns: one entry per group that has a configured cap, ordered by group
        id so two reads of unchanged data return the same tuple and the solver
        model built from it is identical run to run. Empty is the ordinary case.

    **Only capped groups.** A group with no cap for this period is not a
    scheduling fact at all, and carrying it would carry a category every
    enforcement path would then have to remember to ignore.

    **Two queries, whatever the number of groups or members**: one for the caps
    joined to their groups, one for the membership rows of exactly those
    groups. Never one per group, and never one per candidate.

    Read-only and unauthorized on purpose, exactly like
    :func:`app.services.serving_limit.get_serving_limit` and
    :func:`app.services.event_gap.get_min_intervening_events`: it answers a
    question about scheduling input, and every caller that exposes the answer to
    a human has already established who may see it. A check here would duplicate
    theirs and make the solver path depend on an actor it does not have.
    """
    cap_rows = session.execute(_period_caps_statement(scheduling_period_id)).all()
    if not cap_rows:
        return ()

    group_ids = [row.member_group_id for row in cap_rows]
    members: dict[int, set[int]] = {group_id: set() for group_id in group_ids}
    for row in session.execute(_group_members_statement(group_ids)).all():
        members[row.member_group_id].add(row.ministry_membership_id)

    return tuple(
        MemberGroupCapConfig(
            member_group_id=row.member_group_id,
            member_group_name=row.name,
            max_per_event=row.max_per_event,
            member_membership_ids=frozenset(members[row.member_group_id]),
        )
        for row in cap_rows
    )


def list_member_groups(
    session: Session, *, actor: Person, ministry: Ministry
) -> tuple[MemberGroupSummary, ...]:
    """This ministry's member groups and who is in each, for its head.

    **Read-only.** Ordered by ``lower(name)`` then id, matching every other
    listing in this package, so two reads of unchanged data return the same
    tuple.

    Deactivated memberships are **not** filtered out: a group is a standing
    record of who was put in it, and quietly dropping somebody who left the
    ministry would make the listing disagree with what the enforcement paths
    see. The solver already ignores them -- a deactivated membership is not a
    candidate -- so the honest listing costs nothing.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``ministry``.
    """
    require_ministry_reader(actor, ministry_id=ministry.id)
    group_rows = session.execute(_ministry_groups_statement(ministry.id)).all()
    if not group_rows:
        return ()
    group_ids = [row.member_group_id for row in group_rows]
    members: dict[int, list[int]] = {group_id: [] for group_id in group_ids}
    for row in session.execute(_group_members_statement(group_ids)).all():
        members[row.member_group_id].append(row.ministry_membership_id)
    return tuple(
        MemberGroupSummary(
            member_group_id=row.member_group_id,
            ministry_id=row.ministry_id,
            name=row.name,
            member_membership_ids=tuple(sorted(members[row.member_group_id])),
        )
        for row in group_rows
    )


def list_period_member_group_limits(
    session: Session, *, actor: Person, scheduling_period: SchedulingPeriod
) -> PeriodMemberGroupLimits:
    """Every group of this period's ministry, with its cap for this period.

    **Read-only**, and the authorized read behind the management screen --
    matching :func:`app.services.serving_limit.list_serving_limits` and
    :func:`app.services.event_gap.get_event_gap_rule` in shape, so the API layer
    stays thin and does not decide who may look.

    A group with no cap appears with ``max_per_event=None``: absence is the
    meaningful value here, not a missing one.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``scheduling_period.ministry_id``.
    """
    require_ministry_reader(actor, ministry_id=scheduling_period.ministry_id)
    rows = session.execute(
        _period_group_limits_statement(
            scheduling_period.id, scheduling_period.ministry_id
        )
    ).all()
    return PeriodMemberGroupLimits(
        scheduling_period_id=scheduling_period.id,
        scheduling_period_name=scheduling_period.name,
        ministry_id=scheduling_period.ministry_id,
        groups=tuple(
            PeriodMemberGroupLimit(
                member_group_id=row.member_group_id,
                name=row.name,
                member_count=row.member_count,
                max_per_event=row.max_per_event,
            )
            for row in rows
        ),
    )


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------


def create_member_group(
    session: Session, *, actor: Person, ministry: Ministry, name: str
) -> MemberGroup:
    """Define a member group for ``ministry``.

    **Idempotent by name, case-insensitively.** Creating a group whose name
    already exists returns the existing row unchanged -- no second row, no flush
    and **no audit event**: nothing changed, and writing a history row for a
    non-event would make the trail claim a head acted when they did not. The
    database enforces the same uniqueness independently.

    The new row and its audit row are added to the same ``session`` and are
    written by the caller's commit (:mod:`app.services`). A new row is flushed
    once, immediately after being added, to obtain the identity its audit row
    must reference. This function never commits or rolls back.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``ministry``.
    :raises InvalidOperationError: ``ministry`` is not persisted, or ``name`` is
        blank.
    """
    require_ministry_operator(actor, ministry_id=ministry.id)
    if ministry.id is None:
        raise InvalidOperationError("ministry must be persisted (id is None)")
    name = _require_name(name)

    existing = _find_group_by_name(session, ministry_id=ministry.id, name=name)
    if existing is not None:
        return existing  # idempotent no-op

    group = MemberGroup(ministry_id=ministry.id, name=name)
    session.add(group)
    # The audit row below needs a real target_id, and a new identity bigint
    # does not exist until the INSERT actually runs -- the minimum flush that
    # makes that true, scoped to the one pending row that needs it.
    session.flush([group])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MEMBER_GROUP_CREATED,
        target_table=_GROUP_TABLE,
        target_id=group.id,
        ministry_id=ministry.id,
        summary=f"Created the member group {name} for {ministry.name}",
        # Only meaningful business state, never the whole ORM row (audit §7.2).
        after_values={"member_group_id": group.id, "name": name,
                      "ministry_id": ministry.id},
    )
    return group


def set_member_group_membership(
    session: Session,
    *,
    actor: Person,
    member_group: MemberGroup,
    membership: MinistryMembership,
    is_member: bool,
) -> MemberGroupMember | None:
    """Put ``membership`` into ``member_group``, or take it out.

    One explicit setter, matching
    :func:`app.services.role_qualification.set_role_qualification` in shape:
    ``True`` records membership of the group, ``False`` removes it.

    **A request that changes nothing does nothing**, and is not audited: adding
    somebody already in the group, or removing somebody who is not in it, is not
    a change to record.

    **Target activity, and the removal asymmetry.** Adding somebody requires the
    membership and the person behind it to be active: a person who has left the
    ministry should not be given new scheduling constraints. **Removal is exempt
    from both**, so a departed member's stray group membership can always be
    tidied up -- the same asymmetry
    :func:`app.services.serving_limit.set_serving_limit` and
    :func:`app.services.same_date_exclusion.clear_same_date_exclusion` apply.

    **Nothing is repaired.** If the draft already has too many of the group on
    one event, adding another member to the group does not delete an assignment
    -- :mod:`app.services.finalization_readiness` reports the conflict until a
    head fixes the schedule or changes the rule.

    :returns: the row, or ``None`` when the member is not (or is no longer) in
        the group.
    :raises AuthorizationError: the actor is not an active Ministry Head
        of the group's ministry.
    :raises InvalidOperationError: the group or membership is not persisted, or
        they belong to different ministries.
    """
    require_ministry_operator(actor, ministry_id=member_group.ministry_id)
    _require_persisted_group(member_group)
    if membership.id is None:
        raise InvalidOperationError("membership must be persisted (id is None)")
    if membership.ministry_id != member_group.ministry_id:
        raise InvalidOperationError(
            "membership and member group must belong to the same ministry"
        )

    existing = _find_group_member(
        session,
        member_group_id=member_group.id,
        ministry_membership_id=membership.id,
    )

    if not is_member:
        if existing is None:
            return None  # true no-op: nothing to remove
        target_id = existing.id
        session.delete(existing)
        record_audit_event(
            session,
            actor=actor,
            action=ACTION_MEMBER_GROUP_MEMBER_REMOVED,
            target_table=_GROUP_MEMBER_TABLE,
            target_id=target_id,
            ministry_id=member_group.ministry_id,
            summary=(
                f"Removed {membership.person.display_name} from the member"
                f" group {member_group.name}"
            ),
            before_values=_member_values(member_group, membership),
            # No after_values: the row is gone, and "not in this group" is
            # exactly the absence the empty side represents.
        )
        return None

    if existing is not None:
        return existing  # idempotent no-op

    _require_active_target(membership)

    row = MemberGroupMember(
        member_group_id=member_group.id,
        ministry_membership_id=membership.id,
        # Shared by both composite foreign keys on the model; taken from the
        # group, which the ministry check above has proven agrees with the
        # membership.
        ministry_id=member_group.ministry_id,
    )
    session.add(row)
    session.flush([row])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MEMBER_GROUP_MEMBER_ADDED,
        target_table=_GROUP_MEMBER_TABLE,
        target_id=row.id,
        ministry_id=member_group.ministry_id,
        summary=(
            f"Added {membership.person.display_name} to the member group"
            f" {member_group.name}"
        ),
        after_values=_member_values(member_group, membership),
    )
    return row


def set_member_group_event_limit(
    session: Session,
    *,
    actor: Person,
    member_group: MemberGroup,
    scheduling_period: SchedulingPeriod,
    max_per_event: int | None,
) -> MemberGroupEventLimit | None:
    """Record, change, or clear ``member_group``'s per-event cap for a period.

    One explicit setter, matching
    :func:`app.services.serving_limit.set_serving_limit` and
    :func:`app.services.event_gap.set_min_intervening_events`: a positive
    integer records or revises the cap, and ``None`` clears it back to no cap --
    a delete for an existing row, a no-op for an absent one.

    **A request that changes nothing does nothing**, and is not audited.

    **Lowering a cap below what the schedule already contains is allowed**, and
    deliberately so. A head may agree a stricter rule after a draft is built.
    Nothing here deletes an assignment to make the new number true -- that would
    destroy a decision a person made. The version simply becomes unfinalizable
    until a head repairs it, which
    :mod:`app.services.finalization_readiness` reports.

    **The availability lock does not apply.** That lock freezes *collected
    availability* so a draft is built against a stable set of answers. A group
    cap is not an availability answer; it is read live at generation time like a
    qualification, and a head must be able to change it while a draft is in
    progress -- which is precisely the workflow that replaces overriding it.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``scheduling_period.ministry_id``.
    :raises InvalidOperationError: the group or period is not persisted, they
        belong to different ministries, or ``max_per_event`` is not ``None`` and
        not a positive integer.
    """
    require_ministry_operator(actor, ministry_id=scheduling_period.ministry_id)
    _require_persisted_group(member_group)
    if scheduling_period.id is None:
        raise InvalidOperationError(
            "scheduling period must be persisted (id is None)"
        )
    if member_group.ministry_id != scheduling_period.ministry_id:
        raise InvalidOperationError(
            "member group and scheduling period must belong to the same ministry"
        )
    _require_valid_maximum(max_per_event)

    existing = _find_event_limit(
        session,
        member_group_id=member_group.id,
        scheduling_period_id=scheduling_period.id,
    )

    if max_per_event is None:
        if existing is None:
            return None  # true no-op: nothing to remove
        previous = existing.max_per_event
        target_id = existing.id
        session.delete(existing)
        record_audit_event(
            session,
            actor=actor,
            action=ACTION_MEMBER_GROUP_LIMIT_CLEARED,
            target_table=_LIMIT_TABLE,
            target_id=target_id,
            ministry_id=scheduling_period.ministry_id,
            summary=(
                f"Cleared the per-event limit on the member group"
                f" {member_group.name} for {scheduling_period.name}"
            ),
            before_values=_limit_values(
                member_group, scheduling_period, previous
            ),
        )
        return None

    if existing is not None and existing.max_per_event == max_per_event:
        return existing  # idempotent no-op

    if existing is not None:
        previous = existing.max_per_event
        existing.max_per_event = max_per_event
        record_audit_event(
            session,
            actor=actor,
            action=ACTION_MEMBER_GROUP_LIMIT_CHANGED,
            target_table=_LIMIT_TABLE,
            target_id=existing.id,
            ministry_id=scheduling_period.ministry_id,
            summary=(
                f"Changed the per-event limit on the member group"
                f" {member_group.name} for {scheduling_period.name} from"
                f" {previous} to {max_per_event}"
            ),
            before_values=_limit_values(
                member_group, scheduling_period, previous
            ),
            after_values=_limit_values(
                member_group, scheduling_period, max_per_event
            ),
        )
        return existing

    limit = MemberGroupEventLimit(
        member_group_id=member_group.id,
        scheduling_period_id=scheduling_period.id,
        # Shared by both composite foreign keys on the model; taken from the
        # period, which the ministry check above has proven agrees with the
        # group.
        ministry_id=scheduling_period.ministry_id,
        max_per_event=max_per_event,
    )
    session.add(limit)
    session.flush([limit])

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_MEMBER_GROUP_LIMIT_RECORDED,
        target_table=_LIMIT_TABLE,
        target_id=limit.id,
        ministry_id=scheduling_period.ministry_id,
        summary=(
            f"Set {scheduling_period.name} to allow at most {max_per_event}"
            f" member(s) of {member_group.name} per event"
        ),
        after_values=_limit_values(
            member_group, scheduling_period, max_per_event
        ),
    )
    return limit


# --------------------------------------------------------------------------
# Validation and helpers
# --------------------------------------------------------------------------


def _require_name(name: str) -> str:
    if name is None or not name.strip():
        raise InvalidOperationError("member group name must not be blank")
    return name.strip()


def _require_persisted_group(member_group: MemberGroup) -> None:
    if member_group.id is None:
        raise InvalidOperationError("member group must be persisted (id is None)")


def _require_valid_maximum(max_per_event: int | None) -> None:
    if max_per_event is None:
        return
    # bool is an int subclass, and True would silently become a cap of 1.
    if isinstance(max_per_event, bool) or not isinstance(max_per_event, int):
        raise InvalidOperationError("max_per_event must be an integer or None")
    if max_per_event <= 0:
        raise InvalidOperationError(
            "max_per_event must be positive; use None to clear the limit."
            " Zero would mean 'no member of this group may serve at all',"
            " which withholding a qualification or deactivating a membership"
            " already says"
        )


def _require_active_target(membership: MinistryMembership) -> None:
    if membership.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot add a deactivated membership to a member group"
        )
    if membership.person.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot add a deactivated person to a member group"
        )


def _member_values(
    member_group: MemberGroup, membership: MinistryMembership
) -> dict:
    """The structured context a history reader needs, and nothing more.

    Which membership, in which group, in which ministry -- and deliberately not
    what the category means or why this person is in it (requirements §4.7).
    """
    return {
        "member_group_id": member_group.id,
        "ministry_membership_id": membership.id,
        "ministry_id": member_group.ministry_id,
    }


def _limit_values(
    member_group: MemberGroup,
    scheduling_period: SchedulingPeriod,
    max_per_event: int,
) -> dict:
    return {
        "member_group_id": member_group.id,
        "scheduling_period_id": scheduling_period.id,
        "ministry_id": scheduling_period.ministry_id,
        "max_per_event": max_per_event,
    }


# --------------------------------------------------------------------------
# Queries -- each split from its execution so the SQL is testable with no
# database (see tests/test_services_member_group.py)
# --------------------------------------------------------------------------


def _period_caps_statement(scheduling_period_id: int) -> Select:
    """Configured caps for **this** period, joined to their groups for the name.

    Scoped by period alone: the row's composite foreign keys already guarantee
    the period and the group share a ministry, so this scopes by ministry for
    free -- and a cap belongs to one period and expires with it, so no other
    period's rows can leak in.

    Ordered in SQL as well as in Python so the caps reach the solver in a stable
    order and two runs over the same state build an identical model.
    """
    return (
        select(
            MemberGroupEventLimit.member_group_id.label("member_group_id"),
            MemberGroupEventLimit.max_per_event.label("max_per_event"),
            MemberGroup.name.label("name"),
        )
        .join(MemberGroup, MemberGroup.id == MemberGroupEventLimit.member_group_id)
        .where(
            MemberGroupEventLimit.scheduling_period_id == scheduling_period_id
        )
        .order_by(MemberGroupEventLimit.member_group_id)
    )


def _group_members_statement(member_group_ids: Sequence[int]) -> Select:
    """Every membership in each of these groups, in one query.

    ``IN`` over the group ids rather than one query per group: a ministry has a
    handful of groups and a few dozen members, and a round trip per group is
    exactly the N+1 this project keeps out of the scheduling path.
    """
    return (
        select(
            MemberGroupMember.member_group_id.label("member_group_id"),
            MemberGroupMember.ministry_membership_id.label(
                "ministry_membership_id"
            ),
        )
        .where(MemberGroupMember.member_group_id.in_(member_group_ids))
        .order_by(
            MemberGroupMember.member_group_id,
            MemberGroupMember.ministry_membership_id,
        )
    )


def _ministry_groups_statement(ministry_id: int) -> Select:
    return (
        select(
            MemberGroup.id.label("member_group_id"),
            MemberGroup.ministry_id.label("ministry_id"),
            MemberGroup.name.label("name"),
        )
        .where(MemberGroup.ministry_id == ministry_id)
        .order_by(func.lower(MemberGroup.name), MemberGroup.id)
    )


def _period_group_limits_statement(
    scheduling_period_id: int, ministry_id: int
) -> Select:
    """Every group of this ministry, left-joined to its cap for this period.

    A ``LEFT OUTER JOIN``, not an inner one: a group with no configured cap is
    exactly the case this listing exists to show, and an inner join would
    silently drop it -- the same shape
    :func:`app.services.serving_limit._period_serving_limits_statement` uses.

    The member count is a correlated scalar subquery rather than a second
    ``GROUP BY`` join, so one row comes back per group whether or not it has a
    cap and whether or not it has members.
    """
    member_count = (
        select(func.count(MemberGroupMember.id))
        .where(MemberGroupMember.member_group_id == MemberGroup.id)
        .correlate(MemberGroup)
        .scalar_subquery()
    )
    return (
        select(
            MemberGroup.id.label("member_group_id"),
            MemberGroup.name.label("name"),
            member_count.label("member_count"),
            MemberGroupEventLimit.max_per_event.label("max_per_event"),
        )
        .select_from(MemberGroup)
        .outerjoin(
            MemberGroupEventLimit,
            (MemberGroupEventLimit.member_group_id == MemberGroup.id)
            & (
                MemberGroupEventLimit.scheduling_period_id
                == scheduling_period_id
            ),
        )
        .where(MemberGroup.ministry_id == ministry_id)
        .order_by(func.lower(MemberGroup.name), MemberGroup.id)
    )


def _group_by_name_statement(ministry_id: int, name: str) -> Select:
    """Case-insensitive, matching the unique index that enforces the same rule."""
    return select(MemberGroup).where(
        MemberGroup.ministry_id == ministry_id,
        func.lower(MemberGroup.name) == name.lower(),
    )


def _find_group_by_name(
    session: Session, *, ministry_id: int, name: str
) -> MemberGroup | None:
    return session.execute(
        _group_by_name_statement(ministry_id, name)
    ).scalar_one_or_none()


def _group_member_statement(
    member_group_id: int, ministry_membership_id: int
) -> Select:
    return select(MemberGroupMember).where(
        MemberGroupMember.member_group_id == member_group_id,
        MemberGroupMember.ministry_membership_id == ministry_membership_id,
    )


def _find_group_member(
    session: Session, *, member_group_id: int, ministry_membership_id: int
) -> MemberGroupMember | None:
    return session.execute(
        _group_member_statement(member_group_id, ministry_membership_id)
    ).scalar_one_or_none()


def _event_limit_statement(
    member_group_id: int, scheduling_period_id: int
) -> Select:
    return select(MemberGroupEventLimit).where(
        MemberGroupEventLimit.member_group_id == member_group_id,
        MemberGroupEventLimit.scheduling_period_id == scheduling_period_id,
    )


def _find_event_limit(
    session: Session, *, member_group_id: int, scheduling_period_id: int
) -> MemberGroupEventLimit | None:
    return session.execute(
        _event_limit_statement(member_group_id, scheduling_period_id)
    ).scalar_one_or_none()
