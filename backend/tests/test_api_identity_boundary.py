"""The HTTP identity boundary (Task 36).

Offline: no PostgreSQL, no network.

**Test strategy.** The dev-auth gate is the security-critical part, so it is
tested through the *real* dependency chain -- the real settings object, the real
header parsing, the real `get_current_actor` -- with only the database replaced
by a recording fake. Dependency overrides are used for the database, never for
the gate itself; overriding `get_current_actor` would test nothing about whether
an unauthenticated request is refused.

The gate is also exercised directly, function by function, so a failure points
at the parsing rather than at an endpoint.
"""

from __future__ import annotations

import ast
import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api.errors import register_exception_handlers
from app.api.schemas import CurrentActorResponse, HeadedMinistry
from app.api.v1 import _headed_ministries_statement
from app.config import get_settings
from app.main import app
from app.models.core import Ministry, MinistryMembership, Person
from app.services.errors import AuthorizationError, InvalidOperationError, ServiceError

UTC = datetime.timezone.utc
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _person(
    person_id: int, name: str = "John", *, is_admin: bool = False,
    deactivated: bool = False,
) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    if deactivated:
        person.deactivated_at = DEACTIVATED
    return person


def _ministry(ministry_id: int, name: str) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


class _Row:
    """A stand-in for one ``(id, name)`` result row."""

    def __init__(self, id: int, name: str) -> None:
        self.id = id
        self.name = name


class FakeSession:
    """A recording stand-in for the request Session.

    Returns the configured Person for the actor lookup and the configured
    ministry rows for the ``/me`` query, and counts how many times it was used
    so "one Session per request" is checkable.
    """

    instances: list["FakeSession"] = []

    def __init__(self, *, person: Person | None = None, headed: list[_Row] | None = None):
        self.person = person
        self.headed = headed or []
        self.execute_calls = 0
        self.closed = False
        self.commits = 0
        self.rollbacks = 0
        FakeSession.instances.append(self)

    def execute(self, statement):
        self.execute_calls += 1
        text = str(statement)
        if "FROM person" in text:
            return _ScalarResult(self.person)
        return _RowResult(self.headed)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts with dev auth off and no dependency overrides."""
    monkeypatch.delenv(DEV_FLAG, raising=False)
    get_settings.cache_clear()
    FakeSession.instances.clear()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _enable_dev_auth(monkeypatch, value: str = "1") -> None:
    monkeypatch.setenv(DEV_FLAG, value)
    get_settings.cache_clear()


def _use_session(session: FakeSession) -> None:
    """Replace only the database, leaving the whole identity chain real."""
    app.dependency_overrides[deps.get_session] = lambda: session


# --------------------------------------------------------------------------
# 1-10 -- The development-auth gate
# --------------------------------------------------------------------------


def test_01_dev_auth_is_disabled_by_default(client):
    """Nothing set: the flag is off, which is the deployed state."""
    settings = get_settings()

    assert settings.dev_actor_auth_enabled is False
    assert settings.church_scheduling_dev_auth == ""


def test_02_the_header_does_not_authenticate_while_dev_auth_is_disabled(client):
    """The whole security boundary in one test: a real, active, existing Person
    is named, and the request is still refused.
    """
    _use_session(FakeSession(person=_person(42)))

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}


@pytest.mark.parametrize("value", ["", "0", "true", "True", "yes", "on", " 1", "1 ", "11", "01"])
def test_02b_only_the_exact_value_1_enables_dev_auth(client, monkeypatch, value):
    """No truthy surprises: a mechanism this dangerous turns on for one value."""
    _enable_dev_auth(monkeypatch, value)
    _use_session(FakeSession(person=_person(42)))

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert get_settings().dev_actor_auth_enabled is False
    assert response.status_code == 401


def test_03_the_explicit_flag_permits_actor_resolution(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42, "John")))

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert response.status_code == 200
    assert response.json()["person_id"] == 42


def test_04_a_missing_header_is_rejected_even_when_enabled(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42)))

    response = client.get("/api/v1/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}


@pytest.mark.parametrize(
    "value",
    ["abc", "", " ", "1.5", "1e3", "+5", "0x10", "5_0", "42abc", "null", "-"],
)
def test_05_a_malformed_actor_id_is_rejected(client, monkeypatch, value):
    """Strict parsing: several of these are accepted by ``int()`` and none of
    them is an id anybody meant to send.
    """
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42)))

    response = client.get("/api/v1/me", headers={HEADER: value})

    assert response.status_code == 401


def _sessionless_request():
    """A request carrying no session, for the header-parsing tests.

    Task 76 made ``_resolve_actor_person_id`` consult the session before the
    header. These tests are about the header parser, so they pass a request with
    an empty session -- which is also the state every unauthenticated request is
    in, and is what makes the parser reachable at all.
    """
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [], "method": "GET", "path": "/"})
    request.scope["session"] = {}
    return request


@pytest.mark.parametrize("value", ["٣", "１", "1٢3"])
def test_05b_non_ascii_digits_are_rejected_by_the_parser(monkeypatch, value):
    """``"٣".isdigit()`` is True and ``int("٣")`` is 3, so the ASCII check is
    doing real work. Tested against the parser directly because an HTTP client
    will not transmit a non-ASCII header value at all.
    """
    _enable_dev_auth(monkeypatch)

    with pytest.raises(HTTPException) as raised:
        deps._resolve_actor_person_id(
            request=_sessionless_request(),
            settings=get_settings(),
            dev_actor_person_id=value,
        )

    assert raised.value.status_code == 401


def test_05c_surrounding_whitespace_is_tolerated(monkeypatch):
    """Leading and trailing spaces are ordinary HTTP header padding, not a
    malformed id.
    """
    _enable_dev_auth(monkeypatch)

    assert deps._resolve_actor_person_id(
        request=_sessionless_request(),
        settings=get_settings(),
        dev_actor_person_id=" 42 ",
    ) == 42


@pytest.mark.parametrize("value", ["0", "-1", "-42"])
def test_06_a_zero_or_negative_id_is_rejected(client, monkeypatch, value):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42)))

    response = client.get("/api/v1/me", headers={HEADER: value})

    assert response.status_code == 401


def test_07_a_nonexistent_person_is_rejected(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=None))

    response = client.get("/api/v1/me", headers={HEADER: "999"})

    assert response.status_code == 401


def test_08_a_deactivated_person_is_rejected(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42, deactivated=True)))

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert response.status_code == 401


def test_08b_every_identity_failure_returns_the_same_response(client, monkeypatch):
    """Nonexistent and deactivated are indistinguishable from outside, so the
    endpoint cannot be used to enumerate which Person ids exist.
    """
    _enable_dev_auth(monkeypatch)

    _use_session(FakeSession(person=None))
    missing = client.get("/api/v1/me", headers={HEADER: "999"})
    app.dependency_overrides.clear()
    _use_session(FakeSession(person=_person(42, deactivated=True)))
    inactive = client.get("/api/v1/me", headers={HEADER: "42"})

    assert missing.status_code == inactive.status_code == 401
    assert missing.json() == inactive.json() == {"detail": "Not authenticated."}


def test_09_an_active_person_resolves(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42, "John")))

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert response.status_code == 200
    assert response.json()["display_name"] == "John"


def test_10_there_is_no_fallback_or_default_actor(monkeypatch):
    """No "first Person in the database", no hard-coded Admin, no query or body
    parameter -- checked against the module's actual code.
    """
    tree = ast.parse(Path(deps.__file__).read_text())

    # Exactly one Person query, and it is by primary key from the resolved id.
    person_selects = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "select"
        and ast.unparse(node) == "select(Person)"
    ]
    assert len(person_selects) == 1
    where_clauses = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "where"
    ]
    assert where_clauses == ["select(Person).where(Person.id == person_id)"]

    # No "first Person in the database", no ordering, no admin shortcut --
    # checked against the module's calls rather than its prose.
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for smell in ("first", "limit", "order_by", "all", "scalars"):
        assert smell not in called, smell

    # Identity arrives only as a header, never as a query or body parameter.
    used = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "Header" in used
    assert "Query" not in used
    assert "Body" not in used


# --------------------------------------------------------------------------
# 11-14 -- The boundary itself
# --------------------------------------------------------------------------


def test_11_12_one_session_serves_both_the_actor_lookup_and_the_endpoint(
    client, monkeypatch
):
    """FastAPI caches a dependency per request, so ``get_session`` runs once and
    the actor lookup and the ``/me`` query share one Session and one view.
    """
    _enable_dev_auth(monkeypatch)
    session = FakeSession(person=_person(42), headed=[_Row(3, "Setup")])
    _use_session(session)

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert response.status_code == 200
    assert len(FakeSession.instances) == 1  # no second Session opened
    assert session.execute_calls == 2  # actor lookup, then headed ministries


def test_11b_the_real_session_dependency_closes_its_session(monkeypatch):
    """Tested directly, since the endpoint tests replace it."""
    created: list[FakeSession] = []

    def fake_factory():
        session = FakeSession()
        created.append(session)
        return session

    monkeypatch.setattr(deps, "SessionLocal", fake_factory)

    generator = deps.get_session()
    session = next(generator)
    assert session.closed is False
    with pytest.raises(StopIteration):
        next(generator)

    assert created == [session]
    assert session.closed is True


def test_11c_the_session_dependency_owns_the_transaction_boundary():
    """Task 37 moved the commit here on purpose.

    The service layer's convention is unchanged -- services still never commit
    -- but "the caller" is now this dependency rather than each endpoint, so a
    mutation endpoint cannot forget the commit or commit a request that failed.
    Detailed lifecycle behaviour is proven in the Task 37 suite; this only
    pins that the calls exist in the module the boundary lives in.
    """
    source = Path(deps.__file__).read_text()
    tree = ast.parse(source)
    calls = [
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    for boundary_call in ("commit", "rollback", "close"):
        assert boundary_call in calls


def test_13_the_dependency_resolves_identity_only_not_ministry_authorization():
    """"Who is this?" and "may they do it?" are different questions; the second
    stays in the services.
    """
    # Checked against what the module imports and calls, not its prose: the
    # docstring legitimately explains that authorization lives in the services.
    tree = ast.parse(Path(deps.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.extend(alias.name for alias in node.names)

    for authorization_name in ("require_ministry_manager", "require_active_admin"):
        assert authorization_name not in imported
        assert not hasattr(deps, authorization_name)
    # It does not even reach the membership tables authorization would need.
    for model in ("MinistryMembership", "Ministry"):
        assert model not in imported
        assert not hasattr(deps, model)


def test_14_authentication_is_confined_to_the_auth_modules():
    """Task 76 added Google sign-in, and this test now pins *where* it lives.

    Before sign-in existed this asserted that no module knew anything about
    OAuth. That statement is no longer true and should not be patched to
    pretend otherwise -- but the guarantee underneath it still matters and is
    what is checked here: authentication is confined to
    :mod:`app.api.routes_auth` and :mod:`app.auth`, and the resource modules
    that carry the domain API have not grown authentication machinery of their
    own. A second place that parsed tokens or minted sessions is exactly the
    kind of drift worth failing a build over.

    ``app.main`` is excluded from the "no auth names" sweep for the one visible
    reason: it installs the session middleware, which is deliberate and cannot
    be done anywhere else.
    """
    import app.api.errors
    import app.api.schemas
    import app.api.v1

    for module in (deps, app.api.v1, app.api.schemas, app.api.errors):
        names = [n.lower() for n in vars(module)]
        for smell in ("jwt", "oauth", "jose", "passlib", "authlib", "password"):
            assert not any(smell in n for n in names), (module.__name__, smell)

    # No password authentication and no hand-rolled token handling anywhere,
    # including in the auth modules themselves -- Google is the only identity
    # provider, and Authlib is the only thing that touches a token.
    import app.api.routes_auth
    import app.auth.email_link
    import app.auth.google
    import app.auth.session

    for package in ("python-jose", "pyjwt", "passlib", "google-auth"):
        assert package not in Path("pyproject.toml").read_text()

    for module in (
        app.api.routes_auth,
        app.auth.google,
        app.auth.session,
        app.auth.email_link,
    ):
        names = [n.lower() for n in vars(module)]
        for smell in ("password", "passlib", "jose", "pyjwt"):
            assert not any(smell in n for n in names), (module.__name__, smell)


# --------------------------------------------------------------------------
# 15-22 -- GET /api/v1/me
# --------------------------------------------------------------------------


def test_15_an_authenticated_person_gets_their_identity(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42, "John Smith")))

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert response.status_code == 200
    assert response.json() == {
        "person_id": 42,
        "display_name": "John Smith",
        "is_admin": False,
        "headed_ministries": [],
    }


@pytest.mark.parametrize("is_admin", [True, False])
def test_16_the_admin_flag_is_reported_accurately(client, monkeypatch, is_admin):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42, is_admin=is_admin)))

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert response.json()["is_admin"] is is_admin


def test_16b_an_admin_gets_no_invented_headed_ministries(client, monkeypatch):
    """Admin may manage every ministry; this field reports membership state,
    and inventing rows would make the response disagree with the database.
    """
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(1, "Admin", is_admin=True), headed=[]))

    response = client.get("/api/v1/me", headers={HEADER: "1"})

    assert response.json()["is_admin"] is True
    assert response.json()["headed_ministries"] == []


def test_17_an_active_headed_ministry_is_returned(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use_session(FakeSession(person=_person(42), headed=[_Row(3, "Setup")]))

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert response.json()["headed_ministries"] == [{"ministry_id": 3, "name": "Setup"}]


def test_18_several_headed_ministries_are_ordered_deterministically(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    _use_session(
        FakeSession(person=_person(42), headed=[_Row(4, "AV"), _Row(3, "Setup")])
    )

    response = client.get("/api/v1/me", headers={HEADER: "42"})

    assert [m["name"] for m in response.json()["headed_ministries"]] == ["AV", "Setup"]
    # The order comes from SQL, not from Python re-sorting a set.
    compiled = str(_headed_ministries_statement(42))
    assert "ORDER BY ministry.name, ministry.id" in compiled


def test_19_20_only_active_head_memberships_count(client, monkeypatch):
    """Both exclusions live in the query, so an ordinary membership can never be
    reported as a headship.
    """
    from sqlalchemy.dialects import postgresql

    compiled = str(
        _headed_ministries_statement(42).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "ministry_membership.person_id = 42" in compiled
    assert "ministry_membership.is_ministry_head IS true" in compiled
    assert "ministry_membership.deactivated_at IS NULL" in compiled


def test_21_the_response_exposes_no_orm_object_or_internal_fields(client, monkeypatch):
    _enable_dev_auth(monkeypatch)
    person = _person(42, "John")
    person.email = "john@example.test"
    person.phone = "555"
    _use_session(FakeSession(person=person))

    body = client.get("/api/v1/me", headers={HEADER: "42"}).json()

    assert set(body) == {"person_id", "display_name", "is_admin", "headed_ministries"}
    for leaked in ("email", "phone", "church_id", "created_at", "updated_at",
                   "deactivated_at", "user_account", "google_subject"):
        assert leaked not in body
    assert "john@example.test" not in str(body)
    # The DTO cannot be built from an ORM row by accident.
    assert CurrentActorResponse.model_config.get("from_attributes") is not True
    assert HeadedMinistry.model_config.get("from_attributes") is not True


def test_22_an_unauthenticated_request_cannot_reach_me(client):
    _use_session(FakeSession(person=_person(42)))

    response = client.get("/api/v1/me")

    assert response.status_code == 401
    assert "person_id" not in response.json()


# --------------------------------------------------------------------------
# 23-26 -- Domain exception mapping
# --------------------------------------------------------------------------


@pytest.fixture
def error_client() -> TestClient:
    """A throwaway app with the real handlers, so the production app needs no
    test-only routes to prove the mapping.
    """
    error_app = FastAPI()
    register_exception_handlers(error_app)

    @error_app.get("/authorization")
    def raise_authorization():
        raise AuthorizationError("only an Admin may perform this action")

    @error_app.get("/invalid")
    def raise_invalid():
        raise InvalidOperationError("cannot assign a deactivated person")

    @error_app.get("/service")
    def raise_service():
        raise ServiceError("an unmapped domain error")

    @error_app.get("/unexpected")
    def raise_unexpected():
        raise RuntimeError("something genuinely broke")

    return TestClient(error_app, raise_server_exceptions=False)


def test_23_an_authorization_error_becomes_403(error_client):
    response = error_client.get("/authorization")

    assert response.status_code == 403
    assert response.json() == {"detail": "only an Admin may perform this action"}


def test_24_an_invalid_operation_error_becomes_409(error_client):
    """409, not 400: the request was well-formed and the client could not have
    known the state disagreed with it.
    """
    response = error_client.get("/invalid")

    assert response.status_code == 409
    assert response.json() == {"detail": "cannot assign a deactivated person"}


def test_25_the_error_shape_is_small_and_stable(error_client):
    for path in ("/authorization", "/invalid"):
        body = error_client.get(path).json()

        assert set(body) == {"detail"}
        assert isinstance(body["detail"], str)
        assert "Traceback" not in body["detail"]
        assert "File \"" not in body["detail"]


def test_26_an_unexpected_exception_is_not_swallowed_as_a_domain_4xx(error_client):
    response = error_client.get("/unexpected")

    assert response.status_code == 500
    assert "something genuinely broke" not in response.text


def test_26b_an_unmapped_domain_error_is_not_guessed_into_a_4xx(error_client):
    """No handler is registered for the ``ServiceError`` base class: an error
    nobody has decided the meaning of should surface, not be miscategorised.
    """
    response = error_client.get("/service")

    assert response.status_code == 500


# --------------------------------------------------------------------------
# 27 -- Health
# --------------------------------------------------------------------------


def test_27_the_health_endpoint_is_unchanged(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # Still unversioned, and it needs no actor.
    assert client.get("/api/v1/health").status_code == 404


# --------------------------------------------------------------------------
# 28-32 -- Structural guarantees
# --------------------------------------------------------------------------


def test_28_no_route_accepts_an_actor_id_parameter():
    """Identity comes from the dependency, never from the request."""
    schema = app.openapi()

    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            names = {p["name"].lower() for p in operation.get("parameters", [])}
            for name in names:
                assert "actor_person_id" not in name.replace("-", "_") or name == (
                    "x-dev-actor-person-id"
                ), (path, method, name)
            # The only actor-bearing parameter is the dev header, and it is a
            # header rather than a query or body field.
            for parameter in operation.get("parameters", []):
                if "actor" in parameter["name"].lower():
                    assert parameter["in"] == "header"


def _installed_middleware_names(application) -> set[str]:
    """The middleware classes actually installed on ``application``.

    Task 76 installs exactly one, ``SessionMiddleware``, to carry the signed
    sign-in cookie. These assertions previously read ``app.user_middleware ==
    []``, which was the right statement while the answer was "none" but says
    nothing about *which* middleware would be wrong. Comparing the set of names
    keeps the real guarantee -- no CORS policy was introduced -- and still fails
    if anything else is added without a test being updated to name it.
    """
    return {
        middleware.cls.__name__ for middleware in application.user_middleware
    }

def test_29_no_cors_middleware_is_configured():
    """A wildcard would be a security decision made by default; CORS belongs to
    the task that connects a real frontend.
    """
    import app.main as main_module

    installed = [str(middleware) for middleware in app.user_middleware]
    assert not any("CORS" in entry for entry in installed), installed
    assert _installed_middleware_names(app) == {"SessionMiddleware"}

    tree = ast.parse(Path(main_module.__file__).read_text())
    used = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "CORSMiddleware" not in used
    assert not hasattr(main_module, "CORSMiddleware")


def test_30_no_hard_coded_person_or_admin_fallback_exists():
    for module_file in Path("app/api").rglob("*.py"):
        tree = ast.parse(module_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "Person", module_file
            # No literal ids standing in for a person.
            if isinstance(node, ast.Assign):
                rendered = ast.unparse(node)
                assert "person_id = 1" not in rendered, module_file


def test_31_the_authentication_dependencies_are_the_three_intended_ones():
    """Task 76's dependency list, pinned.

    This previously asserted that *no* authentication dependency existed, which
    was correct while authentication was a later task. Sign-in is now
    implemented, so the useful guarantee is the narrower one: exactly three
    packages were added, each with a stated job, and no password-hashing or
    hand-rolled-JWT library came along with them.
    """
    pyproject = Path("pyproject.toml").read_text()

    assert "fastapi" in pyproject  # already there
    # Added deliberately by Task 76. httpx2, not httpx: Authlib's httpx
    # integration imports httpx2 and treats plain httpx as deprecated.
    for package in ("authlib", "httpx2", "itsdangerous"):
        assert package in pyproject, package
    # Still absent, and should stay absent: this application has no passwords
    # and validates no token by hand.
    for package in ("python-jose", "pyjwt", "passlib", "google-auth",
                    "starlette-session", "flask-login"):
        assert package not in pyproject, package


def test_32_no_model_or_migration_change_was_needed():
    """The boundary reads existing tables; nothing about identity storage
    changed, and production authentication is explicitly not this task.
    """
    migrations = sorted(Path("alembic/versions").glob("*.py"))

    # Ten since Task 79 added ``person.church_membership_status``. That column
    # is formal *church* membership -- governance, stated by an Admin and never
    # inferred -- and it changes nothing about how identity is *stored* or
    # resolved, which is what this test is really asserting and which is
    # checked directly below. The count is only here to make a new migration a
    # noticed event rather than a silent one.
    assert len(migrations) == 10
    identity_tables = ("person", "user_account")
    for migration in migrations:
        source = migration.read_text()
        if "create_core_identity_and_ministry_schema" in migration.name:
            continue  # the one that legitimately creates them
        for table in identity_tables:
            assert f"op.alter_column('{table}'" not in source, migration.name
            assert f"op.drop_table('{table}'" not in source, migration.name
    for module_file in Path("app/api").rglob("*.py"):
        source = module_file.read_text()
        assert "mapped_column" not in source
        assert "__tablename__" not in source
