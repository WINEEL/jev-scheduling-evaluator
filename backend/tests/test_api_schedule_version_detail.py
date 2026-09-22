"""The ScheduleVersion review endpoint (Task 38).

A read endpoint, so almost everything worth testing is about *what it says* and
*what it refuses to say*: that the snapshot's numbers survive into the
response rather than today's configuration, that a stale or unready version is
still a 200 with diagnostics attached, that a person who does not manage the
ministry gets nothing, and that a request which only reads really does only
read.

Offline: no database. The Session is a recording stand-in that answers the
handful of statements this endpoint issues, and Tasks 23 and 26 are stubbed
where the point is the mapping rather than the diagnosis -- with dedicated
tests asserting the real functions are the ones being called, on the request's
own Session.
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api import routes_schedule_versions as routes
from app.config import get_settings
from app.main import app
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services import schedule_version_detail as detail_service
from app.services.errors import AuthorizationError
from app.services.finalization_readiness import (
    FinalizationIssue,
    FinalizationReadinessResult,
)
from app.services.schedule_staleness import (
    RequirementFingerprint,
    ScheduleVersionStalenessResult,
)
from app.services.schedule_version_detail import (
    AssignmentDetail,
    RequirementDetail,
    ScheduleVersionDetail,
)

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
URL = "/api/v1/schedule-versions/{id}"

NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)

#: Captured before any test can patch the module attributes.
_REAL_DETAIL = routes.get_schedule_version_detail
_REAL_READINESS = detail_service.get_finalization_readiness


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


class _StubVersion:
    def __init__(self, id: int = 7, **overrides) -> None:
        self.id = id
        self.schedule_id = 70
        self.scheduling_period_id = 700
        self.version_number = 1
        self.status = SCHEDULE_VERSION_STATUS_DRAFT
        self.finalized_at = None
        self.amends_version_id = None
        self.amendment_reason = None
        self.notes = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubPeriod:
    def __init__(self) -> None:
        self.id = 700
        self.name = "Q4 2026"
        self.ministry_id = 900
        self.start_date = NOV_15
        self.end_date = NOV_22
        self.availability_locked_at = None


class _StubMinistry:
    def __init__(self) -> None:
        self.id = 900
        self.name = "Setup"


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    """Answers the actor lookup and the version lookup; records its boundary.

    It has no ``add``, ``flush``, ``delete`` or ``merge`` on purpose: a read
    endpoint that reached for one would raise rather than quietly succeed.
    """

    def __init__(self, *, person=None, version=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.version = _StubVersion() if version is None else version
        self.events: list[str] = []
        self.statements: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.statements.append(sql)
        if "FROM person" in sql:
            return _ScalarResult(self.person)
        if "FROM schedule_version" in sql:
            return _ScalarResult(self.version)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


def _staleness(*, current=(), snapshot=()) -> ScheduleVersionStalenessResult:
    return ScheduleVersionStalenessResult(
        current_requirements=frozenset(current),
        snapshot_requirements=frozenset(snapshot),
    )


def _fingerprint(
    *, event_id=31, event_date=NOV_15, role_id=21, required_count=1
) -> RequirementFingerprint:
    return RequirementFingerprint(
        event_id=event_id,
        event_date=event_date,
        ministry_role_id=role_id,
        required_count=required_count,
    )


def _requirement(**overrides) -> RequirementDetail:
    fields = dict(
        requirement_id=11,
        event_id=31,
        event_date=NOV_15,
        event_name="Sunday Service",
        event_kind="SUNDAY",
        role_id=21,
        role_name="Setup Lead",
        required_count=2,
        assigned_count=1,
    )
    fields.update(overrides)
    return RequirementDetail(**fields)


def _assignment(**overrides) -> AssignmentDetail:
    fields = dict(
        assignment_id=501,
        requirement_id=11,
        event_id=31,
        membership_id=41,
        person_id=51,
        person_display_name="Bea Volunteer",
        role_id=21,
        role_name="Setup Lead",
        is_override=False,
        override_reason=None,
    )
    fields.update(overrides)
    return AssignmentDetail(**fields)


def _detail(
    *,
    version=None,
    requirements=(),
    assignments=(),
    staleness=None,
    issues=(),
) -> ScheduleVersionDetail:
    return ScheduleVersionDetail(
        version=version or _StubVersion(),
        period=_StubPeriod(),
        ministry=_StubMinistry(),
        requirements=tuple(requirements),
        assignments=tuple(assignments),
        readiness=FinalizationReadinessResult(
            staleness=staleness if staleness is not None else _staleness(),
            issues=tuple(issues),
        ),
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def session(monkeypatch) -> RecordingSession:
    recording = RecordingSession()
    monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
    return recording


class _CallLog(list):
    behaviour: dict


@pytest.fixture
def service(monkeypatch) -> _CallLog:
    """Stub the detail service, recording how the route called it."""
    calls = _CallLog()
    behaviour: dict = {"result": _detail(), "raises": None}

    def stub(session, *, actor, version):
        calls.append({"session": session, "actor": actor, "version": version})
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        return behaviour["result"]

    monkeypatch.setattr(routes, "get_schedule_version_detail", stub)
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


def _get(api: TestClient, *, version_id: int = 7, actor_id: int = 1):
    return api.get(URL.format(id=version_id), headers={HEADER: str(actor_id)})


# ==========================================================================
# Identity and authorization (1-7)
# ==========================================================================


def test_01_an_admin_may_read(api, session, service):
    """Authorization is the domain's decision; the route's job is to hand it
    the actor and the version and report what it says.
    """
    session.person.is_admin = True

    response = _get(api)

    assert response.status_code == 200
    assert service[0]["actor"] is session.person


def test_02_a_head_of_this_ministry_may_read(api, session, service):
    response = _get(api)

    assert response.status_code == 200


def test_03_a_head_of_another_ministry_is_403(api, session, service):
    service.behaviour["raises"] = AuthorizationError(
        "you do not manage this ministry"
    )

    response = _get(api)

    assert response.status_code == 403
    assert response.json() == {"detail": "you do not manage this ministry"}


def test_04_an_ordinary_member_is_403(api, session, service):
    """A normal volunteer gains no draft or review access from this endpoint.
    Their finalized master-schedule view is a separate, later endpoint.
    """
    service.behaviour["raises"] = AuthorizationError("not a manager")

    assert _get(api).status_code == 403


def test_04b_the_real_rule_is_the_shared_one_not_a_copy():
    """The service reuses ``require_ministry_reader`` rather than restating
    "admin or head" -- checked by import, not by prose.

    The **reader** rule, deliberately: this endpoint is oversight, so an Admin
    who heads nothing sees the version in full. Whether they may change it is
    a different question the response answers separately, in ``can_operate``.
    """
    tree = ast.parse(Path(detail_service.__file__).read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]

    assert "require_ministry_reader" in imported
    # And never the write rule: a read that enforced it would hide a ministry
    # from the Admin overseeing it.
    assert "require_ministry_operator" not in imported
    # And the route does not re-decide it.
    route_tree = ast.parse(Path(routes.__file__).read_text())
    route_imports = [
        alias.name
        for node in ast.walk(route_tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert "require_ministry_manager" not in route_imports
    assert not hasattr(routes, "require_ministry_manager")


def test_05_an_unauthenticated_request_is_401(api, session, service, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.get(URL.format(id=7))

    assert response.status_code == 401
    assert service == []


def test_06_a_missing_version_is_404(api, session, service):
    session.version = None

    response = _get(api)

    assert response.status_code == 404
    assert response.json() == {"detail": "Schedule version not found."}
    assert service == []


def test_07_the_actor_comes_only_from_the_dependency(api, session, service):
    """No actor id is accepted in the path, the query string or anywhere else.
    """
    import inspect

    parameters = set(inspect.signature(routes.read_schedule_version).parameters)
    assert parameters == {"schedule_version_id", "actor", "session"}
    assert "actor_person_id" not in parameters

    # A query-string attempt is simply ignored.
    response = api.get(
        URL.format(id=7) + "?actor_person_id=99", headers={HEADER: "1"}
    )
    assert response.status_code == 200
    assert service[-1]["actor"] is session.person


# ==========================================================================
# The version itself (8-13)
# ==========================================================================


def test_08_identity_fields_are_reported(api, session, service):
    body = _get(api).json()["schedule_version"]

    assert body["id"] == 7
    assert body["schedule_id"] == 70
    assert body["scheduling_period_id"] == 700
    assert body["version_number"] == 1


def test_09_a_draft_is_reported_as_draft(api, session, service):
    body = _get(api).json()["schedule_version"]

    assert body["status"] == SCHEDULE_VERSION_STATUS_DRAFT
    assert body["finalized_at"] is None


def test_10_a_review_version_is_reported_as_review(api, session, service):
    service.behaviour["result"] = _detail(
        version=_StubVersion(status=SCHEDULE_VERSION_STATUS_REVIEW)
    )

    body = _get(api).json()["schedule_version"]

    assert body["status"] == SCHEDULE_VERSION_STATUS_REVIEW
    assert body["finalized_at"] is None


def test_11_a_finalized_version_reports_its_timestamp(api, session, service):
    finalized_at = datetime.datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
    service.behaviour["result"] = _detail(
        version=_StubVersion(
            status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=finalized_at
        )
    )

    body = _get(api).json()["schedule_version"]

    assert body["status"] == SCHEDULE_VERSION_STATUS_FINALIZED
    assert body["finalized_at"] is not None
    assert body["finalized_at"].startswith("2026-10-01T12:30")


def test_12_amendment_lineage_and_reason_are_reported(api, session, service):
    service.behaviour["result"] = _detail(
        version=_StubVersion(
            version_number=2,
            amends_version_id=6,
            amendment_reason="Ann withdrew after finalization",
        )
    )

    body = _get(api).json()["schedule_version"]

    assert body["amends_version_id"] == 6
    assert body["amendment_reason"] == "Ann withdrew after finalization"


def test_13_notes_are_reported(api, session, service):
    service.behaviour["result"] = _detail(
        version=_StubVersion(notes="Thanksgiving weekend is thin")
    )

    assert _get(api).json()["schedule_version"]["notes"] == (
        "Thanksgiving weekend is thin"
    )


# ==========================================================================
# The period (14-15)
# ==========================================================================


def test_14_the_period_and_its_ministry_are_labelled(api, session, service):
    period = _get(api).json()["period"]

    assert period["id"] == 700
    assert period["name"] == "Q4 2026"
    assert period["ministry_id"] == 900
    assert period["ministry_name"] == "Setup"
    assert period["start_date"] == NOV_15.isoformat()
    assert period["end_date"] == NOV_22.isoformat()


def test_15_the_availability_lock_is_reported(api, session, service):
    locked = datetime.datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
    detail = _detail()
    detail.period.availability_locked_at = locked
    service.behaviour["result"] = detail

    assert _get(api).json()["period"]["availability_locked_at"].startswith(
        "2026-10-15T09:00"
    )


def test_15b_an_open_availability_window_is_null_not_omitted(api, session, service):
    assert _get(api).json()["period"]["availability_locked_at"] is None


# ==========================================================================
# Requirements (16-20)
# ==========================================================================


def test_16_the_snapshot_date_is_reported(api, session, service):
    """Not the current Event's date. If the event moved, the version was still
    built for this date, and ``staleness`` is where that difference belongs.
    """
    service.behaviour["result"] = _detail(
        requirements=[_requirement(event_date=NOV_15)]
    )

    assert _get(api).json()["requirements"][0]["event_date"] == NOV_15.isoformat()


def test_17_the_snapshot_required_count_is_reported(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[_requirement(required_count=3)]
    )

    assert _get(api).json()["requirements"][0]["required_count"] == 3


def test_18_event_and_role_labels_are_resolved(api, session, service):
    service.behaviour["result"] = _detail(requirements=[_requirement()])

    requirement = _get(api).json()["requirements"][0]

    assert requirement["event_name"] == "Sunday Service"
    assert requirement["event_kind"] == "SUNDAY"
    assert requirement["role_name"] == "Setup Lead"


def test_18b_a_missing_label_is_null_and_hides_no_requirement(api, session, service):
    """A deleted Event or Role must not make a required position disappear."""
    service.behaviour["result"] = _detail(
        requirements=[_requirement(event_name=None, event_kind=None, role_name=None)]
    )

    requirements = _get(api).json()["requirements"]

    assert len(requirements) == 1
    assert requirements[0]["event_name"] is None
    assert requirements[0]["role_name"] is None
    assert requirements[0]["required_count"] == 2


def test_19_requirements_keep_the_services_order(api, session, service):
    ordered = [
        _requirement(requirement_id=11, event_id=31, event_date=NOV_15),
        _requirement(requirement_id=12, event_id=32, event_date=NOV_22),
    ]
    service.behaviour["result"] = _detail(requirements=ordered)

    body = _get(api).json()["requirements"]

    assert [r["requirement_id"] for r in body] == [11, 12]


def test_19b_the_query_orders_by_snapshot_date_then_role_display_order():
    """The ordering is in the statement, not left to PostgreSQL. Checked on the
    compiled SQL so a dropped ``order_by`` cannot pass.
    """
    from sqlalchemy.dialects import postgresql

    sql = str(
        detail_service._requirements_statement(1).compile(
            dialect=postgresql.dialect()
        )
    )
    order_by = sql.split("ORDER BY")[1]

    assert order_by.index("event_date") < order_by.index("display_order")
    assert "ministry_role.name" in order_by
    assert order_by.rstrip().endswith("schedule_version_requirement.id")


def test_20_a_version_with_no_requirements_is_supported(api, session, service):
    body = _get(api).json()

    assert body["requirements"] == []
    assert body["summary"]["required_positions"] == 0
    assert body["summary"]["is_fully_staffed"] is True


# ==========================================================================
# Assignments (21-27)
# ==========================================================================


def test_21_an_assignment_names_its_requirement(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[_requirement(requirement_id=11)],
        assignments=[_assignment(requirement_id=11)],
    )

    body = _get(api).json()

    assert body["assignments"][0]["requirement_id"] == 11
    assert body["requirements"][0]["requirement_id"] == 11


def test_22_the_person_is_resolved_through_the_membership(api, session, service):
    service.behaviour["result"] = _detail(assignments=[_assignment()])

    assignment = _get(api).json()["assignments"][0]

    assert assignment["membership_id"] == 41
    assert assignment["person_id"] == 51
    assert assignment["person_display_name"] == "Bea Volunteer"


def test_22b_the_query_reaches_person_only_through_membership():
    """``Assignment -> MinistryMembership -> Person``. Joining Person directly
    would lose the link between "who is serving" and "in which ministry".
    """
    from sqlalchemy.dialects import postgresql

    sql = str(
        detail_service._assignments_statement(1).compile(dialect=postgresql.dialect())
    )

    assert "JOIN ministry_membership ON ministry_membership.id = assignment.ministry_membership_id" in sql
    assert "JOIN person ON person.id = ministry_membership.person_id" in sql


def test_23_an_ordinary_assignment_is_serialized(api, session, service):
    service.behaviour["result"] = _detail(assignments=[_assignment()])

    assignment = _get(api).json()["assignments"][0]

    assert assignment == {
        "assignment_id": 501,
        "requirement_id": 11,
        "event_id": 31,
        "membership_id": 41,
        "person_id": 51,
        "person_display_name": "Bea Volunteer",
        "role_id": 21,
        "role_name": "Setup Lead",
        "is_override": False,
        "override_reason": None,
    }


def test_24_an_override_shows_its_flag_and_reason(api, session, service):
    """A reviewer approving a schedule has to be able to read why a placement
    was forced.
    """
    service.behaviour["result"] = _detail(
        assignments=[
            _assignment(is_override=True, override_reason="Lead asked for cover")
        ]
    )

    assignment = _get(api).json()["assignments"][0]

    assert assignment["is_override"] is True
    assert assignment["override_reason"] == "Lead asked for cover"


def test_25_assignments_keep_the_services_order(api, session, service):
    service.behaviour["result"] = _detail(
        assignments=[
            _assignment(assignment_id=501, person_display_name="Ann"),
            _assignment(assignment_id=502, person_display_name="Bob"),
        ]
    )

    body = _get(api).json()["assignments"]

    assert [a["assignment_id"] for a in body] == [501, 502]


def test_25b_the_assignment_query_orders_by_snapshot_date_then_person():
    from sqlalchemy.dialects import postgresql

    sql = str(
        detail_service._assignments_statement(1).compile(dialect=postgresql.dialect())
    )
    order_by = sql.split("ORDER BY")[1]

    assert order_by.index("event_date") < order_by.index("person.display_name")
    assert order_by.rstrip().endswith("assignment.id")


def test_26_a_version_with_no_assignments_is_supported(api, session, service):
    service.behaviour["result"] = _detail(requirements=[_requirement(assigned_count=0)])

    body = _get(api).json()

    assert body["assignments"] == []
    assert body["summary"]["assigned_positions"] == 0


def test_27_no_membership_qualification_or_audit_internals_leak(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[_requirement()],
        assignments=[_assignment(is_override=True, override_reason="cover")],
        issues=[FinalizationIssue(code="X", message="y")],
    )

    body = _get(api).json()
    text = _get(api).text

    # The assignment carries exactly the review fields and nothing else -- in
    # particular no membership ``notes``, which the version legitimately has a
    # field of its own for.
    assert set(body["assignments"][0]) == {
        "assignment_id",
        "requirement_id",
        "event_id",
        "membership_id",
        "person_id",
        "person_display_name",
        "role_id",
        "role_name",
        "is_override",
        "override_reason",
    }

    for forbidden in (
        "qualification",
        "is_qualified",
        "google",
        "subject",
        "before_values",
        "after_values",
        "overridden_blockers",
        "audit",
        "church_id",
        "email",
        "phone",
        "joined_on",
        "deactivated_at",
    ):
        assert forbidden not in text.lower(), forbidden


def test_27b_the_response_models_forbid_unexpected_fields():
    """``extra="forbid"`` everywhere, so a field cannot arrive by accident."""
    import app.api.schedule_version_schemas as schemas

    for name in schemas.__all__:
        model = getattr(schemas, name)
        assert model.model_config.get("extra") == "forbid", name
        assert model.model_config.get("from_attributes") is not True, name


# ==========================================================================
# Summary (28-32)
# ==========================================================================


def test_28_required_positions_sums_the_snapshot(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[
            _requirement(requirement_id=11, required_count=2, assigned_count=0),
            _requirement(requirement_id=12, required_count=3, assigned_count=0),
        ]
    )

    assert _get(api).json()["summary"]["required_positions"] == 5


def test_29_assigned_positions_counts_actual_assignment_rows(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[_requirement(required_count=2, assigned_count=2)],
        assignments=[_assignment(assignment_id=501), _assignment(assignment_id=502)],
    )

    assert _get(api).json()["summary"]["assigned_positions"] == 2


def test_30_unfilled_positions_is_the_shortfall(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[
            _requirement(requirement_id=11, required_count=3, assigned_count=1),
            _requirement(requirement_id=12, required_count=2, assigned_count=2),
        ]
    )

    summary = _get(api).json()["summary"]

    assert summary["unfilled_positions"] == 2
    assert summary["is_fully_staffed"] is False


def test_31_an_overfill_does_not_create_a_negative_shortfall(api, session, service):
    """An authorized capacity override means more assignments than required.
    Summed naively across the version, that overfill would cancel out a real
    gap and report a schedule as fully staffed when a Sunday is empty.
    """
    service.behaviour["result"] = _detail(
        requirements=[
            _requirement(requirement_id=11, required_count=1, assigned_count=3),
            _requirement(requirement_id=12, required_count=2, assigned_count=0),
        ]
    )

    summary = _get(api).json()["summary"]

    assert summary["unfilled_positions"] == 2
    assert summary["is_fully_staffed"] is False


def test_31b_the_overfilled_count_is_reported_unclamped(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[_requirement(required_count=1, assigned_count=3)]
    )

    assert _get(api).json()["requirements"][0]["assigned_count"] == 3


def test_32_a_fully_staffed_version_says_so(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[
            _requirement(requirement_id=11, required_count=2, assigned_count=2),
            _requirement(requirement_id=12, required_count=1, assigned_count=1),
        ]
    )

    summary = _get(api).json()["summary"]

    assert summary["unfilled_positions"] == 0
    assert summary["is_fully_staffed"] is True


def test_32b_an_overfilled_but_covered_version_is_fully_staffed(api, session, service):
    service.behaviour["result"] = _detail(
        requirements=[_requirement(required_count=1, assigned_count=2)]
    )

    assert _get(api).json()["summary"]["is_fully_staffed"] is True


# ==========================================================================
# Staleness (33-36)
# ==========================================================================


def test_33_the_real_staleness_query_runs_on_the_request_session():
    """Task 23 is reached *through* Task 26, which computes it and carries the
    result whole -- so one call answers both questions and the two halves of
    the response cannot disagree with each other.

    That makes the chain: request Session -> Task 26 (proven by ``test_37``)
    -> Task 23. This test pins the second link, structurally: Task 26 calls the
    genuine ``get_schedule_version_staleness`` and hands it the very Session it
    was given, rather than one of its own. The chain is also exercised for real
    against PostgreSQL in the Task 38 integration suite (test C).
    """
    import app.services.finalization_readiness as readiness_module
    import app.services.schedule_staleness as staleness_module

    # It is the real function, imported, not a local reimplementation.
    assert (
        readiness_module.get_schedule_version_staleness
        is staleness_module.get_schedule_version_staleness
    )

    tree = ast.parse(Path(readiness_module.__file__).read_text())
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "get_schedule_version_staleness"
    )
    # get_schedule_version_staleness(session, version=version)
    assert [arg.id for arg in call.args] == ["session"]
    assert [kw.arg for kw in call.keywords] == ["version"]

    # And the API reads its staleness off that same result rather than calling
    # Task 23 a second time, which could return something different.
    assert "detail.readiness.staleness" in Path(routes.__file__).read_text()


def test_34_a_fresh_result_is_serialized(api, session, service):
    fingerprint = _fingerprint()
    service.behaviour["result"] = _detail(
        staleness=_staleness(current=[fingerprint], snapshot=[fingerprint])
    )

    staleness = _get(api).json()["staleness"]

    assert staleness == {
        "is_stale": False,
        "current_only": [],
        "snapshot_only": [],
    }


def test_35_the_two_differences_are_serialized_as_values(api, session, service):
    """Mapped field by field, never a Python repr of a frozenset."""
    now_required = _fingerprint(required_count=3)
    was_required = _fingerprint(required_count=2)
    service.behaviour["result"] = _detail(
        staleness=_staleness(current=[now_required], snapshot=[was_required])
    )

    staleness = _get(api).json()["staleness"]

    assert staleness["is_stale"] is True
    assert staleness["current_only"] == [
        {
            "event_id": 31,
            "event_date": NOV_15.isoformat(),
            "ministry_role_id": 21,
            "required_count": 3,
        }
    ]
    assert staleness["snapshot_only"] == [
        {
            "event_id": 31,
            "event_date": NOV_15.isoformat(),
            "ministry_role_id": 21,
            "required_count": 2,
        }
    ]
    assert "frozenset" not in _get(api).text


def test_35b_the_differences_come_back_in_a_stable_order(api, session, service):
    """Sets have no order; a JSON array does. Two reads of the same state must
    produce the same document.
    """
    service.behaviour["result"] = _detail(
        staleness=_staleness(
            current=[
                _fingerprint(event_id=32, event_date=NOV_22, role_id=21),
                _fingerprint(event_id=31, event_date=NOV_15, role_id=22),
                _fingerprint(event_id=31, event_date=NOV_15, role_id=21),
            ],
            snapshot=[],
        )
    )

    first = _get(api).json()["staleness"]["current_only"]
    second = _get(api).json()["staleness"]["current_only"]

    assert first == second
    assert [(f["event_date"], f["event_id"], f["ministry_role_id"]) for f in first] == [
        (NOV_15.isoformat(), 31, 21),
        (NOV_15.isoformat(), 31, 22),
        (NOV_22.isoformat(), 32, 21),
    ]


def test_36_a_stale_version_is_still_200(api, session, service):
    """Drift is the answer the caller asked for, not a failure to serve them."""
    service.behaviour["result"] = _detail(
        staleness=_staleness(current=[_fingerprint()], snapshot=[])
    )

    response = _get(api)

    assert response.status_code == 200
    assert response.json()["staleness"]["is_stale"] is True


# ==========================================================================
# Readiness (37-41)
# ==========================================================================


def test_37_the_real_readiness_service_runs_on_the_request_session(monkeypatch):
    seen: list = []

    def spy(session, *, version):
        seen.append((session, version))
        return FinalizationReadinessResult(staleness=_staleness(), issues=())

    monkeypatch.setattr(detail_service, "get_finalization_readiness", spy)
    monkeypatch.setattr(
        detail_service, "require_ministry_reader", lambda actor, *, ministry_id: None
    )
    monkeypatch.setattr(
        detail_service,
        "_resolve_period_and_ministry",
        lambda s, v: (_StubPeriod(), _StubMinistry()),
    )
    monkeypatch.setattr(detail_service, "_load_requirements", lambda s, **k: ())
    monkeypatch.setattr(detail_service, "_load_assignments", lambda s, **k: ())

    session = RecordingSession()
    version = _StubVersion()
    detail_service.get_schedule_version_detail(
        session, actor=_StubPerson(), version=version
    )

    assert len(seen) == 1
    assert seen[0][0] is session
    assert seen[0][1] is version


def test_37b_the_service_imports_the_genuine_readiness_function():
    from app.services import finalization_readiness

    assert _REAL_READINESS is finalization_readiness.get_finalization_readiness


def test_38_a_ready_result_is_serialized(api, session, service):
    readiness = _get(api).json()["finalization_readiness"]

    assert readiness == {"is_ready": True, "issues": []}


def test_39_issue_codes_messages_and_ids_are_preserved(api, session, service):
    service.behaviour["result"] = _detail(
        staleness=_staleness(current=[_fingerprint()], snapshot=[]),
        issues=[
            FinalizationIssue(
                code="STALE_REQUIREMENT_SNAPSHOT",
                message="the requirement snapshot no longer matches",
            ),
            FinalizationIssue(
                code="UNFILLED_REQUIREMENT",
                message="requirement 11 needs 2 and has 1",
                schedule_version_requirement_id=11,
            ),
            FinalizationIssue(
                code="INACTIVE_MEMBERSHIP",
                message="assignment 501 names an inactive membership",
                assignment_id=501,
            ),
        ],
    )

    readiness = _get(api).json()["finalization_readiness"]

    assert readiness["is_ready"] is False
    assert readiness["issues"] == [
        {
            "code": "STALE_REQUIREMENT_SNAPSHOT",
            "message": "the requirement snapshot no longer matches",
            "assignment_id": None,
            "schedule_version_requirement_id": None,
        },
        {
            "code": "UNFILLED_REQUIREMENT",
            "message": "requirement 11 needs 2 and has 1",
            "assignment_id": None,
            "schedule_version_requirement_id": 11,
        },
        {
            "code": "INACTIVE_MEMBERSHIP",
            "message": "assignment 501 names an inactive membership",
            "assignment_id": 501,
            "schedule_version_requirement_id": None,
        },
    ]


def test_40_an_unready_version_is_still_200(api, session, service):
    service.behaviour["result"] = _detail(
        issues=[FinalizationIssue(code="UNFILLED_REQUIREMENT", message="short")]
    )

    response = _get(api)

    assert response.status_code == 200
    assert response.json()["finalization_readiness"]["is_ready"] is False


def test_41_the_api_invents_no_readiness_rules():
    """No issue codes and no readiness vocabulary are defined in the API layer:
    the verdict and its reasons come from Task 26 or they do not exist.
    """
    for module_path in (
        "app/api/routes_schedule_versions.py",
        "app/api/schedule_version_schemas.py",
    ):
        tree = ast.parse(Path(module_path).read_text())
        docstrings = {
            ast.get_docstring(node, clean=False)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
        }
        literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        for code in (
            "UNFILLED_REQUIREMENT",
            "STALE_REQUIREMENT_SNAPSHOT",
            "INACTIVE_MEMBERSHIP",
            "UNAUTHORIZED_BLOCKER",
            "UNAUTHORIZED_OVERFILL",
        ):
            assert code not in (literals - docstrings), (module_path, code)

    # is_ready is read from the domain result, never recomputed here.
    route_source = Path(routes.__file__).read_text()
    assert "is_ready=detail.readiness.is_ready" in route_source


def test_41b_readiness_is_reported_for_every_status(api, session, service):
    """Task 26 is status-agnostic by design, so a head can check a draft before
    submitting it. The contract documents that ready != finalizable.
    """
    for status_value in (
        SCHEDULE_VERSION_STATUS_DRAFT,
        SCHEDULE_VERSION_STATUS_REVIEW,
        SCHEDULE_VERSION_STATUS_FINALIZED,
    ):
        service.behaviour["result"] = _detail(
            version=_StubVersion(status=status_value)
        )
        body = _get(api).json()
        assert body["schedule_version"]["status"] == status_value
        assert "is_ready" in body["finalization_readiness"]

    import app.api.schedule_version_schemas as schemas

    doc = schemas.FinalizationReadiness.__doc__
    assert "does **not** mean the version may be finalized right now" in doc


# ==========================================================================
# Read-only and session behaviour (42-46)
# ==========================================================================


def test_42_the_detail_implementation_never_writes():
    """AST over both the service and the route: no ``add``, ``delete``,
    ``flush``, ``merge``, ``commit`` or ``rollback`` anywhere.
    """
    for module_path in (
        "app/services/schedule_version_detail.py",
        "app/api/routes_schedule_versions.py",
    ):
        calls = [
            node.func.attr
            for node in ast.walk(ast.parse(Path(module_path).read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        for forbidden in (
            "add",
            "add_all",
            "delete",
            "flush",
            "merge",
            "commit",
            "rollback",
        ):
            assert forbidden not in calls, (module_path, forbidden)


def test_42b_the_recording_session_offers_no_write_methods(api, session, service):
    _get(api)

    for forbidden in ("add", "flush", "delete", "merge"):
        assert not hasattr(session, forbidden)


def test_43_the_read_creates_no_audit_event():
    tree = ast.parse(Path(detail_service.__file__).read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]

    assert "record_audit_event" not in imported
    assert not any(name.startswith("ACTION_") for name in imported)
    assert not hasattr(detail_service, "record_audit_event")
    assert not hasattr(detail_service, "AuditEvent")


def test_44_the_read_service_can_change_nothing():
    """The *service* behind the read imports no operation that could change
    anything -- not a transition, not a successor, not an assignment.

    **Task 80 split this test in two.** The route module now genuinely does
    import the two lifecycle transitions, because it serves them on their own
    paths; what must stay true is that the service this read calls cannot
    reach them, and that the GET handler does not. The second half is
    :func:`test_44c_the_read_handler_calls_no_transition`.
    """
    tree = ast.parse(Path("app/services/schedule_version_detail.py").read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    for operation in (
        "submit_schedule_version_for_review",
        "finalize_schedule_version",
        "create_successor_schedule_version",
        "assign_member",
        "remove_assignment",
        "carry_forward_assignment",
        "generate_draft_schedule",
    ):
        assert operation not in imported, operation


def test_44c_the_read_handler_calls_no_transition():
    """``read_schedule_version`` names no mutating operation in its own body.

    Parsed rather than searched: the module beside it legitimately imports
    both transitions for the two POST handlers, so "the name does not appear
    in the file" stopped being the question. "The GET handler does not call
    it" is.
    """
    tree = ast.parse(Path("app/api/routes_schedule_versions.py").read_text())
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "read_schedule_version"
    )
    called = {
        node.func.id
        for node in ast.walk(handler)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for operation in (
        "submit_schedule_version_for_review",
        "finalize_schedule_version",
        "generate_draft_schedule",
    ):
        assert operation not in called, operation


def test_44b_the_version_status_is_reported_never_changed(api, session, service):
    version = _StubVersion(status=SCHEDULE_VERSION_STATUS_DRAFT)
    service.behaviour["result"] = _detail(version=version)

    body = _get(api).json()

    assert body["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_DRAFT
    assert version.status == SCHEDULE_VERSION_STATUS_DRAFT
    assert version.finalized_at is None


def test_45_one_session_serves_the_whole_request(api, session, service):
    _get(api)

    assert service[0]["session"] is session
    assert service[0]["actor"] is session.person
    assert service[0]["version"] is session.version


def test_45b_no_second_session_is_opened(api, monkeypatch, service):
    created: list[RecordingSession] = []

    def factory():
        created.append(RecordingSession())
        return created[-1]

    monkeypatch.setattr(deps, "SessionLocal", factory)

    _get(api)

    assert len(created) == 1


def test_46_the_request_closes_having_changed_nothing(api, session, service):
    """The Task 37 boundary still owns the transaction. A read commits a
    transaction that wrote nothing, which is the same single policy every
    request follows.
    """
    _get(api)

    assert session.events == ["commit", "close"]


def test_46b_a_failed_read_rolls_back_and_closes(api, session, service):
    service.behaviour["raises"] = AuthorizationError("no")

    _get(api)

    assert session.events == ["rollback", "close"]


def test_46c_the_route_adds_no_transaction_handling_of_its_own():
    source = Path(routes.__file__).read_text()
    tree = ast.parse(source)
    read_route = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "read_schedule_version"
    )

    assert not any(isinstance(node, ast.Try) for node in ast.walk(read_route))


# ==========================================================================
# Structural (47-50)
# ==========================================================================


def test_47_no_actor_or_ministry_id_is_accepted_from_the_request(api, session, service):
    import inspect

    parameters = inspect.signature(routes.read_schedule_version).parameters

    for forbidden in ("actor_person_id", "ministry_id", "person_id"):
        assert forbidden not in parameters

    # Nor through the OpenAPI contract.
    spec = api.get("/openapi.json").json()
    operation = spec["paths"]["/api/v1/schedule-versions/{schedule_version_id}"]["get"]
    names = {p["name"] for p in operation.get("parameters", [])}
    assert names <= {"schedule_version_id", "X-Dev-Actor-Person-Id"}


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

def test_48_no_cors_middleware_was_added():
    assert _installed_middleware_names(app) == {"SessionMiddleware"}

    tree = ast.parse(Path("app/main.py").read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert "CORSMiddleware" not in imported


def test_49_no_orm_object_is_serialized_directly():
    """The response models never set ``from_attributes``, and the route builds
    every field by name.
    """
    import app.api.schedule_version_schemas as schemas

    for name in schemas.__all__:
        assert getattr(schemas, name).model_config.get("from_attributes") is not True

    tree = ast.parse(Path(routes.__file__).read_text())
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    for forbidden in ("model_validate", "from_orm"):
        assert forbidden not in calls


def test_50_no_model_or_migration_change_was_needed():
    # Ten since Task 79 added ``person.church_membership_status``; this
    # read-only detail slice still adds none of its own.
    assert len(list(Path("alembic/versions").glob("*.py"))) == 10

    for module_path in (
        "app/services/schedule_version_detail.py",
        "app/api/schedule_version_schemas.py",
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


def test_50b_the_route_module_reaches_the_genuine_detail_service():
    from app.services import schedule_version_detail

    assert _REAL_DETAIL is schedule_version_detail.get_schedule_version_detail


# ==========================================================================
# The read service itself
#
# The route tests above stub the service, because what they check is the HTTP
# contract. These call it directly, so its authorization, its SQL and its
# mapping are covered rather than assumed.
# ==========================================================================


class _RowSession:
    """A Session stand-in for the service: returns configured rows per table.

    Like the request stand-in, it has no write methods at all.
    """

    def __init__(self, *, period_row=None, requirement_rows=(), assignment_rows=()):
        self.period_row = period_row
        self.requirement_rows = list(requirement_rows)
        self.assignment_rows = list(assignment_rows)
        self.statements: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.statements.append(sql)
        # Dispatched on the first selected column, not on "FROM x": the
        # requirements statement counts assignments in a correlated subquery
        # and so legitimately mentions both tables.
        if "FROM scheduling_period" in sql:
            return _OneResult(self.period_row)
        if sql.startswith("SELECT schedule_version_requirement.id"):
            return _AllResult(self.requirement_rows)
        if sql.startswith("SELECT assignment.id"):
            return _AllResult(self.assignment_rows)
        raise AssertionError(f"unexpected query in the read service: {sql}")


class _OneResult:
    def __init__(self, value) -> None:
        self._value = value

    def one_or_none(self):
        return self._value


class _AllResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Row:
    """A stand-in for one result row, addressed by attribute like the real one."""

    def __init__(self, **fields) -> None:
        for key, value in fields.items():
            setattr(self, key, value)


def _requirement_row(**overrides):
    fields = dict(
        id=11,
        event_id=31,
        event_date=NOV_15,
        ministry_role_id=21,
        required_count=2,
        event_name="Sunday Service",
        event_kind="SUNDAY",
        role_name="Setup Lead",
        role_display_order=1,
        assigned_count=1,
    )
    fields.update(overrides)
    return _Row(**fields)


class _Membership:
    def __init__(self, *, ministry_id: int, is_head: bool = True, deactivated=None):
        self.ministry_id = ministry_id
        self.is_ministry_head = is_head
        self.deactivated_at = deactivated


@pytest.fixture
def stubbed_readiness(monkeypatch) -> FinalizationReadinessResult:
    """Stand in for Task 26 in the service-level tests.

    Task 26 has a suite of its own, and it issues many reads; the tests below
    are about this service's authorization, its SQL and its mapping. That it
    calls the genuine Task 26 with the request Session is proven separately by
    ``test_37``, and end to end against PostgreSQL by the integration suite.
    """
    result = FinalizationReadinessResult(staleness=_staleness(), issues=())
    monkeypatch.setattr(
        detail_service, "get_finalization_readiness", lambda session, *, version: result
    )
    return result


def _service_session(**kwargs) -> _RowSession:
    return _RowSession(period_row=(_StubPeriod(), _StubMinistry()), **kwargs)


def _call_service(session, actor, version=None):
    return detail_service.get_schedule_version_detail(
        session, actor=actor, version=version or _StubVersion()
    )


# -- authorization, through the real shared rule ---------------------------


def test_51_the_service_refuses_a_person_who_manages_nothing(stubbed_readiness):
    """The real ``require_ministry_manager``, not a stub: an ordinary member
    gets an AuthorizationError before any content is read.
    """
    actor = _StubPerson()  # not admin, no memberships
    session = _service_session()

    with pytest.raises(AuthorizationError):
        _call_service(session, actor)

    # And it refused *before* reading the version's contents.
    assert not any("schedule_version_requirement" in s for s in session.statements)
    assert not any("FROM assignment" in s for s in session.statements)


def test_51b_the_service_refuses_a_head_of_a_different_ministry(stubbed_readiness):
    actor = _StubPerson()
    actor.ministry_memberships = [_Membership(ministry_id=901)]  # not 900

    with pytest.raises(AuthorizationError):
        _call_service(_service_session(), actor)


def test_51c_the_service_allows_a_head_of_this_ministry(stubbed_readiness):
    actor = _StubPerson()
    actor.ministry_memberships = [_Membership(ministry_id=900)]

    detail = _call_service(_service_session(), actor)

    assert detail.ministry.id == 900


def test_51d_the_service_allows_an_active_admin(stubbed_readiness):
    actor = _StubPerson()
    actor.is_admin = True

    assert _call_service(_service_session(), actor).period.id == 700


def test_51e_a_deactivated_head_is_refused(stubbed_readiness):
    actor = _StubPerson()
    actor.ministry_memberships = [
        _Membership(ministry_id=900, deactivated=datetime.datetime(2026, 1, 1, tzinfo=UTC))
    ]

    with pytest.raises(AuthorizationError):
        _call_service(_service_session(), actor)


# -- the SQL reads the snapshot, not current configuration -----------------


def test_52_the_requirement_select_list_takes_dates_from_the_snapshot():
    """The authoritative scheduling facts must come from
    ``schedule_version_requirement``. Reading ``event.event_date`` here would
    silently rewrite history to match today's configuration -- exactly what
    staleness exists to report instead.
    """
    from sqlalchemy.dialects import postgresql

    sql = str(
        detail_service._requirements_statement(1).compile(dialect=postgresql.dialect())
    )
    select_list = sql.split("FROM")[0]

    assert "schedule_version_requirement.event_date" in select_list
    assert "schedule_version_requirement.required_count" in select_list
    assert "schedule_version_requirement.ministry_role_id" in select_list
    # Today's rows contribute labels only.
    assert "event.event_date" not in select_list
    assert "staffing_requirement" not in sql
    assert "event.name" in select_list
    assert "ministry_role.name" in select_list


def test_52b_label_tables_are_outer_joined_so_a_deleted_row_hides_nothing():
    from sqlalchemy.dialects import postgresql

    sql = str(
        detail_service._requirements_statement(1).compile(dialect=postgresql.dialect())
    )

    assert "LEFT OUTER JOIN event" in sql
    assert "LEFT OUTER JOIN ministry_role" in sql


def test_52c_the_assignment_query_never_consults_current_staffing():
    from sqlalchemy.dialects import postgresql

    sql = str(
        detail_service._assignments_statement(1).compile(dialect=postgresql.dialect())
    )

    assert "staffing_requirement" not in sql
    assert "schedule_version_requirement.event_date" in sql


def test_52d_assigned_counts_come_from_one_subquery_not_a_query_per_row():
    """The N+1 this endpoint would otherwise obviously have."""
    from sqlalchemy.dialects import postgresql

    sql = str(
        detail_service._requirements_statement(1).compile(dialect=postgresql.dialect())
    )

    assert sql.count("SELECT count(assignment.id)") == 1
    assert "FROM assignment" in sql


# -- mapping ---------------------------------------------------------------


def test_53_requirement_rows_are_mapped_field_for_field(stubbed_readiness):
    session = _service_session(requirement_rows=[_requirement_row()])

    detail = _call_service(session, _admin())

    requirement = detail.requirements[0]
    assert requirement.requirement_id == 11
    assert requirement.event_id == 31
    assert requirement.event_date == NOV_15
    assert requirement.event_name == "Sunday Service"
    assert requirement.event_kind == "SUNDAY"
    assert requirement.role_id == 21
    assert requirement.role_name == "Setup Lead"
    assert requirement.required_count == 2
    assert requirement.assigned_count == 1


def test_53b_an_overfilled_requirement_keeps_its_real_count(stubbed_readiness):
    """Not clamped to ``required_count``. Hiding an overfill would hide the
    thing a reviewer most needs to see, and would make the summary disagree
    with the assignment list.
    """
    session = _service_session(
        requirement_rows=[_requirement_row(required_count=1, assigned_count=3)]
    )

    detail = _call_service(session, _admin())

    assert detail.requirements[0].assigned_count == 3
    assert detail.requirements[0].required_count == 1
    assert detail.unfilled_positions == 0
    assert detail.is_fully_staffed is True


def test_53c_a_shortfall_and_an_overfill_do_not_cancel_out(stubbed_readiness):
    session = _service_session(
        requirement_rows=[
            _requirement_row(id=11, required_count=1, assigned_count=4),
            _requirement_row(id=12, event_id=32, required_count=3, assigned_count=0),
        ]
    )

    detail = _call_service(session, _admin())

    assert detail.required_positions == 4
    assert detail.unfilled_positions == 3
    assert detail.is_fully_staffed is False


def _admin() -> _StubPerson:
    actor = _StubPerson()
    actor.is_admin = True
    return actor


def test_54_the_service_reads_and_never_writes(stubbed_readiness):
    """The stand-in Session has no write methods, so a service that reached for
    one would raise. This asserts it completes without doing so.
    """
    session = _service_session(requirement_rows=[_requirement_row()])

    detail = _call_service(session, _admin())

    assert detail.assigned_positions == 0
    for forbidden in ("add", "flush", "delete", "merge", "commit", "rollback"):
        assert not hasattr(session, forbidden)


def test_54b_the_service_opens_no_session_of_its_own():
    tree = ast.parse(Path(detail_service.__file__).read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]

    assert "SessionLocal" not in imported
    assert not hasattr(detail_service, "SessionLocal")


def test_55_an_unpersisted_version_is_refused_rather_than_reported_empty(stubbed_readiness):
    from app.services.errors import InvalidOperationError

    with pytest.raises(InvalidOperationError):
        _call_service(_service_session(), _admin(), version=_StubVersion(id=None))

    with pytest.raises(InvalidOperationError):
        _call_service(
            _service_session(), _admin(), version=_StubVersion(scheduling_period_id=None)
        )


def test_55b_an_unresolvable_period_is_refused(stubbed_readiness):
    from app.services.errors import InvalidOperationError

    session = _RowSession(period_row=None)

    with pytest.raises(InvalidOperationError):
        _call_service(session, _admin())
