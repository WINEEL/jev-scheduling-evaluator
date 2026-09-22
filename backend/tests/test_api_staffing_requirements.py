"""Staffing-requirement management over HTTP (Task 54).

Offline: no database. The Session is a recording stand-in answering the few
statements these routes issue, and :mod:`app.services.staffing_requirement`
is stubbed where the subject is HTTP mapping rather than domain behaviour --
that behaviour has its own dedicated coverage in
``tests/test_services_staffing_requirement.py``.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import routes_staffing_requirements as routes
from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.models.scheduling_input import EVENT_KIND_SUNDAY_SERVICE
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.scheduling_period import EventSummary
from app.services.staffing_requirement import EventStaffing, RoleStaffing

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
STAFFING_URL = "/api/v1/events/{id}/staffing-requirements"
REQUIREMENT_URL = "/api/v1/events/{event_id}/staffing-requirements/{role_id}"
PERIOD_EVENTS_URL = "/api/v1/scheduling-periods/{id}/events"


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


class _StubEvent:
    def __init__(self, id: int = 700, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.event_date = datetime.date(2026, 10, 4)
        self.event_kind = EVENT_KIND_SUNDAY_SERVICE
        self.name = None
        self.cancelled_at = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubRole:
    def __init__(self, id: int = 12, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.name = "Slides"
        self.description = None
        self.display_order = 0
        self.deactivated_at = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubPeriod:
    def __init__(self, id: int = 500, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.name = "Q4 2026"
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubRequirement:
    def __init__(self, required_count: int, **overrides) -> None:
        self.event_id = 700
        self.ministry_role_id = 12
        self.required_count = required_count
        for key, value in overrides.items():
            setattr(self, key, value)


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    """A stand-in request Session, matching
    ``tests/test_api_ministry_roles.py``'s own pattern exactly.
    """

    def __init__(self, *, person=None, event=None, role=None, period=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.event = _StubEvent() if event is None else event
        self.role = _StubRole() if role is None else role
        self.period = _StubPeriod() if period is None else period
        self.transaction_events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        # Checked before the plain "ministry_role" branch would matter here
        # too, though "event" and "ministry_role" share no ambiguous prefix
        # the way "ministry" and "ministry_role" did in Task 53's tests.
        if "FROM ministry_role" in sql:
            return _ScalarResult(self.role)
        if "FROM scheduling_period" in sql:
            return _ScalarResult(self.period)
        if "FROM event" in sql:
            return _ScalarResult(self.event)
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
        monkeypatch, "list_event_staffing_requirements",
        result=EventStaffing(
            event_id=700, event_date=datetime.date(2026, 10, 4), event_name=None,
            event_kind=EVENT_KIND_SUNDAY_SERVICE, ministry_id=900,
            roles=(
                RoleStaffing(
                    ministry_role_id=12, name="Slides", description=None,
                    display_order=0, required_count=1,
                ),
            ),
        ),
    )


@pytest.fixture
def setter(monkeypatch):
    return _stub(monkeypatch, "set_staffing_requirement", result=_StubRequirement(2))


@pytest.fixture
def period_lister(monkeypatch):
    return _stub(
        monkeypatch, "list_period_events",
        result=(
            EventSummary(
                event_id=700, event_date=datetime.date(2026, 10, 4), event_name=None,
                event_kind=EVENT_KIND_SUNDAY_SERVICE, cancelled_at=None,
            ),
        ),
    )


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
# GET /scheduling-periods/{id}/events
# ==========================================================================


def test_admin_may_list_period_events(api, session, period_lister):
    session.person.is_admin = True

    response = _get(api, PERIOD_EVENTS_URL.format(id=500))

    assert response.status_code == 200
    assert period_lister[0]["actor"] is session.person
    assert period_lister[0]["period"] is session.period
    body = response.json()
    assert body["scheduling_period_id"] == 500
    assert body["events"] == [
        {
            "event_id": 700, "event_date": "2026-10-04", "event_name": None,
            "event_kind": EVENT_KIND_SUNDAY_SERVICE, "cancelled_at": None,
        },
    ]


def test_ministrys_own_head_may_list_period_events(api, session, period_lister):
    assert _get(api, PERIOD_EVENTS_URL.format(id=500)).status_code == 200


def test_a_head_of_another_ministry_is_403_on_period_events(api, session, period_lister):
    period_lister.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _get(api, PERIOD_EVENTS_URL.format(id=500))

    assert response.status_code == 403


def test_a_normal_member_is_403_on_period_events(api, session, period_lister):
    period_lister.behaviour["raises"] = AuthorizationError("not a manager")

    assert _get(api, PERIOD_EVENTS_URL.format(id=500)).status_code == 403


def test_a_missing_period_is_404_on_period_events(api, session, period_lister):
    session.period = None

    response = _get(api, PERIOD_EVENTS_URL.format(id=500))

    assert response.status_code == 404
    assert response.json() == {"detail": "Scheduling period not found."}
    assert period_lister == []


def test_a_cancelled_event_is_shown_not_hidden(api, session, monkeypatch):
    cancelled_at = "2026-10-01T00:00:00Z"
    _stub(
        monkeypatch, "list_period_events",
        result=(
            EventSummary(
                event_id=700, event_date=datetime.date(2026, 10, 4), event_name=None,
                event_kind=EVENT_KIND_SUNDAY_SERVICE,
                cancelled_at=datetime.datetime(2026, 10, 1, tzinfo=UTC),
            ),
        ),
    )

    body = _get(api, PERIOD_EVENTS_URL.format(id=500)).json()

    assert body["events"][0]["cancelled_at"] == cancelled_at


def test_an_unauthenticated_request_is_401_on_period_events(
    api, session, period_lister, monkeypatch
):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.get(PERIOD_EVENTS_URL.format(id=500))

    assert response.status_code == 401
    assert period_lister == []


# ==========================================================================
# GET /events/{id}/staffing-requirements
# ==========================================================================


def test_admin_may_list_event_staffing(api, session, lister):
    session.person.is_admin = True

    response = _get(api, STAFFING_URL.format(id=700))

    assert response.status_code == 200
    assert lister[0]["actor"] is session.person
    assert lister[0]["event"] is session.event
    body = response.json()
    assert body["event_id"] == 700
    assert body["roles"][0] == {
        "ministry_role_id": 12, "name": "Slides", "description": None,
        "display_order": 0, "required_count": 1,
    }


def test_ministrys_own_head_may_list_event_staffing(api, session, lister):
    assert _get(api, STAFFING_URL.format(id=700)).status_code == 200


def test_a_role_with_no_requirement_reports_null_not_zero(api, session, monkeypatch):
    _stub(
        monkeypatch, "list_event_staffing_requirements",
        result=EventStaffing(
            event_id=700, event_date=datetime.date(2026, 10, 4), event_name=None,
            event_kind=EVENT_KIND_SUNDAY_SERVICE, ministry_id=900,
            roles=(
                RoleStaffing(
                    ministry_role_id=12, name="Slides", description=None,
                    display_order=0, required_count=None,
                ),
            ),
        ),
    )

    body = _get(api, STAFFING_URL.format(id=700)).json()

    assert body["roles"][0]["required_count"] is None


def test_a_head_of_another_ministry_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _get(api, STAFFING_URL.format(id=700))

    assert response.status_code == 403
    assert response.json() == {"detail": "you do not manage this ministry"}


def test_a_normal_member_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("not a manager")

    assert _get(api, STAFFING_URL.format(id=700)).status_code == 403


def test_a_missing_event_is_404_on_list(api, session, lister):
    session.event = None

    response = _get(api, STAFFING_URL.format(id=700))

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found."}
    assert lister == []


def test_an_unauthenticated_request_is_401_on_list(api, session, lister, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.get(STAFFING_URL.format(id=700))

    assert response.status_code == 401
    assert lister == []


# ==========================================================================
# PUT /events/{event_id}/staffing-requirements/{role_id}
# ==========================================================================


def test_admin_may_set_a_requirement(api, session, setter):
    session.person.is_admin = True

    response = _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12), json={"required_count": 2},
    )

    assert response.status_code == 200
    assert setter[0]["actor"] is session.person
    assert setter[0]["event"] is session.event
    assert setter[0]["role"] is session.role
    assert setter[0]["required_count"] == 2
    body = response.json()
    assert body["ministry_role_id"] == 12
    assert body["required_count"] == 2


def test_ministrys_own_head_may_set_a_requirement(api, session, setter):
    response = _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12), json={"required_count": 1},
    )
    assert response.status_code == 200


def test_a_reason_is_forwarded(api, session, setter):
    _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12),
        json={"required_count": 1, "reason": "Extra help needed this week."},
    )

    assert setter[0]["reason"] == "Extra help needed this week."


def test_a_head_of_another_ministry_is_403_on_put(api, session, setter):
    setter.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12), json={"required_count": 1},
    )

    assert response.status_code == 403


def test_cross_ministry_pair_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "event and role must belong to the same ministry"
    )

    response = _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12), json={"required_count": 1},
    )

    assert response.status_code == 409


def test_deactivated_role_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "cannot record a new or changed staffing requirement for a deactivated role"
    )

    response = _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12), json={"required_count": 1},
    )

    assert response.status_code == 409


def test_a_missing_event_is_404_on_put(api, session, setter):
    session.event = None

    response = _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12), json={"required_count": 1},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found."}
    assert setter == []


def test_a_missing_role_is_404_on_put(api, session, setter):
    session.role = None

    response = _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12), json={"required_count": 1},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Ministry role not found."}
    assert setter == []


@pytest.mark.parametrize("bad_count", [0, -1])
def test_a_non_positive_count_is_422_on_put(api, session, setter, bad_count):
    """Pydantic's own ``ge=1`` catches this before the service is ever
    reached -- zero is expressed by ``DELETE``, never by this request body.
    """
    response = _put(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12),
        json={"required_count": bad_count},
    )

    assert response.status_code == 422
    assert setter == []


def test_an_unauthenticated_request_is_401_on_put(api, session, setter, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.put(
        REQUIREMENT_URL.format(event_id=700, role_id=12), json={"required_count": 1},
    )

    assert response.status_code == 401
    assert setter == []


# ==========================================================================
# DELETE /events/{event_id}/staffing-requirements/{role_id}
# ==========================================================================


def test_admin_may_clear_a_requirement(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_staffing_requirement", result=None)
    session.person.is_admin = True

    response = _delete(api, REQUIREMENT_URL.format(event_id=700, role_id=12))

    assert response.status_code == 204
    assert response.content == b""
    assert calls[0]["required_count"] == 0
    assert calls[0]["event"] is session.event
    assert calls[0]["role"] is session.role


def test_clearing_an_already_absent_requirement_is_still_204(api, session, monkeypatch):
    """The service's own no-op path, not a 404 -- the event and role both
    still exist, only the requirement between them does not."""
    calls = _stub(monkeypatch, "set_staffing_requirement", result=None)

    response = _delete(api, REQUIREMENT_URL.format(event_id=700, role_id=12))

    assert response.status_code == 204


def test_a_reason_query_param_is_forwarded_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_staffing_requirement", result=None)

    _delete(
        api, REQUIREMENT_URL.format(event_id=700, role_id=12),
        params={"reason": "No longer needed this week."},
    )

    assert calls[0]["reason"] == "No longer needed this week."


def test_a_head_of_another_ministry_is_403_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_staffing_requirement", result=None)
    calls.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _delete(api, REQUIREMENT_URL.format(event_id=700, role_id=12))

    assert response.status_code == 403


def test_a_missing_event_is_404_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_staffing_requirement", result=None)
    session.event = None

    response = _delete(api, REQUIREMENT_URL.format(event_id=700, role_id=12))

    assert response.status_code == 404
    assert calls == []


def test_a_missing_role_is_404_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_staffing_requirement", result=None)
    session.role = None

    response = _delete(api, REQUIREMENT_URL.format(event_id=700, role_id=12))

    assert response.status_code == 404
    assert calls == []


def test_an_unauthenticated_request_is_401_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_staffing_requirement", result=None)
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.delete(REQUIREMENT_URL.format(event_id=700, role_id=12))

    assert response.status_code == 401
    assert calls == []


# ==========================================================================
# Schema hygiene
# ==========================================================================


def test_the_response_models_forbid_extra_fields():
    import app.api.staffing_requirement_schemas as schemas

    for name in schemas.__all__:
        model = getattr(schemas, name)
        assert model.model_config.get("extra") == "forbid", name
        assert model.model_config.get("from_attributes") is not True, name
