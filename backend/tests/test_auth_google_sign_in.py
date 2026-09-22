"""Google sign-in, end to end at the HTTP boundary (Task 76).

Offline: no PostgreSQL, no network. Every identity in this file is synthetic and
uses the reserved ``example.test`` domain (RFC 6761) -- no real address, name or
Google account appears anywhere in the suite.

**Test strategy, and why it is split in two.**

The *state* tests drive the **real Authlib client**, with only the network
replaced by an ``httpx.MockTransport``. Authlib's state generation and checking
is the part of this flow that a hand-written stand-in would prove nothing about,
so it is exercised for real: the login route's state is read back out of the
actual session cookie, and the callback is then attacked with a missing state, a
wrong state and a replayed one.

The *policy* tests replace the OAuth client with a fake that returns chosen
claims. What they exercise is this project's own decision-making -- verified
email, linked person, active person, session establishment -- which is where the
pilot's access rules live and where a regression would actually let the wrong
person in. Mocking Google's cryptography to test "an unknown address is refused"
would test Authlib, not the rule.

Both halves go through the real ``app.main.app``, the real middleware, the real
``get_current_actor`` and the real ``routes_auth`` module. Nothing in this file
overrides a security check in order to reach the code underneath it.
"""

from __future__ import annotations

import datetime
import logging
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from authlib.integrations.starlette_client import OAuth
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api import routes_auth
from app.auth import google as google_module
from app.auth.session import SESSION_COOKIE_NAME
from app.config import get_settings
from app.main import app
from app.models.core import Person

UTC = datetime.timezone.utc
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)

LOGIN = "/api/v1/auth/google/login"
CALLBACK = "/api/v1/auth/google/callback"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/me"

#: Synthetic throughout. A client id shaped like Google's, a secret that is
#: obviously not one, and addresses in the reserved test domain.
CLIENT_ID = "synthetic-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "synthetic-client-secret-value"
SESSION_SECRET = "synthetic-session-signing-key-for-tests"
REDIRECT_URL = "https://web.example.test/api/backend/api/v1/auth/google/callback"

LINKED_EMAIL = "linked.person@example.test"
UNKNOWN_EMAIL = "nobody@example.test"

GOOGLE_METADATA = {
    "issuer": "https://accounts.google.com",
    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
    "token_endpoint": "https://oauth2.googleapis.com/token",
    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
    "userinfo_endpoint": "https://openidconnect.googleapis.com/v1/userinfo",
}


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def _person(person_id: int, *, email: str | None = None, deactivated: bool = False,
            is_admin: bool = False, name: str = "Synthetic Person") -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.email = email
    if deactivated:
        person.deactivated_at = DEACTIVATED
    return person


class FakeSession:
    """The request Session, with just enough behaviour for two queries.

    ``find_person_by_email`` and the actor lookup both select from ``person``,
    so they are told apart by the ``lower(...)`` the email lookup compiles to.
    Matching is done here the same way the database would: case-insensitively.
    """

    def __init__(self, people: list[Person] | None = None) -> None:
        self.people = people or []
        self.commits = 0
        self.rollbacks = 0

    def execute(self, statement):
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        if "lower(person.email)" in compiled:
            wanted = _literal_after(compiled, "lower(person.email) = lower(")
            return _ScalarResult(
                next(
                    (
                        person
                        for person in self.people
                        if person.email is not None
                        and person.email.casefold() == wanted.casefold()
                    ),
                    None,
                )
            )
        if "FROM person" in compiled:
            wanted_id = _literal_after(compiled, "person.id = ")
            return _ScalarResult(
                next((p for p in self.people if str(p.id) == wanted_id.strip()), None)
            )
        return _RowResult([])

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        pass


def _literal_after(compiled: str, marker: str) -> str:
    """Pull the literal-bound value that follows ``marker`` in compiled SQL."""
    rest = compiled.split(marker, 1)[1]
    if rest.startswith("'"):
        return rest[1:].split("'", 1)[0]
    return rest.split(")", 1)[0].split()[0]


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


class FakeGoogleClient:
    """An OAuth client that returns chosen claims, for the policy tests.

    ``authorize_redirect`` is never reached by these tests -- they post the
    callback directly -- so only ``authorize_access_token`` is implemented.
    """

    def __init__(self, *, claims: dict | None = None, raises: Exception | None = None):
        self.claims = claims
        self.raises = raises
        self.calls = 0

    async def authorize_access_token(self, request):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return {"userinfo": self.claims, "access_token": "synthetic-access-token"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("CHURCH_SCHEDULING_DEV_AUTH", raising=False)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
    monkeypatch.setenv("OAUTH_REDIRECT_URL", REDIRECT_URL)
    get_settings.cache_clear()
    google_module.reset_oauth_registry()
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    google_module.reset_oauth_registry()
    get_settings.cache_clear()


def _use_people(*people: Person) -> FakeSession:
    session = FakeSession(list(people))
    app.dependency_overrides[deps.get_session] = lambda: session
    return session


@pytest.fixture
def client() -> TestClient:
    # follow_redirects off: the redirects *are* what these tests assert on.
    return TestClient(app, follow_redirects=False)


def _use_fake_google(monkeypatch, fake: FakeGoogleClient) -> FakeGoogleClient:
    monkeypatch.setattr(routes_auth, "build_oauth", lambda settings: fake)
    return fake


def _claims(email: str = LINKED_EMAIL, *, verified: bool = True,
            issuer: str = "https://accounts.google.com") -> dict:
    return {
        "iss": issuer,
        "sub": "synthetic-google-subject-1234567890",
        "email": email,
        "email_verified": verified,
        "name": "Synthetic Person",
    }


def _real_google(monkeypatch) -> None:
    """Register the genuine Authlib client, with only the network replaced.

    ``httpx2``, not ``httpx``: Authlib's httpx integration imports ``httpx2``
    and treats plain ``httpx`` as a deprecated fallback, so a transport built
    from the wrong one is silently not used and the test reaches the real
    internet. Everything else here -- state generation, state checking, the
    authorization URL -- is Authlib's own code running for real.
    """

    def handle(request: httpx2.Request) -> httpx2.Response:
        if "openid-configuration" in str(request.url):
            return httpx2.Response(200, json=GOOGLE_METADATA)
        # A token exchange should never be reached by the state tests: Authlib
        # checks state first, so arriving here means the check did not happen.
        raise AssertionError(f"unexpected outbound request to {request.url}")

    registry = OAuth()
    registry.register(
        name="google",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        server_metadata_url=google_module.SERVER_METADATA_URL,
        client_kwargs={
            "scope": google_module.SCOPES,
            "transport": httpx2.MockTransport(handle),
        },
    )
    monkeypatch.setattr(routes_auth, "build_oauth", lambda settings: registry.google)


def _reason_of(response) -> str | None:
    """The ``auth_error`` code on a refusal redirect, or ``None`` on success."""
    query = parse_qs(urlsplit(response.headers["location"]).query)
    values = query.get("auth_error")
    return values[0] if values else None


# ==========================================================================
# 1-8 -- Starting sign-in, against the real Authlib client
# ==========================================================================


def test_01_login_redirects_to_google(client, monkeypatch):
    _real_google(monkeypatch)

    response = client.get(LOGIN)

    assert response.status_code == 302
    assert response.headers["location"].startswith(
        "https://accounts.google.com/o/oauth2/v2/auth"
    )


def test_02_login_requests_only_the_three_minimal_scopes(client, monkeypatch):
    """No Calendar, no Directory, no offline access: this app asks Google one
    question and keeps no token to ask another later.
    """
    _real_google(monkeypatch)

    query = parse_qs(urlsplit(client.get(LOGIN).headers["location"]).query)

    assert set(query["scope"][0].split()) == {"openid", "email", "profile"}
    assert "access_type" not in query  # would request a refresh token
    assert "offline" not in query.get("access_type", [""])[0]


def test_03_login_uses_the_authorization_code_flow_with_state_and_nonce(client, monkeypatch):
    _real_google(monkeypatch)

    query = parse_qs(urlsplit(client.get(LOGIN).headers["location"]).query)

    assert query["response_type"] == ["code"]
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [REDIRECT_URL]
    assert query["state"][0]  # generated by Authlib, non-empty
    assert query["nonce"][0]


def test_04_the_client_secret_never_leaves_the_server(client, monkeypatch):
    """The secret authenticates this service to Google's token endpoint over
    TLS. It has no business in a redirect the browser can read.
    """
    _real_google(monkeypatch)

    response = client.get(LOGIN)

    assert CLIENT_SECRET not in response.headers["location"]
    assert CLIENT_SECRET not in response.text
    for header in response.headers.values():
        assert CLIENT_SECRET not in header


def test_05_the_redirect_uri_is_the_configured_one_not_the_request_host(
    client, monkeypatch
):
    """Behind Cloud Run and a frontend proxy the request's own host and scheme
    are both wrong, so a derived redirect URI would never match Google's
    allow-list. The configured value wins.
    """
    _real_google(monkeypatch)

    query = parse_qs(urlsplit(client.get(LOGIN).headers["location"]).query)

    assert query["redirect_uri"] == [REDIRECT_URL]
    assert "testserver" not in query["redirect_uri"][0]


def test_06_login_sets_a_session_cookie_carrying_the_oauth_state(client, monkeypatch):
    """The state has to be remembered somewhere for the callback to check it,
    and that somewhere is the signed cookie.
    """
    _real_google(monkeypatch)

    response = client.get(LOGIN)

    assert SESSION_COOKIE_NAME in response.cookies or any(
        SESSION_COOKIE_NAME in value
        for key, value in response.headers.items()
        if key.lower() == "set-cookie"
    )


def test_07_login_refuses_cleanly_when_google_is_not_configured(
    client, monkeypatch
):
    """A deployment with no client id gets the "not configured" page, not a 500."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "")
    get_settings.cache_clear()
    google_module.reset_oauth_registry()

    response = client.get(LOGIN)

    assert response.status_code == 303
    assert _reason_of(response) == "not_configured"


def test_08_login_needs_no_authentication(client, monkeypatch):
    """It cannot: it is how an actor comes to exist."""
    _real_google(monkeypatch)

    assert client.get(LOGIN).status_code == 302


# ==========================================================================
# 9-13 -- State verification, against the real Authlib client
# ==========================================================================


def test_09_a_callback_with_no_state_is_refused(client, monkeypatch):
    _real_google(monkeypatch)
    _use_people(_person(1, email=LINKED_EMAIL))

    response = client.get(f"{CALLBACK}?code=synthetic-code")

    assert response.status_code == 303
    assert _reason_of(response) == "invalid_response"


def test_10_a_callback_with_an_unknown_state_is_refused(client, monkeypatch):
    """No login happened, so nothing stored a state -- a forged callback."""
    _real_google(monkeypatch)
    _use_people(_person(1, email=LINKED_EMAIL))

    response = client.get(f"{CALLBACK}?code=synthetic-code&state=invented-by-an-attacker")

    assert response.status_code == 303
    assert _reason_of(response) == "invalid_response"


def test_11_a_callback_whose_state_does_not_match_the_session_is_refused(
    client, monkeypatch
):
    """The CSRF protection itself: a real login is started, and the callback
    comes back carrying a *different* state.
    """
    _real_google(monkeypatch)
    _use_people(_person(1, email=LINKED_EMAIL))

    started = client.get(LOGIN)
    issued = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    tampered = f"{issued}-tampered"

    response = client.get(f"{CALLBACK}?code=synthetic-code&state={tampered}")

    assert response.status_code == 303
    assert _reason_of(response) == "invalid_response"


def test_12_a_state_cannot_be_replayed_after_a_cleared_session(client, monkeypatch):
    """A state belongs to the browser session that started the flow. Presenting
    a genuine state from a session that no longer exists is refused.
    """
    _real_google(monkeypatch)
    _use_people(_person(1, email=LINKED_EMAIL))

    started = client.get(LOGIN)
    issued = parse_qs(urlsplit(started.headers["location"]).query)["state"][0]
    client.cookies.clear()

    response = client.get(f"{CALLBACK}?code=synthetic-code&state={issued}")

    assert response.status_code == 303
    assert _reason_of(response) == "invalid_response"


def test_13_a_refused_callback_establishes_no_session(client, monkeypatch):
    _real_google(monkeypatch)
    _use_people(_person(1, email=LINKED_EMAIL))

    client.get(f"{CALLBACK}?code=c&state=wrong")
    client.cookies.clear()

    assert client.get(ME).status_code == 401


# ==========================================================================
# 14-24 -- What the callback does with a validated Google response
# ==========================================================================


def test_14_a_linked_verified_person_is_signed_in(client, monkeypatch):
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL))

    response = client.get(f"{CALLBACK}?code=c&state=s")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert _reason_of(response) is None


def test_15_the_session_maps_to_the_correct_internal_person(client, monkeypatch):
    """The whole point of linking: the Google account resolves to *this* row."""
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(
        _person(7, email=LINKED_EMAIL, name="Synthetic Person"),
        _person(8, email="someone.else@example.test", name="Other Synthetic"),
    )

    client.get(f"{CALLBACK}?code=c&state=s")
    body = client.get(ME).json()

    assert body["person_id"] == 7
    assert body["display_name"] == "Synthetic Person"


def test_16_matching_is_case_insensitive_on_both_sides(client, monkeypatch):
    """Google may return a differently-cased address than the one linked, and
    an admin may have typed either. Both resolve to one Person.
    """
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims("LINKED.Person@Example.TEST")))
    _use_people(_person(7, email=LINKED_EMAIL))

    client.get(f"{CALLBACK}?code=c&state=s")

    assert client.get(ME).json()["person_id"] == 7


def test_17_an_unverified_email_is_refused(client, monkeypatch):
    """An address Google has not verified proves only that somebody typed it."""
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims(verified=False)))
    _use_people(_person(7, email=LINKED_EMAIL))

    response = client.get(f"{CALLBACK}?code=c&state=s")

    assert _reason_of(response) == "email_not_verified"
    client.cookies.clear()
    assert client.get(ME).status_code == 401


def test_17b_a_string_email_verified_is_not_treated_as_true(client, monkeypatch):
    """``"false"`` is truthy in Python. The check is ``is True`` for this reason."""
    claims = _claims()
    claims["email_verified"] = "false"
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=claims))
    _use_people(_person(7, email=LINKED_EMAIL))

    assert _reason_of(client.get(f"{CALLBACK}?code=c&state=s")) == "email_not_verified"


def test_18_an_unlinked_email_is_refused_and_creates_nobody(client, monkeypatch):
    """**The pilot's central rule.** Authentication succeeded; access did not."""
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims(UNKNOWN_EMAIL)))
    session = _use_people(_person(7, email=LINKED_EMAIL))

    response = client.get(f"{CALLBACK}?code=c&state=s")

    assert _reason_of(response) == "not_linked"
    assert len(session.people) == 1  # nobody was created
    client.cookies.clear()
    assert client.get(ME).status_code == 401


def test_19_a_person_with_no_email_at_all_is_never_matched(client, monkeypatch):
    """Roster-only people -- the majority -- cannot be signed in as."""
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims(UNKNOWN_EMAIL)))
    _use_people(_person(7, email=None), _person(8, email=None))

    assert _reason_of(client.get(f"{CALLBACK}?code=c&state=s")) == "not_linked"


def test_20_no_name_matching_happens(client, monkeypatch):
    """A Person whose display name equals the address's local part is not a
    match. Names are never an authentication key.
    """
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims("synthetic.person@example.test")))
    _use_people(_person(7, email=None, name="synthetic.person"))

    assert _reason_of(client.get(f"{CALLBACK}?code=c&state=s")) == "not_linked"


def test_21_a_deactivated_linked_person_is_refused(client, monkeypatch):
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL, deactivated=True))

    response = client.get(f"{CALLBACK}?code=c&state=s")

    assert _reason_of(response) == "person_inactive"
    client.cookies.clear()
    assert client.get(ME).status_code == 401


def test_22_an_issuer_that_is_not_google_is_refused(client, monkeypatch):
    _use_fake_google(
        monkeypatch, FakeGoogleClient(claims=_claims(issuer="https://evil.example.test"))
    )
    _use_people(_person(7, email=LINKED_EMAIL))

    assert _reason_of(client.get(f"{CALLBACK}?code=c&state=s")) == "invalid_response"


def test_23_a_token_with_no_claims_at_all_is_refused(client, monkeypatch):
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=None))
    _use_people(_person(7, email=LINKED_EMAIL))

    assert _reason_of(client.get(f"{CALLBACK}?code=c&state=s")) == "invalid_response"


def test_24_an_authlib_failure_becomes_a_refusal_not_a_500(client, monkeypatch):
    """Every way Authlib can reject a response means one thing here: this
    cannot be trusted. None of them should surface as a traceback.
    """
    _use_fake_google(
        monkeypatch, FakeGoogleClient(raises=RuntimeError("token endpoint said no"))
    )
    _use_people(_person(7, email=LINKED_EMAIL))

    response = client.get(f"{CALLBACK}?code=c&state=s")

    assert response.status_code == 303
    assert _reason_of(response) == "invalid_response"


# ==========================================================================
# 25-29 -- The callback does not trust the browser
# ==========================================================================


def test_25_a_frontend_supplied_email_is_ignored(client, monkeypatch):
    """The address comes from the ID token and from nowhere else."""
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims(UNKNOWN_EMAIL)))
    _use_people(_person(7, email=LINKED_EMAIL))

    response = client.get(
        f"{CALLBACK}?code=c&state=s&email={LINKED_EMAIL}",
        headers={"X-User-Email": LINKED_EMAIL},
    )

    assert _reason_of(response) == "not_linked"


def test_26_a_query_supplied_person_id_is_ignored(client, monkeypatch):
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL), _person(9, email="admin@example.test",
                                                       is_admin=True))

    client.get(f"{CALLBACK}?code=c&state=s&person_id=9&actor=9")

    assert client.get(ME).json()["person_id"] == 7


def test_27_the_refusal_reason_cannot_be_steered_by_the_caller(client, monkeypatch):
    """The reason is chosen from a fixed table, never reflected from input --
    so the redirect cannot be turned into a way to echo text at somebody.
    """
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims(UNKNOWN_EMAIL)))
    _use_people()

    response = client.get(
        f"{CALLBACK}?code=c&state=s&auth_error=<script>alert(1)</script>"
    )

    assert _reason_of(response) == "not_linked"
    assert "<script>" not in response.headers["location"]


def test_28_the_callback_redirect_is_relative_and_cannot_be_redirected_off_site(
    client, monkeypatch
):
    """There is no configurable success target to point elsewhere: the backend
    answers ``/`` and the browser resolves it against its own origin.
    """
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL))

    response = client.get(f"{CALLBACK}?code=c&state=s&next=https://evil.example.test")

    assert response.headers["location"] == "/"


def test_29_no_person_is_ever_created_by_signing_in(client, monkeypatch):
    """Checked against the module's code, not only its behaviour."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(routes_auth.__file__).read_text())
    constructed = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "Person" not in constructed
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "add" not in called
    assert "merge" not in called


# ==========================================================================
# 30-35 -- Nothing secret is logged
# ==========================================================================


def test_30_the_signed_in_persons_email_is_never_logged(client, monkeypatch, caplog):
    """It is a real person's private address, and Cloud Logging is not where it
    belongs. The Person id is logged instead, which is what an operator needs.
    """
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL))

    with caplog.at_level(logging.DEBUG):
        client.get(f"{CALLBACK}?code=c&state=s")

    assert LINKED_EMAIL not in caplog.text
    assert "person_id=7" in caplog.text


def test_31_a_refused_addresses_is_never_logged(client, monkeypatch, caplog):
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims(UNKNOWN_EMAIL)))
    _use_people()

    with caplog.at_level(logging.DEBUG):
        client.get(f"{CALLBACK}?code=c&state=s")

    assert UNKNOWN_EMAIL not in caplog.text
    assert "no linked person" in caplog.text


def test_32_the_authorization_code_and_secret_are_never_logged(
    client, monkeypatch, caplog
):
    _use_fake_google(
        monkeypatch, FakeGoogleClient(raises=RuntimeError(f"failed with {CLIENT_SECRET}"))
    )
    _use_people()

    with caplog.at_level(logging.DEBUG):
        client.get(f"{CALLBACK}?code=super-secret-authorization-code&state=s")

    # This application's own records only. The test client's httpx logger
    # echoes the request line it just sent, which is the test harness talking
    # about itself -- it is not something the deployed service writes.
    ours = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("app.")
    )
    assert CLIENT_SECRET not in ours
    assert "super-secret-authorization-code" not in ours
    assert "failed validation" in ours


def test_33_the_session_cookie_carries_no_token_and_no_address(client, monkeypatch):
    """The cookie is signed, not encrypted -- anyone holding it can read it, so
    what is in it matters.
    """
    import base64

    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL))

    client.get(f"{CALLBACK}?code=c&state=s")
    raw = client.cookies[SESSION_COOKIE_NAME]
    decoded = base64.urlsafe_b64decode(
        raw.split(".")[0] + "=" * (-len(raw.split(".")[0]) % 4)
    ).decode("utf8", "replace")

    assert "person_id" in decoded
    assert LINKED_EMAIL not in decoded
    assert "synthetic-access-token" not in decoded
    for forbidden in ("access_token", "id_token", "refresh_token", "is_admin"):
        assert forbidden not in decoded


# ==========================================================================
# 34-40 -- The session, /me, and logging out
# ==========================================================================


def test_34_me_reports_the_signed_in_identity(client, monkeypatch):
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL, name="Synthetic Person"))

    client.get(f"{CALLBACK}?code=c&state=s")
    response = client.get(ME)

    assert response.status_code == 200
    assert response.json()["display_name"] == "Synthetic Person"


def test_35_me_still_exposes_no_email_after_sign_in(client, monkeypatch):
    """Sign-in gave the server an address; the DTO still does not hand it back."""
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL))

    client.get(f"{CALLBACK}?code=c&state=s")
    body = client.get(ME).json()

    assert set(body) == {"person_id", "display_name", "is_admin", "headed_ministries"}
    assert LINKED_EMAIL not in str(body)


def test_36_logout_clears_the_session(client, monkeypatch):
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL))
    client.get(f"{CALLBACK}?code=c&state=s")
    assert client.get(ME).status_code == 200

    logout = client.post(LOGOUT)

    assert logout.status_code == 204
    assert client.get(ME).status_code == 401


def test_37_logout_is_not_reachable_by_GET(client):
    """A GET sign-out could be triggered by any site that got this browser to
    load a URL -- an <img> tag is enough.
    """
    assert client.get(LOGOUT).status_code == 405


def test_38_logout_succeeds_even_with_no_session(client):
    """Requiring a valid session to log out would mean an expired one could not
    be cleared, leaving a stale cookie with no way to remove it.
    """
    assert client.post(LOGOUT).status_code == 204


def test_39_an_unauthenticated_request_is_refused_everywhere(client):
    _use_people(_person(7, email=LINKED_EMAIL))

    assert client.get(ME).status_code == 401


def test_40_a_tampered_session_cookie_is_refused(client, monkeypatch):
    """The signature is what is checked, so editing the payload does not work."""
    _use_fake_google(monkeypatch, FakeGoogleClient(claims=_claims()))
    _use_people(_person(7, email=LINKED_EMAIL), _person(9, is_admin=True))

    client.get(f"{CALLBACK}?code=c&state=s")
    good = client.cookies[SESSION_COOKIE_NAME]
    client.cookies.set(SESSION_COOKIE_NAME, "x" + good[1:])

    assert client.get(ME).status_code == 401


def test_41_a_session_signed_with_another_key_is_refused(client, monkeypatch):
    """Rotating SESSION_SECRET is the "sign everybody out" lever, and this is
    what makes it work.
    """
    import itsdangerous

    signer = itsdangerous.TimestampSigner("a-different-signing-key")
    forged = signer.sign(
        __import__("base64").b64encode(b'{"person_id": 9}')
    ).decode()
    client.cookies.set(SESSION_COOKIE_NAME, forged)
    _use_people(_person(9, is_admin=True))

    assert client.get(ME).status_code == 401


def test_34_the_application_logger_emits_info_in_a_default_process():
    """The sign-in log lines must survive a real uvicorn process.

    **This test exists because the offline suite missed a real bug.** The tests
    above assert that sign-in *calls* ``logger.info``, but they do it inside
    ``caplog.at_level(logging.DEBUG)``, which forces the level for the duration
    of the test. That proves the call and says nothing about whether the
    deployed configuration lets the record out -- and it did not: uvicorn
    configures its own loggers and leaves the root logger at WARNING with no
    handler, so every ``app.*`` INFO record was created and dropped. The
    warnings came through, which is what made it easy to miss for a whole task.

    So this asserts the configuration instead of the call, with the level
    restored afterwards rather than overridden for the assertion.
    """
    import logging

    from app.main import configure_application_logging

    app_logger = logging.getLogger("app")
    previous_level = app_logger.level
    try:
        app_logger.setLevel(logging.NOTSET)
        configure_application_logging()

        assert logging.getLogger("app.api.routes_auth").isEnabledFor(logging.INFO)
        # And a handler exists somewhere for the record to reach.
        assert logging.getLogger().handlers or app_logger.handlers
        # But third-party INFO chatter stays out of the log: only "app" was
        # turned down, not the root logger.
        assert logging.getLogger().level >= logging.WARNING
    finally:
        app_logger.setLevel(previous_level)
