"""The CP-SAT scheduling engine: fill as many required positions as possible.

Pure. Takes a :class:`~app.scheduling.input.SchedulingInput` and a
:class:`SchedulingPolicy`, returns a
:class:`~app.scheduling.result.SchedulingResult`, and touches no database:
no SQLAlchemy, no Session, no ORM model, no application service. The whole
engine can be exercised with a hand-written input, which is how it is tested.

**The model.** One Boolean per *ordinarily eligible* (requirement, candidate)
pair::

    x[requirement_id, membership_id] == 1  ->  propose this placement

Pairs that are already impossible get no variable at all: an unqualified
member, an unavailable one, a church-conflicted one, an inactive role. That
keeps the model small and, more importantly, keeps the constraints readable --
every remaining variable is a placement that would be legitimate on its own,
so the constraints only have to express how placements interact.

Seven constraint families and an objective, and nothing else:

1. **Capacity** -- each requirement takes at most its remaining slots, where
   remaining = ``required_count - existing`` (never negative).
2. **One position per person per event** -- including the events an existing
   assignment already commits them to. This is the approved rule: one
   *ministry* per Sunday, not one event, so the same person may serve two
   different events of this ministry on one date. Cross-ministry claims are
   already reduced to ``blocked_dates`` upstream (ADR 0002/0003) and are
   handled by simply not creating those variables.
3. **Person-period serving maximum** -- a candidate carrying a configured
   maximum takes at most that many assignments across the whole period,
   existing rows included (requirements §4.4.1). A *hard* rule: it is a
   constraint, so every soft pass below chooses only among schedules that
   already respect it, and it is never bypassed by an override -- raising
   somebody's limit is a decision made with that person, not a rule a head
   works around.
4. **Linked-pair same-date exclusion** -- for each configured pair and each
   calendar date, at most one of the two may be present that date, where
   *present* means holding any assignment at any event on it (requirements
   §4.4.2). Also a *hard* rule and also a constraint, so no preference below
   can trade it away and no override reaches it. Fixed assignments count: if
   one linked member already holds a row on a date, the other simply gets no
   variable there.
5. **Ministry event gap** -- when the period configures
   ``min_intervening_events``, one person may hold at most one assignment in
   any window of ``min_intervening_events + 1`` consecutive events of this
   ministry's own event sequence (requirements §4.8). Counted in *events*,
   never in days: an ad-hoc special event sits in the sequence beside the
   ordinary Sundays, so a Saturday special and the Sunday after it are
   consecutive, and so are two Sundays a fortnight apart with nothing between
   them. The sequence spans **both** period boundaries, because
   ``preceding_events`` and ``following_events`` carry the ministry events on
   either side that the rule needs to reach across -- a person authoritatively
   committed to the event just after this window is as much a blocker at its
   last event as one committed just before it is at its first. Also a *hard*
   rule and also a constraint, and like the two above it never removes an
   existing row to make itself true.
6. **Member-group event cap** -- for each configured
   :class:`~app.scheduling.input.MemberGroupCap` and each event, at most
   ``max_per_event`` members of that group may be present (Task 74). Counted
   per *event* and each member once whatever role they hold, which rule 2 makes
   free: one person cannot occupy two positions in an event, so the plain sum
   of a group's variables at an event already counts distinct people. Existing
   assignments consume the allowance. Also a *hard* rule and also a constraint.
7. **Same-event support requirement** -- for each configured
   :class:`~app.scheduling.input.SameEventSupportRequirement` and each event,
   the subject may be present only if at least ``min_supporters`` of their
   approved supporters are present at the **same event** (Task 74). Stated as
   one linear inequality per (subject, event) rather than through an auxiliary
   Boolean, again because rule 2 already bounds a person's presence at an event
   by one. Directional: it never constrains a supporter, only the subject.
   Also a *hard* rule and also a constraint.
8. **The objective** -- maximize the number of new placements, then minimize
   how many of those placements draw on ``BACKUP`` availability (Task 52,
   always on, not policy-gated), then apply whichever further soft
   preferences the *policy* asks for, strictly in order: target excess,
   candidate load balance, role variety. Each runs only after the ones above
   it are fixed as constraints, so no preference can ever cost a filled
   position or outrank a more important preference. Which of the
   policy-gated preferences apply is the caller's decision; none is invented
   here. Backup avoidance is not policy-gated because it is not a ministry
   preference like the others -- it is the tier's own meaning: "use me only
   if an ordinarily AVAILABLE candidate cannot."

**Existing assignments are fixed.** They are not modelled as variables, never
moved, removed, duplicated or re-emitted; they consume capacity and occupy
their event, and that is their entire role. A requirement already overfilled
by a historical capacity override simply takes no new placements -- it is not
an error here, because validating that override is Task 26's job at
finalization.

**Automatic scheduling never overrides anything.** Every bounded blocker Task
22 allows a head to bypass deliberately is, here, simply a pair that does not
exist. A position nobody may ordinarily fill comes back unfilled, with a
diagnostic -- which is the approved behavior: best effort, plus explicit
unresolved slots (schedule-output §2).

**Three functions, one behaviour.** Model construction, the objective passes
and reading the answer back out are separate (:func:`_build_ministry_block`,
:func:`_run_objective_passes`, :func:`_extract_result`), and
:func:`solve_schedule` is exactly those three over a single ministry. The split
exists so that a joined multi-ministry run (:mod:`app.scheduling.joined`, a
development path with no production caller) can put several ministries into one
model and add the church-wide person/date rule across them, while running the
*same* constraint code and the *same* passes. One copy of every rule, one copy
of every pass, and therefore no way for the two paths to come to disagree about
what any of it means. Nothing about a single-ministry run changed: with one
block the passes reduce, term for term, to the objective described above.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from types import MappingProxyType

from ortools.sat.python import cp_model

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    LinkedMembershipPair,
    MemberGroupCap,
    RequirementInput,
    SameEventSupportRequirement,
    SchedulingInput,
)
from app.scheduling.result import (
    DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT,
    DIAGNOSTIC_ALL_AT_PERIOD_LIMIT,
    DIAGNOSTIC_ALL_CHURCH_CONFLICTED,
    DIAGNOSTIC_ALL_UNAVAILABLE,
    DIAGNOSTIC_ALL_WITHIN_EVENT_GAP,
    DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT,
    DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES,
    DIAGNOSTIC_LINKED_DATE_CONFLICT,
    DIAGNOSTIC_NO_QUALIFIED_CANDIDATES,
    DIAGNOSTIC_NO_RESPONSE_DISALLOWED,
    DIAGNOSTIC_ROLE_INACTIVE,
    DIAGNOSTIC_SAME_EVENT_CONTENTION,
    ProposedAssignment,
    SchedulingResult,
    SolutionMetrics,
    UnfilledRequirement,
)

__all__ = [
    "SchedulingEngineError",
    "SchedulingInputError",
    "SchedulingPolicy",
    "solve_schedule",
]


#: Which CP-SAT search strategy the single worker runs.
#:
#: With one worker and no strategy named, CP-SAT picks its general-purpose
#: default. That default could not cope with a real ministry quarter: a
#: thirteen-Sunday, ten-position, fifty-one-volunteer schedule spent about
#: **seventeen minutes** in the load-balance pass, against a quarter of a
#: second once this strategy was named -- the same schedule, the same proven
#: optimum (Task 68). The cliff was steep and close by: the identical input cut
#: to twelve Sundays finished in about thirty-five seconds, so the quarter that
#: broke it was barely larger than the ones that did not.
#:
#: The reason is symmetry. A ministry's volunteers are largely interchangeable
#: within a role, so a balanced schedule has an enormous number of equally good
#: rearrangements, and the default strategy has to walk through them to prove
#: none is better. ``max_lp_sym`` is CP-SAT's linear relaxation *with symmetry
#: handling*: it recognizes that those rearrangements are the same schedule
#: wearing different names, and on the quarter that took seventeen minutes it
#: closed the bound to the optimum before finding its first solution.
#:
#: **This changes the search, never the answer.** It is not a time limit and
#: not an approximation: every pass still runs to proven optimality, so fill,
#: BACKUP, target excess, fairness and role variety all reach exactly the
#: optima the default strategy reached, and one worker keeps the run
#: reproducible. ``test_scheduling_search_portfolio.py`` is what holds that:
#: it solves the same inputs both ways and requires every optimum to match.
#:
#: Named strategies are CP-SAT's own vocabulary, so an OR-Tools upgrade could
#: in principle rename this one. That fails loudly rather than quietly -- an
#: unknown name makes CP-SAT return ``MODEL_INVALID``, which :func:`_solve_pass`
#: turns into :class:`SchedulingEngineError` on the very first pass -- so a
#: rename surfaces as a failing test suite, not as a slow schedule.
_SEARCH_STRATEGY = "max_lp_sym"


class SchedulingInputError(ValueError):
    """The supplied input is structurally invalid.

    A plain ``ValueError`` subclass, deliberately: importing the service
    layer's ``InvalidOperationError`` would break this package's
    database-independence for the sake of one exception type.
    """


class SchedulingEngineError(RuntimeError):
    """CP-SAT returned a status this engine cannot interpret as a schedule."""


@dataclass(frozen=True, slots=True)
class SchedulingPolicy:
    """Per-run policy for the one genuinely ministry-specific question.

    Availability has three states, and what a *missing answer* means is a
    per-ministry decision, not a fact (scheduling-input §8): Setup treats
    no-response as available today, another ministry may reasonably require an
    explicit yes. That choice is a property of the run, so it lives here
    rather than being hard-coded into the engine or baked into the input --
    the input reports what people said, and ``CandidateInput`` is never
    mutated to pretend otherwise.

    Deliberately generic and deliberately not named for any one ministry. When
    ConstraintConfiguration arrives it becomes a stored per-ministry setting;
    until then it is one flag a caller passes.
    """

    #: Whether a member who has not answered may be scheduled automatically.
    allow_no_response: bool = False
    #: How many assignments this ministry would *like* each person to carry in
    #: this period. When set, assignments above this number are penalized
    #: (minimized) as a soft preference. ``None`` means there is no numeric
    #: target-excess objective at all -- this preference simply does not run.
    #: Candidate load balancing is controlled independently by
    #: ``balance_candidate_loads`` below, and applies whether or not a numeric
    #: target is set.
    #:
    #: **A soft preference, never a cap.** It is optimized only after maximum
    #: fill is already fixed, so a person will be taken past it whenever that
    #: is what fills a position nobody else can. Turning it into
    #: ``load <= target`` would leave required slots empty to flatter a number,
    #: which the approved design explicitly rejects.
    #:
    #: Generic ministry policy, not any one ministry's number: the value is
    #: supplied per run, and no default is baked in here.
    target_assignments_per_candidate: int | None = None
    #: Whether to spread work evenly across candidates, by minimizing
    #: ``sum(load^2)`` once everything more important is settled.
    #:
    #: **Independent of the target, and on by default.** These are two
    #: different preferences: a target is a ministry's number ("about three
    #: each"), while balancing is the ordinary wish not to put one volunteer
    #: on every service while eligible people sit unused. A ministry with no
    #: documented number should not have to invent one to get a sensible
    #: spread -- which is exactly what gating balance behind the target used
    #: to force, and what this flag separates.
    #:
    #: Turning it off is a real choice, not a missing value: it says this run
    #: genuinely does not care how the work is distributed.
    balance_candidate_loads: bool = True
    #: The roles across which a volunteer's assignments should be spread, when
    #: everything more important is already settled. ``None`` or empty means
    #: no role-variety preference at all.
    #:
    #: **A caller-selected subset, never "every role".** A ministry may well
    #: want its Setup 2-5 positions rotated while leaving Setup Lead out of it,
    #: and including a role automatically just because somebody is qualified
    #: for it would impose a rotation nobody asked for. Role ids are supplied
    #: per run; none is written into this engine.
    #:
    #: A role listed here that has no requirement in this period is simply
    #: inert -- a ministry's configuration should not have to be edited for a
    #: quarter that happens not to need one of its roles.
    role_variety_role_ids: frozenset[int] | None = None

    def __post_init__(self) -> None:
        # Coerced so the policy is genuinely immutable even when a caller
        # hands in a plain set or list: a frozen dataclass holding a mutable
        # set would be frozen in name only, and a later mutation would
        # silently change what a completed run had optimized.
        if self.role_variety_role_ids is not None and not isinstance(
            self.role_variety_role_ids, frozenset
        ):
            object.__setattr__(
                self, "role_variety_role_ids", frozenset(self.role_variety_role_ids)
            )


@dataclass(frozen=True, slots=True)
class _MinistryBlock:
    """One ministry's decision variables and indexes inside a CP-SAT model.

    Everything :func:`_build_ministry_block` computed while translating one
    :class:`~app.scheduling.input.SchedulingInput` into constraints, kept
    because the objective passes and the result extraction below both need it
    and neither should recompute it.

    **Why this exists at all.** A single-ministry run builds exactly one block;
    a joined multi-ministry run (:mod:`app.scheduling.joined`) builds one per
    ministry into the *same* model and adds the church-wide person/date rule
    across them. Both then run the identical objective passes over a tuple of
    blocks. That is the whole point of the split: there is one copy of the
    constraint families and one copy of the lexicographic passes, so a joined
    run cannot come to disagree with a single-ministry run about what a rule
    means. With one block the passes reduce, term for term, to what they were
    before the split.

    Private: a caller gets a :class:`~app.scheduling.result.SchedulingResult`,
    never this.
    """

    scheduling_input: SchedulingInput
    policy: SchedulingPolicy
    #: ``(requirement_id, membership_id) -> BoolVar`` for every ordinarily
    #: eligible placement this run may still make.
    variables: dict
    requirements_by_id: dict
    candidates_by_membership_id: dict
    #: ``requirement_id -> required_count - existing``, never negative.
    remaining: dict
    #: ``requirement_id -> ordinarily eligible membership ids``.
    eligible: dict
    #: ``membership_id -> event ids an existing assignment already commits
    #: them to``.
    events_taken: dict
    existing_load: dict
    existing_role_load: dict
    #: ``event_id -> position in this ministry's own event sequence``.
    event_index: dict
    #: The subset of ``variables`` whose candidate answered ``BACKUP`` for
    #: that requirement's event.
    backup_placements: list


def solve_schedule(
    scheduling_input: SchedulingInput,
    *,
    policy: SchedulingPolicy,
) -> SchedulingResult:
    """Propose placements for ``scheduling_input``'s unfilled positions.

    Maximizes the number of newly filled positions and returns the best
    feasible answer. **An incomplete schedule is a normal result, not an
    error**: a requirement nobody may fill comes back in
    ``unfilled_requirements`` with diagnostics, and only a genuinely
    unusable model raises.

    ``scheduling_input`` is never mutated -- it is frozen throughout, and
    nothing here writes to it or to anything it references.

    :raises SchedulingInputError: the input is structurally invalid (duplicate
        ids, an existing assignment referencing an unknown requirement or
        candidate, an event mismatch, a non-positive ``required_count``, or the
        same membership assigned twice in one event).
    :raises SchedulingEngineError: CP-SAT returned neither ``OPTIMAL`` nor
        ``FEASIBLE``.
    """
    _validate(scheduling_input)
    _validate_policy(policy)

    model = cp_model.CpModel()
    block = _build_ministry_block(
        model, scheduling_input=scheduling_input, policy=policy
    )
    solver = _new_solver()
    _run_objective_passes(solver, model, blocks=(block,))
    return _extract_result(solver, block)


def _new_solver():
    """The one solver configuration, shared by every run.

    Deterministic by construction: one worker, one named strategy, a fixed
    seed and no time limit, so identical input yields an identical model and
    an identical search. A wall-clock limit would make the answer depend on
    how busy the machine is, which is exactly what a reproducible schedule
    must not do.
    """
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.subsolvers.append(_SEARCH_STRATEGY)
    solver.parameters.random_seed = 0
    return solver


# --------------------------------------------------------------------------
# One ministry's variables and its seven constraint families
# --------------------------------------------------------------------------


def _build_ministry_block(
    model,
    *,
    scheduling_input: SchedulingInput,
    policy: SchedulingPolicy,
) -> _MinistryBlock:
    """Add one ministry's variables and every one of its hard rules to
    ``model``, and return the indexes the passes and the extraction need.

    Adds constraints only -- **never an objective**. That separation is what
    lets a joined run put several ministries in one model before any
    optimization is expressed, and it is what keeps every rule here
    unreachable by every preference below: by the time a pass sets an
    objective, all seven families are already constraints.
    """
    requirements = scheduling_input.requirements
    candidates = scheduling_input.candidates
    existing_by_requirement: dict[int, int] = defaultdict(int)
    events_taken: dict[int, set[int]] = defaultdict(set)
    requirements_by_id = {r.requirement_id: r for r in requirements}
    existing_load: dict[int, int] = defaultdict(int)
    # Which roles a person has already served, for the same reason: variety is
    # about a volunteer's whole period, not only the part the solver chose.
    existing_role_load: dict[tuple[int, int], int] = defaultdict(int)
    for assignment in scheduling_input.existing_assignments:
        existing_by_requirement[assignment.requirement_id] += 1
        events_taken[assignment.membership_id].add(assignment.event_id)
        # Decisions already made are part of what this person is carrying, so
        # fairness has to see them -- otherwise someone with two manual
        # assignments would look idle and be given more.
        existing_load[assignment.membership_id] += 1
        source = requirements_by_id.get(assignment.requirement_id)
        if source is not None:
            existing_role_load[
                (assignment.membership_id, source.ministry_role_id)
            ] += 1

    remaining = {
        requirement.requirement_id: max(
            requirement.required_count
            - existing_by_requirement[requirement.requirement_id],
            0,
        )
        for requirement in requirements
    }

    eligible = {
        requirement.requirement_id: _eligible_membership_ids(
            requirement, candidates, policy
        )
        for requirement in requirements
    }

    variables: dict[tuple[int, int], cp_model.IntVar] = {}
    for requirement in requirements:
        requirement_id = requirement.requirement_id
        if remaining[requirement_id] == 0:
            # Nothing to place here: already full, or overfilled by a
            # historical capacity override. Either way, no variables.
            continue
        for membership_id in eligible[requirement_id]:
            if requirement.event_id in events_taken[membership_id]:
                # Already serving this event through an existing assignment.
                continue
            variables[(requirement_id, membership_id)] = model.NewBoolVar(
                f"x_r{requirement_id}_m{membership_id}"
            )

    # 1. Capacity: never more than the remaining slots, so a requirement can
    #    never end up over its required_count through automatic work.
    # (Rule 3 -- the per-person period maximum -- is added below, before any
    # objective is set, so it constrains the model rather than competing with
    # it.)
    for requirement in requirements:
        requirement_id = requirement.requirement_id
        row = [
            variable
            for (r_id, _), variable in variables.items()
            if r_id == requirement_id
        ]
        if row:
            model.Add(sum(row) <= remaining[requirement_id])

    # 2. One position per person per event. Cross-event rules are deliberately
    #    absent here: two events of *this* ministry on one date are allowed.
    #    The church-wide one-ministry-per-date rule is expressed either by
    #    ``blocked_dates`` (a separately solved ministry, ADR 0002/0003) or, in
    #    a joined run, as a constraint across blocks -- never by weakening this
    #    one.
    per_person_event: dict[tuple[int, int], list] = defaultdict(list)
    for (requirement_id, membership_id), variable in variables.items():
        event_id = _requirement_by_id(requirements, requirement_id).event_id
        per_person_event[(membership_id, event_id)].append(variable)
    for group in per_person_event.values():
        if len(group) > 1:
            model.Add(sum(group) <= 1)

    # 3. Person-period serving maximum: a hard cap on how much one candidate
    #    may carry in this period (requirements §4.4.1). Expressed on the new
    #    decision variables alone, with existing assignments subtracted from
    #    the allowance -- those are fixed inputs this engine never removes, so
    #    the only question it can answer is how many *more* are permitted.
    #
    #    A candidate already at or past their maximum gets ``sum(...) <= 0``,
    #    which forbids new work without deleting anybody's existing
    #    assignment. That over-limit state is real and is reported at
    #    finalization; silently dropping a row to satisfy a lowered maximum
    #    would destroy a decision a person made.
    #
    #    Added here, among the constraints, and deliberately not as an
    #    objective term: it must be unreachable by the target, load-balancing
    #    and role-variety passes, all of which run afterwards and can only
    #    choose among schedules this constraint already permits.
    per_candidate_variables: dict[int, list] = defaultdict(list)
    for (_requirement_id, membership_id), variable in variables.items():
        per_candidate_variables[membership_id].append(variable)
    for candidate in candidates:
        allowance = candidate.remaining_capacity(
            existing_load.get(candidate.membership_id, 0)
        )
        if allowance is None:
            continue
        row = per_candidate_variables.get(candidate.membership_id, [])
        if row:
            model.Add(sum(row) <= allowance)

    # 4. Linked-pair same-date exclusion: for each configured pair and each
    #    calendar date in this input, at most one of the two may be *present*
    #    on that date (requirements §4.4.2). Present means holding **any**
    #    assignment at **any** event on that date -- not merely being in the
    #    same event -- because a period may hold two services on one Sunday and
    #    putting one linked member at each would break the rule just as
    #    squarely.
    #
    #    Expressed through a presence Boolean per (membership, date) rather
    #    than by summing raw placement variables: rule 2 above allows one
    #    person two positions on one date across two events, so a plain
    #    ``sum(A) + sum(B) <= 1`` would forbid that entirely for linked
    #    members -- a rule nobody agreed to, and a needless loss of fill.
    #    ``AddMaxEquality`` over Booleans is exactly "at least one of these",
    #    which is what presence means.
    #
    #    Added here, among the constraints, and deliberately not as an
    #    objective term: it must be unreachable by the target, load-balancing
    #    and role-variety passes, which run afterwards and can only choose
    #    among schedules this constraint already permits.
    _add_same_date_exclusions(
        model,
        variables=variables,
        pairs=scheduling_input.same_date_exclusions,
        requirements_by_id=requirements_by_id,
        dates_taken=_dates_taken(
            scheduling_input.existing_assignments, requirements_by_id
        ),
    )

    # 5. Ministry event gap: at most one assignment per person in any window of
    #    `min_intervening_events + 1` consecutive ministry events, counted along
    #    the sequence that spans the period boundary (requirements §4.8).
    #
    #    Added here, among the constraints, and deliberately not as an objective
    #    term: it must be unreachable by the target, load-balancing and
    #    role-variety passes, which run afterwards and can only choose among
    #    schedules this constraint already permits.
    event_index = _event_index(scheduling_input)
    _add_event_gap_constraints(
        model,
        variables=variables,
        scheduling_input=scheduling_input,
        requirements_by_id=requirements_by_id,
        event_index=event_index,
        events_taken=events_taken,
    )

    # Both group-shaped rules reason about *who is present at an event*, so both
    # read the same two indexes: which new placements each membership could take
    # at each event, and which memberships an existing assignment already fixes
    # there. Built once and shared rather than twice and separately, so the two
    # constraints cannot come to disagree about what presence means.
    variables_by_membership_event = _variables_by_membership_event(
        variables, requirements_by_id
    )
    scheduled_event_ids = scheduling_input.scheduled_event_ids

    # 6. Member-group event cap: at most ``max_per_event`` members of a
    #    configured group present at any one event (Task 74). Existing
    #    assignments consume the allowance, and a group already over its cap at
    #    an event simply gets no new placements there -- never a deletion, and
    #    never an infeasible model, for the same reason rules 4 and 5 never
    #    produce one.
    #
    #    Added here, among the constraints, and deliberately not as an objective
    #    term: it must be unreachable by the target, load-balancing and
    #    role-variety passes.
    _add_member_group_caps(
        model,
        caps=scheduling_input.member_group_caps,
        event_ids=scheduled_event_ids,
        variables_by_membership_event=variables_by_membership_event,
        events_taken=events_taken,
    )

    # 7. Same-event support requirement: a subject may be present at an event
    #    only if enough of their approved supporters are present at that same
    #    event (Task 74). Also a constraint, for the same reason.
    _add_support_requirements(
        model,
        requirements=scheduling_input.support_requirements,
        event_ids=scheduled_event_ids,
        variables_by_membership_event=variables_by_membership_event,
        events_taken=events_taken,
    )

    # Every placement variable whose candidate answered BACKUP for that
    # requirement's event -- the pool the backup pass minimizes. Built from the
    # candidates' own stored answers, not a policy flag: unlike target excess,
    # load balancing and role variety, avoiding BACKUP is not an opt-in
    # ministry preference, it is what the tier itself means (Task 52).
    # Existing assignments are deliberately excluded -- they are fixed inputs
    # this engine never re-evaluates by tier, whatever the candidate's answer
    # is today.
    candidates_by_membership_id = {c.membership_id: c for c in candidates}
    backup_placements = [
        variable
        for (requirement_id, membership_id), variable in variables.items()
        if candidates_by_membership_id[membership_id].availability_for(
            requirements_by_id[requirement_id].event_id
        )
        is AvailabilityState.BACKUP
    ]

    return _MinistryBlock(
        scheduling_input=scheduling_input,
        policy=policy,
        variables=variables,
        requirements_by_id=requirements_by_id,
        candidates_by_membership_id=candidates_by_membership_id,
        remaining=remaining,
        eligible=eligible,
        events_taken=events_taken,
        existing_load=existing_load,
        existing_role_load=existing_role_load,
        event_index=event_index,
        backup_placements=backup_placements,
    )


# --------------------------------------------------------------------------
# The lexicographic objective passes, over one block or many
# --------------------------------------------------------------------------


def _run_objective_passes(solver, model, *, blocks: tuple[_MinistryBlock, ...]) -> None:
    """Maximize fill, then minimize BACKUP, then apply each block's own
    policy-gated preferences -- each pass fixed as a constraint before the
    next sets its objective.

    **The first two passes are church-wide; the rest are ministry-scoped.**
    Fill and BACKUP are counts of positions and of tier draws, directly
    comparable across ministries, so summing them treats every ministry's
    open position as worth exactly as much as every other's -- which is the
    only neutral reading available, and deliberately not a priority order.
    Target excess, load balance and role variety are read from each block's
    *own* policy against that ministry's *own* roles, because one ministry's
    role count and another's are not the same quantity and adding them up
    would invent a comparison nobody approved.

    With a single block this is, term for term, the single-ministry objective
    it replaced.
    """
    all_variables = [
        variable for block in blocks for variable in block.variables.values()
    ]

    # -- Pass 1: maximum fill. Always, and always first. --
    fill = sum(all_variables) if all_variables else 0
    if all_variables:
        model.Maximize(fill)
    _solve_pass(solver, model, "maximum fill")
    optimal_fill = int(solver.Value(fill)) if all_variables else 0

    backup_placements = [
        variable for block in blocks for variable in block.backup_placements
    ]
    wants_soft_pass = bool(backup_placements) or any(
        block.policy.target_assignments_per_candidate is not None
        or block.policy.balance_candidate_loads
        or bool(block.policy.role_variety_role_ids)
        for block in blocks
    )

    if all_variables and wants_soft_pass:
        # Fix what pass 1 achieved. Everything after this point chooses
        # *among* schedules that fill exactly as many positions -- which is
        # what makes every soft preference a tie-break rather than a
        # competing goal.
        model.Add(fill == optimal_fill)

    if backup_placements:
        # -- Pass 2: fewest of those filled positions drawn from BACKUP
        #    availability. Ranked directly below fill and above every
        #    policy-gated preference: an AVAILABLE candidate is preferred over
        #    a BACKUP one whenever swapping them costs nothing else, but never
        #    at the cost of a position pass 1 already filled.
        total_backup = sum(backup_placements)
        model.Minimize(total_backup)
        _solve_pass(solver, model, "backup avoidance")
        optimal_backup = int(solver.Value(total_backup))
        # Fixed in turn, so target excess, load balancing and role variety can
        # only choose among schedules that already use as few BACKUP
        # placements as this input allows.
        model.Add(total_backup == optimal_backup)

    # Both load-based passes read the same per-candidate load variables, and
    # each is gated on its own preference: a target says "aim for this many",
    # balancing says "spread it evenly", and neither implies the other. One
    # set per block: a person serving two ministries carries one load in each,
    # because the two are different ministries' work and pooling them would
    # let one ministry's assignment cancel out another's.
    loads_by_block: list[dict] = []
    for block in blocks:
        target = block.policy.target_assignments_per_candidate
        loads_by_block.append(
            _load_variables(
                model,
                variables=block.variables,
                candidates=block.scheduling_input.candidates,
                existing_load=block.existing_load,
            )
            if block.variables
            and (target is not None or block.policy.balance_candidate_loads)
            else {}
        )

    # -- Pass 3: fewest assignments above each ministry's own target. --
    excesses = []
    for block, loads in zip(blocks, loads_by_block):
        target = block.policy.target_assignments_per_candidate
        if not loads or target is None:
            continue
        for membership_id, load in loads.items():
            excess = model.NewIntVar(
                0, max(load.upper_bound - target, 0), f"excess_m{membership_id}"
            )
            # With the objective minimizing it, >= is enough to pin the
            # variable to max(0, load - target) exactly.
            model.Add(excess >= load.variable - target)
            excesses.append(excess)
    if excesses:
        total_excess = sum(excesses)
        model.Minimize(total_excess)
        _solve_pass(solver, model, "target excess")
        optimal_excess = int(solver.Value(total_excess))
        # Fixed in turn, so nothing after this can trade the target away.
        model.Add(total_excess == optimal_excess)

    # -- Pass 4: the most even distribution among those. --
    squares = []
    for block, loads in zip(blocks, loads_by_block):
        if not loads or not block.policy.balance_candidate_loads:
            continue
        for membership_id, load in loads.items():
            square = model.NewIntVar(
                0, load.upper_bound * load.upper_bound, f"sq_m{membership_id}"
            )
            model.AddMultiplicationEquality(square, [load.variable, load.variable])
            squares.append(square)
    if squares:
        total_cost = sum(squares)
        model.Minimize(total_cost)
        _solve_pass(solver, model, "load balance")
        optimal_cost = int(solver.Value(total_cost))
        # Fixed in turn, so role variety can only choose among schedules that
        # are already as fair as this input allows.
        model.Add(total_cost == optimal_cost)

    # -- Final pass: spread each volunteer's work across the configured
    #    roles. Last, so it never trades away fill, target excess or
    #    fairness -- each of those is a constraint by the time this runs.
    role_squares = []
    for block in blocks:
        variety_roles = block.policy.role_variety_role_ids or frozenset()
        if not variety_roles or not block.variables:
            continue
        role_loads = _role_load_variables(
            model,
            variables=block.variables,
            requirements_by_id=block.requirements_by_id,
            candidates=block.scheduling_input.candidates,
            existing_role_load=block.existing_role_load,
            variety_roles=variety_roles,
        )
        for key, load in role_loads.items():
            square = model.NewIntVar(
                0, load.upper_bound * load.upper_bound, f"rolesq_m{key[0]}_r{key[1]}"
            )
            model.AddMultiplicationEquality(square, [load.variable, load.variable])
            role_squares.append(square)
    if role_squares:
        model.Minimize(sum(role_squares))
        _solve_pass(solver, model, "role variety")


# --------------------------------------------------------------------------
# Reading one ministry's answer back out
# --------------------------------------------------------------------------


def _extract_result(solver, block: _MinistryBlock) -> SchedulingResult:
    """Turn the solved model into one ministry's proposals, unfilled rows and
    metrics.

    Every number is measured from the finished proposals rather than read back
    off a CP-SAT objective variable, so a metric stays correct even when its
    pass never ran.
    """
    scheduling_input = block.scheduling_input
    requirements = scheduling_input.requirements
    candidates = scheduling_input.candidates
    variables = block.variables
    requirements_by_id = block.requirements_by_id
    candidates_by_membership_id = block.candidates_by_membership_id
    remaining = block.remaining
    existing_load = block.existing_load
    existing_role_load = block.existing_role_load
    policy = block.policy
    target = policy.target_assignments_per_candidate
    balance_loads = policy.balance_candidate_loads
    variety_roles = policy.role_variety_role_ids or frozenset()

    proposals = [
        ProposedAssignment(
            requirement_id=requirement_id,
            membership_id=membership_id,
            event_id=_requirement_by_id(requirements, requirement_id).event_id,
        )
        for (requirement_id, membership_id), variable in variables.items()
        if solver.Value(variable) == 1
    ]
    proposals.sort(key=lambda p: (p.requirement_id, p.membership_id))

    placed_by_requirement: dict[int, int] = defaultdict(int)
    for proposal in proposals:
        placed_by_requirement[proposal.requirement_id] += 1

    # Final per-candidate load: what they were already carrying plus what this
    # run just proposed. Computed before the diagnostics because that is the
    # load a diagnostic has to reason about -- "everyone is at their limit" is
    # a statement about the finished schedule, and the pre-run counts cannot
    # see the candidates this very run filled up.
    load_by_membership = dict(existing_load)
    for proposal in proposals:
        load_by_membership[proposal.membership_id] = (
            load_by_membership.get(proposal.membership_id, 0) + 1
        )

    # Who ended up on the roster for each date -- existing rows plus what this
    # run proposed -- and who each membership may not share a date with. Both
    # are needed to say honestly *why* a linked pair left a position open, and
    # both describe the finished schedule rather than the pre-run state: the
    # partner who blocks a candidate may well be someone this very run placed.
    final_presence: dict[object, set[int]] = defaultdict(set)
    linked_partners: dict[int, frozenset[int]] = {}
    if scheduling_input.same_date_exclusions:
        for membership_id, taken in _dates_taken(
            scheduling_input.existing_assignments, requirements_by_id
        ).items():
            for event_date in taken:
                final_presence[event_date].add(membership_id)
        for proposal in proposals:
            source = requirements_by_id[proposal.requirement_id]
            final_presence[source.event_date].add(proposal.membership_id)
        linked_partners = {
            candidate.membership_id: scheduling_input.linked_membership_ids(
                candidate.membership_id
            )
            for candidate in candidates
        }

    # Who ends up on each *event's* roster -- the event-level counterpart of
    # ``final_presence`` above, which the two group-shaped rules need because
    # both are about a crew rather than a calendar day. Built only when one of
    # them is configured, like every other diagnostic index here.
    presence_by_event: dict[int, set[int]] = {}
    support_by_subject: dict[int, SameEventSupportRequirement] = {}
    if scheduling_input.member_group_caps or scheduling_input.support_requirements:
        presence_by_event = _presence_by_event(
            existing_assignments=scheduling_input.existing_assignments,
            requirements_by_id=requirements_by_id,
            proposals=proposals,
        )
        support_by_subject = {
            requirement.subject_membership_id: requirement
            for requirement in scheduling_input.support_requirements
        }

    # Which positions in the ministry's event sequence each membership ends up
    # occupying -- loaded history, existing rows, and what this run proposed.
    # Like the linked-pair presence map above it describes the *finished*
    # schedule rather than the pre-run state, because the person who blocks a
    # candidate under the gap rule may well be that same candidate, placed by
    # this very run at the event before.
    gap_indices: dict[int, frozenset[int]] = {}
    if scheduling_input.min_intervening_events:
        gap_indices = _final_event_indices(
            scheduling_input,
            event_index=block.event_index,
            events_taken=block.events_taken,
            proposals=proposals,
            requirements_by_id=requirements_by_id,
        )

    unfilled = []
    for requirement in requirements:
        requirement_id = requirement.requirement_id
        missing = remaining[requirement_id] - placed_by_requirement[requirement_id]
        if missing <= 0:
            continue
        unfilled.append(
            UnfilledRequirement(
                requirement_id=requirement_id,
                missing_count=missing,
                diagnostic_codes=_diagnose(
                    requirement=requirement,
                    candidates=candidates,
                    policy=policy,
                    eligible_membership_ids=block.eligible[requirement_id],
                    placed=placed_by_requirement[requirement_id],
                    final_load=load_by_membership,
                    linked_partners=linked_partners,
                    presence_by_date=final_presence,
                    min_intervening_events=scheduling_input.min_intervening_events,
                    event_index=block.event_index,
                    final_event_indices=gap_indices,
                    member_group_caps=scheduling_input.member_group_caps,
                    support_by_subject=support_by_subject,
                    presence_by_event=presence_by_event,
                ),
            )
        )
    unfilled.sort(key=lambda u: u.requirement_id)

    final_role_load = dict(existing_role_load)
    for proposal in proposals:
        role_id = requirements_by_id[proposal.requirement_id].ministry_role_id
        key = (proposal.membership_id, role_id)
        final_role_load[key] = final_role_load.get(key, 0) + 1

    # Measured from the resulting loads rather than read back off the solver:
    # the numbers describe the schedule, so they are just as true when there
    # was nothing left to decide (every position already filled) as when the
    # optimizer chose between options. They necessarily equal the optima the
    # passes above reached, because they are the same quantities.
    backup_placement_total = sum(
        1
        for proposal in proposals
        if candidates_by_membership_id[proposal.membership_id].availability_for(
            requirements_by_id[proposal.requirement_id].event_id
        )
        is AvailabilityState.BACKUP
    )

    return SchedulingResult(
        proposed_assignments=tuple(proposals),
        unfilled_requirements=tuple(unfilled),
        metrics=SolutionMetrics(
            load_by_membership=MappingProxyType(dict(sorted(load_by_membership.items()))),
            backup_placement_total=backup_placement_total,
            target_excess_total=(
                None if target is None
                else sum(max(load - target, 0) for load in load_by_membership.values())
            ),
            fairness_cost=(
                None if not balance_loads
                else sum(load * load for load in load_by_membership.values())
            ),
            role_variety_cost=(
                None if not variety_roles
                else sum(
                    count * count
                    for key, count in final_role_load.items()
                    if key[1] in variety_roles
                )
            ),
        ),
    )


# --------------------------------------------------------------------------
# The linked-pair same-date exclusion
# --------------------------------------------------------------------------


def _dates_taken(
    existing_assignments,
    requirements_by_id: dict[int, RequirementInput],
) -> dict[int, set]:
    """``membership_id -> the calendar dates an existing assignment fixes``.

    Read through the requirement's ``event_date`` rather than the event id,
    because the rule is about the *date*: two events on one Sunday are one
    date here, and that is the whole difference between this rule and the
    one-position-per-event rule above.
    """
    taken: dict[int, set] = defaultdict(set)
    for assignment in existing_assignments:
        requirement = requirements_by_id.get(assignment.requirement_id)
        if requirement is not None:
            taken[assignment.membership_id].add(requirement.event_date)
    return taken


def _add_same_date_exclusions(
    model,
    *,
    variables: dict[tuple[int, int], object],
    pairs: tuple[LinkedMembershipPair, ...],
    requirements_by_id: dict[int, RequirementInput],
    dates_taken: dict[int, set],
) -> None:
    """At most one of each linked pair present on each date.

    Three cases per (pair, date), and the split is the point:

    - **Neither is fixed there.** Both get a presence Boolean, and
      ``presence(A) + presence(B) <= 1`` is added. A candidate with no
      variable on that date has constant-zero presence and needs neither.
    - **Exactly one is fixed there.** That side's presence is already 1, so
      the other side's variables on that date are simply forced to zero. No
      presence variable is created for a value that is not in question.
    - **Both are fixed there.** The version is *already* violating the rule --
      a head configured the exclusion after the assignments existed, or
      carried both forward. Nothing is added, deliberately: an infeasible
      model would fail the whole run, and this engine must never respond to a
      pre-existing conflict by deleting somebody's assignment. The violation
      is real, it is reported by
      :mod:`app.services.finalization_readiness`, and generation leaves it
      exactly as bad as it found it -- no new placement can add a second
      violation on a date that already has one.
    """
    if not pairs:
        return

    # Which new placements each membership could take, grouped by date.
    per_membership_date: dict[tuple[int, object], list] = defaultdict(list)
    for (requirement_id, membership_id), variable in variables.items():
        event_date = requirements_by_id[requirement_id].event_date
        per_membership_date[(membership_id, event_date)].append(variable)

    # Every date this input touches, in a stable order so the model built for
    # a given input is identical run to run.
    dates = sorted(
        {requirement.event_date for requirement in requirements_by_id.values()}
    )

    for pair in pairs:
        a_id, b_id = pair.membership_ids
        for event_date in dates:
            a_fixed = event_date in dates_taken.get(a_id, ())
            b_fixed = event_date in dates_taken.get(b_id, ())
            a_vars = per_membership_date.get((a_id, event_date), [])
            b_vars = per_membership_date.get((b_id, event_date), [])

            if a_fixed and b_fixed:
                continue  # pre-existing violation; see the docstring
            if a_fixed:
                for variable in b_vars:
                    model.Add(variable == 0)
                continue
            if b_fixed:
                for variable in a_vars:
                    model.Add(variable == 0)
                continue
            if not a_vars or not b_vars:
                # One side cannot be present here at all, so the sum can never
                # reach two and the constraint would be vacuous.
                continue

            presence_a = _presence_variable(model, a_id, event_date, a_vars)
            presence_b = _presence_variable(model, b_id, event_date, b_vars)
            model.Add(presence_a + presence_b <= 1)


def _presence_variable(model, membership_id: int, event_date, row: list):
    """A Boolean that is 1 exactly when this membership takes *any* of ``row``.

    ``AddMaxEquality`` over Booleans is the OR, stated in the form CP-SAT
    linearizes directly. Equality rather than ``>=`` matters: a one-sided
    bound would let the solver set presence to 1 for free and never bind the
    pair constraint at all.
    """
    presence = model.NewBoolVar(f"present_m{membership_id}_{event_date.isoformat()}")
    model.AddMaxEquality(presence, row)
    return presence


# --------------------------------------------------------------------------
# The ministry event-gap rule
# --------------------------------------------------------------------------


def _event_index(scheduling_input: SchedulingInput) -> dict[int, int]:
    """``event_id -> its position`` in this ministry's event sequence.

    Positions, not dates. The whole rule is about how many of this ministry's
    events lie between two assignments, so the only thing the constraints below
    ever compare is an index -- there is deliberately no arithmetic on dates
    anywhere in this section, because "seven days apart" and "the next event"
    are different rules and only one of them is this one (requirements §4.8).
    """
    return {
        event_id: index
        for index, event_id in enumerate(scheduling_input.ministry_event_sequence)
    }


def _gap_windows(length: int, min_intervening_events: int) -> list[range]:
    """Every run of ``min_intervening_events + 1`` consecutive positions.

    **Windows rather than pairs**, and it is worth saying why. "At least N
    events between two assignments" is exactly "at most one assignment in any
    N+1 consecutive events", and the window form states that in one linear
    constraint per window instead of N pairwise ones -- fewer constraints, and
    a tighter linear relaxation for a search strategy (``max_lp_sym``) whose
    whole value is the relaxation it computes (Task 68).

    A sequence no longer than one window yields a single window covering it,
    so a period shorter than the configured gap still gets the rule applied to
    everything it does contain rather than to nothing.
    """
    size = min_intervening_events + 1
    if length < 2:
        return []
    if length <= size:
        return [range(length)]
    return [range(start, start + size) for start in range(length - size + 1)]


def _fixed_event_indices(
    scheduling_input: SchedulingInput,
    *,
    event_index: dict[int, int],
    events_taken: dict[int, set[int]],
) -> dict[int, frozenset[int]]:
    """``membership_id -> positions this run cannot change``.

    Two sources, and they are the same kind of fact from the solver's point of
    view: an assignment already in this version, and an authoritative
    assignment at one of the loaded events on either side of it. Neither is a
    decision the engine may revisit -- it never moves an existing row, and it
    certainly never edits the finalized schedule of the quarter before or
    after.
    """
    fixed: dict[int, set[int]] = defaultdict(set)
    for event in (
        scheduling_input.preceding_events + scheduling_input.following_events
    ):
        index = event_index.get(event.event_id)
        if index is None:
            continue
        for membership_id in event.assigned_membership_ids:
            fixed[membership_id].add(index)
    for membership_id, event_ids in events_taken.items():
        for event_id in event_ids:
            index = event_index.get(event_id)
            if index is not None:
                fixed[membership_id].add(index)
    return {membership_id: frozenset(indices) for membership_id, indices in fixed.items()}


def _add_event_gap_constraints(
    model,
    *,
    variables: dict[tuple[int, int], object],
    scheduling_input: SchedulingInput,
    requirements_by_id: dict[int, RequirementInput],
    event_index: dict[int, int],
    events_taken: dict[int, set[int]],
) -> None:
    """At most one assignment per person in any window of consecutive events.

    **No presence variable is needed, and that is not an accident.** Rule 2
    above already forbids one person two positions in one event, so the plain
    sum of a candidate's placement variables across a window counts *events
    they are present at*, and ``<= 1`` is the rule stated directly. Introducing
    an auxiliary Boolean per (membership, event) would say the same thing with
    more variables for the search to walk through.

    Three cases per (candidate, window), and the split is the point:

    - **Nothing fixed in the window.** The window's variables sum to at most
      one, which is the rule.
    - **Something fixed in the window.** The candidate is already present there
      -- through an existing assignment, or through an authoritative assignment
      at one of the loaded events on either side -- so every variable of theirs
      in that window is forced to zero. This is the case that makes the rule
      hold across both edges of a period: somebody who served the last event of
      the previous quarter gets no variable at the first event of this one, and
      somebody already published on the first event of the *next* quarter gets
      none at this one's last.
    - **Two or more fixed in the window.** The version is *already* violating
      the rule -- a head configured or raised the gap after the assignments
      existed, or carried them forward. The variables are still forced to zero,
      so this run cannot make it worse, and nothing is added that would make the
      model infeasible: an infeasible model fails the whole generation, and this
      engine must never respond to a pre-existing conflict by deleting somebody's
      assignment. The violation is real, and
      :mod:`app.services.finalization_readiness` reports it until a head acts.

    Windows lying entirely inside the loaded events on either side contain no
    variables at all and are skipped, which is why loading more surrounding
    events than the gap needs is inert rather than harmful.
    """
    gap = scheduling_input.min_intervening_events
    if not gap:
        return
    length = len(event_index)
    windows = _gap_windows(length, gap)
    if not windows:
        return

    # Which new placements each candidate could take, grouped by position.
    by_membership_index: dict[int, dict[int, list]] = defaultdict(
        lambda: defaultdict(list)
    )
    for (requirement_id, membership_id), variable in variables.items():
        index = event_index[requirements_by_id[requirement_id].event_id]
        by_membership_index[membership_id][index].append(variable)

    fixed = _fixed_event_indices(
        scheduling_input, event_index=event_index, events_taken=events_taken
    )

    # Sorted so the model built for a given input is identical run to run --
    # the same reproducibility the single worker and fixed seed exist for.
    for membership_id in sorted(by_membership_index):
        by_index = by_membership_index[membership_id]
        fixed_indices = fixed.get(membership_id, frozenset())
        for window in windows:
            occupied = [index for index in window if index in by_index]
            if not occupied:
                continue
            if fixed_indices.intersection(window):
                for index in occupied:
                    for variable in by_index[index]:
                        model.Add(variable == 0)
                continue
            if len(occupied) > 1:
                # Only when the window spans two or more events this candidate
                # could actually take. One event's worth of variables is already
                # capped at one by rule 2, so the constraint would be vacuous.
                model.Add(
                    sum(
                        variable
                        for index in occupied
                        for variable in by_index[index]
                    )
                    <= 1
                )


def _final_event_indices(
    scheduling_input: SchedulingInput,
    *,
    event_index: dict[int, int],
    events_taken: dict[int, set[int]],
    proposals: list[ProposedAssignment],
    requirements_by_id: dict[int, RequirementInput],
) -> dict[int, frozenset[int]]:
    """``membership_id -> positions occupied in the finished schedule``.

    The fixed positions plus whatever this run proposed. Built after the solve,
    for the diagnostics alone -- the constraints above never need it, because
    they reason about variables rather than about an outcome.
    """
    occupied: dict[int, set[int]] = {
        membership_id: set(indices)
        for membership_id, indices in _fixed_event_indices(
            scheduling_input, event_index=event_index, events_taken=events_taken
        ).items()
    }
    for proposal in proposals:
        index = event_index.get(requirements_by_id[proposal.requirement_id].event_id)
        if index is not None:
            occupied.setdefault(proposal.membership_id, set()).add(index)
    return {
        membership_id: frozenset(indices)
        for membership_id, indices in occupied.items()
    }


# --------------------------------------------------------------------------
# The two group-shaped rules: member-group caps and same-event support
# --------------------------------------------------------------------------


def _variables_by_membership_event(
    variables: dict[tuple[int, int], object],
    requirements_by_id: dict[int, RequirementInput],
) -> dict[tuple[int, int], list]:
    """``(membership_id, event_id) -> the placements they could take there``.

    One index shared by both rules below, because both ask the same question of
    the model: *could this person be present at this event, and through which
    variables?* A membership with two qualified roles at one event has two
    entries here; rule 2 already caps their sum at one, which is what lets both
    rules treat that sum as a presence indicator rather than a count of
    positions.
    """
    by_key: dict[tuple[int, int], list] = defaultdict(list)
    for (requirement_id, membership_id), variable in variables.items():
        event_id = requirements_by_id[requirement_id].event_id
        by_key[(membership_id, event_id)].append(variable)
    return by_key


def _add_member_group_caps(
    model,
    *,
    caps: tuple[MemberGroupCap, ...],
    event_ids: tuple[int, ...],
    variables_by_membership_event: dict[tuple[int, int], list],
    events_taken: dict[int, set[int]],
) -> None:
    """At most ``max_per_event`` members of each capped group, per event.

    **One linear constraint per (group, event), and no auxiliary variables.**
    Rule 2 forbids one person two positions in one event, so the sum of a
    group's placement variables at an event *is* the number of distinct group
    members this run would put there. Introducing a presence Boolean per member
    would state the same thing with more variables for the search to walk
    through -- the same reasoning the event-gap rule uses for the same reason.

    **Existing assignments consume the allowance, and are never touched.** A
    member already committed to the event through an existing row gets no
    variable there at all (the model builder skips those pairs), so they are
    counted once -- in ``fixed`` -- rather than twice.

    Three cases per (group, event), and the split is the point:

    - **Room left.** The variables sum to at most the remaining allowance.
    - **Already full.** The allowance is zero and every one of the group's
      variables at that event is forced to zero. No deletion, no reshuffle.
    - **Already over.** A head raised the group's membership or lowered the cap
      after the assignments existed. Treated exactly like "already full":
      ``max(..., 0)`` keeps the allowance non-negative, so this run cannot make
      the violation worse and the model stays feasible. An infeasible model
      would fail the whole generation, and this engine must never respond to a
      pre-existing conflict by deleting somebody's assignment. The violation is
      real and :mod:`app.services.finalization_readiness` reports it until a
      head acts.
    """
    if not caps:
        return
    for cap in sorted(caps, key=lambda c: c.member_group_id):
        members = sorted(cap.member_membership_ids)
        if not members:
            continue  # a group with nobody in it caps nothing
        for event_id in event_ids:
            fixed = sum(
                1
                for membership_id in members
                if event_id in events_taken.get(membership_id, ())
            )
            row = [
                variable
                for membership_id in members
                for variable in variables_by_membership_event.get(
                    (membership_id, event_id), ()
                )
            ]
            if not row:
                continue
            model.Add(sum(row) <= max(cap.max_per_event - fixed, 0))


def _add_support_requirements(
    model,
    *,
    requirements: tuple[SameEventSupportRequirement, ...],
    event_ids: tuple[int, ...],
    variables_by_membership_event: dict[tuple[int, int], list],
    events_taken: dict[int, set[int]],
) -> None:
    """A subject is present at an event only if enough supporters are too.

    **One linear inequality per (subject, event)**, stated directly on the
    placement variables::

        min_supporters * present(subject) <= fixed_supporters + sum(supporter vars)

    where ``present(subject)`` is the sum of the subject's own variables at that
    event. Rule 2 bounds both sides' per-person sums by one, so every sum here
    counts *distinct people present* rather than positions filled, and no
    auxiliary Boolean is needed on either side.

    Three cases per (subject, event), and the split is the point:

    - **The subject is not fixed there.** The inequality above is added as it
      stands. If the supporters cannot reach the count at that event, it forces
      the subject's variables to zero -- the subject is simply not placed there,
      which is the rule doing its job rather than an error.
    - **The subject is fixed there**, through an existing assignment this engine
      may not remove. ``present(subject)`` is the constant one, so the
      inequality becomes a genuine demand on the run: it must place enough
      supporters at that event. That is the intended behaviour -- a subject
      carried forward or assigned by hand obliges the run to find their support.
    - **The subject is fixed there and the demand cannot be met** -- too few
      supporters are configured, or none of them can be placed at that event.
      Nothing is added, deliberately: the constraint would make the model
      infeasible and fail the whole generation, and this engine must never
      respond to a pre-existing conflict by deleting somebody's assignment. The
      violation is real, :mod:`app.services.finalization_readiness` reports it,
      and generation leaves it exactly as bad as it found it.

    Nothing here ever constrains a supporter's own schedule: they appear only on
    the permissive side of the inequality, so satisfying somebody's requirement
    is something a supporter's placement *allows*, never something it is forced
    into for its own sake.
    """
    if not requirements:
        return
    for requirement in sorted(
        requirements, key=lambda r: r.subject_membership_id
    ):
        subject_id = requirement.subject_membership_id
        supporters = sorted(requirement.supporter_membership_ids)
        needed = requirement.min_supporters
        for event_id in event_ids:
            subject_vars = variables_by_membership_event.get(
                (subject_id, event_id), ()
            )
            subject_fixed = event_id in events_taken.get(subject_id, ())
            if not subject_vars and not subject_fixed:
                continue  # the subject cannot be present here at all

            fixed_supporters = sum(
                1
                for membership_id in supporters
                if event_id in events_taken.get(membership_id, ())
            )
            supporter_vars = [
                variable
                for membership_id in supporters
                for variable in variables_by_membership_event.get(
                    (membership_id, event_id), ()
                )
            ]
            # How many distinct supporters could end up present here at best:
            # those already committed, plus those with at least one variable.
            # Counted in people rather than variables, because a supporter
            # qualified for two roles at one event still counts once.
            reachable = fixed_supporters + sum(
                1
                for membership_id in supporters
                if variables_by_membership_event.get((membership_id, event_id))
            )

            if subject_fixed:
                if reachable < needed:
                    continue  # pre-existing violation; see the docstring
                if supporter_vars:
                    model.Add(sum(supporter_vars) >= needed - fixed_supporters)
                # No else: with no supporter variables, ``reachable`` is
                # ``fixed_supporters`` and the branch above has already proven
                # it meets the count, so there is nothing left to constrain.
                continue

            model.Add(
                needed * sum(subject_vars)
                <= fixed_supporters + (sum(supporter_vars) if supporter_vars else 0)
            )


def _presence_by_event(
    *,
    existing_assignments,
    requirements_by_id: dict[int, RequirementInput],
    proposals: list[ProposedAssignment],
) -> dict[int, set[int]]:
    """``event_id -> the memberships on its roster`` in the finished schedule.

    Existing rows plus what this run proposed. Built after the solve, for the
    diagnostics alone: the constraints above reason about variables, and an
    outcome is exactly what they must not depend on.
    """
    presence: dict[int, set[int]] = defaultdict(set)
    for assignment in existing_assignments:
        requirement = requirements_by_id.get(assignment.requirement_id)
        if requirement is not None:
            presence[requirement.event_id].add(assignment.membership_id)
    for proposal in proposals:
        requirement = requirements_by_id.get(proposal.requirement_id)
        if requirement is not None:
            presence[requirement.event_id].add(proposal.membership_id)
    return presence


# --------------------------------------------------------------------------
# The lexicographic passes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CandidateLoad:
    """One candidate's load variable and the largest value it can take."""

    variable: object
    upper_bound: int


def _solve_pass(solver, model, label: str) -> None:
    """Run one optimization pass, or fail loudly.

    Each pass narrows the model rather than re-weighting one objective: an
    optimum found here is added back as a constraint before the next pass sets
    its own objective. That is what makes the priority order exact instead of
    a matter of choosing large-enough weights -- there is no arithmetic in
    which a fairness gain can outbid a filled position, because by the time
    fairness is considered the fill count is a constraint.
    """
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise SchedulingEngineError(
            f"the scheduling model could not be solved during the {label} pass"
            f" (CP-SAT status {solver.StatusName(status)})"
        )


def _load_variables(
    model,
    *,
    variables: dict[tuple[int, int], object],
    candidates: tuple[CandidateInput, ...],
    existing_load: dict[int, int],
) -> dict[int, "_CandidateLoad"]:
    """One integer per candidate: everything they are carrying in this run.

    ``load = existing assignments + new proposals``. Existing rows are a
    constant term, never a variable -- they are fixed decisions, and fairness
    influences only what is still to be decided.

    Only candidates who could actually receive something, or who already carry
    something, get a variable; someone ineligible everywhere contributes a
    constant to every schedule and would only add noise to the objective.
    """
    per_candidate: dict[int, list] = defaultdict(list)
    for (_, membership_id), variable in variables.items():
        per_candidate[membership_id].append(variable)

    loads: dict[int, _CandidateLoad] = {}
    for candidate in candidates:
        membership_id = candidate.membership_id
        row = per_candidate.get(membership_id, [])
        already = existing_load.get(membership_id, 0)
        if not row and not already:
            continue
        upper = already + len(row)
        load = model.NewIntVar(already, upper, f"load_m{membership_id}")
        model.Add(load == already + sum(row))
        # The bound is carried rather than read back from the variable:
        # ``IntVar.Proto()`` is not a safe accessor in the pinned OR-Tools
        # build, and this value is known exactly here anyway.
        loads[membership_id] = _CandidateLoad(variable=load, upper_bound=upper)
    return loads


def _role_load_variables(
    model,
    *,
    variables: dict[tuple[int, int], object],
    requirements_by_id: dict[int, RequirementInput],
    candidates: tuple[CandidateInput, ...],
    existing_role_load: dict[tuple[int, int], int],
    variety_roles: frozenset[int],
) -> dict[tuple[int, int], "_CandidateLoad"]:
    """One integer per (candidate, configured role): how much of that role
    this person carries, existing work included.

    **Only the configured roles get a variable.** A role outside the set --
    Setup Lead, in the motivating example -- contributes nothing to the
    objective, so serving it repeatedly is neither rewarded nor punished. That
    is the whole reason the set is caller-selected rather than derived from
    whatever people happen to be qualified for.

    A pair with no existing load and no possible new placement is skipped: it
    would be a constant zero in the objective and only add variables.
    """
    per_pair: dict[tuple[int, int], list] = defaultdict(list)
    for (requirement_id, membership_id), variable in variables.items():
        role_id = requirements_by_id[requirement_id].ministry_role_id
        if role_id in variety_roles:
            per_pair[(membership_id, role_id)].append(variable)

    keys = set(per_pair) | {
        key for key, count in existing_role_load.items()
        if key[1] in variety_roles and count
    }
    known_memberships = {candidate.membership_id for candidate in candidates}

    role_loads: dict[tuple[int, int], _CandidateLoad] = {}
    for key in sorted(keys):
        membership_id, role_id = key
        if membership_id not in known_memberships:
            continue
        row = per_pair.get(key, [])
        already = existing_role_load.get(key, 0)
        upper = already + len(row)
        load = model.NewIntVar(already, upper, f"roleload_m{membership_id}_r{role_id}")
        model.Add(load == already + sum(row))
        role_loads[key] = _CandidateLoad(variable=load, upper_bound=upper)
    return role_loads


# --------------------------------------------------------------------------
# Ordinary eligibility
# --------------------------------------------------------------------------


def _eligible_membership_ids(
    requirement: RequirementInput,
    candidates: tuple[CandidateInput, ...],
    policy: SchedulingPolicy,
) -> tuple[int, ...]:
    """Who may *ordinarily* fill this requirement, in a stable order.

    Every one of Task 22's bounded blockers appears here as a reason to leave
    the pair out entirely -- never as something an automatic run may bypass.
    """
    if not requirement.role_is_active:
        return ()
    return tuple(
        candidate.membership_id
        for candidate in candidates
        if _is_ordinarily_eligible(requirement, candidate, policy)
    )


def _is_ordinarily_eligible(
    requirement: RequirementInput,
    candidate: CandidateInput,
    policy: SchedulingPolicy,
) -> bool:
    if not candidate.is_qualified_for(requirement.ministry_role_id):
        return False
    if candidate.is_blocked_on(requirement.event_date):
        return False
    return _availability_allows(
        candidate.availability_for(requirement.event_id), policy
    )


def _availability_allows(state: AvailabilityState, policy: SchedulingPolicy) -> bool:
    """``UNAVAILABLE`` never; ``AVAILABLE`` and ``BACKUP`` always -- both are
    fully feasible, ``BACKUP`` only costs the soft preference pass 2 of the
    objective minimizes (Task 52); ``NO_RESPONSE`` only if the run's policy
    says a missing answer may be scheduled.

    **Exhaustive on purpose.** Every :class:`AvailabilityState` member is
    handled by name; an unrecognized one raises rather than silently taking
    whichever branch happens to be last, which is exactly the failure mode
    Task 51 flagged in this function before ``BACKUP`` existed.
    """
    if state is AvailabilityState.AVAILABLE:
        return True
    if state is AvailabilityState.BACKUP:
        return True
    if state is AvailabilityState.UNAVAILABLE:
        return False
    if state is AvailabilityState.NO_RESPONSE:
        return policy.allow_no_response
    raise AssertionError(f"unhandled AvailabilityState member: {state!r}")


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


def _diagnose(
    *,
    requirement: RequirementInput,
    candidates: tuple[CandidateInput, ...],
    policy: SchedulingPolicy,
    eligible_membership_ids: tuple[int, ...],
    placed: int,
    final_load: dict[int, int],
    linked_partners: dict[int, frozenset[int]],
    presence_by_date: dict[object, set[int]],
    min_intervening_events: int | None,
    event_index: dict[int, int],
    final_event_indices: dict[int, frozenset[int]],
    member_group_caps: tuple[MemberGroupCap, ...] = (),
    support_by_subject: dict[int, SameEventSupportRequirement] | None = None,
    presence_by_event: dict[int, set[int]] | None = None,
) -> tuple[str, ...]:
    """Why this requirement is short, as a small sorted set of codes.

    Deliberately reports *every* reason that applies rather than guessing at a
    single cause: if nobody is qualified and the role is also inactive, both
    are true and claiming one would be misleading. Equally deliberately, it
    does not attempt a minimal unsat core -- proving the smallest explanation
    would mean running a second optimizer, which is a lot of machinery for a
    line of text.

    When candidates *do* exist individually but could not all be placed, the
    cause is competition, and it is named generically.

    **The linked-pair attribution is a statement about the finished schedule,
    not an unsat core.** ``LINKED_DATE_CONFLICT`` is emitted when every
    otherwise-feasible candidate for this position is linked to somebody who
    ended up on the roster for this date. That is honest and useful, and it is
    the limit of what this engine can say cheaply: a shortfall that a pair rule
    caused only indirectly -- by pushing the optimizer onto a different
    candidate three Sundays earlier -- falls back to the generic contention
    codes, because proving that attribution would mean running a second
    optimizer over counterfactual models.
    """
    codes: set[str] = set()

    if not requirement.role_is_active:
        codes.add(DIAGNOSTIC_ROLE_INACTIVE)

    qualified = [
        candidate
        for candidate in candidates
        if candidate.is_qualified_for(requirement.ministry_role_id)
    ]
    if not qualified:
        codes.add(DIAGNOSTIC_NO_QUALIFIED_CANDIDATES)
    else:
        unblocked = [
            candidate
            for candidate in qualified
            if not candidate.is_blocked_on(requirement.event_date)
        ]
        if not unblocked:
            codes.add(DIAGNOSTIC_ALL_CHURCH_CONFLICTED)
        else:
            states = [
                candidate.availability_for(requirement.event_id)
                for candidate in unblocked
            ]
            if all(state is AvailabilityState.UNAVAILABLE for state in states):
                codes.add(DIAGNOSTIC_ALL_UNAVAILABLE)
            elif not any(_availability_allows(state, policy) for state in states):
                # Nobody left is usable by availability at all: every
                # remaining candidate is either UNAVAILABLE or an
                # unanswered NO_RESPONSE this run's policy will not schedule
                # automatically. Checked through the same rule the model
                # itself uses (``_availability_allows``) rather than
                # re-testing AVAILABLE by name, so a BACKUP candidate --
                # always usable -- can never be misreported here as if the
                # pool held only silence or refusals (Task 52).
                codes.add(DIAGNOSTIC_NO_RESPONSE_DISALLOWED)

    if qualified and not codes:
        # Everyone who could otherwise have taken this position is blocked by
        # a *permission* rule rather than by availability: they are at the
        # maximum they agreed to for this period, they are linked to somebody
        # already on the roster for this date, or they served too recently in
        # this ministry's own event sequence. Reporting contention or
        # unavailability here would be actively misleading -- the schedule is
        # not short of willing people, it is short of permitted ones, and the
        # remedies differ. Several codes are emitted when several apply to
        # different candidates, because each is then true and picking one would
        # be a guess.
        feasible = [
            candidate
            for candidate in qualified
            if not candidate.is_blocked_on(requirement.event_date)
            and candidate.availability_for(requirement.event_id)
            is not AvailabilityState.UNAVAILABLE
        ]
        if feasible:
            feasible_ids = {candidate.membership_id for candidate in feasible}
            at_limit = {
                candidate.membership_id
                for candidate in feasible
                if candidate.remaining_capacity(
                    final_load.get(candidate.membership_id, 0)
                )
                == 0
            }
            # A candidate is linked-blocked when a member they may not share a
            # date with is on the finished roster for this date -- including
            # one this very run placed, which is why the presence map is built
            # after the solve rather than from the pre-run state.
            present = presence_by_date.get(requirement.event_date, set())
            linked_blocked = {
                candidate.membership_id
                for candidate in feasible
                if linked_partners.get(candidate.membership_id, frozenset())
                & present
            }
            gap_blocked = _gap_blocked_membership_ids(
                feasible,
                requirement=requirement,
                min_intervening_events=min_intervening_events,
                event_index=event_index,
                final_event_indices=final_event_indices,
            )
            roster = (presence_by_event or {}).get(requirement.event_id, set())
            group_blocked = _group_capped_membership_ids(
                feasible, member_group_caps=member_group_caps, roster=roster
            )
            support_blocked = _support_blocked_membership_ids(
                feasible,
                support_by_subject=support_by_subject or {},
                roster=roster,
            )
            # Ordered, because the single-cause branch reports the first rule
            # that explains the whole shortfall on its own -- and because a
            # deterministic order keeps the code set stable for a given input.
            reasons = (
                (DIAGNOSTIC_ALL_AT_PERIOD_LIMIT, at_limit),
                (DIAGNOSTIC_LINKED_DATE_CONFLICT, linked_blocked),
                (DIAGNOSTIC_ALL_WITHIN_EVENT_GAP, gap_blocked),
                (DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT, group_blocked),
                (DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT, support_blocked),
            )
            explained = set().union(*(blocked for _, blocked in reasons))
            sole = next(
                (code for code, blocked in reasons if blocked == feasible_ids), None
            )
            if sole is not None:
                codes.add(sole)
            elif explained == feasible_ids:
                # No rule alone explains it, but between them they cover
                # everybody. Naming one would understate why the position is
                # open; naming contention would be plainly wrong.
                for code, blocked in reasons:
                    if blocked:
                        codes.add(code)
            elif explained and explained | roster >= feasible_ids:
                # The rest are already on this event's own roster, which is
                # ordinary contention rather than a permission rule -- rule 2
                # forbids anybody a second position in one event. Reporting
                # only contention here would hide a real cap; reporting only
                # the cap would overstate it, so both are said.
                #
                # ``roster`` is empty unless a member-group cap or a support
                # requirement is configured for this run, so this branch cannot
                # change what any other rule reports.
                for code, blocked in reasons:
                    if blocked:
                        codes.add(code)
                codes.add(DIAGNOSTIC_SAME_EVENT_CONTENTION)

    if eligible_membership_ids and not codes:
        # People were eligible for this position on their own, so the shortfall
        # is contention: the same finite candidates were needed elsewhere, or
        # are already serving this event.
        codes.add(DIAGNOSTIC_SAME_EVENT_CONTENTION)
        codes.add(DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES)
    elif not eligible_membership_ids and not codes:
        codes.add(DIAGNOSTIC_INSUFFICIENT_FEASIBLE_CANDIDATES)

    return tuple(sorted(codes))


def _group_capped_membership_ids(
    feasible: list[CandidateInput],
    *,
    member_group_caps: tuple[MemberGroupCap, ...],
    roster: set[int],
) -> set[int]:
    """Who a member-group cap keeps out of this position, in the finished
    schedule.

    A statement about the outcome, not an unsat core -- the same honesty
    ``LINKED_DATE_CONFLICT`` and ``ALL_WITHIN_EVENT_GAP`` keep. A candidate
    counts here when some group they belong to already has its full complement
    on this event's roster. **The candidate is excluded from that count**: the
    question is whether adding *them* would breach the cap, so counting a
    person who is not on the roster would be counting a placement that did not
    happen.

    A shortfall the cap caused only indirectly -- by pushing the optimizer onto
    somebody else at another event -- falls back to the generic contention
    codes, because proving that would mean solving counterfactual models.
    """
    if not member_group_caps:
        return set()
    blocked: set[int] = set()
    for candidate in feasible:
        membership_id = candidate.membership_id
        for cap in member_group_caps:
            if not cap.contains(membership_id):
                continue
            present = len(
                (cap.member_membership_ids & roster) - {membership_id}
            )
            if present >= cap.max_per_event:
                blocked.add(membership_id)
                break
    return blocked


def _support_blocked_membership_ids(
    feasible: list[CandidateInput],
    *,
    support_by_subject: dict[int, SameEventSupportRequirement],
    roster: set[int],
) -> set[int]:
    """Who a same-event support requirement keeps out of this position.

    A candidate counts here when they are the subject of a configured
    requirement and this event's finished roster carries too few of their
    approved supporters -- including the case where no supporter is configured
    at all, which is the honest report for a requirement nobody can satisfy.

    Like the helpers above, it describes the outcome rather than proving a
    cause: a run that could have placed a supporter and chose not to because
    something more important needed them is reported here too, and rightly --
    from the head's point of view the position is open because the support was
    not there.
    """
    if not support_by_subject:
        return set()
    blocked: set[int] = set()
    for candidate in feasible:
        requirement = support_by_subject.get(candidate.membership_id)
        if requirement is None:
            continue
        present = len(requirement.supporter_membership_ids & roster)
        if present < requirement.min_supporters:
            blocked.add(candidate.membership_id)
    return blocked


def _gap_blocked_membership_ids(
    feasible: list[CandidateInput],
    *,
    requirement: RequirementInput,
    min_intervening_events: int | None,
    event_index: dict[int, int],
    final_event_indices: dict[int, frozenset[int]],
) -> set[int]:
    """Who the event-gap rule keeps out of this position, in the finished
    schedule.

    A statement about the outcome, not an unsat core -- the same honesty
    ``LINKED_DATE_CONFLICT`` keeps. A candidate counts here when they end up
    serving another event within the configured gap of this one, whether that
    was an existing row, last quarter's finalized schedule, or a placement this
    very run made. A shortfall the gap rule caused only *indirectly* -- by
    pushing the optimizer onto somebody else three events earlier -- falls back
    to the generic contention codes, because proving that would mean solving
    counterfactual models.
    """
    if not min_intervening_events:
        return set()
    index = event_index.get(requirement.event_id)
    if index is None:
        return set()
    return {
        candidate.membership_id
        for candidate in feasible
        if any(
            other != index and abs(other - index) <= min_intervening_events
            for other in final_event_indices.get(candidate.membership_id, ())
        )
    }


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate(scheduling_input: SchedulingInput) -> None:
    """Protect the solver's assumptions against hand-written input.

    Task 30's builder cannot produce any of these -- the database's own
    constraints see to that -- but this package's whole point is that it works
    without a database, so a caller constructing values by hand must be told
    when they have described something impossible rather than getting a
    confidently wrong schedule.
    """
    requirement_ids = [r.requirement_id for r in scheduling_input.requirements]
    if len(set(requirement_ids)) != len(requirement_ids):
        raise SchedulingInputError("requirement ids must be unique")

    membership_ids = [c.membership_id for c in scheduling_input.candidates]
    if len(set(membership_ids)) != len(membership_ids):
        raise SchedulingInputError("candidate membership ids must be unique")

    for requirement in scheduling_input.requirements:
        if requirement.required_count <= 0:
            raise SchedulingInputError(
                f"requirement {requirement.requirement_id} has a non-positive"
                f" required_count ({requirement.required_count})"
            )

    for candidate in scheduling_input.candidates:
        maximum = candidate.max_assignments_in_period
        if maximum is not None and maximum <= 0:
            # Zero is not a limit, it is "never schedule this person", which
            # availability and membership deactivation already say. Accepting
            # it here would give that meaning a second, quieter spelling.
            raise SchedulingInputError(
                f"candidate {candidate.membership_id} has a non-positive"
                f" max_assignments_in_period ({maximum}); use None for no"
                " maximum"
            )

    _validate_event_gap(scheduling_input)
    _validate_member_group_caps(scheduling_input)
    _validate_support_requirements(scheduling_input)

    seen_pairs: set[tuple[int, int]] = set()
    for pair in scheduling_input.same_date_exclusions:
        # LinkedMembershipPair canonicalizes and refuses a self-pair on
        # construction, so what is left to check here is duplication: the same
        # rule twice is a builder bug, and silently deduplicating it would hide
        # one.
        if pair.membership_ids in seen_pairs:
            raise SchedulingInputError(
                "the same linked pair appears more than once:"
                f" {pair.membership_a_id} and {pair.membership_b_id}"
            )
        seen_pairs.add(pair.membership_ids)

    # A pair naming a membership that is not a candidate is deliberately *not*
    # an error. A member deactivated part-way through a period stops being a
    # candidate while their pair rule stays configured; the rule is then simply
    # inert, because someone with no variables and no existing assignment can
    # never be present. Refusing the input would block generation over a rule
    # that cannot affect it.

    assignment_ids = [a.assignment_id for a in scheduling_input.existing_assignments]
    if len(set(assignment_ids)) != len(assignment_ids):
        raise SchedulingInputError("existing assignment ids must be unique")

    requirements_by_id = {
        r.requirement_id: r for r in scheduling_input.requirements
    }
    known_memberships = set(membership_ids)
    seen_membership_event: set[tuple[int, int]] = set()

    for assignment in scheduling_input.existing_assignments:
        requirement = requirements_by_id.get(assignment.requirement_id)
        if requirement is None:
            raise SchedulingInputError(
                f"existing assignment {assignment.assignment_id} references"
                f" unknown requirement {assignment.requirement_id}"
            )
        if assignment.membership_id not in known_memberships:
            raise SchedulingInputError(
                f"existing assignment {assignment.assignment_id} references"
                f" unknown candidate membership {assignment.membership_id}"
            )
        if assignment.event_id != requirement.event_id:
            raise SchedulingInputError(
                f"existing assignment {assignment.assignment_id} claims event"
                f" {assignment.event_id} but its requirement is for event"
                f" {requirement.event_id}"
            )
        key = (assignment.membership_id, assignment.event_id)
        if key in seen_membership_event:
            # Impossible state the database prevents; modelling around it
            # would mean silently accepting a schedule that cannot exist.
            raise SchedulingInputError(
                f"membership {assignment.membership_id} is assigned more than"
                f" once in event {assignment.event_id}"
            )
        seen_membership_event.add(key)


def _validate_event_gap(scheduling_input: SchedulingInput) -> None:
    """The event-gap rule's own structural assumptions.

    All three are things the builder cannot produce -- the database's own
    ordering and the query that loads the history see to that -- and all three
    would go wrong *quietly* if a hand-written input broke them, enforcing gaps
    between the wrong events rather than failing. That is exactly the class of
    thing this module validates rather than trusts.
    """
    gap = scheduling_input.min_intervening_events
    if gap is not None:
        # bool is an int subclass, and True would silently become a gap of 1.
        if isinstance(gap, bool) or not isinstance(gap, int):
            raise SchedulingInputError(
                "min_intervening_events must be an integer or None,"
                f" got {gap!r}"
            )
        if gap <= 0:
            raise SchedulingInputError(
                "min_intervening_events must be positive; use None for no"
                f" event-gap rule, got {gap}"
            )

    preceding = scheduling_input.preceding_events
    following = scheduling_input.following_events
    adjacent_ids = [event.event_id for event in preceding + following]
    if len(set(adjacent_ids)) != len(adjacent_ids):
        # Covers each side on its own and the two against each other: one event
        # cannot be both the one before this window and the one after it.
        raise SchedulingInputError("adjacent event ids must be unique")

    overlap = set(adjacent_ids) & set(scheduling_input.scheduled_event_ids)
    if overlap:
        # An event cannot be both outside the window and something this run
        # schedules: it would occupy two positions in the sequence, and the
        # second would silently shift every gap after it.
        raise SchedulingInputError(
            "an event cannot be both adjacent to this run and scheduled in it:"
            f" {sorted(overlap)}"
        )

    if not scheduling_input.requirements:
        return
    boundaries = [
        (requirement.event_date, requirement.event_id)
        for requirement in scheduling_input.requirements
    ]
    first, last = min(boundaries), max(boundaries)

    if preceding:
        latest = max(event.sort_key for event in preceding)
        if latest >= first:
            raise SchedulingInputError(
                "every preceding event must fall before the first scheduled"
                f" event; {latest} is not before {first}"
            )
    if following:
        earliest = min(event.sort_key for event in following)
        if earliest <= last:
            raise SchedulingInputError(
                "every following event must fall after the last scheduled"
                f" event; {earliest} is not after {last}"
            )


def _validate_member_group_caps(scheduling_input: SchedulingInput) -> None:
    """The member-group rule's own structural assumptions.

    Both are things the builder cannot produce -- the database's uniqueness and
    its ``max_per_event > 0`` CHECK see to that -- and both would go wrong
    *quietly*: two caps for one group would silently apply whichever is
    stricter without anyone having configured it, and a cap of zero or less
    would forbid a group entirely under the guise of a limit. That is exactly
    the class of thing this module validates rather than trusts.

    A cap naming memberships that are not candidates is deliberately **not** an
    error, for the same reason an inert linked pair is not: a member
    deactivated part-way through a period stops being a candidate while their
    group membership stays recorded, and somebody who can never be present can
    never consume a cap.
    """
    seen: set[int] = set()
    for cap in scheduling_input.member_group_caps:
        if cap.member_group_id in seen:
            raise SchedulingInputError(
                "the same member group is capped more than once:"
                f" {cap.member_group_id}"
            )
        seen.add(cap.member_group_id)
        maximum = cap.max_per_event
        # bool is an int subclass, and True would silently become a cap of 1.
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise SchedulingInputError(
                f"member group {cap.member_group_id} has a non-integer"
                f" max_per_event ({maximum!r})"
            )
        if maximum <= 0:
            # Zero is not a cap, it is "no member of this group may serve",
            # which withholding a qualification or deactivating a membership
            # already says. Accepting it here would give that meaning a second,
            # quieter spelling.
            raise SchedulingInputError(
                f"member group {cap.member_group_id} has a non-positive"
                f" max_per_event ({maximum}); omit the cap entirely for no"
                " limit"
            )


def _validate_support_requirements(scheduling_input: SchedulingInput) -> None:
    """The same-event support rule's own structural assumptions.

    ``SameEventSupportRequirement`` already refuses a self-supporting set on
    construction, so what is left here is duplication and the count itself: two
    requirements for one subject would be two answers to a question the
    database gives one answer to, and a non-positive count would be "no
    requirement" wearing the clothes of one.

    A supporter set too small to reach the count is deliberately **not** an
    error -- it is a configuration a head can see and fix, and the engine
    reports it as an unfilled position with ``ALL_WITHOUT_EVENT_SUPPORT``
    rather than refusing to run at all.
    """
    seen: set[int] = set()
    for requirement in scheduling_input.support_requirements:
        subject_id = requirement.subject_membership_id
        if subject_id in seen:
            raise SchedulingInputError(
                "the same membership has more than one support requirement:"
                f" {subject_id}"
            )
        seen.add(subject_id)
        needed = requirement.min_supporters
        if isinstance(needed, bool) or not isinstance(needed, int):
            raise SchedulingInputError(
                f"support requirement for membership {subject_id} has a"
                f" non-integer min_supporters ({needed!r})"
            )
        if needed <= 0:
            raise SchedulingInputError(
                f"support requirement for membership {subject_id} has a"
                f" non-positive min_supporters ({needed}); omit the requirement"
                " entirely for no rule"
            )


def _validate_policy(policy: SchedulingPolicy) -> None:
    """A target of zero is meaningful -- "ideally nobody serves twice" -- and
    still cannot reduce fill, because it is optimized only after fill is
    fixed. A negative target is not a preference anyone could hold.
    """
    target = policy.target_assignments_per_candidate
    if target is not None and target < 0:
        raise SchedulingInputError(
            "target_assignments_per_candidate must not be negative,"
            f" got {target}"
        )
    for role_id in policy.role_variety_role_ids or ():
        # Role ids are database identities in this domain, so a non-positive
        # one is a mistake rather than an unusual preference. A configured
        # role that simply has no requirement this period is fine and is
        # deliberately not checked for.
        if not isinstance(role_id, int) or isinstance(role_id, bool) or role_id <= 0:
            raise SchedulingInputError(
                f"role_variety_role_ids must contain positive role ids, got {role_id!r}"
            )


def _requirement_by_id(
    requirements: tuple[RequirementInput, ...], requirement_id: int
) -> RequirementInput:
    for requirement in requirements:
        if requirement.requirement_id == requirement_id:
            return requirement
    raise SchedulingInputError(f"unknown requirement {requirement_id}")
