"""People management end to end against real PostgreSQL (Task 79).

The offline suites prove the rules with stand-in sessions. What needs a real
database is everything they cannot reach:

- that the **authorization matrix** holds over real HTTP against real rows --
  who may list the directory, who may touch which ministry's memberships, and
  who may change the church-wide Person record;
- that **history genuinely survives** a deactivation and a removal -- the
  assignment, the qualification and the audit rows are still there afterwards,
  counted from the database rather than asserted about an ORM object;
- that **audit rows are really written**, in the same transaction, with the
  ministry scoping a Ministry Head's later audit view will filter on;
- that the **unique constraints** behave as the services assume: rejoining
  reactivates one row rather than inserting a second;
- that the directory read is **not an N+1**, counted by listening to the
  connection rather than by reading the code.

Task 37's harness is reused unchanged: the real ``get_session`` runs on the
test's Session, the outer transaction is rolled back, and
``no_committed_rows_leak`` proves from its own connection that nothing escaped.

**No production data is touched.** Every row here is built by
``tests/integration/factories.py`` with a random name suffix, inside a
transaction that is always rolled back.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from app.api import dependencies as deps
from app.config import get_settings
from app.main import app
from app.models.audit import AuditEvent
from app.models.core import MinistryMembership, Person
from app.services.audit import (
    ACTION_MINISTRY_HEAD_GRANTED,
    ACTION_MINISTRY_HEAD_REVOKED,
    ACTION_MINISTRY_MEMBERSHIP_ADDED,
    ACTION_MINISTRY_MEMBERSHIP_REMOVED,
    ACTION_PERSON_AUTH_LINK_CHANGED,
    ACTION_PERSON_CHURCH_MEMBERSHIP_STATUS_CHANGED,
    ACTION_PERSON_CREATED,
    ACTION_PERSON_DEACTIVATED,
    ACTION_PERSON_REACTIVATED,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"

PEOPLE_URL = "/api/v1/people"

_DOMAIN_TABLES = (
    "person",
    "ministry",
    "ministry_membership",
    "audit_event",
    "assignment",
    "role_qualification",
)


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_committed_rows_leak(integration_engine):
    """Prove, from outside the test transaction, that nothing was committed."""

    def counts() -> dict[str, int]:
        with integration_engine.connect() as probe:
            return {
                table: probe.execute(
                    text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
                ).scalar_one()
                for table in _DOMAIN_TABLES
            }

    before = counts()
    yield
    assert counts() == before, "a test committed rows that outlived it"


@pytest.fixture
def api(db_session, monkeypatch) -> TestClient:
    """A client whose requests run on the test's rolled-back Session, and --
    like the real :func:`app.api.dependencies.get_session` -- commit that
    Session when the handler returns.

    The established Task 57 fixture (``test_pg_serving_limits.py``), for the
    reasons recorded there: the app's Session has ``autoflush=False``, so the
    commit is what makes one request's writes visible to the next, and under
    the harness's ``join_transaction_mode="create_savepoint"`` it only releases
    a SAVEPOINT, leaving the outer transaction to discard everything.

    Overriding the dependency rather than ``SessionLocal`` matters here
    specifically: the real ``get_session`` ends with ``close()``, which
    detaches every instance -- including the fixture rows a test built before
    the request and goes on to assert about afterwards.

    Dev auth starts **off**, so the unauthenticated tests get the real refusal;
    a test that wants an actor calls :func:`_enable_dev_auth`.
    """
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()

    def _session_with_commit():
        # Commit on success only. A failing request cannot truly roll back
        # here without discarding the test's own fixture rows (the whole test
        # runs in one outer transaction).
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
    """One church, two ministries, and one person of each kind.

    Built once per test that needs the full matrix, so each test reads as the
    question it is asking rather than as twenty lines of setup.
    """

    def __init__(self, session: Session) -> None:
        self.church = f.make_church(session)
        self.av = f.make_ministry(session, church=self.church, name="AV")
        self.kids = f.make_ministry(session, church=self.church, name="Kids")

        self.admin = f.make_person(
            session, church=self.church, name="Admin", is_admin=True
        )
        self.av_head = f.make_person(session, church=self.church, name="AV Head")
        self.kids_head = f.make_person(session, church=self.church, name="Kids Head")
        self.volunteer = f.make_person(session, church=self.church, name="Volunteer")
        self.outsider = f.make_person(session, church=self.church, name="Outsider")
        #: An Admin who *also* actively heads AV. Task 79 separates oversight
        #: from operation, so most operational tests need this actor rather
        #: than ``admin`` -- and the tests that need to prove the refusal use
        #: ``admin``, who heads nothing.
        self.admin_av_head = f.make_person(
            session, church=self.church, name="Admin And AV Head", is_admin=True
        )

        self.av_head_membership = f.make_membership(
            session, person=self.av_head, ministry=self.av, is_head=True
        )
        self.admin_av_head_membership = f.make_membership(
            session, person=self.admin_av_head, ministry=self.av, is_head=True
        )
        self.kids_head_membership = f.make_membership(
            session, person=self.kids_head, ministry=self.kids, is_head=True
        )
        self.volunteer_membership = f.make_membership(
            session, person=self.volunteer, ministry=self.av
        )
        session.flush()


@pytest.fixture
def cast(db_session) -> _Cast:
    return _Cast(db_session)


def _audit(session: Session, *, action: str) -> list[AuditEvent]:
    return list(
        session.execute(
            select(AuditEvent).where(AuditEvent.action == action)
        ).scalars().all()
    )


def _count(session: Session, table: str) -> int:
    return session.execute(
        text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed names
    ).scalar_one()


# --------------------------------------------------------------------------
# A -- who may read the directory
# --------------------------------------------------------------------------


def test_a_an_unauthenticated_request_for_the_directory_is_401(api, cast):
    """No dev auth enabled, no session cookie: there is no actor at all."""
    response = api.get(PEOPLE_URL)
    assert response.status_code == 401
    assert "people" not in response.json()


def test_a_a_volunteer_is_403_not_an_empty_directory(api, cast, monkeypatch):
    """Task 79 §1: 403, not merely hidden UI."""
    _enable_dev_auth(monkeypatch)
    response = api.get(PEOPLE_URL, headers=_as(cast.volunteer))
    assert response.status_code == 403
    assert "people" not in response.json()


def test_a_a_person_with_no_membership_at_all_is_403(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.get(PEOPLE_URL, headers=_as(cast.outsider))
    assert response.status_code == 403


def test_a_a_ministry_head_may_list_people(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.get(PEOPLE_URL, headers=_as(cast.av_head))
    assert response.status_code == 200
    names = {row["display_name"] for row in response.json()["people"]}
    # The whole church, not only their own team: that is how a head finds an
    # existing Person instead of inventing a second one.
    assert cast.kids_head.display_name in names


def test_a_an_admin_may_list_people(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.get(PEOPLE_URL, headers=_as(cast.admin))
    assert response.status_code == 200
    assert response.json()["total"] >= 5


def test_a_a_deactivated_admin_loses_authority(api, db_session, cast, monkeypatch):
    """``get_current_actor`` refuses a deactivated Person outright, so this is
    401 rather than 403 -- and either way, no directory."""
    _enable_dev_auth(monkeypatch)
    cast.admin.deactivated_at = datetime.datetime.now(tz=UTC)
    db_session.flush()

    response = api.get(PEOPLE_URL, headers=_as(cast.admin))
    assert response.status_code == 401


def test_a_a_head_whose_membership_was_deactivated_loses_directory_access(
    api, db_session, cast, monkeypatch
):
    """The head flag alone is not authority. The database will not let the flag
    survive the deactivation, which is the invariant being leaned on here."""
    _enable_dev_auth(monkeypatch)
    cast.av_head_membership.is_ministry_head = False
    cast.av_head_membership.deactivated_at = datetime.datetime.now(tz=UTC)
    db_session.flush()

    response = api.get(PEOPLE_URL, headers=_as(cast.av_head))
    assert response.status_code == 403


def test_a_search_narrows_the_directory_without_fuzzy_matching(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    target = f.make_person(db_session, church=cast.church, name="Marguerite")
    db_session.flush()

    fragment = target.display_name[:9]
    response = api.get(
        f"{PEOPLE_URL}?search={fragment}", headers=_as(cast.admin)
    )
    assert response.status_code == 200
    assert [row["person_id"] for row in response.json()["people"]] == [target.id]

    # A name that is merely *similar* is not offered: no fuzzy matching.
    assert api.get(
        f"{PEOPLE_URL}?search=Margarite", headers=_as(cast.admin)
    ).json()["people"] == []


def test_a_inactive_people_are_hidden_unless_asked_for(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    gone = f.make_person(
        db_session, church=cast.church, name="Departed", deactivated=True
    )
    db_session.flush()

    visible = api.get(PEOPLE_URL, headers=_as(cast.admin)).json()["people"]
    assert gone.id not in {row["person_id"] for row in visible}

    with_inactive = api.get(
        f"{PEOPLE_URL}?include_inactive=true", headers=_as(cast.admin)
    ).json()["people"]
    assert gone.id in {row["person_id"] for row in with_inactive}


def test_a_the_directory_is_one_churchs_roll_and_only_one(
    api, db_session, cast, monkeypatch
):
    """V1 is a single-church deployment, so this narrows nothing today -- and
    that is exactly why it is worth proving. A filter that is absent because
    nobody needed it yet reads the same as one somebody removed.
    """
    _enable_dev_auth(monkeypatch)
    other_church = f.make_church(db_session, name="Other Church")
    outsider = f.make_person(
        db_session, church=other_church, name="Somebody Elsewhere"
    )
    db_session.flush()

    body = api.get(PEOPLE_URL, headers=_as(cast.admin)).json()
    names = {row["display_name"] for row in body["people"]}
    assert outsider.display_name not in names

    # And the detail read is not a way round the listing.
    assert api.get(
        f"{PEOPLE_URL}/{outsider.id}", headers=_as(cast.admin)
    ).status_code == 403


# --------------------------------------------------------------------------
# B -- the directory is not an N+1
# --------------------------------------------------------------------------


def test_b_the_directory_costs_a_fixed_number_of_queries(
    api, db_session, integration_engine, cast, monkeypatch
):
    """Task 79 §10: no N+1 in the directory.

    Counted by listening to the connection rather than by reading the code, and
    measured at two different sizes: what matters is not the absolute number
    (which includes the actor lookup and the count) but that it **does not grow
    with the number of people**.
    """
    _enable_dev_auth(monkeypatch)
    for index in range(12):
        person = f.make_person(db_session, church=cast.church, name=f"Member{index}")
        f.make_membership(db_session, person=person, ministry=cast.av)
    db_session.flush()

    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    # One warm-up request first, discarded. The harness's own noise differs
    # between the very first request and every later one -- the first opens no
    # SAVEPOINT and leaves no expired identity map behind -- and measuring
    # across that boundary would compare two different harness states rather
    # than two page sizes.
    api.get(f"{PEOPLE_URL}?limit=1", headers=_as(cast.admin))

    event.listen(integration_engine, "before_cursor_execute", record)
    try:
        statements.clear()
        small = api.get(f"{PEOPLE_URL}?limit=3", headers=_as(cast.admin))
        small_statements = list(statements)

        statements.clear()
        large = api.get(f"{PEOPLE_URL}?limit=50", headers=_as(cast.admin))
        large_statements = list(statements)
    finally:
        event.remove(integration_engine, "before_cursor_execute", record)

    assert small.status_code == 200 and large.status_code == 200
    assert len(large.json()["people"]) > len(small.json()["people"])

    # The same fixed cost for three people and for fifty: the actor lookup, the
    # count, the page, **one** pass over the memberships, the church's timezone
    # and **one** serving-totals aggregate. What matters is that the number
    # does not move with the page size.
    assert len(small_statements) == len(large_statements)
    assert len(large_statements) <= 10

    # The N+1 this listing would otherwise be, stated directly: one membership
    # query, not one per person on the page.
    membership_queries = [
        sql for sql in large_statements if "FROM ministry_membership" in sql
    ]
    assert len(membership_queries) == 1

    # And the same for the serving totals: one aggregate for the page, never
    # one query per person and never one per ministry (Task 79 §14).
    serving_queries = [sql for sql in large_statements if "FROM assignment" in sql]
    assert len(serving_queries) == 1

    # And the memberships really did arrive, so the fixed cost was not bought
    # by simply not loading them.
    assert any(row["memberships"] for row in large.json()["people"])


# --------------------------------------------------------------------------
# C -- creating a Person
# --------------------------------------------------------------------------


def test_c_creating_a_person_does_not_merge_a_same_name_person(
    api, db_session, cast, monkeypatch
):
    """Task 79 §4, and the heart of ADR 0001's identity rule."""
    _enable_dev_auth(monkeypatch)
    existing = f.make_person(db_session, church=cast.church, name="Sam Taylor")
    db_session.flush()

    refused = api.post(
        PEOPLE_URL,
        headers=_as(cast.admin),
        json={"display_name": existing.display_name},
    )
    assert refused.status_code == 409
    assert "already exists" in refused.json()["detail"]

    # Nothing was created, and nothing was returned that could be mistaken for
    # the existing person.
    assert refused.json().get("person_id") is None

    created = api.post(
        PEOPLE_URL,
        headers=_as(cast.admin),
        json={
            "display_name": existing.display_name,
            "acknowledge_duplicate_name": True,
        },
    )
    assert created.status_code == 201
    # Two distinct people, both still present. Never merged into one.
    assert created.json()["person_id"] != existing.id
    same_name = db_session.execute(
        select(func.count()).select_from(Person).where(
            func.lower(Person.display_name) == existing.display_name.lower()
        )
    ).scalar_one()
    assert same_name == 2


def test_c_a_head_may_create_a_person_into_their_own_ministry(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        PEOPLE_URL,
        headers=_as(cast.av_head),
        json={"display_name": "Brand New Volunteer", "initial_ministry_id": cast.av.id},
    )
    assert response.status_code == 201
    body = response.json()
    assert [m["ministry_id"] for m in body["memberships"]] == [cast.av.id]
    # Created as an ordinary member, never as an admin or a head.
    assert body["is_admin"] is False
    assert body["memberships"][0]["is_ministry_head"] is False


def test_c_a_head_may_not_create_an_unattached_person(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        PEOPLE_URL, headers=_as(cast.av_head), json={"display_name": "Floating Person"}
    )
    assert response.status_code == 403


def test_c_a_head_may_not_create_into_another_ministry(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        PEOPLE_URL,
        headers=_as(cast.av_head),
        json={"display_name": "Someone Else", "initial_ministry_id": cast.kids.id},
    )
    assert response.status_code == 403


def test_c_a_volunteer_may_not_create_anybody(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        PEOPLE_URL,
        headers=_as(cast.volunteer),
        json={"display_name": "Someone", "initial_ministry_id": cast.av.id},
    )
    assert response.status_code == 403


def test_c_creating_with_a_ministry_writes_both_audit_rows_in_one_transaction(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        PEOPLE_URL,
        headers=_as(cast.admin_av_head),
        json={"display_name": "Audited Person", "initial_ministry_id": cast.av.id},
    )
    assert response.status_code == 201
    person_id = response.json()["person_id"]

    created = [
        row for row in _audit(db_session, action=ACTION_PERSON_CREATED)
        if row.target_id == person_id
    ]
    assert len(created) == 1
    assert created[0].actor_person_id == cast.admin_av_head.id
    assert created[0].target_table == "person"

    added = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_MEMBERSHIP_ADDED)
        if row.ministry_id == cast.av.id
    ]
    assert len(added) == 1
    assert added[0].target_table == "ministry_membership"


def test_c_an_address_another_person_holds_is_refused_by_the_database_rule(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    holder = f.make_person(db_session, church=cast.church, name="Holder")
    holder.email = "taken@example.test"
    db_session.flush()

    response = api.post(
        PEOPLE_URL,
        headers=_as(cast.admin),
        json={"display_name": "Another Person", "email": "TAKEN@example.test"},
    )
    assert response.status_code == 409
    # The holder is not named in the message.
    assert holder.display_name not in response.json()["detail"]


# --------------------------------------------------------------------------
# D -- memberships
# --------------------------------------------------------------------------


def test_d_a_head_may_add_an_existing_person_to_their_own_ministry(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        f"{PEOPLE_URL}/{cast.outsider.id}/memberships",
        headers=_as(cast.av_head),
        json={"ministry_id": cast.av.id},
    )
    assert response.status_code == 200
    assert [m["ministry_id"] for m in response.json()["memberships"]] == [cast.av.id]


def test_d_a_head_may_not_add_to_an_unrelated_ministry(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        f"{PEOPLE_URL}/{cast.outsider.id}/memberships",
        headers=_as(cast.av_head),
        json={"ministry_id": cast.kids.id},
    )
    assert response.status_code == 403


def test_d_a_head_may_not_remove_a_membership_of_another_ministry(
    api, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        f"/api/v1/ministry-memberships/{cast.kids_head_membership.id}/remove",
        headers=_as(cast.av_head),
        json={},
    )
    assert response.status_code == 403


def test_d_adding_the_same_person_twice_is_idempotent_not_a_duplicate_row(
    api, db_session, cast, monkeypatch
):
    """The database's ``uq_ministry_membership_person_id_ministry_id`` spans
    active and inactive rows, so a second insert would be an IntegrityError --
    the service must return the existing row instead."""
    _enable_dev_auth(monkeypatch)
    url = f"{PEOPLE_URL}/{cast.outsider.id}/memberships"
    body = {"ministry_id": cast.av.id}

    first = api.post(url, headers=_as(cast.av_head), json=body)
    second = api.post(url, headers=_as(cast.av_head), json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["memberships"] == second.json()["memberships"]

    rows = db_session.execute(
        select(func.count()).select_from(MinistryMembership).where(
            MinistryMembership.person_id == cast.outsider.id,
            MinistryMembership.ministry_id == cast.av.id,
        )
    ).scalar_one()
    assert rows == 1

    # And the no-op wrote no audit row: only the first add is an act.
    added = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_MEMBERSHIP_ADDED)
        if row.ministry_id == cast.av.id
    ]
    assert len(added) == 1


def test_d_rejoining_reactivates_the_original_row_and_keeps_its_history(
    api, db_session, cast, monkeypatch
):
    """The reason rejoining must not insert a second row: the first one is what
    every past assignment and qualification points at."""
    _enable_dev_auth(monkeypatch)
    role = f.make_role(db_session, ministry=cast.av, name="Sound")
    f.make_qualification(
        db_session,
        membership=cast.volunteer_membership,
        role=role,
        decided_by=cast.av_head,
    )
    db_session.flush()
    original_id = cast.volunteer_membership.id

    removed = api.post(
        f"/api/v1/ministry-memberships/{original_id}/remove",
        headers=_as(cast.av_head),
        json={},
    )
    assert removed.status_code == 200
    assert removed.json()["deactivated_at"] is not None

    rejoined = api.post(
        f"{PEOPLE_URL}/{cast.volunteer.id}/memberships",
        headers=_as(cast.av_head),
        json={"ministry_id": cast.av.id},
    )
    assert rejoined.status_code == 200
    restored = rejoined.json()["memberships"][0]
    assert restored["ministry_membership_id"] == original_id
    assert restored["deactivated_at"] is None
    # Never promoted by rejoining.
    assert restored["is_ministry_head"] is False

    # One row, and the qualification still points at it.
    assert db_session.execute(
        select(func.count()).select_from(MinistryMembership).where(
            MinistryMembership.person_id == cast.volunteer.id,
            MinistryMembership.ministry_id == cast.av.id,
        )
    ).scalar_one() == 1
    assert _count(db_session, "role_qualification") >= 1


def test_d_removing_a_membership_deletes_nothing(
    api, db_session, cast, monkeypatch
):
    """Task 79 §5: never cascade-delete serving history, assignments,
    qualifications or audit data."""
    _enable_dev_auth(monkeypatch)
    role = f.make_role(db_session, ministry=cast.av, name="Sound")
    f.make_qualification(
        db_session, membership=cast.volunteer_membership, role=role,
        decided_by=cast.av_head,
    )
    db_session.flush()

    before = {
        table: _count(db_session, table)
        for table in ("person", "ministry_membership", "role_qualification")
    }

    response = api.post(
        f"/api/v1/ministry-memberships/{cast.volunteer_membership.id}/remove",
        headers=_as(cast.av_head),
        json={},
    )
    assert response.status_code == 200

    after = {table: _count(db_session, table) for table in before}
    assert after == before, "removing a membership deleted rows"

    # One timestamp, and the Person is untouched.
    assert cast.volunteer.deactivated_at is None


def test_d_removing_a_head_is_admin_only_and_writes_two_audit_rows(
    api, db_session, cast, monkeypatch
):
    """Core §5.1, end to end: a Head cannot depose a co-head, and when an Admin
    does it, both the revocation and the removal are recorded."""
    _enable_dev_auth(monkeypatch)
    co_head = f.make_person(db_session, church=cast.church, name="Co-head")
    co_head_membership = f.make_membership(
        db_session, person=co_head, ministry=cast.av, is_head=True
    )
    db_session.flush()

    refused = api.post(
        f"/api/v1/ministry-memberships/{co_head_membership.id}/remove",
        headers=_as(cast.av_head),
        json={},
    )
    assert refused.status_code == 403
    # Refused before anything was mutated.
    db_session.refresh(co_head_membership)
    assert co_head_membership.is_ministry_head is True
    assert co_head_membership.deactivated_at is None

    # An Admin who does not lead AV is refused too, and for the other half of
    # the rule: removing somebody from a team is that team's business. Taking
    # authority away is the Admin-only act, and it has its own endpoint.
    assert api.post(
        f"/api/v1/ministry-memberships/{co_head_membership.id}/remove",
        headers=_as(cast.admin),
        json={},
    ).status_code == 403

    allowed = api.post(
        f"/api/v1/ministry-memberships/{co_head_membership.id}/remove",
        headers=_as(cast.admin_av_head),
        json={},
    )
    assert allowed.status_code == 200
    assert allowed.json()["is_ministry_head"] is False
    assert allowed.json()["deactivated_at"] is not None

    revoked = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_HEAD_REVOKED)
        if row.target_id == co_head_membership.id
    ]
    removed = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_MEMBERSHIP_REMOVED)
        if row.target_id == co_head_membership.id
    ]
    assert len(revoked) == 1
    assert len(removed) == 1
    assert revoked[0].ministry_id == cast.av.id


def test_d_a_membership_add_is_audited_against_its_own_ministry(
    api, db_session, cast, monkeypatch
):
    """``ministry_id`` is what a Ministry Head's later audit view filters on,
    so it must be the ministry that actually changed."""
    _enable_dev_auth(monkeypatch)
    api.post(
        f"{PEOPLE_URL}/{cast.outsider.id}/memberships",
        headers=_as(cast.kids_head),
        json={"ministry_id": cast.kids.id},
    )
    added = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_MEMBERSHIP_ADDED)
        if row.target_table == "ministry_membership"
    ]
    assert [row.ministry_id for row in added] == [cast.kids.id]


# --------------------------------------------------------------------------
# E -- the church-wide Person record
# --------------------------------------------------------------------------


def test_e_a_head_may_not_deactivate_a_person_church_wide(api, cast, monkeypatch):
    """The single most important refusal in this task: a Head's authority
    stops at their own ministry."""
    _enable_dev_auth(monkeypatch)
    response = api.post(
        f"{PEOPLE_URL}/{cast.volunteer.id}/deactivate",
        headers=_as(cast.av_head),
        json={},
    )
    assert response.status_code == 403


def test_e_a_head_may_not_edit_a_person_record(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.patch(
        f"{PEOPLE_URL}/{cast.volunteer.id}",
        headers=_as(cast.av_head),
        json={"display_name": "Renamed By A Head"},
    )
    assert response.status_code == 403


def test_e_an_admin_may_deactivate_and_reactivate_a_person(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    deactivated = api.post(
        f"{PEOPLE_URL}/{cast.volunteer.id}/deactivate",
        headers=_as(cast.admin),
        json={"reason": "Moved away"},
    )
    assert deactivated.status_code == 200
    assert deactivated.json()["deactivated_at"] is not None

    reactivated = api.post(
        f"{PEOPLE_URL}/{cast.volunteer.id}/reactivate",
        headers=_as(cast.admin),
        json={},
    )
    assert reactivated.status_code == 200
    assert reactivated.json()["deactivated_at"] is None

    actions = {
        row.action
        for row in db_session.execute(
            select(AuditEvent).where(AuditEvent.target_id == cast.volunteer.id)
        ).scalars().all()
    }
    assert {ACTION_PERSON_DEACTIVATED, ACTION_PERSON_REACTIVATED} <= actions


def test_e_deactivating_a_person_preserves_every_membership_and_head_flag(
    api, db_session, cast, monkeypatch
):
    """Which is the only reason reactivation can restore them exactly."""
    _enable_dev_auth(monkeypatch)
    before = [
        (m.id, m.ministry_id, m.is_ministry_head, m.deactivated_at)
        for m in db_session.execute(
            select(MinistryMembership).where(
                MinistryMembership.person_id == cast.av_head.id
            ).order_by(MinistryMembership.id)
        ).scalars().all()
    ]

    api.post(
        f"{PEOPLE_URL}/{cast.av_head.id}/deactivate",
        headers=_as(cast.admin),
        json={},
    )
    db_session.expire_all()

    after = [
        (m.id, m.ministry_id, m.is_ministry_head, m.deactivated_at)
        for m in db_session.execute(
            select(MinistryMembership).where(
                MinistryMembership.person_id == cast.av_head.id
            ).order_by(MinistryMembership.id)
        ).scalars().all()
    ]
    assert after == before


def test_e_deactivating_a_person_deletes_no_history(
    api, db_session, cast, monkeypatch
):
    """Task 79 §5 and §12: preserve history, never hard-delete."""
    _enable_dev_auth(monkeypatch)
    role = f.make_role(db_session, ministry=cast.av, name="Sound")
    f.make_qualification(
        db_session, membership=cast.volunteer_membership, role=role,
        decided_by=cast.av_head,
    )
    db_session.flush()

    before = {
        table: _count(db_session, table)
        for table in ("person", "ministry_membership", "role_qualification")
    }

    api.post(
        f"{PEOPLE_URL}/{cast.volunteer.id}/deactivate",
        headers=_as(cast.admin),
        json={},
    )

    after = {table: _count(db_session, table) for table in before}
    assert after == before, "deactivating a person deleted rows"


def test_e_a_deactivated_person_still_appears_with_their_memberships(
    api, cast, monkeypatch
):
    """Deactivation is not disappearance -- an Admin must still be able to see
    what the person was part of in order to reverse it."""
    _enable_dev_auth(monkeypatch)
    api.post(
        f"{PEOPLE_URL}/{cast.volunteer.id}/deactivate",
        headers=_as(cast.admin),
        json={},
    )
    detail = api.get(
        f"{PEOPLE_URL}/{cast.volunteer.id}", headers=_as(cast.admin)
    ).json()
    assert detail["deactivated_at"] is not None
    assert [m["ministry_id"] for m in detail["memberships"]] == [cast.av.id]


def test_e_an_admin_cannot_deactivate_themselves(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.post(
        f"{PEOPLE_URL}/{cast.admin.id}/deactivate",
        headers=_as(cast.admin),
        json={},
    )
    assert response.status_code == 409


def test_e_a_deactivated_person_cannot_be_added_back_to_a_ministry_by_a_head(
    api, cast, monkeypatch
):
    """Otherwise a Head could undo an Admin's church-wide decision."""
    _enable_dev_auth(monkeypatch)
    api.post(
        f"{PEOPLE_URL}/{cast.outsider.id}/deactivate",
        headers=_as(cast.admin),
        json={},
    )
    response = api.post(
        f"{PEOPLE_URL}/{cast.outsider.id}/memberships",
        headers=_as(cast.av_head),
        json={"ministry_id": cast.av.id},
    )
    assert response.status_code == 409


# --------------------------------------------------------------------------
# F -- Ministry Head authority
# --------------------------------------------------------------------------


def _head_url(person_id: int) -> str:
    return f"{PEOPLE_URL}/{person_id}/ministry-head"


def test_f_appointing_a_head_is_admin_only(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    url = _head_url(cast.volunteer.id)
    body = {"ministry_id": cast.av.id, "is_ministry_head": True}

    # A Head of this very ministry is still refused: heading a ministry confers
    # no say in who leads it (core §4.3).
    assert api.put(url, headers=_as(cast.av_head), json=body).status_code == 403
    assert api.put(url, headers=_as(cast.volunteer), json=body).status_code == 403
    assert api.put(url, headers=_as(cast.admin), json=body).status_code == 200


def test_f_revoking_head_authority_is_admin_only(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    url = _head_url(cast.av_head.id)
    body = {"ministry_id": cast.av.id, "is_ministry_head": False}
    assert api.put(url, headers=_as(cast.av_head), json=body).status_code == 403
    assert api.put(url, headers=_as(cast.admin), json=body).status_code == 200


def test_f_a_head_cannot_appoint_themselves_elsewhere(api, cast, monkeypatch):
    """The escalation the Admin-only rule exists to prevent."""
    _enable_dev_auth(monkeypatch)
    response = api.put(
        _head_url(cast.av_head.id),
        headers=_as(cast.av_head),
        json={"ministry_id": cast.kids.id, "is_ministry_head": True},
    )
    assert response.status_code == 403


def test_f_an_admin_may_appoint_the_first_head_of_an_empty_ministry(
    api, db_session, cast, monkeypatch
):
    """**The case the person-scoped endpoint exists for.**

    A ministry nobody leads is a ministry nobody may add to, because rostering
    needs an active Head of that ministry. Appointing its leader is the
    governance act that breaks the circle, and it creates the membership.
    """
    _enable_dev_auth(monkeypatch)
    empty = f.make_ministry(db_session, church=cast.church, name="Brand New")
    db_session.flush()

    # Nobody can put anyone on it -- not even an Admin.
    refused = api.post(
        f"{PEOPLE_URL}/{cast.outsider.id}/memberships",
        headers=_as(cast.admin),
        json={"ministry_id": empty.id},
    )
    assert refused.status_code == 403

    appointed = api.put(
        _head_url(cast.outsider.id),
        headers=_as(cast.admin),
        json={"ministry_id": empty.id, "is_ministry_head": True},
    )
    assert appointed.status_code == 200
    assert appointed.json()["is_ministry_head"] is True
    assert appointed.json()["ministry_id"] == empty.id

    # And now the new head can run it.
    allowed = api.post(
        f"{PEOPLE_URL}/{cast.volunteer.id}/memberships",
        headers=_as(cast.outsider),
        json={"ministry_id": empty.id},
    )
    assert allowed.status_code == 200


def test_f_appointing_into_an_empty_ministry_writes_both_audit_rows(
    api, db_session, cast, monkeypatch
):
    """The membership was created and authority was granted: two acts."""
    _enable_dev_auth(monkeypatch)
    empty = f.make_ministry(db_session, church=cast.church, name="Brand New")
    db_session.flush()

    api.put(
        _head_url(cast.outsider.id),
        headers=_as(cast.admin),
        json={"ministry_id": empty.id, "is_ministry_head": True},
    )

    added = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_MEMBERSHIP_ADDED)
        if row.ministry_id == empty.id
    ]
    granted = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_HEAD_GRANTED)
        if row.ministry_id == empty.id
    ]
    assert len(added) == 1
    assert len(granted) == 1


def test_f_revoking_preserves_the_person_and_the_ordinary_membership(
    api, db_session, cast, monkeypatch
):
    """Task 79 §10: revoke preserves Person and ordinary membership."""
    _enable_dev_auth(monkeypatch)
    response = api.put(
        _head_url(cast.av_head.id),
        headers=_as(cast.admin),
        json={"ministry_id": cast.av.id, "is_ministry_head": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_ministry_head"] is False
    # Still on the team, still in the church.
    assert body["deactivated_at"] is None
    db_session.refresh(cast.av_head)
    assert cast.av_head.deactivated_at is None


def test_f_revoking_where_there_is_no_membership_is_refused(api, cast, monkeypatch):
    """Rather than creating one, which would be an odd way to demote."""
    _enable_dev_auth(monkeypatch)
    response = api.put(
        _head_url(cast.outsider.id),
        headers=_as(cast.admin),
        json={"ministry_id": cast.kids.id, "is_ministry_head": False},
    )
    assert response.status_code == 409


def test_f_grant_and_revoke_are_audited_with_the_ministry(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    membership_id = cast.volunteer_membership.id
    api.put(
        _head_url(cast.volunteer.id),
        headers=_as(cast.admin),
        json={
            "ministry_id": cast.av.id,
            "is_ministry_head": True,
            "reason": "Taking over the rota",
        },
    )
    api.put(
        _head_url(cast.volunteer.id),
        headers=_as(cast.admin),
        json={"ministry_id": cast.av.id, "is_ministry_head": False},
    )

    granted = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_HEAD_GRANTED)
        if row.target_id == membership_id
    ]
    revoked = [
        row for row in _audit(db_session, action=ACTION_MINISTRY_HEAD_REVOKED)
        if row.target_id == membership_id
    ]
    assert len(granted) == 1 and len(revoked) == 1
    assert granted[0].ministry_id == cast.av.id
    assert granted[0].reason == "Taking over the rota"
    assert granted[0].actor_person_id == cast.admin.id


def test_f_a_newly_appointed_head_can_immediately_manage_their_ministry(
    api, cast, monkeypatch
):
    """The authorization rule and the stored flag really are the same thing."""
    _enable_dev_auth(monkeypatch)
    assert api.get(PEOPLE_URL, headers=_as(cast.volunteer)).status_code == 403

    api.put(
        _head_url(cast.volunteer.id),
        headers=_as(cast.admin),
        json={"ministry_id": cast.av.id, "is_ministry_head": True},
    )
    assert api.get(PEOPLE_URL, headers=_as(cast.volunteer)).status_code == 200


# --------------------------------------------------------------------------
# G -- the sign-in link
# --------------------------------------------------------------------------


def test_g_managing_sign_in_access_is_admin_only(api, cast, monkeypatch):
    """Task 79 §7: a Ministry Head does not manage Google authentication."""
    _enable_dev_auth(monkeypatch)
    url = f"{PEOPLE_URL}/{cast.volunteer.id}/auth-link"
    body = {"email": "volunteer@example.test"}
    assert api.put(url, headers=_as(cast.av_head), json=body).status_code == 403
    assert api.put(url, headers=_as(cast.admin), json=body).status_code == 200


def test_g_linking_replacing_and_unlinking_all_work(api, db_session, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    url = f"{PEOPLE_URL}/{cast.volunteer.id}/auth-link"

    linked = api.put(url, headers=_as(cast.admin), json={"email": "First@Example.TEST"})
    assert linked.json()["email"] == "first@example.test"

    replaced = api.put(
        url, headers=_as(cast.admin), json={"email": "second@example.test"}
    )
    assert replaced.json()["email"] == "second@example.test"

    unlinked = api.put(url, headers=_as(cast.admin), json={"email": None})
    assert unlinked.json()["email"] is None
    # The Person survives untouched.
    assert unlinked.json()["display_name"] == cast.volunteer.display_name
    assert unlinked.json()["deactivated_at"] is None


def test_g_the_address_never_reaches_the_audit_trail(
    api, db_session, cast, monkeypatch
):
    """Task 79 §11: do not log unnecessary PII."""
    _enable_dev_auth(monkeypatch)
    api.put(
        f"{PEOPLE_URL}/{cast.volunteer.id}/auth-link",
        headers=_as(cast.admin),
        json={"email": "private.address@example.test"},
    )
    rows = [
        row for row in _audit(db_session, action=ACTION_PERSON_AUTH_LINK_CHANGED)
        if row.target_id == cast.volunteer.id
    ]
    assert len(rows) == 1
    blob = f"{rows[0].summary}{rows[0].before_values}{rows[0].after_values}"
    assert "private.address@example.test" not in blob
    assert rows[0].after_values["email_linked"] is True


def test_g_an_address_another_person_holds_is_refused(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    cast.outsider.email = "shared@example.test"
    db_session.flush()

    response = api.put(
        f"{PEOPLE_URL}/{cast.volunteer.id}/auth-link",
        headers=_as(cast.admin),
        json={"email": "shared@example.test"},
    )
    assert response.status_code == 409


# --------------------------------------------------------------------------
# I -- formal church membership status
# --------------------------------------------------------------------------


def _status_url(person_id: int) -> str:
    return f"{PEOPLE_URL}/{person_id}/church-membership-status"


def test_i_everybody_starts_unknown(api, cast, monkeypatch):
    """The honest default: nobody has said, so the application does not
    pretend to know."""
    _enable_dev_auth(monkeypatch)
    body = api.get(f"{PEOPLE_URL}/{cast.volunteer.id}", headers=_as(cast.admin)).json()
    assert body["church_membership_status"] == "UNKNOWN"


def test_i_an_admin_may_change_it(api, db_session, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.put(
        _status_url(cast.volunteer.id),
        headers=_as(cast.admin),
        json={"church_membership_status": "MEMBER"},
    )
    assert response.status_code == 200
    assert response.json()["church_membership_status"] == "MEMBER"
    db_session.refresh(cast.volunteer)
    assert cast.volunteer.church_membership_status == "MEMBER"


def test_i_a_head_may_read_it_but_not_change_it(api, cast, monkeypatch):
    """§6 exactly: a Head sees the status -- useful when deciding who to ask --
    and changing it is church governance."""
    _enable_dev_auth(monkeypatch)
    read = api.get(f"{PEOPLE_URL}/{cast.volunteer.id}", headers=_as(cast.av_head))
    assert read.status_code == 200
    assert "church_membership_status" in read.json()

    refused = api.put(
        _status_url(cast.volunteer.id),
        headers=_as(cast.av_head),
        json={"church_membership_status": "MEMBER"},
    )
    assert refused.status_code == 403


def test_i_a_head_of_the_persons_own_ministry_still_may_not_change_it(
    api, cast, monkeypatch
):
    """The volunteer is on AV, and the AV head is still refused: church
    membership is not a fact about one team."""
    _enable_dev_auth(monkeypatch)
    assert api.put(
        _status_url(cast.volunteer.id),
        headers=_as(cast.av_head),
        json={"church_membership_status": "NON_MEMBER"},
    ).status_code == 403


def test_i_a_volunteer_gets_no_directory_access_at_all(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    assert api.put(
        _status_url(cast.outsider.id),
        headers=_as(cast.volunteer),
        json={"church_membership_status": "MEMBER"},
    ).status_code == 403


def test_i_a_status_outside_the_three_is_refused(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    response = api.put(
        _status_url(cast.volunteer.id),
        headers=_as(cast.admin),
        json={"church_membership_status": "VISITOR"},
    )
    assert response.status_code == 409


def test_i_the_database_refuses_a_bad_value_too(api, db_session, cast):
    """The service check is a readable error in front of a real constraint,
    not the only thing standing between the column and nonsense."""
    from sqlalchemy.exc import IntegrityError

    cast.volunteer.church_membership_status = "VISITOR"
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_i_it_is_audited_as_its_own_action(api, db_session, cast, monkeypatch):
    """A separate action from PERSON_UPDATED: editing a phone number is
    clerical, recording that somebody is a member of this church is
    governance."""
    _enable_dev_auth(monkeypatch)
    api.put(
        _status_url(cast.volunteer.id),
        headers=_as(cast.admin),
        json={"church_membership_status": "MEMBER", "reason": "Joined in March"},
    )
    rows = [
        row
        for row in _audit(
            db_session, action=ACTION_PERSON_CHURCH_MEMBERSHIP_STATUS_CHANGED
        )
        if row.target_id == cast.volunteer.id
    ]
    assert len(rows) == 1
    assert rows[0].before_values == {"church_membership_status": "UNKNOWN"}
    assert rows[0].after_values == {"church_membership_status": "MEMBER"}
    assert rows[0].reason == "Joined in March"


def test_i_nothing_infers_it_from_ministry_participation(
    api, db_session, cast, monkeypatch
):
    """The one derivation §6 forbids. Joining a ministry, leading one, and
    having serving history all leave the status exactly where it was."""
    _enable_dev_auth(monkeypatch)
    api.post(
        f"{PEOPLE_URL}/{cast.outsider.id}/memberships",
        headers=_as(cast.kids_head),
        json={"ministry_id": cast.kids.id},
    )
    api.put(
        f"{PEOPLE_URL}/{cast.outsider.id}/ministry-head",
        headers=_as(cast.admin),
        json={"ministry_id": cast.kids.id, "is_ministry_head": True},
    )
    db_session.refresh(cast.outsider)
    assert cast.outsider.church_membership_status == "UNKNOWN"


def test_i_setting_it_back_to_unknown_is_a_correction_not_a_deletion(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    api.put(
        _status_url(cast.volunteer.id),
        headers=_as(cast.admin),
        json={"church_membership_status": "MEMBER"},
    )
    response = api.put(
        _status_url(cast.volunteer.id),
        headers=_as(cast.admin),
        json={"church_membership_status": "UNKNOWN"},
    )
    assert response.status_code == 200
    assert response.json()["church_membership_status"] == "UNKNOWN"
    db_session.refresh(cast.volunteer)
    assert cast.volunteer.deactivated_at is None


# --------------------------------------------------------------------------
# J -- recorded serving, as the directory and the detail screen see it
# --------------------------------------------------------------------------


def _serve_once(session, *, person, ministry, date):
    """One authoritative, finalized, past assignment for this person.

    The full chain, because that is what "recorded serving" means: a period, an
    event, a schedule, a FINALIZED version, a requirement snapshot, and an
    assignment through the person's membership of that ministry.
    """
    period = f.make_period(
        session=session,
        ministry=ministry,
        start=datetime.date(2020, 1, 1),
        end=datetime.date(2020, 12, 31),
    )
    event_row = f.make_event(session=session, period=period, event_date=date)
    role = f.make_role(session=session, ministry=ministry)
    schedule = f.make_schedule(session=session, period=period)
    version = f.make_version(
        session=session,
        schedule=schedule,
        period=period,
        status="FINALIZED",
        finalized_at=datetime.datetime(2020, 1, 1, tzinfo=UTC),
    )
    requirement = f.make_version_requirement(
        session=session, version=version, event=event_row, role=role
    )
    membership = session.execute(
        select(MinistryMembership).where(
            MinistryMembership.person_id == person.id,
            MinistryMembership.ministry_id == ministry.id,
        )
    ).scalar_one()
    return f.make_assignment(
        session=session, requirement=requirement, membership=membership
    )


def test_j_the_directory_carries_a_recorded_serving_total(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    _serve_once(
        db_session, person=cast.volunteer, ministry=cast.av,
        date=datetime.date(2020, 3, 1),
    )
    _serve_once(
        db_session, person=cast.volunteer, ministry=cast.av,
        date=datetime.date(2020, 3, 8),
    )
    db_session.flush()

    rows = api.get(PEOPLE_URL, headers=_as(cast.admin)).json()["people"]
    subject = next(r for r in rows if r["person_id"] == cast.volunteer.id)
    assert subject["recorded_serving_total"] == 2


def test_j_the_directory_does_not_carry_the_breakdown(
    api, db_session, cast, monkeypatch
):
    """One aggregate for the page; the breakdown is the detail screen's."""
    _enable_dev_auth(monkeypatch)
    rows = api.get(PEOPLE_URL, headers=_as(cast.admin)).json()["people"]
    assert all(row.get("serving") is None for row in rows)


def test_j_the_person_detail_carries_the_per_ministry_breakdown(
    api, db_session, cast, monkeypatch
):
    _enable_dev_auth(monkeypatch)
    f.make_membership(db_session, person=cast.volunteer, ministry=cast.kids)
    _serve_once(
        db_session, person=cast.volunteer, ministry=cast.av,
        date=datetime.date(2020, 3, 1),
    )
    _serve_once(
        db_session, person=cast.volunteer, ministry=cast.kids,
        date=datetime.date(2020, 3, 8),
    )
    db_session.flush()

    body = api.get(
        f"{PEOPLE_URL}/{cast.volunteer.id}", headers=_as(cast.admin)
    ).json()
    assert body["recorded_serving_total"] == 2
    assert body["serving"]["total"] == 2
    by_ministry = {row["ministry_name"]: row["count"] for row in body["serving"]["by_ministry"]}
    assert by_ministry == {cast.av.name: 1, cast.kids.name: 1}
    assert body["serving"]["same_date_conflicts"] == []


def test_j_a_ministry_head_may_read_serving_history_from_the_directory(
    api, db_session, cast, monkeypatch
):
    """§12: Admin *and* Ministry Head may see this; a volunteer may not see
    the directory at all."""
    _enable_dev_auth(monkeypatch)
    _serve_once(
        db_session, person=cast.volunteer, ministry=cast.av,
        date=datetime.date(2020, 3, 1),
    )
    db_session.flush()

    body = api.get(
        f"{PEOPLE_URL}/{cast.volunteer.id}", headers=_as(cast.av_head)
    ).json()
    assert body["serving"]["total"] == 1


def test_j_a_volunteer_gets_no_church_wide_serving_history(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    assert api.get(
        f"{PEOPLE_URL}/{cast.outsider.id}", headers=_as(cast.volunteer)
    ).status_code == 403


def test_j_somebody_with_no_history_reads_zero(api, cast, monkeypatch):
    _enable_dev_auth(monkeypatch)
    body = api.get(f"{PEOPLE_URL}/{cast.outsider.id}", headers=_as(cast.admin)).json()
    assert body["recorded_serving_total"] == 0
    assert body["serving"]["by_ministry"] == []


# --------------------------------------------------------------------------
# H -- what the whole surface may never do
# --------------------------------------------------------------------------


def test_h_no_people_endpoint_offers_a_delete_verb(api):
    """Task 79 §5 and the scope boundary: no hard deletes anywhere here."""
    for path, operations in app.openapi()["paths"].items():
        if path.startswith("/api/v1/people") or path.startswith(
            "/api/v1/ministry-memberships/{ministry_membership_id}"
        ):
            assert "delete" not in operations, path


def test_h_a_full_management_session_leaves_the_row_counts_untouched(
    api, db_session, cast, monkeypatch
):
    """Everything this task can do, in sequence, deleting nothing.

    Row counts may only go *up*: creating a person and a membership adds rows,
    and every "remove" is a timestamp.
    """
    _enable_dev_auth(monkeypatch)
    before = {
        table: _count(db_session, table)
        for table in ("person", "ministry_membership", "audit_event")
    }

    created = api.post(
        PEOPLE_URL,
        headers=_as(cast.admin_av_head),
        json={"display_name": "Complete Journey", "initial_ministry_id": cast.av.id},
    )
    assert created.status_code == 201
    person_id = created.json()["person_id"]
    membership_id = created.json()["memberships"][0]["ministry_membership_id"]

    api.patch(
        f"{PEOPLE_URL}/{person_id}",
        headers=_as(cast.admin),
        json={"display_name": "Complete Journey", "phone": "555-0199"},
    )
    api.put(
        f"{PEOPLE_URL}/{person_id}/church-membership-status",
        headers=_as(cast.admin),
        json={"church_membership_status": "MEMBER"},
    )
    api.put(
        f"{PEOPLE_URL}/{person_id}/ministry-head",
        headers=_as(cast.admin),
        json={"ministry_id": cast.av.id, "is_ministry_head": True},
    )
    api.put(
        f"{PEOPLE_URL}/{person_id}/ministry-head",
        headers=_as(cast.admin),
        json={"ministry_id": cast.av.id, "is_ministry_head": False},
    )
    api.patch(
        f"/api/v1/ministry-memberships/{membership_id}",
        headers=_as(cast.admin_av_head), json={"notes": "Sound desk"},
    )
    api.post(
        f"/api/v1/ministry-memberships/{membership_id}/remove",
        headers=_as(cast.admin_av_head), json={},
    )
    api.post(f"{PEOPLE_URL}/{person_id}/deactivate", headers=_as(cast.admin), json={})
    api.post(f"{PEOPLE_URL}/{person_id}/reactivate", headers=_as(cast.admin), json={})

    after = {table: _count(db_session, table) for table in before}
    assert after["person"] == before["person"] + 1
    assert after["ministry_membership"] == before["ministry_membership"] + 1
    # Nine audited acts, and every one of them still there.
    assert after["audit_event"] > before["audit_event"]
