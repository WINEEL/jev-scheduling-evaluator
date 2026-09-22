"""Entering the scheduling flow over HTTP (Task 40).

Two endpoints complete the first-pass path: find the periods a head could
schedule, and start the first schedule for one of them. Generation (Task 37)
and review (Task 38) already existed, so what is tested here is the *entry* --
and, just as importantly, what these endpoints refuse to be: they are not
general CRUD, they do not create successor versions, and they do not fill a
schedule they have just started.

Offline: no database. The Session is a recording stand-in answering the few
statements these routes issue, and Task 20's creation service is stubbed where
the subject is HTTP mapping rather than domain behaviour -- with dedicated
tests asserting the real service is the one being called, on the request's own
Session.
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api import routes_schedule_entry as routes
from app.config import get_settings
from app.main import app
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services import schedule_entry as entry_service
from app.services.errors import AuthorizationError, InvalidOperationError

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
PERIODS_URL = "/api/v1/ministries/{id}/scheduling-periods"
START_URL = "/api/v1/scheduling-periods/{id}/schedule-versions"

OCT_4 = datetime.date(2026, 10, 4)
DEC_27 = datetime.date(2026, 12, 27)
LOCKED_AT = datetime.datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

#: Captured before any test can patch the module attributes.
_REAL_LIST = routes.list_ministry_scheduling_periods
_REAL_START = routes.start_first_schedule
_REAL_CREATE_INITIAL = entry_service.create_initial_schedule_version


# --------------------------------------------------------------------------
# Stand-ins
# --------------------------------------------------------------------------


class _StubPerson:
    def __init__(self, id: int = 1) -> None:
        self.id = id
        self.display_name = "Ada Head"
        self.is_admin = False
        self.deactivated_at = None
        self.ministry_memberships: list = []


class _StubMinistry:
    def __init__(self, id: int = 900, name: str = "Setup") -> None:
        self.id = id
        self.name = name


class _StubPeriod:
    def __init__(self, id: int = 700, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.name = "Q4 2026"
        self.start_date = OCT_4
        self.end_date = DEC_27
        self.availability_locked_at = LOCKED_AT
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubVersion:
    def __init__(self, **overrides) -> None:
        self.id = 21
        self.schedule_id = 8
        self.scheduling_period_id = 700
        self.version_number = 1
        self.status = SCHEDULE_VERSION_STATUS_DRAFT
        self.notes = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    """A stand-in request Session recording its transaction boundary.

    It has no ``add``, ``delete``, ``flush`` or ``merge``: the routes must not
    reach for one, and the stubbed service stands in for the one place a real
    flush belongs.
    """

    def __init__(self, *, person=None, ministry=None, period=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.ministry = _StubMinistry() if ministry is None else ministry
        self.period = _StubPeriod() if period is None else period
        self.events: list[str] = []
        self.ministry_lookups = 0
        self.period_lookups = 0

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "FROM person" in sql:
            return _ScalarResult(self.person)
        if "FROM ministry" in sql:
            self.ministry_lookups += 1
            return _ScalarResult(self.ministry)
        if "FROM scheduling_period" in sql:
            self.period_lookups += 1
            return _ScalarResult(self.period)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")

    @property
    def commits(self) -> int:
        return self.events.count("commit")


def _period_summary(**overrides) -> entry_service.SchedulingPeriodSummary:
    fields = dict(
        scheduling_period_id=700,
        name="Q4 2026",
        start_date=OCT_4,
        end_date=DEC_27,
        availability_locked_at=LOCKED_AT,
        schedule=None,
    )
    fields.update(overrides)
    return entry_service.SchedulingPeriodSummary(**fields)


def _schedule_summary(**overrides) -> entry_service.LatestScheduleSummary:
    fields = dict(
        schedule_id=8,
        latest_version_id=21,
        latest_version_number=1,
        latest_version_status=SCHEDULE_VERSION_STATUS_DRAFT,
    )
    fields.update(overrides)
    return entry_service.LatestScheduleSummary(**fields)


def _listing(*periods, ministry=None) -> entry_service.MinistrySchedulingPeriods:
    return entry_service.MinistrySchedulingPeriods(
        ministry=ministry or _StubMinistry(), periods=tuple(periods)
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def session(monkeypatch) -> RecordingSession:
    """Install a recording Session behind the **real** ``get_session``."""
    recording = RecordingSession()
    monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
    return recording


class _CallLog(list):
    behaviour: dict


@pytest.fixture
def listing(monkeypatch) -> _CallLog:
    calls = _CallLog()
    behaviour: dict = {"result": _listing(), "raises": None}

    def stub(session, *, actor, ministry):
        calls.append({"session": session, "actor": actor, "ministry": ministry})
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        return behaviour["result"]

    monkeypatch.setattr(routes, "list_ministry_scheduling_periods", stub)
    calls.behaviour = behaviour
    return calls


@pytest.fixture
def starter(monkeypatch) -> _CallLog:
    calls = _CallLog()
    behaviour: dict = {
        "result": entry_service.StartedSchedule(
            version=_StubVersion(), requirement_snapshot_count=13
        ),
        "raises": None,
    }

    def stub(session, *, actor, period, notes=None):
        calls.append(
            {
                "session": session,
                "actor": actor,
                "period": period,
                "notes": notes,
                "events_so_far": list(getattr(session, "events", [])),
            }
        )
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        return behaviour["result"]

    monkeypatch.setattr(routes, "start_first_schedule", stub)
    calls.behaviour = behaviour
    return calls


@pytest.fixture
def api(monkeypatch) -> TestClient:
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _get_periods(api, *, ministry_id: int = 900, actor_id: int = 1):
    return api.get(
        PERIODS_URL.format(id=ministry_id), headers={HEADER: str(actor_id)}
    )


def _start(api, *, period_id: int = 700, actor_id: int = 1, **kwargs):
    return api.post(
        START_URL.format(id=period_id), headers={HEADER: str(actor_id)}, **kwargs
    )


# ==========================================================================
# Period listing -- identity and authorization (1-6)
# ==========================================================================


def test_01_an_admin_may_list(api, session, listing):
    session.person.is_admin = True

    response = _get_periods(api)

    assert response.status_code == 200
    assert listing[0]["actor"] is session.person


def test_02_the_ministrys_own_head_may_list(api, session, listing):
    assert _get_periods(api).status_code == 200


def test_03_a_head_of_another_ministry_is_403(api, session, listing):
    listing.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _get_periods(api)

    assert response.status_code == 403
    assert response.json() == {"detail": "you do not manage this ministry"}


def test_04_a_normal_user_is_403(api, session, listing):
    listing.behaviour["raises"] = AuthorizationError("not a manager")

    assert _get_periods(api).status_code == 403


def test_05_a_missing_ministry_is_404(api, session, listing):
    session.ministry = None

    response = _get_periods(api)

    assert response.status_code == 404
    assert response.json() == {"detail": "Ministry not found."}
    assert listing == []


def test_06_an_unauthenticated_request_is_401(api, session, listing, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.get(PERIODS_URL.format(id=900))

    assert response.status_code == 401
    assert listing == []


# ==========================================================================
# Period listing -- the payload (7-19)
# ==========================================================================


def test_07_a_ministry_with_no_periods_returns_an_empty_list(api, session, listing):
    body = _get_periods(api).json()

    assert body == {
        "ministry_id": 900,
        "ministry_name": "Setup",
        "periods": [],
        # Task 80. The stub actor heads nothing, so the listing tells the
        # client it is a read -- which is exactly what an Admin overseeing a
        # ministry they do not run should get.
        "can_operate": False,
    }


def test_08_a_periods_basic_fields_are_reported(api, session, listing):
    listing.behaviour["result"] = _listing(_period_summary())

    period = _get_periods(api).json()["periods"][0]

    assert period["scheduling_period_id"] == 700
    assert period["name"] == "Q4 2026"
    assert period["start_date"] == OCT_4.isoformat()
    assert period["end_date"] == DEC_27.isoformat()


def test_09_an_unlocked_period_reports_a_null_lock(api, session, listing):
    listing.behaviour["result"] = _listing(
        _period_summary(availability_locked_at=None)
    )

    assert _get_periods(api).json()["periods"][0]["availability_locked_at"] is None


def test_10_a_locked_period_reports_its_timestamp(api, session, listing):
    listing.behaviour["result"] = _listing(_period_summary())

    locked = _get_periods(api).json()["periods"][0]["availability_locked_at"]

    assert locked is not None
    assert locked.startswith("2026-09-30T12:00")


def test_11_a_period_that_has_not_been_scheduled_has_a_null_schedule(
    api, session, listing
):
    listing.behaviour["result"] = _listing(_period_summary(schedule=None))

    assert _get_periods(api).json()["periods"][0]["schedule"] is None


def test_12_a_scheduled_period_reports_only_its_latest_version(
    api, session, listing
):
    """One version, not a history. A landing screen needs to know which
    schedule to open, not every schedule there has ever been.
    """
    listing.behaviour["result"] = _listing(
        _period_summary(
            schedule=_schedule_summary(latest_version_id=99, latest_version_number=3)
        )
    )

    schedule = _get_periods(api).json()["periods"][0]["schedule"]

    assert schedule == {
        "schedule_id": 8,
        "latest_version_id": 99,
        "latest_version_number": 3,
        "latest_version_status": SCHEDULE_VERSION_STATUS_DRAFT,
    }


def test_13_the_latest_version_is_chosen_by_version_number():
    """Checked on the compiled SQL, because relationship order and insertion
    order are both wrong answers -- and neither would show up in a fixture
    that happened to be created in ascending order.
    """
    from sqlalchemy.dialects import postgresql

    sql = str(
        entry_service._periods_statement(900).compile(dialect=postgresql.dialect())
    )

    assert "DISTINCT ON (schedule_version.schedule_id)" in sql
    inner_order = sql.split("FROM schedule_version ORDER BY")[1]
    assert inner_order.startswith(
        " schedule_version.schedule_id, schedule_version.version_number DESC"
    )
    # No status filter: the newest version counts whatever state it is in.
    assert "schedule_version.status =" not in sql


@pytest.mark.parametrize(
    "status_value",
    [
        SCHEDULE_VERSION_STATUS_DRAFT,
        SCHEDULE_VERSION_STATUS_REVIEW,
        SCHEDULE_VERSION_STATUS_FINALIZED,
    ],
)
def test_14_to_16_every_latest_status_is_reported(
    api, session, listing, status_value
):
    listing.behaviour["result"] = _listing(
        _period_summary(schedule=_schedule_summary(latest_version_status=status_value))
    )

    schedule = _get_periods(api).json()["periods"][0]["schedule"]

    assert schedule["latest_version_status"] == status_value


def test_17_periods_keep_the_services_deterministic_order(api, session, listing):
    listing.behaviour["result"] = _listing(
        _period_summary(scheduling_period_id=700, name="Q4 2026"),
        _period_summary(scheduling_period_id=701, name="Q1 2027"),
    )

    body = _get_periods(api).json()["periods"]

    assert [p["scheduling_period_id"] for p in body] == [700, 701]


def test_17c_both_joins_are_outer_so_no_period_is_ever_dropped():
    """**The failure that would hurt most.** An inner join on Schedule would
    silently hide every period that has *not* been scheduled yet -- precisely
    the ones a head opens this screen to start. An inner join on the latest
    version would hide a Schedule row that has no version.
    """
    from sqlalchemy.dialects import postgresql

    sql = str(
        entry_service._periods_statement(900).compile(dialect=postgresql.dialect())
    )

    assert sql.count("LEFT OUTER JOIN") == 2
    assert "LEFT OUTER JOIN schedule ON" in sql
    # The period table is the driving table, never joined away.
    assert sql.split("FROM")[1].strip().startswith("scheduling_period")


def test_17b_the_query_orders_by_start_date_then_name_then_id():
    from sqlalchemy.dialects import postgresql

    sql = str(
        entry_service._periods_statement(900).compile(dialect=postgresql.dialect())
    )
    outer_order = sql.rsplit("ORDER BY", 1)[1]

    assert outer_order.strip().startswith("scheduling_period.start_date")
    assert "scheduling_period.name" in outer_order
    assert outer_order.rstrip().endswith("scheduling_period.id")


def test_18_no_historical_version_array_is_exposed(api, session, listing):
    """The contract has no place to put one, which is the point: a client
    cannot come to depend on a history this endpoint never promised.
    """
    import app.api.schedule_entry_schemas as schemas

    listing.behaviour["result"] = _listing(
        _period_summary(schedule=_schedule_summary())
    )
    schedule = _get_periods(api).json()["periods"][0]["schedule"]

    assert set(schedule) == {
        "schedule_id",
        "latest_version_id",
        "latest_version_number",
        "latest_version_status",
    }
    for forbidden in ("versions", "history", "all_versions"):
        assert forbidden not in schemas.LatestScheduleSummaryResponse.model_fields


def test_19_no_orm_or_internal_fields_leak(api, session, listing):
    listing.behaviour["result"] = _listing(
        _period_summary(schedule=_schedule_summary())
    )

    text = _get_periods(api).text.lower()

    for forbidden in (
        "created_at",
        "updated_at",
        "church_id",
        "deactivated_at",
        "description",
        "amends_version_id",
        "amendment_reason",
        "finalized_at",
        "audit",
    ):
        assert forbidden not in text, forbidden


def test_19b_the_response_models_forbid_extra_fields():
    import app.api.schedule_entry_schemas as schemas

    for name in schemas.__all__:
        model = getattr(schemas, name)
        assert model.model_config.get("extra") == "forbid", name
        assert model.model_config.get("from_attributes") is not True, name


# ==========================================================================
# Starting the first schedule -- authorization and resolution (20-25)
# ==========================================================================


def test_20_an_admin_may_start_a_schedule(api, session, starter):
    session.person.is_admin = True

    response = _start(api)

    assert response.status_code == 201
    assert starter[0]["actor"] is session.person


def test_21_the_ministrys_own_head_may_start_a_schedule(api, session, starter):
    assert _start(api).status_code == 201


def test_22_a_head_of_another_ministry_is_403(api, session, starter):
    starter.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _start(api)

    assert response.status_code == 403
    assert session.events == ["rollback", "close"]


def test_23_a_normal_user_is_403(api, session, starter):
    starter.behaviour["raises"] = AuthorizationError("not a manager")

    assert _start(api).status_code == 403


def test_24_a_missing_period_is_404(api, session, starter):
    session.period = None

    response = _start(api)

    assert response.status_code == 404
    assert response.json() == {"detail": "Scheduling period not found."}
    assert starter == []


def test_25_unlocked_availability_is_the_domains_409(api, session, starter):
    """The rule is Task 20's and stays there; the API reports its refusal."""
    starter.behaviour["raises"] = InvalidOperationError(
        "cannot create the initial schedule version while this period's"
        " availability is still open -- lock it first"
    )

    response = _start(api)

    assert response.status_code == 409
    assert "availability is still open" in response.json()["detail"]
    assert session.events == ["rollback", "close"]


def test_25b_the_route_does_not_check_the_lock_itself():
    """Checked against imports and calls, not prose: the docstrings
    legitimately explain that the lock rule lives in the service.
    """
    tree = ast.parse(Path(routes.__file__).read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]

    for domain_name in (
        "require_ministry_manager",
        "lock_availability",
        "create_initial_schedule_version",
        "create_successor_schedule_version",
    ):
        assert domain_name not in imported
        assert not hasattr(routes, domain_name)

    source = Path(routes.__file__).read_text()
    tree = ast.parse(source)
    start_route = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "start_period_first_schedule"
    )
    attributes = {
        node.attr for node in ast.walk(start_route) if isinstance(node, ast.Attribute)
    }
    assert "availability_locked_at" not in attributes


# ==========================================================================
# Starting the first schedule -- behaviour and response (26-35)
# ==========================================================================


def test_26_a_locked_valid_period_starts_a_schedule(api, session, starter):
    response = _start(api)

    assert response.status_code == 201
    assert starter[0]["period"] is session.period


def test_27_an_existing_first_schedule_is_the_domains_409(api, session, starter):
    starter.behaviour["raises"] = InvalidOperationError(
        "this schedule already has a version;"
        " successor-version creation is a separate operation"
    )

    response = _start(api)

    assert response.status_code == 409
    assert "already has a version" in response.json()["detail"]


def test_28_the_route_delegates_to_the_real_initial_version_service():
    """The route calls the entry service, which calls Task 20 -- and Task 20
    is the genuine article, not a reimplementation. Snapshot construction
    appears nowhere in the API layer.
    """
    from app.services import schedule_version

    assert _REAL_CREATE_INITIAL is schedule_version.create_initial_schedule_version
    assert _REAL_START is entry_service.start_first_schedule

    for module_path in (
        "app/api/routes_schedule_entry.py",
        "app/api/schedule_entry_schemas.py",
    ):
        tree = ast.parse(Path(module_path).read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        assert "ScheduleVersionRequirement" not in imported
        assert "StaffingRequirement" not in imported

    # And the entry service delegates rather than building a version itself.
    entry_tree = ast.parse(Path(entry_service.__file__).read_text())
    entry_imports = [
        alias.name
        for node in ast.walk(entry_tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert "create_initial_schedule_version" in entry_imports
    assert "ScheduleVersion(" not in Path(entry_service.__file__).read_text()


def test_29_actor_period_and_session_are_passed_through(api, session, starter):
    _start(api)

    call = starter[0]
    assert call["session"] is session
    assert call["actor"] is session.person
    assert call["period"] is session.period


def test_29b_an_optional_note_is_carried_through(api, session, starter):
    _start(api, json={"notes": "Initial Q4 schedule"})

    assert starter[0]["notes"] == "Initial Q4 schedule"


def test_29c_no_body_means_no_note(api, session, starter):
    _start(api)

    assert starter[0]["notes"] is None


def test_29d_a_blank_note_is_the_domains_409(api, session, starter):
    """Task 20 rejects whitespace-only notes; the API does not second-guess
    it with a 422 of its own.
    """
    starter.behaviour["raises"] = InvalidOperationError(
        "notes must not be blank when supplied"
    )

    assert _start(api, json={"notes": "   "}).status_code == 409


def test_30_the_response_carries_the_new_ids_and_state(api, session, starter):
    body = _start(api).json()

    assert body == {
        "schedule_id": 8,
        "schedule_version_id": 21,
        "scheduling_period_id": 700,
        "version_number": 1,
        "status": SCHEDULE_VERSION_STATUS_DRAFT,
        "requirement_snapshot_count": 13,
    }


def test_31_the_requirement_snapshot_count_is_reported(api, session, starter):
    starter.behaviour["result"] = entry_service.StartedSchedule(
        version=_StubVersion(), requirement_snapshot_count=65
    )

    assert _start(api).json()["requirement_snapshot_count"] == 65


def test_31b_a_period_with_no_staffing_reports_zero_rather_than_hiding_it(
    api, session, starter
):
    starter.behaviour["result"] = entry_service.StartedSchedule(
        version=_StubVersion(), requirement_snapshot_count=0
    )

    assert _start(api).json()["requirement_snapshot_count"] == 0


def test_32_no_successor_version_is_ever_created():
    """Neither the route nor the entry service can create one: they do not
    import the operation that would.
    """
    for module_path in (
        "app/api/routes_schedule_entry.py",
        "app/services/schedule_entry.py",
    ):
        tree = ast.parse(Path(module_path).read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        assert "create_successor_schedule_version" not in imported

    assert not hasattr(routes, "create_successor_schedule_version")
    assert not hasattr(entry_service, "create_successor_schedule_version")


def test_33_no_carry_forward_is_invoked():
    for module_path in (
        "app/api/routes_schedule_entry.py",
        "app/services/schedule_entry.py",
    ):
        tree = ast.parse(Path(module_path).read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        assert not any("carry_forward" in name for name in imported)

    for module in (routes, entry_service):
        assert not hasattr(module, "carry_forward_assignment")


def test_34_no_assignments_are_generated_by_starting_a_schedule():
    """Starting a schedule creates an empty one. Filling it is a separate,
    deliberate call to the generate endpoint.
    """
    for module_path in (
        "app/api/routes_schedule_entry.py",
        "app/services/schedule_entry.py",
    ):
        tree = ast.parse(Path(module_path).read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        for forbidden in (
            "generate_draft_schedule",
            "assign_member",
            "solve_schedule",
            "SchedulingPolicy",
        ):
            assert forbidden not in imported, (module_path, forbidden)


def test_35_the_new_version_is_a_draft(api, session, starter):
    body = _start(api).json()

    assert body["status"] == SCHEDULE_VERSION_STATUS_DRAFT
    assert body["version_number"] == 1


def test_35b_no_lifecycle_transition_is_reachable():
    for module_path in (
        "app/api/routes_schedule_entry.py",
        "app/services/schedule_entry.py",
    ):
        tree = ast.parse(Path(module_path).read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        for operation in (
            "submit_for_review",
            "finalize_schedule_version",
            "get_finalization_readiness",
        ):
            assert operation not in imported


# ==========================================================================
# Transaction and audit (36-40)
# ==========================================================================


def test_36_a_successful_creation_commits_once(api, session, starter):
    response = _start(api)

    assert response.status_code == 201
    assert session.events == ["commit", "close"]


def test_36b_the_commit_happens_after_the_service_has_run(api, session, starter):
    _start(api)

    assert starter[0]["events_so_far"] == []
    assert session.events == ["commit", "close"]


def test_37_a_domain_failure_rolls_the_request_back(api, session, starter):
    starter.behaviour["raises"] = InvalidOperationError("availability is still open")

    _start(api)

    assert session.events == ["rollback", "close"]
    assert session.commits == 0


def test_37b_an_unexpected_failure_also_rolls_back(api, session, starter):
    starter.behaviour["raises"] = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _start(api)

    assert session.events == ["rollback", "close"]


def test_38_the_creation_audits_ride_the_same_transaction():
    """Task 20 writes the Schedule and ScheduleVersion audit rows into the
    same Session, so the request's single commit writes the domain rows and
    their history together. Nothing in this task's code commits separately.
    """
    from app.services import schedule_version

    source = Path(schedule_version.__file__).read_text()
    assert "record_audit_event" in source

    for module_path in (
        "app/services/schedule_entry.py",
        "app/api/routes_schedule_entry.py",
    ):
        calls = [
            node.func.attr
            for node in ast.walk(ast.parse(Path(module_path).read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "commit" not in calls, module_path
        assert "rollback" not in calls, module_path


def test_39_no_new_api_audit_action_was_added():
    """The API records nothing of its own; Task 20's existing actions stay the
    source of truth.
    """
    for module_path in (
        "app/api/routes_schedule_entry.py",
        "app/services/schedule_entry.py",
    ):
        tree = ast.parse(Path(module_path).read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        assert "record_audit_event" not in imported
        assert not any(name.startswith("ACTION_") for name in imported)

    import app.services.audit as audit

    actions = {name for name in dir(audit) if name.startswith("ACTION_")}
    for invented in (
        "ACTION_SCHEDULE_STARTED",
        "ACTION_SCHEDULING_PERIODS_LISTED",
        "ACTION_FIRST_SCHEDULE_CREATED",
    ):
        assert invented not in actions


def test_40_neither_route_handles_the_transaction_itself():
    tree = ast.parse(Path(routes.__file__).read_text())

    for route_name in (
        "read_ministry_scheduling_periods",
        "start_period_first_schedule",
    ):
        node = next(
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == route_name
        )
        assert not any(isinstance(child, ast.Try) for child in ast.walk(node))


def test_40b_the_listing_route_writes_nothing(api, session, listing):
    _get_periods(api)

    for forbidden in ("add", "flush", "delete", "merge"):
        assert not hasattr(session, forbidden)

    calls = [
        node.func.attr
        for node in ast.walk(ast.parse(Path(routes.__file__).read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    for forbidden in ("add", "flush", "delete", "merge", "commit", "rollback"):
        assert forbidden not in calls


# ==========================================================================
# Identity and boundary (41-44)
# ==========================================================================


def test_41_the_actor_comes_only_from_the_dependency(api, session, starter, listing):
    import inspect

    assert set(inspect.signature(routes.read_ministry_scheduling_periods).parameters) == {
        "ministry_id",
        "actor",
        "session",
    }
    assert set(inspect.signature(routes.start_period_first_schedule).parameters) == {
        "scheduling_period_id",
        "body",
        "actor",
        "session",
    }


def test_42_no_actor_or_ministry_id_is_accepted_from_the_request(
    api, session, starter, listing
):
    import app.api.schedule_entry_schemas as schemas

    # Not in the body.
    assert set(schemas.StartFirstScheduleRequest.model_fields) == {"notes"}
    assert _start(api, json={"actor_person_id": 99}).status_code == 422
    assert _start(api, json={"ministry_id": 5}).status_code == 422
    assert _start(api, json={"version_number": 2}).status_code == 422
    assert _start(api, json={"status": "FINALIZED"}).status_code == 422
    assert _start(api, json={"amendment_reason": "x"}).status_code == 422

    # Nor in the query string: it is ignored, and the header's actor is used.
    response = api.post(
        START_URL.format(id=700) + "?actor_person_id=99", headers={HEADER: "1"}
    )
    assert response.status_code == 201
    assert starter[-1]["actor"] is session.person


def test_42b_the_openapi_contract_exposes_no_actor_parameter(api):
    spec = api.get("/openapi.json").json()

    for path in (
        "/api/v1/ministries/{ministry_id}/scheduling-periods",
        "/api/v1/scheduling-periods/{scheduling_period_id}/schedule-versions",
    ):
        for operation in spec["paths"][path].values():
            names = {p["name"] for p in operation.get("parameters", [])}
            assert not any("actor" in n.lower() for n in names - {HEADER})


def test_43_one_session_serves_the_whole_request(api, session, starter):
    _start(api)

    assert session.period_lookups == 1
    assert starter[0]["session"] is session


def test_43b_no_second_session_is_opened(api, monkeypatch, listing):
    created: list[RecordingSession] = []

    def factory():
        created.append(RecordingSession())
        return created[-1]

    monkeypatch.setattr(deps, "SessionLocal", factory)

    _get_periods(api)

    assert len(created) == 1


def test_43c_the_listing_resolves_its_ministry_on_the_request_session(
    api, session, listing
):
    _get_periods(api)

    assert session.ministry_lookups == 1
    assert listing[0]["ministry"] is session.ministry


def _installed_middleware_names(application) -> set[str]:
    """The middleware classes actually installed on ``application``.

    Task 76 installs exactly one, ``SessionMiddleware``, to carry the signed
    sign-in cookie. These assertions previously read ``app.user_middleware ==
    []``, which was the right statement while the answer was "none" but says
    nothing about *which* middleware would be wrong. Comparing the set of names
    keeps the real guarantee -- no CORS policy was introduced -- and still fails
    if anything else is added without a test being updated to name it.
    """
    return {
        middleware.cls.__name__ for middleware in application.user_middleware
    }

def test_44_no_cors_or_authentication_change_was_made():
    assert _installed_middleware_names(app) == {"SessionMiddleware"}

    tree = ast.parse(Path("app/main.py").read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert "CORSMiddleware" not in imported

    for module_path in (
        "app/api/routes_schedule_entry.py",
        "app/services/schedule_entry.py",
    ):
        calls = [
            node.func.attr
            for node in ast.walk(ast.parse(Path(module_path).read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "set_cookie" not in calls
        assert "delete_cookie" not in calls

    for module in (routes, entry_service):
        for name in ("jwt", "jose", "google", "oauth", "OAuth2"):
            assert not hasattr(module, name)


# ==========================================================================
# Structural (45-47)
# ==========================================================================


def test_45_no_model_change_was_needed():
    for module_path in (
        "app/api/routes_schedule_entry.py",
        "app/api/schedule_entry_schemas.py",
        "app/services/schedule_entry.py",
    ):
        tree = ast.parse(Path(module_path).read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        for schema_name in ("Base", "mapped_column", "Mapped", "relationship"):
            assert schema_name not in imported, module_path


def test_46_the_schedule_entry_api_added_no_migration():
    # Ten since Task 79 added ``person.church_membership_status``. The API
    # slice this module covers still contributes none.
    assert len(list(Path("alembic/versions").glob("*.py"))) == 10


def test_47_no_general_crud_was_added(api):
    """The API surface is the first-pass flow, identity, health, Task 53's
    ministry-role management, Task 54's staffing-requirement management,
    Task 55's role-qualification management, Task 56's availability
    management, Task 57's serving-limit management, Task 71's period-scoped
    scheduling rules, Task 72's availability lock, Task 74's two generic hard
    rules with the member groups one of them counts, and **Task 79's people
    and membership management**.

    **This assertion used to say "no person CRUD endpoint appeared", and Task
    79 is the task that deliberately adds one.** What the guard was protecting
    was never "people are unmanageable" -- it was that the surface stays an
    inventory somebody has signed off, so a route cannot appear by accident.
    That still holds: the people endpoints below are listed one by one, with
    the same rule every other entry follows -- a soft-deactivation is a POST
    action, and no ``DELETE`` exists for a Person or a membership, because
    neither is ever deleted.
    """
    spec = api.get("/openapi.json").json()
    routes_exposed = {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
    }

    assert routes_exposed == {
        ("GET", "/health"),
        ("GET", "/api/v1/me"),
        # Task 80: the two schedule lifecycle transitions, each on its own
        # path. There is deliberately no third -- no un-finalize, and no
        # PATCH that would take a target status from the caller.
        ("POST", "/api/v1/schedule-versions/{schedule_version_id}/submit-for-review"),
        ("POST", "/api/v1/schedule-versions/{schedule_version_id}/finalize"),
        # Task 77: the signed-in person's own upcoming commitments. Takes no
        # parameters -- the subject is the actor -- and there is deliberately
        # no route for reading anybody else's.
        ("GET", "/api/v1/me/schedule"),
        # Task 76: Google sign-in. These three are the only unauthenticated
        # routes in the API besides /health -- they are how an actor comes to
        # exist, so they cannot require one. Every other route below takes
        # get_current_actor.
        ("GET", "/api/v1/auth/google/login"),
        ("GET", "/api/v1/auth/google/callback"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/ministries/{ministry_id}/scheduling-periods"),
        ("POST", "/api/v1/scheduling-periods/{scheduling_period_id}/schedule-versions"),
        ("POST", "/api/v1/schedule-versions/{schedule_version_id}/generate"),
        ("GET", "/api/v1/schedule-versions/{schedule_version_id}"),
        # Task 53: ministry role management.
        ("GET", "/api/v1/ministries/{ministry_id}/roles"),
        ("POST", "/api/v1/ministries/{ministry_id}/roles"),
        ("PATCH", "/api/v1/ministry-roles/{ministry_role_id}"),
        ("POST", "/api/v1/ministry-roles/{ministry_role_id}/deactivate"),
        ("POST", "/api/v1/ministry-roles/{ministry_role_id}/reactivate"),
        # Task 54: staffing-requirement management, plus the period-events
        # listing it needed to exist at all (no endpoint exposed a period's
        # events before this task).
        ("GET", "/api/v1/scheduling-periods/{scheduling_period_id}/events"),
        ("GET", "/api/v1/events/{event_id}/staffing-requirements"),
        ("PUT", "/api/v1/events/{event_id}/staffing-requirements/{ministry_role_id}"),
        ("DELETE", "/api/v1/events/{event_id}/staffing-requirements/{ministry_role_id}"),
        # Task 55: role-qualification management. No DELETE -- a
        # RoleQualification row is never deleted once created.
        ("GET", "/api/v1/ministry-roles/{ministry_role_id}/qualifications"),
        ("PUT", "/api/v1/ministry-roles/{ministry_role_id}/qualifications/{ministry_membership_id}"),
        # Task 56: availability management.
        ("GET", "/api/v1/events/{event_id}/availability"),
        ("PUT", "/api/v1/events/{event_id}/availability/{ministry_membership_id}"),
        ("DELETE", "/api/v1/events/{event_id}/availability/{ministry_membership_id}"),
        # Task 72: closing availability collection. Not new authority and not
        # new domain behaviour -- lock_availability has existed since Task 15;
        # this is the HTTP surface it never had, and without it the lock that
        # starting a schedule requires was unreachable from the product.
        ("POST", "/api/v1/scheduling-periods/{scheduling_period_id}/availability-lock"),
        # Task 57: serving-limit management. A PUT to set a positive hard
        # maximum, and a DELETE that clears it back to "no limit" -- the same
        # real-row-removal shape as staffing and availability.
        ("GET", "/api/v1/scheduling-periods/{scheduling_period_id}/serving-limits"),
        ("PUT", "/api/v1/scheduling-periods/{scheduling_period_id}/serving-limits/{ministry_membership_id}"),
        ("DELETE", "/api/v1/scheduling-periods/{scheduling_period_id}/serving-limits/{ministry_membership_id}"),
        # Task 71: the period's own scheduling rules. One read for the whole
        # rule set, and a PUT/DELETE pair for the single rule it holds today --
        # ``min_intervening_events``, where DELETE restores "no rule" rather
        # than storing a zero that would mean the same thing.
        ("GET", "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules"),
        ("PUT", "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/min-intervening-events"),
        ("DELETE", "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/min-intervening-events"),
        # Task 74: the two generic hard rules, as siblings under the same
        # scheduling-rules collection -- a per-event cap on a member group, and
        # a member's same-event support requirement. Each is a PUT to set and a
        # DELETE to clear, the shape every rule here already uses.
        ("PUT", "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/member-group-limits/{member_group_id}"),
        ("DELETE", "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/member-group-limits/{member_group_id}"),
        ("PUT", "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/same-event-support/{subject_membership_id}"),
        ("DELETE", "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/same-event-support/{subject_membership_id}"),
        # Task 74: member groups themselves are a ministry resource, because a
        # category of people outlives any one quarter. No DELETE for a group --
        # a group a period's history refers to is not removed here.
        ("GET", "/api/v1/ministries/{ministry_id}/member-groups"),
        ("POST", "/api/v1/ministries/{ministry_id}/member-groups"),
        ("PUT", "/api/v1/member-groups/{member_group_id}/members/{ministry_membership_id}"),
        ("DELETE", "/api/v1/member-groups/{member_group_id}/members/{ministry_membership_id}"),
        # Task 79: the church-wide people directory and the Person record.
        # Reading is Admin-or-Ministry-Head; writing the Person is Admin-only.
        ("GET", "/api/v1/people"),
        ("POST", "/api/v1/people"),
        ("GET", "/api/v1/people/{person_id}"),
        ("PATCH", "/api/v1/people/{person_id}"),
        # The sign-in link: a PUT because it *sets* the address to whatever is
        # sent, and null removes it. No DELETE, so unlinking and relinking are
        # visibly one operation with one audit action.
        ("PUT", "/api/v1/people/{person_id}/auth-link"),
        # Task 79: formal church membership -- MEMBER, NON_MEMBER or UNKNOWN.
        # A PUT because it *sets* the status, and Admin-only because stating
        # that somebody is a member of this church is governance, not clerical
        # editing. Entirely separate from ministry membership below.
        ("PUT", "/api/v1/people/{person_id}/church-membership-status"),
        # Task 79: Ministry Head authority, as church governance. Person in the
        # URL, ministry and direction in the body -- the three things an Admin
        # chooses explicitly. Replaced the two membership-scoped routes this
        # task first shipped, because a ministry nobody leads has no membership
        # to hang a promotion off and could therefore never get its first head.
        ("PUT", "/api/v1/people/{person_id}/ministry-head"),
        # Church-wide lifecycle. POST actions, never DELETE: deactivating
        # preserves every membership, assignment, qualification and audit row.
        ("POST", "/api/v1/people/{person_id}/deactivate"),
        ("POST", "/api/v1/people/{person_id}/reactivate"),
        # Task 79: memberships. `remove` is a POST action for the same reason:
        # one timestamp is written and nothing is deleted, so `DELETE` would
        # describe something that does not happen.
        ("GET", "/api/v1/people/{person_id}/memberships"),
        ("POST", "/api/v1/people/{person_id}/memberships"),
        ("PATCH", "/api/v1/ministry-memberships/{ministry_membership_id}"),
        ("POST", "/api/v1/ministry-memberships/{ministry_membership_id}/remove"),
        # Task 79: the Admin's church-wide ministry list. Read-only, and there
        # is deliberately no POST, PATCH or DELETE beside it -- the domain has
        # no reviewed rule for creating or archiving a ministry through the
        # API, and adding the verb before the rule would be building the
        # dangerous half first.
        ("GET", "/api/v1/ministries"),
    }


def test_47b_no_mutation_endpoint_for_configuration_exists(api):
    """Explicitly: nothing hard-deletes a role or a qualification decision.
    Task 53 deliberately added role management (a ``PATCH`` and two ``POST``
    actions, never a ``DELETE`` -- roles are deactivated, never deleted);
    Task 54 deliberately added staffing-requirement management (a ``PUT`` to
    set a count, and a ``DELETE`` that removes a real configuration row,
    never a person's or ministry's own record); Task 55 deliberately added
    role-qualification management (a ``PUT`` only -- a ``RoleQualification``
    row is never deleted once created, so there is no matching ``DELETE``);
    Task 56 deliberately added availability management (a ``PUT`` to record
    an answer, and a ``DELETE`` that clears it back to no response -- the
    same real-row-removal shape as staffing); Task 57 deliberately added
    serving-limit management (a ``PUT`` to set a positive hard maximum, and a
    ``DELETE`` that clears it back to no limit); Task 71 deliberately added
    the period's event-gap rule (a ``PUT`` to set a positive number of
    intervening events, and a ``DELETE`` that clears it back to no rule); and
    Task 74 deliberately added the two generic hard rules (each a ``PUT`` to
    set and a ``DELETE`` that clears it back to no rule) plus group membership
    (a ``PUT`` to put somebody in a group and a ``DELETE`` to take them out --
    a real join row, never a person's own record); and Task 79 deliberately
    added people management, whose three ``PUT``s each *set* a value -- the
    sign-in link (an address, or ``null`` to remove it), the formal church
    membership status, and whether somebody leads a named ministry -- and which
    has **no** ``DELETE`` at all, because neither a Person, a membership nor a
    ministry is ever deleted.
    Everything else the project stakeholder's first pass assumes is already configured remains
    absent.
    """
    spec = api.get("/openapi.json").json()
    allowed_put_or_delete = {
        "/api/v1/events/{event_id}/staffing-requirements/{ministry_role_id}",
        "/api/v1/ministry-roles/{ministry_role_id}/qualifications/{ministry_membership_id}",
        "/api/v1/events/{event_id}/availability/{ministry_membership_id}",
        "/api/v1/scheduling-periods/{scheduling_period_id}/serving-limits/{ministry_membership_id}",
        "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/min-intervening-events",
        "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/member-group-limits/{member_group_id}",
        "/api/v1/scheduling-periods/{scheduling_period_id}/scheduling-rules/same-event-support/{subject_membership_id}",
        "/api/v1/member-groups/{member_group_id}/members/{ministry_membership_id}",
        # Task 79. The three people-management PUTs, and there is deliberately
        # no matching DELETE for any of them -- see the assertion below. Each
        # *sets* a value: an address (or null), a church membership status, and
        # whether somebody leads a named ministry.
        "/api/v1/people/{person_id}/auth-link",
        "/api/v1/people/{person_id}/church-membership-status",
        "/api/v1/people/{person_id}/ministry-head",
    }

    for path, operations in spec["paths"].items():
        for method in operations:
            if method.upper() in {"PUT", "DELETE"} and path not in allowed_put_or_delete:
                raise AssertionError(f"unexpected {method.upper()} {path}")

    # This used to read ``assert "people" not in path``, back when no people
    # endpoint existed. Task 79 added them, and the claim worth keeping is the
    # stronger and more specific one: nothing about a person or a membership is
    # ever removed by HTTP verb.
    for path, operations in spec["paths"].items():
        if path.startswith("/api/v1/people") or path.startswith(
            "/api/v1/ministry-memberships/{ministry_membership_id}"
        ):
            assert "delete" not in operations, path


def test_47c_the_flow_is_reachable_in_six_calls(api):
    """The whole flow, as a contract: list periods, start, generate, review,
    submit, finalize. No successor or carry-forward *operation* appears in it.

    **The assertion is about operations, not prose** (Task 80). It used to
    scan the whole rendered spec for the word "successor", which worked only
    while nothing was allowed to explain itself: the finalize endpoint has to
    say that a published schedule is corrected by a successor version, because
    that is the one thing its caller most needs to know and there is no
    endpoint that does it. So the summaries and operation ids -- what the API
    *offers* -- are what is checked.
    """
    spec = api.get("/openapi.json").json()
    paths = spec["paths"]

    assert "/api/v1/ministries/{ministry_id}/scheduling-periods" in paths
    assert "/api/v1/scheduling-periods/{scheduling_period_id}/schedule-versions" in paths
    assert "generate" in str(paths["/api/v1/schedule-versions/{schedule_version_id}/generate"])
    assert "get" in paths["/api/v1/schedule-versions/{schedule_version_id}"]
    assert "post" in paths["/api/v1/schedule-versions/{schedule_version_id}/submit-for-review"]
    assert "post" in paths["/api/v1/schedule-versions/{schedule_version_id}/finalize"]

    offered = " ".join(
        str(operation.get("summary", "")) + " " + str(operation.get("operationId", ""))
        for operations in paths.values()
        for operation in operations.values()
    ).lower()
    for jargon in ("successor", "carry_forward", "carry-forward", "amend"):
        assert jargon not in offered, jargon

    # And no path offers one, whatever any description says.
    for path in paths:
        for jargon in ("successor", "carry-forward", "amend"):
            assert jargon not in path.lower(), path


# ==========================================================================
# The entry service itself
#
# The route tests above stub it, because their subject is the HTTP contract.
# These call it directly, so its authorization, its SQL and its mapping are
# covered rather than assumed.
# ==========================================================================


class _Membership:
    def __init__(self, *, ministry_id: int, is_head: bool = True, deactivated=None):
        self.ministry_id = ministry_id
        self.is_ministry_head = is_head
        self.deactivated_at = deactivated


class _Row:
    def __init__(self, **fields) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


class _AllResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)


class _ScalarOneResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one(self):
        return self._value


class _ServiceSession:
    """A Session stand-in for the entry service. No write methods at all
    except ``flush``, which the service legitimately calls once.
    """

    def __init__(self, *, period_rows=(), snapshot_count: int = 0) -> None:
        self.period_rows = list(period_rows)
        self.snapshot_count = snapshot_count
        self.statements: list[str] = []
        self.flushes = 0

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("SELECT count("):
            return _ScalarOneResult(self.snapshot_count)
        if "FROM scheduling_period" in sql:
            return _AllResult(self.period_rows)
        raise AssertionError(f"unexpected query in the entry service: {sql}")

    def flush(self, *args, **kwargs) -> None:
        self.flushes += 1


def _row(**overrides):
    fields = dict(
        id=700,
        name="Q4 2026",
        start_date=OCT_4,
        end_date=DEC_27,
        availability_locked_at=LOCKED_AT,
        schedule_id=None,
        version_id=None,
        version_number=None,
        status=None,
    )
    fields.update(overrides)
    return _Row(**fields)


def _admin() -> _StubPerson:
    actor = _StubPerson()
    actor.is_admin = True
    return actor


def _head(ministry_id: int = 900) -> _StubPerson:
    actor = _StubPerson()
    actor.ministry_memberships = [_Membership(ministry_id=ministry_id)]
    return actor


# -- listing: authorization through the real shared rule -------------------


def test_48_listing_refuses_a_person_who_manages_nothing():
    session = _ServiceSession()

    with pytest.raises(AuthorizationError):
        entry_service.list_ministry_scheduling_periods(
            session, actor=_StubPerson(), ministry=_StubMinistry()
        )

    # Refused before reading any period.
    assert session.statements == []


def test_48b_listing_refuses_a_head_of_another_ministry():
    with pytest.raises(AuthorizationError):
        entry_service.list_ministry_scheduling_periods(
            _ServiceSession(), actor=_head(901), ministry=_StubMinistry(id=900)
        )


def test_48c_listing_allows_this_ministrys_head_and_an_admin():
    for actor in (_head(900), _admin()):
        result = entry_service.list_ministry_scheduling_periods(
            _ServiceSession(), actor=actor, ministry=_StubMinistry()
        )
        assert result.ministry.id == 900


def test_48d_a_deactivated_head_is_refused():
    actor = _StubPerson()
    actor.ministry_memberships = [
        _Membership(
            ministry_id=900, deactivated=datetime.datetime(2026, 1, 1, tzinfo=UTC)
        )
    ]

    with pytest.raises(AuthorizationError):
        entry_service.list_ministry_scheduling_periods(
            _ServiceSession(), actor=actor, ministry=_StubMinistry()
        )


# -- listing: mapping ------------------------------------------------------


def test_49_a_period_with_no_schedule_maps_to_none():
    session = _ServiceSession(period_rows=[_row(schedule_id=None)])

    result = entry_service.list_ministry_scheduling_periods(
        session, actor=_admin(), ministry=_StubMinistry()
    )

    period = result.periods[0]
    assert period.scheduling_period_id == 700
    assert period.name == "Q4 2026"
    assert period.availability_locked_at == LOCKED_AT
    assert period.schedule is None


def test_49b_a_period_with_a_schedule_maps_its_latest_version():
    session = _ServiceSession(
        period_rows=[
            _row(
                schedule_id=8,
                version_id=21,
                version_number=3,
                status=SCHEDULE_VERSION_STATUS_REVIEW,
            )
        ]
    )

    schedule = entry_service.list_ministry_scheduling_periods(
        session, actor=_admin(), ministry=_StubMinistry()
    ).periods[0].schedule

    assert schedule.schedule_id == 8
    assert schedule.latest_version_id == 21
    assert schedule.latest_version_number == 3
    assert schedule.latest_version_status == SCHEDULE_VERSION_STATUS_REVIEW


def test_49c_a_schedule_with_no_version_yet_is_still_reported():
    """The outer join is left, not inner: a Schedule row that somehow has no
    version must not make its period vanish from the list.
    """
    session = _ServiceSession(period_rows=[_row(schedule_id=8)])

    schedule = entry_service.list_ministry_scheduling_periods(
        session, actor=_admin(), ministry=_StubMinistry()
    ).periods[0].schedule

    assert schedule.schedule_id == 8
    assert schedule.latest_version_id is None
    assert schedule.latest_version_status is None


def test_49d_an_unlocked_period_keeps_a_null_lock():
    session = _ServiceSession(period_rows=[_row(availability_locked_at=None)])

    result = entry_service.list_ministry_scheduling_periods(
        session, actor=_admin(), ministry=_StubMinistry()
    )

    assert result.periods[0].availability_locked_at is None


def test_50_listing_is_one_query_regardless_of_how_many_periods():
    """The N+1 this endpoint would otherwise obviously have: one query, not
    one per period to find its schedule and one more for its latest version.
    """
    session = _ServiceSession(
        period_rows=[_row(id=700 + i, schedule_id=i) for i in range(20)]
    )

    result = entry_service.list_ministry_scheduling_periods(
        session, actor=_admin(), ministry=_StubMinistry()
    )

    assert len(result.periods) == 20
    assert len(session.statements) == 1


def test_50b_listing_never_writes():
    session = _ServiceSession(period_rows=[_row()])

    entry_service.list_ministry_scheduling_periods(
        session, actor=_admin(), ministry=_StubMinistry()
    )

    assert session.flushes == 0
    for forbidden in ("add", "delete", "merge", "commit", "rollback"):
        assert not hasattr(session, forbidden)


# -- starting: delegation to Task 20 ---------------------------------------


@pytest.fixture
def stubbed_initial(monkeypatch) -> _CallLog:
    """Stand in for Task 20, whose own rules have their own suite.

    What matters here is that the entry service hands it the request's
    Session, actor, period and notes unchanged -- and does nothing else.
    """
    calls = _CallLog()
    behaviour: dict = {"version": _StubVersion(), "raises": None}

    def stub(session, *, actor, period, notes=None):
        calls.append(
            {"session": session, "actor": actor, "period": period, "notes": notes}
        )
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        return behaviour["version"]

    monkeypatch.setattr(entry_service, "create_initial_schedule_version", stub)
    calls.behaviour = behaviour
    return calls


def test_51_starting_delegates_everything_to_task_20(stubbed_initial):
    session = _ServiceSession(snapshot_count=13)
    actor = _admin()
    period = _StubPeriod()

    started = entry_service.start_first_schedule(
        session, actor=actor, period=period, notes="Initial Q4 schedule"
    )

    assert len(stubbed_initial) == 1
    call = stubbed_initial[0]
    assert call["session"] is session
    assert call["actor"] is actor
    assert call["period"] is period
    assert call["notes"] == "Initial Q4 schedule"
    assert started.version is stubbed_initial.behaviour["version"]


def test_51b_the_entry_service_adds_no_authorization_of_its_own(stubbed_initial):
    """Task 20 already authorizes. A second check here would be a second
    opinion that could disagree with the first -- and this stub proves the
    service does not add one, because an actor who manages nothing still
    reaches the delegated call.
    """
    entry_service.start_first_schedule(
        _ServiceSession(), actor=_StubPerson(), period=_StubPeriod()
    )

    assert len(stubbed_initial) == 1


def test_52_the_snapshot_is_counted_after_a_flush(stubbed_initial):
    """Task 20 leaves the snapshot rows pending because it needs none of their
    identities; counting them is a later statement that does.
    """
    session = _ServiceSession(snapshot_count=65)

    started = entry_service.start_first_schedule(
        session, actor=_admin(), period=_StubPeriod()
    )

    assert started.requirement_snapshot_count == 65
    assert session.flushes == 1
    assert session.statements[-1].startswith("SELECT count(")


def test_52b_a_period_with_no_staffing_counts_zero(stubbed_initial):
    session = _ServiceSession(snapshot_count=0)

    started = entry_service.start_first_schedule(
        session, actor=_admin(), period=_StubPeriod()
    )

    assert started.requirement_snapshot_count == 0


def test_52c_the_count_is_scoped_to_the_new_version():
    from sqlalchemy.dialects import postgresql

    statement = (
        entry_service.select(entry_service.func.count())
        .select_from(entry_service.ScheduleVersionRequirement)
        .where(entry_service.ScheduleVersionRequirement.schedule_version_id == 21)
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "FROM schedule_version_requirement" in sql
    assert "schedule_version_requirement.schedule_version_id =" in sql


def test_53_a_domain_refusal_propagates_untouched(stubbed_initial):
    """The entry service neither catches nor translates Task 20's errors --
    the API's central handler maps them to 409.
    """
    stubbed_initial.behaviour["raises"] = InvalidOperationError(
        "cannot create the initial schedule version while this period's"
        " availability is still open -- lock it first"
    )
    session = _ServiceSession()

    with pytest.raises(InvalidOperationError, match="availability is still open"):
        entry_service.start_first_schedule(
            session, actor=_admin(), period=_StubPeriod()
        )

    # And nothing was flushed or counted after the failure.
    assert session.flushes == 0


def test_53b_the_entry_service_never_commits_or_rolls_back():
    calls = [
        node.func.attr
        for node in ast.walk(ast.parse(Path(entry_service.__file__).read_text()))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert "commit" not in calls
    assert "rollback" not in calls
    assert "add" not in calls
    assert "delete" not in calls
    # One flush, and only one.
    assert calls.count("flush") == 1
