"""What a scheduling run produced: proposals, and what it could not fill.

Plain immutable values, like everything else in this package -- no SQLAlchemy,
no ORM, no Session. A result is a *proposal*: nothing here has been persisted,
and turning any of it into an ``Assignment`` is a later, deliberate operation
that must go through Task 22's own validation.

**``proposed_assignments`` are new proposals only.** Assignments the version
already carries are inputs, not output: they are fixed, they consume capacity,
and re-emitting them would make a caller unable to tell what the solver
actually decided from what a person had already decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping

__all__ = [
    "DIAGNOSTIC_ALL_CHURCH_CONFLICTED",
    "DIAGNOSTIC_ALL_UNAVAILABLE",
    "DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT",
    "DIAGNOSTIC_ALL_AT_PERIOD_LIMIT",
    "DIAGNOSTIC_ALL_JOINED_MINISTRY_CONFLICT",
    "DIAGNOSTIC_ALL_WITHIN_EVENT_GAP",
    "DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT",
    "DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES",
    "DIAGNOSTIC_LINKED_DATE_CONFLICT",
    "DIAGNOSTIC_NO_QUALIFIED_CANDIDATES",
    "DIAGNOSTIC_NO_RESPONSE_DISALLOWED",
    "DIAGNOSTIC_ROLE_INACTIVE",
    "DIAGNOSTIC_SAME_EVENT_CONTENTION",
    "ProposedAssignment",
    "SchedulingResult",
    "SolutionMetrics",
    "UnfilledRequirement",
]

#: The bounded diagnostic vocabulary. Short codes, not a framework: each one
#: names a reason a position could not be filled, and a requirement may carry
#: several because several may genuinely apply. They are result metadata --
#: never persisted, and never claimed to be a minimal explanation, which would
#: mean running a second optimizer to prove.
DIAGNOSTIC_ROLE_INACTIVE = "ROLE_INACTIVE"
DIAGNOSTIC_NO_QUALIFIED_CANDIDATES = "NO_QUALIFIED_CANDIDATES"
DIAGNOSTIC_ALL_UNAVAILABLE = "ALL_UNAVAILABLE"
DIAGNOSTIC_NO_RESPONSE_DISALLOWED = "NO_RESPONSE_DISALLOWED"
DIAGNOSTIC_ALL_CHURCH_CONFLICTED = "ALL_CHURCH_CONFLICTED"
DIAGNOSTIC_SAME_EVENT_CONTENTION = "SAME_EVENT_CONTENTION"
DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES = "INSUFFICIENT_FEASIBLE_CANDIDATES"
#: Every otherwise-eligible candidate has reached the maximum number of
#: assignments configured for them in this period (requirements §4.4.1). A
#: distinct code because the remedy is distinct: this is not an availability
#: problem and not a qualification problem, and telling a head "nobody is
#: available" when the truth is "everyone is at their agreed limit" would send
#: them to the wrong screen -- and to the wrong conversation.
DIAGNOSTIC_ALL_AT_PERIOD_LIMIT = "ALL_AT_PERIOD_LIMIT"
#: Every otherwise-eligible candidate is linked to somebody who is already on
#: the roster for this date, so taking any of them would break a configured
#: same-date exclusion (requirements §4.4.2). Its own code, for the same
#: reason ``ALL_AT_PERIOD_LIMIT`` is: these people are neither unavailable nor
#: unqualified nor church-conflicted, and reporting them as any of those would
#: send a head to the wrong screen. It says a pair rule bit -- never who is
#: linked to whom, and never why.
DIAGNOSTIC_LINKED_DATE_CONFLICT = "LINKED_DATE_CONFLICT"
#: Every otherwise-eligible candidate served too recently in this ministry's own
#: event sequence to be scheduled here, under the period's configured
#: ``min_intervening_events`` rule (requirements §4.8). Its own code for the
#: same reason ``ALL_AT_PERIOD_LIMIT`` and ``LINKED_DATE_CONFLICT`` have theirs:
#: these people are willing, qualified, available and unconflicted, and telling
#: a head "nobody is available" would send them to the wrong screen entirely.
#: The remedy is a different one again -- change the gap rule, or accept the
#: open position.
DIAGNOSTIC_ALL_WITHIN_EVENT_GAP = "ALL_WITHIN_EVENT_GAP"
#: Every otherwise-eligible candidate belongs to a member group that has already
#: reached its configured maximum for this event (Task 74). Its own code, for
#: the same reason the three above have theirs: these people are qualified,
#: available, unconflicted and within every personal limit, and the remedy is a
#: different one again -- raise the group's number for this period, or accept
#: the open position. The code names a cap, never which group or what the
#: category means.
DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT = "ALL_AT_GROUP_EVENT_LIMIT"
#: Every otherwise-eligible candidate has a configured same-event support
#: requirement that this event's roster does not satisfy (Task 74) -- either
#: too few of their approved supporters ended up serving it, or none is
#: configured at all. Its own code for the same reason: the schedule is not
#: short of willing people, it is short of permitted ones, and the remedy is to
#: place a supporter, widen the approved set, or clear the requirement. It says
#: a support rule bit -- never who supports whom, and never why.
DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT = "ALL_WITHOUT_EVENT_SUPPORT"
#: Every otherwise-eligible candidate ended a **joined** multi-ministry run
#: (:mod:`app.scheduling.joined`) serving a different ministry on this date, so
#: the church-wide one-ministry-per-person-per-date rule is what left this
#: position open (Task 81). Never emitted by a single-ministry run, which
#: correctly knows nothing about the other ministries in a joined solve.
#:
#: Its own code for the same reason the ones above have theirs: these people
#: are qualified, available, unconflicted and within every personal limit, and
#: the remedy is a different one again -- more people cleared for this role, or
#: a different demand on that Sunday. It is distinct from
#: ``ALL_CHURCH_CONFLICTED``, which names a commitment to a ministry *outside*
#: the run, already finalized and unmovable. This one names a choice the run
#: itself made, and it is reported alongside the
#: :class:`~app.scheduling.joined.ChurchWideContention` rows that say where.
DIAGNOSTIC_ALL_JOINED_MINISTRY_CONFLICT = "ALL_JOINED_MINISTRY_CONFLICT"


@dataclass(frozen=True, slots=True)
class ProposedAssignment:
    """One new placement the solver is proposing.

    Deliberately just three ids. It is not an ``Assignment`` and carries no
    override flag or reason: automatic scheduling never creates an override
    (module scope), so there is nothing to record.
    """

    requirement_id: int
    membership_id: int
    event_id: int


@dataclass(frozen=True, slots=True)
class UnfilledRequirement:
    """A requirement the solver could not fully staff.

    Not an error -- an approved outcome. A generated schedule may be
    incomplete, and the unresolved positions must be preserved and displayed
    usefully rather than the run failing (schedule-output §2).
    """

    requirement_id: int
    missing_count: int
    diagnostic_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SolutionMetrics:
    """What the soft passes achieved, for inspection and explanation.

    Not needed to *use* a result -- the proposals and unfilled rows are the
    output -- but the two optimized quantities are otherwise invisible, and a
    caller explaining "Ann is carrying four against a target of three" would
    have to re-derive them. Domain numbers only; no OR-Tools internals.

    Each number is ``None`` when **its own** preference was not applied, so a
    reported figure always means "this was optimized, and here is what it
    reached". They are independent: a run may balance loads with no target at
    all, in which case ``fairness_cost`` is a number and
    ``target_excess_total`` is ``None`` -- not zero, which would claim a target
    had been set and perfectly met.
    """

    #: Total load per candidate: existing assignments plus new proposals.
    #: Only candidates carrying something appear.
    load_by_membership: Mapping[int, int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: How many of ``proposed_assignments`` placed a candidate whose stored
    #: answer for that event was ``BACKUP`` (Task 52). Unlike the fields
    #: below, this is never ``None``: avoiding ``BACKUP`` is not an opt-in
    #: ministry preference like a target or role variety, it is the pass that
    #: always runs whenever any ``BACKUP`` placement was even possible, so a
    #: plain count -- zero when none was needed -- describes every run
    #: honestly.
    backup_placement_total: int = 0
    #: Sum over candidates of ``max(0, load - target)`` at the optimum.
    #: ``None`` when no numeric target was configured.
    target_excess_total: int | None = None
    #: The convex balance measure, ``sum(load^2)``, at the optimum. Lower is
    #: more evenly distributed: 2,2,2 costs 12 where 4,1,1 costs 18.
    #: ``None`` when load balancing was switched off -- a number here would
    #: suggest a distribution had been optimized when nothing looked at it.
    fairness_cost: int | None = None
    #: ``sum(role_load^2)`` over the configured variety roles. Lower is more
    #: varied: two assignments in one role cost 4, one in each of two cost 2.
    #: ``None`` when no variety roles were configured -- zero would suggest a
    #: preference had been evaluated and perfectly satisfied.
    role_variety_cost: int | None = None


@dataclass(frozen=True, slots=True)
class SchedulingResult:
    """The whole outcome of one run, in a stable order."""

    proposed_assignments: tuple[ProposedAssignment, ...] = ()
    unfilled_requirements: tuple[UnfilledRequirement, ...] = ()
    #: Optional soft-objective summary. Defaults keep every Task 31 caller
    #: and test working unchanged.
    metrics: SolutionMetrics = field(default_factory=lambda: SolutionMetrics())

    @property
    def is_complete(self) -> bool:
        """Every required position is filled -- by an existing assignment, a
        new proposal, or both.
        """
        return not self.unfilled_requirements

    @property
    def filled_count(self) -> int:
        """How many positions this run newly filled. Existing assignments are
        not counted: they were already filled before it started.
        """
        return len(self.proposed_assignments)

    @property
    def unfilled_count(self) -> int:
        """Total missing positions, not the number of short requirements."""
        return sum(u.missing_count for u in self.unfilled_requirements)

    def proposals_for(self, requirement_id: int) -> tuple[ProposedAssignment, ...]:
        return tuple(
            proposal
            for proposal in self.proposed_assignments
            if proposal.requirement_id == requirement_id
        )
