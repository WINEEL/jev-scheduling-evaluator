"""One person's own upcoming assignments, across every ministry they serve in.

**The question this answers is different from every other query in this
package.** The scheduling services ask "who is committed at this ministry's
events?" -- a ministry-scoped question asked by somebody managing that ministry.
This asks "where am I expected, anywhere?", and the person asking may manage
nothing at all. That difference drives both decisions below.

Identity: the canonical Person, never a membership
--------------------------------------------------
Assignments hang off :class:`MinistryMembership`, which is ministry-scoped by
design (ADR 0001) -- one row per person per ministry. So a volunteer who serves
in Setup and AV has two memberships and would have two partial schedules if this
aggregated by membership. It aggregates by ``person_id`` instead, which is the
church-wide identity ADR 0001 exists to provide, so the answer is one list
covering everything that person is committed to.

That is also why a duplicated Person is a correctness bug for this endpoint and
not merely untidy data: two Person rows for one human split their schedule in
two, and each half looks complete. See
:mod:`scripts.person_mapping` for the explicit mechanism that prevents it at
import time, and note that nothing here tries to repair it by matching names --
guessing that two rows are the same human is exactly the mistake that would put
somebody else's schedule in front of a volunteer.

Visibility: which version speaks
--------------------------------
**A volunteer must never be shown a draft.** A draft is a proposal a ministry
head has not yet stood behind; telling somebody they are serving on the 11th
because an unreviewed draft says so is worse than telling them nothing. ADR 0003
already settled what "the schedule" means -- the highest-numbered ``FINALIZED``
version of each schedule -- and :func:`authoritative_version_subquery` is that
rule, written once.

:func:`visible_version_subquery` **generalizes** that rule rather than restating
it: per schedule it takes the highest-numbered version the caller is *allowed*
to see, where a manager of the ministry may also see a working draft and
everybody else may not. For a caller who manages nothing it reduces to exactly
``authoritative_version_subquery`` -- which is asserted by a test rather than
left as a claim.

**One version per schedule, never two.** A person may appear in both a finalized
v1 and a draft v2 of the same schedule. Showing both would list the same Sunday
twice with different roles and no way to tell which is real, so the higher
version wins and the other is not returned.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_FINALIZED,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import Event, SchedulingPeriod

__all__ = [
    "MyAssignment",
    "get_my_upcoming_schedule",
    "managed_ministry_ids",
    "visible_version_subquery",
]


@dataclass(frozen=True, slots=True)
class MyAssignment:
    """One place this person is expected, as they need to read it.

    Carries the ministry and role by **name** as well as by id because this is
    the one screen whose reader may belong to no ministry team and have no way
    to look either up.

    ``is_confirmed`` is the field that matters most. It is ``True`` only for a
    ``FINALIZED`` version -- a schedule a ministry head has stood behind. A
    manager previewing their own draft sees ``False``, and the UI is expected to
    say so plainly rather than let a proposal read as a commitment.
    """

    assignment_id: int
    event_id: int
    event_date: datetime.date
    event_kind: str
    event_name: str | None
    ministry_id: int
    ministry_name: str
    ministry_role_id: int
    ministry_role_name: str
    schedule_version_id: int
    schedule_version_status: str
    is_confirmed: bool


def managed_ministry_ids(actor: Person) -> tuple[frozenset[int], bool]:
    """Which ministries ``actor`` may see working drafts for.

    Returns ``(ministry_ids, is_global)``. ``is_global`` is ``True`` for an
    active Admin, who may manage any ministry -- including ones they are not a
    member of, so no id set could represent them.

    Mirrors :func:`app.services.authorization.require_ministry_reader` exactly:
    an active Admin, or an **active** membership carrying ``is_ministry_head``.
    A deactivated Person manages nothing, whatever their flags say.

    Read from ``actor.ministry_memberships`` rather than by a fresh query, the
    same way ``require_ministry_reader`` does, so both answers come from one
    loaded collection and cannot disagree within a request.
    """
    if actor.deactivated_at is not None:
        return frozenset(), False
    if actor.is_admin:
        return frozenset(), True
    return (
        frozenset(
            membership.ministry_id
            for membership in actor.ministry_memberships
            if membership.is_ministry_head and membership.deactivated_at is None
        ),
        False,
    )


def visible_version_subquery(
    *, managed_ministries: frozenset[int], manages_every_ministry: bool
):
    """Per schedule, the highest-numbered version this caller may read.

    A generalization of :func:`app.services.sunday_conflict.authoritative_version_subquery`,
    and deliberately written as the same ``DISTINCT ON (schedule_id) ... ORDER BY
    schedule_id, version_number DESC`` construct ADR 0003 specifies, so it uses
    the same partial index.

    The one added clause is the permission: a version qualifies if it is
    ``FINALIZED`` **or** belongs to a ministry this caller manages. So

    - a volunteer sees the authoritative version and nothing else;
    - a ministry head sees their own ministry's latest version, draft included,
      and still only the authoritative version of every other ministry;
    - an Admin sees the latest version everywhere.

    When the caller manages nothing the permission clause is dropped entirely
    rather than compiled as ``IN ()``, which keeps the statement identical to
    the authoritative one -- the property
    ``test_visible_reduces_to_authoritative_for_a_volunteer`` checks.
    """
    finalized = ScheduleVersion.status == SCHEDULE_VERSION_STATUS_FINALIZED

    if manages_every_ministry:
        permitted = None  # every version qualifies
    elif managed_ministries:
        permitted = or_(
            finalized, SchedulingPeriod.ministry_id.in_(sorted(managed_ministries))
        )
    else:
        permitted = finalized

    statement = (
        select(ScheduleVersion.id)
        .distinct(ScheduleVersion.schedule_id)
        .join(
            SchedulingPeriod,
            SchedulingPeriod.id == ScheduleVersion.scheduling_period_id,
        )
        .order_by(ScheduleVersion.schedule_id, ScheduleVersion.version_number.desc())
    )
    if permitted is not None:
        statement = statement.where(permitted)
    return statement.subquery()


def _upcoming_assignments_statement(
    *, person_id: int, on_or_after: datetime.date, visible_versions
) -> Select:
    """Every assignment this person can see, from today forward.

    Joined rather than lazy-loaded: this is one screen rendering one list, and
    walking relationships per row would turn it into a query per assignment.

    **Cancelled events are excluded.** An event the ministry has called off is
    not somewhere anybody is expected, and leaving it in would have people turn
    up.

    Ordered by date first because that is the order the reader thinks in, then
    by ministry name and the role's own display order so two commitments on one
    Sunday appear in a stable, meaningful sequence rather than by row id.
    """
    return (
        select(
            Assignment.id,
            Event.id,
            Event.event_date,
            Event.event_kind,
            Event.name,
            Ministry.id,
            Ministry.name,
            MinistryRole.id,
            MinistryRole.name,
            ScheduleVersion.id,
            ScheduleVersion.status,
        )
        .join(
            MinistryMembership,
            MinistryMembership.id == Assignment.ministry_membership_id,
        )
        .join(Event, Event.id == Assignment.event_id)
        .join(Ministry, Ministry.id == Assignment.ministry_id)
        .join(
            ScheduleVersionRequirement,
            ScheduleVersionRequirement.id == Assignment.schedule_version_requirement_id,
        )
        .join(
            MinistryRole,
            MinistryRole.id == ScheduleVersionRequirement.ministry_role_id,
        )
        .join(ScheduleVersion, ScheduleVersion.id == Assignment.schedule_version_id)
        .where(
            MinistryMembership.person_id == person_id,
            Assignment.schedule_version_id.in_(select(visible_versions.c.id)),
            Event.event_date >= on_or_after,
            Event.cancelled_at.is_(None),
        )
        .order_by(
            Event.event_date,
            Ministry.name,
            MinistryRole.display_order,
            MinistryRole.name,
            Assignment.id,
        )
    )


def get_my_upcoming_schedule(
    session: Session, *, actor: Person, on_or_after: datetime.date
) -> list[MyAssignment]:
    """Where ``actor`` is expected, from ``on_or_after`` onward, across every ministry.

    ``on_or_after`` is passed in rather than read from the clock here, so "what
    counts as upcoming" is decided once, at the boundary, in the church's own
    timezone -- and so a test can ask for a fixed day instead of pinning the
    system clock.

    **There is no ministry parameter and no actor parameter.** The caller cannot
    ask for somebody else's schedule: the person is taken from the authenticated
    actor, and the only way to reach another person's rows would be to hold their
    session. That is why this service needs no authorization check of its own --
    it is not "may you see this?", it is "this is yours".

    A deactivated actor gets an empty list: :func:`managed_ministry_ids` grants
    them nothing, and the endpoint above refuses them anyway.
    """
    managed, global_manager = managed_ministry_ids(actor)
    visible = visible_version_subquery(
        managed_ministries=managed, manages_every_ministry=global_manager
    )
    rows = session.execute(
        _upcoming_assignments_statement(
            person_id=actor.id, on_or_after=on_or_after, visible_versions=visible
        )
    ).all()

    return [
        MyAssignment(
            assignment_id=row[0],
            event_id=row[1],
            event_date=row[2],
            event_kind=row[3],
            event_name=row[4],
            ministry_id=row[5],
            ministry_name=row[6],
            ministry_role_id=row[7],
            ministry_role_name=row[8],
            schedule_version_id=row[9],
            schedule_version_status=row[10],
            is_confirmed=row[10] == SCHEDULE_VERSION_STATUS_FINALIZED,
        )
        for row in rows
    ]
