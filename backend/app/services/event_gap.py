"""The ministry event-gap rule: how many events must pass before serving again.

The third *structured hard scheduling constraint*, and the first one whose
scope is the **ministry and period** rather than a person or a pair
(requirements §4.8). It answers a question neither availability, a serving
limit nor a linked-pair exclusion can: not "may this person serve on this
date?", not "how many times this period?", and not "may these two share a
day?", but *"how soon after serving may the same person serve again?"*

**What the rule means, stated once.** A scheduling period may configure
``min_intervening_events``. When it is set to ``N``, at least ``N`` of that
ministry's own events must fall between any two assignments of the same person.
``N = 1`` is therefore exactly "no consecutive assignments": after serving one
event, a person must skip the next one.

**Counted in events, never in days, and this is the whole point.** The sequence
is the ministry's own non-cancelled events in chronological order -- ordinary
recurring services and ad-hoc special events alike, with nothing privileged
about a Sunday. Two events with no other ministry event between them are
consecutive whether they are seven days apart or one:

- serve an ordinary service, and the next ministry event is blocked, whatever
  day it falls on;
- a special event held between two ordinary ones *participates* -- it is the
  next event after the one before it, and the one before the one after it;
- serve the special event, and the following event is blocked even if it is the
  very next morning;
- skip one event and the person is eligible again.

A "more than seven days apart" reading would be a different rule, and it would
be wrong the first time a ministry held a mid-week special event. Nothing in
this module does arithmetic on dates; dates are used only to *order* events.

**Ministry- and period-scoped, and it expires with the period.** The setting
lives on ``scheduling_period``, which belongs to exactly one ministry, so a
Setup head configuring a gap can no more affect AV's schedule than they can
edit AV's availability. It is deliberately not a church-wide rest rule: two
ministries may reasonably want different answers, and a person serving Setup
says nothing about how soon they may serve Kids. Like a serving limit, it is
never consulted for, nor copied into, a later period.

**Absence means no rule.** ``None`` clears: the column goes back to ``NULL``,
and consecutive assignments are allowed again -- the behaviour every period has
by default and had before this rule existed. There is deliberately no stored
zero meaning "consecutive is fine", because that is precisely what ``NULL``
already says, and one fact with two spellings is two things free to disagree.
The database's ``min_intervening_events > 0`` CHECK makes that structural.

**Hard, and deliberately not overridable** (requirements §6). It is applied as
a solver constraint before any objective, as an absolute check in manual
assignment, and as a finalization gate. A head who needs a placement the rule
forbids changes the number here -- which is audited -- and then makes the
assignment, exactly as they would raise a serving maximum. There is no
one-time exception, because a rule a head can wave away for one Sunday is not
the hard rule a ministry asked for.

**The sequence spans *both* ends of the window being scheduled.** A period
boundary is an artifact of how a ministry organises its planning; it is not a
gap in the ministry's events. Someone who served the last event before a new
period has served the event immediately preceding its first one, and someone
already committed to the event after a period's last one is just as much "the
next event" from that last one's point of view. The rule is symmetric -- it
does not care which of two assignments was decided first -- so treating one
boundary and not the other would enforce it in one direction only, and a head
filling the end of a quarter would get a schedule that breaks the rule against
a quarter already published.

:func:`load_adjacent_ministry_events` is what supplies both, and it loads
**only** as many events on each side as the configured gap needs -- never "the
ministry's history" and never "everything after". Beyond that distance an event
cannot share a window with anything this run schedules, so loading it would be
work that changes no answer.

**Who counts as "serving" an event outside the window** is ADR 0003's
authoritative reading, shared with the church-wide conflict rule through
:func:`app.services.sunday_conflict.authoritative_version_subquery`: the
highest-numbered FINALIZED version of that event's schedule. That reading
matters especially on the forward side, where a *draft* for a later period may
well exist: a draft nobody has agreed to blocks nobody, and a version an
amendment superseded no longer speaks for what happened. An event whose
schedule was never finalized still *counts* as an intervening event -- it
simply blocks nobody.

**V1 scope: only this ministry's active Head may configure it** (Task 80).
Being scheduled by the rule confers no authority over it, exactly as for
serving limits and same-date exclusions.

Not implemented here, deliberately: a soft "prefer a longer gap" preference, a
church-wide rest rule, a per-person gap, any carry-forward between periods, and
any repair of assignments that a newly configured gap has put in conflict --
that last one is reported by :mod:`app.services.finalization_readiness` and
fixed by a head, never by this service deleting somebody's assignment.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Collection, Iterable, Mapping, Sequence

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from app.models.core import Person
from app.models.schedule_output import Assignment, ScheduleVersionRequirement
from app.models.scheduling_input import Event, SchedulingPeriod
from app.services.audit import (
    ACTION_EVENT_GAP_RULE_CHANGED,
    ACTION_EVENT_GAP_RULE_CLEARED,
    ACTION_EVENT_GAP_RULE_RECORDED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError
from app.services.sunday_conflict import authoritative_version_subquery

__all__ = [
    "MIN_EVENT_GAP_CONFLICT",
    "AdjacentMinistryEvent",
    "MinistryEventSequence",
    "PeriodEventGapRule",
    "describe_event_gap",
    "gap_windows",
    "get_event_gap_rule",
    "get_min_intervening_events",
    "load_adjacent_ministry_events",
    "load_event_gap_sequence",
    "load_scheduled_event_sequence",
    "positions_within_gap",
    "set_min_intervening_events",
]

#: The one code naming this rule wherever it is reported to a person: the
#: domain error :func:`app.services.assignment.assign_member` raises, and the
#: readiness issue :mod:`app.services.finalization_readiness` emits.
#:
#: **Deliberately not a member of**
#: :data:`app.services.assignment_policy.OVERRIDABLE_BLOCKERS`, for the same
#: reason ``SAME_DATE_LINKED_MEMBER_CONFLICT`` is not: that catalogue is the
#: bounded set of checks an ``override_reason`` may bypass, and adding this one
#: would make it bypassable by definition.
#:
#: The solver has its own vocabulary and its own code
#: (``ALL_WITHIN_EVENT_GAP``), because a solver diagnostic names *why a position
#: is open* while this names *why one placement is refused* -- the same split
#: the linked-pair rule already keeps.
MIN_EVENT_GAP_CONFLICT = "MIN_EVENT_GAP_CONFLICT"

_TARGET_TABLE = "scheduling_period"


@dataclass(frozen=True, slots=True)
class AdjacentMinistryEvent:
    """One of a ministry's events just *outside* the window being scheduled.

    Either side: the events immediately before the version's first scheduled
    event, and the ones immediately after its last. One type for both, because
    the rule is symmetric and the solver treats them identically -- a fixed
    presence in the sequence that this run may not change. Which side an event
    is on is carried by *where it is*, not by a flag.

    Carries the two things the rule needs and nothing else: where the event
    sits in the sequence, and which memberships are recorded as serving at it.
    Never the role anybody served, never the assignment rows, and never
    anything about the schedule it came from -- the gap rule does not care what
    somebody did last quarter or will do next, only that they did or will.
    """

    event_id: int
    event_date: datetime.date
    #: Memberships holding an **authoritative** assignment at this event.
    #: Empty is ordinary and does not make the event any less part of the
    #: sequence.
    assigned_membership_ids: frozenset[int] = frozenset()

    @property
    def sort_key(self) -> tuple[datetime.date, int]:
        """``(event_date, event_id)`` -- the canonical event ordering."""
        return (self.event_date, self.event_id)


@dataclass(frozen=True, slots=True)
class MinistryEventSequence:
    """The ordered ministry events one schedule version's gap rule runs along,
    and who is already committed at the ones outside it.

    **One sequence, three enforcement paths.** Manual assignment, the batch
    writer behind generation, and the finalization gate all ask their questions
    of this object, so none of them can quietly disagree with the others about
    what "the next event" is. The solver reasons about the same sequence from
    its own pure values (:attr:`app.scheduling.input.SchedulingInput.ministry_event_sequence`),
    built by the input builder from these same loaders.

    **Preceding events, then the events the version schedules, then following
    events** -- one chronological line through both boundaries of the window.
    Both ends are loaded for the same reason and to the same depth: the rule is
    symmetric, so an assignment already fixed just after the window constrains
    the window's end exactly as one just before it constrains the start.

    Positions, never dates, decide the rule; the dates are carried only so a
    message can name the event a person would recognise.
    """

    #: The configured gap. Always positive -- a sequence is not built at all
    #: for a period with no rule.
    min_intervening_events: int
    #: ``(event_id, event_date)`` in canonical order, spanning both boundaries.
    events: tuple[tuple[int, datetime.date], ...] = ()
    #: ``membership_id -> event ids outside this version`` they are already
    #: authoritatively committed to, on either side. Only the outside events:
    #: what a version's own assignments occupy is the caller's own state --
    #: which a generation run changes as it goes -- and each caller knows it
    #: exactly.
    adjacent_events_by_membership: Mapping[int, frozenset[int]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def index_by_event(self) -> Mapping[int, int]:
        return MappingProxyType(
            {event_id: index for index, (event_id, _) in enumerate(self.events)}
        )

    def occupied_events_for(
        self, membership_id: int, *, version_event_ids: Collection[int]
    ) -> frozenset[int]:
        """Every event this membership occupies in the sequence.

        The caller supplies the events occupied *in the version being worked
        on*; the authoritative commitments on either side of it are added here.
        Keeping the two halves apart is what makes the version's own state --
        which a run changes as it goes -- the caller's business, while the
        outside events, which nothing this run does can change, stay here.
        """
        return frozenset(version_event_ids) | self.adjacent_events_by_membership.get(
            membership_id, frozenset()
        )

    def conflicting_event_date(
        self, *, target_event_id: int, occupied_event_ids: Collection[int]
    ) -> datetime.date | None:
        """The date of a nearby event that forbids serving ``target_event_id``.

        ``None`` when the placement is clear, which is the ordinary answer. The
        target event itself is never its own conflict: a person already
        assigned there is an idempotency case the caller settles, not a gap
        violation.

        Conflicts are found in **both** directions: an event the person serves
        before the target and one they serve after it are the same violation of
        the same symmetric rule, and the window this searches is centred on the
        target rather than trailing behind it.

        An event this sequence does not contain -- one further outside the
        window than the gap reaches, or one dropped from the version's snapshot
        -- yields ``None`` rather than an error. The rule can only speak about
        the sequence it was built for, and inventing a position for an unknown
        event would be inventing a fact.

        The nearest conflicting event is reported when there are several, so a
        head is pointed at the clash they are most likely to act on.
        """
        index = self.index_by_event.get(target_event_id)
        if index is None:
            return None
        window = positions_within_gap(
            index,
            length=len(self.events),
            min_intervening_events=self.min_intervening_events,
        )
        occupied = set(occupied_event_ids)
        nearest = min(
            (
                position
                for position in window
                if position != index and self.events[position][0] in occupied
            ),
            key=lambda position: abs(position - index),
            default=None,
        )
        return None if nearest is None else self.events[nearest][1]

    def conflicting_event_dates(
        self, occupied_event_ids: Collection[int]
    ) -> tuple[tuple[datetime.date, datetime.date], ...]:
        """Every pair of occupied events too close together, earlier date first.

        For the finalization gate, which reports what is wrong rather than
        refusing one placement: a version may break the rule in several places
        at once, and a head fixing it wants the whole list.

        Pairs, not positions, and each pair once: the rule is symmetric, and
        emitting a clash from both ends would say the same thing twice.
        """
        occupied = set(occupied_event_ids)
        positions = sorted(
            index
            for index, (event_id, _) in enumerate(self.events)
            if event_id in occupied
        )
        return tuple(
            (self.events[earlier][1], self.events[later][1])
            for earlier, later in zip(positions, positions[1:])
            if later - earlier <= self.min_intervening_events
        )


@dataclass(frozen=True, slots=True)
class PeriodEventGapRule:
    """One scheduling period's event-gap rule, for the head managing it."""

    scheduling_period_id: int
    scheduling_period_name: str
    ministry_id: int
    #: ``None`` = no rule; otherwise the positive number of this ministry's
    #: events that must fall between two assignments of the same person.
    min_intervening_events: int | None


# --------------------------------------------------------------------------
# Pure sequence arithmetic -- no database, no dates
# --------------------------------------------------------------------------


def gap_windows(length: int, min_intervening_events: int) -> list[range]:
    """Every run of ``min_intervening_events + 1`` consecutive positions.

    "At least ``N`` events between two assignments" is exactly "at most one
    assignment in any ``N + 1`` consecutive events", and the window form states
    that as one constraint per window rather than ``N`` pairwise ones.

    A sequence no longer than a single window yields one window covering all of
    it, so a period shorter than the configured gap still has the rule applied
    to what it does contain rather than to nothing.

    Pure, and deliberately shared with the solver's own copy of the same
    reasoning (:func:`app.scheduling.solver._gap_windows`) only by being tested
    against it -- the solver may not import this module, which reads a
    database.
    """
    size = min_intervening_events + 1
    if length < 2:
        return []
    if length <= size:
        return [range(length)]
    return [range(start, start + size) for start in range(length - size + 1)]


def positions_within_gap(
    position: int, *, length: int, min_intervening_events: int
) -> range:
    """The positions ``position`` conflicts with, itself excluded by the caller.

    ``[position - N, position + N]`` clamped to the sequence. Serving at any of
    them means fewer than ``N`` events separate the two assignments, which is
    what the rule forbids. Symmetric, because the rule is: it does not matter
    which of two assignments came first.
    """
    low = max(position - min_intervening_events, 0)
    high = min(position + min_intervening_events, length - 1)
    return range(low, high + 1)


def describe_event_gap(min_intervening_events: int) -> str:
    """Plain wording for one configured gap, accurate for any value.

    Written once and shared by every message a person reads -- the manual
    assignment refusal and the finalization issue -- so the rule cannot be
    described two different ways by two different screens. ``1`` gets the
    sentence a ministry head would actually say; larger values get the general
    form rather than a strained plural of the special case.
    """
    if min_intervening_events == 1:
        return (
            "this ministry does not allow the same person to serve two"
            " consecutive events in this period"
        )
    return (
        f"this ministry requires at least {min_intervening_events} of its"
        " events between two assignments of the same person in this period"
    )


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def get_min_intervening_events(
    session: Session, *, scheduling_period_id: int
) -> int | None:
    """The configured gap for this period, or ``None`` when there is no rule.

    Read-only and unauthorized on purpose, exactly like
    :func:`app.services.serving_limit.get_serving_limit`: it answers a question
    about scheduling input, and every caller that exposes the answer to a human
    -- the input builder, manual assignment, finalization readiness -- has
    already established who may see it. A check here would duplicate theirs and
    make the solver path depend on an actor it does not have.

    Queried rather than read off a ``SchedulingPeriod`` the caller happens to
    hold: a head may change the rule while a draft is open, and a value loaded
    when the request began is not the same fact as the value now.
    """
    return session.execute(
        _min_intervening_events_statement(scheduling_period_id)
    ).scalar_one_or_none()


def get_event_gap_rule(
    session: Session, *, actor: Person, scheduling_period: SchedulingPeriod
) -> PeriodEventGapRule:
    """This period's event-gap rule, for someone allowed to manage it.

    **Read-only.** The authorized read behind the management screen, matching
    :func:`app.services.serving_limit.list_serving_limits` in shape: the API
    layer stays thin and does not decide who may look.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``scheduling_period.ministry_id``.
    """
    require_ministry_reader(actor, ministry_id=scheduling_period.ministry_id)
    return PeriodEventGapRule(
        scheduling_period_id=scheduling_period.id,
        scheduling_period_name=scheduling_period.name,
        ministry_id=scheduling_period.ministry_id,
        min_intervening_events=scheduling_period.min_intervening_events,
    )


def load_adjacent_ministry_events(
    session: Session,
    *,
    ministry_id: int,
    min_intervening_events: int,
    first_event_date: datetime.date,
    first_event_id: int,
    last_event_date: datetime.date,
    last_event_id: int,
) -> tuple[tuple[AdjacentMinistryEvent, ...], tuple[AdjacentMinistryEvent, ...]]:
    """This ministry's events just outside a scheduled window, both sides.

    :returns: ``(preceding, following)``, each in chronological order, each at
        most ``min_intervening_events`` long.

    **Exactly as many events as the rule needs on each side, and no more.** A
    window reaches ``min_intervening_events`` positions in either direction and
    no further, so an event beyond that cannot share a window with anything this
    run schedules and loading it would be work that changes no answer. That is
    the whole of "inspect only the minimum number of events needed".

    **Three queries, whatever the gap and whichever side has rows.** One for the
    earlier events, one for the later ones, and **one** for the authoritative
    assignments at all of them together -- never one per event, never one per
    side, and never one per candidate. A caller asking for a gap of one pays
    three round trips; a caller asking for three pays the same three.

    Both boundaries are ``(date, id)`` pairs rather than dates, because two
    events may share a date and the sequence must still have a defined order
    (the event model stores no time of day -- scheduling-input §5). An event on
    the first event's date with a lower id is genuinely earlier in the sequence
    and is loaded as preceding; one on the last event's date with a higher id is
    genuinely later and is loaded as following. The version's own events fall
    between the two boundaries and are excluded from both sides by construction.

    **Cancelled events are excluded**, which is the same reading every other
    scheduling path takes: a cancelled event means nobody is serving, so it is
    neither a blocker nor an intervening event. Reading ``cancelled_at`` live
    is deliberate -- it is current state, unlike a committed date (ADR 0003).

    Read-only, and unauthorized for the same reason
    :func:`get_min_intervening_events` is.

    :raises InvalidOperationError: ``min_intervening_events`` is not positive.
        A caller reaching here with no rule configured has confused "no rule"
        with "a gap of zero", and returning empty tuples would hide it.
    """
    if min_intervening_events <= 0:
        raise InvalidOperationError(
            "min_intervening_events must be positive to load adjacent events;"
            " a period with no configured rule needs no surrounding events at"
            " all"
        )

    # The earlier query orders newest-first so its ``LIMIT`` takes the
    # *nearest* events; the sequence itself runs the other way, so it is
    # reversed here rather than by a second sort somewhere downstream. The
    # later query already returns the nearest first *and* in sequence order, so
    # it is not reversed -- the asymmetry is in the SQL, not in the meaning.
    preceding_rows = list(
        reversed(
            session.execute(
                _preceding_events_statement(
                    ministry_id=ministry_id,
                    before_event_date=first_event_date,
                    before_event_id=first_event_id,
                    limit=min_intervening_events,
                )
            ).all()
        )
    )
    following_rows = session.execute(
        _following_events_statement(
            ministry_id=ministry_id,
            after_event_date=last_event_date,
            after_event_id=last_event_id,
            limit=min_intervening_events,
        )
    ).all()

    if not preceding_rows and not following_rows:
        return (), ()

    # One assignment read covering both sides. Splitting it would double the
    # round trips to answer one question about one set of events.
    assigned = _authoritative_memberships_by_event(
        session,
        ministry_id=ministry_id,
        event_ids=[row.event_id for row in preceding_rows + following_rows],
    )

    def build(rows) -> tuple[AdjacentMinistryEvent, ...]:
        return tuple(
            AdjacentMinistryEvent(
                event_id=row.event_id,
                event_date=row.event_date,
                assigned_membership_ids=assigned.get(row.event_id, frozenset()),
            )
            for row in rows
        )

    return build(preceding_rows), build(following_rows)


def load_scheduled_event_sequence(
    session: Session, *, schedule_version_id: int
) -> tuple[tuple[int, datetime.date], ...]:
    """The distinct events one version schedules for, in canonical order.

    Read from ``schedule_version_requirement`` -- the **snapshot** -- never from
    current ``event`` rows, exactly as every other date in the scheduling path
    is (schedule-output §8). A version means the events it committed to, and an
    event moved afterwards must not silently reorder the sequence a schedule
    was built against.

    One query, and the distinctness is done in Python: a version holds a few
    dozen requirement rows across a handful of events, so a ``DISTINCT`` in SQL
    would buy nothing and a ``GROUP BY`` would obscure the ordering the rule
    depends on.
    """
    rows = session.execute(
        _scheduled_events_statement(schedule_version_id)
    ).all()
    seen: set[int] = set()
    events: list[tuple[int, datetime.date]] = []
    for row in rows:
        if row.event_id in seen:
            continue
        seen.add(row.event_id)
        events.append((row.event_id, row.event_date))
    return tuple(events)


def load_event_gap_sequence(
    session: Session,
    *,
    scheduling_period_id: int,
    ministry_id: int,
    schedule_version_id: int,
    scheduled_events: Sequence[tuple[int, datetime.date]] | None = None,
) -> MinistryEventSequence | None:
    """The whole sequence one version's gap rule is evaluated against.

    :returns: ``None`` when this period configures no rule -- the ordinary
        case, costing exactly one query and loading no surrounding events
        whatsoever. The callers read that ``None`` as "this rule does not
        apply", never as an empty sequence that happens to forbid nothing.

    ``scheduled_events`` lets a caller that has already read the version's
    requirements -- the finalization gate does -- supply them instead of paying
    for the query again. Supplied or read, they must be the snapshot's events
    in canonical order; :func:`load_scheduled_event_sequence` is what produces
    them, and a caller passing its own is expected to have derived them the
    same way.

    **At most five queries, and never more with a larger gap**: the rule, the
    version's events, the preceding events, the following events, and one read
    of who serves at all of those.
    """
    min_intervening_events = get_min_intervening_events(
        session, scheduling_period_id=scheduling_period_id
    )
    if min_intervening_events is None:
        return None

    scheduled = (
        tuple(scheduled_events)
        if scheduled_events is not None
        else load_scheduled_event_sequence(
            session, schedule_version_id=schedule_version_id
        )
    )
    if not scheduled:
        # A version with no requirements schedules nothing, so there are no
        # boundaries to load events against and nothing for the rule to
        # constrain. An empty snapshot is a legitimate state (Task 28).
        return MinistryEventSequence(min_intervening_events=min_intervening_events)

    first_event_id, first_event_date = scheduled[0]
    last_event_id, last_event_date = scheduled[-1]
    preceding, following = load_adjacent_ministry_events(
        session,
        ministry_id=ministry_id,
        min_intervening_events=min_intervening_events,
        first_event_date=first_event_date,
        first_event_id=first_event_id,
        last_event_date=last_event_date,
        last_event_id=last_event_id,
    )

    adjacent: dict[int, set[int]] = {}
    for event in preceding + following:
        for membership_id in event.assigned_membership_ids:
            adjacent.setdefault(membership_id, set()).add(event.event_id)

    return MinistryEventSequence(
        min_intervening_events=min_intervening_events,
        events=(
            tuple((e.event_id, e.event_date) for e in preceding)
            + scheduled
            + tuple((e.event_id, e.event_date) for e in following)
        ),
        adjacent_events_by_membership=MappingProxyType(
            {
                membership_id: frozenset(event_ids)
                for membership_id, event_ids in adjacent.items()
            }
        ),
    )


def _authoritative_memberships_by_event(
    session: Session, *, ministry_id: int, event_ids: Sequence[int]
) -> dict[int, frozenset[int]]:
    """``event_id -> memberships serving there`` in the authoritative schedule.

    One query for every event asked about. Keyed by ``ministry_membership_id``
    rather than by person: the candidates the rule constrains are this
    ministry's memberships, a person has at most one membership per ministry
    (core §7), and both halves of the comparison are therefore the same
    identity with no mapping to invent.
    """
    if not event_ids:
        return {}
    by_event: dict[int, set[int]] = {}
    for row in session.execute(
        _authoritative_assignments_statement(
            ministry_id=ministry_id, event_ids=event_ids
        )
    ).all():
        by_event.setdefault(row.event_id, set()).add(row.membership_id)
    return {
        event_id: frozenset(memberships)
        for event_id, memberships in by_event.items()
    }


# --------------------------------------------------------------------------
# Mutation
# --------------------------------------------------------------------------


def set_min_intervening_events(
    session: Session,
    *,
    actor: Person,
    scheduling_period: SchedulingPeriod,
    min_intervening_events: int | None,
    reason: str | None = None,
) -> SchedulingPeriod:
    """Record, change, or clear ``scheduling_period``'s event-gap rule.

    One explicit setter, matching
    :func:`app.services.serving_limit.set_serving_limit`: a positive integer
    records or revises the rule, and ``None`` clears it back to no rule.

    **A request that changes nothing does nothing**, and is not audited:
    clearing an absent rule, or setting the value it already has, is not a
    change to record. Writing a history row for a non-event would make the
    audit trail claim a head acted when they did not.

    **Configuring a gap that the current draft already breaks is allowed**, and
    deliberately so. A head may agree a stricter rule after a draft is built.
    Nothing here deletes an assignment to make the new rule true -- that would
    destroy a decision a person made, possibly one a volunteer is counting on.
    The version simply becomes unfinalizable until a head repairs it, which
    :mod:`app.services.finalization_readiness` reports.

    **The availability lock does not apply.** That lock freezes *collected
    availability* so a draft is built against a stable set of answers. A gap
    rule is not an availability answer; it is read live at generation time like
    a qualification, and a head must be able to change it while a draft is in
    progress -- which is precisely the workflow that replaces overriding it.

    The mutation and its audit row are added to the same ``session`` and are
    written by the caller's commit, so they land together or not at all
    (:mod:`app.services`). **No flush**: this mutates an attribute on a row the
    caller already holds, which already has its identity, so there is nothing
    an identity-only flush would obtain. This function never commits or rolls
    back.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of ``scheduling_period.ministry_id``.
    :raises InvalidOperationError: ``scheduling_period`` is not persisted;
        ``min_intervening_events`` is not ``None`` and not a positive integer;
        or ``reason`` was supplied but blank.
    """
    require_ministry_operator(actor, ministry_id=scheduling_period.ministry_id)
    if scheduling_period.id is None:
        raise InvalidOperationError(
            "scheduling period must be persisted (id is None)"
        )
    _require_valid_gap(min_intervening_events)
    reason = _validate_optional_reason(reason)

    previous = scheduling_period.min_intervening_events
    if previous == min_intervening_events:
        return scheduling_period  # idempotent no-op, audited as nothing

    scheduling_period.min_intervening_events = min_intervening_events

    if min_intervening_events is None:
        action = ACTION_EVENT_GAP_RULE_CLEARED
        summary = (
            f"Cleared the event-gap rule for"
            f" {scheduling_period.ministry.name} {scheduling_period.name}:"
            " the same person may serve consecutive events again"
        )
    elif previous is None:
        action = ACTION_EVENT_GAP_RULE_RECORDED
        summary = (
            f"Set {scheduling_period.ministry.name} {scheduling_period.name}"
            f" to require {min_intervening_events} intervening event(s)"
            " between assignments of the same person"
        )
    else:
        action = ACTION_EVENT_GAP_RULE_CHANGED
        summary = (
            f"Changed {scheduling_period.ministry.name}"
            f" {scheduling_period.name}'s required intervening events from"
            f" {previous} to {min_intervening_events}"
        )

    record_audit_event(
        session,
        actor=actor,
        action=action,
        target_table=_TARGET_TABLE,
        target_id=scheduling_period.id,
        ministry_id=scheduling_period.ministry_id,
        summary=summary,
        reason=reason,
        # Only the one changed business field (audit §7.2). Both sides are
        # always written, including the ``None`` that represents "no rule" --
        # absence is the meaningful value here, not a missing one.
        before_values={"min_intervening_events": previous},
        after_values={"min_intervening_events": min_intervening_events},
    )
    return scheduling_period


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _require_valid_gap(min_intervening_events: int | None) -> None:
    if min_intervening_events is None:
        return
    # bool is an int subclass, and True would silently become a gap of 1.
    if isinstance(min_intervening_events, bool) or not isinstance(
        min_intervening_events, int
    ):
        raise InvalidOperationError(
            "min_intervening_events must be an integer or None"
        )
    if min_intervening_events <= 0:
        raise InvalidOperationError(
            "min_intervening_events must be positive; use None to clear the"
            " rule. Zero would mean 'consecutive assignments are allowed',"
            " which is exactly what no rule already says"
        )


def _validate_optional_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


# --------------------------------------------------------------------------
# Queries -- each split from its execution so the SQL is testable with no
# database (see tests/test_services_event_gap.py)
# --------------------------------------------------------------------------


def _min_intervening_events_statement(scheduling_period_id: int) -> Select:
    """One column, not the whole row: the answer is a single nullable integer,
    and selecting the period would invite a caller to read stale neighbours
    off it.
    """
    return select(SchedulingPeriod.min_intervening_events).where(
        SchedulingPeriod.id == scheduling_period_id
    )


def _scheduled_events_statement(schedule_version_id: int) -> Select:
    """The version's snapshot requirements, ordered as the sequence needs them.

    ``ORDER BY event_date, event_id`` is the canonical event ordering used
    everywhere in this project -- the same leading pair
    :attr:`app.scheduling.input.RequirementInput.sort_key` uses. Two events on
    one date are ordered by id because the event model stores no time of day
    (scheduling-input §5); inventing one would be inventing a fact, and leaving
    them unordered would make the gap rule depend on row order.
    """
    return (
        select(
            ScheduleVersionRequirement.event_id.label("event_id"),
            ScheduleVersionRequirement.event_date.label("event_date"),
        )
        .where(
            ScheduleVersionRequirement.schedule_version_id == schedule_version_id
        )
        .order_by(
            ScheduleVersionRequirement.event_date,
            ScheduleVersionRequirement.event_id,
        )
    )


def _preceding_events_statement(
    *,
    ministry_id: int,
    before_event_date: datetime.date,
    before_event_id: int,
    limit: int,
) -> Select:
    """The ``limit`` most recent non-cancelled events of this ministry before a
    ``(date, id)`` boundary.

    ``ORDER BY event_date DESC, id DESC`` with ``LIMIT`` is what makes this
    "only enough history": the database returns the nearest few events and
    stops, rather than the caller filtering a ministry's whole past in Python.

    The boundary predicate is the explicit two-part comparison rather than a
    row-value one, so the same statement reads identically on every dialect the
    test suite compiles it against, and so the ``(event_date, id)`` ordering it
    encodes is visible rather than implied.
    """
    return (
        select(Event.id.label("event_id"), Event.event_date.label("event_date"))
        .where(
            Event.ministry_id == ministry_id,
            Event.cancelled_at.is_(None),
            or_(
                Event.event_date < before_event_date,
                and_(
                    Event.event_date == before_event_date,
                    Event.id < before_event_id,
                ),
            ),
        )
        .order_by(Event.event_date.desc(), Event.id.desc())
        .limit(limit)
    )


def _following_events_statement(
    *,
    ministry_id: int,
    after_event_date: datetime.date,
    after_event_id: int,
    limit: int,
) -> Select:
    """The ``limit`` earliest non-cancelled events of this ministry after a
    ``(date, id)`` boundary.

    The mirror of :func:`_preceding_events_statement`, predicate for predicate:
    same ministry scope, same cancelled-event exclusion, same explicit two-part
    ``(date, id)`` comparison, same ``LIMIT`` -- with the comparison and the
    ordering reversed. Written as its own statement rather than as a flag on
    one shared builder, because a direction parameter would put four
    conditionals inside the one function whose predicates must be read at a
    glance to be trusted.

    ``ORDER BY event_date, id`` ascending means the nearest events come back
    first *and* already in sequence order, so unlike the preceding side nothing
    downstream reverses them.
    """
    return (
        select(Event.id.label("event_id"), Event.event_date.label("event_date"))
        .where(
            Event.ministry_id == ministry_id,
            Event.cancelled_at.is_(None),
            or_(
                Event.event_date > after_event_date,
                and_(
                    Event.event_date == after_event_date,
                    Event.id > after_event_id,
                ),
            ),
        )
        .order_by(Event.event_date, Event.id)
        .limit(limit)
    )


def _authoritative_assignments_statement(
    *, ministry_id: int, event_ids: Iterable[int]
) -> Select:
    """Who serves at these events, per ADR 0003's authoritative reading.

    The subquery is :func:`app.services.sunday_conflict.authoritative_version_subquery`,
    shared rather than rewritten, so "who served" cannot come to mean one thing
    for the church-wide conflict rule and another for this one.

    ``Assignment.event_id`` is read directly: the model's four-column composite
    foreign key already pins it to its requirement's event, so joining
    ``schedule_version_requirement`` to re-derive it would prove something the
    database guarantees. The ministry predicate is scoping the query says out
    loud -- the event ids already imply it.
    """
    authoritative_versions = authoritative_version_subquery()
    return (
        select(
            Assignment.event_id.label("event_id"),
            Assignment.ministry_membership_id.label("membership_id"),
        )
        .join(
            authoritative_versions,
            authoritative_versions.c.id == Assignment.schedule_version_id,
        )
        .where(
            Assignment.event_id.in_(event_ids),
            Assignment.ministry_id == ministry_id,
        )
    )
