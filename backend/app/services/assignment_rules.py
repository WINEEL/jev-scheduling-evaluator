"""The assignment rules themselves -- evaluated against supplied facts, never
against a database.

Extracted from :mod:`app.services.assignment` (Task 22) when a second writer
appeared: :mod:`app.services.generated_assignment` persists a whole solver
result at once and cannot afford Task 22's eleven queries *per row*, but must
apply Task 22's rules exactly. Two writers, one rule definition -- the same
reason :mod:`app.services.sunday_conflict` keeps its one-person and set-based
forms built from a single statement builder.

**[REVIEWED] Nothing here reads the database.** Not as a style preference: it
is the property that makes two writers safe. Every rule is a function of
values a :class:`PairFactSource` hands over, so the *only* difference between
manual assignment and batch generation is **how** the facts were read -- one
row at a time, or in one set-based prefetch -- never **which** rule is
applied, in what order, or with what message.

That invariant is pinned by a test (``tests/test_services_assignment_rules.py``):
this module imports no Session, no engine, no ``select`` and no statement of
its own.

**Facts are pulled, not pushed, and that is deliberate.** The rules call
:class:`PairFactSource` methods *at the point each rule is evaluated*, so a
placement refused by an early absolute rule never causes the reads a later one
would have needed. Handing the rules a fully-populated struct instead would be
simpler to write and would quietly make every manual rejection cost the
queries it currently short-circuits away.

**What lives here and what does not.** The *checks* and the audit payload they
justify: the absolute rules, the bounded overridable-blocker catalogue
(:mod:`app.services.assignment_policy` still owns the code strings themselves),
the override/no-override symmetry, and the ``ASSIGNMENT_ADDED`` /
``ASSIGNMENT_OVERRIDE_APPLIED`` row. Not here: reading facts, deciding what to
write, flushing, or anything about *why* a caller wants a row -- a proposal
from the solver and a head's deliberate override arrive here identical, and
are judged identically.

Section numbers refer to ``docs/architecture/schedule-output-data-model.md``;
the reasoning behind each rule stays in :mod:`app.services.assignment`'s module
docstring, which is still the document of record for what is overridable and
why.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.core import MinistryMembership, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.services.assignment_policy import (
    BLOCKER_CAPACITY_FULL,
    BLOCKER_DESCRIPTIONS,
    BLOCKER_NOT_QUALIFIED,
    BLOCKER_ROLE_DEACTIVATED,
    BLOCKER_UNAVAILABLE,
)
from app.services.audit import (
    ACTION_ASSIGNMENT_ADDED,
    ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
    record_audit_event,
)
from app.services.errors import InvalidOperationError
from app.services.event_gap import MIN_EVENT_GAP_CONFLICT, describe_event_gap
from app.services.member_group import (
    MEMBER_GROUP_EVENT_LIMIT_CONFLICT,
    describe_member_group_cap,
)
from app.services.same_date_exclusion import SAME_DATE_LINKED_MEMBER_CONFLICT
from app.services.same_event_support import (
    SAME_EVENT_SUPPORT_CONFLICT,
    describe_support_requirement,
)

__all__ = [
    "ASSIGNMENT_TARGET_TABLE",
    "EventGapFacts",
    "MemberGroupEventFacts",
    "OverridableFacts",
    "PairFactSource",
    "SameEventSupportFacts",
    "ServingLimitFacts",
    "assign_after_values",
    "assign_summary",
    "build_assignment",
    "CROSS_MINISTRY_SUNDAY_CONFLICT",
    "collect_overridable_blockers",
    "describe_blockers",
    "record_assignment_created",
    "require_absolute_rules",
    "require_mutable_working_version_status",
    "require_same_event_support",
    "resolve_override",
    "validate_optional_text",
]

ASSIGNMENT_TARGET_TABLE = "assignment"

#: The prefix every cross-ministry same-Sunday refusal carries, so the one
#: hard church-wide rule is greppable in a message the same way the
#: ministry-scoped hard rules are (``MIN_EVENT_GAP_CONFLICT`` and friends).
#:
#: Named for what it is -- a conflict *between ministries* -- rather than for
#: the day of the week: the rule is about one person being claimed twice, and
#: an ad-hoc Wednesday event is claimed exactly as a Sunday is.
CROSS_MINISTRY_SUNDAY_CONFLICT = "cross_ministry_conflict"


@dataclass(frozen=True, slots=True)
class ServingLimitFacts:
    """The member's period maximum, and what they already hold against it.

    Two values together rather than a pre-computed "would exceed" boolean:
    comparing them is a *rule*, and rules live here, not in whichever read
    produced the numbers. ``held`` is meaningless -- and is not required to be
    accurate -- when ``maximum`` is ``None``, which is the ordinary case and
    the one a source may answer without counting anything.
    """

    #: The configured maximum for this scheduling period, or ``None`` when
    #: this member has none (requirements §4.4.1).
    maximum: int | None
    #: How many assignments this membership already holds **in this version**.
    held: int = 0


@dataclass(frozen=True, slots=True)
class EventGapFacts:
    """The period's event-gap rule, and the nearby event that breaks it.

    Two values rather than a pre-computed boolean, for the same reason
    :class:`ServingLimitFacts` carries two: the *rule* -- that a configured gap
    plus a nearby assignment means refuse -- belongs here, and the number is
    needed to say so in words a head can act on.

    Finding *which* event is nearby is a read, not a rule: it needs the
    ministry's event sequence, and only a source with a Session can walk it.
    So the source answers "is there one, and when", and this module decides
    what that means -- exactly the split
    :meth:`PairFactSource.linked_member_assigned_on_date` already uses, with a
    date added so the message can name the clash.

    ``conflicting_event_date`` is meaningless -- and is not required to be
    accurate -- when ``min_intervening_events`` is ``None``, which is the
    ordinary case and the one a source may answer without looking at any
    events at all.
    """

    #: The configured gap for this scheduling period, or ``None`` when the
    #: period has no event-gap rule (requirements §4.8).
    min_intervening_events: int | None
    #: The date of a nearby ministry event this membership already serves,
    #: within the configured gap of the requirement's own event. ``None`` means
    #: there is no such event and the placement is clear.
    conflicting_event_date: datetime.date | None = None


@dataclass(frozen=True, slots=True)
class MemberGroupEventFacts:
    """One capped member group this membership belongs to, and the event's
    current complement from it.

    Three values rather than a pre-computed "would breach" boolean, for the same
    reason :class:`ServingLimitFacts` carries two: comparing them is a *rule*,
    and rules live here, not in whichever read produced the numbers. The name is
    carried because every message a person reads has to say *which* group
    refused the placement -- and a group's name is a neutral label the head
    chose, never a fact about the people in it.

    ``members_present`` counts *people from the group on this event's roster*,
    and the membership being placed is necessarily not among them: the
    one-position-per-event rule is evaluated first and refuses when it is.
    """

    member_group_name: str
    max_per_event: int
    #: How many members of this group already hold an assignment at this event
    #: in this version.
    members_present: int = 0


@dataclass(frozen=True, slots=True)
class SameEventSupportFacts:
    """This membership's own support requirement, and what the event's roster
    currently offers it.

    Two numbers and a count of what is even approved, rather than a
    pre-computed boolean, for the reason every other facts class here carries
    raw values: the comparison is the rule.

    ``min_supporters`` is ``None`` when this membership has no requirement
    configured for the period, which is the ordinary case and the one a source
    may answer without counting anything. The other two fields are then
    meaningless and are not required to be accurate.

    ``approved_supporter_count`` exists only to tell two different failures
    apart in the message: *nobody who could support them is on this crew* and
    *not enough people are approved to support them at all* send a head to two
    different screens.
    """

    #: The configured count, or ``None`` when there is no requirement.
    min_supporters: int | None
    #: Approved supporters already assigned at **this event** in this version.
    supporters_present: int = 0
    #: How many supporters are approved in total.
    approved_supporter_count: int = 0


@dataclass(frozen=True, slots=True)
class OverridableFacts:
    """Current state for the four checks an ``override_reason`` may bypass.

    Gathered as a group because
    :func:`collect_overridable_blockers` is deliberately exhaustive: it never
    short-circuits, so that one rejection can name every violated check at
    once rather than one at a time across repeated calls. A source therefore
    always needs all of them, and answering them together is honest about that.

    **The church-wide same-Sunday conflict used to be a fifth field here.** It
    is now an absolute rule with its own ``PairFactSource`` method
    (:meth:`PairFactSource.has_cross_ministry_sunday_conflict`), read *before*
    this group and outside it -- which is what makes an override incapable of
    reaching it, rather than merely unlikely to.
    """

    #: An approved ``RoleQualification`` for this membership and the
    #: requirement's role. A missing row and an explicit ``False`` are both
    #: ``False`` here -- one scheduling outcome, per
    #: :data:`~app.services.assignment_policy.BLOCKER_NOT_QUALIFIED`.
    is_qualified: bool
    #: An **explicit** ``UNAVAILABLE`` row for this membership and event. A
    #: missing row is "no response" and is not this (scheduling-input §8).
    is_unavailable: bool
    #: How many assignments currently fill this requirement, compared below
    #: against the snapshot ``required_count``, never current
    #: ``staffing_requirement`` (§12).
    current_filled_count: int


class PairFactSource(Protocol):
    """Where the rules get current state for one (requirement, membership) pair.

    Implemented twice, deliberately: :mod:`app.services.assignment` answers
    each question with its own query, and
    :mod:`app.services.generated_assignment` answers all of them for a whole
    solver result out of one prefetch plus what the run itself has written so
    far. Neither implementation contains a rule; both contain only reads.

    Each method is called at most once per placement, at the moment its rule
    is evaluated, so an implementation may do real work in it.
    """

    def fills_other_position_in_event(self) -> bool:
        """Whether this membership already fills some **other** required
        position in this event and version (§10). The exact-pair case is
        idempotency, and is settled by the caller before the rules run.
        """
        ...

    def serving_limit(self) -> ServingLimitFacts:
        """The period maximum and current holding (requirements §4.4.1)."""
        ...

    def linked_member_assigned_on_date(self) -> bool:
        """Whether a membership linked to this one by a same-date exclusion
        already holds an assignment on this requirement's snapshot date, in
        this version (requirements §4.4.2).
        """
        ...

    def event_gap(self) -> EventGapFacts:
        """The period's configured event gap, and any nearby assignment this
        membership already holds within it (requirements §4.8).
        """
        ...

    def member_group_event_limits(self) -> tuple[MemberGroupEventFacts, ...]:
        """Every capped member group this membership belongs to, with the
        event's current complement from each (Task 74).

        Plural because a member may be in several capped groups and all of them
        apply at once -- the hard rules are a conjunction, not a ranking. Empty
        is the ordinary case: no group they are in carries a cap for this
        period, and the rule has nothing to say.
        """
        ...

    def same_event_support(self) -> SameEventSupportFacts:
        """This membership's own same-event support requirement, and how much
        of it this event's roster currently satisfies (Task 74).
        """
        ...

    def has_cross_ministry_sunday_conflict(self) -> bool:
        """Whether this Person is already committed to a **different** ministry
        on the requirement's **snapshot** ``event_date`` (ADR 0002/0003) --
        never the current ``Event.event_date``.

        Its own method rather than a field on :class:`OverridableFacts`,
        because it feeds an **absolute** rule and is therefore asked earlier
        and separately. A source that queries pays for this one read and, when
        it refuses, pays for none of the overridable group.

        Asked through canonical Person identity (``membership.person_id``),
        never a name or an address: the whole point of the rule is that one
        human cannot be in two ministries at once, and two spellings of a name
        are two humans as far as a string comparison is concerned.
        """
        ...

    def overridable_facts(self) -> OverridableFacts:
        """State for all four overridable checks at once."""
        ...


# --------------------------------------------------------------------------
# Absolute rules -- never overridable (assignment module docstring)
# --------------------------------------------------------------------------


def require_mutable_working_version_status(schedule_version: ScheduleVersion) -> None:
    """The half of §7's mutability rule that lives on the row in hand.

    Whether a *newer* version exists is a fact about other rows and is always
    queried by the caller; this function deliberately cannot answer it and
    does not pretend to.
    """
    if schedule_version.status not in (
        SCHEDULE_VERSION_STATUS_DRAFT,
        SCHEDULE_VERSION_STATUS_REVIEW,
    ):
        raise InvalidOperationError(
            "cannot change assignments on a finalized schedule version"
        )


def require_absolute_rules(
    *,
    requirement: ScheduleVersionRequirement,
    membership: MinistryMembership,
    facts: PairFactSource,
) -> None:
    """Every check an ``override_reason`` can never bypass, in Task 22's order.

    Order is part of the contract, not an implementation detail. Two things
    depend on it: a placement violating two rules at once is reported the same
    way by both writers, and each ``facts`` method is reached only once the
    rules before it have passed -- so a source that queries pays for nothing a
    rejection made unnecessary.

    **The same-event support rule is deliberately not here** (Task 74). Every
    rule in this function is a property of *one placement against state that
    already exists*, so applying it row by row gives the same answer whatever
    order the rows arrive in. The support rule is not: it says a placement is
    permitted *because of other placements*, so judging the subject before the
    supporter and judging it after give different answers for the same
    schedule. It is therefore its own exported rule,
    :func:`require_same_event_support`, applied by each writer at the moment its
    state is complete -- per placement for a head's single considered change,
    once over the whole run for generation. Same rule, same message, one
    definition; only *when* it is asked differs, because only then is there
    something true to ask about.

    :raises InvalidOperationError: the first violated rule, with its own
        message.
    """
    if membership.ministry_id != requirement.ministry_id:
        raise InvalidOperationError(
            "membership and requirement must belong to the same ministry"
        )
    if membership.deactivated_at is not None:
        raise InvalidOperationError("cannot assign a deactivated membership")
    if membership.person.deactivated_at is not None:
        raise InvalidOperationError("cannot assign a deactivated person")
    if requirement.event.cancelled_at is not None:
        raise InvalidOperationError("cannot assign to a cancelled event")

    if facts.fills_other_position_in_event():
        raise InvalidOperationError(
            "this membership already fills a different required position"
            " in this event"
        )

    _require_within_serving_limit(facts.serving_limit())
    if facts.linked_member_assigned_on_date():
        raise InvalidOperationError(
            f"{SAME_DATE_LINKED_MEMBER_CONFLICT}: a member linked to"
            f" {membership.person.display_name} by a same-date exclusion is"
            f" already assigned on {requirement.event_date.isoformat()} in"
            " this version. This rule is not overridable: remove that"
            " assignment, or change the linked-member constraint with"
            " set_same_date_exclusion / clear_same_date_exclusion, and try"
            " again."
        )
    _require_event_gap(
        facts.event_gap(), membership=membership, requirement=requirement
    )
    _require_member_group_event_limits(
        facts.member_group_event_limits(),
        membership=membership,
        requirement=requirement,
    )
    _require_no_cross_ministry_sunday_conflict(
        facts.has_cross_ministry_sunday_conflict(),
        membership=membership,
        requirement=requirement,
    )


def _require_no_cross_ministry_sunday_conflict(
    has_conflict: bool,
    *,
    membership: MinistryMembership,
    requirement: ScheduleVersionRequirement,
) -> None:
    """Refuse a placement that would put one Person in two ministries on one day.

    **The church-wide hard rule, and non-overridable** (ADR 0002/0003; Task 79's
    final correction). One Person serves at most one ministry per Sunday. It
    was one of Task 22's five bounded overridable blockers until that review:
    a reason could bypass it, and a rule a reason can talk its way past is not
    a hard rule. It is now here, with the serving maximum and the event gap,
    and for a stronger version of their reason -- those are one ministry's own
    decisions, while this one is the church's, and no single ministry's head
    is in a position to judge it differently on one Sunday. The other ministry
    is not even in the room.

    **Last among the absolute rules, deliberately.** Every rule before it is a
    fact about this ministry and this version; this one is the only rule that
    consults the rest of the church, so it is also the only one whose read a
    ministry-local rejection should not have to pay for. Placing it last keeps
    every existing rejection message and ordering exactly as it was.

    **Not the same-ministry case.** Two roles at one event, or two of this
    ministry's events on one date, are governed by this ministry's own rules --
    ``fills_other_position_in_event``, the event gap, the serving maximum. The
    conflict query excludes the requirement's own ministry for precisely that
    reason (ADR 0002: one *ministry* per Sunday, not one row).

    The remedy is deliberately not an override: remove the other ministry's
    assignment, or record the church-wide commitment differently, and assign
    somebody else here.
    """
    if not has_conflict:
        return
    raise InvalidOperationError(
        f"{CROSS_MINISTRY_SUNDAY_CONFLICT}:"
        f" {membership.person.display_name} is already committed to another"
        f" ministry on {requirement.event_date.isoformat()}, and one person"
        " may serve at most one ministry on the same day. This rule is not"
        " overridable: supplying an override_reason will not change this"
        " answer. Free them in the other ministry first, or assign somebody"
        " else."
    )


def _require_within_serving_limit(limit: ServingLimitFacts) -> None:
    """Refuse an assignment that would exceed this member's period maximum.

    **Non-overridable** (requirements §4.4.1, §6). See
    :mod:`app.services.assignment` for why: the number records what a
    volunteer said they could manage, and is changed with them, not around
    them. Absence of a maximum is the ordinary case and must never behave like
    zero.
    """
    if limit.maximum is None:
        return
    if limit.held + 1 > limit.maximum:
        raise InvalidOperationError(
            f"this assignment would exceed the member's serving maximum for"
            f" this scheduling period ({limit.held} of {limit.maximum} already"
            " assigned). This maximum is not overridable: raise it with"
            " set_serving_limit after agreeing the new number with the"
            " volunteer."
        )


def _require_event_gap(
    facts: EventGapFacts,
    *,
    membership: MinistryMembership,
    requirement: ScheduleVersionRequirement,
) -> None:
    """Refuse a placement that would serve again too soon in this ministry.

    **Non-overridable** (requirements §4.8, §6), for the reason the serving
    maximum is not overridable rather than the reason the bounded overridable blockers
    are: this is a rule a ministry decided for its whole period, not a fact
    about the world that a head may judge differently on one Sunday. A head who
    needs the placement changes the number with ``set_min_intervening_events``
    -- which is audited -- and then assigns.

    No configured rule is the ordinary case and must never behave like a gap of
    zero, which is why the check is on ``min_intervening_events is None`` and
    not on truthiness of the date alone.
    """
    if facts.min_intervening_events is None:
        return
    if facts.conflicting_event_date is None:
        return
    raise InvalidOperationError(
        f"{MIN_EVENT_GAP_CONFLICT}: {membership.person.display_name} is"
        f" already assigned on {facts.conflicting_event_date.isoformat()},"
        f" which is within {facts.min_intervening_events} event(s) of"
        f" {requirement.event_date.isoformat()} in this ministry's event"
        f" sequence, and {describe_event_gap(facts.min_intervening_events)}."
        " This rule is not overridable: remove that assignment, or change the"
        " rule with set_min_intervening_events, and try again."
    )


def _require_member_group_event_limits(
    limits: tuple[MemberGroupEventFacts, ...],
    *,
    membership: MinistryMembership,
    requirement: ScheduleVersionRequirement,
) -> None:
    """Refuse a placement that would put too many of one group on an event.

    **Non-overridable** (Task 74, requirements §6), for the reason the event-gap
    rule is not overridable rather than the reason the bounded overridable blockers
    are: this is a rule a ministry decided for its whole period, not a fact
    about the world a head may judge differently on one Sunday. A head who needs
    the placement raises the number with ``set_member_group_event_limit`` --
    which is audited -- and then assigns.

    **Every capped group is checked, and the first breach refuses.** A member in
    two capped groups must satisfy both; reporting one of them is enough to act
    on, and continuing to collect the rest would cost reads a rejection has
    already made unnecessary.

    Absence of a cap is the ordinary case and is represented by the group simply
    not appearing in ``limits`` -- never by a sentinel number, which is why
    there is no "no cap" branch to get wrong here.
    """
    for limit in limits:
        if limit.members_present + 1 > limit.max_per_event:
            raise InvalidOperationError(
                f"{MEMBER_GROUP_EVENT_LIMIT_CONFLICT}:"
                f" {membership.person.display_name} is in the member group"
                f" {limit.member_group_name}, and"
                f" {limit.members_present} of its members are already assigned"
                f" on {requirement.event_date.isoformat()} in this version,"
                f" where {describe_member_group_cap(limit.member_group_name, limit.max_per_event)}."
                " This rule is not overridable: remove one of those"
                " assignments, or change the limit with"
                " set_member_group_event_limit, and try again."
            )


def require_same_event_support(
    facts: SameEventSupportFacts,
    *,
    subject_display_name: str,
    event_date: datetime.date,
) -> None:
    """Refuse a schedule in which a subject serves an event without their
    support.

    Its own exported rule rather than a step inside
    :func:`require_absolute_rules`, because it is the one rule that is not a
    property of a single placement in isolation -- see that function's docstring
    for why, and for the guarantee that both writers still apply this one
    definition.

    **Non-overridable** (Task 74, requirements §6). A head who needs the
    placement assigns one of the approved supporters to the same event, widens
    the approved set, or clears the requirement -- each audited -- rather than
    working around the rule for one Sunday.

    **No requirement is the ordinary case and must never behave like a
    requirement of zero**, which is why the check is on ``min_supporters is
    None`` and not on truthiness of the count.

    The message distinguishes two failures a head fixes in two different places:
    too few approved supporters happened to be rostered, and too few are
    approved at all. It says nothing whatsoever about *why* the support is
    needed -- the system does not know and must not imply (requirements §4.7).
    """
    if facts.min_supporters is None:
        return
    if facts.supporters_present >= facts.min_supporters:
        return

    if facts.approved_supporter_count < facts.min_supporters:
        remedy = (
            f"only {facts.approved_supporter_count} member(s) are approved to"
            " support them, which is fewer than the requirement asks for;"
            " widen the approved set with"
            " set_same_event_support_requirement, or clear the requirement"
        )
    else:
        remedy = (
            "assign one of their approved supporting members to the same"
            " event first, or change the requirement with"
            " set_same_event_support_requirement"
        )
    raise InvalidOperationError(
        f"{SAME_EVENT_SUPPORT_CONFLICT}: {subject_display_name} may only serve"
        f" alongside approved supporting members, and"
        f" {facts.supporters_present} of the {facts.min_supporters} required"
        f" are assigned on {event_date.isoformat()} in this version"
        f" ({describe_support_requirement(facts.min_supporters)})."
        f" This rule is not overridable: {remedy}, and try again."
    )


# --------------------------------------------------------------------------
# The bounded overridable catalogue
# --------------------------------------------------------------------------


def collect_overridable_blockers(
    *, requirement: ScheduleVersionRequirement, facts: OverridableFacts
) -> frozenset[str]:
    """Every overridable rule currently violated for this pair.

    Computed exhaustively, never short-circuited, so one rejection can name
    all of them at once rather than one at a time across repeated calls.
    """
    blockers: set[str] = set()

    if requirement.ministry_role.deactivated_at is not None:
        blockers.add(BLOCKER_ROLE_DEACTIVATED)
    if not facts.is_qualified:
        blockers.add(BLOCKER_NOT_QUALIFIED)
    if facts.is_unavailable:
        blockers.add(BLOCKER_UNAVAILABLE)
    # The church-wide same-Sunday conflict is deliberately absent: it is an
    # absolute rule (:func:`require_absolute_rules`) and can never appear in
    # this set, so no ``override_reason`` can ever be said to have authorized
    # it.
    if facts.current_filled_count >= requirement.required_count:
        blockers.add(BLOCKER_CAPACITY_FULL)

    return frozenset(blockers)


def describe_blockers(blockers: frozenset[str]) -> str:
    return "; ".join(BLOCKER_DESCRIPTIONS[b] for b in sorted(blockers))


def resolve_override(*, blockers: frozenset[str], override_reason: str | None) -> bool:
    """Reconcile what is violated with what was authorized.

    Both directions are errors, and symmetrically so: a blocked placement with
    no reason is not permitted, and a reason with nothing to override would
    make the row's ``is_override=True`` claim a bypass that never happened.

    :returns: whether the resulting row is an override.
    """
    if blockers and override_reason is None:
        raise InvalidOperationError(
            "cannot assign without an override: " + describe_blockers(blockers)
        )
    if not blockers and override_reason is not None:
        raise InvalidOperationError(
            "override_reason was supplied but no overridable rule was violated"
        )
    return override_reason is not None


def validate_optional_text(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    if not value.strip():
        raise InvalidOperationError(f"{field} must not be blank when supplied")
    return value


# --------------------------------------------------------------------------
# The row, and the audit row that justifies it
# --------------------------------------------------------------------------


def build_assignment(
    *,
    requirement: ScheduleVersionRequirement,
    membership: MinistryMembership,
    is_override: bool,
    override_reason: str | None,
) -> Assignment:
    """The pending row. Not added to any session -- that is the caller's act,
    and the batch writer adds many at once.

    ``schedule_version_id``, ``event_id`` and ``ministry_id`` are set
    explicitly from the requirement: all three are pinned by the model's
    four-column composite foreign key, but no relationship manages them (§10).
    """
    return Assignment(
        schedule_version_requirement_id=requirement.id,
        ministry_membership_id=membership.id,
        schedule_version_id=requirement.schedule_version_id,
        event_id=requirement.event_id,
        ministry_id=requirement.ministry_id,
        is_override=is_override,
        override_reason=override_reason,
    )


def record_assignment_created(
    session,
    *,
    actor: Person,
    assignment: Assignment,
    requirement: ScheduleVersionRequirement,
    membership: MinistryMembership,
    override_reason: str | None,
    blockers: frozenset[str],
) -> None:
    """Write the creation audit row for ``assignment``.

    Takes a ``session`` only to hand it to
    :func:`~app.services.audit.record_audit_event`, which **issues no SQL** --
    it builds a row and adds it. Nothing here queries, flushes or commits, so
    this module still reads nothing: the caller has already flushed
    ``assignment`` far enough to have the identity this row references, and
    both land together at the caller's commit.
    """
    is_override = assignment.is_override
    record_audit_event(
        session,
        actor=actor,
        # One human domain action, one audit row: an override is recorded as
        # ASSIGNMENT_OVERRIDE_APPLIED instead of ASSIGNMENT_ADDED, never both.
        action=(
            ACTION_ASSIGNMENT_OVERRIDE_APPLIED
            if is_override
            else ACTION_ASSIGNMENT_ADDED
        ),
        target_table=ASSIGNMENT_TARGET_TABLE,
        target_id=assignment.id,
        ministry_id=requirement.ministry_id,
        summary=assign_summary(
            membership=membership, requirement=requirement, is_override=is_override
        ),
        # The override reason IS the historical reason for this act; an
        # ordinary assignment has no separate operation-reason concept.
        reason=override_reason if is_override else None,
        after_values=assign_after_values(
            requirement=requirement,
            membership=membership,
            is_override=is_override,
            override_reason=override_reason,
            blockers=blockers,
        ),
    )


def assign_summary(
    *,
    membership: MinistryMembership,
    requirement: ScheduleVersionRequirement,
    is_override: bool,
) -> str:
    """Names the person, the role, the ministry, and the **snapshot** date
    (audit §7.3, §14) -- never the current ``Event.event_date``, since a
    finalized/working version's own meaning must not follow the event row if
    it moves later.
    """
    person_name = membership.person.display_name
    role_name = requirement.ministry_role.name
    ministry_name = requirement.ministry_role.ministry.name
    when = requirement.event_date.isoformat()
    suffix = " with override" if is_override else ""
    return (
        f"Assigned {person_name} to {role_name} for {ministry_name} on"
        f" {when}{suffix}"
    )


def assign_after_values(
    *,
    requirement: ScheduleVersionRequirement,
    membership: MinistryMembership,
    is_override: bool,
    override_reason: str | None,
    blockers: frozenset[str],
) -> dict[str, Any]:
    """Only meaningful business state, never the whole ORM row (audit §7.2).

    For an override, ``overridden_blockers`` additionally names exactly which
    overridable rules were bypassed -- current
    RoleQualification/Availability/capacity/conflict state can change later, so
    this is the only place that fact is preserved. Sorted for a deterministic
    payload, and present only on the override action: a normal
    ``ASSIGNMENT_ADDED`` row has nothing that was overridden, so the key is
    omitted rather than written as an empty/misleading list.
    """
    return {
        "schedule_version_requirement_id": requirement.id,
        "ministry_membership_id": membership.id,
        "schedule_version_id": requirement.schedule_version_id,
        "event_id": requirement.event_id,
        "is_override": is_override,
        "override_reason": override_reason,
        **({"overridden_blockers": sorted(blockers)} if is_override else {}),
    }
