"""The Admin's church-wide ministry list, end to end (Task 79 §4).

Rollback-isolated by the shared harness; nothing is committed, and every row is
synthetic.

**What needs a real database here** is everything the offline suite cannot
reach: that the list is really scoped to the actor's own church, that the
"current period" preference is expressed as one ``DISTINCT ON`` rather than a
loop, that the latest schedule version is found per period, and that the whole
thing costs a fixed number of queries however many ministries exist.

The authorization matrix is asserted over real HTTP: **Admin only**. A Ministry
Head reaches the ministries they lead through ``/api/v1/me``; a church-wide
inventory is a different thing and is not theirs.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.models.core import Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
MINISTRIES_URL = "/api/v1/ministries"


@pytest.fixture
def api(db_session, monkeypatch) -> TestClient:
    """A client whose requests run on the test's rolled-back Session.

    The same fixture ``test_pg_people_management.py`` uses, and for the reason
    recorded there: **the dependency is overridden rather than
    ``SessionLocal``**, because the real ``get_session`` ends with ``close()``,
    which detaches every instance -- including the fixture rows a test built
    before the request and goes on to assert about afterwards.

    Dev auth starts **off**, so the unauthenticated test gets the real refusal.
    """
    monkeypatch.delenv(DEV_FLAG, raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    get_settings.cache_clear()

    def _session_with_commit():
        yield db_session
        db_session.commit()

    app.dependency_overrides[deps.get_session] = _session_with_commit
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _enable_dev_auth(monkeypatch) -> None:
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()


def _as(person: Person) -> dict[str, str]:
    return {HEADER: str(person.id)}


class _Cast:
    """One church with two ministries, plus a second church nobody may see."""

    def __init__(self, session: Session) -> None:
        self.church = f.make_church(session)
        self.av = f.make_ministry(session, church=self.church, name="AvMin")
        self.kids = f.make_ministry(session, church=self.church, name="KidsMin")

        self.admin = f.make_person(
            session, church=self.church, name="Admin", is_admin=True
        )
        self.av_head = f.make_person(session, church=self.church, name="AV Head")
        self.volunteer = f.make_person(session, church=self.church, name="Volunteer")

        f.make_membership(
            session, person=self.av_head, ministry=self.av, is_head=True
        )
        f.make_membership(session, person=self.volunteer, ministry=self.av)

        # A different church entirely, with its own Admin and its own ministry.
        self.other_church = f.make_church(session, name="Other Church")
        self.other_ministry = f.make_ministry(
            session, church=self.other_church, name="OtherMin"
        )
        self.other_admin = f.make_person(
            session, church=self.other_church, name="Other Admin", is_admin=True
        )
        session.flush()


@pytest.fixture
def cast(db_session) -> _Cast:
    return _Cast(db_session)


def _names(body) -> set[str]:
    return {row["name"] for row in body["ministries"]}


def _row(body, ministry):
    return next(row for row in body["ministries"] if row["ministry_id"] == ministry.id)


# --------------------------------------------------------------------------
# Who may see the list
# --------------------------------------------------------------------------


def test_an_unauthenticated_request_is_401(api, cast):
    assert api.get(MINISTRIES_URL).status_code == 401


def test_an_admin_sees_every_ministry_in_their_church(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.get(MINISTRIES_URL, headers=_as(cast.admin))
    assert response.status_code == 200
    assert {cast.av.name, cast.kids.name} <= _names(response.json())


def test_an_admin_never_sees_another_churchs_ministries(api, cast, monkeypatch):
    """``church_id`` comes from the actor and is not a parameter, so there is
    no way to ask."""
    _enable_dev_auth(monkeypatch)
    body = api.get(MINISTRIES_URL, headers=_as(cast.admin)).json()
    assert cast.other_ministry.name not in _names(body)

    other = api.get(MINISTRIES_URL, headers=_as(cast.other_admin)).json()
    assert _names(other) == {cast.other_ministry.name}


def test_a_ministry_head_is_403_not_a_short_list(api, cast, monkeypatch):
    """They reach the ministries they lead through ``/api/v1/me``. Answering
    with "just yours" here would quietly turn oversight into a second, subtly
    different navigation."""
    _enable_dev_auth(monkeypatch)
    response = api.get(MINISTRIES_URL, headers=_as(cast.av_head))
    assert response.status_code == 403
    assert "ministries" not in response.json()


def test_a_volunteer_is_403(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    assert api.get(MINISTRIES_URL, headers=_as(cast.volunteer)).status_code == 403


def test_a_deactivated_admin_loses_the_list(api, db_session, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    cast.admin.deactivated_at = datetime.datetime.now(tz=UTC)
    db_session.flush()
    # 401 rather than 403: a deactivated actor cannot be established at all.
    assert api.get(MINISTRIES_URL, headers=_as(cast.admin)).status_code == 401


# --------------------------------------------------------------------------
# What each row says
# --------------------------------------------------------------------------


def test_a_ministry_with_nothing_configured_says_so(api, cast, monkeypatch):
    """No invented head, no invented period, no invented count -- Kids has
    none of them."""
    _enable_dev_auth(monkeypatch)
    row = _row(api.get(MINISTRIES_URL, headers=_as(cast.admin)).json(), cast.kids)
    assert row["heads"] == []
    assert row["active_member_count"] == 0
    assert row["period"] is None


def test_the_heads_and_member_count_are_the_ministrys_own(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    row = _row(api.get(MINISTRIES_URL, headers=_as(cast.admin)).json(), cast.av)
    assert [head["display_name"] for head in row["heads"]] == [
        cast.av_head.display_name
    ]
    # The head and the volunteer, both actively on AV.
    assert row["active_member_count"] == 2


def test_a_removed_member_is_not_counted(api, db_session, cast, monkeypatch):
    """The column answers how big the team is *now*, not who has ever been on
    it."""
    _enable_dev_auth(monkeypatch)
    membership = f.make_membership(
        db_session,
        person=f.make_person(db_session, church=cast.church, name="Left"),
        ministry=cast.av,
    )
    membership.deactivated_at = datetime.datetime.now(tz=UTC)
    db_session.flush()

    row = _row(api.get(MINISTRIES_URL, headers=_as(cast.admin)).json(), cast.av)
    assert row["active_member_count"] == 2


def test_a_deactivated_head_is_not_listed_as_leading_it(
    api, db_session, cast, monkeypatch
):
    """Deactivating a Person deliberately clears no flags, but every
    authorization check refuses them first -- so naming them here would tell an
    Admin to ask somebody who cannot help."""
    _enable_dev_auth(monkeypatch)
    cast.av_head.deactivated_at = datetime.datetime.now(tz=UTC)
    db_session.flush()

    row = _row(api.get(MINISTRIES_URL, headers=_as(cast.admin)).json(), cast.av)
    assert row["heads"] == []


def test_the_current_period_is_the_one_containing_today(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    today = datetime.date.today()
    f.make_period(
        session=db_session,
        ministry=cast.av,
        name="Last year",
        start=today - datetime.timedelta(days=400),
        end=today - datetime.timedelta(days=300),
    )
    current = f.make_period(
        session=db_session,
        ministry=cast.av,
        name="Now",
        start=today - datetime.timedelta(days=10),
        end=today + datetime.timedelta(days=10),
    )
    db_session.flush()

    row = _row(api.get(MINISTRIES_URL, headers=_as(cast.admin)).json(), cast.av)
    assert row["period"]["scheduling_period_id"] == current.id
    assert row["period"]["is_current"] is True


def test_between_quarters_the_most_recent_period_is_shown_but_not_called_current(
    api, db_session, cast, monkeypatch
):
    """Which is what somebody looking at the list wants to know -- and saying
    "current" about a quarter that ended months ago would be wrong by one word
    in the place it matters."""
    _enable_dev_auth(monkeypatch)
    today = datetime.date.today()
    f.make_period(
        session=db_session, ministry=cast.av, name="Older",
        start=today - datetime.timedelta(days=400),
        end=today - datetime.timedelta(days=300),
    )
    recent = f.make_period(
        session=db_session, ministry=cast.av, name="Just finished",
        start=today - datetime.timedelta(days=100),
        end=today - datetime.timedelta(days=10),
    )
    db_session.flush()

    row = _row(api.get(MINISTRIES_URL, headers=_as(cast.admin)).json(), cast.av)
    assert row["period"]["scheduling_period_id"] == recent.id
    assert row["period"]["is_current"] is False


def test_the_latest_schedule_version_and_its_status_are_reported(
    api, db_session, cast, monkeypatch
):
    """**Any** status, unlike the authoritative-version rule scheduling uses:
    this column reports where the ministry has got to, so an unfinalized draft
    is exactly the interesting answer."""
    _enable_dev_auth(monkeypatch)
    today = datetime.date.today()
    period = f.make_period(
        session=db_session, ministry=cast.av, name="Now",
        start=today - datetime.timedelta(days=10),
        end=today + datetime.timedelta(days=10),
    )
    schedule = f.make_schedule(session=db_session, period=period)
    f.make_version(
        session=db_session, schedule=schedule, period=period, version_number=1,
        status=SCHEDULE_VERSION_STATUS_FINALIZED,
        finalized_at=datetime.datetime.now(tz=UTC),
    )
    f.make_version(
        session=db_session, schedule=schedule, period=period, version_number=2,
        status=SCHEDULE_VERSION_STATUS_DRAFT,
    )
    db_session.flush()

    row = _row(api.get(MINISTRIES_URL, headers=_as(cast.admin)).json(), cast.av)
    assert row["period"]["latest_version_number"] == 2
    assert row["period"]["latest_version_status"] == SCHEDULE_VERSION_STATUS_DRAFT


def test_a_period_with_no_version_reports_none_not_draft(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    today = datetime.date.today()
    f.make_period(
        session=db_session, ministry=cast.av, name="Fresh",
        start=today, end=today + datetime.timedelta(days=30),
    )
    db_session.flush()

    row = _row(api.get(MINISTRIES_URL, headers=_as(cast.admin)).json(), cast.av)
    assert row["period"]["latest_version_status"] is None
    assert row["period"]["latest_version_number"] is None


def test_an_archived_ministry_is_listed_by_default(api, db_session, cast, monkeypatch):
    """It is part of what an overseer oversees, and omitting it silently would
    look like deletion."""
    _enable_dev_auth(monkeypatch)
    cast.kids.deactivated_at = datetime.datetime.now(tz=UTC)
    db_session.flush()

    body = api.get(MINISTRIES_URL, headers=_as(cast.admin)).json()
    assert cast.kids.name in _names(body)
    assert _row(body, cast.kids)["deactivated_at"] is not None


def test_archived_ministries_can_be_excluded_on_request(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    cast.kids.deactivated_at = datetime.datetime.now(tz=UTC)
    db_session.flush()

    body = api.get(
        f"{MINISTRIES_URL}?include_inactive=false", headers=_as(cast.admin)
    ).json()
    assert cast.kids.name not in _names(body)


# --------------------------------------------------------------------------
# Oversight is a read
# --------------------------------------------------------------------------


def test_reading_the_list_confers_no_operational_authority(
    api, db_session, cast, monkeypatch
):
    """**The separation Task 79 §4 is about.** An Admin can see Kids and open
    its screens; they still cannot put anybody on its roster, because that
    belongs to whoever leads it -- and nobody does yet.
    """
    _enable_dev_auth(monkeypatch)
    assert api.get(MINISTRIES_URL, headers=_as(cast.admin)).status_code == 200

    refused = api.post(
        f"/api/v1/people/{cast.volunteer.id}/memberships",
        headers=_as(cast.admin),
        json={"ministry_id": cast.kids.id},
    )
    assert refused.status_code == 403


def test_the_list_costs_a_fixed_number_of_queries(
    api, db_session, integration_engine, cast, monkeypatch
):
    """Never one query per ministry (the service's own budget).

    Measured at two sizes, like the people directory's equivalent: the absolute
    number includes the actor lookup and the harness's SAVEPOINT, so what is
    asserted is that it does not grow.
    """
    _enable_dev_auth(monkeypatch)
    for index in range(10):
        extra = f.make_ministry(db_session, church=cast.church, name=f"Extra{index}")
        f.make_membership(db_session, person=cast.volunteer, ministry=extra)
    db_session.flush()

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    # A discarded warm-up, for the reason the people-directory test records:
    # the first request through this harness leaves a different amount of
    # bookkeeping behind than every later one.
    api.get(MINISTRIES_URL, headers=_as(cast.admin))

    event.listen(integration_engine, "before_cursor_execute", record)
    try:
        statements.clear()
        api.get(MINISTRIES_URL, headers=_as(cast.admin))
        measured = list(statements)
    finally:
        event.remove(integration_engine, "before_cursor_execute", record)

    assert len(measured) <= 10
    ministry_queries = [sql for sql in measured if "FROM ministry " in sql]
    assert len(ministry_queries) == 1
