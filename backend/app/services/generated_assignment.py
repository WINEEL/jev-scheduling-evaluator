"""Persisting a whole solver result at once (Task 63).

The second writer of ``Assignment`` rows, and the only one that writes many.
:func:`app.services.assignment.assign_member` remains the path for a head's
single, considered change; this is the path for the fifty-odd rows one
generation run produces.

**Why this exists: query count was the whole cost.** Task 34 wrote generated
proposals one at a time through ``assign_member``, on the stated reasoning
that batching the writes would mean reimplementing the validation and audit
that make each one trustworthy. That reasoning was right about the risk and
wrong about the price. Each row cost eleven round trips -- a
newer-version probe, an idempotency lookup, a one-position-per-event probe, a
serving-limit read, a linked-member read, a qualification read, an
availability read, two church-wide conflict queries, a capacity count and the
INSERT -- so a fourteen-Sunday Setup schedule spent **823 queries and 28.7
seconds** persisting a solve that took 1.5 seconds. Against a hosted database,
where a round trip is tens of milliseconds, 93% of generation was latency.

**The risk it was right about is answered by sharing the rules, not by
skipping them.** Every check ``assign_member`` applies is applied here, in the
same order, from one definition in :mod:`app.services.assignment_rules` --
which reads nothing, so the *only* difference between the two writers is how
the facts were gathered. This module is a
:class:`~app.services.assignment_rules.PairFactSource` backed by a prefetch
instead of by eleven queries a row. **No rule was relaxed, removed, made
conditional, or given a fast path**; a solver proposal is still a proposal and
is still refused on current facts.

**Two kinds of fact, and only one of them varies during a run.**

*Static for the run*: qualification, availability, the church-wide conflict,
configured serving maximums, configured same-date exclusions, the period's
event-gap rule together with the ministry event sequence and the events on
either side of it, configured member groups with their per-event caps,
configured same-event support requirements with their approved supporter sets,
role and person and membership activity, event cancellation. Nothing this run
writes can change any of them, so each is read **once**, for every (membership,
role/event/date) pair the run will need.

Two of them are worth stating explicitly, because both read rows that *look*
like they could move under the run. The church-wide conflict query counts only
*authoritative FINALIZED* versions in *other* ministries, and this run writes
DRAFT rows in the target ministry -- it could not see its own output even if it
were re-run per row, which is exactly why Task 34's per-row calls returned the
same answer every time. The surrounding ministry events the gap rule reads are
safe for the same reason applied to this ministry: only authoritative FINALIZED
assignments count there, so a draft already under way for the next quarter
cannot make this run's answer depend on it.

*Changing as the run proceeds*: how full each requirement is, how many
assignments each member holds in this version, which events each member is
already in, which memberships are on each date, and which memberships are at
each event. Those five are seeded from the database and then **advanced in
memory as each row is accepted**, so the fiftieth placement is judged against
the forty-nine before it -- the same thing ``assign_member`` achieved by
re-counting. A run therefore still cannot overfill a requirement, put one person
twice in an event, exceed a serving maximum, seat two linked members on one
date, place one person at two events the gap rule keeps apart, or put more of a
member group on one event than its cap allows, including when the *only* rows
that would cause it are ones this run just created.

**One rule is applied to the run rather than to each row, and it is stated
here.** The same-event support requirement (Task 74) permits a placement
*because of other placements*, so it is the one rule whose answer depends on
where in the run it is asked. Judging it row by row would refuse a perfectly
valid schedule whenever the solver happened to emit the subject before their
supporter. It is therefore collected during the loop and applied once the run's
state is complete -- through
:func:`app.services.assignment_rules.require_same_event_support`, the very
function ``assign_member`` calls per placement, so the rule, its message and its
non-overridability are still defined exactly once. A refusal still raises before
anything is flushed, so a run that would seat somebody without their support
writes nothing at all.

**The version's own writability is the exception, and is read twice.** It is
the one fact whose wrong answer would corrupt rather than inconvenience -- rows
added to a version another request has finalized are rows added to published
history, and nothing downstream would notice. So it is checked at the top, to
fail fast and cheaply, and again in one round trip immediately before the
INSERT (:func:`_require_still_writable`). Everything else defers to the
finalization-readiness gate, which re-evaluates every rule against current
state before a schedule can be published; that gate cannot help here, because
each individual row really is valid.

**Order is preserved exactly.** Proposals are evaluated in the order the
solver emitted them, each fully judged before the next is considered, so a run
that would have been refused at the seventh row is still refused at the
seventh row, with the same message. Nothing is reordered to make batching
easier.

**All of it, or none of it.** The rows are added and flushed together and the
audit rows follow in the same transaction. A refusal raises before anything is
flushed and the caller's transaction discards the run -- Task 34's guarantee,
now with the additional property that a rejected run has written nothing at
all rather than part of a schedule nobody decided on.

**Automatic work never overrides anything.** ``override_reason`` is ``None``
and there is no parameter for it to come from, so
:func:`~app.services.assignment_rules.resolve_override` can only ever return
``False`` here: a blocked placement fails the run. It is not retried with an
override, the person is not silently skipped, and no substitute is chosen.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.core import (
    MinistryMembership,
    MinistryRole,
    Person,
    RoleQualification,
)
from app.models.schedule_output import (
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import AVAILABILITY_UNAVAILABLE, Availability
from app.scheduling.result import ProposedAssignment
from app.services.assignment_rules import (
    EventGapFacts,
    MemberGroupEventFacts,
    OverridableFacts,
    SameEventSupportFacts,
    ServingLimitFacts,
    build_assignment,
    collect_overridable_blockers,
    record_assignment_created,
    require_absolute_rules,
    require_mutable_working_version_status,
    require_same_event_support,
    resolve_override,
)
from app.services.authorization import require_ministry_operator
from app.services.errors import InvalidOperationError
from app.services.event_gap import MinistryEventSequence, load_event_gap_sequence
from app.services.member_group import (
    MemberGroupCapConfig,
    count_group_members_present,
    load_member_group_caps,
)
from app.services.same_date_exclusion import get_same_date_exclusion_pairs
from app.services.same_event_support import (
    SupportRequirementConfig,
    count_supporters_present,
    load_support_requirements,
)
from app.services.serving_limit import get_serving_limits_for
from app.services.sunday_conflict import get_sunday_conflicts_for

__all__ = ["persist_generated_assignments"]


def persist_generated_assignments(
    session: Session,
    *,
    actor: Person,
    version: ScheduleVersion,
    ministry_id: int,
    proposals: Sequence[ProposedAssignment],
) -> tuple[Assignment, ...]:
    """Write every proposal in ``proposals``, as ``actor``, in proposal order.

    ``version`` must already have been established as a mutable working
    version that ``actor`` may write to -- the caller's own lifecycle gate.
    This function re-establishes it anyway, twice: once on entry, for the same
    reason ``assign_member`` does (the caller's gate ran before the solver, and
    "before the solver" is not now), and once more in a single round trip
    immediately before the INSERT, because the prefetch between them takes long
    enough for another request to finalize or supersede the version. See
    :func:`_require_still_writable`.

    :returns: the rows corresponding to ``proposals``, in the same order.
        A proposal that already exists as an assignment yields the existing
        row rather than a second one, matching ``assign_member``'s
        idempotency; the solver emits new placements only, so this is a
        guarantee rather than an expected case.
    :raises AuthorizationError: ``actor`` may not manage a proposal's ministry.
    :raises InvalidOperationError: the version is not (or is no longer) a
        mutable working version; a proposal names a requirement or membership
        that does not belong here; or any rule refuses a placement on current
        facts.
    """
    if not proposals:
        return ()

    # Fail fast, before a prefetch's worth of round trips for a version that
    # cannot be written to at all -- and before any rule runs, so a finalized
    # version is reported as such rather than as whichever rule happened to
    # fail first. This is not the last word on the question:
    # ``_require_still_writable`` asks it again, fresh, just before the write.
    _require_mutable_working_version(session, schedule_version=version)

    requirements = _load_requirements(
        session,
        schedule_version_id=version.id,
        requirement_ids=sorted({p.requirement_id for p in proposals}),
    )
    memberships = _load_memberships(
        session,
        ministry_id=ministry_id,
        membership_ids=sorted({p.membership_id for p in proposals}),
    )
    resolved = _resolve(
        proposals,
        requirements=requirements,
        memberships=memberships,
        schedule_version_id=version.id,
        ministry_id=ministry_id,
    )

    facts = _BatchFacts.prefetch(
        session,
        version=version,
        ministry_id=ministry_id,
        requirements=[requirement for requirement, _ in resolved],
        memberships=[membership for _, membership in resolved],
    )

    created: list[Assignment] = []
    pending: list[Assignment] = []
    audit: list[tuple[Assignment, ScheduleVersionRequirement, MinistryMembership]] = []
    # Every placement this run makes whose member carries a same-event support
    # requirement. That rule is the one rule that is not a property of a single
    # placement against existing state -- it permits a placement *because of
    # other placements* -- so judging it row by row would refuse a run the
    # moment it happened to reach the subject before the supporter, for a
    # schedule that satisfies the rule perfectly well once complete. Collected
    # here and judged below, against the run's finished state, through the same
    # shared rule ``assign_member`` applies per placement.
    support_checks: list[tuple[MinistryMembership, ScheduleVersionRequirement]] = []

    for requirement, membership in resolved:
        # Per placement, exactly as ``assign_member`` does it -- an actor
        # entitled to one ministry's schedule is not thereby entitled to
        # another's, and a version is only ever one ministry's, so this is a
        # cheap in-memory re-assertion rather than a new question.
        require_ministry_operator(actor, ministry_id=requirement.ministry_id)

        existing = facts.exact_assignment(
            requirement_id=requirement.id, membership_id=membership.id
        )
        if existing is not None:
            created.append(existing)
            continue

        pair = _PairFacts(facts, requirement=requirement, membership=membership)
        require_absolute_rules(
            requirement=requirement, membership=membership, facts=pair
        )
        blockers = collect_overridable_blockers(
            requirement=requirement, facts=pair.overridable_facts()
        )
        # Always ``False``: automatic work never overrides (module docstring).
        # The call is still made, because the rule that a blocked placement
        # without a reason is refused is the same rule, and stating it once
        # here is what guarantees generation cannot acquire a quiet exemption.
        is_override = resolve_override(blockers=blockers, override_reason=None)

        assignment = build_assignment(
            requirement=requirement,
            membership=membership,
            is_override=is_override,
            override_reason=None,
        )
        pending.append(assignment)
        created.append(assignment)
        audit.append((assignment, requirement, membership))
        if facts.has_support_requirement(membership_id=membership.id):
            support_checks.append((membership, requirement))
        facts.record_accepted(
            assignment=assignment, requirement=requirement, membership=membership
        )

    if not pending:
        return tuple(created)

    # The same-event support rule, applied once the run's state is complete.
    # Every placement above has been recorded, so the roster each subject is
    # judged against is the one this run actually produced -- pre-run rows
    # included. A refusal here raises before anything is flushed, so a run that
    # would seat somebody without their support writes nothing at all rather
    # than part of a schedule nobody decided on.
    #
    # Only subjects this run *placed* are checked. A subject whose row predates
    # the run is not a decision this run made, and repairing one would mean
    # deleting somebody's assignment -- which this module never does. That
    # pre-existing violation is real and the finalization gate reports it.
    for membership, requirement in support_checks:
        require_same_event_support(
            facts.same_event_support(
                membership_id=membership.id, event_id=requirement.event_id
            ),
            subject_display_name=membership.person.display_name,
            event_date=requirement.event_date,
        )

    # The last thing before the write, and the only check that happens after
    # the facts were read. Everything above was evaluated against a prefetch
    # taken ~9 round trips ago; this one round trip re-asks the single
    # question whose wrong answer would corrupt rather than merely
    # inconvenience -- *may this version still be written to at all?*
    # See its own docstring for why it is here and not merged with the gate
    # at the top.
    _require_still_writable(session, schedule_version=version)

    session.add_all(pending)
    # One flush for every new row, for the reason ``assign_member`` flushes
    # once for its one row: the audit rows below reference identities that do
    # not exist until the INSERT runs. SQLAlchemy sends these as a single
    # multi-row INSERT ... RETURNING, so the whole run costs one round trip
    # where Task 34 spent one per row. This function never commits or rolls
    # back.
    session.flush(pending)

    for assignment, requirement, membership in audit:
        record_assignment_created(
            session,
            actor=actor,
            assignment=assignment,
            requirement=requirement,
            membership=membership,
            override_reason=None,
            # An ordinary generated assignment overrode nothing, and
            # ``resolve_override`` has already refused the run if anything was
            # blocked -- so this is provably empty, passed explicitly rather
            # than assumed.
            blockers=frozenset(),
        )

    return tuple(created)


# --------------------------------------------------------------------------
# The write-time mutability gate
# --------------------------------------------------------------------------


def _require_mutable_working_version(
    session: Session, *, schedule_version: ScheduleVersion
) -> None:
    """**[APPROVED]** A version may be changed only while it is DRAFT/REVIEW
    *and* no newer version of the same schedule exists (§7).

    The same two-part rule :mod:`app.services.assignment` applies, with the
    status half shared outright and the "is it still the latest?" half queried
    fresh -- that fact lives on other rows, so nothing about holding this
    object could ever answer it honestly.
    """
    require_mutable_working_version_status(schedule_version)
    if _newer_version_exists(
        session,
        schedule_id=schedule_version.schedule_id,
        version_number=schedule_version.version_number,
    ):
        raise InvalidOperationError(
            "cannot change assignments on a schedule version"
            " that has been superseded by a newer version"
        )


def _newer_version_exists_statement(
    schedule_id: int, version_number: int
) -> Select[tuple[int]]:
    """The same ``LIMIT 1`` existence probe every other lifecycle rule uses."""
    return (
        select(ScheduleVersion.id)
        .where(
            ScheduleVersion.schedule_id == schedule_id,
            ScheduleVersion.version_number > version_number,
        )
        .limit(1)
    )


def _newer_version_exists(
    session: Session, *, schedule_id: int, version_number: int
) -> bool:
    stmt = _newer_version_exists_statement(schedule_id, version_number)
    return session.execute(stmt).scalar_one_or_none() is not None


@dataclass(frozen=True, slots=True)
class _LiveVersionStatus:
    """This version's ``status`` as the **database** has it at this instant.

    A separate object rather than the ``ScheduleVersion`` in hand, and that is
    the whole point: the ORM object's ``status`` was loaded when the request
    began and ``flush()`` does not expire it, so reading it again would return
    the same answer however long ago it was read. This carries the value the
    re-check actually queried, so the shared status rule is applied to a fresh
    fact rather than a remembered one.
    """

    status: str


def _writability_statement(
    schedule_id: int, schedule_version_id: int, version_number: int
) -> Select[tuple[int, str]]:
    """One round trip answering both halves of §7's mutability rule.

    Returns this version's own row **and** every newer version of the same
    schedule: ``id = :version`` finds the first, ``version_number > :number``
    finds the second, and a schedule holds a handful of versions, so the union
    is a couple of rows. Splitting it into two probes would cost two round
    trips at the exact point in the run where a round trip is the thing being
    minimized -- and, worse, would take the two readings at two different
    instants.
    """
    return select(ScheduleVersion.id, ScheduleVersion.status).where(
        ScheduleVersion.schedule_id == schedule_id,
        or_(
            ScheduleVersion.id == schedule_version_id,
            ScheduleVersion.version_number > version_number,
        ),
    )


def _require_still_writable(
    session: Session, *, schedule_version: ScheduleVersion
) -> None:
    """Re-ask, immediately before the INSERT, whether this version may be
    written to -- reading both halves fresh from the database.

    **Why this exists, stated plainly, because a second gate wants
    justifying.** The gate at the top of the run cannot answer this question:
    the whole prefetch runs between it and the write, and in that window a
    concurrent request can finalize this version or create a successor. Two
    consequences, and they are not equally serious:

    - **A successor appears.** The version becomes unpublishable but is not
      corrupted, and Task 34's per-row writer caught this because it re-probed
      before *every* row. Batching replaced N probes with one, and this is the
      one that restores the parity -- at one query for the run instead of one
      per row.
    - **The version is FINALIZED.** New rows would be added to a published,
      immutable roster, and **nothing else in the system would notice**:
      finalization readiness would inspect the version and report it ready,
      because every individual row really is valid. Task 34 did not catch this
      either -- it read ``status`` off the ORM object, loaded once per request
      and never expired -- so this half is not a restoration but a gap both
      writers had, closed here because the query that closes the other half
      closes this one for free.

    **This narrows a race; it does not eliminate one.** A finalize committed
    between this statement and the INSERT a moment later is still not seen.
    Eliminating it needs the version row locked (``SELECT ... FOR UPDATE``) by
    *every* writer that touches a version's assignments or its lifecycle --
    manual assignment, carry-forward, submit, finalize and successor creation
    -- which is a locking decision across five services, not something this
    function should invent. What this does buy is that the window is now one
    round trip rather than the whole prefetch, which is as tight as the per-row
    writer ever managed and tighter than it managed for its own first row.

    :raises InvalidOperationError: the version is no longer DRAFT or REVIEW, a
        newer version of its schedule now exists, or its row has gone.
    """
    rows = session.execute(
        _writability_statement(
            schedule_version.schedule_id,
            schedule_version.id,
            schedule_version.version_number,
        )
    ).all()

    live_status = next(
        (status for row_id, status in rows if row_id == schedule_version.id), None
    )
    if live_status is None:
        # The row the whole run is about is gone. Refused rather than written
        # against, which would fail on the foreign key anyway -- but with an
        # IntegrityError at commit, far from its cause.
        raise InvalidOperationError(
            "the schedule version being generated into no longer exists"
        )
    require_mutable_working_version_status(_LiveVersionStatus(status=live_status))

    if any(row_id != schedule_version.id for row_id, _ in rows):
        raise InvalidOperationError(
            "cannot change assignments on a schedule version"
            " that has been superseded by a newer version"
        )


# --------------------------------------------------------------------------
# Resolution: a proposal is three ids, and they must name rows that belong here
# --------------------------------------------------------------------------


def _resolve(
    proposals: Sequence[ProposedAssignment],
    *,
    requirements: dict[int, ScheduleVersionRequirement],
    memberships: dict[int, MinistryMembership],
    schedule_version_id: int,
    ministry_id: int,
) -> list[tuple[ScheduleVersionRequirement, MinistryMembership]]:
    """Pair every proposal with its rows, in proposal order.

    Resolution, not revalidation: an id that names no requirement of this
    version, or no membership of this ministry, cannot be acted on at all, and
    failing here says so plainly instead of letting the rules puzzle over a
    mismatch. Activity is deliberately not filtered in the loading queries --
    a deactivated membership must reach the rules and be refused there, with
    its own clear error, rather than vanishing into "unresolvable".
    """
    resolved: list[tuple[ScheduleVersionRequirement, MinistryMembership]] = []
    for proposal in proposals:
        requirement = requirements.get(proposal.requirement_id)
        if requirement is None:
            raise InvalidOperationError(
                f"proposed requirement {proposal.requirement_id} does not"
                f" belong to schedule version {schedule_version_id}"
            )
        membership = memberships.get(proposal.membership_id)
        if membership is None:
            raise InvalidOperationError(
                f"proposed membership {proposal.membership_id} does not belong"
                f" to ministry {ministry_id}"
            )
        resolved.append((requirement, membership))
    return resolved


# --------------------------------------------------------------------------
# The prefetch
# --------------------------------------------------------------------------


class _BatchFacts:
    """Every fact the rules will ask for, read once for the whole run.

    Split into what the run cannot change (read once, never touched again) and
    what it can (seeded from the database, then advanced by
    :meth:`record_accepted` as each placement is accepted). The module
    docstring sets out which is which and why.
    """

    __slots__ = (
        "_existing",
        "_qualifications",
        "_availability",
        "_conflicts",
        "_serving_maximums",
        "_linked",
        "_event_sequence",
        "_group_caps_by_membership",
        "_support_by_membership",
        "_filled_by_requirement",
        "_held_by_membership",
        "_membership_events",
        "_events_by_membership",
        "_memberships_by_date",
        "_memberships_by_event",
    )

    def __init__(
        self,
        *,
        existing: dict[tuple[int, int], Assignment],
        qualifications: set[tuple[int, int]],
        availability: set[tuple[int, int]],
        conflicts: set[tuple[int, datetime.date]],
        serving_maximums: dict[int, int],
        linked: dict[int, frozenset[int]],
        event_sequence: MinistryEventSequence | None,
        group_caps_by_membership: dict[int, tuple[MemberGroupCapConfig, ...]],
        support_by_membership: dict[int, SupportRequirementConfig],
        filled_by_requirement: dict[int, int],
        held_by_membership: dict[int, int],
        membership_events: set[tuple[int, int]],
        events_by_membership: dict[int, set[int]],
        memberships_by_date: dict[datetime.date, set[int]],
        memberships_by_event: dict[int, set[int]],
    ) -> None:
        self._existing = existing
        self._qualifications = qualifications
        self._availability = availability
        self._conflicts = conflicts
        self._serving_maximums = serving_maximums
        self._linked = linked
        self._event_sequence = event_sequence
        self._group_caps_by_membership = group_caps_by_membership
        self._support_by_membership = support_by_membership
        self._filled_by_requirement = filled_by_requirement
        self._held_by_membership = held_by_membership
        self._membership_events = membership_events
        self._events_by_membership = events_by_membership
        self._memberships_by_date = memberships_by_date
        self._memberships_by_event = memberships_by_event

    @classmethod
    def prefetch(
        cls,
        session: Session,
        *,
        version: ScheduleVersion,
        ministry_id: int,
        requirements: Sequence[ScheduleVersionRequirement],
        memberships: Sequence[MinistryMembership],
    ) -> "_BatchFacts":
        """A fixed number of set-based reads, whatever the number of proposals.

        The count does not grow with the schedule -- that is the whole point,
        and ``tests/test_services_generated_assignment.py`` pins it as a
        property rather than as a number a future refactor might legitimately
        change.
        """
        membership_ids = sorted({m.id for m in memberships})
        person_ids = sorted({m.person_id for m in memberships})
        role_ids = sorted({r.ministry_role_id for r in requirements})
        event_ids = sorted({r.event_id for r in requirements})
        dates = sorted({r.event_date for r in requirements})
        ministry_ids = sorted({r.ministry_id for r in requirements})

        # -- What the run changes, seeded from what is already there. Read as
        # (assignment, snapshot date) in one join: an Assignment carries no
        # date of its own (§11), and the version means the dates it committed
        # to, never the live Event row.
        existing: dict[tuple[int, int], Assignment] = {}
        filled_by_requirement: dict[int, int] = {}
        held_by_membership: dict[int, int] = {}
        membership_events: set[tuple[int, int]] = set()
        events_by_membership: dict[int, set[int]] = {}
        memberships_by_date: dict[datetime.date, set[int]] = {}
        memberships_by_event: dict[int, set[int]] = {}
        for row, event_date in session.execute(
            _existing_assignments_statement(version.id)
        ).all():
            existing[(row.schedule_version_requirement_id, row.ministry_membership_id)] = row
            filled_by_requirement[row.schedule_version_requirement_id] = (
                filled_by_requirement.get(row.schedule_version_requirement_id, 0) + 1
            )
            held_by_membership[row.ministry_membership_id] = (
                held_by_membership.get(row.ministry_membership_id, 0) + 1
            )
            membership_events.add((row.ministry_membership_id, row.event_id))
            # The same rows as ``membership_events``, inverted: the event-gap
            # rule asks "which events is this person at?", which that set can
            # only answer by being scanned. Built alongside rather than derived
            # later so both are advanced together in ``record_accepted``.
            events_by_membership.setdefault(row.ministry_membership_id, set()).add(
                row.event_id
            )
            memberships_by_date.setdefault(event_date, set()).add(
                row.ministry_membership_id
            )
            # The same rows again, keyed by *event* rather than date: the
            # member-group cap and the support requirement are both about one
            # crew, and a period may hold two services on one Sunday. Built
            # alongside the date index rather than derived from it, because the
            # two are genuinely different questions and collapsing them would
            # make one of the rules wrong.
            memberships_by_event.setdefault(row.event_id, set()).add(
                row.ministry_membership_id
            )

        # -- What the run cannot change. --
        qualifications = {
            (row.ministry_membership_id, row.ministry_role_id)
            for row in session.execute(
                _qualifications_statement(membership_ids, role_ids)
            ).scalars()
            if row.is_qualified
        }
        unavailable = {
            (row.ministry_membership_id, row.event_id)
            for row in session.execute(
                _availability_statement(membership_ids, event_ids)
            ).scalars()
            if row.availability_state == AVAILABILITY_UNAVAILABLE
        }
        serving_maximums = get_serving_limits_for(
            session,
            ministry_membership_ids=membership_ids,
            scheduling_period_id=version.scheduling_period_id,
        )
        linked = _linked_membership_ids(
            get_same_date_exclusion_pairs(
                session, scheduling_period_id=version.scheduling_period_id
            )
        )
        # The ministry's event sequence and the history the gap rule needs,
        # read once for the run. ``None`` when the period configures no rule,
        # which is the ordinary case and costs the single query that reads the
        # setting. Nothing this run writes can change the sequence or last
        # quarter's finalized schedule, so it belongs squarely among the facts
        # read once (module docstring).
        event_sequence = load_event_gap_sequence(
            session,
            scheduling_period_id=version.scheduling_period_id,
            ministry_id=ministry_id,
            schedule_version_id=version.id,
        )
        # The two group-shaped rules, read once for the run and inverted into
        # "which rules touch this membership?" -- the question every placement
        # asks. Both are configuration a head changes deliberately, and nothing
        # this run writes can change either, so both belong squarely among the
        # facts read once (module docstring). Four queries between them,
        # whatever the number of proposals, groups, members or supporters.
        group_caps_by_membership = _group_caps_by_membership(
            load_member_group_caps(
                session, scheduling_period_id=version.scheduling_period_id
            )
        )
        support_by_membership = {
            requirement.subject_membership_id: requirement
            for requirement in load_support_requirements(
                session, scheduling_period_id=version.scheduling_period_id
            )
        }
        # The church-wide rule, asked once for every (person, date) pair this
        # run touches, through the shared set-based form -- the same query the
        # one-person function runs, widened to the whole set. A version is one
        # ministry's, so ``ministry_ids`` holds one entry in practice; looping
        # is what makes that a fact about the data rather than an assumption.
        conflicts: set[tuple[int, datetime.date]] = set()
        for target_ministry_id in ministry_ids:
            for key, result in get_sunday_conflicts_for(
                session,
                person_ids=person_ids,
                conflict_dates=dates,
                target_ministry_id=target_ministry_id,
            ).items():
                if result.is_blocked:
                    conflicts.add(key)

        return cls(
            existing=existing,
            qualifications=qualifications,
            availability=unavailable,
            conflicts=conflicts,
            serving_maximums=serving_maximums,
            linked=linked,
            event_sequence=event_sequence,
            group_caps_by_membership=group_caps_by_membership,
            support_by_membership=support_by_membership,
            filled_by_requirement=filled_by_requirement,
            held_by_membership=held_by_membership,
            membership_events=membership_events,
            events_by_membership=events_by_membership,
            memberships_by_date=memberships_by_date,
            memberships_by_event=memberships_by_event,
        )

    # -- Reads -------------------------------------------------------------

    def exact_assignment(
        self, *, requirement_id: int, membership_id: int
    ) -> Assignment | None:
        """This exact membership already filling this exact requirement -- the
        idempotency case. Distinct from the one-position-per-event question,
        which is broader (this membership, this event, *any* requirement).
        """
        return self._existing.get((requirement_id, membership_id))

    def fills_other_position_in_event(
        self, *, membership_id: int, event_id: int
    ) -> bool:
        return (membership_id, event_id) in self._membership_events

    def serving_limit(self, *, membership_id: int) -> ServingLimitFacts:
        maximum = self._serving_maximums.get(membership_id)
        if maximum is None:
            return ServingLimitFacts(maximum=None)
        return ServingLimitFacts(
            maximum=maximum, held=self._held_by_membership.get(membership_id, 0)
        )

    def linked_member_assigned_on_date(
        self, *, membership_id: int, event_date: datetime.date
    ) -> bool:
        linked_ids = self._linked.get(membership_id)
        if not linked_ids:
            return False  # the ordinary case: no rule, so no question
        return bool(linked_ids & self._memberships_by_date.get(event_date, frozenset()))

    def event_gap(
        self, *, membership_id: int, event_id: int
    ) -> EventGapFacts:
        """The event-gap rule, answered from the prefetched sequence.

        The sequence and the history never change during a run; which events
        this membership occupies *does*, and it is read from
        ``_events_by_membership``, which :meth:`record_accepted` advances. So
        the fiftieth placement is judged against the forty-nine before it --
        a run cannot seat somebody at two consecutive events by making both
        placements itself.
        """
        sequence = self._event_sequence
        if sequence is None:
            return EventGapFacts(min_intervening_events=None)
        occupied = sequence.occupied_events_for(
            membership_id,
            version_event_ids=self._events_by_membership.get(membership_id, ()),
        )
        return EventGapFacts(
            min_intervening_events=sequence.min_intervening_events,
            conflicting_event_date=sequence.conflicting_event_date(
                target_event_id=event_id, occupied_event_ids=occupied
            ),
        )

    def member_group_event_limits(
        self, *, membership_id: int, event_id: int
    ) -> tuple[MemberGroupEventFacts, ...]:
        """The member-group caps touching this placement, from the prefetch.

        The caps and their membership never change during a run; who is on the
        event's roster *does*, and it is read from ``_memberships_by_event``,
        which :meth:`record_accepted` advances. So the fiftieth placement is
        judged against the forty-nine before it -- a run cannot exceed a group's
        cap by making every one of the placements itself.
        """
        caps = self._group_caps_by_membership.get(membership_id)
        if not caps:
            return ()  # the ordinary case: no capped group, so no question
        roster = self._memberships_by_event.get(event_id, frozenset())
        return tuple(
            MemberGroupEventFacts(
                member_group_name=cap.member_group_name,
                max_per_event=cap.max_per_event,
                members_present=count_group_members_present(cap, roster),
            )
            for cap in caps
        )

    def has_support_requirement(self, *, membership_id: int) -> bool:
        """Whether this membership is the subject of a support requirement.

        Used by the run to decide *which* placements need the deferred check at
        all, so a run in a period with no such rule collects nothing and checks
        nothing. It is not itself a rule, and answers no question about an
        event.
        """
        return membership_id in self._support_by_membership

    def same_event_support(
        self, *, membership_id: int, event_id: int
    ) -> SameEventSupportFacts:
        """The support requirement's state for one (subject, event), from the
        prefetch.

        Read against ``_memberships_by_event``, which the run advances, which is
        exactly what makes the deferred check above meaningful: by the time it
        runs, the roster it counts is the one this run produced.
        """
        requirement = self._support_by_membership.get(membership_id)
        if requirement is None:
            return SameEventSupportFacts(min_supporters=None)
        roster = self._memberships_by_event.get(event_id, frozenset())
        return SameEventSupportFacts(
            min_supporters=requirement.min_supporters,
            supporters_present=count_supporters_present(requirement, roster),
            approved_supporter_count=len(requirement.supporter_membership_ids),
        )

    def has_cross_ministry_sunday_conflict(
        self,
        *,
        requirement: ScheduleVersionRequirement,
        membership: MinistryMembership,
    ) -> bool:
        """The church-wide hard rule, answered from the run's one prefetch.

        The snapshot date, never the live Event row; and keyed by
        ``person_id``, because the rule is about the human rather than one of
        their memberships.
        """
        return (membership.person_id, requirement.event_date) in self._conflicts

    def overridable_facts(
        self,
        *,
        requirement: ScheduleVersionRequirement,
        membership: MinistryMembership,
    ) -> OverridableFacts:
        return OverridableFacts(
            is_qualified=(
                (membership.id, requirement.ministry_role_id) in self._qualifications
            ),
            is_unavailable=(
                (membership.id, requirement.event_id) in self._availability
            ),
            current_filled_count=self._filled_by_requirement.get(requirement.id, 0),
        )

    # -- The one write ------------------------------------------------------

    def record_accepted(
        self,
        *,
        assignment: Assignment,
        requirement: ScheduleVersionRequirement,
        membership: MinistryMembership,
    ) -> None:
        """Advance the run-varying facts, so every later placement is judged
        against this one.

        This is what replaces re-counting from the database between rows. It
        must stay exhaustive: a fact that is seeded but not advanced here
        would make the rule depending on it blind to the run's own output,
        which is precisely the class of bug batching invites.

        The pending row is registered for idempotency too, so a result that
        proposed the same pair twice yields the one row twice -- what
        ``assign_member`` did by re-querying after its flush, and what the
        version/event/membership unique constraint would otherwise refuse at
        flush time.
        """
        self._existing.setdefault((requirement.id, membership.id), assignment)
        self._filled_by_requirement[requirement.id] = (
            self._filled_by_requirement.get(requirement.id, 0) + 1
        )
        self._held_by_membership[membership.id] = (
            self._held_by_membership.get(membership.id, 0) + 1
        )
        self._membership_events.add((membership.id, requirement.event_id))
        self._events_by_membership.setdefault(membership.id, set()).add(
            requirement.event_id
        )
        self._memberships_by_date.setdefault(requirement.event_date, set()).add(
            membership.id
        )
        self._memberships_by_event.setdefault(requirement.event_id, set()).add(
            membership.id
        )


class _PairFacts:
    """One (requirement, membership) pair's view of :class:`_BatchFacts`.

    The :class:`~app.services.assignment_rules.PairFactSource` the rules
    actually receive: it holds no state of its own and answers every question
    out of the prefetch, so the rules cannot tell it apart from
    :mod:`app.services.assignment`'s per-row source except by speed.
    """

    __slots__ = ("_facts", "_requirement", "_membership")

    def __init__(
        self,
        facts: _BatchFacts,
        *,
        requirement: ScheduleVersionRequirement,
        membership: MinistryMembership,
    ) -> None:
        self._facts = facts
        self._requirement = requirement
        self._membership = membership

    def fills_other_position_in_event(self) -> bool:
        return self._facts.fills_other_position_in_event(
            membership_id=self._membership.id, event_id=self._requirement.event_id
        )

    def serving_limit(self) -> ServingLimitFacts:
        return self._facts.serving_limit(membership_id=self._membership.id)

    def linked_member_assigned_on_date(self) -> bool:
        return self._facts.linked_member_assigned_on_date(
            membership_id=self._membership.id,
            event_date=self._requirement.event_date,
        )

    def event_gap(self) -> EventGapFacts:
        return self._facts.event_gap(
            membership_id=self._membership.id,
            event_id=self._requirement.event_id,
        )

    def member_group_event_limits(self) -> tuple[MemberGroupEventFacts, ...]:
        return self._facts.member_group_event_limits(
            membership_id=self._membership.id,
            event_id=self._requirement.event_id,
        )

    def same_event_support(self) -> SameEventSupportFacts:
        return self._facts.same_event_support(
            membership_id=self._membership.id,
            event_id=self._requirement.event_id,
        )

    def has_cross_ministry_sunday_conflict(self) -> bool:
        return self._facts.has_cross_ministry_sunday_conflict(
            requirement=self._requirement, membership=self._membership
        )

    def overridable_facts(self) -> OverridableFacts:
        return self._facts.overridable_facts(
            requirement=self._requirement, membership=self._membership
        )


def _group_caps_by_membership(
    caps: Iterable[MemberGroupCapConfig],
) -> dict[int, tuple[MemberGroupCapConfig, ...]]:
    """Invert the period's configured caps into "which caps touch this member?".

    A member may be in several capped groups and all of them apply at once --
    the hard rules are a conjunction, not a ranking -- so the value is a tuple
    rather than a single cap. Order follows the caps' own order, which
    :func:`app.services.member_group.load_member_group_caps` fixes by group id,
    so a run's refusal message is the same one every time for the same state.
    """
    by_membership: dict[int, list[MemberGroupCapConfig]] = {}
    for cap in caps:
        for membership_id in cap.member_membership_ids:
            by_membership.setdefault(membership_id, []).append(cap)
    return {
        membership_id: tuple(found)
        for membership_id, found in by_membership.items()
    }


def _linked_membership_ids(
    pairs: Iterable[tuple[int, int]]
) -> dict[int, frozenset[int]]:
    """Invert the period's configured pairs into "who is this one linked to?".

    Built from **both** sides of every pair, because the pair is unordered and
    a membership may be stored as either half -- the same reason
    :func:`app.services.same_date_exclusion.get_linked_membership_ids` queries
    with an ``OR`` rather than looking up the canonical low id.
    """
    linked: dict[int, set[int]] = {}
    for membership_a_id, membership_b_id in pairs:
        linked.setdefault(membership_a_id, set()).add(membership_b_id)
        linked.setdefault(membership_b_id, set()).add(membership_a_id)
    return {key: frozenset(value) for key, value in linked.items()}


# --------------------------------------------------------------------------
# Queries -- each split from its execution so the SQL is testable with no
# database (see tests/test_services_generated_assignment.py)
# --------------------------------------------------------------------------


def _requirements_statement(
    schedule_version_id: int, requirement_ids: Sequence[int]
) -> Select[tuple[ScheduleVersionRequirement]]:
    """Scoped to this version in the query itself, so a requirement belonging
    to some other version simply does not come back and the caller reports an
    unresolvable proposal rather than writing into the wrong schedule.

    The three eager loads are not an optimization detail: the rules read
    ``event.cancelled_at``, ``ministry_role.deactivated_at`` and the role's and
    ministry's names for the audit summary, on **every** proposal. Left lazy
    they would be a round trip per distinct event and role -- reintroducing,
    inside the batch, the very per-row querying it exists to remove.
    """
    return (
        select(ScheduleVersionRequirement)
        .where(
            ScheduleVersionRequirement.schedule_version_id == schedule_version_id,
            ScheduleVersionRequirement.id.in_(requirement_ids),
        )
        .options(
            joinedload(ScheduleVersionRequirement.event),
            joinedload(ScheduleVersionRequirement.ministry_role).joinedload(
                MinistryRole.ministry
            ),
        )
    )


def _load_requirements(
    session: Session, *, schedule_version_id: int, requirement_ids: list[int]
) -> dict[int, ScheduleVersionRequirement]:
    rows = (
        session.execute(
            _requirements_statement(schedule_version_id, requirement_ids)
        )
        .scalars()
        .unique()
        .all()
    )
    return {row.id: row for row in rows}


def _memberships_statement(
    ministry_id: int, membership_ids: Sequence[int]
) -> Select[tuple[MinistryMembership]]:
    """Ministry-scoped for the same reason requirements are version-scoped.
    Activity is deliberately *not* filtered: a deactivated membership must
    reach the rules and be refused there, with its own clear error, rather than
    vanishing into "unresolvable".

    ``person`` is eager-loaded because two absolute rules and the audit summary
    read it for every proposal.
    """
    return (
        select(MinistryMembership)
        .where(
            MinistryMembership.ministry_id == ministry_id,
            MinistryMembership.id.in_(membership_ids),
        )
        .options(joinedload(MinistryMembership.person))
    )


def _load_memberships(
    session: Session, *, ministry_id: int, membership_ids: list[int]
) -> dict[int, MinistryMembership]:
    rows = (
        session.execute(_memberships_statement(ministry_id, membership_ids))
        .scalars()
        .unique()
        .all()
    )
    return {row.id: row for row in rows}


def _existing_assignments_statement(schedule_version_id: int) -> Select:
    """Every assignment this version already carries, with the **snapshot**
    date each one is committed to.

    One query answering four questions at once -- idempotency, the
    one-position-per-event rule, the serving count and the linked-pair date
    rule -- because all four are about the same rows and asking separately
    would be three more round trips for no additional truth. Joined to
    ``schedule_version_requirement`` for ``event_date`` rather than reading the
    live ``event`` row: the version means the dates it committed to, and an
    event that has since moved must not silently relocate the question (§8,
    §11).

    Scoped to this version alone. A predecessor version's assignments are
    history, not commitments anyone is carrying.
    """
    return (
        select(Assignment, ScheduleVersionRequirement.event_date)
        .join(
            ScheduleVersionRequirement,
            ScheduleVersionRequirement.id
            == Assignment.schedule_version_requirement_id,
        )
        .where(Assignment.schedule_version_id == schedule_version_id)
    )


def _qualifications_statement(
    membership_ids: Sequence[int], role_ids: Sequence[int]
) -> Select[tuple[RoleQualification]]:
    """Absence is "never assessed" and an explicit ``False`` is "assessed no"
    (core §8). Both are the same scheduling outcome, so this read keeps only
    the approved rows and the rules treat a missing key as the single
    ``not_qualified`` blocker -- exactly the conflation
    :mod:`app.services.assignment_policy` documents, and no further one.
    """
    return select(RoleQualification).where(
        RoleQualification.ministry_membership_id.in_(membership_ids),
        RoleQualification.ministry_role_id.in_(role_ids),
    )


def _availability_statement(
    membership_ids: Sequence[int], event_ids: Sequence[int]
) -> Select[tuple[Availability]]:
    """Absence is "no response", distinct from both explicit answers
    (scheduling-input §8), and is not a blocker. Only explicit ``UNAVAILABLE``
    rows are kept.
    """
    return select(Availability).where(
        Availability.ministry_membership_id.in_(membership_ids),
        Availability.event_id.in_(event_ids),
    )
