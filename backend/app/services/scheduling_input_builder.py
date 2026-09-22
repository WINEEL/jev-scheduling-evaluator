"""Turning current persisted state into the solver's plain input.

Read-only. This is the boundary between the database and
:mod:`app.scheduling`: everything on the far side of it is plain Python
values, so the solver never holds a Session, never lazy-loads, and never has
to know which of its facts came from a snapshot and which from a live row.

**[APPROVED] The version's own snapshot is what gets scheduled** (§8). Dates,
roles and counts come from ``schedule_version_requirement``, never from
current ``staffing_requirement`` or ``event.event_date``. A version means what
it was built against, and a solver run must schedule for the same Sundays the
version committed to.

**Only a working, current version.** Status must be DRAFT or REVIEW, and no
newer version of the schedule may exist (queried fresh, as everywhere else in
this project). Building input for a FINALIZED version would invite a solver to
propose changes to a published schedule, and building it for a superseded one
would optimize a version nobody is working on. Neither status is ever mutated
here.

**Task 23's staleness comparison is the gate, and the only one.** If current
input has drifted from the snapshot, this refuses rather than quietly
scheduling stale requirements -- and rather than "repairing" the snapshot,
which is immutable history whose repair mechanism is a successor version
(Task 28). That gate is also what covers a cancelled event: a cancelled event
drops out of current requirements, so its snapshot row shows up as a
difference and the version reads as stale. Adding a second, independent
cancelled-event rule here would be a competing definition of the same fact --
one that could disagree with Task 23 -- so there isn't one.

Not done here, deliberately: authorization (the operation that later *runs*
scheduling owns it), any optimization, any mutation, and any interpretation of
``NO_RESPONSE`` -- this layer reports the three states and leaves the
per-ministry policy to the layer that consumes them (scheduling-input §8).
"""

from __future__ import annotations

import datetime
from types import MappingProxyType

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, MinistryRole, Person, RoleQualification
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import (
    Availability,
    MembershipSameDateExclusion,
    MembershipServingLimit,
    SchedulingPeriod,
)
from app.scheduling.input import (
    AdjacentEventInput,
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    LinkedMembershipPair,
    MemberGroupCap,
    RequirementInput,
    SameEventSupportRequirement,
    SchedulingInput,
)
from app.services.errors import InvalidOperationError
from app.services.event_gap import (
    get_min_intervening_events,
    load_adjacent_ministry_events,
)
from app.services.member_group import load_member_group_caps
from app.services.same_event_support import load_support_requirements
from app.services.schedule_staleness import get_schedule_version_staleness
from app.services.sunday_conflict import get_sunday_conflicts_for

__all__ = ["build_scheduling_input"]

_WORKING_STATUSES = (SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW)


def build_scheduling_input(
    session: Session,
    *,
    version: ScheduleVersion,
) -> SchedulingInput:
    """Collect everything a scheduling run for ``version`` needs.

    **Read-only.** Only ``session.execute()`` -- never ``add()``, ``delete()``,
    ``flush()``, ``commit()`` or ``rollback()``, no AuditEvent, and no second
    Session. Nothing about the version, its snapshot, its assignments or
    anybody's availability is changed by asking this question.

    **No authorization.** Whether the caller may schedule this ministry is the
    calling operation's decision, exactly as for the other low-level readers in
    this package (Tasks 21, 23, 26).

    Queries, in order -- every one set-based, none growing with the number of
    candidates or dates:

    1. the owning ``SchedulingPeriod`` (for the ministry, which
       ``ScheduleVersion`` does not carry);
    2. the snapshot requirements, joined to ``ministry_role`` for current role
       activity;
    3. active memberships joined to active people;
    4. one qualification query for every candidate x required role;
    5. one availability query for every candidate x snapshot event;
    6. this version's existing assignments;
    7. Task 21's church-wide conflict rule for every candidate x distinct
       snapshot date, through :func:`get_sunday_conflicts_for` -- two round
       trips, not one per pair;
    8. the linked-pair same-date exclusions configured for this period;
    9. this period's configured ``min_intervening_events`` (one query), and --
       **only when one is configured** -- the ministry events immediately
       before this version's first scheduled event and immediately after its
       last, plus one read of who serves at all of them (three more). An
       unconfigured period pays one query and loads no surrounding events at
       all.
    10. the member-group per-event caps configured for this period, and the
        memberships in exactly those groups (Task 74) -- one query, or two when
        a cap exists;
    11. the same-event support requirements configured for this period, and the
        approved supporters of exactly those -- one query, or two when a
        requirement exists.

    Step 7 was once an N-per-row read; it now calls the batched church-wide
    conflict entry point in :mod:`app.services.sunday_conflict`, which is built
    from the very statements the one-person function uses (widened ``=`` to
    ``IN``), so ADR 0003's authoritative-version resolution is still defined in
    exactly one place and the batched answer cannot drift from the per-person
    one. Finalization readiness (Task 49) already reads conflicts this way.

    :raises InvalidOperationError: ``version`` lacks the persisted context this
        needs; its status is not DRAFT or REVIEW; it has been superseded by a
        newer version; its owning period cannot be resolved; or its requirement
        snapshot is stale against current scheduling input (Task 23).
    """
    _require_version_context(version)
    _require_working_latest_version(session, version)

    staleness = get_schedule_version_staleness(session, version=version)
    if staleness.is_stale:
        raise InvalidOperationError(
            "cannot build scheduling input: this version's requirement"
            " snapshot no longer matches current scheduling input"
            f" ({len(staleness.current_only)} added,"
            f" {len(staleness.snapshot_only)} removed or changed);"
            " create a successor version to schedule against current"
            " configuration"
        )

    period = _resolve_scheduling_period(session, version)
    requirements = _build_requirements(session, version=version)
    candidates = _build_candidates(
        session,
        ministry_id=period.ministry_id,
        scheduling_period_id=period.id,
        requirements=requirements,
    )
    existing = _build_existing_assignments(session, version=version)
    exclusions = _build_same_date_exclusions(
        session, scheduling_period_id=period.id, ministry_id=period.ministry_id
    )
    min_intervening_events = get_min_intervening_events(
        session, scheduling_period_id=period.id
    )
    preceding, following = _build_adjacent_events(
        session,
        ministry_id=period.ministry_id,
        min_intervening_events=min_intervening_events,
        requirements=requirements,
    )
    member_group_caps = _build_member_group_caps(
        session, scheduling_period_id=period.id
    )
    support_requirements = _build_support_requirements(
        session, scheduling_period_id=period.id
    )

    return SchedulingInput(
        schedule_version_id=version.id,
        scheduling_period_id=version.scheduling_period_id,
        ministry_id=period.ministry_id,
        requirements=requirements,
        candidates=candidates,
        existing_assignments=existing,
        same_date_exclusions=exclusions,
        member_group_caps=member_group_caps,
        support_requirements=support_requirements,
        min_intervening_events=min_intervening_events,
        preceding_events=preceding,
        following_events=following,
    )


# --------------------------------------------------------------------------
# Version gates
# --------------------------------------------------------------------------


def _require_version_context(version: ScheduleVersion) -> None:
    """Each id checked explicitly, so a transient object is refused as such
    rather than becoming a query against ``NULL`` that quietly returns an
    empty, plausible-looking input.
    """
    for attribute in ("id", "schedule_id", "scheduling_period_id", "version_number"):
        if getattr(version, attribute) is None:
            raise InvalidOperationError(
                f"version must be persisted: {attribute} is None"
            )


def _require_working_latest_version(session: Session, version: ScheduleVersion) -> None:
    if version.status not in _WORKING_STATUSES:
        raise InvalidOperationError(
            "scheduling input can only be built for a DRAFT or REVIEW version,"
            f" and this one is {version.status}"
        )
    if _newer_version_exists(
        session, schedule_id=version.schedule_id, version_number=version.version_number
    ):
        raise InvalidOperationError(
            "cannot build scheduling input for a schedule version that has"
            " been superseded by a newer version"
        )


# --------------------------------------------------------------------------
# Building the pure values
# --------------------------------------------------------------------------


def _build_requirements(
    session: Session, *, version: ScheduleVersion
) -> tuple[RequirementInput, ...]:
    """The snapshot rows, plus each role's *current* activity.

    One join rather than a role lookup per requirement, and the role's
    ``deactivated_at`` is the only current-state value read: everything else
    is the snapshot's own (module docstring).
    """
    rows = _fetch_requirement_rows(session, schedule_version_id=version.id)
    requirements = [
        RequirementInput(
            requirement_id=row.requirement_id,
            event_id=row.event_id,
            # The snapshot's date. The live event row is never consulted here.
            event_date=row.event_date,
            ministry_role_id=row.ministry_role_id,
            ministry_id=row.ministry_id,
            required_count=row.required_count,
            role_is_active=row.role_deactivated_at is None,
        )
        for row in rows
    ]
    return tuple(sorted(requirements, key=lambda r: r.sort_key))


def _build_candidates(
    session: Session,
    *,
    ministry_id: int,
    scheduling_period_id: int,
    requirements: tuple[RequirementInput, ...],
) -> tuple[CandidateInput, ...]:
    """Active members of this ministry, with their current qualifications,
    stored availability answers, church-wide blocked dates and any configured
    serving maximum for this period.

    Scoped to the roles and events this version actually requires -- there is
    no reason to carry a qualification for a role nobody is being scheduled
    into, and a smaller input is a clearer one.

    The serving maximum is read **current**, like qualifications and
    availability and unlike the requirement snapshot: a limit the head changed
    this morning is the limit that applies to a run this afternoon
    (requirements §4.4.1).
    """
    membership_rows = _fetch_candidate_rows(session, ministry_id=ministry_id)
    membership_ids = [row.membership_id for row in membership_rows]
    role_ids = sorted({r.ministry_role_id for r in requirements})
    event_ids = sorted({r.event_id for r in requirements})
    dates = sorted({r.event_date for r in requirements})

    qualified = _fetch_qualified_pairs(
        session, membership_ids=membership_ids, ministry_role_ids=role_ids
    )
    availability = _fetch_availability(
        session, membership_ids=membership_ids, event_ids=event_ids
    )
    serving_limits = _fetch_serving_limits(
        session,
        membership_ids=membership_ids,
        scheduling_period_id=scheduling_period_id,
    )
    person_ids = sorted({row.person_id for row in membership_rows})
    blocked_by_person = _fetch_blocked_dates(
        session, person_ids=person_ids, ministry_id=ministry_id, dates=dates
    )

    candidates = []
    for row in membership_rows:
        blocked = blocked_by_person.get(row.person_id, frozenset())
        answers = {
            event_id: state
            for (membership_id, event_id), state in availability.items()
            if membership_id == row.membership_id
        }
        candidates.append(
            CandidateInput(
                membership_id=row.membership_id,
                person_id=row.person_id,
                display_name=row.display_name,
                qualified_role_ids=frozenset(
                    role_id
                    for (membership_id, role_id) in qualified
                    if membership_id == row.membership_id
                ),
                # Read-only: a caller cannot reach in and add an answer the
                # person never gave. Absent keys read as NO_RESPONSE through
                # CandidateInput.availability_for.
                availability_by_event=MappingProxyType(answers),
                blocked_dates=frozenset(blocked),
                # Absent means uncapped, and the absence is the whole
                # representation: there is no row meaning "unlimited" to read.
                max_assignments_in_period=serving_limits.get(row.membership_id),
            )
        )
    return tuple(sorted(candidates, key=lambda c: (c.person_id, c.membership_id)))


def _serving_limits_statement(
    membership_ids: list[int], scheduling_period_id: int
) -> Select:
    """Configured serving maxima for these members in **this** period.

    Scoped by ``scheduling_period_id``, never by membership alone: a limit
    belongs to one period and expires with it, so another period's row must
    not leak into this run (requirements §4.4.1). Scoping by period also makes
    the ministry scope automatic -- the row's composite foreign keys already
    guarantee the period and the membership share a ministry.
    """
    return select(
        MembershipServingLimit.ministry_membership_id.label("membership_id"),
        MembershipServingLimit.max_assignments.label("max_assignments"),
    ).where(
        MembershipServingLimit.scheduling_period_id == scheduling_period_id,
        MembershipServingLimit.ministry_membership_id.in_(membership_ids),
    )


def _fetch_serving_limits(
    session: Session, *, membership_ids: list[int], scheduling_period_id: int
) -> dict[int, int]:
    """``membership_id -> max_assignments``, empty when nobody has a limit.

    Execution split from statement construction for the same testing reason as
    the other fetchers here.
    """
    if not membership_ids:
        return {}
    stmt = _serving_limits_statement(membership_ids, scheduling_period_id)
    return {
        row.membership_id: row.max_assignments
        for row in session.execute(stmt).all()
    }


def _fetch_blocked_dates(
    session: Session,
    *,
    person_ids: list[int],
    ministry_id: int,
    dates: list[datetime.date],
) -> dict[int, frozenset[datetime.date]]:
    """Task 21's church-wide conflict rule for every candidate x snapshot date,
    in the two round trips :func:`get_sunday_conflicts_for` takes -- not the
    one-per-pair reads the one-person entry point would cost.

    The rule is unchanged: ``get_sunday_conflicts_for`` is built from the very
    statements :func:`get_person_sunday_conflicts` uses, widened from ``=`` to
    ``IN`` (ADR 0002/0003 authoritative-version resolution, the snapshot
    ``event_date``, the cross-ministry test, the cancelled-event exclusion),
    so batching re-derives nothing and can disagree with the per-person answer
    on nothing.

    ``dates`` are the requirements' **snapshot** dates, which are also the
    dates the solver will schedule for -- asking about any other date would
    answer a question nobody is asking. The result carries an entry for every
    candidate, empty where nothing blocks them.

    Keyed by ``person_id`` rather than ``membership_id``: a church-wide block
    is a fact about the person and the date, and two memberships of one person
    in this ministry read the same blocked set (as they did when this looped).
    """
    if not person_ids or not dates:
        return {person_id: frozenset() for person_id in person_ids}

    conflicts = get_sunday_conflicts_for(
        session,
        person_ids=person_ids,
        conflict_dates=dates,
        target_ministry_id=ministry_id,
    )
    blocked: dict[int, set[datetime.date]] = {
        person_id: set() for person_id in person_ids
    }
    for (person_id, conflict_date), result in conflicts.items():
        if result.is_blocked:
            blocked[person_id].add(conflict_date)
    return {person_id: frozenset(dates_) for person_id, dates_ in blocked.items()}


def _build_existing_assignments(
    session: Session, *, version: ScheduleVersion
) -> tuple[ExistingAssignmentInput, ...]:
    """Decisions this version already carries, scoped to it alone.

    Reduced to plain ids and the override flag: the row's stored reason and
    its audit history stay in the database, where Task 26 reads them at
    finalization. Nothing here is mutated, and nothing here says whether the
    solver may change them -- that is Task 31's question.
    """
    rows = _fetch_existing_assignment_rows(session, schedule_version_id=version.id)
    return tuple(
        ExistingAssignmentInput(
            assignment_id=row.assignment_id,
            requirement_id=row.requirement_id,
            membership_id=row.membership_id,
            event_id=row.event_id,
            is_override=row.is_override,
        )
        for row in rows
    )


def _build_same_date_exclusions(
    session: Session, *, scheduling_period_id: int, ministry_id: int
) -> tuple[LinkedMembershipPair, ...]:
    """The linked pairs configured for **this** period, as pure values.

    **The translation is the identity, and that is worth saying out loud.**
    The solver's candidate identity is ``membership_id`` -- the same identity
    :class:`CandidateInput` carries and the same one the persisted rule names
    -- so no mapping table is needed and none is invented. What the pure form
    adds is canonical ordering and immutability
    (:class:`~app.scheduling.input.LinkedMembershipPair`).

    Read **current**, like qualifications, availability and serving limits and
    unlike the requirement snapshot: a rule the head configured this morning
    is the rule that applies to a run this afternoon. Scoped by
    ``scheduling_period_id`` alone, because a rule belongs to one period and
    expires with it, and because the row's composite foreign keys already
    guarantee the period and both memberships share a ministry.

    **A pair naming a membership that is not a current candidate is kept, not
    dropped.** A member deactivated part-way through the period stops being a
    candidate while their rule stays configured; the solver treats such a pair
    as inert, because somebody with no variables and no existing assignment
    can never be present. Filtering here instead would mean this builder
    deciding a rule no longer applies, which is a head's decision.

    :raises InvalidOperationError: a persisted row's ``ministry_id`` disagrees
        with the run's ministry. The database's composite foreign keys make
        that unreachable; it is checked because a pair that mapped
        inconsistently would silently constrain the wrong people, and failing
        loudly is the only safe response to state that cannot be true.
    """
    rows = _fetch_same_date_exclusion_rows(
        session, scheduling_period_id=scheduling_period_id
    )
    pairs = []
    for row in rows:
        if row.ministry_id != ministry_id:
            raise InvalidOperationError(
                "a same-date exclusion for this scheduling period belongs to"
                f" ministry {row.ministry_id}, not {ministry_id};"
                " refusing to build scheduling input from inconsistent"
                " constraint data"
            )
        pairs.append(
            LinkedMembershipPair(
                membership_a_id=row.membership_a_id,
                membership_b_id=row.membership_b_id,
            )
        )
    return tuple(pairs)


def _build_member_group_caps(
    session: Session, *, scheduling_period_id: int
) -> tuple[MemberGroupCap, ...]:
    """The per-event member-group caps configured for **this** period, as pure
    values (Task 74).

    **The translation drops the group's name, deliberately.** The service form
    carries it because every message a person reads has to say which rule
    refused a placement; the solver neither shows a message nor needs a label --
    it counts people and reports a code. Handing it a name would be handing the
    engine a fact it has no use for, and the point of this boundary is that what
    reaches the solver is only what the solver needs.

    Read **current**, like qualifications, availability, serving limits and pair
    exclusions and unlike the requirement snapshot: a cap the head configured
    this morning is the cap that applies to a run this afternoon. Scoped by
    ``scheduling_period_id`` alone, because a cap belongs to one period and
    expires with it, and because the row's composite foreign keys already
    guarantee the period and the group share a ministry.

    **Groups whose members are not all current candidates are kept, not
    filtered.** A member deactivated part-way through the period stops being a
    candidate while their group membership stays recorded; the solver treats
    such a member as inert, because somebody with no variables and no existing
    assignment can never be present. Filtering here instead would mean this
    builder deciding a rule no longer applies, which is a head's decision.

    **Two queries when the period configures a cap, one when it does not**,
    whatever the number of groups or members.
    """
    return tuple(
        MemberGroupCap(
            member_group_id=cap.member_group_id,
            max_per_event=cap.max_per_event,
            member_membership_ids=cap.member_membership_ids,
        )
        for cap in load_member_group_caps(
            session, scheduling_period_id=scheduling_period_id
        )
    )


def _build_support_requirements(
    session: Session, *, scheduling_period_id: int
) -> tuple[SameEventSupportRequirement, ...]:
    """The same-event support requirements configured for **this** period, as
    pure values (Task 74).

    **The translation is the identity plus immutability.** The solver's
    candidate identity is ``membership_id`` -- the same identity
    :class:`~app.scheduling.input.CandidateInput` carries and the same one the
    persisted rule names -- so no mapping table is needed and none is invented.
    What the pure form adds is a frozen supporter set and the construction-time
    refusal of a self-supporting rule.

    Read **current**, and scoped by period alone, for the same reasons as the
    caps above.

    **A requirement whose supporter set is too small to satisfy it is kept, not
    dropped.** It means the subject can never be placed, which the solver reports
    honestly as an unfilled position with ``ALL_WITHOUT_EVENT_SUPPORT`` -- a
    configuration a head can see and fix. Silently discarding the rule here
    would schedule somebody the ministry said must not serve alone.

    **Two queries when the period configures a requirement, one when it does
    not**, whatever the number of subjects or supporters.
    """
    return tuple(
        SameEventSupportRequirement(
            subject_membership_id=requirement.subject_membership_id,
            min_supporters=requirement.min_supporters,
            supporter_membership_ids=requirement.supporter_membership_ids,
        )
        for requirement in load_support_requirements(
            session, scheduling_period_id=scheduling_period_id
        )
    )


def _build_adjacent_events(
    session: Session,
    *,
    ministry_id: int,
    min_intervening_events: int | None,
    requirements: tuple[RequirementInput, ...],
) -> tuple[tuple[AdjacentEventInput, ...], tuple[AdjacentEventInput, ...]]:
    """The ministry events on either side of this run, as pure values.

    :returns: ``(preceding, following)`` -- the events just before the first
        one this run schedules, and just after the last.

    **Nothing at all when no rule is configured**, which is the ordinary case:
    nothing is loaded, no query is issued, and the solver receives two empty
    tuples it never looks at. The cost of the rule is paid only by periods that
    asked for it.

    **Both boundaries come from the snapshot**, like every other date here --
    never the period's ``start_date`` or ``end_date``. The version means the
    events it committed to, and an event with no staffing requirement is not
    one this run schedules; treating the period's calendar edges as the
    boundaries would make the surrounding events depend on dates nobody is
    being scheduled for.

    Read **current**, like qualifications, availability, serving limits and
    pair exclusions: a rule the head configured this morning, and the schedules
    somebody finalized last night for the quarters either side, all apply to a
    run this afternoon.

    The translation from :class:`~app.services.event_gap.AdjacentMinistryEvent`
    to :class:`~app.scheduling.input.AdjacentEventInput` is a change of layer,
    not of meaning -- the service type may be read from a database and the
    solver type may not, which is the whole boundary this module exists to
    draw.
    """
    if not min_intervening_events or not requirements:
        return (), ()

    boundaries = [
        (requirement.event_date, requirement.event_id)
        for requirement in requirements
    ]
    first, last = min(boundaries), max(boundaries)
    preceding, following = load_adjacent_ministry_events(
        session,
        ministry_id=ministry_id,
        min_intervening_events=min_intervening_events,
        first_event_date=first[0],
        first_event_id=first[1],
        last_event_date=last[0],
        last_event_id=last[1],
    )

    def to_values(events) -> tuple[AdjacentEventInput, ...]:
        return tuple(
            AdjacentEventInput(
                event_id=event.event_id,
                event_date=event.event_date,
                assigned_membership_ids=event.assigned_membership_ids,
            )
            for event in events
        )

    return to_values(preceding), to_values(following)


# --------------------------------------------------------------------------
# Queries -- each split from its execution so the SQL is testable with no
# database (see tests/test_services_scheduling_input_builder.py)
# --------------------------------------------------------------------------


def _requirements_statement(schedule_version_id: int) -> Select:
    """Snapshot requirements for this version, with each role's current
    ``deactivated_at``. Ordered in SQL as well as in Python so the rows arrive
    predictably even when read directly.
    """
    return (
        select(
            ScheduleVersionRequirement.id.label("requirement_id"),
            ScheduleVersionRequirement.event_id.label("event_id"),
            ScheduleVersionRequirement.event_date.label("event_date"),
            ScheduleVersionRequirement.ministry_role_id.label("ministry_role_id"),
            ScheduleVersionRequirement.ministry_id.label("ministry_id"),
            ScheduleVersionRequirement.required_count.label("required_count"),
            MinistryRole.deactivated_at.label("role_deactivated_at"),
        )
        .join(
            MinistryRole,
            MinistryRole.id == ScheduleVersionRequirement.ministry_role_id,
        )
        .where(ScheduleVersionRequirement.schedule_version_id == schedule_version_id)
        .order_by(
            ScheduleVersionRequirement.event_date,
            ScheduleVersionRequirement.event_id,
            ScheduleVersionRequirement.ministry_role_id,
            ScheduleVersionRequirement.id,
        )
    )


def _fetch_requirement_rows(session: Session, *, schedule_version_id: int):
    """Execution split from statement construction, as everywhere else in this
    package, so the SQL is inspectable and the orchestration is stubbable with
    no database.
    """
    return session.execute(_requirements_statement(schedule_version_id)).all()


def _candidates_statement(ministry_id: int) -> Select:
    """Active memberships of this ministry, joined to active people.

    Both activity checks are in the query, not filtered afterwards: a
    deactivated membership or person is not a candidate at all, and loading
    them only to drop them would invite someone to use the wider list by
    mistake.
    """
    return (
        select(
            MinistryMembership.id.label("membership_id"),
            MinistryMembership.person_id.label("person_id"),
            Person.display_name.label("display_name"),
        )
        .join(Person, Person.id == MinistryMembership.person_id)
        .where(
            MinistryMembership.ministry_id == ministry_id,
            MinistryMembership.deactivated_at.is_(None),
            Person.deactivated_at.is_(None),
        )
        .order_by(MinistryMembership.person_id, MinistryMembership.id)
    )


def _fetch_candidate_rows(session: Session, *, ministry_id: int):
    return session.execute(_candidates_statement(ministry_id)).all()


def _qualifications_statement(
    membership_ids: list[int], ministry_role_ids: list[int]
) -> Select:
    """Approved qualifications only.

    ``is_qualified IS TRUE`` in the query is what folds "no row" and "explicit
    false" into one outcome, exactly as Task 22 does: a pair simply does not
    come back, and the candidate is not eligible either way.
    """
    return select(
        RoleQualification.ministry_membership_id.label("membership_id"),
        RoleQualification.ministry_role_id.label("ministry_role_id"),
    ).where(
        RoleQualification.ministry_membership_id.in_(membership_ids),
        RoleQualification.ministry_role_id.in_(ministry_role_ids),
        RoleQualification.is_qualified.is_(True),
    )


def _fetch_qualified_pairs(
    session: Session, *, membership_ids: list[int], ministry_role_ids: list[int]
) -> set[tuple[int, int]]:
    if not membership_ids or not ministry_role_ids:
        return set()
    rows = session.execute(
        _qualifications_statement(membership_ids, ministry_role_ids)
    ).all()
    return {(row.membership_id, row.ministry_role_id) for row in rows}


def _availability_statement(membership_ids: list[int], event_ids: list[int]) -> Select:
    """Stored answers only. Absence is the third state and is supplied by
    :meth:`app.scheduling.input.CandidateInput.availability_for`, never by a
    row this builder invents.
    """
    return select(
        Availability.ministry_membership_id.label("membership_id"),
        Availability.event_id.label("event_id"),
        Availability.availability_state.label("availability_state"),
    ).where(
        Availability.ministry_membership_id.in_(membership_ids),
        Availability.event_id.in_(event_ids),
    )


def _fetch_availability(
    session: Session, *, membership_ids: list[int], event_ids: list[int]
) -> dict[tuple[int, int], AvailabilityState]:
    if not membership_ids or not event_ids:
        return {}
    rows = session.execute(_availability_statement(membership_ids, event_ids)).all()
    return {
        (row.membership_id, row.event_id): AvailabilityState(row.availability_state)
        for row in rows
    }


def _existing_assignments_statement(schedule_version_id: int) -> Select:
    return (
        select(
            Assignment.id.label("assignment_id"),
            Assignment.schedule_version_requirement_id.label("requirement_id"),
            Assignment.ministry_membership_id.label("membership_id"),
            Assignment.event_id.label("event_id"),
            Assignment.is_override.label("is_override"),
        )
        .where(Assignment.schedule_version_id == schedule_version_id)
        .order_by(Assignment.id)
    )


def _fetch_existing_assignment_rows(session: Session, *, schedule_version_id: int):
    return session.execute(_existing_assignments_statement(schedule_version_id)).all()


def _scheduling_period_statement(
    scheduling_period_id: int,
) -> Select[tuple[SchedulingPeriod]]:
    return select(SchedulingPeriod).where(SchedulingPeriod.id == scheduling_period_id)


def _resolve_scheduling_period(
    session: Session, version: ScheduleVersion
) -> SchedulingPeriod:
    """The ministry comes from here, not from the requirements.

    ``ScheduleVersion`` carries no ``ministry_id``, and deriving it from the
    snapshot would leave a version with zero requirements with no ministry at
    all -- a legitimate state (Task 28 allows an empty snapshot) that must
    still produce usable input.
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
    """The same ``LIMIT 1`` existence probe Tasks 22/24/27/28/29 use. "Latest"
    is a fact about the schedule, living on other rows, so it is queried fresh
    rather than read off a relationship collection.
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


def _same_date_exclusions_statement(scheduling_period_id: int) -> Select:
    """Configured linked pairs for **this** period.

    Ordered in SQL as well, so the pairs reach the solver in a stable order
    and two runs over the same state build an identical model.
    """
    return (
        select(
            MembershipSameDateExclusion.membership_a_id.label("membership_a_id"),
            MembershipSameDateExclusion.membership_b_id.label("membership_b_id"),
            MembershipSameDateExclusion.ministry_id.label("ministry_id"),
        )
        .where(
            MembershipSameDateExclusion.scheduling_period_id
            == scheduling_period_id
        )
        .order_by(
            MembershipSameDateExclusion.membership_a_id,
            MembershipSameDateExclusion.membership_b_id,
        )
    )


def _fetch_same_date_exclusion_rows(session: Session, *, scheduling_period_id: int):
    return session.execute(
        _same_date_exclusions_statement(scheduling_period_id)
    ).all()
