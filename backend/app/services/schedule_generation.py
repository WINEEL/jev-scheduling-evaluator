"""Generating a draft schedule: build, solve, and persist the proposals.

The one application operation that turns the scheduling engine's answer into
real rows. It composes work that already exists rather than reimplementing any
of it:

- **Task 30** (:func:`app.services.scheduling_input_builder.build_scheduling_input`)
  extracts the current facts, including its own staleness gate;
- **Tasks 31-33** (:func:`app.scheduling.solver.solve_schedule`) choose the
  placements, on plain values, with no database;
- **Task 63**
  (:func:`app.services.generated_assignment.persist_generated_assignments`)
  writes them.

**[REVIEWED] One write path, and that is load-bearing.** No ``Assignment`` is
constructed here and none is added to the session directly. A solver proposal
is a *proposal*, not permission: by the time it is written, current state is
re-checked in full -- authorization, the version's mutability, ministry
integrity, active membership and person, a cancelled event,
one-position-per-event, the serving maximum, linked-member same-date
exclusions, role activity, qualification, explicit ``UNAVAILABLE``, the
church-wide Sunday conflict, and capacity. The input was built a moment
earlier, but "a moment earlier" is not now, and the database is the only thing
that knows what is true at write time.

**Task 63 changed how those facts are read, and nothing about which rules
apply.** Task 34 wrote each row through
:func:`app.services.assignment.assign_member`, at eleven round trips apiece --
93% of a generation run's wall time. The rules now live in
:mod:`app.services.assignment_rules`, which reads nothing, and the batch
writer supplies them from one prefetch. Manual assignment still applies the
same rules from its own per-row reads. The two writers cannot drift, because
there is only one copy of the rules to drift from.

**Automatic work never overrides anything.** ``override_reason`` is always
``None``. If a blocker appeared between building the input and writing the
row, the generation fails -- it is not retried with an override, the person is
not silently skipped, and no substitute is chosen. A head who still wants that
placement makes it deliberately through Task 22, with a reason they are
willing to put their name to.

**All of it, or none of it.** A solver result is one coherent optimization: it
chose *these* placements because of how they fit together. Persisting half of
it would produce a schedule the optimizer never evaluated and nobody decided
on, so a failure part-way through propagates and the caller's transaction
discards the whole run. Nothing here catches, skips, deletes or compensates.

**Only into a latest DRAFT.** Task 22 allows manual edits to DRAFT *or*
REVIEW, deliberately -- a head making one considered change to a version under
review is visible and intentional. Automatic generation writes many rows at
once, so it stops at DRAFT: rewriting a schedule underneath the people
reviewing it is a different thing entirely.

Not done here, deliberately: no lifecycle change (the version is still DRAFT
afterwards, and moving it to REVIEW stays a separate, human act), no successor
version, no policy of its own, no generation-run audit row or provenance
column, and no locking -- Task 22's revalidation plus the database's own
constraints are the correctness backstop, not a lock this task would have to
invent.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.core import Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    Assignment,
    ScheduleVersion,
)
from app.models.scheduling_input import SchedulingPeriod
from app.scheduling.result import SchedulingResult
from app.scheduling.solver import SchedulingPolicy, solve_schedule
from app.services.authorization import require_ministry_operator
from app.services.errors import InvalidOperationError
from app.services.generated_assignment import persist_generated_assignments
from app.services.scheduling_input_builder import build_scheduling_input

__all__ = ["DraftGenerationResult", "generate_draft_schedule"]


@dataclass(frozen=True, slots=True)
class DraftGenerationResult:
    """What one generation run decided, and what it actually wrote.

    Both halves are needed and neither substitutes for the other:
    ``scheduling_result`` is the engine's own answer, including the positions
    it could not fill and why, while ``created_assignments`` is the persisted
    consequence. Keeping them separate is what lets a caller show a head "we
    filled these four and here is why the fifth is still open".

    A service-layer type, so ORM rows are welcome here -- which is exactly why
    it lives in ``app.services`` and not in the pure scheduling package, whose
    whole value is that it holds no database state.
    """

    scheduling_result: SchedulingResult
    #: Only rows this invocation created. Assignments the version already
    #: carried are inputs to the run, not output from it.
    created_assignments: tuple[Assignment, ...] = ()

    @property
    def created_count(self) -> int:
        return len(self.created_assignments)

    @property
    def is_complete(self) -> bool:
        """Whether the schedule is now fully staffed.

        Read from the solver's answer, which accounts for existing assignments
        as well as new ones -- ``created_count`` alone could not tell you.
        """
        return self.scheduling_result.is_complete


def generate_draft_schedule(
    session: Session,
    *,
    actor: Person,
    version: ScheduleVersion,
    policy: SchedulingPolicy,
) -> DraftGenerationResult:
    """Generate and persist assignments for ``version``, as ``actor``.

    **Sequence, and why it is ordered this way:**

    1. Check the version carries the persisted context this needs, resolve its
       period, and authorize -- **before** any expensive work. Solving a
       schedule for someone who may not ask is wasted effort at best.
    2. Require a latest DRAFT.
    3. Build the current input (Task 30), which applies its own staleness gate
       and refuses a version whose snapshot has drifted.
    4. Solve (Tasks 31-33) with exactly the policy supplied -- this service
       adds nothing to it and reads nothing from it.
    5. Write every proposal through the batch writer (Task 63), in the
       solver's own deterministic order, re-validated against current facts
       one placement at a time.

    **An incomplete schedule is a successful outcome, not a failure.** If the
    engine could staff four of five positions, the four are written and the
    fifth comes back in ``unfilled_requirements`` with its diagnostics. Raising
    would throw away four good placements to complain about one that nobody
    can fill (schedule-output §2).

    **Repeatable without special machinery.** A second run rebuilds the input,
    where the first run's rows now appear as existing assignments -- so an
    already-complete schedule yields zero proposals, zero writes and zero
    audit rows. There is no run-provenance column and none is needed.

    **No flush of its own.** The one flush is the batch writer's, which
    flushes the whole run at once to obtain the identities its audit events
    reference. This function never commits or rolls back; if a placement is
    refused, the exception propagates and the caller's transaction discards
    the entire run -- which, since nothing is flushed until every placement
    has been accepted, now means nothing was written at all.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of the version's Ministry.
    :raises InvalidOperationError: ``version`` lacks persisted context; its
        period cannot be resolved; it is not a latest DRAFT; its snapshot is
        stale (Task 23, via the builder); a proposal cannot be resolved to
        rows of this version and ministry; or a rule refuses a placement on
        current facts.
    """
    _require_version_context(version)
    period = _resolve_scheduling_period(session, version)
    require_ministry_operator(actor, ministry_id=period.ministry_id)
    _require_latest_draft(session, version)

    scheduling_input = build_scheduling_input(session, version=version)
    scheduling_result = solve_schedule(scheduling_input, policy=policy)

    created = _persist_proposals(
        session,
        actor=actor,
        version=version,
        ministry_id=period.ministry_id,
        scheduling_result=scheduling_result,
    )

    return DraftGenerationResult(
        scheduling_result=scheduling_result, created_assignments=created,
    )


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def _require_version_context(version: ScheduleVersion) -> None:
    """The same explicit guard the builder applies, checked here too because
    authorization happens before the builder is ever called -- a transient
    object must be refused as such, not become a query against ``NULL``.
    """
    for attribute in ("id", "schedule_id", "scheduling_period_id", "version_number"):
        if getattr(version, attribute) is None:
            raise InvalidOperationError(
                f"version must be persisted: {attribute} is None"
            )


def _require_latest_draft(session: Session, version: ScheduleVersion) -> None:
    """DRAFT only, and still the latest version of its schedule.

    Stricter than Task 22's own mutability rule, on purpose: a manual edit to
    a REVIEW version is one considered change a head can see, while generation
    rewrites many rows at once and should not happen underneath the people
    reviewing it.
    """
    if version.status != SCHEDULE_VERSION_STATUS_DRAFT:
        raise InvalidOperationError(
            "a schedule can only be generated into a DRAFT version, and this"
            f" one is {version.status}"
        )
    if _newer_version_exists(
        session, schedule_id=version.schedule_id, version_number=version.version_number
    ):
        raise InvalidOperationError(
            "cannot generate into a schedule version that has been superseded"
            " by a newer version"
        )


# --------------------------------------------------------------------------
# Persisting the proposals
# --------------------------------------------------------------------------


def _persist_proposals(
    session: Session,
    *,
    actor: Person,
    version: ScheduleVersion,
    ministry_id: int,
    scheduling_result: SchedulingResult,
) -> tuple[Assignment, ...]:
    """Hand the whole result to the batch writer, in proposal order.

    One call, not one per proposal. What that buys and what it deliberately
    does not change is :mod:`app.services.generated_assignment`'s own subject;
    from here the contract is unchanged -- every proposal is validated against
    current facts before it becomes a row, nothing is overridden, nothing is
    skipped, and a refusal propagates untouched so the caller's transaction
    discards the run.
    """
    return persist_generated_assignments(
        session,
        actor=actor,
        version=version,
        ministry_id=ministry_id,
        proposals=scheduling_result.proposed_assignments,
    )


# --------------------------------------------------------------------------
# Queries -- each split from its execution so the SQL is testable with no
# database (see tests/test_services_schedule_generation.py)
# --------------------------------------------------------------------------


def _scheduling_period_statement(
    scheduling_period_id: int,
) -> Select[tuple[SchedulingPeriod]]:
    return select(SchedulingPeriod).where(SchedulingPeriod.id == scheduling_period_id)


def _resolve_scheduling_period(
    session: Session, version: ScheduleVersion
) -> SchedulingPeriod:
    """``ScheduleVersion`` carries no ``ministry_id``, so this one query is
    what makes authorization possible before any solver work begins.
    """
    period = session.execute(
        _scheduling_period_statement(version.scheduling_period_id)
    ).scalar_one_or_none()
    if period is None:
        raise InvalidOperationError(
            "version's scheduling period could not be resolved"
        )
    return period


def _newer_version_exists_statement(schedule_id: int, version_number: int) -> Select[tuple[int]]:
    """The same ``LIMIT 1`` existence probe every other lifecycle rule uses.
    "Latest" is a fact about the schedule, living on other rows, so it is
    queried fresh rather than read off a relationship collection.
    """
    return (
        select(ScheduleVersion.id)
        .where(
            ScheduleVersion.schedule_id == schedule_id,
            ScheduleVersion.version_number > version_number,
        )
        .limit(1)
    )


def _newer_version_exists(session: Session, *, schedule_id: int, version_number: int) -> bool:
    stmt = _newer_version_exists_statement(schedule_id, version_number)
    return session.execute(stmt).scalar_one_or_none() is not None
