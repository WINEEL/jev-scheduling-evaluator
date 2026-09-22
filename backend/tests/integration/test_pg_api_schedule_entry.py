"""Entering the scheduling flow against real PostgreSQL (Task 40).

Two endpoints, and then the thing this whole task exists for: **test F**, which
walks the project stakeholder's simplified first-pass flow end to end over real HTTP against a real
database — list the periods, start the first schedule, generate it, review it —
with nothing mocked. No successor version and no carry-forward is involved at
any point, and the test asserts that.

Task 37's harness is reused unchanged: the real ``get_session`` runs on the
test's Session, the outer transaction is rolled back, and
``no_committed_rows_leak`` proves from its own connection that nothing escaped.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.models.scheduling_input import AVAILABILITY_AVAILABLE, SchedulingPeriod
from app.services.audit import (
    ACTION_SCHEDULE_CREATED,
    ACTION_SCHEDULE_VERSION_CREATED,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
LOCKED_AT = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"

_DOMAIN_TABLES = (
    "schedule",
    "schedule_version",
    "schedule_version_requirement",
    "assignment",
    "audit_event",
    "scheduling_period",
    "person",
)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_committed_rows_leak(integration_engine):
    """Prove, from outside the test transaction, that nothing was committed."""

    def counts() -> dict[str, int]:
        with integration_engine.connect() as probe:
            return {
                table: probe.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
                ).scalar_one()
                for table in _DOMAIN_TABLES
            }

    before = counts()
    yield
    assert counts() == before, "a test committed rows that outlived it"


@pytest.fixture
def api(db_session, monkeypatch) -> TestClient:
    """The real ``get_session`` running on the test's Session."""
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(deps, "SessionLocal", lambda: db_session)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _enable_dev_auth(monkeypatch) -> None:
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()


class _Period:
    """A created period's ids, plus the ORM object while it is still usable.

    ``period`` is valid only until the first HTTP request of a test, whose
    Session ``close()`` detaches it; the ids and name stay valid throughout.
    """

    def __init__(self, *, id: int, name: str, period) -> None:
        self.id = id
        self.name = name
        self.period = period


class _Ministry:
    """A ministry with a Head, an ordinary member, and roles.

    Everything is committed at the end so it sits behind the savepoint each
    request opens. Plain ids are captured because a request's ``close()``
    detaches every ORM object.
    """

    def __init__(self, session, *, roles: int = 1) -> None:
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.roles = [
            f.make_role(session, ministry=self.ministry, name=f"Position{i}")
            for i in range(roles)
        ]
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        # A church-wide Admin who heads nothing. Since Task 80 they may read
        # this ministry and start none of its schedules, which is what the
        # two oversight tests below check.
        admin = f.make_person(
            session, church=self.church, name="Admin", is_admin=True
        )
        member = f.make_person(session, church=self.church, name="Member")
        f.make_membership(session, person=member, ministry=self.ministry)
        session.flush()

        self.admin_id = admin.id
        self.head_id = self.head.id
        self.member_id = member.id
        self.ministry_id = self.ministry.id
        self.ministry_name = self.ministry.name
        self.role_ids = [role.id for role in self.roles]
        session.commit()

    def add_period(
        self,
        *,
        name: str,
        start: datetime.date,
        locked: bool = True,
        sundays: int = 0,
        required_count: int = 1,
        volunteers: int = 0,
    ) -> "_Period":
        """A period, optionally locked, optionally with staffing and people.

        Returns plain ids and its stored name: the factory uniquifies names,
        and a request's ``close()`` detaches every ORM object anyway.
        """
        period = f.make_period(
            self.session, ministry=self.ministry, name=name,
            start=start, end=start + datetime.timedelta(days=60),
            availability_locked=locked,
        )
        events = []
        for index in range(sundays):
            event = f.make_event(
                self.session, period=period,
                event_date=start + datetime.timedelta(days=7 * index),
            )
            event.name = f"Sunday {index + 1}"
            events.append(event)
            for role in self.roles:
                f.make_staffing_requirement(
                    self.session, event=event, role=role,
                    required_count=required_count,
                )
        memberships = []
        for index in range(volunteers):
            person = f.make_person(
                self.session, church=self.church, name=f"Volunteer{index}"
            )
            membership = f.make_membership(
                self.session, person=person, ministry=self.ministry
            )
            for role in self.roles:
                f.make_qualification(
                    self.session, membership=membership, role=role,
                    decided_by=self.head, is_qualified=True,
                )
            for event in events:
                f.make_availability(
                    self.session, membership=membership, event=event,
                    state=AVAILABILITY_AVAILABLE,
                )
            memberships.append(membership)
        self.session.flush()
        record = _Period(id=period.id, name=period.name, period=period)
        self.session.commit()
        return record


def _get_periods(api, *, ministry_id: int, actor_id: int):
    return api.get(
        f"/api/v1/ministries/{ministry_id}/scheduling-periods",
        headers={HEADER: str(actor_id)},
    )


def _start(api, *, period_id: int, actor_id: int, body=None):
    return api.post(
        f"/api/v1/scheduling-periods/{period_id}/schedule-versions",
        headers={HEADER: str(actor_id)},
        json={} if body is None else body,
    )


def _fresh(db_session) -> Session:
    """A Session that took no part in the request."""
    return Session(
        bind=db_session.get_bind(),
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )


def _count(session, table: str, where: str, params: dict) -> int:
    return session.execute(
        text(f"SELECT count(*) FROM {table} WHERE {where}"),  # noqa: S608
        params,
    ).scalar_one()


# ==========================================================================
# A -- listing real periods
# ==========================================================================


def test_a_the_list_reports_each_periods_real_scheduling_state(
    api, db_session, monkeypatch
):
    """Three periods: one never scheduled, one scheduled with several
    versions, one still unlocked. Deterministic order, latest version only.
    """
    setup = _Ministry(db_session)
    # Created out of chronological order on purpose.
    later = setup.add_period(name="Q1 2027", start=datetime.date(2027, 1, 3))
    unscheduled = setup.add_period(
        name="Q4 2026", start=datetime.date(2026, 10, 4)
    )
    unlocked = setup.add_period(
        name="Q2 2027", start=datetime.date(2027, 4, 4), locked=False
    )

    # Give the January period a real schedule with three versions. They are
    # created in ascending order, so test_13's SQL assertion -- not this
    # fixture -- is what proves "latest" means highest version_number.
    schedule = f.make_schedule(db_session, period=later.period)
    for number, status_value in enumerate(
        (
            SCHEDULE_VERSION_STATUS_DRAFT,
            SCHEDULE_VERSION_STATUS_REVIEW,
            SCHEDULE_VERSION_STATUS_FINALIZED,
        ),
        start=1,
    ):
        f.make_version(
            db_session,
            schedule=schedule,
            period=later.period,
            version_number=number,
            status=status_value,
            finalized_at=(
                LOCKED_AT if status_value == SCHEDULE_VERSION_STATUS_FINALIZED else None
            ),
        )
    db_session.flush()
    schedule_id = schedule.id
    db_session.commit()
    _enable_dev_auth(monkeypatch)

    response = _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=setup.head_id
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ministry_id"] == setup.ministry_id
    assert body["ministry_name"] == setup.ministry_name

    # Deterministic order by start_date, not creation order.
    assert [p["scheduling_period_id"] for p in body["periods"]] == [
        unscheduled.id,
        later.id,
        unlocked.id,
    ]

    by_id = {p["scheduling_period_id"]: p for p in body["periods"]}

    # Never scheduled -- and still listed, which is the whole point.
    assert by_id[unscheduled.id]["schedule"] is None
    assert by_id[unscheduled.id]["availability_locked_at"] is not None

    # Still open for availability.
    assert by_id[unlocked.id]["availability_locked_at"] is None
    assert by_id[unlocked.id]["schedule"] is None

    # Scheduled: the newest version only, and it is version 3.
    scheduled = by_id[later.id]["schedule"]
    assert scheduled["schedule_id"] == schedule_id
    assert scheduled["latest_version_number"] == 3
    assert scheduled["latest_version_status"] == SCHEDULE_VERSION_STATUS_FINALIZED
    assert set(scheduled) == {
        "schedule_id",
        "latest_version_id",
        "latest_version_number",
        "latest_version_status",
    }


def test_a2_an_admin_sees_the_same_list(api, db_session, monkeypatch):
    """Oversight, and Task 80 did not touch it: reading a ministry is not
    operating it."""
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=datetime.date(2026, 10, 4))
    _enable_dev_auth(monkeypatch)

    body = _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=setup.admin_id
    ).json()

    assert [p["scheduling_period_id"] for p in body["periods"]] == [period.id]


def test_a3_a_ministry_with_no_periods_returns_an_empty_list(
    api, db_session, monkeypatch
):
    setup = _Ministry(db_session)
    _enable_dev_auth(monkeypatch)

    body = _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=setup.head_id
    ).json()

    assert body["periods"] == []


def test_a4_a_missing_ministry_is_404(api, db_session, monkeypatch):
    setup = _Ministry(db_session)
    _enable_dev_auth(monkeypatch)

    response = _get_periods(
        api, ministry_id=setup.ministry_id + 10_000_000, actor_id=setup.admin_id
    )

    assert response.status_code == 404


# ==========================================================================
# B -- starting a real first schedule
# ==========================================================================


def test_b_starting_creates_a_real_schedule_version_and_snapshot(
    api, db_session, monkeypatch
):
    setup = _Ministry(db_session, roles=2)
    period = setup.add_period(
        name="Q4 2026", start=NOV_15, sundays=3, required_count=2
    )
    _enable_dev_auth(monkeypatch)

    response = _start(
        api, period_id=period.id, actor_id=setup.head_id,
        body={"notes": "Initial Q4 schedule"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["scheduling_period_id"] == period.id
    assert body["version_number"] == 1
    assert body["status"] == SCHEDULE_VERSION_STATUS_DRAFT
    # 3 Sundays x 2 roles = 6 snapshot rows (required_count is per row).
    assert body["requirement_snapshot_count"] == 6

    # The rows are really there, seen by a Session that took no part.
    fresh = _fresh(db_session)
    try:
        assert _count(
            fresh, "schedule", "id = :id", {"id": body["schedule_id"]}
        ) == 1
        version_rows = fresh.execute(
            text(
                "SELECT version_number, status, notes, scheduling_period_id,"
                "       schedule_id, amends_version_id"
                " FROM schedule_version WHERE id = :id"
            ),
            {"id": body["schedule_version_id"]},
        ).one()
        assert version_rows.version_number == 1
        assert version_rows.status == SCHEDULE_VERSION_STATUS_DRAFT
        assert version_rows.notes == "Initial Q4 schedule"
        assert version_rows.scheduling_period_id == period.id
        assert version_rows.schedule_id == body["schedule_id"]
        # Not a successor: nothing was amended.
        assert version_rows.amends_version_id is None

        assert _count(
            fresh,
            "schedule_version_requirement",
            "schedule_version_id = :v",
            {"v": body["schedule_version_id"]},
        ) == 6
        # Starting a schedule fills nothing.
        assert _count(
            fresh, "assignment", "schedule_version_id = :v",
            {"v": body["schedule_version_id"]},
        ) == 0

        # Task 20's audit rows committed with the request, both of them.
        assert _count(
            fresh, "audit_event", "action = :a AND target_id = :id",
            {"a": ACTION_SCHEDULE_CREATED, "id": body["schedule_id"]},
        ) == 1
        assert _count(
            fresh, "audit_event", "action = :a AND target_id = :id",
            {"a": ACTION_SCHEDULE_VERSION_CREATED,
             "id": body["schedule_version_id"]},
        ) == 1
    finally:
        fresh.close()


def test_b2_the_head_may_start_one_and_a_note_is_optional(
    api, db_session, monkeypatch
):
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=NOV_15, sundays=1)
    _enable_dev_auth(monkeypatch)

    response = _start(api, period_id=period.id, actor_id=setup.head_id)

    assert response.status_code == 201
    assert response.json()["requirement_snapshot_count"] == 1
    fresh = _fresh(db_session)
    try:
        assert fresh.execute(
            text("SELECT notes FROM schedule_version WHERE id = :id"),
            {"id": response.json()["schedule_version_id"]},
        ).scalar_one() is None
    finally:
        fresh.close()


def test_b2b_an_admin_who_heads_nothing_may_not_start_one(
    api, db_session, monkeypatch
):
    """**Task 80.** Starting a schedule creates this ministry's first version
    -- an operational write, and one the Admin who may read every ministry in
    the church does not get."""
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=NOV_15, sundays=1)
    _enable_dev_auth(monkeypatch)

    response = _start(api, period_id=period.id, actor_id=setup.admin_id)

    assert response.status_code == 403
    fresh = _fresh(db_session)
    try:
        assert fresh.execute(
            text("SELECT count(*) FROM schedule_version")
        ).scalar_one() == 0
    finally:
        fresh.close()


def test_b3_a_missing_period_is_404(api, db_session, monkeypatch):
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=NOV_15)
    _enable_dev_auth(monkeypatch)

    response = _start(
        api, period_id=period.id + 10_000_000, actor_id=setup.head_id
    )

    assert response.status_code == 404


# ==========================================================================
# C -- availability still open
# ==========================================================================


def test_c_an_unlocked_period_is_409_and_writes_nothing(api, db_session, monkeypatch):
    setup = _Ministry(db_session)
    period = setup.add_period(
        name="Q4 2026", start=NOV_15, sundays=2, locked=False
    )
    _enable_dev_auth(monkeypatch)

    response = _start(api, period_id=period.id, actor_id=setup.head_id)

    assert response.status_code == 409
    assert "availability is still open" in response.json()["detail"]

    fresh = _fresh(db_session)
    try:
        assert _count(
            fresh, "schedule", "scheduling_period_id = :p", {"p": period.id}
        ) == 0
        assert _count(
            fresh, "schedule_version", "scheduling_period_id = :p", {"p": period.id}
        ) == 0
        assert _count(
            fresh, "audit_event", "action = :a", {"a": ACTION_SCHEDULE_CREATED}
        ) == 0
    finally:
        fresh.close()


# ==========================================================================
# D -- a second attempt
# ==========================================================================


def test_d_a_second_start_is_409_and_creates_no_second_version(
    api, db_session, monkeypatch
):
    """The first request really committed; the second is refused rather than
    quietly creating a successor.
    """
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=NOV_15, sundays=2)
    _enable_dev_auth(monkeypatch)

    first = _start(api, period_id=period.id, actor_id=setup.head_id)
    assert first.status_code == 201
    first_version_id = first.json()["schedule_version_id"]

    second = _start(api, period_id=period.id, actor_id=setup.head_id)

    assert second.status_code == 409
    assert "already has a version" in second.json()["detail"]

    fresh = _fresh(db_session)
    try:
        rows = fresh.execute(
            text(
                "SELECT id, version_number FROM schedule_version"
                " WHERE scheduling_period_id = :p ORDER BY id"
            ),
            {"p": period.id},
        ).all()
        assert [row.id for row in rows] == [first_version_id]
        assert [row.version_number for row in rows] == [1]
        # And exactly one Schedule, reused rather than duplicated.
        assert _count(
            fresh, "schedule", "scheduling_period_id = :p", {"p": period.id}
        ) == 1
    finally:
        fresh.close()


# ==========================================================================
# E -- authorization against real membership rows
# ==========================================================================


def test_e_an_ordinary_member_cannot_list_or_start(api, db_session, monkeypatch):
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=NOV_15, sundays=1)
    _enable_dev_auth(monkeypatch)

    listing = _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=setup.member_id
    )
    assert listing.status_code == 403
    assert "Q4 2026" not in listing.text

    creation = _start(api, period_id=period.id, actor_id=setup.member_id)
    assert creation.status_code == 403

    fresh = _fresh(db_session)
    try:
        assert _count(
            fresh, "schedule", "scheduling_period_id = :p", {"p": period.id}
        ) == 0
        assert _count(
            fresh, "schedule_version", "scheduling_period_id = :p", {"p": period.id}
        ) == 0
    finally:
        fresh.close()


def test_e2_a_head_of_a_different_ministry_is_refused(api, db_session, monkeypatch):
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=NOV_15, sundays=1)
    other = f.make_ministry(db_session, church=setup.church, name="AV")
    outsider = f.make_person(db_session, church=setup.church, name="AV Head")
    f.make_membership(db_session, person=outsider, ministry=other, is_head=True)
    db_session.flush()
    outsider_id = outsider.id
    db_session.commit()
    _enable_dev_auth(monkeypatch)

    assert _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=outsider_id
    ).status_code == 403
    assert _start(
        api, period_id=period.id, actor_id=outsider_id
    ).status_code == 403


def test_e3_unauthenticated_requests_are_401(api, db_session):
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=NOV_15)

    assert _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=setup.head_id
    ).status_code == 401
    assert _start(
        api, period_id=period.id, actor_id=setup.head_id
    ).status_code == 401


# ==========================================================================
# F -- the whole first-pass flow, over real HTTP
# ==========================================================================


def test_f_the_first_pass_flow_end_to_end(api, db_session, monkeypatch):
    """**The key simplified-flow test.**

    Four real HTTP calls, nothing mocked: Tasks 20, 34 and 38 all run for
    real against PostgreSQL.

        list periods → start first schedule → generate → review

    The same version id flows through all four, assignments are really
    persisted, the review endpoint shows them — and no successor version or
    carry-forward is involved anywhere.
    """
    setup = _Ministry(db_session, roles=1)
    period = setup.add_period(
        name="Q4 2026", start=NOV_15, sundays=2, required_count=1, volunteers=2
    )
    _enable_dev_auth(monkeypatch)

    # 1 -- which periods can I schedule?
    listing = _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=setup.head_id
    )
    assert listing.status_code == 200
    listed = next(
        p
        for p in listing.json()["periods"]
        if p["scheduling_period_id"] == period.id
    )
    assert listed["availability_locked_at"] is not None, "ready to schedule"
    assert listed["schedule"] is None, "not started yet"

    # 2 -- start the first schedule.
    started = _start(api, period_id=period.id, actor_id=setup.head_id)
    assert started.status_code == 201
    version_id = started.json()["schedule_version_id"]
    assert started.json()["requirement_snapshot_count"] == 2
    assert started.json()["status"] == SCHEDULE_VERSION_STATUS_DRAFT

    # The listing now reports it as started, pointing at the same version.
    relisted = _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=setup.head_id
    ).json()
    schedule = next(
        p
        for p in relisted["periods"]
        if p["scheduling_period_id"] == period.id
    )["schedule"]
    assert schedule["latest_version_id"] == version_id
    assert schedule["latest_version_number"] == 1
    assert schedule["latest_version_status"] == SCHEDULE_VERSION_STATUS_DRAFT

    # 3 -- generate (Task 37/34, unmocked).
    generated = api.post(
        f"/api/v1/schedule-versions/{version_id}/generate",
        headers={HEADER: str(setup.head_id)},
        json={},
    )
    assert generated.status_code == 200
    assert generated.json()["schedule_version_id"] == version_id
    assert generated.json()["created_count"] == 2
    assert generated.json()["is_complete"] is True
    created_ids = {
        a["assignment_id"] for a in generated.json()["created_assignments"]
    }

    # 4 -- review it (Task 38, unmocked).
    review = api.get(
        f"/api/v1/schedule-versions/{version_id}",
        headers={HEADER: str(setup.head_id)},
    )
    assert review.status_code == 200
    detail = review.json()
    assert detail["schedule_version"]["id"] == version_id
    assert detail["schedule_version"]["version_number"] == 1
    assert detail["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_DRAFT
    assert detail["summary"] == {
        "required_positions": 2,
        "assigned_positions": 2,
        "unfilled_positions": 0,
        "is_fully_staffed": True,
    }
    assert {a["assignment_id"] for a in detail["assignments"]} == created_ids
    for assignment in detail["assignments"]:
        assert assignment["person_display_name"]
        assert assignment["is_override"] is False
    assert detail["staleness"]["is_stale"] is False
    assert detail["finalization_readiness"]["is_ready"] is True

    # The assignments really are in the database.
    fresh = _fresh(db_session)
    try:
        assert _count(
            fresh, "assignment", "schedule_version_id = :v", {"v": version_id}
        ) == 2
        # **One version only.** No successor was created anywhere in the flow.
        version_numbers = fresh.execute(
            text(
                "SELECT version_number, amends_version_id FROM schedule_version"
                " WHERE scheduling_period_id = :p ORDER BY version_number"
            ),
            {"p": period.id},
        ).all()
        assert [row.version_number for row in version_numbers] == [1]
        assert [row.amends_version_id for row in version_numbers] == [None]
    finally:
        fresh.close()

    # No successor or carry-forward operation was involved at any step. The
    # review DTO does carry ``amends_version_id`` / ``amendment_reason`` as
    # fields -- that is Task 38's schema, and what matters is that they are
    # null, because this version amends nothing.
    assert detail["schedule_version"]["amends_version_id"] is None
    assert detail["schedule_version"]["amendment_reason"] is None

    for response in (listing, started, generated, review):
        lowered = response.text.lower()
        for jargon in ("successor", "carry_forward", "carry-forward"):
            assert jargon not in lowered, (jargon, response.url)


def test_f2_generation_still_refuses_a_period_that_was_never_started(
    api, db_session, monkeypatch
):
    """The flow's order is real, not decorative: there is nothing to generate
    into until a schedule has been started.
    """
    setup = _Ministry(db_session)
    period = setup.add_period(name="Q4 2026", start=NOV_15, sundays=1)
    _enable_dev_auth(monkeypatch)

    listing = _get_periods(
        api, ministry_id=setup.ministry_id, actor_id=setup.head_id
    ).json()
    listed = next(
        p for p in listing["periods"] if p["scheduling_period_id"] == period.id
    )
    assert listed["schedule"] is None

    # There is no version id to generate into; the id space is simply empty.
    response = api.post(
        "/api/v1/schedule-versions/999999999/generate",
        headers={HEADER: str(setup.head_id)},
        json={},
    )
    assert response.status_code == 404
