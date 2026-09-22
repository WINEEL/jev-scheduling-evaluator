"""The ScheduleVersion review endpoint against real PostgreSQL rows (Task 38).

The offline suite proves the contract with stand-ins. What needs a database is
everything the stand-ins had to assume: that the joins really resolve the
person behind an assignment, that the correlated count really counts, that
**real** Task 23 and Task 26 -- not stubs -- produce the diagnostics, and above
all that a snapshot survives its configuration changing underneath it.

Task 37's harness is reused unchanged: the real ``get_session`` runs on the
test's Session, and the outer transaction is rolled back. ``no_domain_writes``
proves after every test, from its own connection, that this read endpoint
committed nothing.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.models.scheduling_input import AVAILABILITY_AVAILABLE
from app.services.assignment import assign_member
from app.services.finalization_readiness import (
    ISSUE_STALE_REQUIREMENT_SNAPSHOT,
    ISSUE_UNFILLED_REQUIREMENT,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"

#: Every table this endpoint touches, counted before and after each test.
_DOMAIN_TABLES = (
    "assignment",
    "audit_event",
    "schedule_version",
    "schedule_version_requirement",
    "staffing_requirement",
    "event",
    "person",
    "ministry_membership",
)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_domain_writes(integration_engine):
    """Nothing this suite does may outlive its transaction.

    Autouse and depending only on the engine, so it is torn down *after*
    ``db_session`` -- by then the outer rollback has run, and this probe's own
    connection cannot see uncommitted rows, so equal counts are a statement
    about the database rather than about the test's view of it.
    """

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
    """The real ``get_session``, running on the test's Session."""
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
    """A version with ``sundays`` Sundays of ``roles`` roles, and volunteers.

    Everything is committed at the end so it sits behind the savepoint each
    request opens.
    """

    def __init__(
        self,
        session,
        *,
        sundays: int = 2,
        volunteers: int = 2,
        required_count: int = 1,
        roles: int = 1,
        status: str = SCHEDULE_VERSION_STATUS_DRAFT,
    ) -> None:
        self.session = session
        self.church = f.make_church(session)
        self.ministry = f.make_ministry(session, church=self.church, name="Setup")
        self.head = f.make_ministry_head(
            session, church=self.church, ministry=self.ministry, name="Head"
        )
        self.roles = [
            f.make_role(session, ministry=self.ministry, name=f"Position{index}")
            for index in range(roles)
        ]
        self.period = f.make_period(session, ministry=self.ministry)
        self.schedule = f.make_schedule(session, period=self.period)
        self.version = f.make_version(
            session, schedule=self.schedule, period=self.period, status=status
        )
        self.events = []
        self.requirements = {}
        self.staffing = {}
        for index in range(sundays):
            event = f.make_event(
                session,
                period=self.period,
                event_date=NOV_15 + datetime.timedelta(days=7 * index),
            )
            # The factory leaves ``name`` NULL (it is an optional label). Set
            # one here so label resolution is genuinely tested.
            event.name = f"Sunday {index + 1}"
            session.flush()
            self.events.append(event)
            for role in self.roles:
                self.staffing[(index, role.id)] = f.make_staffing_requirement(
                    session, event=event, role=role, required_count=required_count
                )
                self.requirements[(index, role.id)] = f.make_version_requirement(
                    session,
                    version=self.version,
                    event=event,
                    role=role,
                    required_count=required_count,
                )
        self.memberships = []
        self.people = []
        for index in range(volunteers):
            person = f.make_person(
                session, church=self.church, name=f"Volunteer{index}"
            )
            membership = f.make_membership(
                session, person=person, ministry=self.ministry
            )
            for role in self.roles:
                f.make_qualification(
                    session,
                    membership=membership,
                    role=role,
                    decided_by=self.head,
                    is_qualified=True,
                )
            for event in self.events:
                f.make_availability(
                    session,
                    membership=membership,
                    event=event,
                    state=AVAILABILITY_AVAILABLE,
                )
            self.people.append(person)
            self.memberships.append(membership)
        session.flush()

        # A Ministry Head who is not an Admin -- the ordinary reviewer.
        head_person = f.make_person(session, church=self.church, name="Head")
        f.make_membership(
            session, person=head_person, ministry=self.ministry, is_head=True
        )
        # And a member of this ministry who heads nothing.
        member_person = f.make_person(session, church=self.church, name="Member")
        f.make_membership(session, person=member_person, ministry=self.ministry)
        session.flush()

        # **Plain ids, captured now.** Each HTTP request ends with the Task 37
        # boundary closing the Session, which detaches every ORM object here;
        # a later ``scenario.role.id`` would then raise rather than fail the
        # assertion it was written for.
        self.head_id = head_person.id
        self.member_id = member_person.id
        self.admin_id = self.head.id
        self.version_id = self.version.id
        self.ministry_id = self.ministry.id
        self.ministry_name = self.ministry.name
        self.period_id = self.period.id
        self.role_ids = [role.id for role in self.roles]
        self.event_ids = [event.id for event in self.events]
        self.event_dates = [event.event_date for event in self.events]
        self.person_ids = [person.id for person in self.people]
        self.person_names = [person.display_name for person in self.people]
        self.membership_ids = [m.id for m in self.memberships]
        self.requirement_ids = {
            (sunday, index): self.requirements[(sunday, role.id)].id
            for sunday in range(sundays)
            for index, role in enumerate(self.roles)
        }
        self.staffing_ids = {
            (sunday, index): self.staffing[(sunday, role.id)].id
            for sunday in range(sundays)
            for index, role in enumerate(self.roles)
        }
        session.commit()

    def assign(self, *, sunday: int, role_index: int = 0, volunteer: int,
               override_reason: str | None = None) -> tuple[int, int]:
        """Place a volunteer through the real Task 22 write path.

        Returns ``(assignment_id, requirement_id)`` as plain ints, for the same
        detachment reason as above.
        """
        role = self.roles[role_index]
        assignment = assign_member(
            self.session,
            actor=self.head,
            requirement=self.requirements[(sunday, role.id)],
            membership=self.memberships[volunteer],
            override_reason=override_reason,
        )
        self.session.flush()
        identity = (assignment.id, assignment.schedule_version_requirement_id)
        self.session.commit()
        return identity


def _get(api, *, version_id: int, actor_id: int):
    return api.get(
        f"/api/v1/schedule-versions/{version_id}",
        headers={HEADER: str(actor_id)},
    )


# ==========================================================================
# A -- a real DRAFT read end to end
# ==========================================================================


def test_a_a_head_reads_a_real_draft_in_full(api, db_session, monkeypatch):
    scenario = _Scenario(db_session, sundays=2, volunteers=2, roles=2)
    first_id, first_requirement = scenario.assign(sunday=0, role_index=0, volunteer=0)
    second_id, second_requirement = scenario.assign(sunday=1, role_index=1, volunteer=1)
    _enable_dev_auth(monkeypatch)

    response = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id)

    assert response.status_code == 200
    body = response.json()

    # The version and its period, from real rows.
    assert body["schedule_version"]["id"] == scenario.version_id
    assert body["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_DRAFT
    assert body["schedule_version"]["finalized_at"] is None
    assert body["period"]["id"] == scenario.period_id
    assert body["period"]["ministry_id"] == scenario.ministry_id
    assert body["period"]["ministry_name"] == scenario.ministry_name

    # Four requirements: two Sundays x two roles.
    assert len(body["requirements"]) == 4
    dates = [r["event_date"] for r in body["requirements"]]
    assert dates == sorted(dates), "requirements are ordered by snapshot date"
    for requirement in body["requirements"]:
        assert requirement["event_name"] in {"Sunday 1", "Sunday 2"}
        assert requirement["event_kind"] is not None
        assert requirement["role_name"] is not None
        assert requirement["required_count"] == 1

    # The assigned counts really counted.
    assigned = {r["requirement_id"]: r["assigned_count"] for r in body["requirements"]}
    assert assigned[first_requirement] == 1
    assert assigned[second_requirement] == 1
    assert sum(assigned.values()) == 2

    # Assignments resolve to real people through real memberships.
    assert len(body["assignments"]) == 2
    by_id = {a["assignment_id"]: a for a in body["assignments"]}
    assert by_id[first_id]["membership_id"] == scenario.membership_ids[0]
    assert by_id[first_id]["person_id"] == scenario.person_ids[0]
    assert by_id[first_id]["person_display_name"] == scenario.person_names[0]
    assert by_id[first_id]["is_override"] is False
    assert by_id[first_id]["override_reason"] is None
    assert set(by_id) == {first_id, second_id}

    # Totals.
    assert body["summary"] == {
        "required_positions": 4,
        "assigned_positions": 2,
        "unfilled_positions": 2,
        "is_fully_staffed": False,
    }

    # Real Task 23 and Task 26 ran: fresh snapshot, but genuinely unfilled.
    assert body["staleness"]["is_stale"] is False
    assert body["finalization_readiness"]["is_ready"] is False
    codes = {i["code"] for i in body["finalization_readiness"]["issues"]}
    assert ISSUE_UNFILLED_REQUIREMENT in codes


def test_a2_an_admin_may_read_it_too(api, db_session, monkeypatch):
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    _enable_dev_auth(monkeypatch)

    response = _get(api, version_id=scenario.version_id, actor_id=scenario.admin_id)

    assert response.status_code == 200
    assert response.json()["schedule_version"]["id"] == scenario.version_id


def test_a3_a_missing_version_is_404(api, db_session, monkeypatch):
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    _enable_dev_auth(monkeypatch)

    response = _get(
        api, version_id=scenario.version_id + 10_000_000, actor_id=scenario.admin_id
    )

    assert response.status_code == 404


def test_a4_an_empty_version_is_reported_not_refused(api, db_session, monkeypatch):
    scenario = _Scenario(db_session, sundays=0, volunteers=0)
    _enable_dev_auth(monkeypatch)

    body = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id).json()

    assert body["requirements"] == []
    assert body["assignments"] == []
    assert body["summary"]["required_positions"] == 0
    assert body["summary"]["is_fully_staffed"] is True
    # Two empty sets are equal, so an empty version is fresh, not stale.
    assert body["staleness"]["is_stale"] is False


# ==========================================================================
# B -- a real override assignment
# ==========================================================================


def test_b_an_override_shows_its_flag_and_reason_and_no_audit_internals(
    api, db_session, monkeypatch
):
    """The reviewer needs to see *why* a placement was forced. They must not
    see the audit machinery that recorded it.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    # Revoke the qualification, then place the volunteer anyway with a reason.
    db_session.execute(
        text(
            "UPDATE role_qualification SET is_qualified = false"
            " WHERE ministry_membership_id = :m"
        ),
        {"m": scenario.membership_ids[0]},
    )
    db_session.flush()
    db_session.expire_all()
    assignment_id, _ = scenario.assign(
        sunday=0, volunteer=0, override_reason="Only trained person available"
    )
    _enable_dev_auth(monkeypatch)

    response = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id)

    assert response.status_code == 200
    body = response.json()
    served = body["assignments"][0]
    assert served["assignment_id"] == assignment_id
    assert served["is_override"] is True
    assert served["override_reason"] == "Only trained person available"

    # The AuditEvent Task 22 wrote is real...
    audit_rows = db_session.execute(
        text(
            "SELECT count(*) FROM audit_event"
            " WHERE target_table = 'assignment' AND target_id = :id"
        ),
        {"id": assignment_id},
    ).scalar_one()
    assert audit_rows >= 1

    # ...and none of it appears in the response.
    text_body = response.text.lower()
    for forbidden in (
        "overridden_blockers",
        "before_values",
        "after_values",
        "audit",
        "actor_label",
        "occurred_at",
    ):
        assert forbidden not in text_body, forbidden


# ==========================================================================
# C -- a stale version keeps its snapshot
# ==========================================================================


def test_c_current_configuration_drifts_and_the_snapshot_survives(
    api, db_session, monkeypatch
):
    """**The point of the immutable snapshot.**

    After the version was created, someone raises the current staffing
    requirement from 1 to 2 and moves the event a week later. The version must
    still report what it was built for -- and say, separately, that the
    configuration has drifted.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1, required_count=1)
    role_id = scenario.role_ids[0]
    event_id = scenario.event_ids[0]
    original_date = scenario.event_dates[0]
    requirement_id = scenario.requirement_ids[(0, 0)]
    _enable_dev_auth(monkeypatch)

    # Sanity: fresh before the drift.
    before = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id).json()
    assert before["staleness"]["is_stale"] is False

    moved_date = original_date + datetime.timedelta(days=7)
    db_session.execute(
        text("UPDATE staffing_requirement SET required_count = 2 WHERE id = :id"),
        {"id": scenario.staffing_ids[(0, 0)]},
    )
    db_session.execute(
        text("UPDATE event SET event_date = :d WHERE id = :id"),
        {"d": moved_date, "id": event_id},
    )
    db_session.flush()
    db_session.commit()

    response = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id)

    assert response.status_code == 200, "drift is an answer, not an error"
    body = response.json()

    # The snapshot is untouched: the old date and the old count.
    requirement = body["requirements"][0]
    assert requirement["requirement_id"] == requirement_id
    assert requirement["event_date"] == original_date.isoformat()
    assert requirement["required_count"] == 1
    assert body["summary"]["required_positions"] == 1

    # Real Task 23 reports the drift, in both directions.
    staleness = body["staleness"]
    assert staleness["is_stale"] is True
    assert staleness["snapshot_only"] == [
        {
            "event_id": event_id,
            "event_date": original_date.isoformat(),
            "ministry_role_id": role_id,
            "required_count": 1,
        }
    ]
    assert staleness["current_only"] == [
        {
            "event_id": event_id,
            "event_date": moved_date.isoformat(),
            "ministry_role_id": role_id,
            "required_count": 2,
        }
    ]

    # Real Task 26 raises it as an issue too, without the API inventing one.
    codes = {i["code"] for i in body["finalization_readiness"]["issues"]}
    assert ISSUE_STALE_REQUIREMENT_SNAPSHOT in codes
    assert body["finalization_readiness"]["is_ready"] is False

    # The label still resolves -- to today's event row, which is where a
    # display name belongs. Only the scheduling facts are frozen.
    assert requirement["event_name"] == "Sunday 1"


def test_c2_a_deleted_event_leaves_the_requirement_visible_but_unlabelled(
    api, db_session, monkeypatch
):
    """A label is a convenience; a required position is a fact. Losing the
    first must not lose the second.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    role_id = scenario.role_ids[0]
    original_date = scenario.event_dates[0]
    _enable_dev_auth(monkeypatch)

    db_session.execute(
        text("UPDATE ministry_role SET name = :n WHERE id = :id"),
        {"n": "Renamed Position", "id": role_id},
    )
    db_session.flush()
    db_session.commit()

    body = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id).json()

    # The label follows today's row; the scheduling facts do not.
    assert body["requirements"][0]["role_name"] == "Renamed Position"
    assert body["requirements"][0]["role_id"] == role_id
    assert body["requirements"][0]["event_date"] == original_date.isoformat()
    # A rename is not a staleness signal: the comparison is on ids and counts.
    assert body["staleness"]["is_stale"] is False


# ==========================================================================
# D -- an unready REVIEW version
# ==========================================================================


def test_d_an_unready_review_version_returns_200_with_its_issues(
    api, db_session, monkeypatch
):
    scenario = _Scenario(
        db_session,
        sundays=2,
        volunteers=1,
        required_count=1,
        status=SCHEDULE_VERSION_STATUS_REVIEW,
    )
    scenario.assign(sunday=0, volunteer=0)  # one of two Sundays filled
    _enable_dev_auth(monkeypatch)

    response = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id)

    assert response.status_code == 200, "an unready version is still readable"
    body = response.json()

    assert body["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_REVIEW
    assert body["summary"]["unfilled_positions"] == 1
    assert body["finalization_readiness"]["is_ready"] is False

    issues = body["finalization_readiness"]["issues"]
    unfilled = [i for i in issues if i["code"] == ISSUE_UNFILLED_REQUIREMENT]
    assert len(unfilled) == 1
    # Task 26's own message and row id, passed through unchanged.
    assert unfilled[0]["message"]
    assert unfilled[0]["schedule_version_requirement_id"] == (
        scenario.requirement_ids[(1, 0)]
    )
    assert unfilled[0]["assignment_id"] is None


# ==========================================================================
# E -- a ready REVIEW version
# ==========================================================================


def test_e_a_fully_valid_review_version_is_ready(api, db_session, monkeypatch):
    scenario = _Scenario(
        db_session,
        sundays=2,
        volunteers=2,
        required_count=1,
        status=SCHEDULE_VERSION_STATUS_REVIEW,
    )
    scenario.assign(sunday=0, volunteer=0)
    scenario.assign(sunday=1, volunteer=1)
    _enable_dev_auth(monkeypatch)

    body = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id).json()

    assert body["summary"] == {
        "required_positions": 2,
        "assigned_positions": 2,
        "unfilled_positions": 0,
        "is_fully_staffed": True,
    }
    assert body["staleness"]["is_stale"] is False
    assert body["finalization_readiness"] == {"is_ready": True, "issues": []}


def test_e2_a_ready_draft_is_reported_as_ready_without_implying_it_may_finalize(
    api, db_session, monkeypatch
):
    """Task 26 is status-agnostic, so a head can check a draft before
    submitting it. ``is_ready`` describes the contents; it is not permission to
    finalize, and this endpoint performs no transition either way.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1, required_count=1)
    scenario.assign(sunday=0, volunteer=0)
    _enable_dev_auth(monkeypatch)

    body = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id).json()

    assert body["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_DRAFT
    assert body["finalization_readiness"]["is_ready"] is True
    # Unchanged by the read.
    assert db_session.execute(
        text("SELECT status FROM schedule_version WHERE id = :id"),
        {"id": scenario.version_id},
    ).scalar_one() == SCHEDULE_VERSION_STATUS_DRAFT


# ==========================================================================
# F -- authorization against real membership rows
# ==========================================================================


def test_f_an_ordinary_member_cannot_inspect_a_draft(api, db_session, monkeypatch):
    """A member of the very ministry, with an active membership -- and still
    403, because membership is not head authority. Their finalized
    master-schedule view is a separate, later endpoint.
    """
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    _enable_dev_auth(monkeypatch)

    response = _get(api, version_id=scenario.version_id, actor_id=scenario.member_id)

    assert response.status_code == 403
    # Nothing about the version leaked with the refusal.
    assert "requirements" not in response.text
    assert "person_display_name" not in response.text


def test_f2_a_head_of_a_different_ministry_cannot_inspect_it(
    api, db_session, monkeypatch
):
    scenario = _Scenario(db_session, sundays=1, volunteers=1)
    other_ministry = f.make_ministry(
        db_session, church=scenario.church, name="AV"
    )
    outsider = f.make_person(db_session, church=scenario.church, name="AV Head")
    f.make_membership(
        db_session, person=outsider, ministry=other_ministry, is_head=True
    )
    db_session.flush()
    db_session.commit()
    outsider_id = outsider.id
    _enable_dev_auth(monkeypatch)

    response = _get(api, version_id=scenario.version_id, actor_id=outsider_id)

    assert response.status_code == 403


def test_f3_a_review_version_is_equally_protected(api, db_session, monkeypatch):
    scenario = _Scenario(
        db_session, sundays=1, volunteers=1, status=SCHEDULE_VERSION_STATUS_REVIEW
    )
    _enable_dev_auth(monkeypatch)

    assert _get(
        api, version_id=scenario.version_id, actor_id=scenario.member_id
    ).status_code == 403


def test_f4_an_unauthenticated_request_is_401(api, db_session):
    scenario = _Scenario(db_session, sundays=1, volunteers=1)

    response = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id)

    assert response.status_code == 401


# ==========================================================================
# G -- the read really is a read
# ==========================================================================


def test_g_the_request_changes_no_domain_row(api, db_session, monkeypatch):
    """Counted around the GET itself, on the same connection, so this catches
    a write that the request committed *and* one it merely flushed.
    """
    scenario = _Scenario(db_session, sundays=2, volunteers=2)
    scenario.assign(sunday=0, volunteer=0)
    _enable_dev_auth(monkeypatch)

    def counts() -> dict[str, int]:
        return {
            table: db_session.execute(
                text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
            ).scalar_one()
            for table in _DOMAIN_TABLES
        }

    before = counts()
    response = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id)
    after = counts()

    assert response.status_code == 200
    assert after == before
    # In particular, not one new audit row.
    assert after["audit_event"] == before["audit_event"]


def test_g2_repeated_reads_are_identical_and_still_write_nothing(
    api, db_session, monkeypatch
):
    """Deterministic ordering, checked against real PostgreSQL rather than
    trusted: two reads of unchanged data must produce the same document.
    """
    scenario = _Scenario(db_session, sundays=2, volunteers=2, roles=2)
    scenario.assign(sunday=0, role_index=0, volunteer=0)
    scenario.assign(sunday=0, role_index=1, volunteer=1)
    scenario.assign(sunday=1, role_index=0, volunteer=1)
    _enable_dev_auth(monkeypatch)

    # Positive control first: prove the counter actually moves for a real
    # write, so "unchanged" below means something. It has to happen before any
    # request, because the Task 37 boundary closes the Session at the end of
    # one and detaches the scenario's rows.
    before_write = db_session.execute(
        text("SELECT count(*) FROM audit_event")
    ).scalar_one()
    scenario.assign(sunday=1, role_index=1, volunteer=0)
    after_write = db_session.execute(
        text("SELECT count(*) FROM audit_event")
    ).scalar_one()
    assert after_write > before_write, "the counter would have caught a write"

    first = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id)
    second = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(first.json()["assignments"]) == 4

    # And neither read moved it.
    assert db_session.execute(
        text("SELECT count(*) FROM audit_event")
    ).scalar_one() == after_write


def test_g3_an_overfilled_requirement_is_reported_unclamped(
    api, db_session, monkeypatch
):
    """A real authorized capacity override: two people on a requirement asking
    for one. ``assigned_count`` must say two.
    """
    scenario = _Scenario(db_session, sundays=2, volunteers=2, required_count=1)
    scenario.assign(sunday=0, volunteer=0)
    scenario.assign(sunday=0, volunteer=1, override_reason="Training a new server")
    _enable_dev_auth(monkeypatch)

    body = _get(api, version_id=scenario.version_id, actor_id=scenario.head_id).json()

    overfilled = next(
        r
        for r in body["requirements"]
        if r["requirement_id"] == scenario.requirement_ids[(0, 0)]
    )
    assert overfilled["required_count"] == 1
    assert overfilled["assigned_count"] == 2

    # The overfill does not cancel out the genuinely empty second Sunday.
    assert body["summary"]["required_positions"] == 2
    assert body["summary"]["assigned_positions"] == 2
    assert body["summary"]["unfilled_positions"] == 1
    assert body["summary"]["is_fully_staffed"] is False
