"""The whole schedule lifecycle, over HTTP, against real rows (Task 80).

**What this file is for, and why it is not covered by the modules beside it.**
``test_pg_schedule_finalization.py`` proves the finalization *service*;
``test_pg_one_ministry_per_sunday.py`` proves the Sunday rule at every layer
including readiness; ``test_pg_my_schedule.py`` proves what a volunteer sees
for a version in each status. Each of those sets its subject up directly. What
none of them does is walk the journey a ministry actually takes --

    generate -> DRAFT -> submit -> REVIEW -> finalize -> the volunteer sees it

-- through the endpoints a browser calls, in one transaction, with nothing
staged by hand. That is the claim this task makes to a church, so it is the
claim tested here: not that each gate works, but that the chain does, and that
a volunteer's own screen changes exactly once, at the end of it.

The second subject is **who may walk it**. Task 80 took ministry operations
away from an Admin who heads no ministry, and finalization is the most
consequential of them. The authorization matrix below is asserted against real
memberships and the real endpoints, because "an Admin can still read it and can
no longer publish it" is the whole correction, and a stub could be told to say
either.

Harness: the shared one. Each test runs inside a connection-scoped transaction
that is always rolled back; the request's own ``commit()`` releases a savepoint,
so what a later request sees is what the earlier one really wrote.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    Schedule,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    Event,
    SchedulingPeriod,
)
from app.services.audit import (
    ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
    ACTION_SCHEDULE_VERSION_FINALIZED,
    ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW,
    record_audit_event,
)
from app.services.assignment_policy import BLOCKER_SUNDAY_CONFLICT
from app.services.my_schedule import get_my_upcoming_schedule
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
#: A Sunday, and the day every schedule in this file is about.
SUNDAY = datetime.date(2026, 11, 15)
BEFORE = datetime.date(2026, 11, 1)

DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"

VERSION_URL = "/api/v1/schedule-versions/{id}"
GENERATE_URL = VERSION_URL + "/generate"
SUBMIT_URL = VERSION_URL + "/submit-for-review"
FINALIZE_URL = VERSION_URL + "/finalize"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture
def api(db_session, monkeypatch) -> TestClient:
    """A client whose requests run the **real** ``get_session`` on the test's
    Session -- only the factory is redirected, so the commit, the rollback and
    the close are the shipped ones.
    """
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()
    monkeypatch.setattr(deps, "SessionLocal", lambda: db_session)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


class _World:
    """One ministry with one Sunday, one position, and two qualified,
    available volunteers -- plus every actor whose authority is in question.

    Two volunteers rather than one, because "no other person's assignment
    leaks" needs somebody else to be on the schedule at all.
    """

    def __init__(self, session, *, required_count: int = 1) -> None:
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Setup Head"
        )

        # The three actors Task 80 distinguishes, built as they really differ.
        self.admin = f.make_person(
            session, church=self.church, name="Elder", is_admin=True
        )
        self.admin_and_head = f.make_ministry_head(
            session,
            church=self.church,
            ministry=self.ministry,
            name="Elder And Head",
            is_admin=True,
        )
        self.other_ministry = f.make_ministry(session, church=self.church, name="AV")
        self.other_head = f.make_ministry_head(
            session, church=self.church, ministry=self.other_ministry, name="AV Head"
        )

        self.role = f.make_role(session, ministry=self.ministry, name="Position")
        self.period = f.make_period(session, ministry=self.ministry)
        self.event = f.make_event(session, period=self.period, event_date=SUNDAY)
        f.make_staffing_requirement(
            session, event=self.event, role=self.role, required_count=required_count
        )
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session,
            schedule=self.schedule,
            period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.requirement = f.make_version_requirement(
            session,
            version=self.version,
            event=self.event,
            role=self.role,
            required_count=required_count,
        )

        self.volunteers = []
        self.memberships = []
        for index in range(2):
            person = f.make_person(
                session, church=self.church, name=f"Volunteer{index}"
            )
            membership = f.make_membership(
                session, person=person, ministry=self.ministry
            )
            f.make_qualification(
                session,
                membership=membership,
                role=self.role,
                decided_by=self.head,
                is_qualified=True,
            )
            f.make_availability(
                session,
                membership=membership,
                event=self.event,
                state=AVAILABILITY_AVAILABLE,
            )
            self.volunteers.append(person)
            self.memberships.append(membership)

        session.flush()
        # Committed so the fixture sits *behind* the savepoint each request
        # opens: a request that rolls back discards its own work without
        # taking the world with it.
        session.commit()

        # **Ids, not objects, from here on.** The commit above expires every
        # instance, and each request closes the Session it borrowed -- so an
        # ORM object held across a request raises DetachedInstanceError on its
        # next attribute read. Ids survive that; anything else is re-fetched
        # through :meth:`get` when a test actually needs the row.
        self.version_id = self.version.id
        self.schedule_id = self.schedule.id
        self.period_id = self.period.id
        self.event_id = self.event.id
        self.role_id = self.role.id
        self.requirement_id = self.requirement.id
        self.ministry_id = self.ministry.id
        self.ministry_name = self.ministry.name
        self.other_ministry_id = self.other_ministry.id
        self.head_id = self.head.id
        self.admin_id = self.admin.id
        self.admin_and_head_id = self.admin_and_head.id
        self.other_head_id = self.other_head.id
        self.volunteer_ids = [person.id for person in self.volunteers]
        self.membership_ids = [membership.id for membership in self.memberships]

    def get(self, model, row_id):
        """One row, on the test's own Session, now."""
        return self.session.get(model, row_id)

    # -- request helpers ---------------------------------------------------

    def read(self, api, *, actor_id: int, version_id: int | None = None):
        return api.get(
            VERSION_URL.format(id=version_id or self.version_id),
            headers={HEADER: str(actor_id)},
        )

    def generate(self, api, *, actor_id: int | None = None, version_id: int | None = None):
        return api.post(
            GENERATE_URL.format(id=version_id or self.version_id),
            headers={HEADER: str(actor_id or self.head_id)},
            json={},
        )

    def submit(self, api, *, actor_id: int | None = None, body=None):
        return api.post(
            SUBMIT_URL.format(id=self.version_id),
            headers={HEADER: str(actor_id or self.head_id)},
            json={} if body is None else body,
        )

    def finalize(self, api, *, actor_id: int | None = None, body=None):
        return api.post(
            FINALIZE_URL.format(id=self.version_id),
            headers={HEADER: str(actor_id or self.head_id)},
            json={} if body is None else body,
        )

    # -- state helpers -----------------------------------------------------

    def status(self) -> str:
        return self.session.execute(
            select(ScheduleVersion.status).where(ScheduleVersion.id == self.version_id)
        ).scalar_one()

    def my_schedule(self, person_id: int):
        person = self.session.get(Person, person_id)
        return get_my_upcoming_schedule(self.session, actor=person, on_or_after=BEFORE)

    def audit_actions(self) -> list[str]:
        """This version's lifecycle audit rows, in the order they were written.

        Ordered explicitly: a query without one returns whatever PostgreSQL
        finds convenient, and "submitted, then finalized" is exactly the fact
        these tests are about.
        """
        return list(
            self.session.execute(
                select(AuditEvent.action)
                .where(
                    AuditEvent.target_table == "schedule_version",
                    AuditEvent.target_id == self.version_id,
                )
                .order_by(AuditEvent.id)
            ).scalars()
        )


@pytest.fixture
def world(db_session) -> _World:
    return _World(db_session)


def _assigned_person(world: _World) -> tuple[int, int]:
    """The ids of whichever volunteer the solver chose, and the other one.

    Read from the schedule rather than assumed: with two interchangeable
    people either is a correct answer, and a test that named one would fail
    for a reason that has nothing to do with the lifecycle.
    """
    membership_id = world.session.execute(
        select(Assignment.ministry_membership_id).where(
            Assignment.schedule_version_id == world.version_id
        )
    ).scalar_one()
    index = world.membership_ids.index(membership_id)
    return world.volunteer_ids[index], world.volunteer_ids[1 - index]


# ==========================================================================
# 1. The chain
# ==========================================================================


def test_01_the_whole_chain_ends_with_the_volunteer_seeing_their_own_turn(api, world):
    """generate -> DRAFT -> submit -> REVIEW -> finalize -> visible.

    The single most important assertion in this task, and it is made in one
    test on purpose: each step's own gate is covered elsewhere, and what is at
    stake here is that the steps connect -- that a schedule built through the
    product reaches the person standing at the front on Sunday.
    """
    assert world.generate(api).status_code == 200
    assert world.status() == SCHEDULE_VERSION_STATUS_DRAFT

    serving, not_serving = _assigned_person(world)

    # A draft tells nobody. Not the person on it, and not anybody else.
    assert world.my_schedule(serving) == []
    assert world.my_schedule(not_serving) == []

    submitted = world.submit(api)
    assert submitted.status_code == 200
    assert submitted.json()["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_REVIEW
    assert world.status() == SCHEDULE_VERSION_STATUS_REVIEW

    # Review is still private: a head submits a schedule *so that* it gets
    # checked, which is not the same as standing behind it.
    assert world.my_schedule(serving) == []

    finalized = world.finalize(api)
    assert finalized.status_code == 200, finalized.json()
    assert finalized.json()["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_FINALIZED
    assert world.status() == SCHEDULE_VERSION_STATUS_FINALIZED

    # And only now.
    mine = world.my_schedule(serving)
    assert len(mine) == 1
    assert mine[0].event_date == SUNDAY
    assert mine[0].ministry_name == world.ministry_name
    assert mine[0].is_confirmed is True


def test_02_no_other_persons_assignment_leaks_into_the_chain(api, world):
    """The volunteer who was not scheduled sees nothing, before or after.

    Not a restatement of test 01: there the second volunteer is idle, here the
    point is that finalization -- which makes assignments visible -- makes
    visible only the reader's own.
    """
    world.generate(api)
    world.submit(api)
    world.finalize(api)

    serving, not_serving = _assigned_person(world)

    assert len(world.my_schedule(serving)) == 1
    assert world.my_schedule(not_serving) == []


def test_03_finalizing_is_the_only_step_that_changes_what_a_volunteer_sees(api, world):
    """Asserted as a sequence of observations rather than as a rule, because
    the rule is easy to state and easy to break somewhere else: the boundary
    must move exactly once, at the last step."""
    world.generate(api)
    serving, _ = _assigned_person(world)

    seen = [len(world.my_schedule(serving))]
    world.submit(api)
    seen.append(len(world.my_schedule(serving)))
    world.finalize(api)
    seen.append(len(world.my_schedule(serving)))

    assert seen == [0, 0, 1]


def test_04_each_transition_writes_exactly_one_audit_row(api, world):
    world.generate(api)
    world.submit(api, body={"reason": "Checked with the team"})
    world.finalize(api, body={"reason": "Agreed at the meeting"})

    assert world.audit_actions() == [
        ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW,
        ACTION_SCHEDULE_VERSION_FINALIZED,
    ]

    rows = list(
        world.session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.target_table == "schedule_version",
                AuditEvent.target_id == world.version_id,
            )
            .order_by(AuditEvent.id)
        ).scalars()
    )
    for row in rows:
        assert row.actor_person_id == world.head_id
        assert row.ministry_id == world.ministry_id
    assert rows[0].reason == "Checked with the team"
    assert rows[0].after_values["status"] == SCHEDULE_VERSION_STATUS_REVIEW
    assert rows[1].reason == "Agreed at the meeting"
    assert rows[1].after_values["status"] == SCHEDULE_VERSION_STATUS_FINALIZED
    # The moment authority transferred, recorded as text so the JSONB payload
    # stays serializable -- not the whole schedule, and no volunteer's name.
    assert isinstance(rows[1].after_values["finalized_at"], str)


# ==========================================================================
# 2. Who may walk it
# ==========================================================================


@pytest.mark.parametrize("step", ["submit", "finalize"])
def test_05_an_admin_who_heads_nothing_is_refused_both_transitions(api, world, step):
    """**The correction Task 80 exists for**, against real membership rows.

    The same Elder may read every ministry in the church. Publishing one
    ministry's rota is not a read.
    """
    world.generate(api)
    world.submit(api)  # so `finalize` is refused for authority, not for status

    response = getattr(world, step)(api, actor_id=world.admin_id)

    assert response.status_code == 403
    assert "oversight, not operation" in response.json()["detail"]
    assert world.status() == SCHEDULE_VERSION_STATUS_REVIEW


def test_06_that_same_admin_may_still_read_the_version_in_full(api, world):
    """Oversight survives intact, and the response says so rather than leaving
    the client to guess: ``can_operate`` is false, which is what a read-only
    screen is rendered from."""
    world.generate(api)

    response = world.read(api, actor_id=world.admin_id)

    assert response.status_code == 200
    body = response.json()
    assert body["can_operate"] is False
    assert body["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_DRAFT
    # And the whole substance of it, not a redacted version.
    assert body["summary"]["required_positions"] == 1
    assert len(body["assignments"]) == 1
    assert "finalization_readiness" in body


@pytest.mark.parametrize("step", ["submit", "finalize"])
def test_07_a_head_of_another_ministry_is_refused(api, world, step):
    world.generate(api)
    world.submit(api)

    assert getattr(world, step)(api, actor_id=world.other_head_id).status_code == 403


@pytest.mark.parametrize("step", ["submit", "finalize"])
def test_08_a_volunteer_is_refused(api, world, step):
    """Somebody on the schedule, and not running it."""
    world.generate(api)
    world.submit(api)

    response = getattr(world, step)(api, actor_id=world.volunteer_ids[0])

    assert response.status_code == 403


@pytest.mark.parametrize("step", ["submit", "finalize"])
def test_09_an_unauthenticated_request_is_401(api, world, step):
    url = (SUBMIT_URL if step == "submit" else FINALIZE_URL).format(id=world.version_id)

    assert api.post(url, json={}).status_code == 401


def test_10_an_admin_who_also_heads_the_ministry_may_walk_the_whole_chain(api, world):
    """And through the head membership, not the Admin flag -- which is why
    test 05 refuses the Elder who has only the flag."""
    assert world.generate(api, actor_id=world.admin_and_head_id).status_code == 200
    assert world.submit(api, actor_id=world.admin_and_head_id).status_code == 200
    assert world.finalize(api, actor_id=world.admin_and_head_id).status_code == 200

    assert world.status() == SCHEDULE_VERSION_STATUS_FINALIZED


def test_11_the_head_sees_can_operate_and_the_admin_does_not(api, world):
    world.generate(api)

    assert world.read(api, actor_id=world.head_id).json()["can_operate"] is True
    assert world.read(api, actor_id=world.admin_and_head_id).json()["can_operate"] is True
    assert world.read(api, actor_id=world.admin_id).json()["can_operate"] is False


def test_12_a_volunteer_cannot_even_read_the_version(api, world):
    """The read rule is Admin-or-head; a volunteer is neither, and their own
    schedule is reached through ``/me/schedule``, which needs no check at
    all."""
    assert world.read(api, actor_id=world.volunteer_ids[0]).status_code == 403


# ==========================================================================
# 3. Invalid transitions, and repeats
# ==========================================================================


def test_13_a_draft_cannot_be_finalized(api, world):
    """There is no DRAFT -> FINALIZED shortcut: a schedule passes through
    human review before it binds anybody."""
    world.generate(api)

    response = world.finalize(api)

    assert response.status_code == 409
    assert "REVIEW" in response.json()["detail"]
    assert world.status() == SCHEDULE_VERSION_STATUS_DRAFT


def test_14_submitting_twice_is_an_unaudited_no_op(api, world):
    world.generate(api)
    world.submit(api)

    second = world.submit(api)

    assert second.status_code == 200
    assert world.status() == SCHEDULE_VERSION_STATUS_REVIEW
    assert world.audit_actions() == [ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW]


def test_15_finalizing_twice_keeps_the_original_moment(api, world):
    world.generate(api)
    world.submit(api)
    first = world.finalize(api).json()["schedule_version"]["finalized_at"]

    second = world.finalize(api)

    assert second.status_code == 200
    assert second.json()["schedule_version"]["finalized_at"] == first
    assert world.audit_actions() == [
        ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW,
        ACTION_SCHEDULE_VERSION_FINALIZED,
    ]


def test_16_an_unfilled_version_cannot_be_finalized(api, db_session):
    """Readiness is a gate, not advice: the request is refused outright and
    the version stays exactly where it was."""
    world = _World(db_session, required_count=2)
    # Only one of the two positions is filled: the roster has two volunteers,
    # but the requirement asks for two and generation is not run here.
    f.make_assignment(
        db_session,
        requirement=world.get(ScheduleVersionRequirement, world.requirement_id),
        membership=world.get(MinistryMembership, world.membership_ids[0]),
    )
    db_session.commit()
    world.submit(api)

    response = world.finalize(api)

    assert response.status_code == 409
    assert "readiness issue" in response.json()["detail"]
    assert world.status() == SCHEDULE_VERSION_STATUS_REVIEW
    assert world.audit_actions() == [ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW]


def test_17_a_stale_draft_cannot_be_submitted(api, world):
    """Its remedy is a fresh version, so the transition is refused rather than
    the snapshot repaired -- snapshot rows are immutable history."""
    world.generate(api)
    # The ministry now needs two people on that Sunday; the version's snapshot
    # still says one.
    from app.services.staffing_requirement import set_staffing_requirement

    set_staffing_requirement(
        world.session,
        actor=world.get(Person, world.head_id),
        event=world.get(Event, world.event_id),
        role=world.get(MinistryRole, world.role_id),
        required_count=2,
    )
    world.session.commit()

    response = world.submit(api)

    assert response.status_code == 409
    assert "no longer matches" in response.json()["detail"]
    assert world.status() == SCHEDULE_VERSION_STATUS_DRAFT


# ==========================================================================
# 4. One ministry per person per Sunday, through the lifecycle
# ==========================================================================


def _commit_elsewhere_on_that_sunday(world: _World, person_id: int) -> None:
    """Finalize an AV schedule that already has ``person`` on ``SUNDAY``.

    Built as a real finalized version rather than an ``existing_commitment``
    row, because the conflict this rule guards is overwhelmingly two ministries
    each running their own schedule -- and because it proves the union query
    reads a sibling ministry's *authoritative* version, which is the half a
    hand-written commitment row would skip.
    """
    session = world.session
    person = session.get(Person, person_id)
    other_ministry = session.get(Ministry, world.other_ministry_id)
    av_role = f.make_role(session, ministry=other_ministry, name="Sound")
    av_period = f.make_period(session, ministry=other_ministry)
    av_event = f.make_event(session, period=av_period, event_date=SUNDAY)
    av_schedule = f.make_schedule(session, period=av_period)
    av_version = f.make_version(
        session,
        schedule=av_schedule,
        period=av_period,
        status=SCHEDULE_VERSION_STATUS_FINALIZED,
        finalized_at=datetime.datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    av_requirement = f.make_version_requirement(
        session, version=av_version, event=av_event, role=av_role
    )
    av_membership = f.make_membership(
        session, person=person, ministry=other_ministry
    )
    f.make_assignment(
        session, requirement=av_requirement, membership=av_membership
    )
    session.commit()


def test_18_a_cross_ministry_sunday_conflict_stops_finalization(api, world):
    """A version in REVIEW carrying this conflict must never become
    FINALIZED, however it got there."""
    world.generate(api)
    serving, _ = _assigned_person(world)
    world.submit(api)

    # AV publishes the same person on the same Sunday, after Setup's schedule
    # was built and reviewed. This is the realistic shape: nothing was wrong
    # when the draft was made.
    _commit_elsewhere_on_that_sunday(world, serving)

    response = world.finalize(api)

    assert response.status_code == 409
    assert world.status() == SCHEDULE_VERSION_STATUS_REVIEW
    # And the volunteer is not told they serve twice.
    assert len(world.my_schedule(serving)) == 1


def test_19_a_legacy_audited_sunday_override_still_cannot_finalize(api, world):
    """**Historical data that says it was allowed does not make it allowed.**

    The service will not create such a row any more, so one is reconstructed
    exactly as it would have been written while the conflict was briefly an
    overridable blocker: ``is_override`` set, with a real audit payload naming
    ``sunday_conflict``. The payload is truthful history -- somebody really did
    decide this while it was permitted -- and it authorizes nothing.
    """
    session = world.session
    volunteer_id = world.volunteer_ids[0]
    _commit_elsewhere_on_that_sunday(world, volunteer_id)

    assignment = f.make_assignment(
        session,
        requirement=world.get(ScheduleVersionRequirement, world.requirement_id),
        membership=world.get(MinistryMembership, world.membership_ids[0]),
        is_override=True,
        override_reason="Approved before the rule was locked",
    )
    session.flush()
    record_audit_event(
        session,
        actor=world.get(Person, world.head_id),
        action=ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
        target_table="assignment",
        target_id=assignment.id,
        ministry_id=world.ministry_id,
        summary="Legacy override",
        reason="Approved before the rule was locked",
        after_values={"overridden_blockers": [BLOCKER_SUNDAY_CONFLICT]},
    )
    session.commit()

    assert world.submit(api).status_code == 200

    response = world.finalize(api)

    assert response.status_code == 409
    assert world.status() == SCHEDULE_VERSION_STATUS_REVIEW
    assert len(world.my_schedule(volunteer_id)) == 1, "only the AV commitment"


# ==========================================================================
# 5. A finalized version is not rewritten
# ==========================================================================


def test_20_a_finalized_version_cannot_be_regenerated(api, world):
    """The promise finalization makes: nothing rewrites a published schedule.

    Refused at the generation endpoint, in the state the product actually
    reaches it in -- after a real finalize, not with a status set by hand.
    """
    world.generate(api)
    world.submit(api)
    world.finalize(api)
    before = list(
        world.session.execute(
            select(ScheduleVersion.status, ScheduleVersion.finalized_at).where(
                ScheduleVersion.id == world.version_id
            )
        ).all()
    )

    response = world.generate(api)

    assert response.status_code == 409
    assert "DRAFT" in response.json()["detail"]
    after = list(
        world.session.execute(
            select(ScheduleVersion.status, ScheduleVersion.finalized_at).where(
                ScheduleVersion.id == world.version_id
            )
        ).all()
    )
    assert after == before


def test_21_a_finalized_version_cannot_be_submitted_for_review_again(api, world):
    """There is no reverse transition of any kind."""
    world.generate(api)
    world.submit(api)
    world.finalize(api)

    response = world.submit(api)

    assert response.status_code == 409
    assert world.status() == SCHEDULE_VERSION_STATUS_FINALIZED


def test_22_a_superseded_version_is_refused_rather_than_reported_done(api, world):
    """Answering "yes, done" for a version an amendment has replaced would
    misstate which version is authoritative, so idempotency stops at the
    latest version."""
    world.generate(api)
    world.submit(api)
    f.make_version(
        world.session,
        schedule=world.get(Schedule, world.schedule_id),
        period=world.get(SchedulingPeriod, world.period_id),
        version_number=2,
        status=SCHEDULE_VERSION_STATUS_DRAFT,
    )
    world.session.commit()

    assert world.submit(api).status_code == 409
    assert world.finalize(api).status_code == 409
    assert world.status() == SCHEDULE_VERSION_STATUS_REVIEW


def test_23_the_correction_path_is_a_successor_version(api, world, db_session):
    """How an already-finalized schedule is changed later, asserted rather
    than only documented: version 1 stays finalized and untouched, version 2 is
    created from the current configuration, and finalizing it is what moves
    authority -- by the query, not by editing version 1.
    """
    from app.services.schedule_version import create_successor_schedule_version

    world.generate(api)
    world.submit(api)
    world.finalize(api)
    serving, _ = _assigned_person(world)
    version_1_finalized_at = world.session.execute(
        select(ScheduleVersion.finalized_at).where(ScheduleVersion.id == world.version_id)
    ).scalar_one()

    successor = create_successor_schedule_version(
        db_session,
        actor=world.get(Person, world.head_id),
        source_version=world.get(ScheduleVersion, world.version_id),
        amendment_reason="Somebody dropped out",
    )
    db_session.commit()

    assert successor.version_number == 2
    assert successor.status == SCHEDULE_VERSION_STATUS_DRAFT
    assert successor.amends_version_id == world.version_id

    # Version 1's own row is untouched, and still the authoritative one --
    # a DRAFT successor supersedes nothing.
    assert world.status() == SCHEDULE_VERSION_STATUS_FINALIZED
    assert (
        world.session.execute(
            select(ScheduleVersion.finalized_at).where(
                ScheduleVersion.id == world.version_id
            )
        ).scalar_one()
        == version_1_finalized_at
    )
    assert len(world.my_schedule(serving)) == 1
