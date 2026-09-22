"""Joined multi-ministry scheduling: several ministries, one CP-SAT solve.

**Development trial (Task 81).** Nothing here is reachable from an API route,
a CLI the product installs, or any production path. It exists to answer one
architectural question: can the existing generic engine schedule several
ministries *together*, with the church-wide rule enforced inside the model,
rather than one at a time with the others frozen into ``blocked_dates``?

**The one church-wide rule, stated exactly.** For every canonical
:class:`~app.models.core.Person` ``p`` and every church-local calendar date
``d``::

    sum over ministries m in this run of present(p, m, d)  <=  1

where ``present(p, m, d)`` is 1 when ``p`` holds **any** assignment -- new or
already fixed -- at **any** event of ministry ``m`` on date ``d``. One
*ministry* per person per date, never one *assignment row*: two events of the
same ministry on one Sunday remain that ministry's own business, decided by
its own rules, exactly as they are in a single-ministry run.

It is added as a **constraint**, before any objective is expressed, which is
what makes it non-negotiable. No pass below it can buy a filled position by
breaking it, there is no weight at which it becomes tradeable, no policy flag
reaches it, and no override exists. If demand cannot be met without breaking
it, the positions come back **unfilled with a diagnostic** -- that is the
honest answer, and it is the only answer this module can give.

**Identity is the Person, never the name.** A person serving two ministries
has two :class:`~app.models.core.MinistryMembership` rows and therefore two
``membership_id``s, and both appear in this model as separate candidates with
separate loads. What ties them together is
:attr:`~app.scheduling.input.CandidateInput.person_id`, and that is the only
key this module groups by. Display names are never read here; the engine
never sees one it could group by even if it wanted to.

**No ministry priority, because none is approved.** The church has not ranked
its ministries, so this module does not rank them. The two church-wide passes
count positions and BACKUP draws -- quantities that mean the same thing in
every ministry -- so an open position is worth exactly as much wherever it is.
Where the rule genuinely forces a choice between two ministries for one person
on one date, the tie is broken by CP-SAT's deterministic search and the fact
is **reported** as a :class:`ChurchWideContention` rather than settled by a
rule nobody agreed to. That report is the deliverable: it is how an unresolved
governance question stays visible instead of being silently decided by
whichever ministry the loop happened to reach first.

**Each ministry keeps its own everything else.** Roles, qualifications,
staffing requirements, availability, serving limits, event-gap rules,
member-group caps, support requirements, existing assignments and policy are
read from that ministry's own
:class:`~app.scheduling.input.SchedulingInput` and
:class:`~app.scheduling.solver.SchedulingPolicy`, through the very same
:func:`~app.scheduling.solver._build_ministry_block` a single-ministry run
uses. There is one copy of those rules, so a joined run cannot come to
disagree with a separate run about what any of them means.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from ortools.sat.python import cp_model

from app.scheduling.input import SchedulingInput
from app.scheduling.result import (
    DIAGNOSTIC_ALL_JOINED_MINISTRY_CONFLICT,
    SchedulingResult,
    UnfilledRequirement,
)
from app.scheduling.solver import (
    SchedulingInputError,
    SchedulingPolicy,
    _build_ministry_block,
    _extract_result,
    _new_solver,
    _run_objective_passes,
    _validate,
    _validate_policy,
)

__all__ = [
    "ChurchWideContention",
    "JoinedMinistryInput",
    "JoinedSchedulingResult",
    "solve_joined_schedule",
]


@dataclass(frozen=True, slots=True)
class JoinedMinistryInput:
    """One ministry's part of a joined run: its input and its own policy.

    A pair rather than a policy field on ``SchedulingInput`` because that is
    already how a single-ministry run is called -- the input reports facts, the
    policy states this run's preferences -- and a joined run changes neither
    half.
    """

    scheduling_input: SchedulingInput
    policy: SchedulingPolicy

    @property
    def ministry_id(self) -> int:
        return self.scheduling_input.ministry_id


@dataclass(frozen=True, slots=True)
class ChurchWideContention:
    """One place the church-wide rule actually had a choice to make.

    Emitted for each (person, date) where **more than one** ministry in this
    run could have used that person -- because it holds a placement variable
    for them that date, or already holds a fixed assignment. That is precisely
    the set of decisions no approved rule governs.

    Not an error and not a warning: a contention simply says "this person was
    wanted in two places on this date, one of them got them, and which one was
    not decided by any church policy". Reported so the question stays visible.
    """

    person_id: int
    event_date: datetime.date
    #: The ministry that ended up holding this person on this date, or ``None``
    #: when the rule was satisfied by them serving nowhere at all.
    scheduled_ministry_id: int | None
    #: The other ministries of this run that could have used them that date and
    #: did not, in ascending id order.
    contending_ministry_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class JoinedSchedulingResult:
    """What one joined run decided, split back out per ministry.

    **Per-ministry results, not a merged one.** Each ministry gets exactly the
    :class:`~app.scheduling.result.SchedulingResult` it would get from its own
    run -- same proposals shape, same unfilled rows, same metrics -- because
    everything downstream (persistence, review, display) is ministry-scoped and
    a joined blob would have to be taken apart again by every caller.
    """

    results_by_ministry: Mapping[int, SchedulingResult] = field(
        default_factory=lambda: MappingProxyType({})
    )
    #: Every (person, date) the church-wide rule had to arbitrate, in
    #: ``(person_id, event_date)`` order.
    contentions: tuple[ChurchWideContention, ...] = ()

    @property
    def ministry_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.results_by_ministry))

    @property
    def is_complete(self) -> bool:
        """Every required position of every ministry is filled."""
        return all(
            result.is_complete for result in self.results_by_ministry.values()
        )

    @property
    def filled_count(self) -> int:
        return sum(
            result.filled_count for result in self.results_by_ministry.values()
        )

    @property
    def unfilled_count(self) -> int:
        return sum(
            result.unfilled_count for result in self.results_by_ministry.values()
        )


def solve_joined_schedule(
    ministries: Sequence[JoinedMinistryInput],
) -> JoinedSchedulingResult:
    """Solve several ministries at once, under one church-wide person/date rule.

    Sequence:

    1. validate each ministry's input exactly as a single-ministry run does,
       then the joined-only invariants (§:func:`_validate_joined`);
    2. build one CP-SAT block per ministry into **one** model, in ascending
       ministry id, so the model -- and therefore the search -- is identical
       for the same inputs whatever order the caller passed them in;
    3. add the church-wide constraint across those blocks;
    4. run the shared lexicographic passes;
    5. read each ministry's answer back out, and record every contention.

    :raises SchedulingInputError: any ministry's input is structurally invalid,
        no ministry was supplied, a ministry appears twice, a membership
        appears in two ministries, a membership maps to two people, a
        requirement names a ministry other than its input's, or existing
        assignments already place one person in two of these ministries on one
        date.
    :raises SchedulingEngineError: CP-SAT returned neither ``OPTIMAL`` nor
        ``FEASIBLE``.
    """
    ordered = _validate_joined(ministries)

    model = cp_model.CpModel()
    blocks = tuple(
        _build_ministry_block(
            model, scheduling_input=entry.scheduling_input, policy=entry.policy
        )
        for entry in ordered
    )

    possibility = _person_date_possibilities(blocks)
    _add_church_wide_person_date_limit(model, blocks=blocks, possibility=possibility)

    solver = _new_solver()
    _run_objective_passes(solver, model, blocks=blocks)

    results = {
        block.scheduling_input.ministry_id: _extract_result(solver, block)
        for block in blocks
    }

    presence = _final_person_date_presence(blocks, results)
    results = _augment_joined_diagnostics(
        blocks, results=results, presence=presence
    )

    return JoinedSchedulingResult(
        results_by_ministry=MappingProxyType(dict(sorted(results.items()))),
        contentions=_contentions(possibility=possibility, presence=presence),
    )


# --------------------------------------------------------------------------
# The church-wide constraint
# --------------------------------------------------------------------------


def _person_date_possibilities(
    blocks: tuple,
) -> dict[tuple[int, datetime.date], dict[int, "_Possibility"]]:
    """``(person_id, date) -> {ministry_id: what that ministry could do}``.

    Keyed by ``person_id``, which is the whole point: a person's two
    memberships collapse to one key here, and that is what makes the rule below
    a statement about a human being rather than about a row.

    Both halves of "could serve" are collected -- the placements still to be
    decided, and the assignments already fixed -- because the rule does not
    distinguish them. A person already committed to one ministry on a date is
    as unavailable to the others as one this run is about to commit.
    """
    found: dict[tuple[int, datetime.date], dict[int, _Possibility]] = defaultdict(dict)

    for block in blocks:
        ministry_id = block.scheduling_input.ministry_id
        person_of = {
            candidate.membership_id: candidate.person_id
            for candidate in block.scheduling_input.candidates
        }

        for (requirement_id, membership_id), variable in block.variables.items():
            event_date = block.requirements_by_id[requirement_id].event_date
            key = (person_of[membership_id], event_date)
            entry = found[key].get(ministry_id)
            if entry is None:
                entry = _Possibility(ministry_id=ministry_id)
                found[key][ministry_id] = entry
            entry.variables.append(variable)

        for assignment in block.scheduling_input.existing_assignments:
            event_date = block.requirements_by_id[
                assignment.requirement_id
            ].event_date
            key = (person_of[assignment.membership_id], event_date)
            entry = found[key].get(ministry_id)
            if entry is None:
                entry = _Possibility(ministry_id=ministry_id)
                found[key][ministry_id] = entry
            entry.is_fixed = True

    return found


@dataclass(slots=True)
class _Possibility:
    """What one ministry could do with one person on one date.

    Mutable and private, unlike everything else in this package: it is a
    scratch index built and thrown away inside a single call, never part of an
    input or a result.
    """

    ministry_id: int
    variables: list = field(default_factory=list)
    #: An assignment of this ministry already commits the person that date.
    is_fixed: bool = False


def _add_church_wide_person_date_limit(
    model,
    *,
    blocks: tuple,
    possibility: dict[tuple[int, datetime.date], dict[int, _Possibility]],
) -> None:
    """``sum over ministries of present(person, ministry, date) <= 1``.

    Added **only** where two or more ministries of this run could actually use
    the person that date. Everywhere else the sum has at most one term and the
    constraint would say nothing -- writing it anyway would grow the model
    without changing a single answer.

    *Present* is a maximum, never a sum: two placements of one ministry on one
    date make that ministry present once, which is exactly the approved rule.
    Summing raw placement variables instead would forbid a ministry's own
    second event on a Sunday, a rule nobody agreed to.

    A ministry that already holds the person that date contributes a constant
    1, so the others are driven to zero without a variable being invented for a
    fact that is not in question.
    """
    for key in sorted(possibility, key=lambda k: (k[0], k[1])):
        entries = possibility[key]
        if len(entries) < 2:
            continue
        person_id, event_date = key
        terms = []
        for ministry_id in sorted(entries):
            entry = entries[ministry_id]
            if entry.is_fixed:
                terms.append(1)
                continue
            if len(entry.variables) == 1:
                terms.append(entry.variables[0])
                continue
            presence = model.NewBoolVar(
                f"church_p{person_id}_m{ministry_id}_{event_date.isoformat()}"
            )
            model.AddMaxEquality(presence, entry.variables)
            terms.append(presence)
        model.Add(sum(terms) <= 1)


# --------------------------------------------------------------------------
# Reading the church-wide outcome back out
# --------------------------------------------------------------------------


def _final_person_date_presence(
    blocks: tuple, results: dict[int, SchedulingResult]
) -> dict[tuple[int, datetime.date], int]:
    """``(person_id, date) -> the ministry that ended up holding them``.

    Built from the finished schedule -- existing rows plus this run's
    proposals -- because that is what a contention report and a joined
    diagnostic have to describe. The church-wide constraint guarantees at most
    one ministry per key, so a plain value is enough and a collision would be
    a bug rather than a state to represent.
    """
    presence: dict[tuple[int, datetime.date], int] = {}
    for block in blocks:
        ministry_id = block.scheduling_input.ministry_id
        person_of = {
            candidate.membership_id: candidate.person_id
            for candidate in block.scheduling_input.candidates
        }
        for assignment in block.scheduling_input.existing_assignments:
            event_date = block.requirements_by_id[
                assignment.requirement_id
            ].event_date
            presence[(person_of[assignment.membership_id], event_date)] = ministry_id
        for proposal in results[ministry_id].proposed_assignments:
            event_date = block.requirements_by_id[
                proposal.requirement_id
            ].event_date
            presence[(person_of[proposal.membership_id], event_date)] = ministry_id
    return presence


def _contentions(
    *,
    possibility: dict[tuple[int, datetime.date], dict[int, _Possibility]],
    presence: dict[tuple[int, datetime.date], int],
) -> tuple[ChurchWideContention, ...]:
    """Every (person, date) two or more ministries could have used.

    Reported whether or not the person was finally scheduled at all: "two
    ministries wanted them and neither got them" is as much an unarbitrated
    situation as "two wanted them and one got them".
    """
    rows = []
    for key in sorted(possibility, key=lambda k: (k[0], k[1])):
        entries = possibility[key]
        if len(entries) < 2:
            continue
        person_id, event_date = key
        scheduled = presence.get(key)
        rows.append(
            ChurchWideContention(
                person_id=person_id,
                event_date=event_date,
                scheduled_ministry_id=scheduled,
                contending_ministry_ids=tuple(
                    ministry_id
                    for ministry_id in sorted(entries)
                    if ministry_id != scheduled
                ),
            )
        )
    return tuple(rows)


def _augment_joined_diagnostics(
    blocks: tuple,
    *,
    results: dict[int, SchedulingResult],
    presence: dict[tuple[int, datetime.date], int],
) -> dict[int, SchedulingResult]:
    """Add ``ALL_JOINED_MINISTRY_CONFLICT`` where the church-wide rule is why.

    The per-ministry diagnostics come from the single-ministry engine, which
    correctly knows nothing about the other ministries in this run -- so a
    position left open because every candidate for it is serving elsewhere that
    date would otherwise be reported as plain contention, sending a head to
    look for an availability problem that does not exist.

    The code is added only when it is **exactly** true: the requirement had at
    least one ordinarily eligible candidate, and every one of them ended the
    run serving a *different* ministry of this run on that date. One eligible
    candidate idle on the date and the code is not added, because then the rule
    is not what left the position open.

    Appended, never substituted: the reasons the engine already gave are still
    true and are still first.
    """
    augmented: dict[int, SchedulingResult] = {}
    for block in blocks:
        ministry_id = block.scheduling_input.ministry_id
        result = results[ministry_id]
        person_of = {
            candidate.membership_id: candidate.person_id
            for candidate in block.scheduling_input.candidates
        }
        rewritten = []
        changed = False
        for unfilled in result.unfilled_requirements:
            requirement = block.requirements_by_id[unfilled.requirement_id]
            eligible = block.eligible[unfilled.requirement_id]
            if eligible and all(
                presence.get((person_of[membership_id], requirement.event_date))
                not in (None, ministry_id)
                for membership_id in eligible
            ):
                changed = True
                rewritten.append(
                    UnfilledRequirement(
                        requirement_id=unfilled.requirement_id,
                        missing_count=unfilled.missing_count,
                        diagnostic_codes=(
                            *unfilled.diagnostic_codes,
                            DIAGNOSTIC_ALL_JOINED_MINISTRY_CONFLICT,
                        ),
                    )
                )
            else:
                rewritten.append(unfilled)
        augmented[ministry_id] = (
            SchedulingResult(
                proposed_assignments=result.proposed_assignments,
                unfilled_requirements=tuple(rewritten),
                metrics=result.metrics,
            )
            if changed
            else result
        )
    return augmented


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _validate_joined(
    ministries: Sequence[JoinedMinistryInput],
) -> tuple[JoinedMinistryInput, ...]:
    """Every single-ministry check, plus the ones only a joined run can make.

    Returns the entries in ascending ministry id. Order matters: it is what
    fixes the order variables are created in, and therefore what makes two runs
    over the same state produce the same model and the same schedule whatever
    order the caller happened to pass.
    """
    entries = tuple(ministries)
    if not entries:
        raise SchedulingInputError("a joined run needs at least one ministry")

    for entry in entries:
        _validate(entry.scheduling_input)
        _validate_policy(entry.policy)

    ministry_ids = [entry.ministry_id for entry in entries]
    if len(set(ministry_ids)) != len(ministry_ids):
        raise SchedulingInputError(
            "a joined run must name each ministry once; one ministry appears"
            " more than once"
        )

    # One membership belongs to one ministry, and one membership belongs to one
    # person. Both are database facts; checked here because this package works
    # without a database, and a joined run that got either wrong would group
    # the wrong rows under one human and enforce the church-wide rule against
    # a person who does not exist.
    owner_of_membership: dict[int, int] = {}
    person_of_membership: dict[int, int] = {}
    for entry in entries:
        scheduling_input = entry.scheduling_input
        for requirement in scheduling_input.requirements:
            if requirement.ministry_id != scheduling_input.ministry_id:
                raise SchedulingInputError(
                    f"requirement {requirement.requirement_id} names ministry"
                    f" {requirement.ministry_id} but was supplied as part of"
                    f" ministry {scheduling_input.ministry_id}"
                )
        for candidate in scheduling_input.candidates:
            seen = owner_of_membership.get(candidate.membership_id)
            if seen is not None and seen != scheduling_input.ministry_id:
                raise SchedulingInputError(
                    f"membership {candidate.membership_id} appears in both"
                    f" ministry {seen} and ministry"
                    f" {scheduling_input.ministry_id}; one membership belongs"
                    " to one ministry"
                )
            owner_of_membership[candidate.membership_id] = scheduling_input.ministry_id

            known_person = person_of_membership.get(candidate.membership_id)
            if known_person is not None and known_person != candidate.person_id:
                raise SchedulingInputError(
                    f"membership {candidate.membership_id} is mapped to person"
                    f" {known_person} and to person {candidate.person_id}"
                )
            person_of_membership[candidate.membership_id] = candidate.person_id

    _validate_existing_church_wide(entries)
    return tuple(sorted(entries, key=lambda e: e.ministry_id))


def _validate_existing_church_wide(entries: tuple[JoinedMinistryInput, ...]) -> None:
    """Refuse input that already breaks the rule this run must enforce.

    Two *existing* assignments placing one person in two of these ministries on
    one date cannot be solved around: the engine never moves or deletes a fixed
    assignment, so the model would simply be infeasible and CP-SAT would report
    that as an engine failure. Naming it here instead says what is actually
    wrong, and says it before any solving work is done.

    Only assignments inside **this run** are checked. A commitment to a
    ministry outside it reaches the model as ``blocked_dates`` on the candidate
    and is already handled by not creating the variable.
    """
    holder: dict[tuple[int, datetime.date], int] = {}
    for entry in sorted(entries, key=lambda e: e.ministry_id):
        scheduling_input = entry.scheduling_input
        person_of = {
            candidate.membership_id: candidate.person_id
            for candidate in scheduling_input.candidates
        }
        requirements_by_id = {
            requirement.requirement_id: requirement
            for requirement in scheduling_input.requirements
        }
        for assignment in scheduling_input.existing_assignments:
            event_date = requirements_by_id[assignment.requirement_id].event_date
            key = (person_of[assignment.membership_id], event_date)
            previous = holder.get(key)
            if previous is not None and previous != scheduling_input.ministry_id:
                raise SchedulingInputError(
                    f"person {key[0]} already holds assignments in ministry"
                    f" {previous} and ministry {scheduling_input.ministry_id}"
                    f" on {event_date.isoformat()}; one person serves at most"
                    " one ministry per date, and a joined run cannot remove an"
                    " existing assignment to make that true"
                )
            holder[key] = scheduling_input.ministry_id
