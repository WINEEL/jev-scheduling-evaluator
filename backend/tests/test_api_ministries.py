"""The Admin's church-wide ministry list over HTTP (Task 79 §4).

Offline: no database. The same shape as ``tests/test_api_people.py`` -- this
file pins the **boundary**: 401 before anything else, a service's
``AuthorizationError`` surfacing as 403 rather than an empty list, and a
response carrying no field the domain did not put in it.

What the list *means* against real rows is
``tests/integration/test_pg_ministry_oversight.py``; who may see it is
``tests/test_services_ministry_directory.py``.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api import routes_ministries as routes
from app.config import get_settings
from app.main import app
from app.services.errors import AuthorizationError
from app.services.ministry_directory import (
    MinistryHeadSummary,
    MinistryOverview,
    MinistryPeriodSummary,
)

DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
MINISTRIES_URL = "/api/v1/ministries"


class _StubPerson:
    def __init__(self) -> None:
        self.id = 1
        self.church_id = 1
        self.display_name = "Ada Actor"
        self.is_admin = True
        self.deactivated_at = None
        self.ministry_memberships: list = []


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        return []


class RecordingSession:
    def __init__(self) -> None:
        self.person = _StubPerson()
        self.events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        return _Result(self.person)

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


@pytest.fixture
def session(monkeypatch) -> RecordingSession:
    recording = RecordingSession()
    monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
    return recording


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv(DEV_FLAG, "1")
    monkeypatch.delenv("APP_ENV", raising=False)
    get_settings.cache_clear()
    return TestClient(app)


def _stub(monkeypatch, result):
    calls: list[dict] = []

    def _fake(*args, **kwargs):
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(routes, "list_church_ministries", _fake)
    return calls


def _overview(**overrides) -> MinistryOverview:
    fields = {
        "ministry_id": 900,
        "name": "Example Ministry",
        "description": None,
        "deactivated_at": None,
        "heads": (),
        "active_member_count": 0,
        "period": None,
    }
    fields.update(overrides)
    return MinistryOverview(**fields)


class TestIdentityBoundary:
    def test_no_actor_is_401(self, client, session):
        response = client.get(MINISTRIES_URL)
        assert response.status_code == 401
        assert response.json()["detail"] == "Not authenticated."

    def test_401_not_an_empty_list(self, client, session):
        """A stranger must not be able to tell a church with no ministries
        from a closed door."""
        assert "ministries" not in client.get(MINISTRIES_URL).json()


class TestAuthorizationMapping:
    def test_a_refusal_is_403_not_an_empty_list(self, client, session, monkeypatch):
        """A ministry head and a volunteer both land here. Answering "no
        ministries" would be a lie a later change could turn into a leak."""
        _stub(
            monkeypatch,
            AuthorizationError("only an Admin may perform this action"),
        )
        response = client.get(MINISTRIES_URL, headers={HEADER: "1"})
        assert response.status_code == 403
        assert "ministries" not in response.json()

    def test_the_actor_is_never_a_request_parameter(self, client, session, monkeypatch):
        """No query string, path segment or body can name who is asking."""
        calls = _stub(monkeypatch, ())
        client.get(f"{MINISTRIES_URL}?actor_person_id=99", headers={HEADER: "1"})
        assert calls[0]["actor"] is session.person


class TestResponseMapping:
    def test_an_empty_church_is_an_empty_list_with_a_total(
        self, client, session, monkeypatch
    ):
        _stub(monkeypatch, ())
        body = client.get(MINISTRIES_URL, headers={HEADER: "1"}).json()
        assert body == {"ministries": [], "total": 0}

    def test_a_ministry_with_nothing_configured_reports_that_honestly(
        self, client, session, monkeypatch
    ):
        """No invented head, no invented period, no invented count."""
        _stub(monkeypatch, (_overview(),))
        row = client.get(MINISTRIES_URL, headers={HEADER: "1"}).json()["ministries"][0]
        assert row["heads"] == []
        assert row["active_member_count"] == 0
        assert row["period"] is None

    def test_a_full_row_maps_field_by_field(self, client, session, monkeypatch):
        _stub(
            monkeypatch,
            (
                _overview(
                    heads=(
                        MinistryHeadSummary(person_id=5, display_name="Head Person"),
                    ),
                    active_member_count=12,
                    period=MinistryPeriodSummary(
                        scheduling_period_id=77,
                        name="Q4",
                        start_date=datetime.date(2026, 10, 1),
                        end_date=datetime.date(2026, 12, 31),
                        is_current=True,
                        latest_version_number=2,
                        latest_version_status="DRAFT",
                    ),
                ),
            ),
        )
        row = client.get(MINISTRIES_URL, headers={HEADER: "1"}).json()["ministries"][0]
        assert row["heads"] == [{"person_id": 5, "display_name": "Head Person"}]
        assert row["active_member_count"] == 12
        assert row["period"]["name"] == "Q4"
        assert row["period"]["is_current"] is True
        assert row["period"]["latest_version_status"] == "DRAFT"

    def test_a_row_carries_exactly_the_documented_fields(
        self, client, session, monkeypatch
    ):
        _stub(monkeypatch, (_overview(),))
        row = client.get(MINISTRIES_URL, headers={HEADER: "1"}).json()["ministries"][0]
        assert set(row) == {
            "ministry_id", "name", "description", "deactivated_at", "heads",
            "active_member_count", "period",
        }

    def test_a_head_row_carries_no_contact_details(
        self, client, session, monkeypatch
    ):
        """An oversight list, not a contact directory: an Admin who needs
        somebody's details opens their Person record, where the rules about who
        may see what already live."""
        _stub(
            monkeypatch,
            (
                _overview(
                    heads=(
                        MinistryHeadSummary(person_id=5, display_name="Head Person"),
                    )
                ),
            ),
        )
        head = client.get(MINISTRIES_URL, headers={HEADER: "1"}).json()[
            "ministries"
        ][0]["heads"][0]
        assert set(head) == {"person_id", "display_name"}

    def test_include_inactive_reaches_the_service(self, client, session, monkeypatch):
        calls = _stub(monkeypatch, ())
        client.get(
            f"{MINISTRIES_URL}?include_inactive=false", headers={HEADER: "1"}
        )
        assert calls[0]["include_inactive"] is False


class TestSurface:
    def test_there_is_no_way_to_create_edit_or_archive_a_ministry(self, client):
        """The domain has no reviewed rule for it -- what happens to a
        ministry's periods, roster and past schedules when it is archived has
        never been decided -- so the verb does not exist yet."""
        spec = client.get("/openapi.json").json()
        operations = set(spec["paths"]["/api/v1/ministries"])
        assert operations == {"get"}
