"""Production fails closed (Task 76, Phase 5).

Offline: no PostgreSQL, no network.

**What this file is for.** The development actor header is the most dangerous
thing in this codebase: anyone who can send ``X-Dev-Actor-Person-Id`` to a
service that honours it is that Person, Admin included. It exists because
building the UI against a real Google round trip would be miserable, and it is
worth keeping -- but only if it is *impossible* for it to survive into a
deployment.

The rule Task 76 added, and which these tests exist to hold in place:

    ``APP_ENV=production`` disables the header, whatever else is set.

That is deliberately not the same as "remember not to set
``CHURCH_SCHEDULING_DEV_AUTH`` in production". Anyone who can set one
environment variable on a Cloud Run service can set the other, and a
misapplied revision, a copied deploy command or a ``.env`` that reaches a
container it was never meant to would each be enough. Production authenticates
through Google or not at all, and no combination of environment variables can
argue with it.

Every identity here is synthetic.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.config import Settings, get_settings
from app.main import app
from app.models.core import Person

UTC = datetime.timezone.utc
DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
ME = "/api/v1/me"


def _person(person_id: int, *, is_admin: bool = False) -> Person:
    person = Person(display_name="Synthetic Person", is_admin=is_admin, church_id=1)
    person.id = person_id
    return person


class FakeSession:
    """Answers the actor lookup with one Person, whoever is asked for.

    Deliberately permissive: it says "yes, that Person exists and is active" to
    every id. If the boundary is going to fail, the database must not be the
    thing that saves it.
    """

    def __init__(self, person: Person | None) -> None:
        self.person = person

    def execute(self, statement):
        if "FROM person" in str(statement):
            return _Scalar(self.person)
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
    for name in (DEV_FLAG, "APP_ENV", "SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _use_person(person: Person | None) -> None:
    app.dependency_overrides[deps.get_session] = lambda: FakeSession(person)


def _configure(monkeypatch, **env: str) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


# ==========================================================================
# 1-8 -- The production override, at the settings level
# ==========================================================================


def test_01_dev_auth_is_off_by_default(monkeypatch):
    assert Settings(_env_file=None, database_url="x").dev_actor_auth_enabled is False


def test_02_dev_auth_is_on_in_development_with_the_exact_flag(monkeypatch):
    settings = Settings(
        _env_file=None, database_url="x", app_env="development",
        church_scheduling_dev_auth="1",
    )

    assert settings.dev_actor_auth_enabled is True


def test_03_production_overrides_the_flag(monkeypatch):
    """**The rule.** The flag says yes; production says no; no wins."""
    settings = Settings(
        _env_file=None, database_url="x", app_env="production",
        church_scheduling_dev_auth="1",
    )

    assert settings.is_production is True
    assert settings.dev_actor_auth_enabled is False


@pytest.mark.parametrize(
    "app_env", ["production", "Production", "PRODUCTION", " production ", "pRoDuCtIoN"]
)
def test_04_production_is_recognised_however_it_is_spelled(app_env):
    """The asymmetry with the flag's exact-match rule is the point, and it runs
    in the safe direction in both cases.

    The flag *grants* a dangerous power, so exactly one spelling enables it.
    This *withdraws* one, so every plausible spelling must still withdraw it --
    reading ``"Production"`` as "not production" would re-enable the header on
    the one deployment that must never have it.
    """
    settings = Settings(
        _env_file=None, database_url="x", app_env=app_env,
        church_scheduling_dev_auth="1",
    )

    assert settings.is_production is True
    assert settings.dev_actor_auth_enabled is False


@pytest.mark.parametrize("app_env", ["development", "test", "staging", "prod", ""])
def test_05_only_production_itself_withdraws_the_header(app_env):
    """No guessing at near-misses. ``prod`` and ``staging`` are not the string
    this project uses, and silently treating them as production would hide a
    misconfiguration rather than fix one -- the deployment would appear to work
    while being configured wrongly.
    """
    settings = Settings(
        _env_file=None, database_url="x", app_env=app_env,
        church_scheduling_dev_auth="1",
    )

    assert settings.is_production is False


@pytest.mark.parametrize("flag", ["", "0", "true", "True", "yes", "on", " 1", "1 ", "01"])
def test_06_no_truthy_value_enables_the_flag_even_in_development(flag):
    settings = Settings(
        _env_file=None, database_url="x", app_env="development",
        church_scheduling_dev_auth=flag,
    )

    assert settings.dev_actor_auth_enabled is False


# ==========================================================================
# 7-13 -- The production override, over real HTTP
# ==========================================================================


def test_07_the_header_authenticates_in_development(client, monkeypatch):
    """The baseline. Without this passing, the tests below prove nothing --
    they could all be green because the header never worked at all.
    """
    _configure(monkeypatch, APP_ENV="development", CHURCH_SCHEDULING_DEV_AUTH="1")
    _use_person(_person(42))

    assert client.get(ME, headers={HEADER: "42"}).status_code == 200


def test_08_the_same_request_is_refused_in_production(client, monkeypatch):
    """The whole boundary in one test: identical request, identical database,
    identical flag -- only APP_ENV differs.
    """
    _configure(monkeypatch, APP_ENV="production", CHURCH_SCHEDULING_DEV_AUTH="1")
    _use_person(_person(42))

    response = client.get(ME, headers={HEADER: "42"})

    assert response.status_code == 401
    assert response.json() == {"detail": "Not authenticated."}


def test_09_an_admin_cannot_be_impersonated_in_production(client, monkeypatch):
    """The worst case, named explicitly."""
    _configure(monkeypatch, APP_ENV="production", CHURCH_SCHEDULING_DEV_AUTH="1")
    _use_person(_person(1, is_admin=True))

    assert client.get(ME, headers={HEADER: "1"}).status_code == 401


def test_10_every_endpoint_is_refused_not_only_me(client, monkeypatch):
    """The gate is the shared dependency, so this holds for the whole API --
    checked against several real routes rather than assumed from one.
    """
    _configure(monkeypatch, APP_ENV="production", CHURCH_SCHEDULING_DEV_AUTH="1")
    _use_person(_person(1, is_admin=True))

    for path in (
        ME,
        "/api/v1/ministries/1/scheduling-periods",
        "/api/v1/ministries/1/roles",
        "/api/v1/schedule-versions/1",
        "/api/v1/scheduling-periods/1/events",
        "/api/v1/scheduling-periods/1/serving-limits",
        "/api/v1/scheduling-periods/1/scheduling-rules",
    ):
        assert client.get(path, headers={HEADER: "1"}).status_code == 401, path


def test_11_a_mutating_endpoint_is_refused_in_production_too(client, monkeypatch):
    _configure(monkeypatch, APP_ENV="production", CHURCH_SCHEDULING_DEV_AUTH="1")
    _use_person(_person(1, is_admin=True))

    response = client.post(
        "/api/v1/ministries/1/roles",
        json={"name": "Invented Role", "description": None},
        headers={HEADER: "1"},
    )

    assert response.status_code == 401


def test_12_health_is_still_reachable_in_production(client, monkeypatch):
    """Cloud Run needs it, and it exposes nothing."""
    _configure(monkeypatch, APP_ENV="production", CHURCH_SCHEDULING_DEV_AUTH="1")

    assert client.get("/health").status_code == 200


def test_13_the_header_is_ignored_rather_than_reported(client, monkeypatch):
    """A production deployment does not tell a caller that a development mode
    exists, or that their header was noticed.
    """
    _configure(monkeypatch, APP_ENV="production", CHURCH_SCHEDULING_DEV_AUTH="1")
    _use_person(_person(42))

    body = client.get(ME, headers={HEADER: "42"}).text

    assert "dev" not in body.lower()
    assert "CHURCH_SCHEDULING_DEV_AUTH" not in body


# ==========================================================================
# 14-17 -- A session still beats the header, and the header cannot beat one
# ==========================================================================


def test_14_a_session_takes_precedence_over_the_header(client, monkeypatch):
    """Even where the header is enabled, a signed-in developer cannot silently
    act as somebody else by adding one.
    """
    from app.auth.session import SESSION_COOKIE_NAME
    from app.api import routes_auth

    _configure(
        monkeypatch,
        APP_ENV="development",
        CHURCH_SCHEDULING_DEV_AUTH="1",
        SESSION_SECRET="synthetic-session-signing-key-for-tests",
        GOOGLE_OAUTH_CLIENT_ID="synthetic.apps.googleusercontent.com",
        GOOGLE_OAUTH_CLIENT_SECRET="synthetic-secret",
    )

    class _Fake:
        async def authorize_access_token(self, request):
            return {"userinfo": {
                "iss": "https://accounts.google.com",
                "email": "linked@example.test",
                "email_verified": True,
            }}

    monkeypatch.setattr(routes_auth, "build_oauth", lambda settings: _Fake())

    linked = _person(7)
    linked.email = "linked@example.test"

    class _Both:
        def execute(self, statement):
            compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
            if "lower(person.email)" in compiled:
                return _Scalar(linked)
            if "FROM person" in compiled:
                # Whichever id is asked for, answer with a Person carrying it,
                # so the test cannot pass merely because id 99 does not exist.
                wanted = compiled.split("person.id = ", 1)[1].split()[0]
                return _Scalar(linked if wanted.strip() == "7" else _person(99, is_admin=True))
            return _Rows([])

        def commit(self): ...
        def rollback(self): ...
        def close(self): ...

    app.dependency_overrides[deps.get_session] = lambda: _Both()

    client.get("/api/v1/auth/google/callback?code=c&state=s")
    assert SESSION_COOKIE_NAME in client.cookies

    # Signed in as 7, and now asking to be 99 -- an existing, active Admin.
    body = client.get(ME, headers={HEADER: "99"}).json()

    assert body["person_id"] == 7
    assert body["is_admin"] is False


def test_15_the_resolution_order_is_session_then_header(monkeypatch):
    """Checked against the code, so the ordering cannot be swapped back without
    a test noticing.
    """
    import ast
    from pathlib import Path

    source = Path(deps.__file__).read_text()
    tree = ast.parse(source)
    resolver = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_resolve_actor_person_id"
    )
    # Statements only. The docstring explains the ordering in prose and would
    # otherwise be what this test measured -- it names all three in the right
    # order regardless of what the code below it does.
    statements = [
        node for node in resolver.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    body = "\n".join(ast.unparse(node) for node in statements)

    session_read = body.index("read_session_person_id")
    gate_check = body.index("dev_actor_auth_enabled")
    header_read = body.index("dev_actor_person_id is None")

    assert session_read < gate_check < header_read


def test_16_the_production_gate_lives_in_settings_not_in_the_route(monkeypatch):
    """One place, so every consumer of the flag inherits the rule rather than
    each remembering to re-check APP_ENV.
    """
    import ast
    from pathlib import Path

    from app import config

    tree = ast.parse(Path(config.__file__).read_text())
    prop = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "dev_actor_auth_enabled"
    )

    assert "is_production" in ast.unparse(prop)


def test_17_production_refuses_to_start_without_sign_in_configured(monkeypatch):
    """A deployment with no SESSION_SECRET must crash rather than start with
    sessions anybody can forge -- and the error names settings, never values.
    """
    from app.main import _session_secret

    settings = Settings(_env_file=None, database_url="x", app_env="production")

    with pytest.raises(RuntimeError) as raised:
        _session_secret(settings)

    message = str(raised.value)
    assert "SESSION_SECRET" in message
    assert "GOOGLE_OAUTH_CLIENT_ID" in message
    assert "x" not in message.replace("Missing", "").replace("sign-in", "")


def test_18_local_development_starts_without_any_of_it(monkeypatch):
    """Running the API to look at a page should not require configuring
    authentication first.
    """
    from app.main import _session_secret

    settings = Settings(_env_file=None, database_url="x", app_env="development")

    assert len(_session_secret(settings)) > 20


def test_19_the_production_session_cookie_is_secure(monkeypatch):
    from app.main import session_middleware_options

    options = session_middleware_options(
        Settings(_env_file=None, database_url="x", app_env="production",
                 session_secret="synthetic-key")
    )

    assert options["https_only"] is True
    assert options["same_site"] == "lax"
    assert options["max_age"] == 8 * 60 * 60


def test_20_the_local_session_cookie_is_not_secure_so_localhost_works(monkeypatch):
    from app.main import session_middleware_options

    options = session_middleware_options(
        Settings(_env_file=None, database_url="x", app_env="development",
                 session_secret="synthetic-key")
    )

    assert options["https_only"] is False
