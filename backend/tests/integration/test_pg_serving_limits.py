"""Serving-limit management over HTTP against real PostgreSQL rows (Task 57).

The offline suites prove the control flow with the reads stubbed
(``tests/test_api_serving_limits.py``, ``tests/test_services_serving_limit.py``)
and the enforcement with a fake candidate state
(``tests/test_scheduling_serving_limit.py``,
``tests/test_services_finalization_readiness.py``). What only a real database
proves is that a maximum written *through the new API* is the same row the
manual-assignment check and the finalization gate then read back.

Nothing is mocked here beyond the request Session, which is the test's own
rolled-back one. Rollback-isolated by the shared harness; nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text

from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.models.schedule_output import SCHEDULE_VERSION_STATUS_DRAFT
from app.services.assignment import assign_member
from app.services.errors import InvalidOperationError
from app.services.finalization_readiness import (
    ISSUE_EXCEEDS_SERVING_LIMIT,
    get_finalization_readiness,
)
from fastapi.testclient import TestClient
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
NOV_8 = datetime.date(2026, 11, 8)
NOV_15 = datetime.date(2026, 11, 15)


@pytest.fixture
def api(db_session, monkeypatch) -> TestClient:
    """A client whose requests run on the test's rolled-back Session, and --
    like the real :func:`app.api.dependencies.get_session` -- commit that
    Session when the handler returns. Under the harness's
    ``join_transaction_mode="create_savepoint"`` that commit only releases a
    SAVEPOINT, so the outer transaction still discards everything; what it
    buys is realistic cross-request visibility (the app's Session has
    ``autoflush=False``).
    """
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()

    def _session_with_commit():
        # Commit on success only. A failing request cannot truly roll back
        # here without discarding the test's own fixture rows (the whole test
        # runs in one outer transaction), and every error path exercised
        # below raises before mutating anything, so there is nothing pending
        # to undo.
        yield db_session
        db_session.commit()

    app.dependency_overrides[deps.get_session] = _session_with_commit
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


class _Scenario:
    """One ministry, its head, a volunteer, a period with two Sundays each
    requiring one person, and a DRAFT version snapshotting them.
    """

    def __init__(self, session):
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="AV")
        self.head_person = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        # A church-wide Admin who heads nothing -- the actor two of the tests
        # below use to show that oversight does not reach a serving limit.
        self.admin = f.make_person(
            session, church=self.church, name="Admin", is_admin=True
        )
        self.person = f.make_person(session, church=self.church, name="Volunteer")
        self.membership = f.make_membership(
            session, person=self.person, ministry=self.ministry,
        )
        self.role = f.make_role(session, ministry=self.ministry, name="Sound")
        f.make_qualification(
            session, membership=self.membership, role=self.role,
            decided_by=self.head_person, is_qualified=True,
        )
        self.period = f.make_period(session, ministry=self.ministry)
        self.event_a = f.make_event(session, period=self.period, event_date=NOV_8)
        self.event_b = f.make_event(session, period=self.period, event_date=NOV_15)
        for event in (self.event_a, self.event_b):
            f.make_staffing_requirement(
                session, event=event, role=self.role, required_count=1,
            )
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.req_a = f.make_version_requirement(
            session, version=self.version, event=self.event_a, role=self.role,
            required_count=1,
        )
        self.req_b = f.make_version_requirement(
            session, version=self.version, event=self.event_b, role=self.role,
            required_count=1,
        )
        session.flush()

    def _headers(self, person):
        return {HEADER: str(person.id)}


def _limit_row(session, *, membership_id, period_id):
    return session.execute(
        text(
            "SELECT max_assignments FROM membership_serving_limit"
            " WHERE ministry_membership_id = :m AND scheduling_period_id = :p"
        ),
        {"m": membership_id, "p": period_id},
    ).scalar_one_or_none()


def _audit_actions(session, *, target_id):
    return session.execute(
        text(
            "SELECT action FROM audit_event"
            " WHERE target_table = 'membership_serving_limit'"
            " AND target_id = :t ORDER BY id"
        ),
        {"t": target_id},
    ).scalars().all()


def test_set_list_and_clear_round_trip_through_the_api(api, db_session):
    s = _Scenario(db_session)
    base = f"/api/v1/scheduling-periods/{s.period.id}/serving-limits"

    # No limit yet.
    listed = api.get(base, headers=s._headers(s.head_person))
    assert listed.status_code == 200
    body = listed.json()
    entry = next(m for m in body["memberships"] if m["ministry_membership_id"] == s.membership.id)
    assert entry["max_assignments"] is None

    # Set one.
    put = api.put(
        f"{base}/{s.membership.id}", headers=s._headers(s.head_person),
        json={"max_assignments": 3},
    )
    assert put.status_code == 200
    assert put.json()["max_assignments"] == 3
    assert _limit_row(db_session, membership_id=s.membership.id, period_id=s.period.id) == 3

    # Changed, listed.
    api.put(
        f"{base}/{s.membership.id}", headers=s._headers(s.head_person),
        json={"max_assignments": 2},
    )
    relisted = api.get(base, headers=s._headers(s.head_person)).json()
    entry = next(m for m in relisted["memberships"] if m["ministry_membership_id"] == s.membership.id)
    assert entry["max_assignments"] == 2

    # Cleared.
    deleted = api.delete(f"{base}/{s.membership.id}", headers=s._headers(s.head_person))
    assert deleted.status_code == 204
    assert _limit_row(db_session, membership_id=s.membership.id, period_id=s.period.id) is None

    # Every change audited (record, change, clear) -- and no reason column.
    row_id = db_session.execute(
        text(
            "SELECT target_id FROM audit_event"
            " WHERE target_table = 'membership_serving_limit' LIMIT 1"
        )
    ).scalar_one()
    assert _audit_actions(db_session, target_id=row_id) == [
        "SERVING_LIMIT_RECORDED",
        "SERVING_LIMIT_CHANGED",
        "SERVING_LIMIT_CLEARED",
    ]


def test_an_admin_who_heads_nothing_may_read_but_not_write(api, db_session):
    """**Task 80's split, at a real endpoint.** A serving limit is how often
    somebody serves in one ministry for one period -- the ministry head's
    decision. The Elder overseeing the church sees the whole list and cannot
    change a number on it.
    """
    s = _Scenario(db_session)
    base = f"/api/v1/scheduling-periods/{s.period.id}/serving-limits"

    listed = api.get(base, headers=s._headers(s.admin))

    assert listed.status_code == 200
    assert listed.json()["can_operate"] is False
    assert any(
        m["ministry_membership_id"] == s.membership.id
        for m in listed.json()["memberships"]
    )

    refused = api.put(
        f"{base}/{s.membership.id}", headers=s._headers(s.admin),
        json={"max_assignments": 3},
    )

    assert refused.status_code == 403
    assert _limit_row(
        db_session, membership_id=s.membership.id, period_id=s.period.id
    ) is None


def test_the_head_sees_can_operate_on_the_same_list(api, db_session):
    s = _Scenario(db_session)
    base = f"/api/v1/scheduling-periods/{s.period.id}/serving-limits"

    assert api.get(base, headers=s._headers(s.head_person)).json()["can_operate"] is True


def test_a_normal_member_is_forbidden(api, db_session):
    s = _Scenario(db_session)
    base = f"/api/v1/scheduling-periods/{s.period.id}/serving-limits"

    assert api.get(base, headers=s._headers(s.person)).status_code == 403
    assert api.put(
        f"{base}/{s.membership.id}", headers=s._headers(s.person),
        json={"max_assignments": 3},
    ).status_code == 403


def test_a_head_of_another_ministry_is_forbidden(api, db_session):
    s = _Scenario(db_session)
    other_ministry = f.make_ministry(db_session, church=s.church, name="Setup")
    other_head_person = f.make_person(db_session, church=s.church, name="Setup Head")
    f.make_membership(
        db_session, person=other_head_person, ministry=other_ministry, is_head=True,
    )
    db_session.flush()
    base = f"/api/v1/scheduling-periods/{s.period.id}/serving-limits"

    assert api.get(base, headers=s._headers(other_head_person)).status_code == 403


def test_cross_ministry_membership_and_period_is_409(api, db_session):
    s = _Scenario(db_session)
    other_ministry = f.make_ministry(db_session, church=s.church, name="Setup")
    other_person = f.make_person(db_session, church=s.church, name="Setup Volunteer")
    other_membership = f.make_membership(
        db_session, person=other_person, ministry=other_ministry,
    )
    db_session.flush()

    response = api.put(
        f"/api/v1/scheduling-periods/{s.period.id}/serving-limits/{other_membership.id}",
        headers=s._headers(s.head_person), json={"max_assignments": 2},
    )
    assert response.status_code == 409


def test_an_api_set_maximum_blocks_a_manual_assignment_over_it(api, db_session):
    s = _Scenario(db_session)

    api.put(
        f"/api/v1/scheduling-periods/{s.period.id}/serving-limits/{s.membership.id}",
        headers=s._headers(s.head_person), json={"max_assignments": 1},
    )

    assign_member(
        db_session, actor=s.head_person, requirement=s.req_a, membership=s.membership,
    )
    db_session.flush()

    with pytest.raises(InvalidOperationError) as exc:
        assign_member(
            db_session, actor=s.head_person, requirement=s.req_b, membership=s.membership,
        )
    assert "serving maximum" in str(exc.value)
    # And the limit is not overridable: an override_reason does not lift it.
    with pytest.raises(InvalidOperationError):
        assign_member(
            db_session, actor=s.head_person, requirement=s.req_b, membership=s.membership,
            override_reason="We really need them.",
        )


def test_lowering_an_api_maximum_below_existing_assignments_is_caught_by_readiness(
    api, db_session
):
    s = _Scenario(db_session)

    assign_member(db_session, actor=s.head_person, requirement=s.req_a, membership=s.membership)
    assign_member(db_session, actor=s.head_person, requirement=s.req_b, membership=s.membership)
    db_session.flush()

    # Nothing wrong yet.
    before = get_finalization_readiness(db_session, version=s.version)
    assert all(i.code != ISSUE_EXCEEDS_SERVING_LIMIT for i in before.issues)

    # Head agrees a lower number after the draft exists.
    api.put(
        f"/api/v1/scheduling-periods/{s.period.id}/serving-limits/{s.membership.id}",
        headers=s._headers(s.head_person), json={"max_assignments": 1},
    )
    db_session.flush()

    after = get_finalization_readiness(db_session, version=s.version)
    assert after.is_ready is False
    assert any(i.code == ISSUE_EXCEEDS_SERVING_LIMIT for i in after.issues)

    # Clearing it again restores readiness for this rule.
    api.delete(
        f"/api/v1/scheduling-periods/{s.period.id}/serving-limits/{s.membership.id}",
        headers=s._headers(s.head_person),
    )
    db_session.flush()
    restored = get_finalization_readiness(db_session, version=s.version)
    assert all(i.code != ISSUE_EXCEEDS_SERVING_LIMIT for i in restored.issues)
