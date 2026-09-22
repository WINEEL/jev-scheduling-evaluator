"""Explicit person reuse across two ministry imports (Task 77, items 9-11).

Rollback-isolated; nothing is committed. Every identity is synthetic.

**This is the test that joins the two halves of Task 77.** It imports the same
human into two ministries and checks the thing that actually matters: with the
mapping, they are one Person with two memberships and *one* combined schedule;
without it, they are two Person rows and two half-schedules that each look
complete.

Real PostgreSQL is needed because the checks being exercised are database facts
-- an existing membership, a church mismatch, a deactivated row -- and because
the resulting schedule is read back through the same ``DISTINCT ON`` query the
endpoint uses.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import func, select

from app.models.core import MinistryMembership, Person
from app.models.schedule_output import SCHEDULE_VERSION_STATUS_FINALIZED
from app.services.my_schedule import get_my_upcoming_schedule
from scripts.person_mapping import PersonMap, PersonMappingError, resolve_person_map
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
TODAY = datetime.date(2026, 10, 1)
SOON = datetime.date(2026, 10, 11)
LATER = datetime.date(2026, 10, 18)
FINALIZED_AT = datetime.datetime(2026, 9, 20, tzinfo=UTC)

SHARED_NAME = "Serves In Both"


def _finalized_commitment(session, *, ministry, role, membership, on):
    period = f.make_period(session, ministry=ministry, start=TODAY,
                           end=datetime.date(2027, 1, 3))
    event = f.make_event(session, period=period, event_date=on)
    schedule = f.make_schedule(session, period=period)
    version = f.make_version(session, schedule=schedule, period=period,
                             status=SCHEDULE_VERSION_STATUS_FINALIZED,
                             finalized_at=FINALIZED_AT)
    requirement = f.make_version_requirement(session, version=version, event=event,
                                             role=role)
    f.make_assignment(session, requirement=requirement, membership=membership)
    session.flush()


# ==========================================================================
# 1-2 -- The bug, and the fix, side by side
# ==========================================================================


def test_01_without_a_mapping_the_same_human_becomes_two_people(db_session):
    """**The duplicate bug, reproduced.** This is what the importer does today
    for anybody who serves in a second ministry -- and each half of the
    resulting schedule looks complete, which is why it is not self-evident.
    """
    church = f.make_church(db_session)
    setup = f.make_ministry(db_session, church=church, name="SetupMin")
    av = f.make_ministry(db_session, church=church, name="AvMin")

    # Two imports, each creating its own Person for the same human.
    first = f.make_person(db_session, church=church, name=SHARED_NAME)
    second = f.make_person(db_session, church=church, name=SHARED_NAME)
    first_membership = f.make_membership(db_session, person=first, ministry=setup)
    second_membership = f.make_membership(db_session, person=second, ministry=av)

    _finalized_commitment(db_session, ministry=setup, role=f.make_role(
        db_session, ministry=setup), membership=first_membership, on=SOON)
    _finalized_commitment(db_session, ministry=av, role=f.make_role(
        db_session, ministry=av), membership=second_membership, on=LATER)

    assert first.id != second.id
    # Each identity sees only half, with nothing to indicate the other exists.
    assert len(get_my_upcoming_schedule(db_session, actor=first, on_or_after=TODAY)) == 1
    assert len(get_my_upcoming_schedule(db_session, actor=second, on_or_after=TODAY)) == 1


def test_02_with_a_mapping_they_are_one_person_with_one_schedule(db_session):
    """**The fix.** The second import reuses the Person the operator named, so
    the volunteer has two memberships and one combined schedule.
    """
    church = f.make_church(db_session)
    setup = f.make_ministry(db_session, church=church, name="SetupMin")
    av = f.make_ministry(db_session, church=church, name="AvMin")

    person = f.make_person(db_session, church=church, name=SHARED_NAME)
    setup_membership = f.make_membership(db_session, person=person, ministry=setup)

    # The second import resolves the operator's map instead of creating a row.
    resolved = resolve_person_map(
        db_session,
        PersonMap(by_source_name={person.display_name.casefold(): person.id}),
        church_id=church.id,
        ministry_id=av.id,
        source_names=[person.display_name],
    )
    assert resolved[person.display_name.casefold()].id == person.id
    av_membership = f.make_membership(
        db_session, person=resolved[person.display_name.casefold()], ministry=av
    )

    _finalized_commitment(db_session, ministry=setup, role=f.make_role(
        db_session, ministry=setup), membership=setup_membership, on=SOON)
    _finalized_commitment(db_session, ministry=av, role=f.make_role(
        db_session, ministry=av), membership=av_membership, on=LATER)

    # One Person, two memberships, one schedule covering both ministries.
    same_name = db_session.execute(
        select(func.count()).select_from(Person)
        .where(Person.display_name == person.display_name)
    ).scalar_one()
    assert same_name == 1
    assert db_session.execute(
        select(func.count()).select_from(MinistryMembership)
        .where(MinistryMembership.person_id == person.id)
    ).scalar_one() == 2

    rows = get_my_upcoming_schedule(db_session, actor=person, on_or_after=TODAY)
    assert len(rows) == 2
    assert {r.ministry_name for r in rows} == {setup.name, av.name}
    assert [r.event_date for r in rows] == [SOON, LATER]


# ==========================================================================
# 3-7 -- Every check refuses, and refuses the whole import
# ==========================================================================


def test_03_a_person_id_that_does_not_exist_is_refused(db_session):
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)

    with pytest.raises(PersonMappingError, match="does not exist"):
        resolve_person_map(
            db_session,
            PersonMap(by_source_name={"alice example": 99_000_000}),
            church_id=church.id, ministry_id=ministry.id,
            source_names=["Alice Example"],
        )


def test_04_a_person_from_a_different_church_is_refused(db_session):
    ours = f.make_church(db_session)
    theirs = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=ours)
    outsider = f.make_person(db_session, church=theirs, name="Alice Example")

    with pytest.raises(PersonMappingError, match="different church"):
        resolve_person_map(
            db_session,
            PersonMap(by_source_name={"alice example": outsider.id}),
            church_id=ours.id, ministry_id=ministry.id,
            source_names=["Alice Example"],
        )


def test_05_a_deactivated_person_is_refused(db_session):
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    gone = f.make_person(db_session, church=church, name="Alice Example",
                         deactivated=True)

    with pytest.raises(PersonMappingError, match="deactivated"):
        resolve_person_map(
            db_session,
            PersonMap(by_source_name={"alice example": gone.id}),
            church_id=church.id, ministry_id=ministry.id,
            source_names=["Alice Example"],
        )


def test_06_someone_already_in_this_ministry_is_refused(db_session):
    """Re-importing the same ministry would give them a second membership in
    it. The unique constraint refuses that anyway -- this refuses it earlier,
    with an explanation instead of an IntegrityError.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    person = f.make_person(db_session, church=church, name="Alice Example")
    f.make_membership(db_session, person=person, ministry=ministry)

    with pytest.raises(PersonMappingError, match="already a member"):
        resolve_person_map(
            db_session,
            PersonMap(by_source_name={"alice example": person.id}),
            church_id=church.id, ministry_id=ministry.id,
            source_names=["Alice Example"],
        )


def test_07_one_bad_row_refuses_the_whole_map(db_session):
    """No partial application. Importing some volunteers onto existing people
    and duplicating the rest is the worst outcome and the hardest to spot.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    good = f.make_person(db_session, church=church, name="Alice Example")

    with pytest.raises(PersonMappingError):
        resolve_person_map(
            db_session,
            PersonMap(by_source_name={
                "alice example": good.id,
                "bob example": 99_000_000,   # does not exist
            }),
            church_id=church.id, ministry_id=ministry.id,
            source_names=["Alice Example", "Bob Example"],
        )

    # Nothing was written for the good row either.
    assert db_session.execute(
        select(func.count()).select_from(MinistryMembership)
        .where(MinistryMembership.person_id == good.id)
    ).scalar_one() == 0


def test_08_a_reused_person_keeps_the_email_they_sign_in_with(db_session):
    """The reason reuse matters for authentication as well as for scheduling:
    the existing row is the one Google sign-in already resolves to.
    """
    church = f.make_church(db_session)
    setup = f.make_ministry(db_session, church=church, name="SetupMin")
    av = f.make_ministry(db_session, church=church, name="AvMin")
    person = f.make_person(db_session, church=church, name=SHARED_NAME)
    person.email = "serves.in.both@example.test"
    f.make_membership(db_session, person=person, ministry=setup)
    db_session.flush()

    resolved = resolve_person_map(
        db_session,
        PersonMap(by_source_name={person.display_name.casefold(): person.id}),
        church_id=church.id, ministry_id=av.id,
        source_names=[person.display_name],
    )

    assert resolved[person.display_name.casefold()].email == "serves.in.both@example.test"
