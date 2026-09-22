"""Is this ScheduleVersion currently fit to become the authoritative schedule?

Read-only. Answers one question -- *would finalizing this version right now
publish a correct roster?* -- and returns the diagnostics behind the answer.
It changes nothing: Task 27 owns authorization, the REVIEW/latest-version
requirement, the status transition, ``finalized_at`` and the audit row.
Section numbers refer to ``docs/architecture/schedule-output-data-model.md``.

**[APPROVED] REVIEW status is not fitness** (§6, §16). A version reaches
REVIEW because a head submitted it; it becomes authoritative only if the
church-wide facts still hold. Between those two moments a qualification can be
revoked, a role deactivated, a person deactivated, an event cancelled, or
another ministry's schedule finalized and taken the same person for that
Sunday. Every gate below is therefore evaluated against **current** state, not
against what was true when the assignment was made.

Eight gates, all evaluated (never short-circuited) so one call reports
everything wrong at once:

1. **The requirement snapshot is fresh** -- Task 23's exact set comparison,
   reused wholesale (:mod:`app.services.schedule_staleness`), never
   re-implemented and never "repaired": snapshot rows are immutable history
   (§8), and a stale version's remedy is a successor version (§13).
2. **Every requirement is staffed** -- ``assignments >= required_count`` on the
   **snapshot's** ``required_count``, never current ``staffing_requirement``.
   Deliberately ``>=`` and not ``==``: Task 22 permits an authorized capacity
   override, so an overfilled requirement can be perfectly legitimate.
3. **Every assignment is still valid** -- the absolute structural checks, plus
   Task 22's **four** remaining overridable checks re-evaluated against today.
   The fifth used to be the church-wide same-Sunday conflict; it is now gate
   3b, because it is not overridable.
3b. **Nobody serves two ministries on one day** -- if another ministry has
   already committed this Person on the requirement's snapshot date, the
   version cannot be finalized (ADR 0002/0003). **An override authorizes
   nothing here, and this is Task 79's final correction**: until then the
   conflict was one of the five bounded blockers, so an assignment whose audit
   named ``sunday_conflict`` passed gate 3 and a contradictory schedule could
   become FINALIZED. It is now checked with the absolutes, before override
   history is consulted at all, and reported whether the assignment carries an
   override or not -- which is also what makes contradictory *historical* data
   visible rather than quietly accepted.
4. **No member exceeds their serving maximum for this period** -- counted
   within *this* version and compared against the **currently configured**
   limit (requirements §4.4.1). This gate is what makes a lowered limit safe:
   a head who reduces somebody's maximum after a draft was built never has an
   assignment silently deleted, and equally never gets to finalize a schedule
   that breaks the number they just agreed. **An override authorizes nothing
   here** -- the serving maximum is not one of Task 22's bounded overridable blockers
   and never was, so no historical ``overridden_blockers`` payload can excuse
   it. It also catches assignments that predate the limit entirely and ones
   carried forward from a predecessor, because it asks about the version's
   current contents rather than how they got there.
5. **No linked pair shares a date** -- for every same-date exclusion
   **currently** configured for this period, the two memberships must not both
   hold an assignment on one calendar date in this version (requirements
   §4.4.2). This gate is what makes adding a constraint after a draft exists
   safe: nothing deletes an assignment to satisfy a rule a head has just
   recorded, and equally nothing lets that head finalize a schedule that
   breaks the arrangement two volunteers made. Like gate 4, **an override
   authorizes nothing here**, and it catches assignments that predate the
   constraint or were carried forward, because it asks about the version's
   current contents rather than how they got there. Dates are compared on the
   snapshot ``event_date``, so two events on one Sunday are one date.
6. **Nobody serves again too soon** -- when the period **currently** configures
   ``min_intervening_events``, no member may hold two assignments closer
   together than that many of the ministry's own events (requirements §4.8).
   Counted along the same sequence the solver and manual assignment use, which
   reaches past **both** ends of the period: somebody who served the last event
   before it may not serve the first event in it, and somebody already
   published on the first event after it may not serve the last event in it.
   Like gates 4 and 5, **an override authorizes nothing here**, and it catches
   assignments that predate the rule or were carried forward. This is what
   makes configuring or tightening a gap after a draft exists safe: nothing
   deletes an assignment to satisfy a rule a head just recorded, and equally
   nothing lets that head finalize a schedule that breaks it.
7. **No event carries too many of one member group** -- for every member-group
   cap **currently** configured for this period, no event in this version may
   hold more than ``max_per_event`` of that group's members, counted once per
   person whatever role they serve (Task 74). Per *event*, not per date: two
   services on one Sunday are two crews. Like gates 4 to 6, **an override
   authorizes nothing here**, and it catches assignments that predate the cap.
8. **Nobody serves an event without their required support** -- for every
   same-event support requirement **currently** configured for this period, an
   event at which the subject holds an assignment must also carry at least
   ``min_supporters`` of their approved supporters (Task 74). This is the gate
   that catches what removal deliberately allows: taking a supporter off an
   event is permitted, and leaves the version unfinalizable rather than
   triggering a repair. **An override authorizes nothing here** either.

**[REVIEWED] An override authorizes the blockers it actually bypassed, and
nothing else.** This is the load-bearing rule of this module.
``is_override=True`` is not a permanent exemption from every future problem:
Task 22 records *which* blockers were bypassed in the creation audit's
``after_values["overridden_blockers"]``, and only those exact codes are
covered. A member whose override was granted for being marked unavailable is
**not** thereby cleared of a qualification that was revoked two weeks later --
nobody ever agreed to that, and no audit row says they did. The comparison is
one line, ``current_blockers - historical_blockers``, and everything else here
exists to compute those two sets honestly.

**And one rule is outside that comparison entirely.** The church-wide
same-Sunday conflict is not in ``current_blockers`` and cannot be in
``historical_blockers`` (the stored payload is intersected with
:data:`~app.services.assignment_policy.OVERRIDABLE_BLOCKERS` before it is
believed), so there is no reading of any audit row that lets it through. A
payload naming it is still *valid* history -- it records a decision somebody
really made while it was permitted -- it simply excuses nothing.

The converse also holds: a historical blocker that no longer applies is not an
error. An override granted for unavailability, on a member who has since
answered AVAILABLE, leaves true history and no current problem.

Because that history is the only authorization that exists, an override
assignment whose audit is missing, ambiguous or malformed is **not** given the
benefit of the doubt -- it authorizes nothing, and says so as an issue.
:mod:`app.services.assignment_policy` holds the shared code vocabulary.

Not implemented here, deliberately: the REVIEW -> FINALIZED transition,
``finalized_at``, successor versions, the solver, any Assignment mutation or
automatic repair, audit events, and any API over the result.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, joinedload

from app.models.audit import AuditEvent
from app.models.core import MinistryMembership, RoleQualification
from app.models.schedule_output import (
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import (
    AVAILABILITY_UNAVAILABLE,
    Availability,
    MembershipSameDateExclusion,
    MembershipServingLimit,
)
from app.services.assignment_policy import (
    BLOCKER_CAPACITY_FULL,
    BLOCKER_DESCRIPTIONS,
    BLOCKER_NOT_QUALIFIED,
    BLOCKER_ROLE_DEACTIVATED,
    BLOCKER_UNAVAILABLE,
    KNOWN_BLOCKERS,
    OVERRIDABLE_BLOCKERS,
)
from app.services.assignment_rules import CROSS_MINISTRY_SUNDAY_CONFLICT
from app.services.audit import ACTION_ASSIGNMENT_OVERRIDE_APPLIED
from app.services.errors import InvalidOperationError
from app.services.event_gap import (
    MIN_EVENT_GAP_CONFLICT,
    MinistryEventSequence,
    describe_event_gap,
    load_event_gap_sequence,
)
from app.services.member_group import (
    MEMBER_GROUP_EVENT_LIMIT_CONFLICT,
    MemberGroupCapConfig,
    count_group_members_present,
    describe_member_group_cap,
    load_member_group_caps,
)
from app.services.same_date_exclusion import SAME_DATE_LINKED_MEMBER_CONFLICT
from app.services.same_event_support import (
    SAME_EVENT_SUPPORT_CONFLICT,
    SupportRequirementConfig,
    count_supporters_present,
    describe_support_requirement,
    load_support_requirements,
)
from app.services.serving_limit import get_serving_limit
from app.services.schedule_staleness import (
    ScheduleVersionStalenessResult,
    get_schedule_version_staleness,
)
from app.services.sunday_conflict import (
    SundayConflictResult,
    get_person_sunday_conflicts,
    get_sunday_conflicts_for,
)

__all__ = [
    "ISSUE_AMBIGUOUS_OVERRIDE_AUDIT",
    "ISSUE_CANCELLED_EVENT",
    "ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT",
    "ISSUE_INACTIVE_MEMBERSHIP",
    "ISSUE_INACTIVE_PERSON",
    "ISSUE_EXCEEDS_SERVING_LIMIT",
    "ISSUE_MEMBER_GROUP_EVENT_LIMIT_CONFLICT",
    "ISSUE_MIN_EVENT_GAP_CONFLICT",
    "ISSUE_SAME_EVENT_SUPPORT_CONFLICT",
    "ISSUE_INVALID_OVERRIDE_PAYLOAD",
    "ISSUE_MISSING_OVERRIDE_AUDIT",
    "ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT",
    "ISSUE_STALE_REQUIREMENT_SNAPSHOT",
    "ISSUE_UNAUTHORIZED_BLOCKER",
    "ISSUE_UNAUTHORIZED_OVERFILL",
    "ISSUE_UNFILLED_REQUIREMENT",
    "FinalizationIssue",
    "FinalizationReadinessResult",
    "get_finalization_readiness",
]

_ASSIGNMENT_TARGET_TABLE = "assignment"
_OVERRIDDEN_BLOCKERS_KEY = "overridden_blockers"

#: Issue codes. A separate vocabulary from the blocker codes on purpose: a
#: blocker names *a scheduling condition*, an issue names *why this version
#: cannot be finalized*, and one issue (``UNAUTHORIZED_BLOCKER``) reports a
#: blocker without being one.
ISSUE_STALE_REQUIREMENT_SNAPSHOT = "STALE_REQUIREMENT_SNAPSHOT"
ISSUE_UNFILLED_REQUIREMENT = "UNFILLED_REQUIREMENT"
ISSUE_INACTIVE_MEMBERSHIP = "INACTIVE_MEMBERSHIP"
ISSUE_INACTIVE_PERSON = "INACTIVE_PERSON"
ISSUE_CANCELLED_EVENT = "CANCELLED_EVENT"
ISSUE_UNAUTHORIZED_BLOCKER = "UNAUTHORIZED_BLOCKER"
ISSUE_MISSING_OVERRIDE_AUDIT = "MISSING_OVERRIDE_AUDIT"
ISSUE_AMBIGUOUS_OVERRIDE_AUDIT = "AMBIGUOUS_OVERRIDE_AUDIT"
ISSUE_INVALID_OVERRIDE_PAYLOAD = "INVALID_OVERRIDE_PAYLOAD"
#: A member holds more assignments in this version than their currently
#: configured serving maximum for the period allows (requirements §4.4.1).
#: Reported rather than repaired, like every other issue here: the remedy is a
#: head removing an assignment or agreeing a larger number with the volunteer,
#: and neither is a decision this module may take.
ISSUE_EXCEEDS_SERVING_LIMIT = "EXCEEDS_SERVING_LIMIT"
ISSUE_UNAUTHORIZED_OVERFILL = "UNAUTHORIZED_OVERFILL"
#: Two members linked by a same-date exclusion both hold assignments on one
#: calendar date in this version (requirements §4.4.2). Reported rather than
#: repaired, like every other issue here: the remedy is a head removing one of
#: the assignments or clearing the constraint, and neither is a decision this
#: module may take. **An override authorizes nothing here** -- the exclusion is
#: not one of Task 22's bounded overridable blockers and never was.
#:
#: The code is the one :mod:`app.services.same_date_exclusion` defines, so the
#: string a head sees when manual assignment refuses is the same string
#: readiness reports, and neither can drift.
ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT = SAME_DATE_LINKED_MEMBER_CONFLICT
#: One member holds two assignments closer together than the period's currently
#: configured ``min_intervening_events`` allows, counted along this ministry's
#: own event sequence (requirements §4.8). Reported rather than repaired, like
#: every other issue here: the remedy is a head removing an assignment or
#: relaxing the rule, and neither is a decision this module may take. **An
#: override authorizes nothing here** -- the gap rule is not one of Task 22's
#: bounded overridable blockers and never was.
#:
#: The code is the one :mod:`app.services.event_gap` defines, so the string a
#: head sees when manual assignment refuses is the string readiness reports,
#: and neither can drift.
ISSUE_MIN_EVENT_GAP_CONFLICT = MIN_EVENT_GAP_CONFLICT
#: More members of one configured member group hold assignments at one event
#: than the period's **currently** configured cap for that group allows (Task
#: 74). Reported rather than repaired, like every other issue here: the remedy
#: is a head removing an assignment or raising the number, and neither is a
#: decision this module may take. **An override authorizes nothing here** -- the
#: cap is not one of Task 22's bounded overridable blockers and never was.
#:
#: The code is the one :mod:`app.services.member_group` defines, so the string a
#: head sees when manual assignment refuses is the string readiness reports.
ISSUE_MEMBER_GROUP_EVENT_LIMIT_CONFLICT = MEMBER_GROUP_EVENT_LIMIT_CONFLICT
#: A member with a **currently** configured same-event support requirement holds
#: an assignment at an event where too few of their approved supporters do (Task
#: 74). Reported rather than repaired: the remedy is a head assigning a
#: supporter, widening the approved set, removing the subject's assignment, or
#: clearing the rule -- none of which this module may decide. **An override
#: authorizes nothing here.**
#:
#: This is the gate that catches what removal deliberately does not refuse:
#: taking a *supporter* off an event leaves the subject unsupported, and
#: :func:`app.services.assignment.remove_assignment` allows it because removal
#: is the cleanup path. The version simply cannot be finalized until it is put
#: right.
ISSUE_SAME_EVENT_SUPPORT_CONFLICT = SAME_EVENT_SUPPORT_CONFLICT
#: One Person holds an assignment here while another ministry has already
#: claimed them on the requirement's **snapshot** date (ADR 0002/0003).
#:
#: **The church-wide hard rule, and no override authorizes it** -- Task 79's
#: final correction. Until then the conflict was one of Task 22's five bounded
#: overridable blockers, so an assignment whose audit row named
#: ``sunday_conflict`` passed this gate and the version could be FINALIZED.
#: It is now checked here beside the inactive-membership and cancelled-event
#: gates: with the absolutes, before override history is consulted at all, and
#: reported whether or not the assignment carries an override.
#:
#: That is what makes a contradictory *historical* schedule visible rather than
#: quietly accepted: a version carrying such a row simply cannot be finalized
#: until somebody removes one of the two assignments.
#:
#: The code is the one :mod:`app.services.assignment_rules` defines, so the
#: string a head sees when manual assignment refuses is the string readiness
#: reports, and neither can drift.
ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT = CROSS_MINISTRY_SUNDAY_CONFLICT


@dataclass(frozen=True, slots=True)
class FinalizationIssue:
    """One reason this version is not ready, with enough context to act on it.

    ``assignment_id`` / ``schedule_version_requirement_id`` are populated
    whenever the issue is about a specific row, so a caller can link straight
    to it rather than re-deriving which assignment the message means. Both are
    ``None`` for a version-wide issue such as a stale snapshot.
    """

    code: str
    message: str
    assignment_id: int | None = None
    schedule_version_requirement_id: int | None = None


@dataclass(frozen=True, slots=True)
class FinalizationReadinessResult:
    """The verdict and everything behind it.

    Task 23's staleness result is carried whole rather than reduced to a
    boolean: the caller refusing finalization needs to say *which* requirements
    drifted, and that answer already exists in a reviewed shape. Nothing here
    is persisted -- readiness is derived state, recomputed on demand, and
    storing it would immediately be a second thing that can go stale.
    """

    staleness: ScheduleVersionStalenessResult
    issues: tuple[FinalizationIssue, ...]

    @property
    def is_ready(self) -> bool:
        """Fresh snapshot **and** no issues at all.

        Staleness is re-checked here rather than trusted to have produced an
        issue, so the two fields can never disagree about the verdict.
        """
        return not self.staleness.is_stale and not self.issues


@dataclass(frozen=True, slots=True)
class _CurrentFacts:
    """Everything the per-assignment checks need, fetched once as sets.

    **Why this exists.** Each check below asks the same three questions of a
    different assignment: is this membership qualified for this role, did they
    answer UNAVAILABLE for this event, and what is their serving maximum. Asked
    one assignment at a time that is three round trips per assignment, and
    against a hosted database round trips *are* the response time -- a
    sixty-five position schedule was issuing hundreds of them.

    Asked once for the whole version they are three queries, and the answers
    are identical: these are simple lookups by key, so fetching the set and
    indexing it in memory decides exactly what the individual queries decided.
    Nothing is cached beyond this one call, and nothing is shared between
    requests -- a stale answer here would be a wrong finalization verdict.

    Absence is preserved as absence. ``qualifications`` and ``availability``
    hold only rows that exist, so a missing key still means "no row", which is
    the distinction :func:`_current_blockers` depends on.
    """

    qualifications: dict[tuple[int, int], RoleQualification]
    availability: dict[tuple[int, int], Availability]
    serving_limits: dict[int, int]
    #: Canonically ordered ``(membership_a_id, membership_b_id)`` pairs
    #: currently configured for this period. Read here rather than per pair,
    #: for the same reason as everything else in this class.
    same_date_pairs: tuple[tuple[int, int], ...]
    #: ``(person_id, snapshot event_date) -> conflict result``, with an entry
    #: for every pair asked about, so absence never has to be interpreted.
    conflicts: dict[tuple[int, datetime.date], SundayConflictResult]
    #: The ministry's event sequence and the history before this period, or
    #: ``None`` when the period configures no event-gap rule at all -- which
    #: is the ordinary case, and the one that loads no history.
    event_sequence: MinistryEventSequence | None = None
    #: The member-group caps **currently** configured for this period, with
    #: each group's members. Empty is the ordinary case (Task 74).
    member_group_caps: tuple[MemberGroupCapConfig, ...] = ()
    #: The same-event support requirements **currently** configured for this
    #: period, with each subject's approved supporters. Empty is the ordinary
    #: case (Task 74).
    support_requirements: tuple[SupportRequirementConfig, ...] = ()


def _fetch_current_facts(
    session: Session,
    *,
    scheduling_period_id: int,
    schedule_version_id: int,
    assignments: Sequence[Assignment],
    requirements: Sequence[ScheduleVersionRequirement],
) -> _CurrentFacts:
    """A fixed number of set-based reads, replacing several per assignment."""
    membership_ids = sorted({a.ministry_membership_id for a in assignments})
    if not membership_ids:
        # No assignments means no gate below has anything to evaluate --
        # including the pair gate, which is about two assignments coinciding,
        # and the gap gate, which is about two assignments being too close.
        return _CurrentFacts(
            qualifications={}, availability={}, serving_limits={},
            conflicts={}, same_date_pairs=(),
        )

    role_ids = sorted({r.ministry_role_id for r in requirements})
    event_ids = sorted({a.event_id for a in assignments})

    qualifications = {
        (row.ministry_membership_id, row.ministry_role_id): row
        for row in session.execute(
            select(RoleQualification).where(
                RoleQualification.ministry_membership_id.in_(membership_ids),
                RoleQualification.ministry_role_id.in_(role_ids),
            )
        ).scalars()
    }
    availability = {
        (row.ministry_membership_id, row.event_id): row
        for row in session.execute(
            select(Availability).where(
                Availability.ministry_membership_id.in_(membership_ids),
                Availability.event_id.in_(event_ids),
            )
        ).scalars()
    }
    serving_limits = {
        row.ministry_membership_id: row.max_assignments
        for row in session.execute(
            select(MembershipServingLimit).where(
                MembershipServingLimit.scheduling_period_id == scheduling_period_id,
                MembershipServingLimit.ministry_membership_id.in_(membership_ids),
            )
        ).scalars()
    }
    # The church-wide conflict rule, asked once for every (person, date) pair
    # this version touches. The rule itself is untouched -- this is the same
    # query the one-person function runs, widened to the whole set.
    person_ids = sorted({a.ministry_membership.person_id for a in assignments})
    conflict_dates = sorted({r.event_date for r in requirements})
    ministry_ids = {r.ministry_id for r in requirements}
    conflicts: dict[tuple[int, datetime.date], SundayConflictResult] = {}
    if person_ids and conflict_dates and len(ministry_ids) == 1:
        conflicts = get_sunday_conflicts_for(
            session,
            person_ids=person_ids,
            conflict_dates=conflict_dates,
            target_ministry_id=next(iter(ministry_ids)),
        )

    # Every pair configured for the period, not only those naming an assigned
    # membership: the filtering is done in the gate, where "both are assigned
    # on one date" is the actual question, and a period holds a handful of
    # rules at most.
    same_date_pairs = tuple(
        (row.membership_a_id, row.membership_b_id)
        for row in session.execute(
            _same_date_exclusions_statement(scheduling_period_id)
        ).scalars()
    )

    # The event-gap rule's sequence, read once. The version's own events are
    # handed over from ``requirements``, which this function already holds, so
    # the loader spends its queries on the rule and the earlier history rather
    # than re-reading the snapshot. Ordered here exactly as
    # ``load_scheduled_event_sequence`` orders it -- by ``(event_date,
    # event_id)`` -- because the caller supplying the sequence is also
    # promising it is the canonical one.
    ministry_ids = {r.ministry_id for r in requirements}
    event_sequence = None
    if len(ministry_ids) == 1:
        event_sequence = load_event_gap_sequence(
            session,
            scheduling_period_id=scheduling_period_id,
            ministry_id=next(iter(ministry_ids)),
            schedule_version_id=schedule_version_id,
            scheduled_events=_snapshot_event_sequence(requirements),
        )

    # Task 74's two rules, read once for the whole version like everything else
    # in this class, and read **current** so a cap or requirement a head
    # configured after the draft was built is caught immediately. Two queries
    # each when the rule is configured, one each when it is not.
    member_group_caps = load_member_group_caps(
        session, scheduling_period_id=scheduling_period_id
    )
    support_requirements = load_support_requirements(
        session, scheduling_period_id=scheduling_period_id
    )

    return _CurrentFacts(
        qualifications=qualifications,
        availability=availability,
        serving_limits=serving_limits,
        conflicts=conflicts,
        same_date_pairs=same_date_pairs,
        event_sequence=event_sequence,
        member_group_caps=member_group_caps,
        support_requirements=support_requirements,
    )


def _snapshot_event_sequence(
    requirements: Sequence[ScheduleVersionRequirement],
) -> tuple[tuple[int, datetime.date], ...]:
    """The version's distinct snapshot events, canonically ordered.

    Derived from rows already in hand rather than queried again, and ordered by
    ``(event_date, event_id)`` -- the ordering the whole gap rule is defined
    against. The snapshot date is used, never the live ``event`` row's, exactly
    as everywhere else in this module.
    """
    return tuple(
        (event_id, event_date)
        for event_date, event_id in sorted(
            {(r.event_date, r.event_id) for r in requirements}
        )
    )


def get_finalization_readiness(
    session: Session,
    *,
    version: ScheduleVersion,
) -> FinalizationReadinessResult:
    """Report whether ``version`` currently satisfies the finalization gates.

    **Read-only.** Only ``session.execute()`` and relationship loads -- never
    ``add()``, ``delete()``, ``flush()``, ``commit()`` or ``rollback()``, no
    AuditEvent, and no second Session. Nothing is mutated, including
    assignments that turn out to be invalid: reporting is this function's whole
    job, and repairing is a decision for a person.

    **Status-agnostic** by design: a DRAFT, REVIEW or FINALIZED version can all
    be inspected descriptively. Requiring REVIEW is Task 27's rule, and
    embedding it here would stop a head from checking a draft before
    submitting it.

    Every gate is evaluated; none short-circuits. A stale version still gets
    its assignments checked, because a caller fixing problems wants the whole
    list, not the first one.

    :raises InvalidOperationError: ``version`` lacks the persisted context this
        needs (``id``, ``schedule_id``, ``scheduling_period_id``) -- refused
        outright rather than reported as ready on the strength of queries that
        matched nothing.
    """
    _require_version_context(version)

    staleness = get_schedule_version_staleness(session, version=version)

    issues: list[FinalizationIssue] = []
    if staleness.is_stale:
        issues.append(
            FinalizationIssue(
                code=ISSUE_STALE_REQUIREMENT_SNAPSHOT,
                message=(
                    "the requirement snapshot no longer matches current"
                    f" scheduling input ({len(staleness.current_only)} added,"
                    f" {len(staleness.snapshot_only)} removed or changed);"
                    " a fresh version is needed"
                ),
            )
        )

    requirements = _fetch_requirements(session, schedule_version_id=version.id)
    assignments = _fetch_assignments(session, schedule_version_id=version.id)
    filling = _group_by_requirement(assignments)
    history = _resolve_override_history(session, assignments=assignments)
    facts = _fetch_current_facts(
        session,
        scheduling_period_id=version.scheduling_period_id,
        schedule_version_id=version.id,
        assignments=assignments,
        requirements=requirements,
    )

    for requirement in requirements:
        issues.extend(
            _check_requirement(
                session,
                requirement=requirement,
                assignments=filling.get(requirement.id, ()),
                history=history,
                facts=facts,
            )
        )

    issues.extend(
        _check_serving_limits(assignments=assignments, facts=facts)
    )
    issues.extend(
        _check_same_date_exclusions(
            assignments=assignments, requirements=requirements, facts=facts
        )
    )
    issues.extend(_check_event_gap(assignments=assignments, facts=facts))
    issues.extend(
        _check_member_group_event_limits(
            assignments=assignments, requirements=requirements, facts=facts
        )
    )
    issues.extend(
        _check_same_event_support(
            assignments=assignments, requirements=requirements, facts=facts
        )
    )

    return FinalizationReadinessResult(staleness=staleness, issues=tuple(issues))


def _memberships_by_event(
    assignments: Sequence[Assignment],
) -> dict[int, set[int]]:
    """``event_id -> the memberships on its roster`` in this version.

    Shared by gates 7 and 8, which both reason about one **event's** crew
    rather than a calendar date -- a period may hold two services on one Sunday,
    and collapsing them would make both rules wrong in the same way.

    Memberships, not assignments: the one-position-per-event rule (§10) already
    makes those the same number, and counting rows would be a different rule.
    """
    by_event: dict[int, set[int]] = {}
    for assignment in assignments:
        by_event.setdefault(assignment.event_id, set()).add(
            assignment.ministry_membership_id
        )
    return by_event


def _snapshot_dates_by_event(
    requirements: Sequence[ScheduleVersionRequirement],
) -> dict[int, datetime.date]:
    """``event_id -> its snapshot date``, from rows already in hand.

    The **snapshot** date, never the live ``event`` row's, exactly as everywhere
    else in this module -- and derived from ``requirements`` rather than reached
    through each assignment's relationship, which would be a lazy load per
    assignment on a path that exists to issue a fixed number of reads.
    """
    return {
        requirement.event_id: requirement.event_date
        for requirement in requirements
    }


def _check_member_group_event_limits(
    *,
    assignments: Sequence[Assignment],
    requirements: Sequence[ScheduleVersionRequirement],
    facts: _CurrentFacts,
) -> list[FinalizationIssue]:
    """Gate 7: no event carries more of a group than its cap allows.

    Version-wide rather than per-requirement, like gates 4, 5 and 6, and **one
    issue per (group, event)** rather than one per assignment. The problem is
    the composition of the crew, not any one row: emitting an issue against each
    of three members would say the same thing three times while implying that
    one in particular is wrong, which is exactly the decision this module must
    leave to a head.

    **Counted within this version only**, and per event: a predecessor version's
    assignments are history, and a group member at the *other* service on the
    same Sunday is not on this crew.

    **Evaluated against the currently configured cap**, so a cap a head records
    or lowers after a draft was built is caught immediately -- with nothing
    deleted. That is the whole point of reporting rather than repairing.

    **An override authorizes nothing here.** The cap is not one of Task 22's
    bounded overridable blockers, so no historical ``overridden_blockers`` payload is
    consulted and none could excuse it.

    The message names the group, the date and the numbers, and says nothing
    whatsoever about what the category *means* -- the system does not know and
    must not imply (requirements §4.7). Groups and events are reported in a
    deterministic order so the issue list is stable for a given database state.
    """
    if not facts.member_group_caps:
        return []

    rosters = _memberships_by_event(assignments)
    dates = _snapshot_dates_by_event(requirements)

    issues: list[FinalizationIssue] = []
    for cap in facts.member_group_caps:
        for event_id in sorted(rosters):
            present = count_group_members_present(cap, rosters[event_id])
            if present <= cap.max_per_event:
                continue
            when = dates.get(event_id)
            issues.append(
                FinalizationIssue(
                    code=ISSUE_MEMBER_GROUP_EVENT_LIMIT_CONFLICT,
                    message=(
                        f"{present} members of the member group"
                        f" {cap.member_group_name} are assigned to the event on"
                        f" {when.isoformat() if when else 'an unknown date'},"
                        f" but {describe_member_group_cap(cap.member_group_name, cap.max_per_event)}"
                        " in this period; remove one of the assignments, or"
                        " raise the limit"
                    ),
                )
            )
    return issues


def _check_same_event_support(
    *,
    assignments: Sequence[Assignment],
    requirements: Sequence[ScheduleVersionRequirement],
    facts: _CurrentFacts,
) -> list[FinalizationIssue]:
    """Gate 8: nobody with a support requirement serves an event without it.

    Version-wide, and **one issue per (subject, event)**: the problem is that a
    crew lacks somebody, not that any particular row is wrong.

    **This is the gate that catches what removal deliberately allows.**
    :func:`app.services.assignment.remove_assignment` refuses almost nothing --
    it is the cleanup path, and re-validating it would leave a head unable to
    fix a schedule by hand -- so taking a supporter off an event is permitted
    and leaves the subject unsupported. Nothing deletes the subject's assignment
    in response; the version simply cannot be finalized until a head puts it
    right.

    **Counted within this version only**, and per event -- a supporter at the
    other service on the same Sunday is not on this crew, which is the whole
    difference between this rule and gate 5's date rule.

    **Evaluated against the currently configured requirement**, so a rule a head
    records or tightens after a draft was built is caught immediately. **An
    override authorizes nothing here.**

    The message names the subject, the date and the numbers, and says nothing
    about *why* the support is needed -- the system does not know and must not
    imply (requirements §4.7).
    """
    if not facts.support_requirements:
        return []

    rosters = _memberships_by_event(assignments)
    dates = _snapshot_dates_by_event(requirements)
    names: dict[int, str] = {}
    for assignment in assignments:
        names.setdefault(
            assignment.ministry_membership_id,
            assignment.ministry_membership.person.display_name,
        )

    issues: list[FinalizationIssue] = []
    for requirement in facts.support_requirements:
        subject_id = requirement.subject_membership_id
        for event_id in sorted(rosters):
            roster = rosters[event_id]
            if subject_id not in roster:
                continue  # subject absent, so the rule says nothing
            present = count_supporters_present(requirement, roster)
            if present >= requirement.min_supporters:
                continue
            when = dates.get(event_id)
            issues.append(
                FinalizationIssue(
                    code=ISSUE_SAME_EVENT_SUPPORT_CONFLICT,
                    message=(
                        f"{names.get(subject_id, f'membership {subject_id}')}"
                        " is assigned to the event on"
                        f" {when.isoformat() if when else 'an unknown date'}"
                        f" with {present} of the {requirement.min_supporters}"
                        " required approved supporting member(s) also assigned"
                        f" there, but {describe_support_requirement(requirement.min_supporters)}"
                        " in this period; assign an approved supporting member"
                        " to that event, remove this assignment, or change the"
                        " requirement"
                    ),
                )
            )
    return issues


def _check_event_gap(
    *,
    assignments: Sequence[Assignment],
    facts: _CurrentFacts,
) -> list[FinalizationIssue]:
    """Gate 6: nobody serves again sooner than the period's rule allows.

    Version-wide rather than per-requirement, like gates 4 and 5, because the
    rule is about a person across a sequence of events rather than about one
    position -- and **one issue per offending pair of events**, not one per
    assignment. The problem is the coincidence of two placements, and emitting
    an issue against each of them would say the same thing twice while implying
    that one in particular is wrong, which is exactly the decision this module
    must leave to a head.

    Only *adjacent* occupied events are compared, which is not a shortcut: if
    two occupied events are closer than the rule allows, some adjacent pair of
    occupied events is too, so the list is complete and carries no duplicates
    of the same underlying violation.

    **Counted within this version only**, plus the authoritative history the
    sequence already carries. A predecessor version's assignments are history,
    not commitments anybody is doing twice.

    **Evaluated against the currently configured rule**, so a gap a head
    records or tightens after a draft was built is caught immediately -- with
    nothing deleted.

    **An override authorizes nothing here.** The gap rule is not one of Task
    22's bounded overridable blockers, so no historical ``overridden_blockers``
    payload is consulted and none could excuse it.

    Members are reported in a deterministic order so the issue list is stable
    for a given database state.
    """
    sequence = facts.event_sequence
    if sequence is None or not sequence.events:
        return []

    events_by_membership: dict[int, set[int]] = {}
    names: dict[int, str] = {}
    for assignment in assignments:
        events_by_membership.setdefault(
            assignment.ministry_membership_id, set()
        ).add(assignment.event_id)
        names.setdefault(
            assignment.ministry_membership_id,
            assignment.ministry_membership.person.display_name,
        )

    issues: list[FinalizationIssue] = []
    for membership_id in sorted(events_by_membership):
        occupied = sequence.occupied_events_for(
            membership_id, version_event_ids=events_by_membership[membership_id]
        )
        for earlier, later in sequence.conflicting_event_dates(occupied):
            issues.append(
                FinalizationIssue(
                    code=ISSUE_MIN_EVENT_GAP_CONFLICT,
                    message=(
                        f"{names[membership_id]} serves on"
                        f" {earlier.isoformat()} and again on"
                        f" {later.isoformat()} with too few of this ministry's"
                        " events in between:"
                        f" {describe_event_gap(sequence.min_intervening_events)}."
                        " Remove one of the assignments, or change the rule for"
                        " this period"
                    ),
                )
            )
    return issues


def _check_serving_limits(
    *,
    assignments: Sequence[Assignment],
    facts: _CurrentFacts,
) -> list[FinalizationIssue]:
    """Gate 4: nobody over their currently configured maximum for this period.

    Version-wide rather than per-requirement, because the rule is about a
    person across the whole period rather than about one position -- so it is
    evaluated once, after the per-requirement pass, and reports one issue per
    member rather than one per assignment. Attaching it to an arbitrary one of
    their assignments would suggest that assignment is the problem, when what
    is wrong is the total.

    Counted **within this version only**: a predecessor's assignments are
    history, and adding them in would report a successor as doubly committed
    for work nobody is doing.

    Members are reported in a deterministic order so the issue list is stable
    for a given database state.
    """
    counts: dict[int, int] = {}
    for assignment in assignments:
        counts[assignment.ministry_membership_id] = (
            counts.get(assignment.ministry_membership_id, 0) + 1
        )

    issues: list[FinalizationIssue] = []
    for membership_id in sorted(counts):
        # From the per-request prefetch, which reads the same rows for this
        # period that ``get_serving_limit`` read one membership at a time.
        maximum = facts.serving_limits.get(membership_id)
        if maximum is None:
            continue
        held = counts[membership_id]
        if held > maximum:
            issues.append(
                FinalizationIssue(
                    code=ISSUE_EXCEEDS_SERVING_LIMIT,
                    message=(
                        f"membership {membership_id} holds {held} assignments"
                        f" in this version but their serving maximum for this"
                        f" period is {maximum}; remove an assignment or agree a"
                        " higher maximum with the volunteer"
                    ),
                )
            )
    return issues


def _check_same_date_exclusions(
    *,
    assignments: Sequence[Assignment],
    requirements: Sequence[ScheduleVersionRequirement],
    facts: _CurrentFacts,
) -> list[FinalizationIssue]:
    """Gate 5: no linked pair both on the roster for one calendar date.

    Version-wide, and **one issue per (pair, date)** rather than one per
    assignment. The problem is the coincidence, not either row: emitting an
    issue against each of the two assignments would say the same thing twice
    and imply that one of them in particular is wrong, when which one a head
    removes is exactly the decision this module must leave to them. On a date
    with two events and several positions, a naive per-assignment reading
    could produce four issues for one problem; this produces one.

    **Dates come from the snapshot** ``schedule_version_requirement.event_date``,
    never the live ``event`` row, exactly as everywhere else here -- and that
    is what makes this a *date* rule rather than an event rule: two services
    on one Sunday are one date, and a pair split across them is still a
    conflict.

    **Evaluated against the currently configured rules**, so a constraint a
    head adds after a draft was built is caught immediately -- with nothing
    deleted. That is the whole point of reporting rather than repairing:
    removing one of two assignments to satisfy a rule recorded five minutes
    ago would destroy a decision somebody made, possibly one a volunteer is
    counting on.

    **An override authorizes nothing here.** The exclusion is not one of
    Task 22's bounded overridable blockers, so no historical ``overridden_blockers``
    payload is consulted and none could excuse it.

    The message names the two people, the date and the remedy, and says
    nothing whatsoever about *why* they are linked -- the system does not know
    and must not imply (requirements §4.7).
    """
    if not facts.same_date_pairs:
        return []

    date_by_requirement = {
        requirement.id: requirement.event_date for requirement in requirements
    }
    dates_by_membership: dict[int, set] = {}
    names: dict[int, str] = {}
    for assignment in assignments:
        event_date = date_by_requirement.get(
            assignment.schedule_version_requirement_id
        )
        if event_date is None:
            # An assignment whose requirement is not in this version's
            # snapshot cannot exist -- the composite key forbids it -- so
            # there is no date to reason about and nothing to invent.
            continue
        dates_by_membership.setdefault(
            assignment.ministry_membership_id, set()
        ).add(event_date)
        names.setdefault(
            assignment.ministry_membership_id,
            assignment.ministry_membership.person.display_name,
        )

    issues: list[FinalizationIssue] = []
    for membership_a_id, membership_b_id in facts.same_date_pairs:
        shared = dates_by_membership.get(
            membership_a_id, set()
        ) & dates_by_membership.get(membership_b_id, set())
        for event_date in sorted(shared):
            issues.append(
                FinalizationIssue(
                    code=ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT,
                    message=(
                        f"{names[membership_a_id]} and {names[membership_b_id]}"
                        f" are both assigned on {event_date.isoformat()}, but a"
                        " same-date exclusion is configured for them in this"
                        " period; remove one of the assignments, or clear the"
                        " constraint"
                    ),
                )
            )
    return issues


# --------------------------------------------------------------------------
# Per-requirement evaluation
# --------------------------------------------------------------------------


def _check_requirement(
    session: Session,
    *,
    requirement: ScheduleVersionRequirement,
    assignments: Sequence[Assignment],
    history: dict[int, _OverrideHistory],
    facts: _CurrentFacts,
) -> list[FinalizationIssue]:
    """Completeness and capacity for one requirement, then each assignment.

    Rows are visited in id order (the queries impose it), and blocker codes are
    emitted sorted, so the issue list is deterministic for a given database
    state.
    """
    issues: list[FinalizationIssue] = []
    assigned = len(assignments)

    if assigned < requirement.required_count:
        issues.append(
            FinalizationIssue(
                code=ISSUE_UNFILLED_REQUIREMENT,
                message=(
                    f"{_requirement_label(requirement)} needs"
                    f" {requirement.required_count} and has {assigned}"
                ),
                schedule_version_requirement_id=requirement.id,
            )
        )

    issues.extend(
        _check_capacity(requirement=requirement, assignments=assignments, history=history)
    )

    for assignment in assignments:
        issues.extend(
            _check_assignment(
                session,
                requirement=requirement,
                assignment=assignment,
                history=history[assignment.id],
                facts=facts,
            )
        )
    return issues


def _check_capacity(
    *,
    requirement: ScheduleVersionRequirement,
    assignments: Sequence[Assignment],
    history: dict[int, _OverrideHistory],
) -> list[FinalizationIssue]:
    """Overfill is legitimate only as far as it was authorized.

    Task 22 lets a requirement exceed ``required_count`` only through an
    explicit capacity override, so ``excess`` extra people need at least
    ``excess`` assignments carrying the capacity blocker in their history.

    Counted, never matched positionally: the rows carry no ordering that says
    which one is "the extra", and picking by id or insertion order would make
    the verdict depend on an accident of sequence. If two of three people were
    authorized past a full requirement, the requirement is authorized to hold
    three, whichever two they were.
    """
    excess = len(assignments) - requirement.required_count
    if excess <= 0:
        return []

    authorized = sum(
        1
        for assignment in assignments
        if BLOCKER_CAPACITY_FULL in history[assignment.id].blockers
    )
    if authorized >= excess:
        return []

    return [
        FinalizationIssue(
            code=ISSUE_UNAUTHORIZED_OVERFILL,
            message=(
                f"{_requirement_label(requirement)} needs"
                f" {requirement.required_count} and has {len(assignments)},"
                f" but only {authorized} of the {excess} extra"
                " assignments were authorized past a full requirement"
            ),
            schedule_version_requirement_id=requirement.id,
        )
    ]


def _check_assignment(
    session: Session,
    *,
    requirement: ScheduleVersionRequirement,
    assignment: Assignment,
    history: _OverrideHistory,
    facts: _CurrentFacts,
) -> list[FinalizationIssue]:
    """The absolute checks, the override-history integrity checks, and the
    current-versus-authorized blocker comparison, for one assignment.
    """
    issues: list[FinalizationIssue] = []
    membership = assignment.ministry_membership
    person = membership.person

    # -- Absolute: never overridable, at creation (Task 22) or now. --
    if membership.deactivated_at is not None:
        issues.append(
            _assignment_issue(
                ISSUE_INACTIVE_MEMBERSHIP,
                f"{person.display_name} is no longer an active member of this ministry",
                assignment, requirement,
            )
        )
    if person.deactivated_at is not None:
        issues.append(
            _assignment_issue(
                ISSUE_INACTIVE_PERSON,
                f"{person.display_name} has been deactivated",
                assignment, requirement,
            )
        )
    # The event reached through the requirement, whose composite foreign key
    # already pins it to the assignment's own event_id -- no second query to
    # re-prove what the database guarantees.
    if requirement.event.cancelled_at is not None:
        issues.append(
            _assignment_issue(
                ISSUE_CANCELLED_EVENT,
                f"the event on {requirement.event_date.isoformat()} has been cancelled",
                assignment, requirement,
            )
        )
    # **The church-wide hard rule, checked with the absolutes and before any
    # override history is consulted** (Task 79's final correction). Until then
    # this was one of the overridable blockers below, so a row whose audit
    # named ``sunday_conflict`` passed the gate. Nothing an override can say
    # reaches this branch now: it is evaluated for every assignment, override
    # or not, and its issue is appended unconditionally.
    if _has_cross_ministry_conflict(
        session, requirement=requirement, membership=membership, facts=facts
    ):
        issues.append(
            _assignment_issue(
                ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT,
                f"{person.display_name} is also committed to another ministry"
                f" on {requirement.event_date.isoformat()}, and one person may"
                " serve at most one ministry on the same day. This is not"
                " overridable: remove one of the two assignments.",
                assignment, requirement,
            )
        )

    # -- Override history must be trustworthy before it can authorize. --
    issues.extend(history.issues_for(assignment, requirement))

    # -- Current overridable conditions versus what was authorized. --
    current = _current_blockers(
        session, requirement=requirement, membership=membership, facts=facts
    )
    if assignment.is_override:
        unauthorized = ", and this was not among the blockers overridden"
    else:
        unauthorized = ", and this assignment carries no override"
    for code in sorted(current - history.blockers):
        issues.append(
            _assignment_issue(
                ISSUE_UNAUTHORIZED_BLOCKER,
                f"{person.display_name}: {BLOCKER_DESCRIPTIONS[code]}{unauthorized}",
                assignment, requirement,
            )
        )
    return issues


def _current_blockers(
    session: Session,
    *,
    requirement: ScheduleVersionRequirement,
    membership: MinistryMembership,
    facts: _CurrentFacts,
) -> frozenset[str]:
    """Task 22's overridable conditions, re-evaluated against today.

    **Aggregate capacity is deliberately excluded**: whether a requirement is
    over its count is a property of the requirement, not of any one assignment,
    and is handled once in :func:`_check_capacity`. Including it here would
    make every assignment on a legitimately overfilled requirement report a
    blocker it cannot individually answer for.

    **The church-wide same-Sunday conflict is also excluded, and that is Task
    79's correction.** It used to be computed here and compared against what an
    override authorized, which is exactly how an audited override let a
    contradictory schedule finalize. It is now an absolute gate in
    :func:`_check_assignment`, reported regardless of override history.

    The remaining three checks are Task 22's, in the same senses: a missing
    qualification row and an explicit ``False`` are one outcome, and a missing
    Availability row is "no response" and is not a blocker.
    """
    blockers: set[str] = set()

    if requirement.ministry_role.deactivated_at is not None:
        blockers.add(BLOCKER_ROLE_DEACTIVATED)

    # Read from the per-request prefetch rather than querying per assignment.
    # A missing key means no row, exactly as a query returning nothing did.
    qualification = facts.qualifications.get(
        (membership.id, requirement.ministry_role_id)
    )
    if qualification is None or not qualification.is_qualified:
        blockers.add(BLOCKER_NOT_QUALIFIED)

    availability = facts.availability.get((membership.id, requirement.event_id))
    if availability is not None and availability.availability_state == AVAILABILITY_UNAVAILABLE:
        blockers.add(BLOCKER_UNAVAILABLE)

    return frozenset(blockers)


def _has_cross_ministry_conflict(
    session: Session,
    *,
    requirement: ScheduleVersionRequirement,
    membership: MinistryMembership,
    facts: _CurrentFacts,
) -> bool:
    """Is another ministry already committed to this Person on this date?

    From the per-request batch where it covers this pair, and from the one-pair
    query otherwise -- a version spanning more than one ministry is not
    something the batch's single ``target_ministry_id`` can describe, and
    falling back keeps the answer right rather than convenient.

    The conflict date is the requirement's **snapshot** date, never the current
    event's, so a moved event cannot silently relocate the question; and the
    subject is ``membership.person_id``, so the rule is about the human.
    """
    conflict = facts.conflicts.get((membership.person_id, requirement.event_date))
    if conflict is None:
        conflict = get_person_sunday_conflicts(
            session,
            person_id=membership.person_id,
            conflict_date=requirement.event_date,
            target_ministry_id=requirement.ministry_id,
        )
    return conflict.is_blocked


# --------------------------------------------------------------------------
# Override history
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _OverrideHistory:
    """What an assignment's creation audit actually authorized.

    ``blockers`` is empty whenever nothing can be trusted -- a normal
    assignment, a missing audit, an ambiguous one, or a malformed payload --
    so the comparison in :func:`_check_assignment` needs no special cases:
    an unauthorized assignment simply covers nothing.
    """

    blockers: frozenset[str]
    problem: str | None = None

    def issues_for(
        self, assignment: Assignment, requirement: ScheduleVersionRequirement
    ) -> list[FinalizationIssue]:
        if self.problem is None:
            return []
        code, message = _HISTORY_PROBLEMS[self.problem]
        return [_assignment_issue(code, message, assignment, requirement)]


_HISTORY_MISSING = "missing"
_HISTORY_AMBIGUOUS = "ambiguous"
_HISTORY_INVALID = "invalid"

_HISTORY_PROBLEMS = {
    _HISTORY_MISSING: (
        ISSUE_MISSING_OVERRIDE_AUDIT,
        "this assignment is marked as an override but has no override audit"
        " event, so nothing records what was authorized",
    ),
    _HISTORY_AMBIGUOUS: (
        ISSUE_AMBIGUOUS_OVERRIDE_AUDIT,
        "this assignment has more than one override audit event, so what was"
        " authorized cannot be determined",
    ),
    _HISTORY_INVALID: (
        ISSUE_INVALID_OVERRIDE_PAYLOAD,
        "this assignment's override audit event does not record a valid list"
        " of overridden blockers",
    ),
}

_NO_OVERRIDE = _OverrideHistory(blockers=frozenset())


def _resolve_override_history(
    session: Session, *, assignments: Sequence[Assignment]
) -> dict[int, _OverrideHistory]:
    """One batched audit read for every override assignment in the version.

    Normal assignments are not looked up at all: their authorization is
    empty by definition, and hunting for override history they should not have
    would invite trusting a stray row.
    """
    override_ids = [a.id for a in assignments if a.is_override]
    history: dict[int, _OverrideHistory] = {
        a.id: _NO_OVERRIDE for a in assignments if not a.is_override
    }
    if not override_ids:
        return history

    rows = session.execute(_override_audit_statement(override_ids)).scalars().all()
    by_assignment: dict[int, list[AuditEvent]] = {}
    for row in rows:
        by_assignment.setdefault(row.target_id, []).append(row)

    for assignment_id in override_ids:
        history[assignment_id] = _interpret_override_audits(
            by_assignment.get(assignment_id, [])
        )
    return history


def _interpret_override_audits(rows: Sequence[AuditEvent]) -> _OverrideHistory:
    """Exactly one audit row, carrying a well-formed payload, authorizes."""
    if not rows:
        return _OverrideHistory(blockers=frozenset(), problem=_HISTORY_MISSING)
    if len(rows) > 1:
        return _OverrideHistory(blockers=frozenset(), problem=_HISTORY_AMBIGUOUS)

    payload = rows[0].after_values
    if not isinstance(payload, dict) or _OVERRIDDEN_BLOCKERS_KEY not in payload:
        return _OverrideHistory(blockers=frozenset(), problem=_HISTORY_INVALID)

    codes = payload[_OVERRIDDEN_BLOCKERS_KEY]
    if not _is_valid_blocker_list(codes):
        return _OverrideHistory(blockers=frozenset(), problem=_HISTORY_INVALID)

    # **Validated against the whole vocabulary, but authorized against only the
    # codes that may still authorize.** A row written while the same-Sunday
    # conflict was overridable is an intact record of a real decision -- so it
    # is not reported as corrupt -- and the intersection is what stops it
    # excusing the one rule that is now absolute. Belt and braces: that rule no
    # longer reaches the blocker comparison at all.
    return _OverrideHistory(blockers=frozenset(codes) & OVERRIDABLE_BLOCKERS)


def _is_valid_blocker_list(codes: Any) -> bool:
    """A JSON array of known blocker codes, and nothing else.

    Task 22 is the only writer of this payload and only ever writes codes from
    the bounded vocabulary, so an unrecognized value is corruption rather than
    a newer dialect -- and it must not be allowed to quietly authorize
    anything. Rejecting the whole payload (instead of ignoring the odd
    element) is the conservative reading: if part of the record is wrong,
    none of it can be relied on to say what a person approved.

    **Validated against** :data:`~app.services.assignment_policy.KNOWN_BLOCKERS`
    **, which is wider than what may authorize.** ``sunday_conflict`` was a
    legitimate entry until Task 79 made that rule absolute, and a row carrying
    it is a truthful record of a decision somebody made -- not corruption.
    Whether it still *permits* anything is a separate question, answered by the
    intersection in :func:`_interpret_override_audits`.
    """
    if isinstance(codes, (str, bytes)) or not isinstance(codes, (list, tuple)):
        return False
    return all(isinstance(code, str) and code in KNOWN_BLOCKERS for code in codes)


# --------------------------------------------------------------------------
# Queries -- plain selects, ordered so results are deterministic
# --------------------------------------------------------------------------


def _requirements_statement(
    schedule_version_id: int,
) -> Select[tuple[ScheduleVersionRequirement]]:
    return (
        select(ScheduleVersionRequirement)
        .where(ScheduleVersionRequirement.schedule_version_id == schedule_version_id)
        # Both are read for every requirement that has an assignment: the role
        # to see whether it has since been deactivated, the event to see
        # whether it has since been cancelled.
        .options(
            joinedload(ScheduleVersionRequirement.ministry_role),
            joinedload(ScheduleVersionRequirement.event),
        )
        .order_by(ScheduleVersionRequirement.id)
    )


def _fetch_requirements(
    session: Session, *, schedule_version_id: int
) -> Sequence[ScheduleVersionRequirement]:
    """Execution split from statement construction so the SQL is testable with
    no database (see ``tests/test_services_finalization_readiness.py``).
    """
    return session.execute(_requirements_statement(schedule_version_id)).scalars().all()


def _assignments_statement(schedule_version_id: int) -> Select[tuple[Assignment]]:
    """Every assignment of the version, with the rows each check actually reads.

    ``ministry_membership`` and its ``person`` are eager-loaded because
    :func:`_check_assignment` reads both for *every* assignment. Left lazy they
    cost one round trip each on first touch, and against a hosted database
    round trips are what this endpoint's latency is made of. Nothing here is
    loaded speculatively: both are on the checking path itself.
    """
    return (
        select(Assignment)
        .where(Assignment.schedule_version_id == schedule_version_id)
        .options(
            joinedload(Assignment.ministry_membership).joinedload(
                MinistryMembership.person
            )
        )
        .order_by(Assignment.id)
    )


def _fetch_assignments(
    session: Session, *, schedule_version_id: int
) -> Sequence[Assignment]:
    """Every assignment of the version in one read, rather than one query per
    requirement: V1 schedules hold roughly 65 requirements, and the grouping
    below is cheaper and clearer than 65 round trips.
    """
    return session.execute(_assignments_statement(schedule_version_id)).scalars().all()


def _override_audit_statement(assignment_ids: Sequence[int]) -> Select[tuple[AuditEvent]]:
    """The creation audit for each override assignment (audit §6.2).

    Matched on the action constant, never on a string literal, so a typo is an
    ImportError rather than an assignment that silently appears unauthorized.
    """
    return (
        select(AuditEvent)
        .where(
            AuditEvent.target_table == _ASSIGNMENT_TARGET_TABLE,
            AuditEvent.target_id.in_(assignment_ids),
            AuditEvent.action == ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
        )
        .order_by(AuditEvent.target_id, AuditEvent.id)
    )


def _same_date_exclusions_statement(
    scheduling_period_id: int,
) -> Select[tuple[MembershipSameDateExclusion]]:
    """Linked pairs configured for this period.

    Scoped by period alone: the row's composite foreign keys already guarantee
    the period and both memberships share a ministry, so this scopes by
    ministry for free -- and a rule belongs to one period and expires with it,
    so no other period's rows can leak in.
    """
    return (
        select(MembershipSameDateExclusion)
        .where(
            MembershipSameDateExclusion.scheduling_period_id
            == scheduling_period_id
        )
        .order_by(
            MembershipSameDateExclusion.membership_a_id,
            MembershipSameDateExclusion.membership_b_id,
        )
    )


def _qualification_statement(
    ministry_membership_id: int, ministry_role_id: int
) -> Select[tuple[RoleQualification]]:
    return select(RoleQualification).where(
        RoleQualification.ministry_membership_id == ministry_membership_id,
        RoleQualification.ministry_role_id == ministry_role_id,
    )


def _find_qualification(
    session: Session, *, ministry_membership_id: int, ministry_role_id: int
) -> RoleQualification | None:
    stmt = _qualification_statement(ministry_membership_id, ministry_role_id)
    return session.execute(stmt).scalar_one_or_none()


def _availability_statement(
    ministry_membership_id: int, event_id: int
) -> Select[tuple[Availability]]:
    return select(Availability).where(
        Availability.ministry_membership_id == ministry_membership_id,
        Availability.event_id == event_id,
    )


def _find_availability(
    session: Session, *, ministry_membership_id: int, event_id: int
) -> Availability | None:
    stmt = _availability_statement(ministry_membership_id, event_id)
    return session.execute(stmt).scalar_one_or_none()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _require_version_context(version: ScheduleVersion) -> None:
    if version.id is None:
        raise InvalidOperationError("version must be persisted (id is None)")
    if version.schedule_id is None:
        raise InvalidOperationError("version must have a schedule_id")
    if version.scheduling_period_id is None:
        raise InvalidOperationError("version must have a scheduling_period_id")


def _group_by_requirement(
    assignments: Iterable[Assignment],
) -> dict[int, list[Assignment]]:
    grouped: dict[int, list[Assignment]] = {}
    for assignment in assignments:
        grouped.setdefault(assignment.schedule_version_requirement_id, []).append(
            assignment
        )
    return grouped


def _assignment_issue(
    code: str,
    message: str,
    assignment: Assignment,
    requirement: ScheduleVersionRequirement,
) -> FinalizationIssue:
    return FinalizationIssue(
        code=code,
        message=f"{_requirement_label(requirement)}: {message}",
        assignment_id=assignment.id,
        schedule_version_requirement_id=requirement.id,
    )


def _requirement_label(requirement: ScheduleVersionRequirement) -> str:
    """Names the role and the **snapshot** date -- the date this version
    committed to, never the current event row's (§13).
    """
    return (
        f"{requirement.ministry_role.name}"
        f" on {requirement.event_date.isoformat()}"
    )
