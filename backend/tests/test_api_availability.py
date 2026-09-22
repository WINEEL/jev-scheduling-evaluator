"""Availability management over HTTP (Task 56).

Offline: no database. The Session is a recording stand-in answering the few
statements these routes issue, and :mod:`app.services.availability` is
stubbed where the subject is HTTP mapping rather than domain behaviour --
that behaviour has its own dedicated coverage in
``tests/test_services_availability.py``.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import routes_availability as routes
from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.models.scheduling_input import EVENT_KIND_SUNDAY_SERVICE
from app.services.availability import EventAvailability, MembershipAvailability
from app.services.errors import AuthorizationError, InvalidOperationError

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
AVAILABILITY_URL = "/api/v1/events/{id}/availability"
MEMBER_AVAILABILITY_URL = "/api/v1/events/{event_id}/availability/{membership_id}"


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


class _StubPeriod:
    def __init__(self, id: int = 12, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.name = "Q4 2026"
        self.availability_locked_at = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubAvailability:
    def __init__(self, availability_state: str) -> None:
        self.availability_state = availability_state


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    """A stand-in request Session, matching earlier tasks' own pattern
    exactly.
    """

    def __init__(self, *, person=None, event=None, membership=None, period=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.event = _StubEvent() if event is None else event
        self.membership = _StubMembership() if membership is None else membership
        self.period = _StubPeriod() if period is None else period
        self.transaction_events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "FROM ministry_membership" in sql:
            return _ScalarResult(self.membership)
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
        monkeypatch, "list_event_availability",
        result=EventAvailability(
            event_id=700, event_date=datetime.date(2026, 10, 4), event_name=None,
            event_kind=EVENT_KIND_SUNDAY_SERVICE, ministry_id=900,
            availability_locked_at=None,
            memberships=(
                MembershipAvailability(118, 42, "Ben", None, None, "AVAILABLE"),
            ),
        ),
    )


@pytest.fixture
def setter(monkeypatch):
    return _stub(monkeypatch, "set_availability", result=_StubAvailability("AVAILABLE"))


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
# GET /events/{id}/availability
# ==========================================================================


def test_admin_may_list_event_availability(api, session, lister):
    session.person.is_admin = True

    response = _get(api, AVAILABILITY_URL.format(id=700))

    assert response.status_code == 200
    assert lister[0]["actor"] is session.person
    assert lister[0]["event"] is session.event
    body = response.json()
    assert body["event_id"] == 700
    assert body["availability_locked_at"] is None
    assert body["memberships"][0] == {
        "ministry_membership_id": 118, "person_id": 42, "person_display_name": "Ben",
        "membership_deactivated_at": None, "person_deactivated_at": None,
        "availability_state": "AVAILABLE",
    }


def test_ministrys_own_head_may_list_event_availability(api, session, lister):
    assert _get(api, AVAILABILITY_URL.format(id=700)).status_code == 200


def test_include_inactive_query_param_is_forwarded(api, session, lister):
    _get(api, AVAILABILITY_URL.format(id=700), params={"include_inactive": "true"})

    assert lister[0]["include_inactive"] is True


def test_include_inactive_defaults_to_false(api, session, lister):
    _get(api, AVAILABILITY_URL.format(id=700))

    assert lister[0]["include_inactive"] is False


def test_no_response_reports_null_not_a_placeholder(api, session, monkeypatch):
    _stub(
        monkeypatch, "list_event_availability",
        result=EventAvailability(
            event_id=700, event_date=datetime.date(2026, 10, 4), event_name=None,
            event_kind=EVENT_KIND_SUNDAY_SERVICE, ministry_id=900,
            availability_locked_at=None,
            memberships=(MembershipAvailability(118, 42, "Ben", None, None, None),),
        ),
    )

    body = _get(api, AVAILABILITY_URL.format(id=700)).json()

    assert body["memberships"][0]["availability_state"] is None


def test_a_locked_period_is_reported(api, session, monkeypatch):
    locked_at = datetime.datetime(2026, 9, 1, tzinfo=UTC)
    _stub(
        monkeypatch, "list_event_availability",
        result=EventAvailability(
            event_id=700, event_date=datetime.date(2026, 10, 4), event_name=None,
            event_kind=EVENT_KIND_SUNDAY_SERVICE, ministry_id=900,
            availability_locked_at=locked_at, memberships=(),
        ),
    )

    body = _get(api, AVAILABILITY_URL.format(id=700)).json()

    assert body["availability_locked_at"] == "2026-09-01T00:00:00Z"


def test_a_head_of_another_ministry_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _get(api, AVAILABILITY_URL.format(id=700))

    assert response.status_code == 403
    assert response.json() == {"detail": "you do not manage this ministry"}


def test_a_normal_member_is_403_on_list(api, session, lister):
    lister.behaviour["raises"] = AuthorizationError("not a manager")

    assert _get(api, AVAILABILITY_URL.format(id=700)).status_code == 403


def test_a_missing_event_is_404_on_list(api, session, lister):
    session.event = None

    response = _get(api, AVAILABILITY_URL.format(id=700))

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found."}
    assert lister == []


def test_an_unauthenticated_request_is_401_on_list(api, session, lister, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.get(AVAILABILITY_URL.format(id=700))

    assert response.status_code == 401
    assert lister == []


# ==========================================================================
# PUT /events/{event_id}/availability/{membership_id}
# ==========================================================================


@pytest.mark.parametrize("state", ["AVAILABLE", "BACKUP", "UNAVAILABLE"])
def test_admin_may_record_any_of_the_three_states(api, session, setter, state):
    session.person.is_admin = True
    setter.behaviour["result"] = _StubAvailability(state)

    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": state},
    )

    assert response.status_code == 200
    assert setter[0]["actor"] is session.person
    assert setter[0]["event"] is session.event
    assert setter[0]["membership"] is session.membership
    assert setter[0]["availability_state"] == state
    body = response.json()
    assert body["ministry_membership_id"] == 118
    assert body["availability_state"] == state


def test_ministrys_own_head_may_set_availability(api, session, setter):
    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "AVAILABLE"},
    )
    assert response.status_code == 200


def test_a_reason_is_forwarded(api, session, setter):
    _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "UNAVAILABLE", "reason": "Out of town."},
    )

    assert setter[0]["reason"] == "Out of town."


def test_an_invalid_state_is_422(api, session, setter):
    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "MAYBE"},
    )

    assert response.status_code == 422
    assert setter == []


def test_null_is_rejected_on_put_clearing_is_a_separate_verb(api, session, setter):
    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": None},
    )

    assert response.status_code == 422
    assert setter == []


def test_a_head_of_another_ministry_is_403_on_put(api, session, setter):
    setter.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "AVAILABLE"},
    )

    assert response.status_code == 403


def test_cross_ministry_pair_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "membership and event must belong to the same ministry"
    )

    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "AVAILABLE"},
    )

    assert response.status_code == 409


def test_locked_period_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "cannot change availability: this scheduling period's availability has been locked"
    )

    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "AVAILABLE"},
    )

    assert response.status_code == 409


def test_deactivated_target_is_409_on_put(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError(
        "cannot record availability for a deactivated membership"
    )

    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "AVAILABLE"},
    )

    assert response.status_code == 409


def test_a_missing_event_is_404_on_put(api, session, setter):
    session.event = None

    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "AVAILABLE"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Event not found."}
    assert setter == []


def test_a_missing_membership_is_404_on_put(api, session, setter):
    session.membership = None

    response = _put(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "AVAILABLE"},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Ministry membership not found."}
    assert setter == []


def test_an_unauthenticated_request_is_401_on_put(api, session, setter, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.put(
        MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        json={"availability_state": "AVAILABLE"},
    )

    assert response.status_code == 401
    assert setter == []


# ==========================================================================
# DELETE /events/{event_id}/availability/{membership_id}
# ==========================================================================


def test_admin_may_clear_availability(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_availability", result=None)
    session.person.is_admin = True

    response = _delete(api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118))

    assert response.status_code == 204
    assert response.content == b""
    assert calls[0]["availability_state"] is None
    assert calls[0]["event"] is session.event
    assert calls[0]["membership"] is session.membership


def test_clearing_an_already_absent_response_is_still_204(api, session, monkeypatch):
    """The service's own no-op path, not a 404 -- the event and membership
    both still exist, only the response between them does not."""
    calls = _stub(monkeypatch, "set_availability", result=None)

    response = _delete(api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118))

    assert response.status_code == 204


def test_a_reason_query_param_is_forwarded_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_availability", result=None)

    _delete(
        api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118),
        params={"reason": "No longer needed."},
    )

    assert calls[0]["reason"] == "No longer needed."


def test_a_head_of_another_ministry_is_403_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_availability", result=None)
    calls.behaviour["raises"] = AuthorizationError("you do not manage this ministry")

    response = _delete(api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118))

    assert response.status_code == 403


def test_locked_period_is_409_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_availability", result=None)
    calls.behaviour["raises"] = InvalidOperationError(
        "cannot change availability: this scheduling period's availability has been locked"
    )

    response = _delete(api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118))

    assert response.status_code == 409


def test_a_missing_event_is_404_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_availability", result=None)
    session.event = None

    response = _delete(api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118))

    assert response.status_code == 404
    assert calls == []


def test_a_missing_membership_is_404_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_availability", result=None)
    session.membership = None

    response = _delete(api, MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118))

    assert response.status_code == 404
    assert calls == []


def test_an_unauthenticated_request_is_401_on_delete(api, session, monkeypatch):
    calls = _stub(monkeypatch, "set_availability", result=None)
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    response = api.delete(MEMBER_AVAILABILITY_URL.format(event_id=700, membership_id=118))

    assert response.status_code == 401
    assert calls == []


# ==========================================================================
# POST /scheduling-periods/{id}/availability-lock  (Task 72)
# ==========================================================================


LOCK_URL = "/api/v1/scheduling-periods/{id}/availability-lock"
LOCKED_AT = datetime.datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def locker(monkeypatch):
    """:func:`lock_availability`, stubbed. It returns the period it locked, so
    the stand-in stamps the timestamp the way the real service would.
    """
    calls = _CallLog()
    behaviour: dict = {"raises": None}

    def fake(session, *, actor, period, reason=None):
        calls.append({"session": session, "actor": actor, "period": period, "reason": reason})
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        if period.availability_locked_at is None:
            period.availability_locked_at = LOCKED_AT
        return period

    monkeypatch.setattr(routes, "lock_availability", fake)
    calls.behaviour = behaviour
    return calls


def _post(api, url, *, actor_id: int = 1, **kwargs):
    return api.post(url, headers={HEADER: str(actor_id)}, **kwargs)


def test_locking_a_period_returns_its_new_lock_state(api, session, locker):
    response = _post(api, LOCK_URL.format(id=12), json={})

    assert response.status_code == 200
    assert locker[0]["actor"] is session.person
    assert locker[0]["period"] is session.period
    assert response.json() == {
        "scheduling_period_id": 12,
        "scheduling_period_name": "Q4 2026",
        "ministry_id": 900,
        "availability_locked_at": LOCKED_AT.isoformat().replace("+00:00", "Z"),
    }


def test_the_body_is_optional(api, session, locker):
    """A head clicking a button sends no note, and should not have to."""
    assert _post(api, LOCK_URL.format(id=12)).status_code == 200
    assert locker[0]["reason"] is None


def test_a_reason_is_forwarded_to_the_audit_trail(api, session, locker):
    _post(api, LOCK_URL.format(id=12), json={"reason": "Answers collected"})

    assert locker[0]["reason"] == "Answers collected"


def test_locking_an_already_locked_period_is_not_an_error(api, session, locker):
    """The service is idempotent and returns the original instant; repeating
    the call must not become a 409 about a state that is already what the
    caller asked for.
    """
    earlier = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
    session.period.availability_locked_at = earlier

    response = _post(api, LOCK_URL.format(id=12), json={})

    assert response.status_code == 200
    assert response.json()["availability_locked_at"] == earlier.isoformat().replace(
        "+00:00", "Z"
    )


def test_an_unknown_period_is_404_and_the_service_is_never_called(api, session, locker):
    session.period = None

    assert _post(api, LOCK_URL.format(id=12), json={}).status_code == 404
    assert locker == []


def test_a_head_of_another_ministry_is_refused(api, session, locker):
    locker.behaviour["raises"] = AuthorizationError("not this ministry")

    assert _post(api, LOCK_URL.format(id=12), json={}).status_code == 403


def test_a_blank_reason_is_the_services_own_refusal(api, session, locker):
    locker.behaviour["raises"] = InvalidOperationError("reason must not be blank")

    assert _post(api, LOCK_URL.format(id=12), json={"reason": " "}).status_code == 409


def test_an_unrecognized_body_field_is_rejected(api, session, locker):
    response = _post(api, LOCK_URL.format(id=12), json={"locked": False})

    assert response.status_code == 422
    assert locker == []


def test_locking_requires_an_actor(api, session, locker, monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    assert api.post(LOCK_URL.format(id=12), json={}).status_code == 401
    assert locker == []


# ==========================================================================
# Schema hygiene
# ==========================================================================


def test_the_response_models_forbid_extra_fields():
    import app.api.availability_schemas as schemas

    for name in schemas.__all__:
        model = getattr(schemas, name)
        assert model.model_config.get("extra") == "forbid", name
        assert model.model_config.get("from_attributes") is not True, name
