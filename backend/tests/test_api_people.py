"""People and membership management over HTTP (Task 79).

Offline: no database. The Session is a recording stand-in answering the few
statements these routes issue, and the domain services are stubbed where the
subject is **HTTP mapping** rather than domain behaviour -- that behaviour has
its own dedicated coverage in ``tests/test_services_person_directory.py`` and
``tests/test_services_ministry_membership.py``, and the end-to-end matrix
against real PostgreSQL is in
``tests/integration/test_pg_people_management.py``.

The same shape as ``tests/test_api_ministry_roles.py``, deliberately: this file
exists to pin the *boundary* -- that an unauthenticated request is 401 before
anything else happens, that a service's ``AuthorizationError`` surfaces as 403
rather than an empty list, that nothing in the request body can name the actor,
and that a response carries no field the domain did not put in it.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api import routes_people as routes
from app.config import get_settings
from app.main import app
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.person_directory import DirectoryEntry, DirectoryMembership, DirectoryPage

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"

PEOPLE_URL = "/api/v1/people"
PERSON_URL = "/api/v1/people/{id}"
AUTH_LINK_URL = "/api/v1/people/{id}/auth-link"
DEACTIVATE_URL = "/api/v1/people/{id}/deactivate"
REACTIVATE_URL = "/api/v1/people/{id}/reactivate"
MEMBERSHIPS_URL = "/api/v1/people/{id}/memberships"
MEMBERSHIP_URL = "/api/v1/ministry-memberships/{id}"
REMOVE_URL = "/api/v1/ministry-memberships/{id}/remove"
CHURCH_STATUS_URL = "/api/v1/people/{id}/church-membership-status"
MINISTRY_HEAD_URL = "/api/v1/people/{id}/ministry-head"


# --------------------------------------------------------------------------
# Stand-ins
# --------------------------------------------------------------------------


class _StubPerson:
    def __init__(self, id: int = 1, **overrides) -> None:
        self.id = id
        self.church_id = 1
        self.display_name = "Ada Actor"
        self.email = None
        self.phone = None
        self.is_admin = False
        self.deactivated_at = None
        self.ministry_memberships: list = []
        for key, value in overrides.items():
            setattr(self, key, value)


class _StubMinistry:
    def __init__(self, id: int = 900, name: str = "AV") -> None:
        self.id = id
        self.name = name
        self.deactivated_at = None


class _StubMembership:
    def __init__(self, id: int = 700, **overrides) -> None:
        self.id = id
        self.person_id = 1
        self.ministry_id = 900
        self.is_ministry_head = False
        self.notes = None
        self.joined_on = None
        self.deactivated_at = None
        self.ministry = _StubMinistry()
        self.person = _StubPerson()
        for key, value in overrides.items():
            setattr(self, key, value)


class _ScalarResult:
    """The Result methods these routes call.

    ``all()`` answers empty: ``/api/v1/me`` reads the actor's headed
    ministries through it, and this stand-in has none to report.
    """

    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        return []


class RecordingSession:
    """A stand-in request Session, matching ``tests/test_api_ministry_roles.py``."""

    #: Sentinel distinguishing "not supplied" from a deliberate ``None``,
    #: which is how a test says "this row does not exist".
    _UNSET = object()

    def __init__(self, *, person=None, ministry=_UNSET, membership=_UNSET,
                 target_person=_UNSET) -> None:
        self.person = _StubPerson() if person is None else person
        self.ministry = (
            _StubMinistry() if ministry is RecordingSession._UNSET else ministry
        )
        self.membership = (
            _StubMembership() if membership is RecordingSession._UNSET else membership
        )
        # Two different lookups read ``person``: the actor (first, from
        # ``get_current_actor``) and the URL's target (second). They are
        # normally the same stand-in; a 404 test needs them to differ.
        self.target_person = (
            self.person if target_person is RecordingSession._UNSET else target_person
        )
        self.person_lookups = 0
        self.events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        # Checked most-specific-first: "FROM ministry_membership" also contains
        # the substring "FROM ministry".
        if "FROM ministry_membership" in sql:
            return _ScalarResult(self.membership)
        if "FROM person" in sql:
            self.person_lookups += 1
            first = self.person_lookups == 1
            return _ScalarResult(self.person if first else self.target_person)
        if "FROM ministry" in sql:
            return _ScalarResult(self.ministry)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

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
    """A client whose requests carry a development actor header.

    The header is the *local* identity path and is only read because the flag
    below enables it; production disables it outright
    (:mod:`app.api.dependencies`).
    """
    monkeypatch.setenv(DEV_FLAG, "1")
    monkeypatch.delenv("APP_ENV", raising=False)
    get_settings.cache_clear()
    return TestClient(app)


def _entry(**overrides) -> DirectoryEntry:
    fields = {
        "person_id": 1,
        "display_name": "Ada Actor",
        "email": None,
        "phone": None,
        "is_admin": False,
        "church_membership_status": "UNKNOWN",
        "deactivated_at": None,
        "memberships": (),
        "recorded_serving_total": 0,
        "serving_summary": None,
    }
    fields.update(overrides)
    return DirectoryEntry(**fields)


def _membership_row(**overrides) -> DirectoryMembership:
    fields = {
        "ministry_membership_id": 700,
        "ministry_id": 900,
        "ministry_name": "AV",
        "is_ministry_head": False,
        "deactivated_at": None,
        "notes": None,
        "joined_on": None,
    }
    fields.update(overrides)
    return DirectoryMembership(**fields)


def _stub(monkeypatch, name: str, result):
    """Replace one service function with a recording stand-in.

    Returns the call log. ``result`` may be a value to return or an exception
    instance to raise.
    """
    calls: list[dict] = []

    def _fake(*args, **kwargs):
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(routes, name, _fake)
    return calls


# --------------------------------------------------------------------------
# The identity boundary
# --------------------------------------------------------------------------


class TestUnauthenticated:
    """401 before anything else, on every route."""

    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", PEOPLE_URL),
            ("post", PEOPLE_URL),
            ("get", PERSON_URL.format(id=1)),
            ("patch", PERSON_URL.format(id=1)),
            ("put", AUTH_LINK_URL.format(id=1)),
            ("post", DEACTIVATE_URL.format(id=1)),
            ("post", REACTIVATE_URL.format(id=1)),
            ("get", MEMBERSHIPS_URL.format(id=1)),
            ("post", MEMBERSHIPS_URL.format(id=1)),
            ("patch", MEMBERSHIP_URL.format(id=700)),
            ("post", REMOVE_URL.format(id=700)),
            ("put", CHURCH_STATUS_URL.format(id=1)),
            ("put", MINISTRY_HEAD_URL.format(id=1)),
        ],
    )
    def test_no_actor_is_401(self, client, session, method, url):
        # ``request`` rather than ``client.get(json=...)``: the TestClient's
        # GET helper takes no body, and every route must answer 401 with or
        # without one.
        response = client.request(method.upper(), url, json={})
        assert response.status_code == 401
        assert response.json()["detail"] == "Not authenticated."

    def test_the_directory_is_401_not_an_empty_list(self, client, session):
        """A stranger must not be able to tell an empty church from a closed
        door."""
        response = client.get(PEOPLE_URL)
        assert response.status_code == 401
        assert "people" not in response.json()

    def test_the_dev_header_is_ignored_when_the_flag_is_off(self, monkeypatch, session):
        monkeypatch.setenv(DEV_FLAG, "0")
        get_settings.cache_clear()
        with TestClient(app) as unflagged:
            response = unflagged.get(PEOPLE_URL, headers={HEADER: "1"})
        assert response.status_code == 401


class TestActorIsNeverAParameter:
    """The request may say what to change, never who is changing it."""

    def test_the_create_body_rejects_an_actor_field(self, client, session):
        response = client.post(
            PEOPLE_URL,
            headers={HEADER: "1"},
            json={"display_name": "New Person", "actor_person_id": 2},
        )
        assert response.status_code == 422

    def test_the_create_body_rejects_an_is_admin_field(self, client, session):
        """Authority is never smuggled in at creation."""
        response = client.post(
            PEOPLE_URL,
            headers={HEADER: "1"},
            json={"display_name": "New Person", "is_admin": True},
        )
        assert response.status_code == 422

    def test_the_edit_body_rejects_an_email_field(self, client, session):
        """The sign-in link has its own endpoint and its own collision rule."""
        response = client.patch(
            PERSON_URL.format(id=1),
            headers={HEADER: "1"},
            json={"display_name": "Renamed", "email": "someone@example.test"},
        )
        assert response.status_code == 422

    def test_the_edit_body_rejects_a_deactivated_at_field(self, client, session):
        response = client.patch(
            PERSON_URL.format(id=1),
            headers={HEADER: "1"},
            json={"display_name": "Renamed", "deactivated_at": None},
        )
        assert response.status_code == 422

    def test_the_membership_edit_body_rejects_a_head_flag(self, client, session):
        """A head editing a teammate's notes cannot promote them."""
        response = client.patch(
            MEMBERSHIP_URL.format(id=700),
            headers={HEADER: "1"},
            json={"notes": "x", "is_ministry_head": True},
        )
        assert response.status_code == 422

    @pytest.mark.parametrize(
        "field",
        ["gender", "birthday", "birth_date", "age", "is_child", "affinities", "avoidances"],
    )
    def test_no_privacy_field_is_accepted_on_create(self, client, session, field):
        """Task 79 §9: none of these concepts exists in this domain, and the
        API must not quietly start collecting them."""
        response = client.post(
            PEOPLE_URL,
            headers={HEADER: "1"},
            json={"display_name": "New Person", field: "anything"},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# Status-code mapping
# --------------------------------------------------------------------------


class TestDirectoryMapping:
    def test_an_authorized_read_returns_the_page(self, client, session, monkeypatch):
        _stub(
            monkeypatch,
            "list_people",
            DirectoryPage(
                people=(_entry(memberships=(_membership_row(),)),),
                total=1, limit=100, offset=0,
            ),
        )
        response = client.get(PEOPLE_URL, headers={HEADER: "1"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["people"][0]["display_name"] == "Ada Actor"
        assert body["people"][0]["memberships"][0]["ministry_name"] == "AV"

    def test_an_unauthorized_read_is_403_with_the_domains_own_message(
        self, client, session, monkeypatch
    ):
        _stub(
            monkeypatch,
            "list_people",
            AuthorizationError("only an Admin, or an active Ministry Head, may view"
                               " the people directory"),
        )
        response = client.get(PEOPLE_URL, headers={HEADER: "1"})
        assert response.status_code == 403
        assert "Ministry Head" in response.json()["detail"]

    def test_a_volunteer_gets_403_not_an_empty_list(self, client, session, monkeypatch):
        """Task 79 §1 says so explicitly: 403, not merely hidden UI."""
        _stub(monkeypatch, "list_people", AuthorizationError("refused"))
        response = client.get(PEOPLE_URL, headers={HEADER: "1"})
        assert response.status_code == 403
        assert "people" not in response.json()

    def test_the_search_term_reaches_the_service(self, client, session, monkeypatch):
        calls = _stub(
            monkeypatch, "list_people",
            DirectoryPage(people=(), total=0, limit=100, offset=0),
        )
        client.get(f"{PEOPLE_URL}?search=ada", headers={HEADER: "1"})
        assert calls[0]["search"] == "ada"

    def test_include_inactive_reaches_the_service(self, client, session, monkeypatch):
        calls = _stub(
            monkeypatch, "list_people",
            DirectoryPage(people=(), total=0, limit=100, offset=0),
        )
        client.get(f"{PEOPLE_URL}?include_inactive=true", headers={HEADER: "1"})
        assert calls[0]["include_inactive"] is True

    def test_an_oversized_page_is_refused_at_the_boundary(self, client, session):
        response = client.get(f"{PEOPLE_URL}?limit=100000", headers={HEADER: "1"})
        assert response.status_code == 422

    def test_a_negative_offset_is_refused_at_the_boundary(self, client, session):
        response = client.get(f"{PEOPLE_URL}?offset=-1", headers={HEADER: "1"})
        assert response.status_code == 422


class TestPersonMapping:
    def test_a_missing_person_is_404(self, client, monkeypatch):
        """An established actor asking for a person nobody has."""
        recording = RecordingSession(target_person=None)
        monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
        response = client.get(PERSON_URL.format(id=999), headers={HEADER: "1"})
        assert response.status_code == 404
        assert response.json()["detail"] == "Person not found."

    def test_a_missing_ministry_on_create_is_404(self, client, monkeypatch):
        recording = RecordingSession(ministry=None)
        monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
        response = client.post(
            PEOPLE_URL, headers={HEADER: "1"},
            json={"display_name": "New Person", "initial_ministry_id": 404},
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Ministry not found."

    def test_a_missing_membership_is_404(self, client, monkeypatch):
        recording = RecordingSession(membership=None)
        monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
        response = client.post(
            REMOVE_URL.format(id=404), headers={HEADER: "1"}, json={}
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Ministry membership not found."

    def test_creation_returns_201(self, client, session, monkeypatch):
        _stub(monkeypatch, "create_person", _StubPerson(id=5))
        _stub(monkeypatch, "read_person", _entry(person_id=5, display_name="New Person"))
        response = client.post(
            PEOPLE_URL, headers={HEADER: "1"}, json={"display_name": "New Person"}
        )
        assert response.status_code == 201
        assert response.json()["person_id"] == 5

    def test_a_duplicate_name_refusal_is_409_and_explains_itself(
        self, client, session, monkeypatch
    ):
        _stub(
            monkeypatch,
            "create_person",
            InvalidOperationError(
                "a person named 'Sam Taylor' already exists. Check whether this"
                " is the same human before creating another; confirm to create"
                " a second person with this name"
            ),
        )
        response = client.post(
            PEOPLE_URL, headers={HEADER: "1"}, json={"display_name": "Sam Taylor"}
        )
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    def test_the_duplicate_acknowledgement_reaches_the_service(
        self, client, session, monkeypatch
    ):
        calls = _stub(monkeypatch, "create_person", _StubPerson(id=5))
        _stub(monkeypatch, "read_person", _entry(person_id=5))
        client.post(
            PEOPLE_URL,
            headers={HEADER: "1"},
            json={"display_name": "Sam Taylor", "acknowledge_duplicate_name": True},
        )
        assert calls[0]["acknowledge_duplicate_name"] is True

    def test_creating_with_a_ministry_also_adds_the_membership(
        self, client, session, monkeypatch
    ):
        """One request, one transaction, two audited acts."""
        _stub(monkeypatch, "create_person", _StubPerson(id=5))
        added = _stub(monkeypatch, "add_person_to_ministry", _StubMembership())
        _stub(monkeypatch, "read_person", _entry(person_id=5))

        response = client.post(
            PEOPLE_URL,
            headers={HEADER: "1"},
            json={"display_name": "New Person", "initial_ministry_id": 900},
        )
        assert response.status_code == 201
        assert len(added) == 1
        assert added[0]["ministry"].id == 900

    def test_a_head_creating_without_a_ministry_is_403(
        self, client, session, monkeypatch
    ):
        _stub(
            monkeypatch,
            "create_person",
            AuthorizationError(
                "a Ministry Head must name the ministry a new person is being"
                " created for; only an Admin may create an unattached person"
            ),
        )
        response = client.post(
            PEOPLE_URL, headers={HEADER: "1"}, json={"display_name": "New Person"}
        )
        assert response.status_code == 403

    def test_an_admin_only_edit_refusal_is_403(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "update_person",
            AuthorizationError("only an Admin may perform this action"),
        )
        response = client.patch(
            PERSON_URL.format(id=1), headers={HEADER: "1"},
            json={"display_name": "Renamed"},
        )
        assert response.status_code == 403

    def test_deactivation_refusal_for_a_head_is_403(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "deactivate_person",
            AuthorizationError("only an Admin may perform this action"),
        )
        response = client.post(DEACTIVATE_URL.format(id=1), headers={HEADER: "1"}, json={})
        assert response.status_code == 403

    def test_self_deactivation_is_409(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "deactivate_person",
            InvalidOperationError(
                "an Admin cannot deactivate themselves; ask another Admin to do it"
            ),
        )
        response = client.post(DEACTIVATE_URL.format(id=1), headers={HEADER: "1"}, json={})
        assert response.status_code == 409

    def test_deactivate_and_reactivate_need_no_body(self, client, session, monkeypatch):
        _stub(monkeypatch, "deactivate_person", _StubPerson())
        _stub(monkeypatch, "reactivate_person", _StubPerson())
        _stub(monkeypatch, "read_person", _entry())
        assert client.post(
            DEACTIVATE_URL.format(id=1), headers={HEADER: "1"}
        ).status_code == 200
        assert client.post(
            REACTIVATE_URL.format(id=1), headers={HEADER: "1"}
        ).status_code == 200


class TestAuthLinkMapping:
    def test_a_head_is_403(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "set_person_auth_link",
            AuthorizationError("only an Admin may perform this action"),
        )
        response = client.put(
            AUTH_LINK_URL.format(id=1), headers={HEADER: "1"},
            json={"email": "someone@example.test"},
        )
        assert response.status_code == 403

    def test_a_claimed_address_is_409(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "set_person_auth_link",
            InvalidOperationError(
                "that address is already linked to another person;"
                " one address belongs to one person"
            ),
        )
        response = client.put(
            AUTH_LINK_URL.format(id=1), headers={HEADER: "1"},
            json={"email": "taken@example.test"},
        )
        assert response.status_code == 409

    def test_null_removes_the_link(self, client, session, monkeypatch):
        calls = _stub(monkeypatch, "set_person_auth_link", _StubPerson())
        _stub(monkeypatch, "read_person", _entry())
        response = client.put(
            AUTH_LINK_URL.format(id=1), headers={HEADER: "1"}, json={"email": None}
        )
        assert response.status_code == 200
        assert calls[0]["email"] is None


class TestMembershipMapping:
    def test_adding_returns_the_refreshed_list(self, client, session, monkeypatch):
        _stub(monkeypatch, "add_person_to_ministry", _StubMembership())
        _stub(monkeypatch, "list_person_memberships", (_membership_row(),))
        response = client.post(
            MEMBERSHIPS_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 900},
        )
        assert response.status_code == 200
        assert response.json()["memberships"][0]["ministry_id"] == 900

    def test_the_ministry_is_required_in_the_body(self, client, session):
        """Never inferred from the actor, even for a head of exactly one."""
        response = client.post(
            MEMBERSHIPS_URL.format(id=1), headers={HEADER: "1"}, json={}
        )
        assert response.status_code == 422

    def test_a_cross_ministry_add_is_403(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "add_person_to_ministry",
            AuthorizationError(
                "only an Admin, or an active Ministry Head of this ministry,"
                " may perform this action"
            ),
        )
        response = client.post(
            MEMBERSHIPS_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 901},
        )
        assert response.status_code == 403

    def test_a_cross_ministry_remove_is_403(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "remove_person_from_ministry",
            AuthorizationError(
                "only an Admin, or an active Ministry Head of this ministry,"
                " may perform this action"
            ),
        )
        response = client.post(
            REMOVE_URL.format(id=700), headers={HEADER: "1"}, json={}
        )
        assert response.status_code == 403

    def test_removing_a_head_as_a_head_is_403(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "remove_person_from_ministry",
            AuthorizationError("only an Admin may perform this action"),
        )
        response = client.post(REMOVE_URL.format(id=700), headers={HEADER: "1"}, json={})
        assert response.status_code == 403

    def test_the_remove_route_is_not_a_delete_verb(self):
        """The wording and the verb both say what actually happens: a
        membership is deactivated, never deleted."""
        paths = app.openapi()["paths"]
        remove = paths["/api/v1/ministry-memberships/{ministry_membership_id}/remove"]
        assert set(remove) == {"post"}

    def test_no_route_in_this_module_offers_a_delete(self):
        """Task 79 §5: nothing here may be a hard delete, and the HTTP surface
        should not even offer the verb."""
        for path, operations in app.openapi()["paths"].items():
            if path.startswith("/api/v1/people") or path.startswith(
                "/api/v1/ministry-memberships/{ministry_membership_id}"
            ):
                assert "delete" not in operations, path


class TestMinistryHeadMapping:
    """One governance route: person in the URL, ministry and direction in the
    body (Task 79 §10)."""

    def test_it_is_403_for_a_non_admin(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "set_ministry_head_authority",
            AuthorizationError("only an Admin may perform this action"),
        )
        response = client.put(
            MINISTRY_HEAD_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 900, "is_ministry_head": True},
        )
        assert response.status_code == 403

    def test_a_grant_returns_the_membership(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "set_ministry_head_authority",
            _StubMembership(is_ministry_head=True),
        )
        response = client.put(
            MINISTRY_HEAD_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 900, "is_ministry_head": True},
        )
        assert response.status_code == 200
        assert response.json()["is_ministry_head"] is True

    def test_a_revoke_returns_the_surviving_membership(
        self, client, session, monkeypatch
    ):
        """The person stays on the team; only the flag changed."""
        _stub(
            monkeypatch, "set_ministry_head_authority",
            _StubMembership(is_ministry_head=False),
        )
        body = client.put(
            MINISTRY_HEAD_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 900, "is_ministry_head": False},
        ).json()
        assert body["is_ministry_head"] is False
        assert body["deactivated_at"] is None

    def test_the_direction_reaches_the_service(
        self, client, session, monkeypatch
    ):
        calls = _stub(
            monkeypatch, "set_ministry_head_authority", _StubMembership()
        )
        client.put(
            MINISTRY_HEAD_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 900, "is_ministry_head": False},
        )
        assert calls[0]["is_ministry_head"] is False

    def test_an_invalid_operation_is_409(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "set_ministry_head_authority",
            InvalidOperationError(
                "cannot grant Ministry Head authority to a deactivated person"
            ),
        )
        response = client.put(
            MINISTRY_HEAD_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 900, "is_ministry_head": True},
        )
        assert response.status_code == 409

    def test_the_ministry_is_required(self, client, session):
        """Never inferred from the actor: an Admin says which team, always."""
        response = client.put(
            MINISTRY_HEAD_URL.format(id=1), headers={HEADER: "1"},
            json={"is_ministry_head": True},
        )
        assert response.status_code == 422

    def test_the_direction_is_required(self, client, session):
        """No default. "Set head authority" with the verb missing is not a
        request anybody meant to send."""
        response = client.put(
            MINISTRY_HEAD_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 900},
        )
        assert response.status_code == 422

    def test_the_body_cannot_name_the_actor(self, client, session):
        response = client.put(
            MINISTRY_HEAD_URL.format(id=1), headers={HEADER: "1"},
            json={"ministry_id": 900, "is_ministry_head": True, "actor_person_id": 99},
        )
        assert response.status_code == 422


class TestChurchMembershipStatusMapping:
    """Task 79 §6. Admin-only, and a different fact from ministry membership."""

    def test_it_is_403_for_a_ministry_head(self, client, session, monkeypatch):
        _stub(
            monkeypatch, "set_church_membership_status",
            AuthorizationError("only an Admin may perform this action"),
        )
        response = client.put(
            CHURCH_STATUS_URL.format(id=1), headers={HEADER: "1"},
            json={"church_membership_status": "MEMBER"},
        )
        assert response.status_code == 403

    def test_the_status_reaches_the_service(self, client, session, monkeypatch):
        calls = _stub(monkeypatch, "set_church_membership_status", _StubPerson())
        _stub(monkeypatch, "read_person", _entry(church_membership_status="MEMBER"))
        response = client.put(
            CHURCH_STATUS_URL.format(id=1), headers={HEADER: "1"},
            json={"church_membership_status": "MEMBER"},
        )
        assert response.status_code == 200
        assert calls[0]["status"] == "MEMBER"
        assert response.json()["church_membership_status"] == "MEMBER"

    def test_an_unknown_status_is_409_from_the_service(
        self, client, session, monkeypatch
    ):
        _stub(
            monkeypatch, "set_church_membership_status",
            InvalidOperationError("church_membership_status must be one of: ..."),
        )
        response = client.put(
            CHURCH_STATUS_URL.format(id=1), headers={HEADER: "1"},
            json={"church_membership_status": "VISITOR"},
        )
        assert response.status_code == 409

    def test_the_body_cannot_name_a_ministry(self, client, session):
        """Formal church membership has nothing to do with any team, and a
        field that let a caller mention one would invite the confusion this
        whole distinction exists to prevent."""
        response = client.put(
            CHURCH_STATUS_URL.format(id=1), headers={HEADER: "1"},
            json={"church_membership_status": "MEMBER", "ministry_id": 900},
        )
        assert response.status_code == 422


# --------------------------------------------------------------------------
# What a response is allowed to carry
# --------------------------------------------------------------------------


class TestResponseSurface:
    def test_a_person_response_carries_exactly_the_documented_fields(
        self, client, session, monkeypatch
    ):
        _stub(monkeypatch, "read_person", _entry(email="a@example.test", phone="555"))
        body = client.get(PERSON_URL.format(id=1), headers={HEADER: "1"}).json()
        assert set(body) == {
            "person_id", "display_name", "email", "phone", "is_admin",
            "church_membership_status", "deactivated_at", "memberships",
            "recorded_serving_total", "serving",
        }

    def test_a_directory_row_carries_no_contact_details(
        self, client, session, monkeypatch
    ):
        """**Task 79 §5.** The listing is the whole church's roll and every
        Ministry Head may read it. The table shows no address and no phone
        number, so the payload carries neither -- and the service does not
        fetch them, which is the stronger guarantee.

        The fields are *absent*, not ``null``: a ``null`` would be
        indistinguishable from "this person has no address".
        """
        _stub(
            monkeypatch,
            "list_people",
            DirectoryPage(
                people=(_entry(email="a@example.test", phone="555"),),
                total=1,
                limit=100,
                offset=0,
            ),
        )
        row = client.get(PEOPLE_URL, headers={HEADER: "1"}).json()["people"][0]

        assert "email" not in row
        assert "phone" not in row
        assert set(row) == {
            "person_id", "display_name", "is_admin", "church_membership_status",
            "deactivated_at", "memberships", "recorded_serving_total",
        }

    def test_the_detail_read_still_carries_them_for_the_admin_who_needs_them(
        self, client, session, monkeypatch
    ):
        """An Admin managing sign-in access has to see the address they are
        about to replace."""
        _stub(monkeypatch, "read_person", _entry(email="a@example.test", phone="555"))
        body = client.get(PERSON_URL.format(id=1), headers={HEADER: "1"}).json()

        assert body["email"] == "a@example.test"
        assert body["phone"] == "555"

    def test_a_membership_response_carries_exactly_the_documented_fields(
        self, client, session, monkeypatch
    ):
        _stub(monkeypatch, "list_person_memberships", (_membership_row(),))
        body = client.get(MEMBERSHIPS_URL.format(id=1), headers={HEADER: "1"}).json()
        assert set(body["memberships"][0]) == {
            "ministry_membership_id", "ministry_id", "ministry_name",
            "is_ministry_head", "deactivated_at", "notes", "joined_on",
        }

    def test_no_response_carries_a_privacy_field_this_domain_does_not_have(
        self, client, session, monkeypatch
    ):
        _stub(monkeypatch, "read_person", _entry())
        body = client.get(PERSON_URL.format(id=1), headers={HEADER: "1"}).json()
        for absent in ("gender", "birthday", "age", "is_child", "affinities",
                       "avoidances", "history"):
            assert absent not in body

    def test_the_me_endpoint_was_not_widened_by_this_task(self, client, session):
        """``/me`` is the one identity endpoint every signed-in person may
        call, and it must keep reporting only their own name and reach."""
        body = client.get("/api/v1/me", headers={HEADER: "1"}).json()
        assert set(body) == {
            "person_id", "display_name", "is_admin", "headed_ministries"
        }
        assert "email" not in body
        assert "phone" not in body


# --------------------------------------------------------------------------
# The transaction boundary
# --------------------------------------------------------------------------


class TestTransactionBoundary:
    def test_a_successful_mutation_commits_once(self, client, session, monkeypatch):
        _stub(monkeypatch, "deactivate_person", _StubPerson())
        _stub(monkeypatch, "read_person", _entry())
        client.post(DEACTIVATE_URL.format(id=1), headers={HEADER: "1"}, json={})
        assert session.events.count("commit") == 1
        assert "rollback" not in session.events

    def test_a_refused_mutation_rolls_back_and_never_commits(
        self, client, session, monkeypatch
    ):
        _stub(
            monkeypatch, "deactivate_person",
            AuthorizationError("only an Admin may perform this action"),
        )
        client.post(DEACTIVATE_URL.format(id=1), headers={HEADER: "1"}, json={})
        assert "commit" not in session.events
        assert session.events.count("rollback") == 1

    def test_a_failed_create_with_membership_rolls_the_whole_thing_back(
        self, client, session, monkeypatch
    ):
        """The Person and the membership land together or not at all."""
        _stub(monkeypatch, "create_person", _StubPerson(id=5))
        _stub(
            monkeypatch, "add_person_to_ministry",
            AuthorizationError("only an Admin, or an active Ministry Head of this"
                               " ministry, may perform this action"),
        )
        response = client.post(
            PEOPLE_URL, headers={HEADER: "1"},
            json={"display_name": "New Person", "initial_ministry_id": 900},
        )
        assert response.status_code == 403
        assert "commit" not in session.events
        assert session.events.count("rollback") == 1


# --------------------------------------------------------------------------
# Where the rules live
# --------------------------------------------------------------------------


def test_the_route_module_makes_no_authorization_decision_of_its_own():
    """Task 79 §10: all authorization lives in backend dependencies/services.

    Read as source rather than asserted through behaviour, because the claim
    is an *absence* -- and an absence is exactly what a behavioural test
    cannot prove.
    """
    import ast
    import inspect
    import re

    source = inspect.getsource(routes)

    # Docstrings and comments legitimately *discuss* these rules -- this
    # module's own docstring says "not one ``if actor.is_admin``". What must
    # not appear is a branch on one, so both are stripped before looking.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    code = ast.unparse(tree)
    code = re.sub(r"#.*$", "", code, flags=re.MULTILINE)

    # A branch on the actor's authority, or a call to one of the shared
    # authorization rules. ``is_ministry_head`` is deliberately *not* listed:
    # it is a legitimate response field name, and this file's subject is where
    # decisions are made, not where values are copied.
    for forbidden in (
        "actor.is_admin",
        "actor.deactivated_at",
        "require_active_admin",
        "require_ministry_manager",
        "require_people_directory_reader",
    ):
        assert forbidden not in code, forbidden
