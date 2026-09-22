"""The same-event support requirement: serving only alongside approved members.

The fifth *structured hard scheduling constraint* (Task 74), and the first one
that is **conditional** rather than prohibitive. Every other rule in this system
says who may *not* be placed; this one says on what condition somebody *may* be.

**What the rule means, stated once.** A scheduling period may configure, for one
of its ministry's memberships -- the **subject** -- a requirement that they hold
an assignment at an event only when at least ``min_supporters`` of a configured
set of other memberships -- the **supporters** -- hold one at the **same event**.

Four consequences follow directly, and all four are enforced everywhere:

- **Subject absent, no requirement.** An event the subject does not serve is
  unconstrained; the rule never obliges anybody to serve.
- **Subject present, support required.** Enough approved supporters must be on
  that event's roster.
- **Any eligible role counts.** A supporter satisfies the requirement by being
  at the event, whatever position they fill.
- **The subject cannot satisfy their own requirement.** Guaranteed by the
  database, through the supporter row's
  ``supporter_membership_id <> subject_membership_id`` CHECK, not merely by a
  check in this module.

**Same event, not the same date**, which is the opposite reading from
:mod:`app.services.same_date_exclusion` and deliberately so. A period may hold a
morning and an evening service on one Sunday; a supporter at the other service
is not on this crew, and counting them would satisfy a rule nobody agreed to.

**Neutral by construction, and nothing personal is stored.** The row says a
membership's placement is conditional on others' and nothing else. There is no
reason field, no category, no relationship type and no free-text note: the
scheduler needs the condition and the approved set, not the circumstance behind
them, and a personal fact stored without a use is a privacy cost with no benefit
(requirements §4.7). Nothing is inferred either -- not from surnames, not from
addresses, not from who has historically served together. Absence of a row *is*
"no rule".

**Directional.** The requirement constrains the subject only. A supporter is
free to serve, or not, at any event, in any role, with or without the subject;
appearing in an approved set changes nothing about their own schedule and
imposes no obligation on them. That asymmetry is why this is not a pair rule.

**Scope is (MinistryMembership x SchedulingPeriod)**, like a serving limit: it
binds one ministry's schedule for one period, expires with the period, and is
never consulted for nor copied into a later one.

**Hard, and deliberately not overridable** (requirements §6). It is applied as a
solver constraint before any objective, as an absolute check in manual
assignment, and as a finalization gate. A head who needs the placement changes
the approved set, lowers the count, or clears the requirement here -- each
audited -- and then assigns. There is no one-time exception, because a rule a
head can wave away for one Sunday is not the hard rule the ministry asked for.

**Removal is still the permissive path.** Taking a *supporter* off an event can
leave a subject unsupported, and :func:`app.services.assignment.remove_assignment`
does not refuse it -- removal is the cleanup path and re-validating it would
leave a head unable to fix a schedule by hand. The resulting version is reported
as unfinalizable by :mod:`app.services.finalization_readiness` until a head
repairs it.

**V1 scope: only this ministry's active Head may configure this** (Task 80).
Being a subject or a supporter confers no authority over the rule, exactly as
for serving limits, same-date exclusions, the event-gap rule and member-group
caps.

Not implemented here, deliberately: a soft "prefer to serve together"
preference, a church-wide version of the rule, a requirement conditioned on a
*date* rather than an event, any carry-forward between periods, and any repair of
assignments a newly configured requirement has invalidated -- that last one is
reported by the finalization gate and fixed by a head, never by this service
deleting somebody's assignment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, Person
from app.models.scheduling_input import (
    MembershipSupportRequirement,
    MembershipSupportSupporter,
    SchedulingPeriod,
)
from app.services.audit import (
    ACTION_SUPPORT_REQUIREMENT_CHANGED,
    ACTION_SUPPORT_REQUIREMENT_CLEARED,
    ACTION_SUPPORT_REQUIREMENT_RECORDED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError

__all__ = [
    "SAME_EVENT_SUPPORT_CONFLICT",
    "PeriodSupportRequirements",
    "SupportRequirementConfig",
    "clear_same_event_support_requirement",
    "count_supporters_present",
    "describe_support_requirement",
    "list_support_requirements",
    "load_support_requirements",
    "set_same_event_support_requirement",
]

#: The one code naming this rule wherever it is reported to a person: the
#: domain error :func:`app.services.assignment.assign_member` raises, and the
#: readiness issue :mod:`app.services.finalization_readiness` emits.
#:
#: **Deliberately not a member of**
#: :data:`app.services.assignment_policy.OVERRIDABLE_BLOCKERS`, for the same
#: reason the other three hard rules' codes are not: that catalogue is the
#: bounded set of checks an ``override_reason`` may bypass, and adding this one
#: would make it bypassable by definition.
#:
#: The solver has its own vocabulary and its own code
#: (``ALL_WITHOUT_EVENT_SUPPORT``), because a solver diagnostic names *why a
#: position is open* while this names *why one placement is refused*.
SAME_EVENT_SUPPORT_CONFLICT = "SAME_EVENT_SUPPORT_CONFLICT"

_TARGET_TABLE = "membership_support_requirement"


@dataclass(frozen=True, slots=True)
class SupportRequirementConfig:
    """One subject's requirement for one period, with its approved supporters.

    The shape every *enforcement* path reads: the solver input builder, manual
    assignment, the batch writer behind generation, and the finalization gate.
    One definition, so none of them can quietly disagree about who counts as
    support or how many are needed.

    It carries membership ids and a count. It does not carry, and cannot be made
    to carry, why the requirement exists.
    """

    support_requirement_id: int
    subject_membership_id: int
    min_supporters: int
    supporter_membership_ids: frozenset[int]

    @property
    def is_satisfiable(self) -> bool:
        """Whether enough supporters are even approved to reach the count.

        ``False`` is a configuration mistake a head can correct -- the subject
        could never be placed -- and every path reports it rather than treating
        it as an error. :func:`set_same_event_support_requirement` refuses to
        *create* one, so this can only arise from a set edited down elsewhere.
        """
        return len(self.supporter_membership_ids) >= self.min_supporters


@dataclass(frozen=True, slots=True)
class PeriodSupportRequirementEntry:
    """One requirement, with display names, for the head managing it."""

    subject_membership_id: int
    subject_display_name: str
    min_supporters: int
    supporter_membership_ids: tuple[int, ...]
    supporter_display_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PeriodSupportRequirements:
    """One scheduling period's whole support-requirement picture."""

    scheduling_period_id: int
    scheduling_period_name: str
    ministry_id: int
    requirements: tuple[PeriodSupportRequirementEntry, ...]


def describe_support_requirement(min_supporters: int) -> str:
    """Plain wording for one configured requirement, accurate for any value.

    Written once and shared by every message a person reads -- the manual
    assignment refusal and the finalization issue -- so the rule cannot be
    described two different ways by two different screens. It says what the rule
    *requires*, and never why the ministry agreed it.
    """
    if min_supporters == 1:
        return (
            "this member may serve an event only if at least one of their"
            " approved supporting members serves the same event"
        )
    return (
        f"this member may serve an event only if at least {min_supporters} of"
        " their approved supporting members serve the same event"
    )


def count_supporters_present(
    requirement: SupportRequirementConfig, membership_ids: Collection[int]
) -> int:
    """How many of ``requirement``'s supporters appear in ``membership_ids``.

    The rule's whole arithmetic, in one place. ``membership_ids`` is who is on
    one **event's** roster, so the count is *approved supporters present at that
    event* -- never on that date, and never in the period at large.
    """
    return len(requirement.supporter_membership_ids.intersection(membership_ids))


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def load_support_requirements(
    session: Session, *, scheduling_period_id: int
) -> tuple[SupportRequirementConfig, ...]:
    """Every support requirement configured for this period.

    :returns: one entry per subject, ordered by subject membership id so two
        reads of unchanged data return the same tuple and the solver model built
        from it is identical run to run. Empty is the ordinary case.

    **Two queries, whatever the number of requirements or supporters**: one for
    the requirement rows, one for the supporter rows of exactly those
    requirements. Never one per subject, and never one per candidate.

    A requirement with **no supporter rows is returned as it is**, not dropped:
    it means the subject can never be placed, which is a real configuration a
    head must be able to see reported rather than one this read quietly hides.

    Read-only and unauthorized on purpose, exactly like
    :func:`app.services.serving_limit.get_serving_limit`: it answers a question
    about scheduling input, and every caller that exposes the answer to a human
    has already established who may see it.
    """
    rows = session.execute(
        _period_requirements_statement(scheduling_period_id)
    ).all()
    if not rows:
        return ()

    requirement_ids = [row.support_requirement_id for row in rows]
    supporters: dict[int, set[int]] = {
        requirement_id: set() for requirement_id in requirement_ids
    }
    for row in session.execute(_supporters_statement(requirement_ids)).all():
        supporters[row.support_requirement_id].add(row.supporter_membership_id)

    return tuple(
        SupportRequirementConfig(
            support_requirement_id=row.support_requirement_id,
            subject_membership_id=row.subject_membership_id,
            min_supporters=row.min_supporters,
            supporter_membership_ids=frozenset(
                supporters[row.support_requirement_id]
            ),
        )
        for row in rows
    )


def list_support_requirements(
    session: Session, *, actor: Person, scheduling_period: SchedulingPeriod
) -> PeriodSupportRequirements:
    """This period's support requirements, named, for someone allowed to manage
    them.

    **Read-only**, and the authorized read behind the management screen --
    matching :func:`app.services.serving_limit.list_serving_limits` in shape, so
    the API layer stays thin and does not decide who may look.

    Names the subject and the approved supporters, because a head configuring
    the rule needs to see who it involves. It says nothing about *why*, because
    nothing here knows.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``scheduling_period.ministry_id``.
    """
    require_ministry_reader(actor, ministry_id=scheduling_period.ministry_id)
    configs = load_support_requirements(
        session, scheduling_period_id=scheduling_period.id
    )
    if not configs:
        return PeriodSupportRequirements(
            scheduling_period_id=scheduling_period.id,
            scheduling_period_name=scheduling_period.name,
            ministry_id=scheduling_period.ministry_id,
            requirements=(),
        )

    # One name lookup for every membership either side of every rule, rather
    # than one per membership: a period holds a handful of rules at most, but
    # "a handful" is not a reason to issue a query per person.
    membership_ids = sorted(
        {config.subject_membership_id for config in configs}
        | {
            supporter_id
            for config in configs
            for supporter_id in config.supporter_membership_ids
        }
    )
    names = _display_names(session, membership_ids=membership_ids)

    return PeriodSupportRequirements(
        scheduling_period_id=scheduling_period.id,
        scheduling_period_name=scheduling_period.name,
        ministry_id=scheduling_period.ministry_id,
        requirements=tuple(
            PeriodSupportRequirementEntry(
                subject_membership_id=config.subject_membership_id,
                subject_display_name=names.get(
                    config.subject_membership_id, ""
                ),
                min_supporters=config.min_supporters,
                supporter_membership_ids=tuple(
                    sorted(config.supporter_membership_ids)
                ),
                supporter_display_names=tuple(
                    names.get(supporter_id, "")
                    for supporter_id in sorted(config.supporter_membership_ids)
                ),
            )
            for config in configs
        ),
    )


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------


def set_same_event_support_requirement(
    session: Session,
    *,
    actor: Person,
    subject_membership: MinistryMembership,
    scheduling_period: SchedulingPeriod,
    supporter_memberships: Sequence[MinistryMembership],
    min_supporters: int = 1,
) -> MembershipSupportRequirement:
    """Record or revise ``subject_membership``'s support requirement.

    **The supporter set is replaced wholesale**, not merged. A head editing the
    rule is stating the approved set as it now stands, and a merge would leave
    somebody approved who was meant to be removed -- silently, and only
    discoverable by reading the audit trail.

    **A request that changes nothing does nothing**, and is not audited: the
    same count with the same supporter set is not a change to record.

    **Both the count and the set are validated together.** A requirement asking
    for more supporters than it approves could never be satisfied, so the
    subject could never be placed; that is a configuration mistake rather than a
    stricter rule, and it is refused here rather than discovered as an
    unexplained empty schedule.

    **Target activity, and the clearing asymmetry.** Recording or revising
    requires the subject, the person behind them, and every supporter to be
    active: people who have left the ministry should not be given new scheduling
    constraints or written into somebody else's.
    :func:`clear_same_event_support_requirement` is deliberately exempt, so a
    departed member's stray rule can always be tidied up.

    **Configuring a requirement the current draft already breaks is allowed**,
    and deliberately so. Nothing here deletes an assignment to make the new rule
    true -- that would destroy a decision a person made. The version simply
    becomes unfinalizable until a head repairs it, which
    :mod:`app.services.finalization_readiness` reports.

    **The availability lock does not apply**, for the same reason it does not
    apply to a serving limit: this is not an availability answer, it is read live
    at generation time, and a head must be able to agree it while a draft is in
    progress.

    The row, its supporter rows and the audit row are added to the same
    ``session`` and are written by the caller's commit (:mod:`app.services`). A
    new requirement is flushed once, immediately after being added, to obtain
    the identity its supporter rows and audit row must reference. This function
    never commits or rolls back.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``scheduling_period.ministry_id``.
    :raises InvalidOperationError: anything is unpersisted; the subject, a
        supporter or the period belongs to a different ministry; the subject
        appears among their own supporters; ``min_supporters`` is not a positive
        integer; or fewer supporters are approved than the count requires.
    """
    require_ministry_operator(actor, ministry_id=scheduling_period.ministry_id)
    _require_persisted(subject_membership, scheduling_period)
    _require_valid_count(min_supporters)

    supporter_ids = _resolve_supporter_ids(
        subject_membership,
        supporter_memberships,
        scheduling_period=scheduling_period,
        min_supporters=min_supporters,
    )

    existing = _find_requirement(
        session,
        subject_membership_id=subject_membership.id,
        scheduling_period_id=scheduling_period.id,
    )
    previous_ids: frozenset[int] = frozenset()
    previous_count: int | None = None
    if existing is not None:
        previous_count = existing.min_supporters
        previous_ids = frozenset(
            row.supporter_membership_id
            for row in session.execute(
                _supporters_statement([existing.id])
            ).all()
        )
        if previous_count == min_supporters and previous_ids == supporter_ids:
            return existing  # idempotent no-op

    _require_active_target(subject_membership, what="the subject of")
    for supporter in supporter_memberships:
        _require_active_target(supporter, what="a supporter in")

    if existing is None:
        requirement = MembershipSupportRequirement(
            subject_membership_id=subject_membership.id,
            scheduling_period_id=scheduling_period.id,
            # Shared by both composite foreign keys on the model; taken from
            # the period, which _resolve_supporter_ids has already proven
            # agrees with the subject and every supporter.
            ministry_id=scheduling_period.ministry_id,
            min_supporters=min_supporters,
        )
        session.add(requirement)
        # The supporter rows below carry this row's identity in their own
        # composite foreign key, and the audit row references it -- the minimum
        # flush that makes both possible.
        session.flush([requirement])
        action = ACTION_SUPPORT_REQUIREMENT_RECORDED
        summary = (
            f"Set {subject_membership.person.display_name} to serve"
            f" {scheduling_period.ministry.name} {scheduling_period.name}"
            f" events only alongside at least {min_supporters} of"
            f" {len(supporter_ids)} approved member(s)"
        )
    else:
        requirement = existing
        requirement.min_supporters = min_supporters
        for row in session.execute(
            _supporter_rows_statement([requirement.id])
        ).scalars():
            # Replaced wholesale: the rows that should remain are re-added
            # below, so deleting the lot keeps "the approved set is exactly
            # this" true without a diff nobody would be able to audit.
            session.delete(row)
        action = ACTION_SUPPORT_REQUIREMENT_CHANGED
        summary = (
            f"Changed {subject_membership.person.display_name}'s same-event"
            f" support requirement for {scheduling_period.ministry.name}"
            f" {scheduling_period.name} to at least {min_supporters} of"
            f" {len(supporter_ids)} approved member(s)"
        )

    for supporter_id in sorted(supporter_ids):
        session.add(
            MembershipSupportSupporter(
                support_requirement_id=requirement.id,
                # Copied from the parent and pinned to it by the composite
                # foreign key; present so the database's own
                # "a supporter is not the subject" CHECK has both ids.
                subject_membership_id=subject_membership.id,
                supporter_membership_id=supporter_id,
                ministry_id=scheduling_period.ministry_id,
            )
        )

    record_audit_event(
        session,
        actor=actor,
        action=action,
        target_table=_TARGET_TABLE,
        target_id=requirement.id,
        ministry_id=scheduling_period.ministry_id,
        summary=summary,
        # Only meaningful business state, never the whole ORM row (audit §7.2),
        # and never why the support is needed (requirements §4.7). The supporter
        # set travels here rather than in its own audit rows: it is changed only
        # as part of configuring this requirement, so one act is one history row.
        before_values=(
            None
            if existing is None
            else _values(
                subject_membership_id=subject_membership.id,
                scheduling_period=scheduling_period,
                min_supporters=previous_count,
                supporter_ids=previous_ids,
            )
        ),
        after_values=_values(
            subject_membership_id=subject_membership.id,
            scheduling_period=scheduling_period,
            min_supporters=min_supporters,
            supporter_ids=supporter_ids,
        ),
    )
    return requirement


def clear_same_event_support_requirement(
    session: Session,
    *,
    actor: Person,
    subject_membership: MinistryMembership,
    scheduling_period: SchedulingPeriod,
) -> None:
    """Remove ``subject_membership``'s support requirement, if there is one.

    **A pure no-op when no rule exists** -- not an error, and not audited: there
    was nothing to remove, so nothing happened.

    **Exempt from the activity checks** :func:`set_same_event_support_requirement`
    applies, deliberately: a rule left behind by somebody who has since left the
    ministry must always be removable -- the same asymmetry
    :func:`app.services.same_date_exclusion.clear_same_date_exclusion` applies.

    Deleting the rows is the whole of "no rule": there is no disabled state and
    no tombstone, so absence keeps one representation rather than two free to
    disagree. Assignments the rule was constraining are left exactly as they
    are; clearing a rule never edits a schedule.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``scheduling_period.ministry_id``.
    :raises InvalidOperationError: the subject or the period is not persisted.
    """
    require_ministry_operator(actor, ministry_id=scheduling_period.ministry_id)
    _require_persisted(subject_membership, scheduling_period)

    existing = _find_requirement(
        session,
        subject_membership_id=subject_membership.id,
        scheduling_period_id=scheduling_period.id,
    )
    if existing is None:
        return  # true no-op: nothing to remove

    # Read before the delete: the audit row must name the rule that existed,
    # and an expired instance cannot be interrogated afterwards.
    target_id = existing.id
    previous_count = existing.min_supporters
    supporter_rows = list(
        session.execute(_supporter_rows_statement([existing.id])).scalars()
    )
    previous_ids = frozenset(row.supporter_membership_id for row in supporter_rows)
    for row in supporter_rows:
        # Deleted before the parent: the composite foreign key is ON DELETE
        # RESTRICT, so the order is the guarantee rather than a convention.
        session.delete(row)
    session.delete(existing)

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_SUPPORT_REQUIREMENT_CLEARED,
        target_table=_TARGET_TABLE,
        target_id=target_id,
        ministry_id=scheduling_period.ministry_id,
        summary=(
            f"Cleared {subject_membership.person.display_name}'s same-event"
            f" support requirement for {scheduling_period.ministry.name}"
            f" {scheduling_period.name}"
        ),
        before_values=_values(
            subject_membership_id=subject_membership.id,
            scheduling_period=scheduling_period,
            min_supporters=previous_count,
            supporter_ids=previous_ids,
        ),
        # No after_values: the rows are gone, and "no rule" is exactly the
        # absence the empty side represents.
    )


# --------------------------------------------------------------------------
# Validation and helpers
# --------------------------------------------------------------------------


def _require_persisted(
    subject_membership: MinistryMembership, scheduling_period: SchedulingPeriod
) -> None:
    if subject_membership.id is None:
        raise InvalidOperationError(
            "subject membership must be persisted (id is None)"
        )
    if scheduling_period.id is None:
        raise InvalidOperationError(
            "scheduling period must be persisted (id is None)"
        )


def _require_valid_count(min_supporters: int) -> None:
    # bool is an int subclass, and True would silently become a count of 1.
    if isinstance(min_supporters, bool) or not isinstance(min_supporters, int):
        raise InvalidOperationError("min_supporters must be an integer")
    if min_supporters <= 0:
        raise InvalidOperationError(
            "min_supporters must be positive; clear the requirement instead of"
            " setting it to zero. Zero would mean 'no condition at all', which"
            " having no rule already says"
        )


def _resolve_supporter_ids(
    subject_membership: MinistryMembership,
    supporter_memberships: Sequence[MinistryMembership],
    *,
    scheduling_period: SchedulingPeriod,
    min_supporters: int,
) -> frozenset[int]:
    """The approved set as ids, with every structural rule applied.

    Duplicates are collapsed rather than rejected: naming somebody twice is a
    caller's sloppiness, not a different rule, and the database's own uniqueness
    would refuse the second row anyway. Everything else is a genuine error.
    """
    ministry_id = scheduling_period.ministry_id
    if subject_membership.ministry_id != ministry_id:
        raise InvalidOperationError(
            "the subject membership and the scheduling period must belong to"
            " the same ministry"
        )

    supporter_ids: set[int] = set()
    for supporter in supporter_memberships:
        if supporter.id is None:
            raise InvalidOperationError(
                "every supporter membership must be persisted (id is None)"
            )
        if supporter.ministry_id != ministry_id:
            raise InvalidOperationError(
                "every supporter membership must belong to the same ministry as"
                " the scheduling period"
            )
        if supporter.id == subject_membership.id:
            raise InvalidOperationError(
                "a membership cannot be its own supporter; the subject of a"
                " support requirement must not appear in their own approved set"
            )
        supporter_ids.add(supporter.id)

    if len(supporter_ids) < min_supporters:
        raise InvalidOperationError(
            f"a requirement for {min_supporters} supporter(s) needs at least"
            f" that many approved members; {len(supporter_ids)} were supplied,"
            " which would mean this member could never be scheduled"
        )
    return frozenset(supporter_ids)


def _require_active_target(membership: MinistryMembership, *, what: str) -> None:
    if membership.deactivated_at is not None:
        raise InvalidOperationError(
            f"cannot make a deactivated membership {what} a same-event support"
            " requirement"
        )
    if membership.person.deactivated_at is not None:
        raise InvalidOperationError(
            f"cannot make a deactivated person {what} a same-event support"
            " requirement"
        )


def _values(
    *,
    subject_membership_id: int,
    scheduling_period: SchedulingPeriod,
    min_supporters: int | None,
    supporter_ids: Collection[int],
) -> dict:
    """The structured context a history reader needs, and nothing more.

    Enough to answer "whose requirement, in which period, how many, and which
    memberships were approved" -- and deliberately not why the support is
    needed, which this system never learns and never stores (requirements §4.7).
    Ids are sorted for a deterministic payload.
    """
    return {
        "subject_membership_id": subject_membership_id,
        "scheduling_period_id": scheduling_period.id,
        "ministry_id": scheduling_period.ministry_id,
        "min_supporters": min_supporters,
        "supporter_membership_ids": sorted(supporter_ids),
    }


# --------------------------------------------------------------------------
# Queries -- each split from its execution so the SQL is testable with no
# database (see tests/test_services_same_event_support.py)
# --------------------------------------------------------------------------


def _period_requirements_statement(scheduling_period_id: int) -> Select:
    """Configured requirements for **this** period.

    Scoped by period alone: the row's composite foreign keys already guarantee
    the period and the subject share a ministry, so this scopes by ministry for
    free -- and a rule belongs to one period and expires with it, so no other
    period's rows can leak in.

    Ordered in SQL as well as in Python so the rules reach the solver in a
    stable order and two runs over the same state build an identical model.
    """
    return (
        select(
            MembershipSupportRequirement.id.label("support_requirement_id"),
            MembershipSupportRequirement.subject_membership_id.label(
                "subject_membership_id"
            ),
            MembershipSupportRequirement.min_supporters.label("min_supporters"),
        )
        .where(
            MembershipSupportRequirement.scheduling_period_id
            == scheduling_period_id
        )
        .order_by(MembershipSupportRequirement.subject_membership_id)
    )


def _supporters_statement(support_requirement_ids: Sequence[int]) -> Select:
    """The approved memberships for each of these requirements, in one query."""
    return (
        select(
            MembershipSupportSupporter.support_requirement_id.label(
                "support_requirement_id"
            ),
            MembershipSupportSupporter.supporter_membership_id.label(
                "supporter_membership_id"
            ),
        )
        .where(
            MembershipSupportSupporter.support_requirement_id.in_(
                support_requirement_ids
            )
        )
        .order_by(
            MembershipSupportSupporter.support_requirement_id,
            MembershipSupportSupporter.supporter_membership_id,
        )
    )


def _supporter_rows_statement(support_requirement_ids: Sequence[int]) -> Select:
    """The supporter rows themselves, for the replace-wholesale delete."""
    return select(MembershipSupportSupporter).where(
        MembershipSupportSupporter.support_requirement_id.in_(
            support_requirement_ids
        )
    )


def _requirement_statement(
    subject_membership_id: int, scheduling_period_id: int
) -> Select:
    return select(MembershipSupportRequirement).where(
        MembershipSupportRequirement.subject_membership_id
        == subject_membership_id,
        MembershipSupportRequirement.scheduling_period_id
        == scheduling_period_id,
    )


def _find_requirement(
    session: Session, *, subject_membership_id: int, scheduling_period_id: int
) -> MembershipSupportRequirement | None:
    return session.execute(
        _requirement_statement(subject_membership_id, scheduling_period_id)
    ).scalar_one_or_none()


def _display_names_statement(membership_ids: Sequence[int]) -> Select:
    return (
        select(
            MinistryMembership.id.label("ministry_membership_id"),
            Person.display_name.label("display_name"),
        )
        .join(Person, Person.id == MinistryMembership.person_id)
        .where(MinistryMembership.id.in_(membership_ids))
    )


def _display_names(
    session: Session, *, membership_ids: Sequence[int]
) -> dict[int, str]:
    if not membership_ids:
        return {}
    return {
        row.ministry_membership_id: row.display_name
        for row in session.execute(_display_names_statement(membership_ids)).all()
    }
