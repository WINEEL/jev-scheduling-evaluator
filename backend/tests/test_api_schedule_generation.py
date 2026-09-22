"""The mutating HTTP boundary: one request, one transaction (Task 37).

Task 36 answered *who is asking*. This suite answers the other half: **what
owns COMMIT and ROLLBACK for an HTTP mutation**, proven through the first
endpoint that actually changes anything.

The thing under test is mostly not the scheduling -- that has its own suites --
but the boundary around it: that one Session serves the whole request, that it
commits exactly once when the request succeeds, that it rolls back exactly once
and re-raises when anything fails, that it always closes, and that a failed
commit is never reported as a success.

Offline: no database. ``SessionLocal`` is replaced by a recording stand-in, so
the *real* ``get_session`` runs and its lifecycle is observable. The service is
replaced by a stub in most tests, because what is being checked here is the
translation and the transaction, not the scheduling.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api import routes_schedule_versions as routes
from app.api import schemas
from app.config import get_settings
from app.main import app
from app.scheduling.result import (
    ProposedAssignment,
    SchedulingResult,
    SolutionMetrics,
    UnfilledRequirement,
)
from app.scheduling.solver import (
    SchedulingEngineError,
    SchedulingInputError,
    SchedulingPolicy,
)
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.schedule_generation import DraftGenerationResult

DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
URL = "/api/v1/schedule-versions/{id}/generate"

#: Captured at import, before any test can monkeypatch the module attribute --
#: otherwise a stub installed by one test would be wrapped by the next.
_REAL_GENERATE = routes.generate_draft_schedule


# --------------------------------------------------------------------------
# Stand-ins
# --------------------------------------------------------------------------


class _StubPerson:
    def __init__(self, id: int = 1) -> None:
        self.id = id
        self.display_name = "Ada Head"
        self.is_admin = False
        self.deactivated_at = None


class _StubVersion:
    def __init__(self, id: int = 7) -> None:
        self.id = id


class _StubAssignment:
    """Only the four columns the response is built from, plus a field that

    must never appear in the response -- so a DTO that started serializing the
    ORM row instead of naming its fields would be caught.
    """

    def __init__(self, id: int, requirement_id: int, membership_id: int, event_id: int):
        self.id = id
        self.schedule_version_requirement_id = requirement_id
        self.ministry_membership_id = membership_id
        self.event_id = event_id
        self.override_reason = "SECRET-INTERNAL-FIELD"


class _CallLog(list):
    """A call log that also carries the stub's configured behaviour, so a test
    reads ``service[0]`` and writes ``service.behaviour[...]``.
    """

    behaviour: dict


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowResult:
    def __init__(self, rows) -> None:
        self._rows = rows

    def all(self):
        return list(self._rows)


class RecordingSession:
    """A stand-in request Session that records its transaction boundary.

    It answers the two reads the request genuinely makes -- the actor lookup
    and the schedule-version lookup -- and records every boundary call in
    ``events`` so *ordering* is checkable, not just counts.
    """

    def __init__(self, *, person=None, version=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.version = _StubVersion() if version is None else version
        self.events: list[str] = []
        self.person_lookups = 0
        self.version_lookups = 0

    def execute(self, statement):
        sql = str(statement)
        if "FROM person" in sql:
            self.person_lookups += 1
            return _ScalarResult(self.person)
        if "FROM schedule_version" in sql:
            self.version_lookups += 1
            return _ScalarResult(self.version)
        if "FROM ministry" in sql:
            # ``/me`` still runs through this boundary; it heads nothing here.
            return _RowResult([])
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")

    # Convenience views used by the assertions.
    @property
    def commits(self) -> int:
        return self.events.count("commit")

    @property
    def rollbacks(self) -> int:
        return self.events.count("rollback")

    @property
    def closed(self) -> bool:
        return "close" in self.events


class CommitFailingSession(RecordingSession):
    """A Session whose commit fails -- a deferred constraint, a lost
    connection. The request must not be reported as a success.
    """

    class CommitFailed(Exception):
        pass

    def commit(self) -> None:
        self.events.append("commit")
        raise self.CommitFailed("could not commit")


def _result(
    *,
    assignments=(),
    unfilled=(),
    metrics: SolutionMetrics | None = None,
    proposals=(),
) -> DraftGenerationResult:
    return DraftGenerationResult(
        scheduling_result=SchedulingResult(
            proposed_assignments=tuple(proposals),
            unfilled_requirements=tuple(unfilled),
            metrics=metrics or SolutionMetrics(),
        ),
        created_assignments=tuple(assignments),
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def session(monkeypatch) -> RecordingSession:
    """Install a recording Session behind the **real** ``get_session``.

    Nothing about the dependency is overridden -- only the factory it calls --
    so the commit/rollback/close logic under test is the shipped one.
    """
    recording = RecordingSession()
    monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
    return recording


@pytest.fixture
def service(monkeypatch) -> _CallLog:
    """Replace the generation service with a stub that records its arguments.

    Returns the call log. Tests set ``calls.result`` or ``calls.raises`` on the
    returned list via the helpers below.
    """
    calls = _CallLog()
    behaviour: dict = {"result": _result(), "raises": None}

    def stub(session, *, actor, version, policy):
        calls.append(
            {
                "session": session,
                "actor": actor,
                "version": version,
                "policy": policy,
                # What the boundary had done by the time the service ran.
                "events_so_far": list(getattr(session, "events", [])),
            }
        )
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        return behaviour["result"]

    monkeypatch.setattr(routes, "generate_draft_schedule", stub)
    calls.behaviour = behaviour
    return calls


@pytest.fixture
def api(monkeypatch) -> TestClient:
    """A client with development auth enabled and nothing else overridden."""
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _post(api: TestClient, *, version_id: int = 7, actor_id: int = 1, **kwargs):
    return api.post(
        URL.format(id=version_id), headers={HEADER: str(actor_id)}, **kwargs
    )


# ==========================================================================
# 1 -- the request Session lifecycle
# ==========================================================================


def test_01_a_successful_request_commits_exactly_once_and_closes(api, session, service):
    response = _post(api)

    assert response.status_code == 200
    assert session.events == ["commit", "close"]


def test_01b_the_commit_happens_after_the_service_has_run(api, session, service):
    """Ordering, not just counting: a boundary that committed before the
    operation would report work that had not happened yet.
    """
    _post(api)

    assert service[0]["events_so_far"] == []
    assert session.events == ["commit", "close"]


def test_02_a_failing_request_rolls_back_exactly_once_and_closes(api, session, service):
    service.behaviour["raises"] = InvalidOperationError("not a draft")

    response = _post(api)

    assert response.status_code == 409
    assert session.events == ["rollback", "close"]


def test_02b_a_failed_request_never_commits(api, session, service):
    service.behaviour["raises"] = AuthorizationError("no")

    _post(api)

    assert session.commits == 0


def test_02c_an_unexpected_error_also_rolls_back_and_still_propagates(
    api, session, service
):
    """A bug in the service must not leave a half-written transaction to be
    committed by whatever runs next on that connection.
    """
    service.behaviour["raises"] = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _post(api)

    assert session.events == ["rollback", "close"]


def test_03_the_session_is_closed_even_when_the_rollback_path_ran(
    api, session, service
):
    service.behaviour["raises"] = InvalidOperationError("no")

    _post(api)

    assert session.closed is True


def test_04_a_failed_commit_is_not_reported_as_a_success(api, monkeypatch, service):
    """The load-bearing case. The endpoint returned a perfectly good response
    object; the commit then failed. The client must learn the writes did not
    land, not receive a 200 describing them.
    """
    failing = CommitFailingSession()
    monkeypatch.setattr(deps, "SessionLocal", lambda: failing)

    with pytest.raises(CommitFailingSession.CommitFailed):
        _post(api)

    assert failing.events == ["commit", "rollback", "close"]


def test_05_one_session_serves_the_whole_request(api, session, service):
    """The actor lookup, the version lookup and the service all ran on the
    same object -- FastAPI caches the dependency, so there is one unit of work
    and one transactional view, not three.
    """
    _post(api)

    assert session.person_lookups == 1
    assert session.version_lookups == 1
    assert service[0]["session"] is session


def test_05b_no_second_session_is_ever_created(api, monkeypatch, service):
    created: list[RecordingSession] = []

    def factory():
        created.append(RecordingSession())
        return created[-1]

    monkeypatch.setattr(deps, "SessionLocal", factory)

    _post(api)

    assert len(created) == 1


def test_05c_authentication_does_not_open_a_session_of_its_own(api, session, service):
    """A separate authentication Session would put the actor lookup in a
    different transaction from the work done on that actor's behalf.
    """
    _post(api)

    assert service[0]["actor"] is session.person


def test_06_a_read_only_request_ends_the_same_way(api, session):
    """``/me`` goes through the same boundary. Committing a transaction that
    changed nothing costs nothing and keeps one policy rather than two.
    """
    response = api.get("/api/v1/me", headers={HEADER: "1"})

    assert response.status_code == 200
    assert session.events == ["commit", "close"]


def test_06b_an_unauthenticated_request_rolls_back_and_closes(api, session):
    """The failure happens inside a dependency, before the route body. The
    boundary still has to clean up.
    """
    response = api.post(URL.format(id=7), json={})

    assert response.status_code == 401
    assert session.events == ["rollback", "close"]


# ==========================================================================
# 2 -- the existing API still behaves
# ==========================================================================


def test_07_health_is_independent_of_the_database(api, monkeypatch):
    def explode():
        raise AssertionError("/health must not open a Session")

    monkeypatch.setattr(deps, "SessionLocal", explode)

    response = api.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_08_me_still_returns_the_actor_unchanged(api, session):
    body = api.get("/api/v1/me", headers={HEADER: "1"}).json()

    assert body == {
        "person_id": 1,
        "display_name": "Ada Head",
        "is_admin": False,
        "headed_ministries": [],
    }


# ==========================================================================
# 3 -- resolving the resource, and who is asking
# ==========================================================================


def test_09_a_version_that_does_not_exist_is_404(api, session, service):
    session.version = None

    response = _post(api)

    assert response.status_code == 404
    assert response.json() == {"detail": "Schedule version not found."}
    assert service == []


def test_09b_a_missing_version_is_404_not_the_domains_409(api, session, service):
    """A URL naming nothing is a different failure from one naming something
    in an unusable state, and no amount of retrying fixes the first.
    """
    session.version = None

    assert _post(api).status_code != 409


def test_09c_the_version_is_resolved_on_the_request_session(api, session, service):
    _post(api)

    assert session.version_lookups == 1
    assert service[0]["version"] is session.version


def test_10_the_endpoint_requires_an_actor(api, session, service, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = _post(api)

    assert response.status_code == 401
    assert service == []


@pytest.mark.parametrize("path_id", ["0", "-1", "abc", "1.5", ""])
def test_10b_the_path_id_must_be_a_positive_integer(api, session, service, path_id):
    response = api.post(
        f"/api/v1/schedule-versions/{path_id}/generate", headers={HEADER: "1"}, json={}
    )

    assert response.status_code in (404, 422)
    assert service == []


def test_11_the_actor_cannot_be_named_by_the_caller(api, session, service):
    """The whole point of the identity boundary. An actor id in the body, the
    query string or anywhere else must not be honoured -- and here it is not
    even accepted.
    """
    body_attempt = _post(api, json={"actor_person_id": 99})
    assert body_attempt.status_code == 422

    query_attempt = api.post(
        URL.format(id=7) + "?actor_person_id=99", headers={HEADER: "1"}, json={}
    )
    assert query_attempt.status_code == 200
    # The query string was ignored: the actor is still the header's Person.
    assert service[-1]["actor"] is session.person


def test_11b_the_route_signature_names_no_actor_parameter(api):
    import inspect

    parameters = set(inspect.signature(routes.generate_schedule_version).parameters)

    assert "actor_person_id" not in parameters
    assert parameters == {"schedule_version_id", "body", "actor", "session"}


# ==========================================================================
# 4 -- the request body becomes a SchedulingPolicy
# ==========================================================================


def test_12_an_omitted_body_means_the_default_policy(api, session, service):
    response = _post(api)

    assert response.status_code == 200
    assert service[0]["policy"] == SchedulingPolicy()


def test_12b_an_empty_object_means_the_default_policy(api, session, service):
    _post(api, json={})

    assert service[0]["policy"] == SchedulingPolicy()


def test_13_every_policy_field_is_carried_through(api, session, service):
    _post(
        api,
        json={
            "allow_no_response": True,
            "target_assignments_per_candidate": 3,
            "balance_candidate_loads": False,
            "role_variety_role_ids": [4, 2],
        },
    )

    policy = service[0]["policy"]
    assert policy.allow_no_response is True
    assert policy.target_assignments_per_candidate == 3
    assert policy.balance_candidate_loads is False
    assert policy.role_variety_role_ids == frozenset({2, 4})


def test_13c_a_client_that_omits_load_balancing_still_gets_it(api, session, service):
    """The backward-compatible default (Task 45). A client written before the
    field existed sends no opinion about workload, and must not thereby get a
    schedule that piles every service onto one person.
    """
    _post(api, json={"target_assignments_per_candidate": None})

    assert service[0]["policy"].balance_candidate_loads is True
    # And balancing does not require a numeric target to take effect.
    assert service[0]["policy"].target_assignments_per_candidate is None


def test_13b_the_request_model_mirrors_the_policy_exactly(api):
    """No Setup-specific constants, no ministry defaults, no extra knobs: the
    transport shape is the domain value and nothing more.
    """
    assert set(schemas.GenerateDraftScheduleRequest.model_fields) == set(
        SchedulingPolicy.__dataclass_fields__
    )


def test_14_an_unknown_field_is_refused(api, session, service):
    response = _post(api, json={"consecutive_sunday_gap": 2})

    assert response.status_code == 422
    assert service == []


def test_14b_a_wrongly_typed_field_is_refused(api, session, service):
    response = _post(api, json={"target_assignments_per_candidate": "three"})

    assert response.status_code == 422
    assert service == []


def test_15_a_policy_scheduling_rejects_is_422(api, session, service):
    """Pydantic accepts ``-1``; the domain does not.

    Note where the rejection comes from. The API does **not** re-check the
    policy -- a second copy of that rule could disagree with the first. The
    solver validates it, and the exception used here is the genuine one its
    validator raises, obtained by calling it. What is being tested is only the
    boundary's half: that this exception becomes 422, and that the request
    rolls back on the way out.
    """
    service.behaviour["raises"] = _real_policy_rejection(
        target_assignments_per_candidate=-1
    )

    response = _post(api, json={"target_assignments_per_candidate": -1})

    assert response.status_code == 422
    assert session.events == ["rollback", "close"]


def _real_policy_rejection(**policy_kwargs) -> SchedulingInputError:
    """The actual exception the solver raises for this policy.

    Calling the real validator keeps the test honest: if the domain ever
    stopped rejecting these values, this helper would fail rather than let the
    boundary test pass against an exception nobody raises any more.
    """
    from app.scheduling import solver

    with pytest.raises(SchedulingInputError) as caught:
        solver._validate_policy(SchedulingPolicy(**policy_kwargs))
    return caught.value


def test_15b_a_scheduling_input_error_from_the_service_is_also_422(
    api, session, service
):
    service.behaviour["raises"] = SchedulingInputError("requirement has no event")

    response = _post(api)

    assert response.status_code == 422
    assert session.events == ["rollback", "close"]


def test_15c_a_solver_failure_stays_a_server_error(api, session, service):
    """``SchedulingEngineError`` means the engine failed, which is this
    server's fault. Mapping it to 422 would blame the caller for a bug.
    """
    service.behaviour["raises"] = SchedulingEngineError("solver returned INFEASIBLE")

    with pytest.raises(SchedulingEngineError):
        _post(api)

    assert session.events == ["rollback", "close"]


def test_15d_a_solver_failure_reaches_a_real_client_as_500(api, session, service):
    """What the previous test asserts by propagation, stated as the client sees
    it. ``raise_server_exceptions=False`` makes the test client behave like a
    deployed server rather than re-raising for the test's benefit.
    """
    service.behaviour["raises"] = SchedulingEngineError("solver returned INFEASIBLE")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            URL.format(id=7), headers={HEADER: "1"}, json={}
        )

    assert response.status_code == 500
    # The failure is not explained to the caller, and the transaction is gone.
    assert "INFEASIBLE" not in response.text
    assert session.events == ["rollback", "close"]


# ==========================================================================
# 5 -- orchestration: the endpoint delegates and does not decide
# ==========================================================================


def test_16_the_service_is_called_once_with_the_resolved_pieces(api, session, service):
    _post(api)

    assert len(service) == 1
    call = service[0]
    assert call["session"] is session
    assert call["actor"] is session.person
    assert call["version"] is session.version
    assert isinstance(call["policy"], SchedulingPolicy)


def test_17_an_authorization_refusal_becomes_403(api, session, service):
    service.behaviour["raises"] = AuthorizationError("You do not manage Setup.")

    response = _post(api)

    assert response.status_code == 403
    assert response.json() == {"detail": "You do not manage Setup."}


def test_18_a_state_refusal_becomes_409(api, session, service):
    service.behaviour["raises"] = InvalidOperationError("Version is not a draft.")

    response = _post(api)

    assert response.status_code == 409
    assert response.json() == {"detail": "Version is not a draft."}


def test_19_the_endpoint_repeats_no_domain_rule():
    """Checked against what the module imports and calls, not against its
    prose -- the docstring legitimately explains which rules live elsewhere.
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
        "require_active_admin",
        "assign_member",
        "solve_schedule",
        "build_scheduling_input",
        "detect_schedule_staleness",
    ):
        assert domain_name not in imported
        assert not hasattr(routes, domain_name)


def test_19b_the_endpoint_does_not_inspect_schedule_state_itself():
    """No DRAFT/REVIEW/FINALIZED literal anywhere in the module's code. The
    lifecycle rule is the service's, and a copy here could disagree with it.
    """
    tree = ast.parse(Path(routes.__file__).read_text())
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    docstrings = set(_docstrings(tree))

    for state in ("DRAFT", "REVIEW", "FINALIZED"):
        assert state not in (literals - docstrings)


def _docstrings(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                found.append(doc)
    return found


# ==========================================================================
# 6 -- the response
# ==========================================================================


def _rich_result() -> DraftGenerationResult:
    return _result(
        assignments=[
            _StubAssignment(501, requirement_id=11, membership_id=21, event_id=31),
            _StubAssignment(502, requirement_id=12, membership_id=22, event_id=31),
        ],
        proposals=[
            ProposedAssignment(requirement_id=11, membership_id=21, event_id=31),
            ProposedAssignment(requirement_id=12, membership_id=22, event_id=31),
        ],
        unfilled=[
            UnfilledRequirement(
                requirement_id=13,
                missing_count=2,
                diagnostic_codes=("NO_QUALIFIED_CANDIDATES", "ALL_UNAVAILABLE"),
            )
        ],
        metrics=SolutionMetrics(
            load_by_membership={22: 1, 21: 3},
            target_excess_total=1,
            fairness_cost=10,
            role_variety_cost=4,
        ),
    )


def test_20_the_response_reports_the_whole_outcome(api, session, service):
    service.behaviour["result"] = _rich_result()

    body = _post(api).json()

    assert body == {
        "schedule_version_id": 7,
        "is_complete": False,
        "created_count": 2,
        "created_assignments": [
            {
                "assignment_id": 501,
                "requirement_id": 11,
                "membership_id": 21,
                "event_id": 31,
            },
            {
                "assignment_id": 502,
                "requirement_id": 12,
                "membership_id": 22,
                "event_id": 31,
            },
        ],
        "unfilled_requirements": [
            {
                "requirement_id": 13,
                "missing_count": 2,
                "diagnostic_codes": [
                    "NO_QUALIFIED_CANDIDATES",
                    "ALL_UNAVAILABLE",
                ],
            }
        ],
        "metrics": {
            "assignment_loads": [
                {"membership_id": 21, "assignment_count": 3},
                {"membership_id": 22, "assignment_count": 1},
            ],
            "target_excess_total": 1,
            "fairness_cost": 10,
            "role_variety_cost": 4,
        },
    }


def test_20b_no_orm_internals_leak_into_the_response(api, session, service):
    service.behaviour["result"] = _rich_result()

    text = _post(api).text

    assert "SECRET-INTERNAL-FIELD" not in text
    assert "override_reason" not in text
    assert "audit" not in text.lower()


def test_21_an_incomplete_generation_is_still_200(api, session, service):
    """An incomplete schedule is an approved outcome. Failing the request would
    throw away the assignments the run legitimately made.
    """
    service.behaviour["result"] = _result(
        assignments=[_StubAssignment(1, 11, 21, 31)],
        unfilled=[UnfilledRequirement(requirement_id=12, missing_count=1)],
    )

    response = _post(api)

    assert response.status_code == 200
    assert response.json()["is_complete"] is False
    assert response.json()["created_count"] == 1
    assert session.commits == 1


def test_22_metrics_nobody_optimized_are_null_not_zero(api, session, service):
    """Reporting ``0`` would claim a preference was evaluated and perfectly
    satisfied when it was never evaluated at all.
    """
    service.behaviour["result"] = _result(metrics=SolutionMetrics())

    metrics = _post(api).json()["metrics"]

    assert metrics["target_excess_total"] is None
    assert metrics["fairness_cost"] is None
    assert metrics["role_variety_cost"] is None
    assert metrics["assignment_loads"] == []


def test_22b_assignment_loads_are_ordered_by_membership(api, session, service):
    service.behaviour["result"] = _result(
        metrics=SolutionMetrics(load_by_membership={30: 1, 10: 2, 20: 3})
    )

    loads = _post(api).json()["metrics"]["assignment_loads"]

    assert [entry["membership_id"] for entry in loads] == [10, 20, 30]


def test_23_a_run_that_created_nothing_is_reported_plainly(api, session, service):
    """The second call on an already-full schedule. Not an error, and not a
    silent success either -- ``created_count`` says zero.
    """
    body = _post(api).json()

    assert body["created_count"] == 0
    assert body["created_assignments"] == []
    assert body["is_complete"] is True


def test_23b_the_response_lists_only_rows_this_request_created(api, session, service):
    """``created_assignments`` comes from what was persisted, never from the
    solver's proposals -- a version's pre-existing assignments are inputs.
    """
    service.behaviour["result"] = _result(
        assignments=[_StubAssignment(900, 11, 21, 31)],
        proposals=[
            ProposedAssignment(requirement_id=11, membership_id=21, event_id=31),
            ProposedAssignment(requirement_id=99, membership_id=99, event_id=99),
        ],
    )

    body = _post(api).json()

    assert body["created_count"] == 1
    assert [a["assignment_id"] for a in body["created_assignments"]] == [900]


# ==========================================================================
# 7 -- the audit and mutation boundary
# ==========================================================================


def test_24_the_route_records_no_audit_event_of_its_own():
    """Auditing belongs to the operation that changed something. The HTTP layer
    adds no action and writes no row.
    """
    tree = ast.parse(Path(routes.__file__).read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]

    assert "record_audit_event" not in imported
    assert not any(name.startswith("ACTION_") for name in imported)
    assert not hasattr(routes, "record_audit_event")


def test_25_the_route_writes_nothing_to_the_session_directly(api, session, service):
    """No ``add``, no ``flush``, no ``delete``: every write goes through the
    service. The recording Session has none of those methods, so a route that
    tried would fail loudly.
    """
    _post(api)

    for forbidden in ("add", "flush", "delete", "merge"):
        assert not hasattr(session, forbidden)

    tree = ast.parse(Path(routes.__file__).read_text())
    calls = [
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    for forbidden in ("add", "flush", "delete", "merge", "commit", "rollback"):
        assert forbidden not in calls


def test_26_the_services_still_never_own_the_transaction():
    """The convention Task 37 depends on: services never commit or roll back,
    which is what lets one request wrap the whole operation.
    """
    for module in sorted(Path("app/services").glob("*.py")):
        calls = [
            node.func.attr
            for node in ast.walk(ast.parse(module.read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "commit" not in calls, module
        assert "rollback" not in calls, module


def test_26b_the_boundary_lives_in_exactly_one_place():
    """One committer. If a second module started committing, "one request, one
    transaction" would be an aspiration rather than a guarantee.
    """
    committers = []
    for module in sorted(Path("app").rglob("*.py")):
        calls = [
            node.func.attr
            for node in ast.walk(ast.parse(module.read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        if "commit" in calls:
            committers.append(module.as_posix())

    assert committers == ["app/api/dependencies.py"]


# ==========================================================================
# 8 -- structural guards
# ==========================================================================


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

def test_27_no_cors_middleware_is_configured():
    assert _installed_middleware_names(app) == {"SessionMiddleware"}

    tree = ast.parse(Path("app/main.py").read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert "CORSMiddleware" not in imported


def test_28_no_authentication_scheme_was_smuggled_in():
    """Google sign-in is a later task. Nothing here should have started it.
    Checked by namespace, not by prose: the modules legitimately *discuss*
    Google authentication in their docstrings.
    """
    import app.api.errors
    import app.api.v1

    for module in (deps, routes, app.api.errors, app.api.v1, schemas):
        for name in ("jwt", "jose", "google", "oauth", "OAuth2", "id_token"):
            assert not hasattr(module, name), (module.__name__, name)

    for module_path in ("app/api/dependencies.py", "app/api/routes_schedule_versions.py"):
        calls = [
            node.func.attr
            for node in ast.walk(ast.parse(Path(module_path).read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "set_cookie" not in calls
        assert "delete_cookie" not in calls


def test_29_no_model_or_migration_change_was_needed():
    """Task 37 is a boundary, not a schema change. The route module touches no
    mapping machinery, and the migration count is asserted so that adding one
    from *this* layer would fail (ten since Task 79 added
    ``person.church_membership_status``).
    """
    assert len(list(Path("alembic/versions").glob("*.py"))) == 10

    tree = ast.parse(Path(routes.__file__).read_text())
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    for schema_name in ("Base", "mapped_column", "Mapped", "relationship"):
        assert schema_name not in imported


def test_30_the_scheduling_package_still_knows_nothing_about_http():
    """422 is decided in :mod:`app.api.errors`. The pure package must not have
    learned a status code to get there.
    """
    for module in sorted(Path("app/scheduling").glob("*.py")):
        imported = [
            alias.name
            for node in ast.walk(ast.parse(module.read_text()))
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        for http_name in ("HTTPException", "status", "JSONResponse", "APIRouter"):
            assert http_name not in imported, module

    import app.scheduling.solver as solver

    assert not hasattr(solver, "HTTPException")


def test_31_the_generation_service_is_reached_through_its_real_name():
    """A guard on the stubbing this suite relies on: the route really does call
    ``app.services.schedule_generation.generate_draft_schedule``.
    """
    from app.services import schedule_generation

    assert _REAL_GENERATE is schedule_generation.generate_draft_schedule
