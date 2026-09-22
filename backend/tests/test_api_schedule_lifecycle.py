"""The two lifecycle endpoints (Task 80).

``POST /api/v1/schedule-versions/{id}/submit-for-review`` and
``POST /api/v1/schedule-versions/{id}/finalize`` -- the HTTP surface over
:mod:`app.services.schedule_lifecycle`.

**What is tested here, and what deliberately is not.** These are route tests:
they prove the endpoints exist, take the actor from the session rather than the
request, pass the version and the reason through untouched, map the domain's
two exceptions onto 403 and 409, commit on success and roll back on failure,
and report the post-transition state. They do **not** re-prove the transition
rules -- which statuses are legal, when idempotency applies, what readiness
refuses, that a Sunday conflict cannot be overridden. Those live in
``test_services_schedule_lifecycle.py``, ``test_services_schedule_finalization.py``
and ``test_services_finalization_readiness.py``, tested against the service
that owns them; asserting them again through a stub here would prove only that
the stub was configured to say so.

The two exceptions are the **authorization matrix** and the
**finalized-is-not-regenerable** rule, which are asserted against the real
authorization function rather than a stub, because Task 80's whole point is
who may call these.

Offline: no database. The Session is a recording stand-in that answers the two
lookups the boundary issues.
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
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import SchedulingPeriod
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
)
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.finalization_readiness import (
    FinalizationIssue,
    FinalizationReadinessResult,
)
from app.services.schedule_staleness import ScheduleVersionStalenessResult
from app.services.schedule_version_detail import ScheduleVersionDetail

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"

SUBMIT_URL = "/api/v1/schedule-versions/{id}/submit-for-review"
FINALIZE_URL = "/api/v1/schedule-versions/{id}/finalize"

MINISTRY_ID = 900
VERSION_ID = 7


# --------------------------------------------------------------------------
# Stand-ins
# --------------------------------------------------------------------------


def _person(person_id: int, name: str, *, is_admin: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    return person


def _heads(person: Person, *, ministry_id: int, membership_id: int = 9001) -> Person:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry_id, is_ministry_head=True
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return person


def _volunteer() -> Person:
    """Somebody in the ministry who does not lead it."""
    person = _person(60, "Bea Volunteer")
    membership = MinistryMembership(
        person_id=person.id, ministry_id=MINISTRY_ID, is_ministry_head=False
    )
    membership.id = 9002
    person.ministry_memberships.append(membership)
    return person


class _StubVersion:
    def __init__(self, status: str = SCHEDULE_VERSION_STATUS_DRAFT) -> None:
        self.id = VERSION_ID
        self.schedule_id = 70
        self.scheduling_period_id = 700
        self.version_number = 1
        self.status = status
        self.finalized_at = None
        self.amends_version_id = None
        self.amendment_reason = None
        self.notes = None


class _StubPeriod:
    def __init__(self) -> None:
        self.id = 700
        self.name = "Q4 2026"
        self.ministry_id = MINISTRY_ID
        self.start_date = datetime.date(2026, 10, 4)
        self.end_date = datetime.date(2026, 12, 27)
        self.availability_locked_at = datetime.datetime(2026, 9, 1, tzinfo=UTC)


def _period_row() -> SchedulingPeriod:
    """A persisted-looking period, with its Ministry attached.

    Real model objects rather than stubs: the lifecycle service reads
    ``period.ministry_id`` to authorize and ``period.ministry.name`` to build
    the audit summary, and a stand-in that answered one but not the other
    would pass a test the service would fail.
    """
    period = SchedulingPeriod(
        ministry_id=MINISTRY_ID,
        name="Q4 2026",
        start_date=datetime.date(2026, 10, 4),
        end_date=datetime.date(2026, 12, 27),
    )
    period.id = 700
    period.ministry = _ministry()
    return period


def _ministry() -> Ministry:
    ministry = Ministry(name="Setup", church_id=1)
    ministry.id = MINISTRY_ID
    return ministry


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    """Answers the three lookups these requests issue, and records commits.

    The period lookup is here because the authorization tests below run
    against the **real** lifecycle service: it resolves the version's owning
    period to find the ministry to authorize against, and stubbing that away
    would stub away the thing being tested.
    """

    def __init__(self, *, person: Person, version: _StubVersion) -> None:
        self.person = person
        self.version = version
        self.period = _period_row()
        self.events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "FROM person" in sql:
            return _ScalarResult(self.person)
        if "FROM schedule_version" in sql:
            return _ScalarResult(self.version)
        if "FROM scheduling_period" in sql:
            return _ScalarResult(self.period)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


def _detail(version: _StubVersion, *, issues=(), stale: bool = False) -> ScheduleVersionDetail:
    staleness = ScheduleVersionStalenessResult(
        current_requirements=frozenset({("current",)} if stale else ()),
        snapshot_requirements=frozenset({("snapshot",)} if stale else ()),
    )
    return ScheduleVersionDetail(
        version=version,
        period=_StubPeriod(),
        ministry=_ministry(),
        requirements=(),
        assignments=(),
        readiness=FinalizationReadinessResult(
            staleness=staleness, issues=tuple(issues)
        ),
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def version() -> _StubVersion:
    return _StubVersion()


@pytest.fixture
def actor() -> Person:
    """The default caller: this ministry's active head."""
    return _heads(_person(1, "Ada Head"), ministry_id=MINISTRY_ID)


@pytest.fixture
def session(monkeypatch, actor: Person, version: _StubVersion) -> RecordingSession:
    recording = RecordingSession(person=actor, version=version)
    monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
    return recording


class _CallLog(list):
    behaviour: dict


def _transition_stub(monkeypatch, name: str, version: _StubVersion) -> _CallLog:
    calls = _CallLog()
    behaviour: dict = {"raises": None, "new_status": None}

    def stub(session, *, actor, version, reason=None):
        calls.append(
            {
                "session": session,
                "actor": actor,
                "version": version,
                "reason": reason,
            }
        )
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        if behaviour["new_status"] is not None:
            version.status = behaviour["new_status"]
        return version

    monkeypatch.setattr(routes, name, stub)
    calls.behaviour = behaviour
    return calls


@pytest.fixture
def submit(monkeypatch, version: _StubVersion) -> _CallLog:
    calls = _transition_stub(
        monkeypatch, "submit_schedule_version_for_review", version
    )
    calls.behaviour["new_status"] = SCHEDULE_VERSION_STATUS_REVIEW
    return calls


@pytest.fixture
def finalize(monkeypatch, version: _StubVersion) -> _CallLog:
    calls = _transition_stub(monkeypatch, "finalize_schedule_version", version)
    calls.behaviour["new_status"] = SCHEDULE_VERSION_STATUS_FINALIZED
    return calls


@pytest.fixture
def detail(monkeypatch, version: _StubVersion) -> _CallLog:
    """Stub the read the endpoints build their response from."""
    calls = _CallLog()
    behaviour: dict = {"issues": (), "stale": False}

    def stub(session, *, actor, version):
        calls.append({"session": session, "actor": actor, "version": version})
        return _detail(version, issues=behaviour["issues"], stale=behaviour["stale"])

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


def _post(api: TestClient, url: str, *, actor_id: int | None = 1, body=None):
    headers = {} if actor_id is None else {HEADER: str(actor_id)}
    return api.post(url.format(id=VERSION_ID), headers=headers, json=body)


# ==========================================================================
# 1-8: who may call these
#
# Against the real authorization rule, never a stub: Task 80's subject is who
# may perform these two writes, and a stubbed service would prove only that
# the stub was told to allow it.
# ==========================================================================


@pytest.mark.parametrize("url", [SUBMIT_URL, FINALIZE_URL])
def test_01_an_unauthenticated_request_is_401(api, session, url):
    response = _post(api, url, actor_id=None)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}


@pytest.mark.parametrize("url", [SUBMIT_URL, FINALIZE_URL])
def test_02_a_volunteer_is_403(api, session, url):
    """In the ministry, and not leading it. No lifecycle action is theirs."""
    session.person = _volunteer()

    response = _post(api, url)

    assert response.status_code == 403
    assert "Ministry Head" in response.json()["detail"]


@pytest.mark.parametrize("url", [SUBMIT_URL, FINALIZE_URL])
def test_03_a_head_of_another_ministry_is_403(api, session, url):
    session.person = _heads(_person(2, "AV Head"), ministry_id=MINISTRY_ID + 1)

    response = _post(api, url)

    assert response.status_code == 403


@pytest.mark.parametrize("url", [SUBMIT_URL, FINALIZE_URL])
def test_04_an_admin_who_heads_nothing_is_403(api, session, url):
    """**The correction Task 80 exists for.** Publishing a ministry's schedule
    is the most consequential operational act in the product, and church-wide
    oversight does not confer it. The same Admin may read this version in
    full -- see ``test_api_schedule_version_detail.py`` -- and may not move it.
    """
    session.person = _person(3, "Elder", is_admin=True)

    response = _post(api, url)

    assert response.status_code == 403
    assert "oversight, not operation" in response.json()["detail"]
    assert session.events == ["rollback", "close"]


@pytest.mark.parametrize("url", [SUBMIT_URL, FINALIZE_URL])
def test_05_a_deactivated_head_is_401(api, session, url):
    """Identity fails before authorization does: a deactivated Person is not
    an actor at all, and saying "403" would confirm the id exists."""
    session.person.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)

    response = _post(api, url)

    assert response.status_code == 401


def test_06_the_ministrys_own_head_may_submit(api, session, submit, detail):
    response = _post(api, SUBMIT_URL)

    assert response.status_code == 200
    assert len(submit) == 1
    assert submit[0]["actor"] is session.person


def test_07_the_ministrys_own_head_may_finalize(api, session, finalize, detail, version):
    version.status = SCHEDULE_VERSION_STATUS_REVIEW

    response = _post(api, FINALIZE_URL)

    assert response.status_code == 200
    assert len(finalize) == 1


@pytest.mark.parametrize("url", [SUBMIT_URL, FINALIZE_URL])
def test_08_an_admin_who_also_heads_this_ministry_may_act(
    api, session, submit, finalize, detail, url
):
    """And through the head membership, not through ``is_admin`` -- which is
    why test 04 refuses the same person without it."""
    session.person = _heads(
        _person(4, "Elder And Head", is_admin=True), ministry_id=MINISTRY_ID
    )

    response = _post(api, url)

    assert response.status_code == 200


# ==========================================================================
# 9-15: the route is a translation, and nothing more
# ==========================================================================


def test_09_the_actor_comes_from_the_session_not_the_body(api, session, submit, detail):
    """There is no actor field to send, and sending one is a 422 rather than
    an impersonation."""
    response = _post(api, SUBMIT_URL, body={"actor_person_id": 99})

    assert response.status_code == 422
    assert submit == []


def test_10_the_version_the_url_names_is_the_one_passed(api, session, submit, detail):
    _post(api, SUBMIT_URL)

    assert submit[0]["version"] is session.version


def test_11_an_unknown_version_is_404(api, session, submit):
    session.version = None

    response = _post(api, SUBMIT_URL)

    assert response.status_code == 404
    assert response.json() == {"detail": "Schedule version not found."}
    assert submit == []


def test_12_a_reason_reaches_the_service_unchanged(api, session, submit, detail):
    _post(api, SUBMIT_URL, body={"reason": "Checked with the team"})

    assert submit[0]["reason"] == "Checked with the team"


def test_13_no_body_means_no_reason(api, session, submit, detail):
    _post(api, SUBMIT_URL)

    assert submit[0]["reason"] is None


def test_14_an_unknown_body_field_is_refused(api, session, submit):
    response = _post(api, SUBMIT_URL, body={"status": "FINALIZED"})

    assert response.status_code == 422
    assert submit == [], "the service must not be reached with a rejected body"


def test_15_the_service_runs_on_the_requests_own_session(api, session, submit, detail):
    _post(api, SUBMIT_URL)

    assert submit[0]["session"] is session
    assert detail[0]["session"] is session


# ==========================================================================
# 16-21: the domain's refusals keep their meanings
# ==========================================================================


@pytest.mark.parametrize(
    "message",
    [
        "only a schedule version in REVIEW can be finalized; a draft must be"
        " submitted for review first",
        "cannot change a schedule version that has been superseded by a newer"
        " version",
        "cannot finalize this version: 2 readiness issue(s) remain",
    ],
)
def test_16_an_invalid_transition_is_409(api, session, finalize, message):
    finalize.behaviour["raises"] = InvalidOperationError(message)

    response = _post(api, FINALIZE_URL)

    assert response.status_code == 409
    assert response.json() == {"detail": message}


def test_17_a_stale_draft_refused_by_the_service_is_409(api, session, submit):
    submit.behaviour["raises"] = InvalidOperationError(
        "cannot submit this version for review: its requirement snapshot no"
        " longer matches current scheduling input"
    )

    response = _post(api, SUBMIT_URL)

    assert response.status_code == 409


def test_18_a_refusal_rolls_the_request_back(api, session, finalize):
    finalize.behaviour["raises"] = InvalidOperationError("not ready")

    _post(api, FINALIZE_URL)

    assert session.events == ["rollback", "close"]


def test_19_a_success_commits_exactly_once(api, session, submit, detail):
    _post(api, SUBMIT_URL)

    assert session.events == ["commit", "close"]


def test_20_the_route_repeats_no_domain_rule(api, session, submit, detail, version):
    """A FINALIZED version reaching the submit endpoint is refused by the
    *service*, not by a second opinion in the route.

    Asserted by letting the stub succeed on a status the domain would refuse:
    if the route carried its own status check the request would never arrive,
    and this test would fail.
    """
    version.status = SCHEDULE_VERSION_STATUS_FINALIZED

    response = _post(api, SUBMIT_URL)

    assert response.status_code == 200
    assert len(submit) == 1


def test_21_a_refused_actor_changes_nothing(api, session):
    """Against the real service: the Admin-only actor is refused, and the
    version's status is exactly what it was."""
    session.person = _person(3, "Elder", is_admin=True)

    response = _post(api, SUBMIT_URL)

    assert response.status_code == 403
    assert session.version.status == SCHEDULE_VERSION_STATUS_DRAFT


# ==========================================================================
# 22-27: what comes back
# ==========================================================================


def test_22_the_response_reports_the_new_status(api, session, submit, detail):
    body = _post(api, SUBMIT_URL).json()

    assert body["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_REVIEW


def test_23_finalizing_reports_finalized(api, session, finalize, detail, version):
    version.status = SCHEDULE_VERSION_STATUS_REVIEW

    body = _post(api, FINALIZE_URL).json()

    assert body["schedule_version"]["status"] == SCHEDULE_VERSION_STATUS_FINALIZED


def test_24_the_response_is_the_same_shape_the_read_returns(api, session, submit, detail):
    """So a review screen needs one round trip, not two."""
    body = _post(api, SUBMIT_URL).json()

    assert set(body) == {
        "schedule_version",
        "period",
        "requirements",
        "assignments",
        "summary",
        "staleness",
        "finalization_readiness",
        "can_operate",
    }


def test_25_the_response_says_the_caller_may_operate(api, session, submit, detail):
    body = _post(api, SUBMIT_URL).json()

    assert body["can_operate"] is True


def test_26_readiness_diagnostics_survive_the_transition(api, session, submit, detail):
    detail.behaviour["issues"] = (
        FinalizationIssue(
            code="UNFILLED_REQUIREMENT",
            message="1 position is still unfilled",
            assignment_id=None,
            schedule_version_requirement_id=11,
        ),
    )

    body = _post(api, SUBMIT_URL).json()

    assert body["finalization_readiness"]["is_ready"] is False
    assert body["finalization_readiness"]["issues"][0]["code"] == "UNFILLED_REQUIREMENT"


def test_27_the_response_is_built_after_the_transition(api, session, submit, detail):
    """Order matters: reading first would report the old status."""
    assert detail == []

    _post(api, SUBMIT_URL)

    assert len(submit) == 1 and len(detail) == 1
    assert detail[0]["version"].status == SCHEDULE_VERSION_STATUS_REVIEW


# ==========================================================================
# 28-31: what the endpoints deliberately do not offer
# ==========================================================================


def test_28_there_is_no_un_finalize_or_status_setting_route(api):
    spec = api.get("/openapi.json").json()
    lifecycle_paths = [
        path for path in spec["paths"] if "schedule-versions/{" in path
    ]

    assert sorted(lifecycle_paths) == [
        "/api/v1/schedule-versions/{schedule_version_id}",
        "/api/v1/schedule-versions/{schedule_version_id}/finalize",
        "/api/v1/schedule-versions/{schedule_version_id}/generate",
        "/api/v1/schedule-versions/{schedule_version_id}/submit-for-review",
    ]
    # And the version resource itself accepts no write verb: its status is
    # changed by the two named actions or not at all.
    version_path = spec["paths"]["/api/v1/schedule-versions/{schedule_version_id}"]
    assert set(version_path) == {"get"}


def test_29_neither_transition_accepts_a_target_status(api):
    spec = api.get("/openapi.json").json()
    body_schema = spec["components"]["schemas"]["LifecycleTransitionRequest"]

    assert set(body_schema["properties"]) == {"reason"}
    assert body_schema.get("additionalProperties") is False


def test_30_generation_is_never_reachable_for_a_finalized_version():
    """Asserted at the service, because that is where the rule lives.

    ``generate_draft_schedule`` requires a *latest DRAFT*, so a FINALIZED
    version cannot be regenerated however the endpoint is called. Pinned here
    too because the finalize endpoint is what makes a version FINALIZED, and
    "nothing rewrites a published schedule" is the promise it makes.
    """
    from app.services import schedule_generation

    source = Path(schedule_generation.__file__).read_text()
    tree = ast.parse(source)
    gate = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_require_latest_draft"
    )
    body = ast.get_source_segment(source, gate)

    assert "SCHEDULE_VERSION_STATUS_DRAFT" in body
    assert "!=" in body, "the gate must exclude every status but DRAFT"
    # And the public operation calls it.
    generate = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "generate_draft_schedule"
    )
    called = {
        node.func.id
        for node in ast.walk(generate)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_require_latest_draft" in called
    assert "require_ministry_operator" in called


def test_31_the_endpoints_add_no_rule_of_their_own():
    """Each handler resolves the version, calls its transition, and maps the
    result. Nothing else -- no status comparison, no readiness call, no audit.
    """
    source = Path(routes.__file__).read_text()
    tree = ast.parse(source)
    for handler_name, transition in (
        ("submit_schedule_version", "submit_schedule_version_for_review"),
        ("finalize_schedule_version_endpoint", "finalize_schedule_version"),
    ):
        handler = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == handler_name
        )
        # The body only: a parameter's `Path(...)`/`Body(...)` default is a
        # Call node as well, and those are FastAPI declarations rather than
        # anything the handler does.
        called = {
            node.func.id
            for statement in handler.body
            for node in ast.walk(statement)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert called == {
            "_require_schedule_version",
            transition,
            "_detail_response",
            "get_schedule_version_detail",
        }, (handler_name, called)
