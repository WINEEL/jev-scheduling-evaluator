"""Every ministry in the church, for an Admin to oversee (Task 79 §4).

The capability the product described and never had: an Admin could manage any
ministry but could not *find* one, because nothing in the API listed them. The
Admin home screen said so in as many words. This module is the list.

**Oversight is a read, and this module contains nothing but reads.** It
answers "which ministries exist, who leads each, and where is each one up to?"
It changes nothing, creates nothing, and archives nothing -- and there is
deliberately no ministry create/edit/archive operation here, because the domain
has no reviewed product rule for one (see the Task 79 report). ``Ministry``
rows are created by import scripts today.

Who may call it
---------------
**Active Admin only** (:func:`~app.services.authorization.require_active_admin`).

- A **Ministry Head** does not get this list. They reach the ministries they
  lead through ``/api/v1/me``'s ``headed_ministries``, which is a statement
  about their own memberships; a church-wide inventory is not theirs to browse.
- A **volunteer** gets 403, like every other church-wide read.

**Reading a ministry is not operating it.** An Admin may open every screen this
list links to, because the configuration reads authorize an Admin
(:func:`~app.services.authorization.require_ministry_reader`). What they must
not get from this list is *operational write* authority: adding people to a
roster, changing roles, generating a schedule, submitting or finalizing one.
Task 79 narrowed the membership writes to
:func:`~app.services.authorization.require_ministry_operator` for exactly that
reason, and **Task 80 finished the job** -- every ministry configuration,
scheduling and lifecycle write in the product now takes the operator rule, so
an Admin who heads nothing can open all of this and change none of it.

What each row carries, and what it does not
-------------------------------------------
Name, active state, the ministry's active Head(s), how many people are actively
on it, its current scheduling period, and that period's latest schedule version
and status. **Every one of those is a fact the domain already stores**; nothing
is invented to fill a column, and a ministry with no period, no head or no
schedule reports that honestly rather than showing a plausible blank.

"Current period" means the period containing today in the church's own
timezone; failing that, the most recently started one. A ministry between
quarters therefore shows the quarter it just finished rather than nothing at
all, which is what somebody looking at the list actually wants to know.

Query budget
------------
**Five bounded queries, whatever the number of ministries** -- the ministries,
their heads, their active member counts, one current period each, and one
latest version each -- plus the small ``church_today`` lookup. Never one query
per ministry, and nothing lazy-loads.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryMembership, Person
from app.models.schedule_output import Schedule, ScheduleVersion
from app.models.scheduling_input import SchedulingPeriod
from app.services.authorization import require_active_admin
from app.services.church_calendar import church_today

__all__ = [
    "MinistryHeadSummary",
    "MinistryOverview",
    "MinistryPeriodSummary",
    "list_church_ministries",
]


@dataclass(frozen=True, slots=True)
class MinistryHeadSummary:
    """One person who actively leads a ministry.

    Name and id only. This is an oversight list, not a contact directory: an
    Admin who needs somebody's details opens their Person record, which is the
    screen that knows who may see what.
    """

    person_id: int
    display_name: str


@dataclass(frozen=True, slots=True)
class MinistryPeriodSummary:
    """The scheduling period a ministry is currently in, and its schedule.

    ``latest_version_status`` is ``None`` when no schedule version exists for
    the period yet -- which is the ordinary state of a period somebody has only
    just created, and is reported as such rather than as ``"DRAFT"``.
    """

    scheduling_period_id: int
    name: str
    start_date: datetime.date
    end_date: datetime.date
    #: Whether today falls inside it, as distinct from it merely being the most
    #: recent. A screen that showed "current" for a quarter that ended in March
    #: would be lying by one word.
    is_current: bool
    latest_version_number: int | None = None
    latest_version_status: str | None = None


@dataclass(frozen=True, slots=True)
class MinistryOverview:
    """One row of the Admin's church-wide ministry list."""

    ministry_id: int
    name: str
    description: str | None
    deactivated_at: datetime.datetime | None
    heads: tuple[MinistryHeadSummary, ...] = field(default_factory=tuple)
    #: People with an active membership. Not "people who have ever been on it":
    #: the question this column answers is how big the team is now.
    active_member_count: int = 0
    period: MinistryPeriodSummary | None = None

    @property
    def is_active(self) -> bool:
        return self.deactivated_at is None


def list_church_ministries(
    session: Session, *, actor: Person, include_inactive: bool = True
) -> tuple[MinistryOverview, ...]:
    """Every ministry in ``actor``'s church, for an Admin.

    **Scoped to the actor's own church, always.** ``church_id`` is taken from
    the actor and is never a parameter: V1 is a single-church deployment
    (core §7), and a caller-supplied church id would be the one way an Admin
    could read another church's ministries.

    ``include_inactive`` defaults to **True**, unlike the people directory's
    equivalent. An archived ministry is part of what an overseer is overseeing
    -- past schedules still name it -- and a list that silently omitted one
    would make an Admin think it had been deleted, which nothing here does.

    Ordered by ``lower(name)`` then id, so the list reads the same every time.

    :raises AuthorizationError: the actor is not an active Admin.
    """
    require_active_admin(actor)

    rows = session.execute(
        _ministries_statement(
            church_id=actor.church_id, include_inactive=include_inactive
        )
    ).all()
    ministry_ids = [row.ministry_id for row in rows]
    if not ministry_ids:
        return ()

    heads = _heads_by_ministry(session, ministry_ids)
    member_counts = _active_member_counts(session, ministry_ids)
    periods = _current_periods(
        session, ministry_ids, today=church_today(session, church_id=actor.church_id)
    )

    return tuple(
        MinistryOverview(
            ministry_id=row.ministry_id,
            name=row.name,
            description=row.description,
            deactivated_at=row.deactivated_at,
            heads=tuple(heads.get(row.ministry_id, ())),
            active_member_count=member_counts.get(row.ministry_id, 0),
            period=periods.get(row.ministry_id),
        )
        for row in rows
    )


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def _ministries_statement(*, church_id: int, include_inactive: bool) -> Select:
    """The ministries themselves, explicit columns rather than ORM entities --
    the same shape :mod:`app.services.person_directory` uses, so no attribute
    access on a result row can wander off into a lazy load.
    """
    stmt = select(
        Ministry.id.label("ministry_id"),
        Ministry.name.label("name"),
        Ministry.description.label("description"),
        Ministry.deactivated_at.label("deactivated_at"),
    ).where(Ministry.church_id == church_id)
    if not include_inactive:
        stmt = stmt.where(Ministry.deactivated_at.is_(None))
    return stmt.order_by(func.lower(Ministry.name), Ministry.id)


def _heads_by_ministry(
    session: Session, ministry_ids: list[int]
) -> dict[int, list[MinistryHeadSummary]]:
    """Active heads of every listed ministry, in one query.

    Mirrors :func:`app.services.authorization.require_ministry_operator`
    exactly -- an **active** membership carrying ``is_ministry_head``, and an
    **active** person -- so the name in this column is always somebody who
    could actually act. A deactivated head still holds the flag (deactivating a
    Person deliberately clears nothing), but every authorization check refuses
    them first, so listing them here as the ministry's leader would tell an
    Admin to ask somebody who cannot help.
    """
    rows = session.execute(
        select(
            MinistryMembership.ministry_id.label("ministry_id"),
            Person.id.label("person_id"),
            Person.display_name.label("display_name"),
        )
        .join(Person, Person.id == MinistryMembership.person_id)
        .where(
            MinistryMembership.ministry_id.in_(ministry_ids),
            MinistryMembership.is_ministry_head.is_(True),
            MinistryMembership.deactivated_at.is_(None),
            Person.deactivated_at.is_(None),
        )
        .order_by(
            MinistryMembership.ministry_id,
            func.lower(Person.display_name),
            Person.id,
        )
    ).all()

    grouped: dict[int, list[MinistryHeadSummary]] = {}
    for row in rows:
        grouped.setdefault(row.ministry_id, []).append(
            MinistryHeadSummary(
                person_id=row.person_id, display_name=row.display_name
            )
        )
    return grouped


def _active_member_counts(
    session: Session, ministry_ids: list[int]
) -> dict[int, int]:
    """How many people are actively on each ministry, in one aggregate.

    Counted in the database rather than by measuring a fetched list: a list
    would mean shipping every membership of every ministry across the wire to
    be discarded after ``len()``.
    """
    rows = session.execute(
        select(
            MinistryMembership.ministry_id.label("ministry_id"),
            func.count().label("member_count"),
        )
        .where(
            MinistryMembership.ministry_id.in_(ministry_ids),
            MinistryMembership.deactivated_at.is_(None),
        )
        .group_by(MinistryMembership.ministry_id)
    ).all()
    return {row.ministry_id: row.member_count for row in rows}


def _current_periods(
    session: Session, ministry_ids: list[int], *, today: datetime.date
) -> dict[int, MinistryPeriodSummary]:
    """One period per ministry, plus that period's latest schedule version.

    **Two queries, not one per ministry.** The first picks the period with
    PostgreSQL's ``DISTINCT ON``: ordered so that a period containing today
    wins outright, and otherwise the most recently started one does. Expressing
    the preference as an ``ORDER BY`` key rather than two separate queries is
    what keeps "the one it is in, or failing that the last one" a single pass.

    The second reads the highest-numbered version of each of those periods'
    schedules -- **any** status, unlike
    :func:`app.services.sunday_conflict.authoritative_version_subquery`, and
    deliberately: this column reports where the ministry has got to, so a draft
    nobody has finalized is exactly the interesting answer. Nothing scheduling
    depends on it.
    """
    contains_today = and_(
        SchedulingPeriod.start_date <= today, SchedulingPeriod.end_date >= today
    )
    period_rows = session.execute(
        select(
            SchedulingPeriod.id.label("scheduling_period_id"),
            SchedulingPeriod.ministry_id.label("ministry_id"),
            SchedulingPeriod.name.label("name"),
            SchedulingPeriod.start_date.label("start_date"),
            SchedulingPeriod.end_date.label("end_date"),
            contains_today.label("is_current"),
        )
        .distinct(SchedulingPeriod.ministry_id)
        .where(SchedulingPeriod.ministry_id.in_(ministry_ids))
        .order_by(
            SchedulingPeriod.ministry_id,
            contains_today.desc(),
            SchedulingPeriod.start_date.desc(),
            SchedulingPeriod.id.desc(),
        )
    ).all()
    if not period_rows:
        return {}

    period_ids = [row.scheduling_period_id for row in period_rows]
    version_rows = session.execute(
        select(
            Schedule.scheduling_period_id.label("scheduling_period_id"),
            ScheduleVersion.version_number.label("version_number"),
            ScheduleVersion.status.label("status"),
        )
        .join(Schedule, Schedule.id == ScheduleVersion.schedule_id)
        .distinct(Schedule.scheduling_period_id)
        .where(Schedule.scheduling_period_id.in_(period_ids))
        .order_by(
            Schedule.scheduling_period_id, ScheduleVersion.version_number.desc()
        )
    ).all()
    versions = {row.scheduling_period_id: row for row in version_rows}

    summaries: dict[int, MinistryPeriodSummary] = {}
    for row in period_rows:
        version = versions.get(row.scheduling_period_id)
        summaries[row.ministry_id] = MinistryPeriodSummary(
            scheduling_period_id=row.scheduling_period_id,
            name=row.name,
            start_date=row.start_date,
            end_date=row.end_date,
            is_current=bool(row.is_current),
            latest_version_number=None if version is None else version.version_number,
            latest_version_status=None if version is None else version.status,
        )
    return summaries
