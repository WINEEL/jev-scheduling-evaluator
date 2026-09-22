"""The identity boundary against real PostgreSQL rows (Task 36).

The offline suite proves the gate with a fake session. What needs a database is
the other half: that the actor lookup and the headed-ministries query really
return these rows, and that a Person who genuinely exists is *still* refused
when development auth is off.

Dev auth is enabled per test by setting the environment variable and clearing
the settings cache — never in `.env.test`, so it cannot leak into any other
run. Rollback-isolated by the shared harness; nothing is committed.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"


@pytest.fixture
def api(db_session, monkeypatch) -> TestClient:
    """A client whose requests run on the test's rolled-back Session.

    Only the Session is overridden — the dev-auth gate, the header parsing and
    the actor lookup are all the real ones, running against real rows.
    """
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _enable_dev_auth(monkeypatch) -> None:
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()


def _member(session, *, name: str, is_admin: bool = False, deactivated: bool = False):
    church = f.make_church(session)
    person = f.make_person(
        session, church=church, name=name, is_admin=is_admin, deactivated=deactivated,
    )
    session.flush()
    return church, person


# --------------------------------------------------------------------------
# A -- a real active Person who heads a real ministry
# --------------------------------------------------------------------------


def test_a_an_active_head_gets_their_identity_from_real_rows(api, db_session, monkeypatch):
    church, person = _member(db_session, name="Head")
    setup = f.make_ministry(db_session, church=church, name="Setup")
    f.make_membership(db_session, person=person, ministry=setup, is_head=True)
    db_session.flush()
    _enable_dev_auth(monkeypatch)

    response = api.get("/api/v1/me", headers={HEADER: str(person.id)})

    assert response.status_code == 200
    assert response.json() == {
        "person_id": person.id,
        "display_name": person.display_name,
        "is_admin": False,
        "headed_ministries": [{"ministry_id": setup.id, "name": setup.name}],
    }
    # Nothing the row carries beyond the contract escaped.
    assert set(response.json()) == {
        "person_id", "display_name", "is_admin", "headed_ministries",
    }


def test_a2_several_headed_ministries_come_back_ordered_by_name(api, db_session, monkeypatch):
    church, person = _member(db_session, name="Head")
    # Created in an order that does not match the expected output.
    setup = f.make_ministry(db_session, church=church, name="ZZ Setup")
    av = f.make_ministry(db_session, church=church, name="AA AV")
    for ministry in (setup, av):
        f.make_membership(db_session, person=person, ministry=ministry, is_head=True)
    db_session.flush()
    _enable_dev_auth(monkeypatch)

    body = api.get("/api/v1/me", headers={HEADER: str(person.id)}).json()

    assert [m["ministry_id"] for m in body["headed_ministries"]] == [av.id, setup.id]


def test_a3_an_admin_is_reported_as_admin_without_invented_headships(
    api, db_session, monkeypatch
):
    church, admin = _member(db_session, name="Admin", is_admin=True)
    f.make_ministry(db_session, church=church, name="Setup")
    db_session.flush()
    _enable_dev_auth(monkeypatch)

    body = api.get("/api/v1/me", headers={HEADER: str(admin.id)}).json()

    assert body["is_admin"] is True
    assert body["headed_ministries"] == []


# --------------------------------------------------------------------------
# B -- an inactive Person cannot authenticate
# --------------------------------------------------------------------------


def test_b_a_deactivated_person_cannot_authenticate(api, db_session, monkeypatch):
    church, departed = _member(db_session, name="Departed", deactivated=True)
    setup = f.make_ministry(db_session, church=church, name="Setup")
    db_session.flush()
    _enable_dev_auth(monkeypatch)

    response = api.get("/api/v1/me", headers={HEADER: str(departed.id)})

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}


def test_b2_a_person_id_nobody_has_is_refused_identically(api, db_session, monkeypatch):
    """Same response as the deactivated case, so the endpoint cannot be used to
    discover which Person ids exist.
    """
    _member(db_session, name="Somebody")
    _enable_dev_auth(monkeypatch)

    response = api.get("/api/v1/me", headers={HEADER: "999999999"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}


# --------------------------------------------------------------------------
# C -- an ordinary member heads nothing
# --------------------------------------------------------------------------


def test_c_an_ordinary_member_has_an_empty_headed_ministries_list(
    api, db_session, monkeypatch
):
    church, person = _member(db_session, name="Volunteer")
    setup = f.make_ministry(db_session, church=church, name="Setup")
    f.make_membership(db_session, person=person, ministry=setup, is_head=False)
    db_session.flush()
    _enable_dev_auth(monkeypatch)

    body = api.get("/api/v1/me", headers={HEADER: str(person.id)}).json()

    assert body["person_id"] == person.id
    assert body["headed_ministries"] == []


def test_c2_a_deactivated_head_membership_is_excluded(api, db_session, monkeypatch):
    """The database's own CHECK forbids an inactive membership from carrying
    head authority, so the flag is cleared as the membership is deactivated —
    and the query filters on both, which is what keeps them agreeing.
    """
    church, person = _member(db_session, name="Former Head")
    setup = f.make_ministry(db_session, church=church, name="Setup")
    membership = f.make_membership(
        db_session, person=person, ministry=setup, is_head=True,
    )
    db_session.flush()
    membership.is_ministry_head = False
    membership.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    db_session.flush()
    _enable_dev_auth(monkeypatch)

    body = api.get("/api/v1/me", headers={HEADER: str(person.id)}).json()

    assert body["headed_ministries"] == []


# --------------------------------------------------------------------------
# D -- the gate holds for a Person who genuinely exists
# --------------------------------------------------------------------------


def test_d_a_real_person_is_refused_while_dev_auth_is_disabled(api, db_session):
    """The security boundary over real data: the Person exists, is active, and
    heads a ministry — and the request is still unauthenticated, because the
    flag is not set. This is the deployed configuration.
    """
    church, person = _member(db_session, name="Head")
    setup = f.make_ministry(db_session, church=church, name="Setup")
    f.make_membership(db_session, person=person, ministry=setup, is_head=True)
    db_session.flush()

    assert get_settings().dev_actor_auth_enabled is False

    response = api.get("/api/v1/me", headers={HEADER: str(person.id)})

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}
    assert "display_name" not in response.text


def test_d2_health_needs_no_actor_and_still_works(api):
    response = api.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_the_request_writes_nothing(api, db_session, monkeypatch):
    from sqlalchemy import text

    church, person = _member(db_session, name="Head")
    setup = f.make_ministry(db_session, church=church, name="Setup")
    f.make_membership(db_session, person=person, ministry=setup, is_head=True)
    db_session.flush()
    _enable_dev_auth(monkeypatch)

    before = db_session.execute(text("SELECT count(*) FROM audit_event")).scalar_one()
    api.get("/api/v1/me", headers={HEADER: str(person.id)})

    assert db_session.execute(
        text("SELECT count(*) FROM audit_event")
    ).scalar_one() == before
    assert len(db_session.new) == 0
    assert len(db_session.dirty) == 0
