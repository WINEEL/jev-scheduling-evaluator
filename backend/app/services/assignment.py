"""Manually assigning and removing Assignment rows on a working ScheduleVersion.

Implements the accepted design in
``docs/architecture/schedule-output-data-model.md`` §7, §10, §11, §14 and the
authorization rules in ``docs/architecture/core-data-model.md`` §4.2--§4.3.
Reuses the church-wide conflict logic from ``docs/adr/0002`` /
``docs/adr/0003`` via :mod:`app.services.sunday_conflict`. Section numbers
below refer to the schedule-output document unless stated.

**[APPROVED] Manual assignment mutation is allowed only on a mutable working
version** (§7): ``status`` is ``DRAFT`` or ``REVIEW``, **and** no newer version
of the same schedule exists. A ``FINALIZED`` version is immutable; so is an
older ``DRAFT``/``REVIEW`` that a newer version has since superseded, even
though its own ``status`` column never changed -- "latest" is a fact about the
*schedule*, not about the row itself, which is exactly why it must be
queried (:func:`_newer_version_exists`) rather than read off the version in
hand.

**[APPROVED] An assignment fills one immutable snapshot requirement, never
current input** (§8, §10, §11): capacity is
``requirement.required_count``, never current ``staffing_requirement``; the
conflict date is ``requirement.event_date``, never current
``event.event_date``. Both snapshot values were fixed when the version was
created and stay fixed regardless of what current input says now -- the whole
point of the snapshot.

**[REVIEWED] A bounded, explicit override -- not a generic rule engine** (this
task). A non-blank ``override_reason`` may bypass exactly **four** checks: a
deactivated role, missing/declined qualification, explicit ``UNAVAILABLE``,
and a fully staffed requirement.

**[APPROVED, Task 79 final correction] The church-wide same-Sunday conflict was
the fifth, and is not overridable any more.** One Person serves at most one
ministry on the same day. That is a hard rule of this church (ADR 0002), and a
rule a reason can talk its way past is not hard -- so it moved out of the
bounded catalogue and into :func:`app.services.assignment_rules
.require_absolute_rules`, where supplying ``override_reason`` changes nothing
about the answer. Its code string survives in
:mod:`app.services.assignment_policy` because audit rows written while it *was*
overridable still name it; what it no longer does is authorize anything, at
assignment time or at finalization.

The reason it is absolute is a stronger version of the serving maximum's. A
serving maximum and an event gap are one ministry's own decisions, changed with
the people concerned; this one is not any single ministry's to judge at all --
the ministry already holding that person is not in the room, and there is no
version of "I had nobody else" that makes somebody able to be in two places at
once. The remedy is to free them in the other ministry, or assign somebody
else.

**Nothing else is overridable either** -- version immutability, the Ministry
match, target activity, a cancelled event, the one-position-per-event rule, the
person's configured serving maximum for the period, a linked-pair same-date
exclusion, the ministry's event-gap rule, a member-group per-event cap and a
same-event support requirement are all absolute regardless of
``override_reason``. The first few are structural facts about what the row
would even mean rather than scheduling *judgment calls*. The rest are absolute
for a different, deliberate reason: a serving maximum records what a volunteer
said they could manage (requirements §4.4.1, §6), and a same-date exclusion
records an arrangement two volunteers made between them (§4.4.2). Both are
changed *with the people concerned* -- an audited change through
:func:`app.services.serving_limit.set_serving_limit` or
:func:`app.services.same_date_exclusion.set_same_date_exclusion` -- and never
worked around from this side. Mapping either into the overridable catalogue
would make what individuals actually agreed the weakest rule in the system
instead of the firmest, and there is deliberately no one-time exception
mechanism for either.

Supplying ``override_reason`` when nothing needed overriding is itself
rejected -- an ``Assignment`` claiming ``is_override=True`` must correspond to
a real bypassed rule, or the audit trail of *why* an override happened becomes
meaningless.

**[APPROVED] No-response is not a hard block for manual assignment** (this
task, per the product ambiguity note below). Absence of an ``Availability``
row is neither ``AVAILABLE`` nor ``UNAVAILABLE`` -- it is a third, real state
(scheduling-input §8) -- and V1 manual assignment does not turn it into an
automatic rejection, because no-response policy is Ministry configuration that
does not exist yet as a solver-input transformation. Only an *explicit*
``UNAVAILABLE`` row blocks ordinary assignment.

**[REVIEWED] The rules above now live in :mod:`app.services.assignment_rules`,
and this module still owns every read.** Task 63 gave generated schedules
their own batch writer (:mod:`app.services.generated_assignment`), which
answers the same questions out of one prefetch instead of eleven queries per
row. Two writers applying the same rules from two copies of the code would
drift, so the rules moved to one session-free module and both writers call it.
Nothing about *this* function's behaviour changed: the same checks run in the
same order, each of the reads below happens exactly when it always did, and a
rejection short-circuits the reads a later rule would have needed. What this
module is, is the per-row :class:`~app.services.assignment_rules.PairFactSource`.
"""

from __future__ import annotations

import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, Person, RoleQualification
from app.models.scheduling_input import AVAILABILITY_UNAVAILABLE, Availability
from app.models.schedule_output import (
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.services.audit import (
    # ADDED and OVERRIDE_APPLIED are written by ``assignment_rules`` now, but
    # stay imported here: this module has always been where a reader (and the
    # test suite) looks up the three assignment actions as one set, and
    # splitting them across two modules would hide that they are one
    # vocabulary.
    ACTION_ASSIGNMENT_ADDED,
    ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
    ACTION_ASSIGNMENT_REMOVED,
    record_audit_event,
)
from app.services.assignment_policy import (
    # Imported under the private names this module has always used, so nothing
    # here or in its tests changes meaning; the strings themselves are
    # unchanged and must stay that way -- they are persisted audit history.
    BLOCKER_CAPACITY_FULL as _BLOCKER_CAPACITY_FULL,
    BLOCKER_DESCRIPTIONS as _BLOCKER_DESCRIPTIONS,
    BLOCKER_NOT_QUALIFIED as _BLOCKER_NOT_QUALIFIED,
    BLOCKER_ROLE_DEACTIVATED as _BLOCKER_ROLE_DEACTIVATED,
    BLOCKER_SUNDAY_CONFLICT as _BLOCKER_SUNDAY_CONFLICT,
    BLOCKER_UNAVAILABLE as _BLOCKER_UNAVAILABLE,
)
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
    validate_optional_text as _validate_optional_text,
)
from app.services.authorization import require_ministry_operator
from app.services.errors import InvalidOperationError
from app.services.event_gap import load_event_gap_sequence
from app.services.member_group import (
    count_group_members_present,
    load_member_group_caps,
)
from app.services.same_date_exclusion import get_linked_membership_ids
from app.services.same_event_support import (
    count_supporters_present,
    load_support_requirements,
)
from app.services.serving_limit import get_serving_limit
from app.services.sunday_conflict import get_person_sunday_conflicts

_TARGET_TABLE = "assignment"


def assign_member(
    session: Session,
    *,
    actor: Person,
    requirement: ScheduleVersionRequirement,
    membership: MinistryMembership,
    override_reason: str | None = None,
) -> Assignment:
    """Fill ``requirement`` with ``membership``, as ``actor``.

    **Idempotent for the exact pair.** If ``membership`` already fills exactly
    this ``requirement``, the existing row is returned unchanged -- no audit,
    no flush, no second row -- but only *after* the working-version
    immutability check, so a call against a version that has since become
    FINALIZED or been superseded never appears to succeed merely because the
    row happens to already exist. Assigning the same membership to a
    *different* requirement in the same event/version is a different case
    entirely and is rejected outright (see below), never treated as this
    idempotent path.

    **Validated, absolutely, before anything overridable is even considered:**
    the version is a mutable working version; ``membership`` and
    ``requirement`` share a Ministry; ``membership`` and the Person behind it
    are both active; the requirement's current Event is not cancelled; this
    membership does not already fill a *different* required position in this
    same event and version; the assignment stays within the member's
    configured serving maximum for the period; no member this one is
    linked to by a same-date exclusion already holds an assignment on the
    requirement's date in this version; the placement respects the ministry's
    configured event gap; no member group this person belongs to is already at
    its per-event maximum on this event; and, if this person has a same-event
    support requirement, enough of their approved supporters already hold an
    assignment at this same event. **None of these is overridable** --
    supplying ``override_reason`` changes nothing about them (module
    docstring).

    **Then, and only then, the overridable checks are evaluated together:**
    the role is currently active, the membership currently holds an approved
    qualification for this role, the member is not explicitly ``UNAVAILABLE``
    for this event, there is no church-wide Sunday conflict
    (:mod:`app.services.sunday_conflict`, using ``requirement.event_date``,
    never current ``Event.event_date``), and the requirement is not already at
    ``required_count``. If any of these is violated and no
    ``override_reason`` was supplied, the call is rejected with a message
    naming every violated check. If ``override_reason`` was supplied but
    **none** of these was actually violated, the call is also rejected --
    creating an ``Assignment`` that claims to be an override when nothing was
    overridden would make the audit trail lie about what happened.

    The new row and its audit event are added to the same ``session`` and are
    written by the caller's commit, so they land together or not at all
    (:mod:`app.services`). Flushed once, immediately after being added, to
    obtain the identity the audit row must reference. This function never
    commits or rolls back.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``requirement.ministry_id``.
    :raises InvalidOperationError: any of the absolute checks fails; any
        overridable check fails with no ``override_reason``; an
        ``override_reason`` was supplied but nothing needed overriding; or
        ``override_reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=requirement.ministry_id)
    override_reason = _validate_optional_text(override_reason, field="override_reason")

    _require_mutable_working_version(session, schedule_version=requirement.schedule_version)

    existing = _find_exact_assignment(
        session, schedule_version_requirement_id=requirement.id,
        ministry_membership_id=membership.id,
    )
    if existing is not None:
        return existing

    # One rule definition, shared with the batch writer; the reads stay here.
    # ``_RowFactSource`` issues each query at the moment its rule is
    # evaluated, so the short-circuiting this function has always had is
    # unchanged -- a placement refused on the ministry match still costs no
    # read at all.
    facts = _RowFactSource(session, requirement=requirement, membership=membership)

    # -- Absolute checks: never overridable (module docstring). --
    require_absolute_rules(
        requirement=requirement, membership=membership, facts=facts
    )
    # The one absolute rule that is not a property of this placement alone, so
    # it is its own call rather than a step inside the function above
    # (:func:`app.services.assignment_rules.require_absolute_rules` explains
    # why). Here it is still asked per placement, because a head's single
    # considered change *is* the whole change: the state it is judged against
    # is the version as it stands.
    require_same_event_support(
        facts.same_event_support(),
        subject_display_name=membership.person.display_name,
        event_date=requirement.event_date,
    )

    # -- Overridable checks: gathered together, not short-circuited, so a
    # single rejection can name every violated check at once. The serving
    # maximum is deliberately *not* among them; it was checked above. --
    blockers = collect_overridable_blockers(
        requirement=requirement, facts=facts.overridable_facts()
    )
    is_override = resolve_override(
        blockers=blockers, override_reason=override_reason
    )

    assignment = build_assignment(
        requirement=requirement,
        membership=membership,
        is_override=is_override,
        override_reason=override_reason,
    )
    session.add(assignment)
    # The audit row below needs a real target_id, and a new identity bigint
    # does not exist until the INSERT actually runs -- the minimum flush that
    # makes that true, scoped to the one pending row that needs it.
    session.flush([assignment])

    record_assignment_created(
        session,
        actor=actor,
        assignment=assignment,
        requirement=requirement,
        membership=membership,
        override_reason=override_reason,
        blockers=blockers,
    )
    return assignment


def remove_assignment(
    session: Session,
    *,
    actor: Person,
    assignment: Assignment,
    reason: str | None = None,
) -> None:
    """Remove ``assignment``, as ``actor``.

    **Deliberately permissive about everything except version mutability.**
    Removal reduces the schedule's current state and is the cleanup path, so
    it is allowed regardless of the member's current activity, the role's
    activity or qualification, availability, a church-wide Sunday conflict,
    the current event's cancellation, or whether the row was itself an
    override -- none of that is re-validated. **The one check that still
    applies** is that the assignment belongs to a mutable working version
    (DRAFT or REVIEW, and not superseded): removing from FINALIZED or
    historical state would mutate a preserved record, which nothing in this
    project does.

    The audit row is built from the assignment's values before
    ``session.delete()`` is called, and both land in the same transaction
    regardless of order. No flush: the row already has an id, and nothing
    here needs to prove the deletion happened before the caller commits.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``assignment.ministry_id``.
    :raises InvalidOperationError: the assignment's version is not a mutable
        working version, or ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=assignment.ministry_id)
    reason = _validate_optional_text(reason, field="reason")

    requirement = assignment.schedule_version_requirement
    _require_mutable_working_version(session, schedule_version=requirement.schedule_version)

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_ASSIGNMENT_REMOVED,
        target_table=_TARGET_TABLE,
        target_id=assignment.id,
        ministry_id=assignment.ministry_id,
        summary=_remove_summary(assignment=assignment),
        reason=reason,
        before_values={
            "schedule_version_requirement_id": assignment.schedule_version_requirement_id,
            "ministry_membership_id": assignment.ministry_membership_id,
            "schedule_version_id": assignment.schedule_version_id,
            "event_id": assignment.event_id,
            "is_override": assignment.is_override,
            "override_reason": assignment.override_reason,
        },
    )
    # Genuine removal, not a soft-delete flag: the schema has no
    # deactivated_at column here. No flush -- nothing here needs to prove the
    # deletion happened before the transaction commits.
    session.delete(assignment)


class _RowFactSource:
    """This module's :class:`~app.services.assignment_rules.PairFactSource`:
    one query per question, asked when the rule asking it is evaluated.

    The queries themselves, and the reasoning behind each, are the module-level
    functions below -- unchanged, individually monkeypatchable, and still the
    only place SQL for these facts is written. This class is the thin adapter
    that lets the shared rules pull them in rule order rather than having them
    pushed in read order.
    """

    __slots__ = ("_session", "_requirement", "_membership", "_event_roster")

    def __init__(
        self,
        session: Session,
        *,
        requirement: ScheduleVersionRequirement,
        membership: MinistryMembership,
    ) -> None:
        self._session = session
        self._requirement = requirement
        self._membership = membership
        # Memoized, not prefetched. Two rules ask who is on this event's roster
        # -- the member-group cap and the support requirement -- and both are
        # reached only when the placement has already survived everything
        # before them. Reading it on first use keeps the short-circuiting this
        # module is built around; reading it twice would be a round trip spent
        # re-deriving a fact that cannot have changed in between.
        self._event_roster: frozenset[int] | None = None

    def _roster(self) -> frozenset[int]:
        if self._event_roster is None:
            self._event_roster = _memberships_assigned_to_event(
                self._session,
                schedule_version_id=self._requirement.schedule_version_id,
                event_id=self._requirement.event_id,
            )
        return self._event_roster

    def fills_other_position_in_event(self) -> bool:
        return (
            _find_duplicate_membership_in_event(
                self._session,
                schedule_version_id=self._requirement.schedule_version_id,
                event_id=self._requirement.event_id,
                ministry_membership_id=self._membership.id,
            )
            is not None
        )

    def serving_limit(self) -> ServingLimitFacts:
        """**Counted within this ScheduleVersion only.** A predecessor
        version's assignments are history, not commitments a person is
        carrying: a successor that rebuilt the same period would otherwise
        read one quarter's four Sundays as eight and refuse work nobody is
        actually doing.

        Every Assignment in this version for this membership counts --
        automatic, manual and carried-forward alike, and an overriding one no
        differently. There is no unique-Sunday rule: two positions on one date
        would already be refused by the one-position-per-event check, so the
        assignment count and the Sunday count cannot disagree here.

        No maximum means nothing to count, and the count is not issued -- the
        ordinary case costs one query, not two.
        """
        maximum = get_serving_limit(
            self._session,
            ministry_membership_id=self._membership.id,
            scheduling_period_id=self._requirement.schedule_version.scheduling_period_id,
        )
        if maximum is None:
            return ServingLimitFacts(maximum=None)
        return ServingLimitFacts(
            maximum=maximum,
            held=_count_assignments_in_version(
                self._session,
                schedule_version_id=self._requirement.schedule_version_id,
                ministry_membership_id=self._membership.id,
            ),
        )

    def linked_member_assigned_on_date(self) -> bool:
        """**Same calendar date, not the same event** (§4.4.2), compared on
        the immutable snapshot ``event_date`` -- so a period holding a morning
        and an evening service on one Sunday is caught, which the
        one-position-per-event check cannot see. Scoped to this version, for
        the same reason the serving count is.

        **The rule is looked up from both sides of the pair.**
        ``get_linked_membership_ids`` reads rules naming this membership as
        either half, because the pair is unordered. No rule means no question,
        and the date probe is not issued -- the ordinary case costs one query.
        """
        linked_ids = get_linked_membership_ids(
            self._session,
            ministry_membership_id=self._membership.id,
            scheduling_period_id=self._requirement.schedule_version.scheduling_period_id,
        )
        if not linked_ids:
            return False
        return (
            _find_linked_assignment_on_date(
                self._session,
                schedule_version_id=self._requirement.schedule_version_id,
                event_date=self._requirement.event_date,
                linked_membership_ids=sorted(linked_ids),
            )
            is not None
        )

    def event_gap(self) -> EventGapFacts:
        """**The ministry's own event sequence, not a number of days**
        (requirements §4.8), and the same sequence the solver and the
        finalization gate use -- built here by
        :func:`app.services.event_gap.load_event_gap_sequence` so the three
        cannot come to disagree about which event follows which.

        **No configured rule means no question, and the sequence is not
        loaded**: the ordinary case costs the one query that reads the setting.
        A period that does configure the rule pays four more -- the version's
        events, the ministry events just before and just after them, and one
        read of who serves at those -- plus one for this membership's own
        assignments in this version.

        Presence is read from **this** version only for the version's own
        events, and from the **authoritative** schedule (ADR 0003) for the
        events on either side of it. That split is what stops a predecessor
        version's finalized rows from blocking the successor that is replacing
        them, while still letting last quarter's finalized schedule block this
        period's first event and next quarter's block its last.
        """
        requirement = self._requirement
        version = requirement.schedule_version
        sequence = load_event_gap_sequence(
            self._session,
            scheduling_period_id=version.scheduling_period_id,
            ministry_id=requirement.ministry_id,
            schedule_version_id=version.id,
        )
        if sequence is None:
            return EventGapFacts(min_intervening_events=None)

        occupied = sequence.occupied_events_for(
            self._membership.id,
            version_event_ids=_assigned_event_ids_in_version(
                self._session,
                schedule_version_id=version.id,
                ministry_membership_id=self._membership.id,
            ),
        )
        return EventGapFacts(
            min_intervening_events=sequence.min_intervening_events,
            conflicting_event_date=sequence.conflicting_event_date(
                target_event_id=requirement.event_id,
                occupied_event_ids=occupied,
            ),
        )

    def member_group_event_limits(self) -> tuple[MemberGroupEventFacts, ...]:
        """**Counted per event, and each member once** (Task 74), within this
        version only -- a predecessor version's assignments are history, not
        people standing on this Sunday's crew.

        **No capped group means no question, and the roster is not read**: the
        ordinary case costs the two queries that read the period's caps. A
        member who *is* in a capped group pays one more, shared with the
        support rule below through the memo on this object.

        Every capped group the member belongs to is reported, because all of
        them apply at once; which one refuses is the rule's decision, not this
        read's.
        """
        caps = load_member_group_caps(
            self._session,
            scheduling_period_id=(
                self._requirement.schedule_version.scheduling_period_id
            ),
        )
        applicable = [cap for cap in caps if cap.contains(self._membership.id)]
        if not applicable:
            return ()
        roster = self._roster()
        return tuple(
            MemberGroupEventFacts(
                member_group_name=cap.member_group_name,
                max_per_event=cap.max_per_event,
                members_present=count_group_members_present(cap, roster),
            )
            for cap in applicable
        )

    def same_event_support(self) -> SameEventSupportFacts:
        """**Same event, never the same date** (Task 74): the roster read below
        is scoped to this requirement's event, so a supporter at another
        service on the same Sunday does not count -- the opposite reading from
        :meth:`linked_member_assigned_on_date`, and deliberately so.

        Scoped to this version, for the same reason every other count here is.

        **No requirement means no question, and the roster is not read**: the
        ordinary case costs the two queries that read the period's rules.
        """
        requirements = load_support_requirements(
            self._session,
            scheduling_period_id=(
                self._requirement.schedule_version.scheduling_period_id
            ),
        )
        mine = next(
            (
                requirement
                for requirement in requirements
                if requirement.subject_membership_id == self._membership.id
            ),
            None,
        )
        if mine is None:
            return SameEventSupportFacts(min_supporters=None)
        return SameEventSupportFacts(
            min_supporters=mine.min_supporters,
            supporters_present=count_supporters_present(mine, self._roster()),
            approved_supporter_count=len(mine.supporter_membership_ids),
        )

    def has_cross_ministry_sunday_conflict(self) -> bool:
        """The church-wide hard rule's one read, for the absolute check.

        Its own method since the rule stopped being overridable: it is asked
        before the overridable group and, when it refuses, none of that group's
        three reads happens at all.

        ``requirement.event_date`` -- the immutable snapshot -- never
        ``requirement.event.event_date`` (module docstring). Asked by
        ``person_id``, so the answer is about the human rather than about one
        of their memberships; ``target_ministry_id`` excludes this ministry's
        own rows, because two of *this* ministry's events on one date is this
        ministry's business (ADR 0002).
        """
        requirement = self._requirement
        conflict = get_person_sunday_conflicts(
            self._session,
            person_id=self._membership.person_id,
            conflict_date=requirement.event_date,
            target_ministry_id=requirement.ministry_id,
        )
        return conflict.is_blocked

    def overridable_facts(self) -> OverridableFacts:
        requirement = self._requirement
        membership = self._membership

        qualification = _find_qualification(
            self._session,
            ministry_membership_id=membership.id,
            ministry_role_id=requirement.ministry_role_id,
        )
        availability = _find_availability(
            self._session,
            ministry_membership_id=membership.id,
            event_id=requirement.event_id,
        )
        return OverridableFacts(
            is_qualified=qualification is not None and qualification.is_qualified,
            is_unavailable=(
                availability is not None
                and availability.availability_state == AVAILABILITY_UNAVAILABLE
            ),
            current_filled_count=_current_assignment_count(
                self._session, schedule_version_requirement_id=requirement.id
            ),
        )


def _require_mutable_working_version(session: Session, *, schedule_version: ScheduleVersion) -> None:
    """**[APPROVED]** A version may be changed only while it is DRAFT/REVIEW
    *and* no newer version of the same schedule exists (§7).

    ``status`` is read directly off ``schedule_version`` -- a plain column on
    a to-one relationship the caller already holds, not the kind of
    possibly-stale collection state this project is cautious about. Whether a
    *newer* version exists is a different question entirely and is always
    queried fresh (:func:`_newer_version_exists`): that fact lives on other
    rows, not on this one, so nothing about holding this object could ever
    answer it honestly.
    """
    require_mutable_working_version_status(schedule_version)
    if _newer_version_exists(
        session, schedule_id=schedule_version.schedule_id,
        version_number=schedule_version.version_number,
    ):
        raise InvalidOperationError(
            "cannot change assignments on a schedule version"
            " that has been superseded by a newer version"
        )


def _remove_summary(*, assignment: Assignment) -> str:
    requirement = assignment.schedule_version_requirement
    person_name = assignment.ministry_membership.person.display_name
    role_name = requirement.ministry_role.name
    ministry_name = requirement.ministry_role.ministry.name
    when = requirement.event_date.isoformat()
    return f"Removed {person_name} from {role_name} for {ministry_name} on {when}"


def _newer_version_exists_statement(schedule_id: int, version_number: int) -> Select[tuple[int]]:
    """The query, split from its execution so it is testable with no database
    (see ``tests/test_services_assignment.py``).
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


def _exact_assignment_lookup_statement(
    schedule_version_requirement_id: int, ministry_membership_id: int
) -> Select[tuple[Assignment]]:
    return select(Assignment).where(
        Assignment.schedule_version_requirement_id == schedule_version_requirement_id,
        Assignment.ministry_membership_id == ministry_membership_id,
    )


def _find_exact_assignment(
    session: Session, *, schedule_version_requirement_id: int, ministry_membership_id: int,
) -> Assignment | None:
    """This exact membership already filling this exact requirement, if so --
    the idempotency case. Distinct from
    :func:`_find_duplicate_membership_in_event`, which asks a broader
    question (this membership, this event and version, *any* requirement).
    """
    stmt = _exact_assignment_lookup_statement(
        schedule_version_requirement_id, ministry_membership_id
    )
    return session.execute(stmt).scalar_one_or_none()


def _duplicate_membership_in_event_statement(
    schedule_version_id: int, event_id: int, ministry_membership_id: int
) -> Select[tuple[int]]:
    return (
        select(Assignment.id)
        .where(
            Assignment.schedule_version_id == schedule_version_id,
            Assignment.event_id == event_id,
            Assignment.ministry_membership_id == ministry_membership_id,
        )
        .limit(1)
    )


def _linked_assignment_on_date_statement(
    schedule_version_id: int,
    event_date: datetime.date,
    linked_membership_ids: list[int],
) -> Select[tuple[int]]:
    """Any assignment in this version, on this **date**, held by a linked member.

    Joined to ``schedule_version_requirement`` for its snapshot ``event_date``
    rather than reading the live ``event`` row: the version means the dates it
    committed to, and an event that has since moved must not silently relocate
    this question (§8, §11).
    """
    return (
        select(Assignment.id)
        .join(
            ScheduleVersionRequirement,
            ScheduleVersionRequirement.id
            == Assignment.schedule_version_requirement_id,
        )
        .where(
            Assignment.schedule_version_id == schedule_version_id,
            Assignment.ministry_membership_id.in_(linked_membership_ids),
            ScheduleVersionRequirement.event_date == event_date,
        )
        .limit(1)
    )


def _find_linked_assignment_on_date(
    session: Session,
    *,
    schedule_version_id: int,
    event_date: datetime.date,
    linked_membership_ids: list[int],
) -> int | None:
    stmt = _linked_assignment_on_date_statement(
        schedule_version_id, event_date, linked_membership_ids
    )
    return session.execute(stmt).scalar_one_or_none()


def _assigned_event_ids_in_version_statement(
    schedule_version_id: int, ministry_membership_id: int
) -> Select[tuple[int]]:
    """Which events this membership already holds an assignment at, in this
    version.

    ``Assignment.event_id`` directly, with no join: the model's four-column
    composite foreign key already pins it to its requirement's event, so
    joining ``schedule_version_requirement`` to re-derive it would prove what
    the database guarantees. Scoped to this version alone, because a
    predecessor's assignments are history rather than commitments anybody is
    carrying.
    """
    return select(Assignment.event_id).where(
        Assignment.schedule_version_id == schedule_version_id,
        Assignment.ministry_membership_id == ministry_membership_id,
    )


def _assigned_event_ids_in_version(
    session: Session, *, schedule_version_id: int, ministry_membership_id: int
) -> frozenset[int]:
    stmt = _assigned_event_ids_in_version_statement(
        schedule_version_id, ministry_membership_id
    )
    return frozenset(session.execute(stmt).scalars().all())


def _memberships_assigned_to_event_statement(
    schedule_version_id: int, event_id: int
) -> Select[tuple[int]]:
    """Who is on one **event's** roster in this version.

    ``Assignment.event_id`` directly, with no join: the model's four-column
    composite foreign key already pins it to its requirement's event, so joining
    ``schedule_version_requirement`` to re-derive it would prove what the
    database guarantees.

    Memberships, not assignments -- the two rules that read this count *people
    present*, and the one-position-per-event rule (§10) already makes those the
    same number. Scoped to this version alone, because a predecessor's
    assignments are history rather than people standing on this crew.
    """
    return select(Assignment.ministry_membership_id).where(
        Assignment.schedule_version_id == schedule_version_id,
        Assignment.event_id == event_id,
    )


def _memberships_assigned_to_event(
    session: Session, *, schedule_version_id: int, event_id: int
) -> frozenset[int]:
    stmt = _memberships_assigned_to_event_statement(schedule_version_id, event_id)
    return frozenset(session.execute(stmt).scalars().all())


def _count_assignments_in_version_statement(
    schedule_version_id: int, ministry_membership_id: int
) -> Select[tuple[int]]:
    return (
        select(func.count())
        .select_from(Assignment)
        .where(
            Assignment.schedule_version_id == schedule_version_id,
            Assignment.ministry_membership_id == ministry_membership_id,
        )
    )


def _count_assignments_in_version(
    session: Session, *, schedule_version_id: int, ministry_membership_id: int
) -> int:
    stmt = _count_assignments_in_version_statement(
        schedule_version_id, ministry_membership_id
    )
    return int(session.execute(stmt).scalar_one())


def _find_duplicate_membership_in_event(
    session: Session, *, schedule_version_id: int, event_id: int, ministry_membership_id: int,
) -> int | None:
    """Whether this membership already fills *some* required position in this
    event and version -- called only after :func:`_find_exact_assignment` has
    already ruled out "this exact requirement", so a match here can only mean
    a genuinely different one (§10's one-position-per-event rule).
    """
    stmt = _duplicate_membership_in_event_statement(
        schedule_version_id, event_id, ministry_membership_id
    )
    return session.execute(stmt).scalar_one_or_none()


def _assignment_count_statement(schedule_version_requirement_id: int) -> Select[tuple[int]]:
    return select(func.count(Assignment.id)).where(
        Assignment.schedule_version_requirement_id == schedule_version_requirement_id
    )


def _current_assignment_count(session: Session, *, schedule_version_requirement_id: int) -> int:
    """How many Assignments currently fill this requirement -- compared
    against ``requirement.required_count``, the immutable snapshot capacity,
    never current ``staffing_requirement`` (§12).
    """
    stmt = _assignment_count_statement(schedule_version_requirement_id)
    return session.execute(stmt).scalar_one()


def _qualification_lookup_statement(
    ministry_membership_id: int, ministry_role_id: int
) -> Select[tuple[RoleQualification]]:
    return select(RoleQualification).where(
        RoleQualification.ministry_membership_id == ministry_membership_id,
        RoleQualification.ministry_role_id == ministry_role_id,
    )


def _find_qualification(
    session: Session, *, ministry_membership_id: int, ministry_role_id: int,
) -> RoleQualification | None:
    """Absence is "never assessed", not "false" (core §8) -- kept distinct
    here too: both are folded into the same ``_BLOCKER_NOT_QUALIFIED`` outcome
    for assignment purposes, but never conflated internally, so a caller
    inspecting the row itself (rather than just the blocker set) still sees
    the true distinction.
    """
    stmt = _qualification_lookup_statement(ministry_membership_id, ministry_role_id)
    return session.execute(stmt).scalar_one_or_none()


def _availability_lookup_statement(
    ministry_membership_id: int, event_id: int
) -> Select[tuple[Availability]]:
    return select(Availability).where(
        Availability.ministry_membership_id == ministry_membership_id,
        Availability.event_id == event_id,
    )


def _find_availability(
    session: Session, *, ministry_membership_id: int, event_id: int,
) -> Availability | None:
    """Absence is "no response", distinct from both explicit answers
    (scheduling-input §8) -- and, per this task's product decision, is **not**
    treated as a block for manual assignment (module docstring).
    """
    stmt = _availability_lookup_statement(ministry_membership_id, event_id)
    return session.execute(stmt).scalar_one_or_none()
