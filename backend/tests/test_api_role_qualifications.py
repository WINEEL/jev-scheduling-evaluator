"""Role-qualification management over HTTP (Task 55).

Offline: no database. The Session is a recording stand-in answering the few
statements these routes issue, and :mod:`app.services.role_qualification` is
stubbed where the subject is HTTP mapping rather than domain behaviour --
that behaviour has its own dedicated coverage in
``tests/test_services_role_qualification.py``.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import routes_role_qualifications as routes
from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.role_qualification import MembershipQualification, RoleQualifications

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
QUALIFICATIONS_URL = "/api/v1/ministry-roles/{id}/qualifications"
QUALIFICATION_URL = "/api/v1/ministry-roles/{role_id}/qualifications/{membership_id}"


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


class _StubRole:
    def __init__(self, id: int = 12, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.name = "Slides"
        self.deactivated_at = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubMemberPerson:
    def __init__(self, id: int = 42, name: str = "Ben", deactivated: bool = False) -> None:
        self.id = id
        self.display_name = name
        self.deactivated_at = (
            datetime.datetime(2026, 1, 1, tzinfo=UTC) if deactivated else None
        )


class _StubMembership:
    def __init__(self, id: int = 118, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.person_id = 42
        self.person = _StubMemberPerson()
        self.deactivated_at = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubQualification:
    def __init__(self, is_qualified: bool, decided_at=None) -> None:
        self.is_qualified = is_qualified
        self.decided_at = decided_at or datetime.datetime(2026, 9, 1, tzinfo=UTC)


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    """A stand-in request Session, matching earlier tasks' own pattern
    exactly.
    """

    def __init__(self, *, person=None, role=None, membership=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.role = _StubRole() if role is None else role
        self.membership = _StubMembership() if membership is None else membership
        self.transaction_events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "FROM ministry_role" in sql:
            return _ScalarResult(self.role)
        if "FROM ministry_membership" in sql:
            return _ScalarResult(self.membership)
        if "FROM person" in sql:
            return _ScalarResult(self.person)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    def commit(self) -> None:
        self.transaction_events.append("commit")

    def rollback(self) -> None:
        self.transaction_events.append("rollback")

    def close(self) -> None:
        self.transaction_events.append("close")


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


def _stub(monkeypatch, name: str, *, result):
    calls = _CallLog()
    behaviour: dict = {"result": result, "raises": None}

    def fake(session, *, actor, **kwargs):
        calls.append({"session": session, "actor": actor, **kwargs})
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        return behaviour["result"]

    monkeypatch.setattr(routes, name, fake)
    calls.behaviour = behaviour
    return calls


@pytest.fixture
def lister(monkeypatch):
    return _stub(
        monkeypatch, "list_role_qualifications",
        result=RoleQualifications(
            ministry_role_id=12, role_name="Slides", ministry_id=900,
            memberships=(
                MembershipQualification(118, 42, "Ben", None, None, True, datetime.datetime(2026, 9, 1, tzinfo=UTC)),
            ),
        ),
    )


@pytest.fixture
def setter(monkeypatch):
    return _stub(monkeypatch, "set_role_qualification", result=_StubQualification(True))


@pytest.fixture
def api(monkeypatch) -> TestClient:
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _get(api, url, *, actor_id: int = 1, **kwargs):
    return api.get(url, headers={HEADER: str(actor_id)}, **kwargs)


def _put(api, url, *, actor_id: int = 1, **kwargs):
    return api.put(url, headers={HEADER: str(actor_id)}, **kwargs)


# ==========================================================================
# GET /ministry-roles/{id}/qualifications
# ==========================================================================


def test_admin_may_list_qualifications(api, session, lister):
    session.person.is_admin = True

    response = _get(api, QUALIFICATIONS_URL.format(id=12))

    assert response.status_code == 200
    assert lister[0]["actor"] is session.person
    assert lister[0]["role"] is session.role
    body = response.json()
    assert body["ministry_role_id"] == 12
    assert body["memberships"][0] == {
        "ministry_membership_id": 118, "person_id": 42, "person_display_name": "Ben",
        "membership_deactivated_at": None, "person_deactivated_at": None,
        "is_qualified": True, "decided_at": "2026-09-01T00:00:00Z",
    }


def test_ministrys_own_head_may_list_qualifications(api, session, lister):
    assert _get(api, QUALIFICATIONS_URL.format(id=12)).status_code == 200


def test_include_inactive_query_param_is_forwarded(api, session, lister):
    _get(api, QUALIFICATIONS_URL.format(id=12), params={"include_inactive": "true"})

    assert lister[0]["include_inactive"] is True


def test_include_inactive_defaults_to_false(api, session, lister):
    _get(api, QUALIFICATIONS_URL.format(id=12))

    assert lister[0]["include_inactive"] is False


def test_never_assessed_reports_null_not_false(api, session, monkeypatch):
    _stub(
        monkeypatch, "list_role_qualifications",
        result=RoleQualifications(
            ministry_role_id=12, role_name="Slides", ministry_id=900,
            memberships=(MembershipQualification(118, 42, "Ben", None, None, None, None),),
        ),
    )

    body = _get(api, QUALIFICATIONS_URL.format(id=12)).json()

    assert body["memberships"][0]["is_qualified"] is None
    assert body["memberships"][0]["decided_at"] is None


def test_a_head_of_another_ministry_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _get(api, QUALIFICATIONS_URL.format(id=12))

    assert response.status_code == 403
    assert response.json() == {"detail": "you do not manage this ministry"}


def test_a_normal_member_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("not a manager")

    assert _get(api, QUALIFICATIONS_URL.format(id=12)).status_code == 403


def test_a_missing_role_is_404_on_list(api, session, lister):
    session.role = None

    response = _get(api, QUALIFICATIONS_URL.format(id=12))

    assert response.status_code == 404
    assert response.json() == {"detail": "Ministry role not found."}
    assert lister == []


def test_an_unauthenticated_request_is_401_on_list(api, session, lister, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.get(QUALIFICATIONS_URL.format(id=12))

    assert response.status_code == 401
    assert lister == []


# ==========================================================================
# PUT /ministry-roles/{role_id}/qualifications/{membership_id}
# ==========================================================================


def test_admin_may_approve_a_membership(api, session, setter):
    session.person.is_admin = True

    response = _put(
        api, QUALIFICATION_URL.format(role_id=12, membership_id=118),
        json={"is_qualified": True},
    )

    assert response.status_code == 200
    assert setter[0]["actor"] is session.person
    assert setter[0]["membership"] is session.membership
    assert setter[0]["role"] is session.role
    assert setter[0]["is_qualified"] is True
    body = response.json()
    assert body["ministry_membership_id"] == 118
    assert body["is_qualified"] is True


def test_ministrys_own_head_may_set_a_qualification(api, session, setter):
    response = _put(
        api, QUALIFICATION_URL.format(role_id=12, membership_id=118),
        json={"is_qualified": False},
    )
    assert response.status_code == 200


def test_a_reason_is_forwarded(api, session, setter):
    _put(
        api, QUALIFICATION_URL.format(role_id=12, membership_id=118),
        json={"is_qualified": True, "reason": "Completed training."},
    )

    assert setter[0]["reason"] == "Completed training."


def test_a_head_of_another_ministry_is_403_on_put(api, session, setter):
    setter.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _put(
        api, QUALIFICATION_URL.format(role_id=12, membership_id=118),
        json={"is_qualified": True},
    )

    assert response.status_code == 403


def test_cross_ministry_pair_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "membership and role must belong to the same ministry"
    )

    response = _put(
        api, QUALIFICATION_URL.format(role_id=12, membership_id=118),
        json={"is_qualified": True},
    )

    assert response.status_code == 409


def test_deactivated_target_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "cannot record a new qualification decision for a deactivated membership"
    )

    response = _put(
        api, QUALIFICATION_URL.format(role_id=12, membership_id=118),
        json={"is_qualified": True},
    )

    assert response.status_code == 409


def test_a_missing_role_is_404_on_put(api, session, setter):
    session.role = None

    response = _put(
        api, QUALIFICATION_URL.format(role_id=12, membership_id=118),
        json={"is_qualified": True},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Ministry role not found."}
    assert setter == []


def test_a_missing_membership_is_404_on_put(api, session, setter):
    session.membership = None

    response = _put(
        api, QUALIFICATION_URL.format(role_id=12, membership_id=118),
        json={"is_qualified": True},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Ministry membership not found."}
    assert setter == []


def test_is_qualified_is_required_and_must_be_a_bool(api, session, setter):
    response = _put(api, QUALIFICATION_URL.format(role_id=12, membership_id=118), json={})

    assert response.status_code == 422
    assert setter == []


def test_an_unauthenticated_request_is_401_on_put(api, session, setter, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.put(
        QUALIFICATION_URL.format(role_id=12, membership_id=118), json={"is_qualified": True},
    )

    assert response.status_code == 401
    assert setter == []


def test_no_delete_endpoint_exists_for_qualifications(api, session):
    """A ``RoleQualification`` row is never deleted once created -- there is
    no ``DELETE`` route, unlike staffing requirements (Task 54).
    """
    response = api.delete(
        QUALIFICATION_URL.format(role_id=12, membership_id=118), headers={HEADER: "1"},
    )

    assert response.status_code == 405


# ==========================================================================
# Schema hygiene
# ==========================================================================


def test_the_response_models_forbid_extra_fields():
    import app.api.role_qualification_schemas as schemas

    for name in schemas.__all__:
        model = getattr(schemas, name)
        assert model.model_config.get("extra") == "forbid", name
        assert model.model_config.get("from_attributes") is not True, name
