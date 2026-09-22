"""The entry path into scheduling: what can be scheduled, and starting one.

Two small read/compose operations that a Ministry Head needs *before* the
scheduling flow proper begins:

- :func:`list_ministry_scheduling_periods` -- which of this ministry's periods
  exist, whether availability is locked on each, and whether scheduling has
  already started.
- :func:`start_first_schedule` -- begin scheduling one of them.

**Neither owns a domain rule.** Reading the list is
:func:`app.services.authorization.require_ministry_reader` -- an oversight read,
so an Admin sees any ministry's periods; creating the first schedule is Task 20's
:func:`app.services.schedule_version.create_initial_schedule_version`, called
here and not reimplemented. This module exists so the HTTP layer has one place
to ask those questions from, rather than assembling a domain query and a
domain rule in a route.

**"First schedule", not "version 1".** The product vocabulary for this flow is
deliberately plain -- a head starts scheduling a period, and later opens the
schedule they started. ``ScheduleVersion`` remains the storage concept, and
successor versions, carry-forward and amendment all remain elsewhere: nothing
here creates or consults them.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, Person
from app.models.schedule_output import (
    Schedule,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import SchedulingPeriod
from app.services.authorization import require_ministry_reader
from app.services.errors import InvalidOperationError
from app.services.schedule_version import create_initial_schedule_version

__all__ = [
    "LatestScheduleSummary",
    "MinistrySchedulingPeriods",
    "SchedulingPeriodSummary",
    "StartedSchedule",
    "list_ministry_scheduling_periods",
    "start_first_schedule",
]


@dataclass(frozen=True, slots=True)
class LatestScheduleSummary:
    """Where a period's scheduling has got to -- the newest version only.

    **Deliberately one version, not a history.** This answers "is there a
    schedule, and which one should I open?", which is what a landing screen
    needs. The full contents of a version, and any comparison between
    versions, belong to the detail endpoint.

    ``latest_version_*`` is populated by the highest ``version_number``
    whatever its status: a period whose newest version is FINALIZED has
    finished scheduling, and hiding that would make the screen claim nothing
    had happened.
    """

    schedule_id: int
    latest_version_id: int | None
    latest_version_number: int | None
    latest_version_status: str | None


@dataclass(frozen=True, slots=True)
class SchedulingPeriodSummary:
    """One period, and whether it is ready to schedule or already started."""

    scheduling_period_id: int
    name: str
    start_date: datetime.date
    end_date: datetime.date
    #: ``None`` while availability is still open. Starting a schedule requires
    #: it to be set -- that rule is Task 20's, not this module's.
    availability_locked_at: datetime.datetime | None
    #: ``None`` when scheduling has not started for this period.
    schedule: LatestScheduleSummary | None


@dataclass(frozen=True, slots=True)
class MinistrySchedulingPeriods:
    """A ministry's periods, for someone allowed to manage it."""

    ministry: Ministry
    periods: tuple[SchedulingPeriodSummary, ...]


@dataclass(frozen=True, slots=True)
class StartedSchedule:
    """What starting a schedule produced, for the caller to navigate to.

    ``requirement_snapshot_count`` is how many required positions the new
    schedule froze in place -- the single most useful confirmation that the
    period was configured before scheduling began, since a period with no
    staffing requirements produces a schedule with nothing to fill.
    """

    version: ScheduleVersion
    requirement_snapshot_count: int


def list_ministry_scheduling_periods(
    session: Session,
    *,
    actor: Person,
    ministry: Ministry,
) -> MinistrySchedulingPeriods:
    """``ministry``'s scheduling periods, newest scheduling state included.

    **Read-only**: only ``session.execute``, no writes of any kind.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Head of this ministry.
    """
    require_ministry_reader(actor, ministry_id=ministry.id)

    rows = session.execute(_periods_statement(ministry.id)).all()
    return MinistrySchedulingPeriods(
        ministry=ministry,
        periods=tuple(_period_summary(row) for row in rows),
    )


def start_first_schedule(
    session: Session,
    *,
    actor: Person,
    period: SchedulingPeriod,
    notes: str | None = None,
) -> StartedSchedule:
    """Begin scheduling ``period``: its Schedule, and its first DRAFT version.

    Delegates entirely to Task 20, which owns every rule that governs whether
    this may happen -- the actor's authority, availability being locked, and
    the refusal to treat a second call as a successor. Nothing here re-checks
    or restates them.

    The flush afterwards is the ordinary service convention (see
    :mod:`app.services`): Task 20 leaves the snapshot rows pending because
    *it* needs none of their identities, and counting them is a later
    statement in the same operation that does. It commits nothing; the
    caller's transaction still owns the boundary, and a failure after it rolls
    the whole attempt back.

    :raises AuthorizationError: the actor may not manage this period's ministry.
    :raises InvalidOperationError: availability is still open, ``notes`` is
        blank, or this period's schedule already has a version.
    """
    version = create_initial_schedule_version(
        session, actor=actor, period=period, notes=notes
    )
    session.flush()

    return StartedSchedule(
        version=version,
        requirement_snapshot_count=_snapshot_count(session, version.id),
    )


def _latest_version_subquery():
    """The newest version of each schedule -- one row per schedule.

    ``DISTINCT ON (schedule_id) ... ORDER BY schedule_id, version_number DESC``
    is the same idiom :mod:`app.services.sunday_conflict` uses for the
    authoritative version, and it is what keeps this a single query rather
    than one lookup per period. It differs in one deliberate way: **no status
    filter**. "Which schedule should I open?" is answered by the newest
    version there is, not the newest agreed one.
    """
    return (
        select(
            ScheduleVersion.schedule_id.label("schedule_id"),
            ScheduleVersion.id.label("version_id"),
            ScheduleVersion.version_number.label("version_number"),
            ScheduleVersion.status.label("status"),
        )
        .distinct(ScheduleVersion.schedule_id)
        .order_by(ScheduleVersion.schedule_id, ScheduleVersion.version_number.desc())
        .subquery()
    )


def _periods_statement(ministry_id: int) -> Select:
    """Every period of one ministry, with its schedule and newest version.

    Two ``LEFT OUTER JOIN``s, because both are genuinely optional: a period
    need not have started scheduling, and -- though Task 20 never leaves one
    that way -- a Schedule row could exist with no version yet.

    Ordered by ``start_date``, ``end_date``, ``name``, ``id``: fully
    determined, so two reads of unchanged data return the same list rather
    than whatever order PostgreSQL finds convenient.
    """
    latest = _latest_version_subquery()
    return (
        select(
            SchedulingPeriod.id,
            SchedulingPeriod.name,
            SchedulingPeriod.start_date,
            SchedulingPeriod.end_date,
            SchedulingPeriod.availability_locked_at,
            Schedule.id.label("schedule_id"),
            latest.c.version_id,
            latest.c.version_number,
            latest.c.status,
        )
        .select_from(SchedulingPeriod)
        .outerjoin(Schedule, Schedule.scheduling_period_id == SchedulingPeriod.id)
        .outerjoin(latest, latest.c.schedule_id == Schedule.id)
        .where(SchedulingPeriod.ministry_id == ministry_id)
        .order_by(
            SchedulingPeriod.start_date,
            SchedulingPeriod.end_date,
            SchedulingPeriod.name,
            SchedulingPeriod.id,
        )
    )


def _period_summary(row) -> SchedulingPeriodSummary:
    return SchedulingPeriodSummary(
        scheduling_period_id=row.id,
        name=row.name,
        start_date=row.start_date,
        end_date=row.end_date,
        availability_locked_at=row.availability_locked_at,
        schedule=(
            None
            if row.schedule_id is None
            else LatestScheduleSummary(
                schedule_id=row.schedule_id,
                latest_version_id=row.version_id,
                latest_version_number=row.version_number,
                latest_version_status=row.status,
            )
        ),
    )


def _snapshot_count(session: Session, schedule_version_id: int) -> int:
    return session.execute(
        select(func.count())
        .select_from(ScheduleVersionRequirement)
        .where(ScheduleVersionRequirement.schedule_version_id == schedule_version_id)
    ).scalar_one()
