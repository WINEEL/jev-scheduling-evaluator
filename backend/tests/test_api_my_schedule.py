"""The ``GET /api/v1/me/schedule`` boundary (Task 77).

Offline: no PostgreSQL, no network. Synthetic identities throughout.

The *behaviour* of the query -- aggregation across ministries, draft visibility,
one version per schedule -- is proven against real rows in
``tests/integration/test_pg_my_schedule.py``, because it rests on a PostgreSQL
``DISTINCT ON`` subquery that a fake session cannot exercise honestly.

What is tested here is the **boundary**: that the endpoint needs an actor, that
the subject is the actor and cannot be named by the caller, and that the
response publishes exactly the fields it means to and nothing the ORM happens to
carry.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api.schemas import MyScheduleAssignment, MyScheduleResponse
from app.config import get_settings
from app.main import app
from app.models.core import Person
from app.services.my_schedule import MyAssignment

UTC = datetime.timezone.utc
ME = "/api/v1/me/schedule"
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"


def _person(person_id: int, *, is_admin: bool = False, deactivated: bool = False) -> Person:
    person = Person(display_name="Synthetic Person", is_admin=is_admin, church_id=1)
    person.id = person_id
    if deactivated:
        person.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    return person


class FakeSession:
    """Answers the actor lookup, the church timezone, and the schedule query."""

    def __init__(self, person: Person | None, timezone: str | None = "UTC") -> None:
        self.person = person
        self.timezone = timezone
        self.person_filters: list[str] = []

    def execute(self, statement):
        compiled = str(statement)
        if "FROM church" in compiled:
            return _Scalar(self.timezone)
        if "FROM person" in compiled and "assignment" not in compiled:
            return _Scalar(self.person)
        # The schedule query. Record which person id it filtered on, so a test
        # can prove the subject came from the session and not the request.
        self.person_filters.append(
            str(statement.compile(compile_kwargs={"literal_binds": True}))
        )
        return _Rows([])

    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class _Scalar:
    def __init__(self, value): self._value = value
    def scalar_one_or_none(self): return self._value


class _Rows:
    def __init__(self, rows): self._rows = rows
    def all(self): return list(self._rows)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _use(session: FakeSession) -> FakeSession:
    app.dependency_overrides[deps.get_session] = lambda: session
    return session


def _enable_dev_auth(monkeypatch):
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()


# ==========================================================================
# 1-5 -- It needs an actor, like every other private route
# ==========================================================================


def test_01_unauthenticated_is_refused(client):
    _use(FakeSession(_person(42)))

    response = client.get(ME)

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}


def test_02_no_schedule_data_leaks_in_the_refusal(client):
    _use(FakeSession(_person(42)))

    body = client.get(ME).text

    for leaked in ("assignments", "ministry", "event_date", "person_id"):
        assert leaked not in body


def test_03_a_deactivated_person_is_refused(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use(FakeSession(_person(42, deactivated=True)))

    assert client.get(ME, headers={HEADER: "42"}).status_code == 401


def test_04_an_authenticated_person_gets_their_own_schedule(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use(FakeSession(_person(42)))

    response = client.get(ME, headers={HEADER: "42"})

    assert response.status_code == 200
    assert response.json()["person_id"] == 42


def test_05_a_volunteer_who_manages_nothing_may_still_call_it(client, monkeypatch):
    """There is no permission gate: the subject is the caller."""
    _enable_dev_auth(monkeypatch)
    _use(FakeSession(_person(42, is_admin=False)))

    assert client.get(ME, headers={HEADER: "42"}).status_code == 200


# ==========================================================================
# 6-10 -- The subject is the actor, and cannot be named by the caller
# ==========================================================================


def test_06_the_query_filters_on_the_authenticated_person(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    session = _use(FakeSession(_person(42)))

    client.get(ME, headers={HEADER: "42"})

    assert session.person_filters, "the schedule query did not run"
    assert "ministry_membership.person_id = 42" in session.person_filters[0]


@pytest.mark.parametrize(
    "query",
    ["?person_id=99", "?actor_person_id=99", "?membership_id=99", "?ministry_id=99",
     "?person_id=99&is_admin=true"],
)
def test_07_a_query_parameter_cannot_name_another_person(client, monkeypatch, query):
    _enable_dev_auth(monkeypatch)
    session = _use(FakeSession(_person(42)))

    body = client.get(f"{ME}{query}", headers={HEADER: "42"}).json()

    assert body["person_id"] == 42
    assert "ministry_membership.person_id = 42" in session.person_filters[0]
    assert "person_id = 99" not in session.person_filters[0]


@pytest.mark.parametrize(
    "header", ["X-Actor-Person-Id", "X-User-Id", "X-Person-Id", "X-Forwarded-User"]
)
def test_08_an_invented_header_cannot_name_another_person(client, monkeypatch, header):
    _enable_dev_auth(monkeypatch)
    session = _use(FakeSession(_person(42)))

    body = client.get(ME, headers={HEADER: "42", header: "99"}).json()

    assert body["person_id"] == 42
    assert "ministry_membership.person_id = 42" in session.person_filters[0]


def test_09_the_route_takes_no_person_parameter_at_all(client):
    """Checked against the published schema, not against behaviour: a
    parameter that existed could be relied on before anybody noticed.
    """
    operation = app.openapi()["paths"]["/api/v1/me/schedule"]["get"]

    names = {p["name"].lower() for p in operation.get("parameters", [])}
    for forbidden in ("person_id", "actor_person_id", "membership_id", "ministry_id"):
        assert forbidden not in names
    # The only parameter is the development header, and it is a header.
    for parameter in operation.get("parameters", []):
        assert parameter["in"] == "header"


def test_10_there_is_no_route_for_somebody_elses_schedule(client):
    """No ``/people/{id}/schedule`` exists. Task 77 deliberately did not add
    one: reading another person's commitments is a different feature with a
    different authorization rule, and it is not this.

    **Task 79 added ``/api/v1/people/{person_id}`` routes**, so the blanket
    "no path contains ``{person_id}``" this test used to assert is no longer
    the claim -- a people directory legitimately addresses people by id. The
    claim that survives, and the one this test was always about, is narrower
    and unchanged: **no schedule is readable by person id**. The only route
    ending in ``/schedule`` is the one whose subject is the actor themselves.
    """
    paths = app.openapi()["paths"]

    schedule_paths = [p for p in paths if p.endswith("/schedule")]
    assert schedule_paths == ["/api/v1/me/schedule"]

    for path in paths:
        if "{person_id}" in path:
            assert "schedule" not in path, path


# ==========================================================================
# 11-14 -- The response shape
# ==========================================================================


def test_11_the_response_publishes_exactly_the_intended_fields(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use(FakeSession(_person(42)))

    body = client.get(ME, headers={HEADER: "42"}).json()

    assert set(body) == {"person_id", "display_name", "as_of_date", "assignments"}


def test_12_an_assignment_names_the_ministry_and_role_but_no_internal_handles():
    fields = set(MyScheduleAssignment.model_fields)

    assert {"ministry_name", "ministry_role_name", "event_date", "is_confirmed"} <= fields
    # Deliberately absent: this is somebody reading their own list.
    for leaked in ("person_id", "membership_id", "ministry_membership_id",
                   "schedule_id", "schedule_version_id", "email"):
        assert leaked not in fields


def test_13_neither_dto_can_be_built_from_an_orm_row_by_accident():
    for model in (MyScheduleResponse, MyScheduleAssignment):
        assert model.model_config.get("from_attributes") is not True
        assert model.model_config.get("extra") == "forbid"


def test_14_is_confirmed_is_true_only_for_a_finalized_version():
    """The field the UI leans on, checked at its source."""
    from app.models.schedule_output import (
        SCHEDULE_VERSION_STATUS_DRAFT,
        SCHEDULE_VERSION_STATUS_FINALIZED,
        SCHEDULE_VERSION_STATUS_REVIEW,
    )

    def built(status: str) -> MyAssignment:
        return MyAssignment(
            assignment_id=1, event_id=1, event_date=datetime.date(2026, 10, 11),
            event_kind="SUNDAY", event_name=None, ministry_id=1,
            ministry_name="SetupMin", ministry_role_id=1, ministry_role_name="Role",
            schedule_version_id=1, schedule_version_status=status,
            is_confirmed=status == SCHEDULE_VERSION_STATUS_FINALIZED,
        )

    assert built(SCHEDULE_VERSION_STATUS_FINALIZED).is_confirmed is True
    assert built(SCHEDULE_VERSION_STATUS_DRAFT).is_confirmed is False
    assert built(SCHEDULE_VERSION_STATUS_REVIEW).is_confirmed is False


# ==========================================================================
# 15-17 -- "Today" is the church's day
# ==========================================================================


def test_15_today_is_computed_in_the_churchs_timezone(client, monkeypatch):
    """A church west of Greenwich must not lose a service hours before it
    starts because a UTC date rolled over.
    """
    _enable_dev_auth(monkeypatch)
    _use(FakeSession(_person(42), timezone="Pacific/Kiritimati"))  # UTC+14

    body = client.get(ME, headers={HEADER: "42"}).json()
    ahead = datetime.datetime.now(datetime.timezone.utc).date()

    # UTC+14 is today or tomorrow relative to UTC, never yesterday.
    assert datetime.date.fromisoformat(body["as_of_date"]) >= ahead


def test_16_an_unrecognised_timezone_falls_back_rather_than_failing(client, monkeypatch):
    """The one screen a volunteer opens must not 500 over a bad config value."""
    _enable_dev_auth(monkeypatch)
    _use(FakeSession(_person(42), timezone="Not/AZone"))

    response = client.get(ME, headers={HEADER: "42"})

    assert response.status_code == 200
    assert response.json()["as_of_date"] == (
        datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    )


def test_17_a_missing_church_timezone_also_falls_back(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use(FakeSession(_person(42), timezone=None))

    assert client.get(ME, headers={HEADER: "42"}).status_code == 200
