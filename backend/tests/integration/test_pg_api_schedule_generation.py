"""The mutating HTTP boundary against real PostgreSQL rows (Task 37).

The offline suite proves the boundary's logic with a recording stand-in. What
needs a real database is the part a stand-in cannot fake: that a request which
succeeds leaves real Assignment and AuditEvent rows behind, and that a request
which fails part way through leaves **nothing** -- not even the placements it
had already accepted before the failure.

**How a real request commit is contained.** The harness gives each test a
Connection with an outer transaction that is always rolled back, and a Session
joined to it with ``join_transaction_mode="create_savepoint"``. A ``commit()``
from the request therefore releases a SAVEPOINT rather than ending the real
transaction: the request's work becomes visible to everything sharing that
connection -- which is what these tests check -- while the outer rollback still
discards it, and a separate connection never sees it at all. The production
boundary is not weakened for this; ``get_session`` runs exactly as shipped, and
only the *factory* it calls is redirected at the test's Session.

The honest limit of that: a released savepoint is not a durable commit, so
these tests prove the boundary's **semantics** -- what commits, what rolls
back, and what is still there afterwards -- not PostgreSQL's durability, which
is not this project's to test.

``no_committed_rows_leak`` re-checks after every test, on its own connection,
that nothing escaped.
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
from app.models.schedule_output import SCHEDULE_VERSION_STATUS_DRAFT
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_UNAVAILABLE,
)
from app.services.audit import ACTION_ASSIGNMENT_ADDED
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"

#: Counted before and after every test, on a connection of their own.
_LEAK_TABLES = ("assignment", "audit_event", "church", "person", "schedule_version")


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_committed_rows_leak(integration_engine):
    """Prove, from outside the test's transaction, that nothing was committed.

    Autouse and depending only on the engine, so it is set up before
    ``db_session`` and therefore torn down *after* it -- by the time this
    checks, the outer rollback has already run. Its own connection cannot see
    uncommitted rows, so an unchanged count is a real statement about the
    database rather than about this test's view of it.
    """

    def counts() -> dict[str, int]:
        with integration_engine.connect() as probe:
            return {
                table: probe.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
                ).scalar_one()
                for table in _LEAK_TABLES
            }

    before = counts()
    yield
    assert counts() == before, "a test committed rows that outlived it"


@pytest.fixture
def api(db_session, monkeypatch) -> TestClient:
    """A client whose requests run the **real** ``get_session`` on the test's
    Session.

    Only ``SessionLocal`` is redirected. The commit, the rollback and the close
    are the shipped ones, so what these tests observe is the production
    boundary rather than a re-implementation of it.
    """
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


class _Scenario:
    """A latest DRAFT version with ``sundays`` Sundays of one role, and a
    roster of interchangeable volunteers.

    ``available=False`` leaves the volunteers with no Availability row at all,
    which is the "no response" state -- the third state, and the one the
    ``allow_no_response`` policy turns on.

    ``one_each=True`` makes volunteer *i* available for Sunday *i* and
    explicitly unavailable for the rest, so each requirement has exactly one
    possible person and the run must name **distinct** memberships. Test E
    depends on that: with everyone available for everything the solver quite
    reasonably gives both Sundays to one person, and a test that then revokes
    "the second proposal's" qualification would break the *first* write too and
    prove nothing.
    """

    def __init__(
        self,
        session,
        *,
        sundays: int = 2,
        volunteers: int = 2,
        required_count: int = 1,
        available: bool = True,
        one_each: bool = False,
    ) -> None:
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.role = f.make_role(session, ministry=self.ministry, name="Position")
        self.period = f.make_period(session, ministry=self.ministry)
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session,
            schedule=self.schedule,
            period=self.period,
            status=SCHEDULE_VERSION_STATUS_DRAFT,
        )
        self.events = []
        self.requirements = []
        for index in range(sundays):
            event = f.make_event(
                session,
                period=self.period,
                event_date=NOV_15 + datetime.timedelta(days=7 * index),
            )
            self.events.append(event)
            f.make_staffing_requirement(
                session, event=event, role=self.role, required_count=required_count
            )
            self.requirements.append(
                f.make_version_requirement(
                    session,
                    version=self.version,
                    event=event,
                    role=self.role,
                    required_count=required_count,
                )
            )
        self.memberships = []
        for index in range(volunteers):
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
            if available:
                for position, event in enumerate(self.events):
                    f.make_availability(
                        session,
                        membership=membership,
                        event=event,
                        state=(
                            AVAILABILITY_UNAVAILABLE
                            if one_each and position != index
                            else AVAILABILITY_AVAILABLE
                        ),
                    )
            self.memberships.append(membership)
        session.flush()

        # Commit the fixture so it sits *behind* the savepoint each request
        # opens: a request that rolls back must discard its own work without
        # taking the scenario with it.
        session.commit()
        self.version_id = self.version.id
        self.admin_id = self.head.id
        self.church_id = self.church.id


def _post(api, *, version_id: int, actor_id: int, body=None):
    return api.post(
        f"/api/v1/schedule-versions/{version_id}/generate",
        headers={HEADER: str(actor_id)},
        json={} if body is None else body,
    )


def _fresh(db_session) -> Session:
    """A Session that did not take part in the request.

    Shares the connection -- so it sees the same committed state -- but has its
    own identity map, so what it returns comes from the database rather than
    from objects the request happened to leave in memory.
    """
    return Session(
        bind=db_session.get_bind(),
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )


def _recording_accepted_placements(monkeypatch) -> list[tuple[int, int]]:
    """Record the placements the writer **genuinely accepted**.

    The wrapper calls the real rule-checked row builder, so every rule and
    every audit row is the production one; it only notes which placements got
    past every check, which is what lets a failure test assert the run really
    reached the refusal rather than passing for having done nothing.

    Task 63 note: before the batch writer this recorded *written* rows,
    because each accepted placement was flushed immediately. The writer now
    flushes the whole run at once, so a refused run writes nothing at all --
    which is why "accepted" and "written" are no longer the same thing, and
    why the tests below assert both.
    """
    import app.services.generated_assignment as writer

    accepted: list[tuple[int, int]] = []
    real_build = writer.build_assignment

    def recording(*, requirement, membership, is_override, override_reason):
        accepted.append((requirement.id, membership.id))
        return real_build(
            requirement=requirement, membership=membership,
            is_override=is_override, override_reason=override_reason,
        )

    monkeypatch.setattr(writer, "build_assignment", recording)
    return accepted


def _assignment_rows(session, version_id: int):
    return session.execute(
        text(
            "SELECT id, schedule_version_requirement_id, ministry_membership_id,"
            "       event_id, is_override"
            " FROM assignment WHERE schedule_version_id = :v ORDER BY id"
        ),
        {"v": version_id},
    ).all()


def _added_audit_count(session, version_id: int) -> int:
    return session.execute(
        text(
            "SELECT count(*) FROM audit_event ae"
            " JOIN assignment a ON a.id = ae.target_id"
            " WHERE ae.action = :action AND ae.target_table = 'assignment'"
            "   AND a.schedule_version_id = :v"
        ),
        {"action": ACTION_ASSIGNMENT_ADDED, "v": version_id},
    ).scalar_one()


# ==========================================================================
# A -- a complete generation over HTTP
# ==========================================================================


def test_a_a_successful_request_persists_real_rows(api, db_session, monkeypatch):
    scenario = _Scenario(db_session, sundays=2, volunteers=2)
    _enable_dev_auth(monkeypatch)

    response = _post(api, version_id=scenario.version_id, actor_id=scenario.admin_id)

    assert response.status_code == 200
    body = response.json()
    assert body["schedule_version_id"] == scenario.version_id
    assert body["is_complete"] is True
    assert body["created_count"] == 2
    assert body["unfilled_requirements"] == []

    # The rows the response describes are really there, seen by a Session that
    # took no part in the request.
    fresh = _fresh(db_session)
    try:
        rows = _assignment_rows(fresh, scenario.version_id)
        audits = _added_audit_count(fresh, scenario.version_id)
    finally:
        fresh.close()

    assert len(rows) == 2
    assert {row.id for row in rows} == {
        created["assignment_id"] for created in body["created_assignments"]
    }
    # Every id in the response is a real row's real column.
    by_id = {row.id: row for row in rows}
    for created in body["created_assignments"]:
        row = by_id[created["assignment_id"]]
        assert created["requirement_id"] == row.schedule_version_requirement_id
        assert created["membership_id"] == row.ministry_membership_id
        assert created["event_id"] == row.event_id
    # Automatic scheduling never overrides, and Task 22 wrote one audit row per
    # assignment -- the endpoint added none of its own.
    assert all(row.is_override is False for row in rows)
    assert audits == 2


def test_a2_what_the_request_committed_is_still_invisible_outside_the_test(
    api, db_session, monkeypatch, integration_engine
):
    """The containment claim, checked rather than asserted in a docstring.

    The request genuinely committed, and the row is genuinely there for anyone
    on this connection -- and a separate connection sees nothing, because the
    harness's outer transaction still owns it all.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    _enable_dev_auth(monkeypatch)

    _post(api, version_id=scenario.version_id, actor_id=scenario.admin_id)

    with integration_engine.connect() as outsider:
        visible_elsewhere = outsider.execute(
            text("SELECT count(*) FROM assignment WHERE schedule_version_id = :v"),
            {"v": scenario.version_id},
        ).scalar_one()

    assert visible_elsewhere == 0


# ==========================================================================
# B -- authorization
# ==========================================================================


def test_b_a_volunteer_who_manages_nothing_gets_403_and_writes_nothing(
    api, db_session, monkeypatch
):
    scenario = _Scenario(db_session, sundays=2, volunteers=2)
    outsider = f.make_person(db_session, church=scenario.church, name="Outsider")
    db_session.flush()
    db_session.commit()
    outsider_id = outsider.id
    _enable_dev_auth(monkeypatch)

    response = _post(api, version_id=scenario.version_id, actor_id=outsider_id)

    assert response.status_code == 403
    fresh = _fresh(db_session)
    try:
        assert _assignment_rows(fresh, scenario.version_id) == []
        assert _added_audit_count(fresh, scenario.version_id) == 0
    finally:
        fresh.close()


def test_b2_an_active_ministry_head_is_allowed(api, db_session, monkeypatch):
    """The other side of B: authority really is read from the membership rows,
    not from being an Admin.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    person = f.make_person(db_session, church=scenario.church, name="Head")
    f.make_membership(
        db_session, person=person, ministry=scenario.ministry, is_head=True
    )
    db_session.flush()
    db_session.commit()
    head_id = person.id
    _enable_dev_auth(monkeypatch)

    response = _post(api, version_id=scenario.version_id, actor_id=head_id)

    assert response.status_code == 200
    assert response.json()["created_count"] == 1


# ==========================================================================
# C -- a version that does not exist
# ==========================================================================


def test_c_an_unknown_version_id_is_404(api, db_session, monkeypatch):
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    _enable_dev_auth(monkeypatch)

    missing_id = scenario.version_id + 10_000_000
    response = _post(api, version_id=missing_id, actor_id=scenario.admin_id)

    assert response.status_code == 404
    assert response.json() == {"detail": "Schedule version not found."}


def test_c2_an_unauthenticated_request_for_a_real_version_is_401(api, db_session):
    """Dev auth is off here. Identity is refused before the version is even
    looked up, so a missing-versus-existing probe learns nothing.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1)

    response = _post(api, version_id=scenario.version_id, actor_id=scenario.admin_id)

    assert response.status_code == 401


# ==========================================================================
# D -- an incomplete generation is a 200
# ==========================================================================


def test_d_an_incomplete_generation_returns_200_and_keeps_what_it_filled(
    api, db_session, monkeypatch
):
    """One Sunday needing two people, with one volunteer available. A person
    cannot fill two positions at the same event, so one is filled and one
    cannot be -- and the request still succeeds, because failing it would
    discard the assignment that was legitimately made.

    (Two Sundays and one volunteer would *not* be short: serving on two
    different Sundays is exactly what the schedule is for.)
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1, required_count=2)
    _enable_dev_auth(monkeypatch)

    response = _post(api, version_id=scenario.version_id, actor_id=scenario.admin_id)

    assert response.status_code == 200
    body = response.json()
    assert body["is_complete"] is False
    assert body["created_count"] == 1
    assert len(body["unfilled_requirements"]) == 1
    unfilled = body["unfilled_requirements"][0]
    assert unfilled["missing_count"] == 1
    assert unfilled["diagnostic_codes"] != []

    fresh = _fresh(db_session)
    try:
        assert len(_assignment_rows(fresh, scenario.version_id)) == 1
    finally:
        fresh.close()


# ==========================================================================
# E -- the request transaction rolls back a real partial failure
# ==========================================================================


def test_e_a_domain_failure_part_way_through_rolls_the_whole_request_back(
    api, db_session, monkeypatch
):
    """**The load-bearing test of Task 37.**

    Two proposals. Between the solver deciding and the second placement being
    judged, that volunteer's qualification is revoked, so the real rules
    refuse it -- after the first placement has already passed every check.

    The monkeypatch only *times* the change. Both placements are judged by the
    production rules, the refusal is genuine, and what is being tested is that
    the HTTP request fails and leaves nothing behind. No compensating delete
    does this.

    **Task 63 made this stronger, and the test says so.** Task 34 flushed each
    accepted row immediately, so this test proved the request transaction took
    a real written row down with it. The batch writer flushes the whole run at
    once, so an accepted-then-refused run never writes anything in the first
    place -- the transaction still ends the same way, and there is now also
    nothing for it to undo. Both are asserted: the run genuinely reached the
    refusal, and nothing survived. The request-boundary rollback itself is
    proven independently in ``test_pg_transactions.py``.
    """
    import app.services.schedule_generation as generation

    scenario = _Scenario(db_session, sundays=2, volunteers=2, one_each=True)
    _enable_dev_auth(monkeypatch)

    accepted = _recording_accepted_placements(monkeypatch)
    real_solve = generation.solve_schedule

    def solve_then_revoke(scheduling_input, *, policy):
        result = real_solve(scheduling_input, policy=policy)
        first, second = result.proposed_assignments
        # The premise of the test, checked rather than hoped for: two
        # different people, so revoking the second's qualification cannot
        # also invalidate the first write.
        assert first.membership_id != second.membership_id
        db_session.execute(
            text(
                "UPDATE role_qualification SET is_qualified = false"
                " WHERE ministry_membership_id = :m"
            ),
            {"m": second.membership_id},
        )
        return result

    monkeypatch.setattr(generation, "solve_schedule", solve_then_revoke)

    response = _post(api, version_id=scenario.version_id, actor_id=scenario.admin_id)

    assert response.status_code == 409
    assert "qualified" in response.json()["detail"]

    # **The run really reached the refusal.** Without this the test could pass
    # by never having got as far as the second placement, which would prove
    # nothing about the failure path at all. The first placement passed every
    # rule; the second did not.
    assert len(accepted) == 1

    # Nothing survived -- not the placement that was accepted, and not an
    # audit row for it. Read from a Session that took no part in the request.
    fresh = _fresh(db_session)
    try:
        rows = _assignment_rows(fresh, scenario.version_id)
        audits = _added_audit_count(fresh, scenario.version_id)
    finally:
        fresh.close()

    assert rows == []
    assert audits == 0


def test_e2_the_scenario_itself_survived_the_rolled_back_request(
    api, db_session, monkeypatch
):
    """The rollback is the request's, not the world's.

    A boundary that rolled back too far -- to the start of the connection's
    transaction rather than to the start of the request -- would pass test E
    for the wrong reason, by destroying the fixture too.
    """
    import app.services.schedule_generation as generation

    scenario = _Scenario(db_session, sundays=2, volunteers=2, one_each=True)
    _enable_dev_auth(monkeypatch)

    accepted = _recording_accepted_placements(monkeypatch)
    real_solve = generation.solve_schedule

    def solve_then_revoke(scheduling_input, *, policy):
        result = real_solve(scheduling_input, policy=policy)
        db_session.execute(
            text(
                "UPDATE role_qualification SET is_qualified = false"
                " WHERE ministry_membership_id = :m"
            ),
            {"m": result.proposed_assignments[1].membership_id},
        )
        return result

    monkeypatch.setattr(generation, "solve_schedule", solve_then_revoke)
    assert _post(
        api, version_id=scenario.version_id, actor_id=scenario.admin_id
    ).status_code == 409
    assert len(accepted) == 1

    fresh = _fresh(db_session)
    try:
        assert fresh.execute(
            text("SELECT count(*) FROM schedule_version WHERE id = :v"),
            {"v": scenario.version_id},
        ).scalar_one() == 1
        assert fresh.execute(
            text("SELECT count(*) FROM person WHERE church_id = :c"),
            {"c": scenario.church_id},
        ).scalar_one() == 3
    finally:
        fresh.close()


# ==========================================================================
# F -- generating twice
# ==========================================================================


def test_f_a_second_request_creates_nothing_and_duplicates_nothing(
    api, db_session, monkeypatch
):
    """Not idempotency by suppression: the second run genuinely finds every
    position filled, because the first run's assignments are its inputs.
    """
    scenario = _Scenario(db_session, sundays=2, volunteers=2)
    _enable_dev_auth(monkeypatch)

    first = _post(api, version_id=scenario.version_id, actor_id=scenario.admin_id)
    assert first.status_code == 200
    assert first.json()["created_count"] == 2

    second = _post(api, version_id=scenario.version_id, actor_id=scenario.admin_id)

    assert second.status_code == 200
    body = second.json()
    assert body["created_count"] == 0
    assert body["created_assignments"] == []
    assert body["is_complete"] is True

    fresh = _fresh(db_session)
    try:
        rows = _assignment_rows(fresh, scenario.version_id)
    finally:
        fresh.close()

    assert len(rows) == 2
    assert {row.id for row in rows} == {
        created["assignment_id"] for created in first.json()["created_assignments"]
    }


# ==========================================================================
# G -- the request body really is the scheduling policy
# ==========================================================================


def test_g_allow_no_response_in_the_body_changes_the_real_outcome(
    api, db_session, monkeypatch
):
    """Volunteers who never answered. With the default policy they cannot be
    scheduled and the run comes back incomplete; with ``allow_no_response`` in
    the body the same request fills everything.

    This is the bridge test: the value travelled from JSON through the DTO into
    a ``SchedulingPolicy`` and changed what the solver did.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1, available=False)
    _enable_dev_auth(monkeypatch)

    strict = _post(api, version_id=scenario.version_id, actor_id=scenario.admin_id)

    assert strict.status_code == 200
    assert strict.json()["created_count"] == 0
    assert strict.json()["is_complete"] is False
    assert "NO_RESPONSE_DISALLOWED" in (
        strict.json()["unfilled_requirements"][0]["diagnostic_codes"]
    )

    lenient = _post(
        api,
        version_id=scenario.version_id,
        actor_id=scenario.admin_id,
        body={"allow_no_response": True},
    )

    assert lenient.status_code == 200
    assert lenient.json()["created_count"] == 1
    assert lenient.json()["is_complete"] is True


def test_g2_a_target_in_the_body_produces_real_metrics(api, db_session, monkeypatch):
    """Without a target the soft costs are ``None``; with one they are numbers
    the solver actually optimized.
    """
    scenario = _Scenario(db_session, sundays=2, volunteers=2)
    _enable_dev_auth(monkeypatch)

    body = _post(
        api,
        version_id=scenario.version_id,
        actor_id=scenario.admin_id,
        body={"target_assignments_per_candidate": 1},
    ).json()

    metrics = body["metrics"]
    assert metrics["target_excess_total"] == 0
    assert metrics["fairness_cost"] == 2  # 1**2 + 1**2: one Sunday each
    assert metrics["role_variety_cost"] is None
    assert [load["assignment_count"] for load in metrics["assignment_loads"]] == [1, 1]


def test_g3_a_policy_the_domain_rejects_is_422_and_writes_nothing(
    api, db_session, monkeypatch
):
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    _enable_dev_auth(monkeypatch)

    response = _post(
        api,
        version_id=scenario.version_id,
        actor_id=scenario.admin_id,
        body={"target_assignments_per_candidate": -1},
    )

    assert response.status_code == 422
    fresh = _fresh(db_session)
    try:
        assert _assignment_rows(fresh, scenario.version_id) == []
    finally:
        fresh.close()
