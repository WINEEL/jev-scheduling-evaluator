"""My Schedule against real PostgreSQL rows (Task 77).

Rollback-isolated by the shared harness; nothing is committed. Every identity is
synthetic.

**The scenario these tests are built on is the one the endpoint exists for**: a
single synthetic Person who is a member of *two* ministries, with commitments in
both, plus a second person whose commitments must never appear. That shape is
what proves the two claims Task 77 rests on --

- the list aggregates through the **canonical Person**, so serving in two
  ministries produces one schedule rather than two halves; and
- a volunteer is never shown a **draft**, while a ministry head may preview
  their own.

Real PostgreSQL matters here because the visibility rule is a ``DISTINCT ON``
subquery over ``schedule_version``. That construct is PostgreSQL's, the
authoritative-version rule it generalizes is defined in the same terms (ADR
0003), and neither can be exercised by a fake session.
"""

from __future__ import annotations

import datetime

import pytest

from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services.my_schedule import (
    get_my_upcoming_schedule,
    managed_ministry_ids,
    visible_version_subquery,
)
from app.services.sunday_conflict import authoritative_version_subquery
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
TODAY = datetime.date(2026, 10, 1)
SOON = datetime.date(2026, 10, 11)
LATER = datetime.date(2026, 10, 18)
PAST = datetime.date(2026, 9, 6)
FINALIZED_AT = datetime.datetime(2026, 9, 20, tzinfo=UTC)


class World:
    """One synthetic church: two ministries, one person serving in both."""

    def __init__(self, session):
        self.session = session
        self.church = f.make_church(session)
        self.setup = f.make_ministry(session, church=self.church, name="SetupMin")
        self.av = f.make_ministry(session, church=self.church, name="AvMin")

        # The subject: ONE Person, two memberships. The whole point.
        self.person = f.make_person(session, church=self.church, name="Multi Ministry")
        self.setup_membership = f.make_membership(
            session, person=self.person, ministry=self.setup
        )
        self.av_membership = f.make_membership(
            session, person=self.person, ministry=self.av
        )

        # Somebody else, whose commitments must never leak into the answer.
        self.other = f.make_person(session, church=self.church, name="Somebody Else")
        self.other_membership = f.make_membership(
            session, person=self.other, ministry=self.setup
        )

        self.setup_role = f.make_role(session, ministry=self.setup, name="SetupRole")
        self.av_role = f.make_role(session, ministry=self.av, name="AvRole")

    def commit(
        self, ministry, role, membership, *, on, status, version_number=1,
        cancelled=False,
    ):
        """One assignment, with the whole schedule spine behind it."""
        period = f.make_period(
            self.session, ministry=ministry, start=PAST, end=datetime.date(2027, 1, 3)
        )
        event = f.make_event(
            self.session, period=period, event_date=on, cancelled=cancelled
        )
        schedule = f.make_schedule(self.session, period=period)
        version = f.make_version(
            self.session, schedule=schedule, period=period,
            version_number=version_number, status=status,
            finalized_at=FINALIZED_AT if status == SCHEDULE_VERSION_STATUS_FINALIZED else None,
        )
        requirement = f.make_version_requirement(
            self.session, version=version, event=event, role=role
        )
        assignment = f.make_assignment(
            self.session, requirement=requirement, membership=membership
        )
        self.session.flush()
        return assignment, version, schedule, period, event


@pytest.fixture
def world(db_session):
    return World(db_session)


def _schedule_for(world, actor=None):
    return get_my_upcoming_schedule(
        world.session, actor=actor or world.person, on_or_after=TODAY
    )


# ==========================================================================
# 1-6 -- Aggregation through the canonical Person
# ==========================================================================


def test_01_one_person_two_ministries_one_combined_schedule(world):
    """**The headline case.** Two memberships, two ministries, one list."""
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_FINALIZED)
    world.commit(world.av, world.av_role, world.av_membership,
                 on=LATER, status=SCHEDULE_VERSION_STATUS_FINALIZED)

    rows = _schedule_for(world)

    assert len(rows) == 2
    assert {r.ministry_name for r in rows} == {world.setup.name, world.av.name}
    assert [r.event_date for r in rows] == [SOON, LATER]


def test_02_both_commitments_on_the_same_day_are_both_returned(world):
    """Serving twice on one Sunday is a real thing and must not be collapsed."""
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_FINALIZED)
    world.commit(world.av, world.av_role, world.av_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_FINALIZED)

    rows = _schedule_for(world)

    assert len(rows) == 2
    assert {r.event_date for r in rows} == {SOON}
    # Ordered by ministry name within the day, so the list is stable.
    assert [r.ministry_name for r in rows] == sorted(
        [world.setup.name, world.av.name]
    )


def test_03_somebody_elses_commitment_never_appears(world):
    world.commit(world.setup, world.setup_role, world.other_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_FINALIZED)

    assert _schedule_for(world) == []
    assert len(_schedule_for(world, actor=world.other)) == 1


def test_04_the_role_and_ministry_are_named_not_just_referenced(world):
    """A volunteer may belong to no team and have nowhere to look an id up."""
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_FINALIZED)

    row = _schedule_for(world)[0]

    assert row.ministry_name == world.setup.name
    assert row.ministry_role_name == world.setup_role.name
    assert row.ministry_id == world.setup.id
    assert row.ministry_role_id == world.setup_role.id


def test_05_past_events_are_not_upcoming(world):
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=PAST, status=SCHEDULE_VERSION_STATUS_FINALIZED)

    assert _schedule_for(world) == []


def test_06_a_cancelled_event_is_not_somewhere_anybody_is_expected(world):
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_FINALIZED, cancelled=True)

    assert _schedule_for(world) == []


def test_06b_an_event_today_still_counts(world):
    """``>=``, not ``>``: somebody serving this morning must still see it."""
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=TODAY, status=SCHEDULE_VERSION_STATUS_FINALIZED)

    assert len(_schedule_for(world)) == 1


# ==========================================================================
# 7-14 -- A volunteer is never shown a draft
# ==========================================================================


def test_07_a_volunteer_does_not_see_a_draft(world):
    """**The rule Task 77 turns on.** A draft is a proposal nobody has stood
    behind; showing it to a volunteer is how somebody turns up on the wrong day.
    """
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_DRAFT)

    assert _schedule_for(world) == []


def test_08_a_volunteer_does_not_see_a_version_in_review_either(world):
    """REVIEW is still not finalized: a head is looking at it, not standing
    behind it.
    """
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_REVIEW)

    assert _schedule_for(world) == []


def test_09_a_volunteer_sees_a_finalized_schedule(world):
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_FINALIZED)

    rows = _schedule_for(world)

    assert len(rows) == 1
    assert rows[0].is_confirmed is True
    assert rows[0].schedule_version_status == SCHEDULE_VERSION_STATUS_FINALIZED


def test_10_a_head_previews_their_own_ministrys_draft_marked_unconfirmed(world):
    """A head may see their own working draft -- clearly not confirmed."""
    world.setup_membership.is_ministry_head = True
    world.session.flush()
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_DRAFT)

    rows = _schedule_for(world)

    assert len(rows) == 1
    assert rows[0].is_confirmed is False
    assert rows[0].schedule_version_status == SCHEDULE_VERSION_STATUS_DRAFT


def test_11_a_head_of_one_ministry_does_not_see_anothers_draft(world):
    """The boundary that matters: heading Setup grants nothing in AV."""
    world.setup_membership.is_ministry_head = True
    world.session.flush()
    world.commit(world.av, world.av_role, world.av_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_DRAFT)

    assert _schedule_for(world) == []


def test_12_an_inactive_head_membership_grants_no_preview(world):
    world.setup_membership.is_ministry_head = False
    world.setup_membership.deactivated_at = FINALIZED_AT
    world.session.flush()
    world.commit(world.setup, world.setup_role, world.setup_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_DRAFT)

    assert _schedule_for(world) == []


def test_13_an_admin_sees_their_own_draft_commitments_anywhere(world):
    world.person.is_admin = True
    world.session.flush()
    world.commit(world.av, world.av_role, world.av_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_DRAFT)

    rows = _schedule_for(world)

    assert len(rows) == 1
    assert rows[0].is_confirmed is False


def test_14_an_admin_still_only_sees_their_OWN_commitments(world):
    """Admin widens *which version speaks*, never *whose schedule it is*."""
    world.person.is_admin = True
    world.session.flush()
    world.commit(world.setup, world.setup_role, world.other_membership,
                 on=SOON, status=SCHEDULE_VERSION_STATUS_FINALIZED)

    assert _schedule_for(world) == []


# ==========================================================================
# 15-18 -- One version per schedule
# ==========================================================================


def test_15_a_superseded_finalized_version_does_not_also_appear(world):
    """v1 FINALIZED, v2 FINALIZED: only v2 speaks (ADR 0003)."""
    period = f.make_period(world.session, ministry=world.setup,
                           start=PAST, end=datetime.date(2027, 1, 3))
    event = f.make_event(world.session, period=period, event_date=SOON)
    schedule = f.make_schedule(world.session, period=period)
    for number in (1, 2):
        version = f.make_version(
            world.session, schedule=schedule, period=period, version_number=number,
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT,
        )
        requirement = f.make_version_requirement(
            world.session, version=version, event=event, role=world.setup_role
        )
        f.make_assignment(world.session, requirement=requirement,
                          membership=world.setup_membership)
    world.session.flush()

    rows = _schedule_for(world)

    assert len(rows) == 1


def test_16_a_head_sees_the_draft_successor_not_the_finalized_predecessor(world):
    """v1 FINALIZED, v2 DRAFT, and the reader manages the ministry: the higher
    version wins, and it is the one marked unconfirmed.
    """
    world.setup_membership.is_ministry_head = True
    world.session.flush()

    period = f.make_period(world.session, ministry=world.setup,
                           start=PAST, end=datetime.date(2027, 1, 3))
    event = f.make_event(world.session, period=period, event_date=SOON)
    schedule = f.make_schedule(world.session, period=period)
    for number, status in ((1, SCHEDULE_VERSION_STATUS_FINALIZED),
                           (2, SCHEDULE_VERSION_STATUS_DRAFT)):
        version = f.make_version(
            world.session, schedule=schedule, period=period, version_number=number,
            status=status,
            finalized_at=FINALIZED_AT if status == SCHEDULE_VERSION_STATUS_FINALIZED else None,
        )
        requirement = f.make_version_requirement(
            world.session, version=version, event=event, role=world.setup_role
        )
        f.make_assignment(world.session, requirement=requirement,
                          membership=world.setup_membership)
    world.session.flush()

    rows = _schedule_for(world)

    assert len(rows) == 1
    assert rows[0].schedule_version_status == SCHEDULE_VERSION_STATUS_DRAFT


def test_17_a_volunteer_sees_the_finalized_predecessor_of_that_same_pair(world):
    """The same two versions, read by somebody who manages nothing: the draft is
    invisible, so the finalized one still speaks. A newer draft must never
    *remove* a commitment a volunteer has already been told about.
    """
    period = f.make_period(world.session, ministry=world.setup,
                           start=PAST, end=datetime.date(2027, 1, 3))
    event = f.make_event(world.session, period=period, event_date=SOON)
    schedule = f.make_schedule(world.session, period=period)
    for number, status in ((1, SCHEDULE_VERSION_STATUS_FINALIZED),
                           (2, SCHEDULE_VERSION_STATUS_DRAFT)):
        version = f.make_version(
            world.session, schedule=schedule, period=period, version_number=number,
            status=status,
            finalized_at=FINALIZED_AT if status == SCHEDULE_VERSION_STATUS_FINALIZED else None,
        )
        requirement = f.make_version_requirement(
            world.session, version=version, event=event, role=world.setup_role
        )
        f.make_assignment(world.session, requirement=requirement,
                          membership=world.setup_membership)
    world.session.flush()

    rows = _schedule_for(world)

    assert len(rows) == 1
    assert rows[0].schedule_version_status == SCHEDULE_VERSION_STATUS_FINALIZED


def test_18_visible_reduces_to_authoritative_for_a_volunteer(db_session):
    """The generalization must not have changed the rule it generalizes.

    Compared by *result* against ADR 0003's own subquery, on rows arranged so a
    wrong reading would differ: a superseded FINALIZED v1, a newer DRAFT v2.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    period = f.make_period(db_session, ministry=ministry)
    schedule = f.make_schedule(db_session, period=period)
    for number, status in ((1, SCHEDULE_VERSION_STATUS_FINALIZED),
                           (2, SCHEDULE_VERSION_STATUS_DRAFT)):
        f.make_version(db_session, schedule=schedule, period=period,
                       version_number=number, status=status,
                       finalized_at=FINALIZED_AT if number == 1 else None)
    db_session.flush()

    from sqlalchemy import select

    volunteer_view = visible_version_subquery(
        managed_ministries=frozenset(), manages_every_ministry=False
    )
    mine = set(db_session.execute(select(volunteer_view.c.id)).scalars())
    theirs = set(
        db_session.execute(
            select(authoritative_version_subquery().c.id)
        ).scalars()
    )

    assert mine == theirs


# ==========================================================================
# 19-21 -- managed_ministry_ids mirrors require_ministry_manager
# ==========================================================================


def test_19_a_head_manages_exactly_their_active_head_ministries(world):
    world.setup_membership.is_ministry_head = True
    world.session.flush()

    managed, global_manager = managed_ministry_ids(world.person)

    assert managed == frozenset({world.setup.id})
    assert global_manager is False


def test_20_an_admin_manages_globally_rather_than_by_id(world):
    world.person.is_admin = True
    world.session.flush()

    managed, global_manager = managed_ministry_ids(world.person)

    assert global_manager is True
    assert managed == frozenset()


def test_21_a_deactivated_person_manages_nothing(world):
    world.person.is_admin = True
    world.setup_membership.is_ministry_head = True
    world.person.deactivated_at = FINALIZED_AT
    world.session.flush()

    managed, global_manager = managed_ministry_ids(world.person)

    assert managed == frozenset()
    assert global_manager is False
