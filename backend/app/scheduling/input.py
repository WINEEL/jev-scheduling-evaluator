"""The solver's input: plain, immutable, database-free values.

One :class:`SchedulingInput` describes everything a scheduling run for one
working ScheduleVersion needs to know -- which positions must be filled, who
could fill them, and what is already decided. It is deliberately **not** a view
onto the ORM:

- nothing here imports SQLAlchemy, a ``Session``, or a model class;
- nothing here queries anything, lazily or otherwise;
- every collection is a tuple or frozenset, every mapping is read-only.

That is what lets a solver be written and tested against fixtures with no
database at all, and it is what stops a subtle lazy-load from turning a
"pure" optimization run into a source of surprise queries.

**The values are the version's own snapshot, not current input.** Dates and
counts come from ``schedule_version_requirement`` -- the immutable record of
what this version was built against (schedule-output §8) -- so a solver run is
reproducible for as long as the version exists, whatever the live
``staffing_requirement`` and ``event`` rows say later. Whether current input
has drifted is a separate question, answered by Task 23's staleness comparison
before this object is ever built.

**Three availability states, not two.** ``AVAILABLE`` and ``UNAVAILABLE`` are
stored; **absence of a row is ``NO_RESPONSE``**, which exists only here and is
never written back (scheduling-input §8). Collapsing it into either answer at
this layer would destroy the one distinction the import path was carefully
built to preserve -- how a ministry *treats* no-response is a per-ministry
policy decision, and it belongs to the policy layer that consumes this input,
not to the code that reports the facts.

**Structured person-specific constraints travel as values too.** A
candidate's serving maximum is a field on :class:`CandidateInput`, and a
linked-volunteer same-date exclusion is a :class:`LinkedMembershipPair` in
``same_date_exclusions``. Both are *hard* rules the solver applies as
constraints, and both carry the number or the pair and nothing else -- never
why a volunteer asked for a limit, and never why two people are linked
(requirements §4.7).

**The two group-shaped rules travel as values too** (Task 74). A
:class:`MemberGroupCap` carries an opaque group id, a number and the membership
ids in the group; a :class:`SameEventSupportRequirement` carries a subject, a
count and the approved supporter ids. Neither carries a group's name, a reason,
a category or a relationship -- the solver counts people and conditions one
person's placement on others', and nothing in the values it receives could tell
it why.

**The ministry's event-gap rule travels the same way** (Task 71):
``min_intervening_events`` is the configured number, and
:class:`AdjacentEventInput` carries just enough of the ministry's own events on
*either side* of this run's window for the rule to hold across both boundaries
of a period. Both are plain values like everything else here -- the solver
never queries for the event before or after this period, and could not.

Not represented here, deliberately: AuditEvent history (Task 26 owns
finalization-time override validation), the conflict rows themselves (a blocked
date is enough), and anything about what the solver may *do* with an existing
assignment -- whether it may move or remove one is Task 31's question, so this
model only states that the assignment exists.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "AdjacentEventInput",
    "AvailabilityState",
    "CandidateInput",
    "ExistingAssignmentInput",
    "LinkedMembershipPair",
    "MemberGroupCap",
    "RequirementInput",
    "SameEventSupportRequirement",
    "SchedulingInput",
]


class AvailabilityState(Enum):
    """A member's answer for one event -- including not having answered.

    ``NO_RESPONSE`` is a solver-input value with no database representation
    (scheduling-input §8, decision 15): the table stores explicit answers, and
    a missing row means the person has not said. It is kept distinct here so a
    later per-ministry policy can decide what it means; Setup treats it as
    available today, another ministry may reasonably not, and neither reading
    can be recovered once the distinction is thrown away.

    ``BACKUP`` (Task 52) is a stored, explicit answer like ``AVAILABLE`` --
    fully feasible, never a hard blocker -- but a lower scheduling preference:
    the solver fills a position with a ``BACKUP`` candidate only when no
    ``AVAILABLE`` candidate can take it instead. It is not a weaker form of
    ``NO_RESPONSE``; the person did answer, and said "if you need me."

    This enum is meant to be handled exhaustively wherever it is branched on
    (:func:`app.scheduling.solver._availability_allows` in particular) --
    adding a member here is a real behavior change, not a detail a switch may
    silently fall through.
    """

    AVAILABLE = "AVAILABLE"
    BACKUP = "BACKUP"
    UNAVAILABLE = "UNAVAILABLE"
    NO_RESPONSE = "NO_RESPONSE"


@dataclass(frozen=True, slots=True)
class RequirementInput:
    """One required position in this version's snapshot.

    ``event_date`` is the **snapshot** date, never the current ``event`` row's:
    this version committed to that Sunday, and a solver run must schedule for
    the same one the version means.

    ``role_is_active`` is current state rather than snapshot, and is the one
    place the two are mixed on purpose. A role deactivated after the version
    was created does not change what the version requires -- the position is
    still in the snapshot and still unfilled -- but ordinary candidates must
    not be scheduled into it (Task 22 treats a deactivated role as a blocker).
    Task 23's staleness fingerprint does not include role activity, so nothing
    upstream would otherwise notice; stating it here keeps the solver from
    having to ask.
    """

    requirement_id: int
    event_id: int
    event_date: datetime.date
    ministry_role_id: int
    ministry_id: int
    required_count: int
    role_is_active: bool = True

    @property
    def sort_key(self) -> tuple:
        """Deterministic ordering: date, then event, then role, then id."""
        return (self.event_date, self.event_id, self.ministry_role_id, self.requirement_id)


@dataclass(frozen=True, slots=True)
class CandidateInput:
    """One person who could serve, and the current facts about them.

    Everything is stated per candidate so the solver never needs a query:
    which roles they currently hold an approved qualification for, what they
    answered for each event in this version, and which dates another ministry
    has already claimed them for.
    """

    membership_id: int
    person_id: int
    display_name: str
    #: Roles this membership is *currently* approved for. A missing
    #: RoleQualification row and an explicit ``is_qualified=False`` are the
    #: same outcome here -- both mean "not eligible" -- exactly as Task 22
    #: treats them (core §8 keeps the distinction in the data; scheduling does
    #: not act on it).
    qualified_role_ids: frozenset[int] = frozenset()
    #: Only events with a stored answer appear. Read it through
    #: :meth:`availability_for`, which supplies ``NO_RESPONSE`` for the rest.
    availability_by_event: Mapping[int, AvailabilityState] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: Dates on which another ministry's authoritative schedule already claims
    #: this person (ADR 0002/0003), reduced to the dates themselves -- the
    #: conflicting rows are not the solver's business.
    blocked_dates: frozenset[datetime.date] = frozenset()
    #: The most assignments this candidate may hold in **this** period, across
    #: this run's whole schedule -- existing rows included. ``None`` means no
    #: maximum, which is the ordinary case; absence of a configured limit is
    #: never a limit of zero.
    #:
    #: A **hard** rule, not a preference (requirements §4.4.1). It is applied
    #: as a constraint before any optimisation pass, so no target, load
    #: balancing or role-variety preference can push a candidate past it, and
    #: a position nobody may fill without exceeding it comes back unfilled.
    #:
    #: Scoped per ministry and per period by construction: this input describes
    #: one ministry's one scheduling period, so a cap here says nothing about
    #: what the same person may do elsewhere in the church.
    max_assignments_in_period: int | None = None

    def availability_for(self, event_id: int) -> AvailabilityState:
        """The stored answer, or ``NO_RESPONSE`` when there is none."""
        return self.availability_by_event.get(event_id, AvailabilityState.NO_RESPONSE)

    def is_qualified_for(self, ministry_role_id: int) -> bool:
        return ministry_role_id in self.qualified_role_ids

    def is_blocked_on(self, event_date: datetime.date) -> bool:
        return event_date in self.blocked_dates

    def remaining_capacity(self, existing_count: int) -> int | None:
        """How many *more* assignments this candidate may take, or ``None``.

        ``None`` means uncapped. A candidate already at or past their maximum
        yields ``0`` rather than a negative number: existing assignments are
        fixed inputs the solver never removes, so "how many more" is the only
        question it can act on. An over-limit version is a real state -- a head
        may have lowered the maximum after assigning -- and it is reported at
        finalization rather than repaired by deleting somebody's assignment.
        """
        if self.max_assignments_in_period is None:
            return None
        return max(self.max_assignments_in_period - existing_count, 0)



@dataclass(frozen=True, slots=True)
class LinkedMembershipPair:
    """Two candidates who must not both serve on any one calendar date.

    The pure form of the ministry- and period-scoped same-date exclusion a
    head configures (requirements §4.4.2). It carries **two membership ids and
    nothing else**: no relationship type, no reason, no household, no
    strength. The solver does not need to know why two people are linked, and
    the values that reach it must not be able to tell it.

    **Unordered, canonically.** The ids are sorted on construction, so the
    same pair given either way round is the same value -- equal, and equally
    hashable -- and a builder cannot accidentally emit A+B and B+A as two
    rules. That mirrors the database's own ``membership_a_id <
    membership_b_id`` guarantee rather than adding a second, looser one.

    **Scoped by construction.** A :class:`SchedulingInput` describes one
    ministry's one scheduling period, so a pair here says nothing about what
    the same two people may do in another ministry or a later quarter -- the
    same way ``max_assignments_in_period`` is period-scoped simply by being
    part of this object.
    """

    membership_a_id: int
    membership_b_id: int

    def __post_init__(self) -> None:
        if self.membership_a_id == self.membership_b_id:
            raise ValueError(
                "a linked pair must name two different memberships;"
                f" got {self.membership_a_id} twice"
            )
        if self.membership_a_id > self.membership_b_id:
            # Canonical order, enforced here rather than trusted from the
            # caller: two spellings of one rule would defeat the equality and
            # set arithmetic every consumer below relies on.
            low, high = self.membership_b_id, self.membership_a_id
            object.__setattr__(self, "membership_a_id", low)
            object.__setattr__(self, "membership_b_id", high)

    @property
    def membership_ids(self) -> tuple[int, int]:
        return (self.membership_a_id, self.membership_b_id)

    def other(self, membership_id: int) -> int | None:
        """The partner of ``membership_id``, or ``None`` if it is not in this
        pair."""
        if membership_id == self.membership_a_id:
            return self.membership_b_id
        if membership_id == self.membership_b_id:
            return self.membership_a_id
        return None


@dataclass(frozen=True, slots=True)
class MemberGroupCap:
    """At most this many members of one group may serve any one event.

    The pure form of the ministry- and period-scoped member-group limit a head
    configures (Task 74). It carries **an opaque group id, a number and a set
    of membership ids, and nothing else**: no group name, no description, no
    notion of what the category means. The solver counts people; what they have
    in common is not its business, and the values that reach it must not be
    able to tell it.

    **Counted per event, each member once.** Rule 2 of the engine already
    forbids one person two positions in one event, so the plain sum of a
    group's placement variables at an event counts *distinct members present* --
    which is exactly what the rule caps, whatever roles they were placed into.

    **Scoped by construction.** A :class:`SchedulingInput` describes one
    ministry's one scheduling period, so a cap here says nothing about another
    ministry or a later quarter -- the same way
    ``max_assignments_in_period`` and ``same_date_exclusions`` are scoped
    simply by being part of this object.

    A member id that is not a candidate in this run is harmless and is kept:
    somebody deactivated part-way through a period stops being a candidate
    while their group membership stays recorded, and a member who can never be
    present can never consume the cap.
    """

    member_group_id: int
    max_per_event: int
    member_membership_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        # Coerced so the value is genuinely immutable even when a caller hands
        # in a plain set: a frozen dataclass holding a mutable set would be
        # frozen in name only, and a later mutation would silently change what
        # a completed run had been constrained by.
        if not isinstance(self.member_membership_ids, frozenset):
            object.__setattr__(
                self,
                "member_membership_ids",
                frozenset(self.member_membership_ids),
            )

    def contains(self, membership_id: int) -> bool:
        return membership_id in self.member_membership_ids


@dataclass(frozen=True, slots=True)
class SameEventSupportRequirement:
    """One candidate who may serve an event only if others serve it too.

    The pure form of the ministry- and period-scoped same-event support
    requirement a head configures (Task 74): the *subject* may hold an
    assignment at an event only when at least ``min_supporters`` of
    ``supporter_membership_ids`` hold one at the **same event**.

    **Only the scheduling fact travels.** Three membership-id values and a
    count: no reason, no category, no relationship. The solver does not need to
    know why the condition exists, and could not be told.

    **Directional.** The requirement constrains the subject alone. A supporter
    is free to serve, or not, wherever they like; nothing here makes a claim
    about their schedule.

    **The subject is not one of their own supporters**, checked on
    construction: a self-supporting requirement would be satisfied by the very
    placement it is meant to condition, which is a rule that does nothing while
    looking like a rule that does something.

    **Same event, never the same date.** Two services on one Sunday are two
    events, and a supporter at the other one is not present here -- the
    opposite reading from :class:`LinkedMembershipPair`, deliberately, because
    these are different rules about different things.

    An **empty or too-small supporter set is legitimate input**, not an error:
    it says nobody has been approved to support this person, so the subject
    simply cannot be placed. The engine reports that as an unfilled position
    with its own diagnostic rather than refusing to run -- a configuration a
    head can see and fix beats a crash they cannot.
    """

    subject_membership_id: int
    min_supporters: int = 1
    supporter_membership_ids: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.supporter_membership_ids, frozenset):
            object.__setattr__(
                self,
                "supporter_membership_ids",
                frozenset(self.supporter_membership_ids),
            )
        if self.subject_membership_id in self.supporter_membership_ids:
            raise ValueError(
                "a membership cannot be its own supporter; the subject of a"
                f" support requirement ({self.subject_membership_id}) must not"
                " appear in its own supporter set"
            )

    @property
    def is_satisfiable(self) -> bool:
        """Whether enough supporters are even configured to reach the count.

        ``False`` is a configuration a head can correct, and the engine treats
        it as "this subject cannot be placed" rather than as an error.
        """
        return len(self.supporter_membership_ids) >= self.min_supporters


@dataclass(frozen=True, slots=True)
class AdjacentEventInput:
    """One of this ministry's events just *outside* the window being scheduled,
    and who is committed at it.

    The whole reason this exists is the boundary between periods (requirements
    §4.8). The event-gap rule counts a ministry's events, and the event
    immediately before a period's first event -- or immediately after its last
    -- is an ordinary member of that sequence; it simply happens to belong to
    another quarter. A run that could not see the earlier one would let
    somebody serve the last Sunday of Q3 and the first Sunday of Q4. A run that
    could not see the later one would let somebody serve the last Sunday of Q4
    when Q1 has already been published with them on its first Sunday -- the
    same violation, found from the other end.

    **One type for both sides.** The rule is symmetric: it does not care which
    of two assignments was decided first, only how many events separate them.
    A side flag would be a distinction the constraint never reads. Which side
    an event is on is carried by where it sits in the sequence.

    **Only as many events as the configured gap needs, on each side.** The
    builder loads the ``min_intervening_events`` nearest applicable events in
    each direction and nothing more; there is no notion here of "the ministry's
    history" in general, and none is wanted.

    ``assigned_membership_ids`` is who is recorded as serving at this event in
    the **authoritative** schedule for it (ADR 0003) -- the highest-numbered
    FINALIZED version of that event's schedule. That matters most on the
    forward side, where a draft for a later period may well exist: a draft
    nobody has agreed to does not block anybody, and a version an amendment has
    superseded no longer speaks for what happened.

    **A fixed fact, never a decision.** The solver reads these to know who is
    already committed; it never proposes, moves or removes anything here.
    """

    event_id: int
    event_date: datetime.date
    #: Memberships holding an authoritative assignment at this event. Empty is
    #: ordinary -- an event whose schedule was never finalized still *counts*
    #: as an intervening event, it simply blocks nobody.
    assigned_membership_ids: frozenset[int] = frozenset()

    @property
    def sort_key(self) -> tuple:
        """The same canonical ordering :class:`RequirementInput` uses."""
        return (self.event_date, self.event_id)


@dataclass(frozen=True, slots=True)
class ExistingAssignmentInput:
    """A decision this version already carries.

    Manual (Task 22) or carried forward (Task 29) -- indistinguishable here,
    and deliberately so: both are decisions a person made, and neither is
    something this task teaches the solver to undo. ``is_override`` marks that
    a head knowingly bypassed a bounded check when the row was created; the
    stored justification and its audit history stay where they are, since
    Task 26 is what reads them at finalization.
    """

    assignment_id: int
    requirement_id: int
    membership_id: int
    event_id: int
    is_override: bool = False


@dataclass(frozen=True, slots=True)
class SchedulingInput:
    """Everything one scheduling run needs, and nothing that ties it to a
    database.

    Collections are tuples in a stable, explicitly chosen order (see the
    builder), so two runs over the same state produce the same input and
    therefore the same solver behavior -- reproducibility that incidental
    PostgreSQL row order would quietly destroy.
    """

    schedule_version_id: int
    scheduling_period_id: int
    ministry_id: int
    requirements: tuple[RequirementInput, ...] = ()
    candidates: tuple[CandidateInput, ...] = ()
    existing_assignments: tuple[ExistingAssignmentInput, ...] = ()
    #: Pairs of candidates who must not both serve on any one calendar date in
    #: this period (requirements §4.4.2). A **hard** rule applied as a
    #: constraint before any optimisation pass, so no target, load-balancing or
    #: role-variety preference can trade it away, and no override reaches it.
    #:
    #: Empty is the ordinary case and means exactly what it says: no pair is
    #: linked. Absence is never a hint, and nothing infers a pair from
    #: historical assignments.
    #:
    #: Deliberately a property of the *input as a whole* rather than of a
    #: candidate: the rule is symmetric, and hanging it off one side would
    #: leave two places to keep in agreement.
    same_date_exclusions: tuple[LinkedMembershipPair, ...] = ()
    #: Per-event caps on how many members of one configured group may serve
    #: (Task 74). A **hard** rule applied as a constraint before any
    #: optimisation pass, so no preference can trade it away and no override
    #: reaches it.
    #:
    #: Empty is the ordinary case and means no group is capped. Only groups
    #: that actually carry a cap for this period appear -- a group with no
    #: configured limit is not a scheduling fact at all, and carrying it would
    #: be carrying a category the engine has no use for.
    member_group_caps: tuple[MemberGroupCap, ...] = ()
    #: Candidates who may serve an event only if enough of their configured
    #: supporters serve the same event (Task 74). Also a **hard** rule and also
    #: a constraint, for the same reasons.
    #:
    #: Empty is the ordinary case. A requirement naming a membership that is
    #: not a candidate is kept and is simply inert, exactly as an inert
    #: ``same_date_exclusions`` pair is.
    support_requirements: tuple[SameEventSupportRequirement, ...] = ()
    #: How many of **this ministry's own events** must fall between two
    #: assignments of the same person (requirements §4.8). ``None`` is the
    #: ordinary case and means there is no such rule; ``1`` means one event must
    #: be skipped, so no two consecutive events in the sequence below may go to
    #: the same person.
    #:
    #: A **hard** rule applied as a constraint before any optimisation pass, so
    #: no target, load-balancing or role-variety preference can trade it away,
    #: and no override reaches it.
    #:
    #: Ministry- and period-scoped simply by being part of this object, exactly
    #: as ``max_assignments_in_period`` and ``same_date_exclusions`` are.
    min_intervening_events: int | None = None
    #: This ministry's events immediately *before* the first event this run
    #: schedules, newest last, with who already serves at each. Only as many as
    #: ``min_intervening_events`` requires; empty when the rule is unconfigured,
    #: or when there is no earlier event to load.
    preceding_events: tuple[AdjacentEventInput, ...] = ()
    #: This ministry's events immediately *after* the last event this run
    #: schedules, earliest first, with who already serves at each. The mirror
    #: of ``preceding_events``, loaded to the same depth for the same reason:
    #: the rule is symmetric, so an authoritative assignment just past the end
    #: of this window constrains the window's last event exactly as one just
    #: before its start constrains the first.
    following_events: tuple[AdjacentEventInput, ...] = ()

    @property
    def event_dates(self) -> tuple[datetime.date, ...]:
        """The distinct Sundays this version schedules for, in order."""
        return tuple(sorted({r.event_date for r in self.requirements}))

    @property
    def scheduled_event_ids(self) -> tuple[int, ...]:
        """The distinct events this run schedules for, in canonical order.

        ``(event_date, event_id)`` -- the same ordering
        :attr:`RequirementInput.sort_key` leads with, so two events on one date
        have a defined, stable order rather than one that depends on how the
        requirements happened to arrive. The event model stores no time of day
        (scheduling-input §5), so the id is the only tie-break there is, and
        inventing one would be inventing a fact.
        """
        return tuple(
            event_id
            for _, event_id in sorted(
                {(r.event_date, r.event_id) for r in self.requirements}
            )
        )

    @property
    def ministry_event_sequence(self) -> tuple[int, ...]:
        """The ordered event sequence the gap rule counts along.

        The loaded earlier events, then the events this run schedules, then the
        loaded later ones -- one chronological line through **both** boundaries
        of the window. That is what makes "the previous event in this ministry"
        answerable at the first Sunday of a quarter, and "the next event"
        answerable at the last.

        All three parts are sorted by ``(event_date, event_id)``, and the
        builder guarantees every preceding event is strictly before every
        scheduled one and every following event strictly after
        (:func:`app.scheduling.solver._validate` re-checks it, because a
        hand-written input could say otherwise and a mis-ordered sequence would
        silently enforce the wrong gaps).
        """
        return (
            self._adjacent_event_ids(self.preceding_events)
            + self.scheduled_event_ids
            + self._adjacent_event_ids(self.following_events)
        )

    @staticmethod
    def _adjacent_event_ids(
        events: tuple[AdjacentEventInput, ...]
    ) -> tuple[int, ...]:
        """Sorted here rather than trusted from the caller: the sequence's
        order *is* the rule, so it is established in one place."""
        return tuple(
            event.event_id for event in sorted(events, key=lambda e: e.sort_key)
        )

    @property
    def total_required_positions(self) -> int:
        return sum(r.required_count for r in self.requirements)

    def linked_membership_ids(self, membership_id: int) -> frozenset[int]:
        """Everyone this candidate may not share a date with.

        Answered from both halves of every pair, because a membership may be
        either one -- the pairs are unordered, and the rule is symmetric.
        """
        partners = {
            partner
            for pair in self.same_date_exclusions
            if (partner := pair.other(membership_id)) is not None
        }
        return frozenset(partners)

    def member_group_caps_for(self, membership_id: int) -> tuple[MemberGroupCap, ...]:
        """Every capped group this membership belongs to.

        Plural on purpose: a member may be in several groups, each with its own
        cap, and all of them apply at once. Nothing here decides which is
        "the" rule for a person, because the rules are a conjunction rather
        than a ranking.
        """
        return tuple(cap for cap in self.member_group_caps if cap.contains(membership_id))

    def support_requirement_for(
        self, membership_id: int
    ) -> SameEventSupportRequirement | None:
        """This membership's own support requirement, if it has one.

        Singular, matching the database's one-requirement-per-subject-per-period
        uniqueness: two requirements for one person would be two answers to a
        question that has one.
        """
        for requirement in self.support_requirements:
            if requirement.subject_membership_id == membership_id:
                return requirement
        return None

    def candidate_by_membership_id(self, membership_id: int) -> CandidateInput | None:
        for candidate in self.candidates:
            if candidate.membership_id == membership_id:
                return candidate
        return None

    def eligible_candidates(
        self, requirement: RequirementInput
    ) -> tuple[CandidateInput, ...]:
        """Who may *ordinarily* fill ``requirement``.

        Eligibility here means qualification and role activity only -- the two
        facts that decide whether filling this position would even be a
        sensible request. Availability and church-wide conflicts are
        deliberately left out: ``NO_RESPONSE`` is a policy question, and a
        blocked date is a constraint the solver weighs, not a reason a person
        is not a candidate at all.

        A deactivated role yields nothing, which is the point: the solver must
        never place an ordinary assignment into one, and it should not have to
        remember to check.
        """
        if not requirement.role_is_active:
            return ()
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.is_qualified_for(requirement.ministry_role_id)
        )

    def existing_assignments_for(
        self, requirement_id: int
    ) -> tuple[ExistingAssignmentInput, ...]:
        return tuple(
            assignment
            for assignment in self.existing_assignments
            if assignment.requirement_id == requirement_id
        )
