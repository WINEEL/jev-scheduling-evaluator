"""Recorded serving: how often a Person has actually been rostered (Task 79).

One question, answered for many people at once: *on how many past dates does
this church's authoritative record show this person serving, and in which
ministries?*

The term, and why it is that term
---------------------------------
**"Recorded serving"**, everywhere -- :data:`RECORDED_SERVING_LABEL` is the one
spelling, and the UI uses it verbatim.

**Never "actually served", and never "attended".** This application records who
was *scheduled*; nothing in the domain records who turned up. An assignment in a
finalized schedule proves the church committed that person to that date, not
that they were in the building. Calling the number "times served" would put a
claim about a human being's conduct on a screen, on evidence that cannot
support it -- and somebody would eventually make a decision with it (Task 79
§13's truthfulness requirement).

What counts, exactly
--------------------
One **distinct (ministry, church-local date)** pair, subject to all of:

- the assignment belongs to the **authoritative** version of its schedule --
  the highest-numbered ``FINALIZED`` version, from
  :func:`app.services.sunday_conflict.authoritative_version_subquery`. That is
  ADR 0003's definition and it is *called*, never restated here;
- the date is the version's own snapshot,
  ``schedule_version_requirement.event_date`` -- never the live ``event`` row,
  which may have been moved since people were committed to it (ADR 0003);
- the event is **not cancelled** (``event.cancelled_at IS NULL``): a cancelled
  service is one nobody served;
- the date is **strictly before today in the church's timezone**
  (:func:`app.services.church_calendar.church_today`), so a commitment that has
  not happened yet is never counted as history.

What is therefore excluded, and each for its own reason: **future** dates (not
yet history), **DRAFT** versions (nobody has agreed to them), **REVIEW**
versions (agreed by nobody outside the ministry -- and in any case not
``FINALIZED``), **superseded FINALIZED** versions (an amendment replaced what
they said), and **cancelled** events.

**Distinct by (ministry, date), not one per Assignment row.** Somebody filling
two roles at one service, or serving two of the same ministry's events on one
Sunday, has served that ministry on that date once. ADR 0002 settled that the
church-wide hard rule is *one ministry per Sunday*, not one event per Sunday, so
counting rows would inflate exactly the ministries that run two services.

The invalid state this module reports rather than counts
--------------------------------------------------------
Because the hard rule is one ministry per person per Sunday, an authoritative
record showing one Person in **two different ministries on one date** is not two
legitimate services -- it is a church-wide conflict that should not exist. This
module reports those pairs (:func:`find_cross_ministry_conflicts`, surfaced on
every summary as :attr:`PersonServingSummary.same_date_conflicts`) instead of
quietly normalizing them into a larger number. A count that silently absorbed
the contradiction would hide the only evidence that something is wrong.

Query budget
------------
**Bounded, never per-person and never per-ministry** (Task 79 §14).
:func:`recorded_serving_totals` is *one* aggregate query for an entire
directory page. :func:`recorded_serving_summaries` is *two* -- the per-ministry
breakdown and the conflict scan -- for however many people are asked about,
plus the one small ``church_today`` lookup shared by both entry points.
Nothing here loops over people, and nothing lazy-loads.

Read-only throughout: no ``add``, ``flush``, ``commit`` or ``rollback``, and no
authorization check. Who may ask is the calling service's decision --
:mod:`app.services.person_directory` has already applied
:func:`~app.services.authorization.require_people_directory_reader` before any
of this runs, and a volunteer never reaches it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import Select, distinct, func, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryMembership
from app.models.schedule_output import Assignment, ScheduleVersionRequirement
from app.models.scheduling_input import Event
from app.services.church_calendar import church_today
from app.services.sunday_conflict import authoritative_version_subquery

__all__ = [
    "RECORDED_SERVING_LABEL",
    "CrossMinistryConflict",
    "MinistryServingCount",
    "PersonServingSummary",
    "find_cross_ministry_conflicts",
    "recorded_serving_summaries",
    "recorded_serving_totals",
]

#: The one term for this number, used verbatim by the API and the UI.
#:
#: **Deliberately not "times served" or "attendance".** See the module
#: docstring: the domain records commitments, not attendance, and a label that
#: claimed otherwise would be a statement about somebody's conduct that the
#: data cannot support.
RECORDED_SERVING_LABEL = "Recorded serving"


@dataclass(frozen=True, slots=True)
class MinistryServingCount:
    """How many past dates one person has recorded serving in one ministry."""

    ministry_id: int
    ministry_name: str
    count: int


@dataclass(frozen=True, slots=True)
class CrossMinistryConflict:
    """One date on which the authoritative record puts a person in two
    ministries at once -- a hard-rule violation, not a serving occurrence.

    ``ministry_names`` carries every ministry involved so the report names the
    actual problem rather than saying only that one exists.
    """

    person_id: int
    event_date: datetime.date
    ministry_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PersonServingSummary:
    """One person's recorded serving: the total, the breakdown, the conflicts.

    ``total`` is the sum of ``by_ministry``'s counts, by construction -- both
    come from one distinct-pair query, so a screen showing the breakdown beside
    the total can never show two numbers that disagree.

    ``by_ministry`` lists **only ministries this person has history in**
    (Task 79 §12): a row reading "AV: 0" would suggest an absence that the
    absence of the row already says.
    """

    person_id: int
    total: int
    by_ministry: tuple[MinistryServingCount, ...] = ()
    #: Empty in every healthy church-wide state. Non-empty means the record
    #: contradicts the one-ministry-per-Sunday rule on those dates, and the
    #: number beside it should be read as suspect rather than trusted.
    same_date_conflicts: tuple[CrossMinistryConflict, ...] = ()

    @property
    def has_conflicts(self) -> bool:
        return bool(self.same_date_conflicts)


# --------------------------------------------------------------------------
# The public queries
# --------------------------------------------------------------------------


def recorded_serving_totals(
    session: Session,
    *,
    person_ids: Sequence[int],
    church_id: int,
    as_of: datetime.date | None = None,
) -> dict[int, int]:
    """Recorded serving totals for a whole directory page, in **one query**.

    The directory shows the total and nothing more (Task 79 §12), so this is
    deliberately the cheap half: it aggregates in the database and returns a
    plain mapping rather than building the per-ministry breakdown nobody on
    that screen is looking at.

    **Every requested person appears in the result**, including those with no
    history at all, whose answer is ``0``. A caller reading a missing key as
    "unknown" and a present zero as "none" would be reading the same fact two
    ways.

    ``as_of`` exists for tests and for a caller that has already resolved the
    church's date; left out, it is
    :func:`~app.services.church_calendar.church_today`.
    """
    people = _unique_ids(person_ids)
    totals = {person_id: 0 for person_id in people}
    if not people:
        return totals

    as_of = _resolve_as_of(session, church_id=church_id, as_of=as_of)
    rows = session.execute(
        _totals_statement(person_ids=people, as_of=as_of)
    ).all()
    for row in rows:
        totals[row.person_id] = row.recorded_serving
    return totals


def recorded_serving_summaries(
    session: Session,
    *,
    person_ids: Sequence[int],
    church_id: int,
    as_of: datetime.date | None = None,
) -> dict[int, PersonServingSummary]:
    """Full summaries -- total, per-ministry breakdown, conflicts -- in **two
    queries**, whatever the number of people.

    What the Person detail screen needs. The total is summed from the same
    breakdown rows rather than queried separately, so the two can never
    disagree; the conflict scan is the second query, and it exists because a
    breakdown is precisely where an impossible state would otherwise look like
    an ordinary extra row.

    Every requested person appears, with a zero total and empty tuples when
    they have no recorded history.
    """
    people = _unique_ids(person_ids)
    summaries = {
        person_id: PersonServingSummary(person_id=person_id, total=0)
        for person_id in people
    }
    if not people:
        return summaries

    as_of = _resolve_as_of(session, church_id=church_id, as_of=as_of)

    by_person: dict[int, list[MinistryServingCount]] = {}
    for row in session.execute(
        _by_ministry_statement(person_ids=people, as_of=as_of)
    ).all():
        by_person.setdefault(row.person_id, []).append(
            MinistryServingCount(
                ministry_id=row.ministry_id,
                ministry_name=row.ministry_name,
                count=row.recorded_serving,
            )
        )

    conflicts_by_person: dict[int, list[CrossMinistryConflict]] = {}
    for conflict in find_cross_ministry_conflicts(
        session, person_ids=people, church_id=church_id, as_of=as_of
    ):
        conflicts_by_person.setdefault(conflict.person_id, []).append(conflict)

    for person_id in people:
        counts = tuple(by_person.get(person_id, ()))
        summaries[person_id] = PersonServingSummary(
            person_id=person_id,
            total=sum(entry.count for entry in counts),
            by_ministry=counts,
            same_date_conflicts=tuple(conflicts_by_person.get(person_id, ())),
        )
    return summaries


def find_cross_ministry_conflicts(
    session: Session,
    *,
    person_ids: Sequence[int],
    church_id: int,
    as_of: datetime.date | None = None,
) -> tuple[CrossMinistryConflict, ...]:
    """Dates where the authoritative record puts one person in two ministries.

    **This is the church-wide hard rule read backwards.**
    :mod:`app.services.sunday_conflict` stops such an assignment from being
    made; this asks whether one exists anyway -- through an import, a direct
    database write, or a bug -- and names it.

    A healthy church returns an empty tuple. A non-empty result is a defect to
    investigate, not a number to add up: the two ministries on that date are
    not two services, they are one contradiction (Task 79 §13).

    One query. Grouped by person and snapshot date, keeping only the groups
    with more than one distinct ministry.
    """
    people = _unique_ids(person_ids)
    if not people:
        return ()

    as_of = _resolve_as_of(session, church_id=church_id, as_of=as_of)
    rows = session.execute(
        _cross_ministry_conflict_statement(person_ids=people, as_of=as_of)
    ).all()
    return tuple(
        CrossMinistryConflict(
            person_id=row.person_id,
            event_date=row.event_date,
            ministry_names=tuple(row.ministry_names),
        )
        for row in rows
    )


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------


def _recorded_serving_dates(
    *, person_ids: Sequence[int], as_of: datetime.date
):
    """The one definition of a recorded serving occurrence, as a subquery.

    **Every public query in this module is built on this and only this**, so
    "what counts" is written once. Widening or narrowing the rule here changes
    the total, the breakdown and the conflict scan together, which is the only
    way they can be guaranteed to agree.

    ``DISTINCT`` on ``(person_id, ministry_id, event_date)`` is what collapses
    several role rows for one ministry on one date into the single occurrence
    it was -- see the module docstring on ADR 0002.

    The joins, each for exactly one fact:

    - ``ScheduleVersionRequirement`` -- the immutable snapshot **date** the
      version committed people to, never the live ``Event`` row's date;
    - ``MinistryMembership`` -- the **person**, since ``assignment`` carries no
      ``person_id`` of its own (schedule-output §11);
    - ``Event`` -- **only** ``cancelled_at``, which is current state and is the
      right thing to read live;
    - the authoritative-version subquery -- restricts to the highest
      ``FINALIZED`` version per schedule, which is what excludes DRAFT, REVIEW
      and superseded versions in one predicate rather than three.
    """
    authoritative_versions = authoritative_version_subquery()
    return (
        select(
            MinistryMembership.person_id.label("person_id"),
            Assignment.ministry_id.label("ministry_id"),
            ScheduleVersionRequirement.event_date.label("event_date"),
        )
        .join(
            ScheduleVersionRequirement,
            ScheduleVersionRequirement.id
            == Assignment.schedule_version_requirement_id,
        )
        .join(
            MinistryMembership,
            MinistryMembership.id == Assignment.ministry_membership_id,
        )
        .join(Event, Event.id == Assignment.event_id)
        .join(
            authoritative_versions,
            authoritative_versions.c.id == Assignment.schedule_version_id,
        )
        .where(
            MinistryMembership.person_id.in_(person_ids),
            # Strictly before today: a service happening today has not been
            # served yet, and one that has not happened is not history.
            ScheduleVersionRequirement.event_date < as_of,
            Event.cancelled_at.is_(None),
        )
        .distinct()
        .subquery()
    )


def _totals_statement(
    *, person_ids: Sequence[int], as_of: datetime.date
) -> Select:
    """One row per person who has any history: ``(person_id, count)``.

    Aggregated in the database rather than by counting returned rows in Python:
    a directory page of a hundred people with years of history would otherwise
    ship every one of those pairs across the wire to be counted and discarded.
    """
    occurrences = _recorded_serving_dates(person_ids=person_ids, as_of=as_of)
    return (
        select(
            occurrences.c.person_id.label("person_id"),
            func.count().label("recorded_serving"),
        )
        .group_by(occurrences.c.person_id)
    )


def _by_ministry_statement(
    *, person_ids: Sequence[int], as_of: datetime.date
) -> Select:
    """``(person_id, ministry_id, ministry_name, count)``, ordered for display.

    Joined to ``ministry`` for the name so the caller never has to look one up
    per row -- which would be the per-ministry N+1 this module exists to avoid.

    Ordered by ``lower(name)`` then id: a person's breakdown reads the same way
    every time it is opened, and two ministries with the same name (which the
    unique index forbids, but the ordering should not depend on) still sort
    stably.
    """
    occurrences = _recorded_serving_dates(person_ids=person_ids, as_of=as_of)
    return (
        select(
            occurrences.c.person_id.label("person_id"),
            occurrences.c.ministry_id.label("ministry_id"),
            Ministry.name.label("ministry_name"),
            func.count().label("recorded_serving"),
        )
        .join(Ministry, Ministry.id == occurrences.c.ministry_id)
        .group_by(
            occurrences.c.person_id, occurrences.c.ministry_id, Ministry.name
        )
        .order_by(
            occurrences.c.person_id, func.lower(Ministry.name), occurrences.c.ministry_id
        )
    )


def _cross_ministry_conflict_statement(
    *, person_ids: Sequence[int], as_of: datetime.date
) -> Select:
    """``(person_id, event_date, ministry_names)`` for every violating date.

    ``HAVING count(DISTINCT ministry_id) > 1`` is the hard rule stated as a
    query: one person, one date, more than one ministry. ``array_agg`` collects
    the ministry names so the report can say *which* -- "Person 22 is in two
    ministries on 11 October" is actionable; "Person 22 has a conflict" is not.
    """
    occurrences = _recorded_serving_dates(person_ids=person_ids, as_of=as_of)
    return (
        select(
            occurrences.c.person_id.label("person_id"),
            occurrences.c.event_date.label("event_date"),
            func.array_agg(
                func.coalesce(Ministry.name, "")
            ).label("ministry_names"),
        )
        .join(Ministry, Ministry.id == occurrences.c.ministry_id)
        .group_by(occurrences.c.person_id, occurrences.c.event_date)
        .having(func.count(distinct(occurrences.c.ministry_id)) > 1)
        .order_by(occurrences.c.person_id, occurrences.c.event_date)
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _unique_ids(person_ids: Sequence[int]) -> list[int]:
    """Sorted and de-duplicated, so a caller repeating an id does not widen the
    ``IN`` list or produce two entries for one person.
    """
    return sorted({person_id for person_id in person_ids if person_id > 0})


def _resolve_as_of(
    session: Session, *, church_id: int, as_of: datetime.date | None
) -> datetime.date:
    """The church-local boundary between history and the future.

    Resolved once per public call and threaded through every statement, rather
    than read per query: two queries in one summary must not straddle midnight
    and disagree about whether last night's service has happened.
    """
    if as_of is not None:
        return as_of
    return church_today(session, church_id=church_id)
