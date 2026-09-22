"""Generating several ministries' drafts from **one** joined solve (Task 81).

**Development only, and not wired to anything.** No API route imports this, no
CLI the product installs calls it, and no navigation item leads to it.
``tests/test_services_joined_schedule_generation.py`` asserts the first of
those directly, because "development only" is worth nothing as a comment and
something as a test.

**What it is.** :func:`app.services.schedule_generation.generate_draft_schedule`
with the middle step replaced: instead of one input and
:func:`~app.scheduling.solver.solve_schedule`, it builds N inputs and calls
:func:`~app.scheduling.joined.solve_joined_schedule`, which enforces the
church-wide one-ministry-per-person-per-date rule *inside* the CP-SAT model
rather than reducing the other ministries to frozen ``blocked_dates``. Every
other step is the existing one, called unchanged:

- Task 30's builder extracts each version's facts, staleness gate included;
- Task 81's joined engine chooses the placements, on plain values, no database;
- Task 63's batch writer persists them, re-validating every placement against
  current facts.

**The gates are imported, not restated.** Version context, period resolution
and the latest-DRAFT rule come from
:mod:`app.services.schedule_generation` itself. A second copy of a lifecycle
rule is a second copy to drift from, and the rules this composes are the whole
reason it is safe to compose.

**Atomicity, stated honestly.** Nothing here commits, flushes independently,
rolls back, catches or compensates. Every ministry is written into the
caller's single transaction, in ascending ministry id, and the first refusal
propagates -- so the caller's transaction discards every ministry's rows, not
just the failing one. That is genuine all-or-nothing **for this process's
transaction**, and it is exactly as much as this can claim.

**What it does *not* give you, and must not be read as giving you.** The
church-wide rule holds across the N versions this run writes because the
*solve* respected it, not because the database refused anything. At write time
:mod:`app.services.sunday_conflict` consults the authoritative **FINALIZED**
version of each other ministry (ADR 0003), so two DRAFT versions created by
one joined run are invisible to each other's checks by design. That is
unchanged from today and no weaker than today -- it is the same two-tier
guarantee every pre-finalization assignment already has, with the finalization
readiness gate (Task 49) as the backstop that refuses to finalize a version
contradicting an authoritative one. A concurrent writer can still create a
conflict between two drafts, exactly as it can now.

**Lifecycle is untouched.** Each version is still a latest DRAFT before and
after; nothing is promoted to REVIEW, nothing is finalized, no successor
version is created, and no Task 80 rule is relaxed for a joined run. Promoting
a joined draft is the same deliberate human act it is for any other draft, and
it goes through the same gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from sqlalchemy.orm import Session

from app.models.core import Person
from app.models.schedule_output import Assignment, ScheduleVersion
from app.scheduling.joined import (
    JoinedMinistryInput,
    JoinedSchedulingResult,
    solve_joined_schedule,
)
from app.scheduling.solver import SchedulingPolicy
from app.services.authorization import require_ministry_operator
from app.services.errors import InvalidOperationError
from app.services.generated_assignment import persist_generated_assignments
from app.services.schedule_generation import (
    _require_latest_draft,
    _require_version_context,
    _resolve_scheduling_period,
)
from app.services.scheduling_input_builder import build_scheduling_input

__all__ = [
    "JoinedGenerationResult",
    "JoinedMinistryRequest",
    "generate_joined_draft_schedules",
]


@dataclass(frozen=True, slots=True)
class JoinedMinistryRequest:
    """One ministry's part of a joined generation: which DRAFT, and what
    preferences to solve it with.

    The same pair a single-ministry generation takes, because a joined run
    changes neither half: the version says what is being scheduled, the policy
    says what this run prefers, and no policy is invented for a ministry that
    has not stated one.
    """

    version: ScheduleVersion
    policy: SchedulingPolicy


@dataclass(frozen=True, slots=True)
class JoinedGenerationResult:
    """What one joined run decided, and what it wrote, per ministry.

    Both halves, for the same reason
    :class:`~app.services.schedule_generation.DraftGenerationResult` keeps
    both: the engine's answer includes the positions it could not fill and why,
    and the persisted rows do not.
    """

    joined_result: JoinedSchedulingResult
    #: ``ministry_id -> the rows this invocation created``, in proposal order.
    #: Assignments a version already carried are inputs to the run, not output.
    created_by_ministry: Mapping[int, tuple[Assignment, ...]] = MappingProxyType({})

    @property
    def created_count(self) -> int:
        return sum(len(rows) for rows in self.created_by_ministry.values())

    @property
    def is_complete(self) -> bool:
        """Whether every ministry in the run is now fully staffed."""
        return self.joined_result.is_complete


def generate_joined_draft_schedules(
    session: Session,
    *,
    actor: Person,
    requests: Sequence[JoinedMinistryRequest],
) -> JoinedGenerationResult:
    """Solve several ministries together and persist each one's proposals.

    **Sequence, and why it is ordered this way:**

    1. Gate and authorize **every** ministry first, before any solving. A
       joined run that turned out to be unauthorized half way through would
       have spent the expensive work for nothing, and -- worse -- would have
       written some ministries' rows before discovering it.
    2. Build every version's input (Task 30), each with its own staleness gate.
    3. Solve all of them at once (Task 81), under the church-wide rule.
    4. Write each ministry's proposals through the batch writer (Task 63), in
       ascending ministry id, every placement re-validated against current
       facts.

    **An incomplete schedule is a successful outcome, not a failure**, exactly
    as it is for a single ministry. A position no one may fill -- including one
    left open *because* the church-wide rule bound -- comes back in that
    ministry's ``unfilled_requirements`` with its diagnostics, and the
    placements that were possible are still written.

    :raises AuthorizationError: the actor may not operate one of the
        ministries.
    :raises InvalidOperationError: no request was supplied; one version appears
        twice; a version lacks persisted context, cannot have its period
        resolved, or is not a latest DRAFT; a snapshot is stale; or a placement
        is refused on current facts.
    :raises SchedulingInputError: the joined input is structurally invalid --
        including existing assignments that already place one person in two of
        these ministries on one date.
    :raises SchedulingEngineError: CP-SAT returned neither ``OPTIMAL`` nor
        ``FEASIBLE``.
    """
    entries = tuple(requests)
    if not entries:
        raise InvalidOperationError(
            "a joined generation needs at least one ministry"
        )

    version_ids = [request.version.id for request in entries]
    if len(set(version_ids)) != len(version_ids):
        raise InvalidOperationError(
            "a joined generation must name each schedule version once"
        )

    # Gate everything before solving anything. `_resolve_scheduling_period` is
    # also what supplies the ministry id: ScheduleVersion does not carry one.
    ministry_by_version_id: dict[int, int] = {}
    for request in entries:
        _require_version_context(request.version)
        period = _resolve_scheduling_period(session, request.version)
        require_ministry_operator(actor, ministry_id=period.ministry_id)
        _require_latest_draft(session, request.version)
        ministry_by_version_id[request.version.id] = period.ministry_id

    joined_input = tuple(
        JoinedMinistryInput(
            scheduling_input=build_scheduling_input(
                session, version=request.version
            ),
            policy=request.policy,
        )
        for request in entries
    )
    joined_result = solve_joined_schedule(joined_input)

    # Written in ascending ministry id -- the order the solve itself used -- so
    # that a refusal is reproducible rather than depending on how the caller
    # happened to list the ministries.
    version_by_ministry = {
        ministry_by_version_id[request.version.id]: request.version
        for request in entries
    }
    created: dict[int, tuple[Assignment, ...]] = {}
    for ministry_id in sorted(joined_result.results_by_ministry):
        result = joined_result.results_by_ministry[ministry_id]
        created[ministry_id] = persist_generated_assignments(
            session,
            actor=actor,
            version=version_by_ministry[ministry_id],
            ministry_id=ministry_id,
            proposals=result.proposed_assignments,
        )

    return JoinedGenerationResult(
        joined_result=joined_result,
        created_by_ministry=MappingProxyType(dict(sorted(created.items()))),
    )
