"""The demo seed against real PostgreSQL.

**Only the seed *logic* runs here.** The script's URL guards are not relaxed,
bypassed or reconfigured: these tests call :func:`seed_demo_data` directly on
the integration harness's rollback-isolated Session, so nothing is committed
and the dedicated test branch is never actually seeded. The guards themselves
are covered offline, where their rules can be exercised without a database at
all -- and one of those rules is that this branch must never be a demo target.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from app.models.core import Church, Ministry, MinistryMembership, MinistryRole, Person
from app.models.scheduling_input import AVAILABILITY_UNAVAILABLE
from scripts.demo_dataset import (
    ADMIN_NAME,
    CHURCH_NAME,
    EXPECTED_REQUIRED_POSITIONS,
    EXPECTED_SUNDAYS,
    HEAD_NAME,
    LEAD_ROLE,
    MINISTRY_NAME,
    PERIOD_NAME,
    ROLE_NAMES,
    VOLUNTEERS,
    DemoDataError,
    find_demo_church,
    seed_demo_data,
    verify_demo_data,
)

pytestmark = pytest.mark.integration


def _count(session, sql: str, params: dict | None = None) -> int:
    return session.execute(text(sql), params or {}).scalar_one()


@pytest.fixture(autouse=True)
def no_committed_rows_leak(integration_engine):
    """Nothing this suite writes may outlive its transaction.

    Especially here: this is the one suite that creates a whole church.
    """

    def counts() -> dict[str, int]:
        with integration_engine.connect() as probe:
            return {
                table: probe.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in ("church", "person", "ministry", "scheduling_period", "schedule")
            }

    before = counts()
    yield
    assert counts() == before, "the demo seed committed rows that outlived the test"


# ==========================================================================
# 8: the seed creates the expected fictional structure
# ==========================================================================


def test_08_the_seed_creates_the_expected_structure(db_session):
    result = seed_demo_data(db_session)
    db_session.flush()

    assert result.already_existed is False
    assert result.demo_actor_person_id > 0
    assert result.admin_person_id > 0
    assert result.demo_actor_person_id != result.admin_person_id

    church = db_session.get(Church, result.church_id)
    assert church is not None and church.name == CHURCH_NAME

    ministry = db_session.get(Ministry, result.ministry_id)
    assert ministry is not None and ministry.name == MINISTRY_NAME
    assert "demo" in (ministry.description or "").lower()

    people = db_session.execute(
        text("SELECT display_name, email, is_admin FROM person WHERE church_id = :c"),
        {"c": church.id},
    ).all()
    assert len(people) == len(VOLUNTEERS) + 1
    assert {row.display_name for row in people} == (
        {v.display_name for v in VOLUNTEERS} | {ADMIN_NAME}
    )
    assert all(row.email.endswith("@demo.invalid") for row in people)
    assert sum(1 for row in people if row.is_admin) == 1

    roles = db_session.execute(
        text("SELECT name, display_order FROM ministry_role WHERE ministry_id = :m ORDER BY display_order"),
        {"m": ministry.id},
    ).all()
    assert [row.name for row in roles] == list(ROLE_NAMES)


def test_08b_the_demo_actor_is_an_active_ministry_head(db_session):
    """The whole point: this Person must work with the development actor
    header, which requires an active Person heading this ministry.
    """
    result = seed_demo_data(db_session)
    db_session.flush()

    head = db_session.get(Person, result.demo_actor_person_id)
    assert head is not None
    assert head.display_name == HEAD_NAME
    assert head.deactivated_at is None
    assert head.is_admin is False, "the demo actor is a Ministry Head, not an Admin"

    membership = db_session.execute(
        text(
            "SELECT is_ministry_head, deactivated_at FROM ministry_membership"
            " WHERE person_id = :p AND ministry_id = :m"
        ),
        {"p": head.id, "m": result.ministry_id},
    ).one()
    assert membership.is_ministry_head is True
    assert membership.deactivated_at is None


def test_08c_the_seed_writes_its_audit_history(db_session):
    """The parts that go through real services leave the real audit trail --
    the seed did not take a shortcut around them.
    """
    before = _count(db_session, "SELECT count(*) FROM audit_event")
    seed_demo_data(db_session)
    db_session.flush()

    assert _count(db_session, "SELECT count(*) FROM audit_event") > before


# ==========================================================================
# 9-12: the state the UI expects to find
# ==========================================================================


def test_09_the_seed_leaves_no_schedule_for_the_ui_to_find(db_session):
    """Starting the schedule is the button being demonstrated; the seed must
    not have pressed it.
    """
    result = seed_demo_data(db_session)
    db_session.flush()

    assert _count(
        db_session,
        "SELECT count(*) FROM schedule WHERE scheduling_period_id = :p",
        {"p": result.period_id},
    ) == 0
    assert _count(
        db_session,
        "SELECT count(*) FROM schedule_version WHERE scheduling_period_id = :p",
        {"p": result.period_id},
    ) == 0
    assert _count(db_session, "SELECT count(*) FROM assignment") == 0


def test_10_availability_is_locked_so_the_period_is_ready(db_session):
    result = seed_demo_data(db_session)
    db_session.flush()

    locked = db_session.execute(
        text("SELECT availability_locked_at, name FROM scheduling_period WHERE id = :p"),
        {"p": result.period_id},
    ).one()
    assert locked.availability_locked_at is not None
    assert locked.name == PERIOD_NAME


def test_11_every_event_is_one_of_the_expected_sundays(db_session):
    result = seed_demo_data(db_session)
    db_session.flush()

    rows = db_session.execute(
        text("SELECT event_date, cancelled_at FROM event WHERE scheduling_period_id = :p ORDER BY event_date"),
        {"p": result.period_id},
    ).all()

    assert [row.event_date for row in rows] == list(EXPECTED_SUNDAYS)
    assert all(row.event_date.weekday() == 6 for row in rows)
    assert all(row.cancelled_at is None for row in rows)


def test_12_staffing_is_one_person_per_role_per_sunday(db_session):
    result = seed_demo_data(db_session)
    db_session.flush()

    rows = db_session.execute(
        text(
            "SELECT sr.required_count FROM staffing_requirement sr"
            " JOIN event e ON e.id = sr.event_id"
            " WHERE e.scheduling_period_id = :p"
        ),
        {"p": result.period_id},
    ).all()

    assert len(rows) == EXPECTED_REQUIRED_POSITIONS == 20
    assert all(row.required_count == 1 for row in rows)


def test_12b_every_member_answered_for_every_sunday(db_session):
    """No reliance on a missing answer: the demo works without the frontend's
    "include people who have not answered" checkbox.
    """
    result = seed_demo_data(db_session)
    db_session.flush()

    rows = db_session.execute(
        text(
            "SELECT a.availability_state FROM availability a"
            " JOIN event e ON e.id = a.event_id"
            " WHERE e.scheduling_period_id = :p"
        ),
        {"p": result.period_id},
    ).all()

    assert len(rows) == len(VOLUNTEERS) * len(EXPECTED_SUNDAYS) == 28
    unavailable = sum(1 for row in rows if row.availability_state == AVAILABILITY_UNAVAILABLE)
    assert unavailable == sum(len(v.unavailable_on) for v in VOLUNTEERS) == 6


# ==========================================================================
# 13: Lead qualification really is restricted in the rows
# ==========================================================================


def test_13_lead_qualification_is_restricted_in_the_database(db_session):
    result = seed_demo_data(db_session)
    db_session.flush()

    lead_qualified = _count(
        db_session,
        "SELECT count(*) FROM role_qualification rq"
        " JOIN ministry_role r ON r.id = rq.ministry_role_id"
        " WHERE r.ministry_id = :m AND r.name = :role AND rq.is_qualified",
        {"m": result.ministry_id, "role": LEAD_ROLE},
    )
    everyone = len(VOLUNTEERS)

    assert lead_qualified == 2
    assert lead_qualified < everyone

    other_roles = _count(
        db_session,
        "SELECT count(*) FROM role_qualification rq"
        " JOIN ministry_role r ON r.id = rq.ministry_role_id"
        " WHERE r.ministry_id = :m AND r.name <> :role AND rq.is_qualified",
        {"m": result.ministry_id, "role": LEAD_ROLE},
    )
    assert other_roles == everyone * (len(ROLE_NAMES) - 1)


# ==========================================================================
# 14: everything created is a fictional demo record
# ==========================================================================


def test_14_every_created_person_is_a_fictional_demo_record(db_session):
    result = seed_demo_data(db_session)
    db_session.flush()

    rows = db_session.execute(
        text("SELECT display_name, email, phone FROM person WHERE church_id = :c"),
        {"c": result.church_id},
    ).all()

    expected = {v.display_name for v in VOLUNTEERS} | {ADMIN_NAME}
    for row in rows:
        assert row.display_name in expected
        assert row.email.endswith("@demo.invalid")
        assert row.phone is None


# ==========================================================================
# 15-16: running it again
# ==========================================================================


def test_15_running_the_seed_twice_creates_nothing_new(db_session):
    first = seed_demo_data(db_session)
    db_session.flush()

    before = {
        table: _count(db_session, f"SELECT count(*) FROM {table}")
        for table in ("church", "person", "ministry", "ministry_role", "scheduling_period",
                      "event", "staffing_requirement", "availability", "role_qualification")
    }

    second = seed_demo_data(db_session)
    db_session.flush()

    after = {table: _count(db_session, f"SELECT count(*) FROM {table}") for table in before}

    assert after == before, "a second run must create nothing"
    assert second.already_existed is True
    assert second.demo_actor_person_id == first.demo_actor_person_id
    assert second.church_id == first.church_id
    assert second.ministry_id == first.ministry_id
    assert second.period_id == first.period_id


def test_16_a_partial_demo_dataset_fails_instead_of_being_repaired(db_session):
    """A half-seeded database has no single correct fix. Guessing at one risks
    deleting rows somebody wanted, so the seed refuses and asks for the branch
    to be recreated.
    """
    result = seed_demo_data(db_session)
    db_session.flush()

    # Simulate an interrupted seed: the church is there, but the ministry the
    # demo expects is not. Renamed rather than deleted, because deleting it
    # would fight the foreign keys that (correctly) protect the rows hanging
    # off it -- and what is being tested is the seed's reaction to an absent
    # expected row, not PostgreSQL's referential integrity.
    db_session.execute(
        text("UPDATE ministry SET name = :n WHERE id = :m"),
        {"n": "Something Else", "m": result.ministry_id},
    )
    db_session.flush()

    with pytest.raises(DemoDataError, match="incomplete"):
        seed_demo_data(db_session)


def test_16b_a_duplicated_demo_church_fails_rather_than_guessing(db_session):
    seed_demo_data(db_session)
    db_session.flush()

    db_session.add(Church(name=CHURCH_NAME, timezone="UTC"))
    db_session.flush()

    with pytest.raises(DemoDataError, match="expected at most"):
        find_demo_church(db_session)


def test_16c_missing_people_fail_rather_than_being_topped_up(db_session):
    result = seed_demo_data(db_session)
    db_session.flush()

    # One expected volunteer is no longer present under the name the demo
    # knows. Renamed for the same reason as above.
    db_session.execute(
        text("UPDATE person SET display_name = :n WHERE church_id = :c AND display_name = :old"),
        {"n": "Someone Else", "c": result.church_id, "old": VOLUNTEERS[-1].display_name},
    )
    db_session.flush()

    with pytest.raises(DemoDataError, match="missing some of its people"):
        seed_demo_data(db_session)


# ==========================================================================
# Verification helper
# ==========================================================================


def test_the_verification_reports_a_healthy_seed(db_session):
    seed_demo_data(db_session)
    db_session.flush()

    verification = verify_demo_data(db_session)

    assert verification.problems == ()
    assert verification.churches == 1
    assert verification.people == len(VOLUNTEERS) + 1
    assert verification.roles == len(ROLE_NAMES)
    assert verification.events == len(EXPECTED_SUNDAYS)
    assert verification.staffing_requirements == EXPECTED_REQUIRED_POSITIONS
    assert verification.availability_locked is True
    assert verification.schedules == 0


def test_the_verification_notices_a_schedule_that_should_not_exist(db_session):
    """The check that matters most for the demo: if a schedule already exists,
    the button being demonstrated has nothing to do.
    """
    result = seed_demo_data(db_session)
    db_session.flush()

    db_session.execute(
        text("INSERT INTO schedule (scheduling_period_id) VALUES (:p)"),
        {"p": result.period_id},
    )
    db_session.flush()

    assert "expected no schedule yet -- the UI starts it" in verify_demo_data(db_session).problems
