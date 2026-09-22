"""Serving-limit management over HTTP (Task 57).

Offline: no database. The Session is a recording stand-in answering the few
statements these routes issue, and :mod:`app.services.serving_limit` is
stubbed where the subject is HTTP mapping rather than domain behaviour --
that behaviour has its own dedicated coverage in
``tests/test_services_serving_limit.py``.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api import routes_serving_limits as routes
from app.config import get_settings
from app.main import app
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.serving_limit import MembershipServingLimitEntry, PeriodServingLimits

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
LIST_URL = "/api/v1/scheduling-periods/{id}/serving-limits"
MEMBER_URL = "/api/v1/scheduling-periods/{period_id}/serving-limits/{membership_id}"


class _StubPerson:
    def __init__(self, id: int = 1) -> None:
        self.id = id
        self.display_name = "Ada Head"
        self.is_admin = False
        self.deactivated_at = None
        self.ministry_memberships: list = []


class _StubPeriod:
    def __init__(self, id: int = 500, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.name = "Q4 2026"
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


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    def __init__(self, *, person=None, period=None, membership=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.period = _StubPeriod() if period is None else period
        self.membership = _StubMembership() if membership is None else membership
        self.transaction_events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "FROM ministry_membership" in sql:
            return _ScalarResult(self.membership)
        if "FROM scheduling_period" in sql:
            return _ScalarResult(self.period)
        if "FROM person" in sql:
            return _ScalarResult(self.person)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    def commit(self) -> None:
        self.transaction_events.append("commit")

    def rollback(self) -> None:
        self.transaction_events.append("rollback")

    def close(self) -> None:
        self.transaction_events.append("close")


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
        monkeypatch, "list_serving_limits",
        result=PeriodServingLimits(
            scheduling_period_id=500, scheduling_period_name="Q4 2026",
            ministry_id=900,
            memberships=(
                MembershipServingLimitEntry(118, 42, "Ben", None, None, 4),
            ),
        ),
    )


@pytest.fixture
def setter(monkeypatch):
    return _stub(monkeypatch, "set_serving_limit", result=None)


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


def _delete(api, url, *, actor_id: int = 1, **kwargs):
    return api.delete(url, headers={HEADER: str(actor_id)}, **kwargs)


# ==========================================================================
# GET
# ==========================================================================


def test_admin_may_list_serving_limits(api, session, lister):
    session.person.is_admin = True

    response = _get(api, LIST_URL.format(id=500))

    assert response.status_code == 200
    assert lister[0]["actor"] is session.person
    assert lister[0]["scheduling_period"] is session.period
    body = response.json()
    assert body["scheduling_period_id"] == 500
    assert body["scheduling_period_name"] == "Q4 2026"
    assert body["memberships"][0] == {
        "ministry_membership_id": 118, "person_id": 42, "person_display_name": "Ben",
        "membership_deactivated_at": None, "person_deactivated_at": None,
        "max_assignments": 4,
    }


def test_ministrys_own_head_may_list(api, session, lister):
    assert _get(api, LIST_URL.format(id=500)).status_code == 200


def test_include_inactive_is_forwarded(api, session, lister):
    _get(api, LIST_URL.format(id=500), params={"include_inactive": "true"})
    assert lister[0]["include_inactive"] is True


def test_include_inactive_defaults_to_false(api, session, lister):
    _get(api, LIST_URL.format(id=500))
    assert lister[0]["include_inactive"] is False


def test_no_limit_reports_null_not_a_placeholder(api, session, monkeypatch):
    _stub(
        monkeypatch, "list_serving_limits",
        result=PeriodServingLimits(
            scheduling_period_id=500, scheduling_period_name="Q4 2026",
            ministry_id=900,
            memberships=(MembershipServingLimitEntry(118, 42, "Ben", None, None, None),),
        ),
    )

    body = _get(api, LIST_URL.format(id=500)).json()

    assert body["memberships"][0]["max_assignments"] is None


def test_a_head_of_another_ministry_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _get(api, LIST_URL.format(id=500))

    assert response.status_code == 403
    assert response.json() == {"detail": "you do not manage this ministry"}


def test_a_normal_member_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("not a manager")
    assert _get(api, LIST_URL.format(id=500)).status_code == 403


def test_a_missing_period_is_404_on_list(api, session, lister):
    session.period = None

    response = _get(api, LIST_URL.format(id=500))

    assert response.status_code == 404
    assert response.json() == {"detail": "Scheduling period not found."}
    assert lister == []


def test_an_unauthenticated_request_is_401_on_list(api, session, lister, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.get(LIST_URL.format(id=500))

    assert response.status_code == 401
    assert lister == []


# ==========================================================================
# PUT
# ==========================================================================


def test_admin_may_set_a_positive_maximum(api, session, setter):
    session.person.is_admin = True

    response = _put(
        api, MEMBER_URL.format(period_id=500, membership_id=118),
        json={"max_assignments": 4},
    )

    assert response.status_code == 200
    assert setter[0]["actor"] is session.person
    assert setter[0]["scheduling_period"] is session.period
    assert setter[0]["membership"] is session.membership
    assert setter[0]["max_assignments"] == 4
    body = response.json()
    assert body["ministry_membership_id"] == 118
    assert body["max_assignments"] == 4


def test_ministrys_own_head_may_set(api, session, setter):
    response = _put(
        api, MEMBER_URL.format(period_id=500, membership_id=118),
        json={"max_assignments": 2},
    )
    assert response.status_code == 200


@pytest.mark.parametrize("bad", [0, -1])
def test_a_non_positive_maximum_is_422(api, session, setter, bad):
    response = _put(
        api, MEMBER_URL.format(period_id=500, membership_id=118),
        json={"max_assignments": bad},
    )
    assert response.status_code == 422
    assert setter == []


def test_null_is_rejected_on_put_clearing_is_a_separate_verb(api, session, setter):
    response = _put(
        api, MEMBER_URL.format(period_id=500, membership_id=118),
        json={"max_assignments": None},
    )
    assert response.status_code == 422
    assert setter == []


def test_a_head_of_another_ministry_is_403_on_put(api, session, setter):
    setter.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _put(
        api, MEMBER_URL.format(period_id=500, membership_id=118),
        json={"max_assignments": 3},
    )

    assert response.status_code == 403


def test_cross_ministry_pair_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "membership and scheduling period must belong to the same ministry"
    )

    response = _put(
        api, MEMBER_URL.format(period_id=500, membership_id=118),
        json={"max_assignments": 3},
    )

    assert response.status_code == 409


def test_a_deactivated_target_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "cannot set a serving limit on a deactivated membership"
    )

    response = _put(
        api, MEMBER_URL.format(period_id=500, membership_id=118),
        json={"max_assignments": 3},
    )

    assert response.status_code == 409


def test_a_missing_membership_is_404_on_put(api, session, setter):
    session.membership = None

    response = _put(
        api, MEMBER_URL.format(period_id=500, membership_id=118),
        json={"max_assignments": 3},
    )

    assert response.status_code == 404
    assert setter == []


# ==========================================================================
# DELETE
# ==========================================================================


def test_admin_may_clear_a_maximum(api, session, setter):
    session.person.is_admin = True

    response = _delete(api, MEMBER_URL.format(period_id=500, membership_id=118))

    assert response.status_code == 204
    assert setter[0]["max_assignments"] is None


def test_clearing_an_absent_maximum_is_still_204(api, session, setter):
    response = _delete(api, MEMBER_URL.format(period_id=500, membership_id=118))
    assert response.status_code == 204


def test_a_head_of_another_ministry_is_403_on_delete(api, session, setter):
    setter.behaviour["raises"] = AuthorizationError("nope")
    response = _delete(api, MEMBER_URL.format(period_id=500, membership_id=118))
    assert response.status_code == 403


def test_a_missing_period_is_404_on_delete(api, session, setter):
    session.period = None
    response = _delete(api, MEMBER_URL.format(period_id=500, membership_id=118))
    assert response.status_code == 404
    assert setter == []
