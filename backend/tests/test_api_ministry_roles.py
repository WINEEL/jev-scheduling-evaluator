"""Ministry role management over HTTP (Task 53).

Offline: no database. The Session is a recording stand-in answering the few
statements these routes issue, and :mod:`app.services.ministry_role` is
stubbed where the subject is HTTP mapping rather than domain behaviour --
that behaviour has its own dedicated coverage in
``tests/test_services_ministry_role.py``.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import routes_ministry_roles as routes
from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.services.errors import AuthorizationError, InvalidOperationError

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
ROLES_URL = "/api/v1/ministries/{id}/roles"
ROLE_URL = "/api/v1/ministry-roles/{id}"
DEACTIVATE_URL = "/api/v1/ministry-roles/{id}/deactivate"
REACTIVATE_URL = "/api/v1/ministry-roles/{id}/reactivate"


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
    def __init__(self, id: int = 900, name: str = "Kids") -> None:
        self.id = id
        self.name = name


class _StubRole:
    def __init__(self, id: int = 700, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.name = "Check-in"
        self.description = "Greets families."
        self.display_order = 0
        self.deactivated_at = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    """A stand-in request Session, matching
    ``tests/test_api_schedule_entry.py``'s own pattern exactly.
    """

    def __init__(self, *, person=None, ministry=None, role=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.ministry = _StubMinistry() if ministry is None else ministry
        self.role = _StubRole() if role is None else role
        self.events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        # Checked before the plain "ministry" branch: "FROM ministry_role"
        # also contains the substring "FROM ministry", so the more specific
        # table name must be tested first.
        if "FROM ministry_role" in sql:
            return _ScalarResult(self.role)
        if "FROM person" in sql:
            return _ScalarResult(self.person)
        if "FROM ministry" in sql:
            return _ScalarResult(self.ministry)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


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
    return _stub(monkeypatch, "list_ministry_roles", result=(_StubRole(),))


@pytest.fixture
def creator(monkeypatch):
    return _stub(monkeypatch, "create_ministry_role", result=_StubRole())


@pytest.fixture
def updater(monkeypatch):
    return _stub(monkeypatch, "update_ministry_role", result=_StubRole(name="Registration"))


@pytest.fixture
def deactivator(monkeypatch):
    return _stub(
        monkeypatch, "deactivate_ministry_role",
        result=_StubRole(deactivated_at=datetime.datetime(2026, 9, 1, tzinfo=UTC)),
    )


@pytest.fixture
def reactivator(monkeypatch):
    return _stub(monkeypatch, "reactivate_ministry_role", result=_StubRole())


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


def _post(api, url, *, actor_id: int = 1, **kwargs):
    return api.post(url, headers={HEADER: str(actor_id)}, **kwargs)


def _patch(api, url, *, actor_id: int = 1, **kwargs):
    return api.patch(url, headers={HEADER: str(actor_id)}, **kwargs)


# ==========================================================================
# GET /ministries/{id}/roles
# ==========================================================================


def test_admin_may_list_roles(api, session, lister):
    session.person.is_admin = True

    response = _get(api, ROLES_URL.format(id=900))

    assert response.status_code == 200
    assert lister[0]["actor"] is session.person
    body = response.json()
    assert body["ministry_id"] == 900
    assert body["ministry_name"] == "Kids"
    assert body["roles"][0]["name"] == "Check-in"
    assert body["roles"][0]["deactivated_at"] is None


def test_ministrys_own_head_may_list_roles(api, session, lister):
    assert _get(api, ROLES_URL.format(id=900)).status_code == 200


def test_include_inactive_query_param_is_forwarded(api, session, lister):
    _get(api, ROLES_URL.format(id=900), params={"include_inactive": "true"})

    assert lister[0]["include_inactive"] is True


def test_include_inactive_defaults_to_false(api, session, lister):
    _get(api, ROLES_URL.format(id=900))

    assert lister[0]["include_inactive"] is False


def test_a_head_of_another_ministry_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _get(api, ROLES_URL.format(id=900))

    assert response.status_code == 403
    assert response.json() == {"detail": "you do not manage this ministry"}


def test_a_normal_member_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("not a manager")

    assert _get(api, ROLES_URL.format(id=900)).status_code == 403


def test_a_missing_ministry_is_404_on_list(api, session, lister):
    session.ministry = None

    response = _get(api, ROLES_URL.format(id=900))

    assert response.status_code == 404
    assert response.json() == {"detail": "Ministry not found."}
    assert lister == []


def test_an_unauthenticated_request_is_401_on_list(api, session, lister, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.get(ROLES_URL.format(id=900))

    assert response.status_code == 401
    assert lister == []


# ==========================================================================
# POST /ministries/{id}/roles
# ==========================================================================


def test_admin_may_create_a_role(api, session, creator):
    session.person.is_admin = True

    response = _post(
        api, ROLES_URL.format(id=900),
        json={"name": "Check-in", "description": "Greets families."},
    )

    assert response.status_code == 201
    assert creator[0]["actor"] is session.person
    assert creator[0]["name"] == "Check-in"
    assert creator[0]["description"] == "Greets families."
    body = response.json()
    assert body["name"] == "Check-in"
    assert body["display_order"] == 0


def test_create_a_role_with_only_a_name(api, session, creator):
    response = _post(api, ROLES_URL.format(id=900), json={"name": "Check-in"})

    assert response.status_code == 201
    assert creator[0]["description"] is None


def test_a_head_of_another_ministry_is_403_on_create(api, session, creator):
    creator.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _post(api, ROLES_URL.format(id=900), json={"name": "Check-in"})

    assert response.status_code == 403


def test_duplicate_name_is_409_on_create(api, session, creator):
    creator.behaviour["raises"] = InvalidOperationError(
        "a role named 'Check-in' already exists for this ministry"
    )

    response = _post(api, ROLES_URL.format(id=900), json={"name": "Check-in"})

    assert response.status_code == 409


def test_a_missing_ministry_is_404_on_create(api, session, creator):
    session.ministry = None

    response = _post(api, ROLES_URL.format(id=900), json={"name": "Check-in"})

    assert response.status_code == 404
    assert creator == []


def test_a_blank_name_is_422_on_create(api, session, creator):
    """Pydantic's own ``min_length=1`` catches this before the service is
    ever reached -- the service's own blank-after-strip check is a second,
    independent guard for the rare case a caller sends whitespace only.
    """
    response = _post(api, ROLES_URL.format(id=900), json={"name": ""})

    assert response.status_code == 422
    assert creator == []


def test_display_order_cannot_be_set_by_the_caller(api, session, creator):
    """The request schema forbids extra fields, so a caller-supplied
    ``display_order`` is rejected outright rather than silently ignored.
    """
    response = _post(
        api, ROLES_URL.format(id=900), json={"name": "Check-in", "display_order": 5},
    )

    assert response.status_code == 422


def test_an_unauthenticated_request_is_401_on_create(api, session, creator, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.post(ROLES_URL.format(id=900), json={"name": "Check-in"})

    assert response.status_code == 401
    assert creator == []


# ==========================================================================
# PATCH /ministry-roles/{id}
# ==========================================================================


def test_admin_may_update_a_role(api, session, updater):
    session.person.is_admin = True

    response = _patch(
        api, ROLE_URL.format(id=700), json={"name": "Registration", "description": None},
    )

    assert response.status_code == 200
    assert updater[0]["actor"] is session.person
    assert updater[0]["role"] is session.role
    assert updater[0]["name"] == "Registration"
    assert response.json()["name"] == "Registration"


def test_a_head_of_another_ministry_is_403_on_update(api, session, updater):
    updater.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _patch(api, ROLE_URL.format(id=700), json={"name": "Registration"})

    assert response.status_code == 403


def test_duplicate_name_is_409_on_update(api, session, updater):
    updater.behaviour["raises"] = InvalidOperationError(
        "a role named 'Registration' already exists for this ministry"
    )

    response = _patch(api, ROLE_URL.format(id=700), json={"name": "Registration"})

    assert response.status_code == 409


def test_a_missing_role_is_404_on_update(api, session, updater):
    session.role = None

    response = _patch(api, ROLE_URL.format(id=700), json={"name": "Registration"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Ministry role not found."}
    assert updater == []


def test_a_blank_name_is_422_on_update(api, session, updater):
    response = _patch(api, ROLE_URL.format(id=700), json={"name": ""})

    assert response.status_code == 422
    assert updater == []


def test_an_unauthenticated_request_is_401_on_update(api, session, updater, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.patch(ROLE_URL.format(id=700), json={"name": "Registration"})

    assert response.status_code == 401
    assert updater == []


# ==========================================================================
# POST /ministry-roles/{id}/deactivate
# ==========================================================================


def test_admin_may_deactivate_a_role(api, session, deactivator):
    session.person.is_admin = True

    response = _post(api, DEACTIVATE_URL.format(id=700), json={})

    assert response.status_code == 200
    assert deactivator[0]["role"] is session.role
    assert response.json()["deactivated_at"] is not None


def test_deactivate_accepts_an_empty_body(api, session, deactivator):
    response = api.post(
        DEACTIVATE_URL.format(id=700), headers={HEADER: "1"},
    )

    assert response.status_code == 200


def test_deactivate_forwards_an_optional_reason(api, session, deactivator):
    _post(api, DEACTIVATE_URL.format(id=700), json={"reason": "No longer needed."})

    assert deactivator[0]["reason"] == "No longer needed."


def test_a_head_of_another_ministry_is_403_on_deactivate(api, session, deactivator):
    deactivator.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _post(api, DEACTIVATE_URL.format(id=700), json={})

    assert response.status_code == 403


def test_a_missing_role_is_404_on_deactivate(api, session, deactivator):
    session.role = None

    response = _post(api, DEACTIVATE_URL.format(id=700), json={})

    assert response.status_code == 404
    assert deactivator == []


def test_an_unauthenticated_request_is_401_on_deactivate(api, session, deactivator, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.post(DEACTIVATE_URL.format(id=700), json={})

    assert response.status_code == 401
    assert deactivator == []


# ==========================================================================
# POST /ministry-roles/{id}/reactivate
# ==========================================================================


def test_admin_may_reactivate_a_role(api, session, reactivator):
    session.person.is_admin = True
    session.role = _StubRole(deactivated_at=datetime.datetime(2026, 9, 1, tzinfo=UTC))

    response = _post(api, REACTIVATE_URL.format(id=700), json={})

    assert response.status_code == 200
    assert reactivator[0]["role"] is session.role
    assert response.json()["deactivated_at"] is None


def test_a_head_of_another_ministry_is_403_on_reactivate(api, session, reactivator):
    reactivator.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _post(api, REACTIVATE_URL.format(id=700), json={})

    assert response.status_code == 403


def test_a_missing_role_is_404_on_reactivate(api, session, reactivator):
    session.role = None

    response = _post(api, REACTIVATE_URL.format(id=700), json={})

    assert response.status_code == 404
    assert reactivator == []


def test_an_unauthenticated_request_is_401_on_reactivate(api, session, reactivator, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.post(REACTIVATE_URL.format(id=700), json={})

    assert response.status_code == 401
    assert reactivator == []


# ==========================================================================
# Schema hygiene
# ==========================================================================


def test_the_response_models_forbid_extra_fields():
    import app.api.ministry_role_schemas as schemas

    for name in schemas.__all__:
        model = getattr(schemas, name)
        assert model.model_config.get("extra") == "forbid", name
        assert model.model_config.get("from_attributes") is not True, name
