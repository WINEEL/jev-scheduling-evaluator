"""Google sign-in and the role boundaries, against real PostgreSQL (Task 76).

Rollback-isolated by the shared harness; nothing is committed. Every identity is
synthetic and every address is in the reserved ``.test`` domain.

**What needs a real database, and is therefore here rather than in the offline
suite.**

- The **unique index on ``lower(person.email)``**. The offline tests prove the
  CLI *checks* for a duplicate; only PostgreSQL proves the check cannot be
  raced, because the index is what actually refuses.
- **Authorization after real authentication.** The offline suite proves
  ``require_ministry_manager`` in isolation. What this file proves is the thing
  the pilot depends on: that signing in with Google establishes an actor whose
  authority is then read from real ``ministry_membership`` rows, and that a
  Ministry Head of one ministry is refused on another. Google answers "who";
  these rows answer "what may they do"; nothing in sign-in touches the second.

The OAuth client is replaced with a stub returning chosen claims. Everything
else -- the callback's rules, the session, ``get_current_actor``, the services'
authorization checks and the rows themselves -- is real.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api import dependencies as deps
from app.api import routes_auth
from app.config import get_settings
from app.main import app
from app.models.core import Person
from tests.integration import factories as f

pytestmark = pytest.mark.integration

CALLBACK = "/api/v1/auth/google/callback"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/me"


class StubGoogle:
    """Returns the claims a test chooses, as Authlib would after validation."""

    def __init__(self, email: str, *, verified: bool = True) -> None:
        self.claims = {
            "iss": "https://accounts.google.com",
            "sub": f"synthetic-subject-for-{email}",
            "email": email,
            "email_verified": verified,
        }

    async def authorize_access_token(self, request):
        return {"userinfo": self.claims}


@pytest.fixture
def api(db_session, monkeypatch) -> TestClient:
    """A client on the test's rolled-back Session, with sign-in configured.

    Only the Session and the OAuth client are replaced. The dev-actor header is
    explicitly disabled so nothing in this file can pass because of it.
    """
    monkeypatch.delenv("CHURCH_SCHEDULING_DEV_AUTH", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SESSION_SECRET", "synthetic-session-key-for-integration")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "synthetic.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "synthetic-secret")
    get_settings.cache_clear()
    app.dependency_overrides[deps.get_session] = lambda: db_session
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _sign_in(api: TestClient, monkeypatch, email: str, *, verified: bool = True):
    monkeypatch.setattr(
        routes_auth, "build_oauth", lambda settings: StubGoogle(email, verified=verified)
    )
    return api.get(f"{CALLBACK}?code=synthetic-code&state=synthetic-state")


def _address(label: str) -> str:
    return f"{label}@example.test"


# ==========================================================================
# 1-6 -- The email link, as the database enforces it
# ==========================================================================


def test_01_the_unique_index_refuses_a_duplicate_address(db_session):
    """The rule the CLI checks for is enforced underneath it, so a race between
    two admins linking at once cannot produce two owners of one address.
    """
    church = f.make_church(db_session)
    first = f.make_person(db_session, church=church)
    second = f.make_person(db_session, church=church)

    first.email = _address("shared")
    db_session.flush()
    second.email = _address("shared")

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_02_the_index_is_case_insensitive(db_session):
    """``lower(email)``, so two addresses differing only in case are one."""
    church = f.make_church(db_session)
    first = f.make_person(db_session, church=church)
    second = f.make_person(db_session, church=church)

    first.email = _address("shared")
    db_session.flush()
    second.email = "SHARED@EXAMPLE.TEST"

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_03_many_people_may_have_no_address(db_session):
    """The partial index is what makes this work, and it is the normal case:
    most people came from a roster import and have no email at all.
    """
    church = f.make_church(db_session)
    for _ in range(5):
        person = f.make_person(db_session, church=church)
        assert person.email is None
    db_session.flush()  # no unique violation


def test_04_the_cli_links_and_unlinks_real_rows(db_session):
    """The CLI's command functions, against real rows and the real index."""
    import argparse

    from scripts import link_person_email as cli

    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church)

    def args(**fields):
        base = {"person_id": None, "name": None, "email": None, "replace": False}
        return argparse.Namespace(**{**base, **fields})

    assert cli._link(db_session, args(person_id=person.id, email=_address("linked"))) == cli.EXIT_OK
    assert person.email == _address("linked")

    assert cli._unlink(db_session, args(person_id=person.id)) == cli.EXIT_OK
    assert person.email is None


def test_05_the_cli_refuses_a_duplicate_before_the_database_has_to(db_session):
    import argparse

    from scripts import link_person_email as cli

    church = f.make_church(db_session)
    holder = f.make_person(db_session, church=church)
    other = f.make_person(db_session, church=church)
    holder.email = _address("taken")
    db_session.flush()

    code = cli._link(
        db_session,
        argparse.Namespace(person_id=other.id, name=None, email=_address("taken"), replace=False),
    )

    assert code == cli.EXIT_REFUSED
    assert other.email is None


def test_06_the_lookup_finds_a_row_stored_with_unexpected_case(db_session):
    """A row written by hand in psql with a capitalized address is still found,
    because the query lowercases both sides rather than trusting the stored
    value to have been normalized.
    """
    from app.auth.email_link import find_person_by_email

    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church)
    person.email = "Mixed.Case@Example.Test"
    db_session.flush()

    assert find_person_by_email(db_session, "mixed.case@example.test") is person


# ==========================================================================
# 7-12 -- Signing in resolves to the right internal actor
# ==========================================================================


def test_07_a_linked_person_signs_in_and_maps_to_their_own_row(api, db_session, monkeypatch):
    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church, name="Signed In")
    person.email = _address("signed.in")
    db_session.flush()

    response = _sign_in(api, monkeypatch, _address("signed.in"))

    assert response.status_code == 303
    body = api.get(ME).json()
    assert body["person_id"] == person.id
    assert body["display_name"] == person.display_name


def test_08_an_unlinked_address_is_refused_and_creates_nobody(api, db_session, monkeypatch):
    church = f.make_church(db_session)
    f.make_person(db_session, church=church)
    before = db_session.query(Person).count()

    response = _sign_in(api, monkeypatch, _address("stranger"))

    assert "auth_error=not_linked" in response.headers["location"]
    assert db_session.query(Person).count() == before
    api.cookies.clear()
    assert api.get(ME).status_code == 401


def test_09_an_unverified_address_is_refused_even_when_linked(api, db_session, monkeypatch):
    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church)
    person.email = _address("unverified")
    db_session.flush()

    response = _sign_in(api, monkeypatch, _address("unverified"), verified=False)

    assert "auth_error=email_not_verified" in response.headers["location"]


def test_10_a_deactivated_linked_person_is_refused(api, db_session, monkeypatch):
    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church, deactivated=True)
    person.email = _address("deactivated")
    db_session.flush()

    response = _sign_in(api, monkeypatch, _address("deactivated"))

    assert "auth_error=person_inactive" in response.headers["location"]


def test_11_logging_out_ends_access(api, db_session, monkeypatch):
    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church)
    person.email = _address("logout")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("logout"))
    assert api.get(ME).status_code == 200

    assert api.post(LOGOUT).status_code == 204
    assert api.get(ME).status_code == 401


def test_12_deactivating_a_person_ends_their_live_session_immediately(
    api, db_session, monkeypatch
):
    """There is no server-side session store to revoke, so this is the lever
    that does exist -- and it works on the *next request*, not at expiry,
    because authority is re-read from the row every time.
    """
    import datetime

    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church)
    person.email = _address("revoked")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("revoked"))
    assert api.get(ME).status_code == 200

    person.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    db_session.flush()

    assert api.get(ME).status_code == 401


# ==========================================================================
# 13-22 -- Authorization after real authentication
# ==========================================================================


def test_13_a_normal_user_signs_in_and_gets_no_authority(api, db_session, monkeypatch):
    """**Google authentication alone grants nothing.** A member of a ministry
    who is not its head is authenticated, and can administer nothing.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    person = f.make_person(db_session, church=church, name="Normal User")
    f.make_membership(db_session, person=person, ministry=ministry, is_head=False)
    person.email = _address("normal")
    db_session.flush()

    _sign_in(api, monkeypatch, _address("normal"))
    body = api.get(ME).json()

    assert body["person_id"] == person.id
    assert body["is_admin"] is False
    assert body["headed_ministries"] == []


def test_14_a_normal_user_cannot_change_their_own_ministrys_configuration(
    api, db_session, monkeypatch
):
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    person = f.make_person(db_session, church=church)
    f.make_membership(db_session, person=person, ministry=ministry, is_head=False)
    person.email = _address("normal.write")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("normal.write"))

    response = api.post(
        f"/api/v1/ministries/{ministry.id}/roles",
        json={"name": "Invented Role", "description": None},
    )

    assert response.status_code == 403


def test_15_a_ministry_head_is_reported_and_may_manage_their_own_ministry(
    api, db_session, monkeypatch
):
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    head = f.make_person(db_session, church=church, name="Head")
    f.make_membership(db_session, person=head, ministry=ministry, is_head=True)
    head.email = _address("head")
    db_session.flush()

    _sign_in(api, monkeypatch, _address("head"))
    body = api.get(ME).json()

    assert body["is_admin"] is False
    assert [m["ministry_id"] for m in body["headed_ministries"]] == [ministry.id]

    created = api.post(
        f"/api/v1/ministries/{ministry.id}/roles",
        json={"name": "Own Ministry Role", "description": None},
    )
    assert created.status_code == 201


def test_16_a_ministry_head_cannot_manage_an_unrelated_ministry(
    api, db_session, monkeypatch
):
    """**The boundary that matters most for a multi-ministry pilot.**"""
    church = f.make_church(db_session)
    theirs = f.make_ministry(db_session, church=church, name="Theirs")
    unrelated = f.make_ministry(db_session, church=church, name="Unrelated")
    head = f.make_person(db_session, church=church)
    f.make_membership(db_session, person=head, ministry=theirs, is_head=True)
    head.email = _address("head.scoped")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("head.scoped"))

    response = api.post(
        f"/api/v1/ministries/{unrelated.id}/roles",
        json={"name": "Not Theirs", "description": None},
    )

    assert response.status_code == 403


def test_17_a_ministry_head_cannot_read_an_unrelated_ministrys_configuration(
    api, db_session, monkeypatch
):
    church = f.make_church(db_session)
    theirs = f.make_ministry(db_session, church=church, name="Theirs")
    unrelated = f.make_ministry(db_session, church=church, name="Unrelated")
    head = f.make_person(db_session, church=church)
    f.make_membership(db_session, person=head, ministry=theirs, is_head=True)
    head.email = _address("head.read")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("head.read"))

    assert api.get(f"/api/v1/ministries/{theirs.id}/roles").status_code == 200
    assert api.get(f"/api/v1/ministries/{unrelated.id}/roles").status_code == 403


def test_18_an_inactive_head_membership_confers_nothing(api, db_session, monkeypatch):
    """The database's check constraint means an inactive membership cannot
    carry the flag at all, so a head who has left the ministry loses authority
    with it rather than keeping a stale permission.
    """
    import datetime

    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    head = f.make_person(db_session, church=church)
    membership = f.make_membership(db_session, person=head, ministry=ministry, is_head=True)
    head.email = _address("former.head")
    db_session.flush()

    membership.is_ministry_head = False
    membership.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    db_session.flush()

    _sign_in(api, monkeypatch, _address("former.head"))
    body = api.get(ME).json()

    assert body["headed_ministries"] == []
    assert api.get(f"/api/v1/ministries/{ministry.id}/roles").status_code == 403


def test_19_an_admin_may_manage_a_ministry_they_do_not_head(api, db_session, monkeypatch):
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    admin.email = _address("admin")
    db_session.flush()

    _sign_in(api, monkeypatch, _address("admin"))
    body = api.get(ME).json()

    assert body["is_admin"] is True
    assert body["headed_ministries"] == []  # authority, not membership
    assert api.get(f"/api/v1/ministries/{ministry.id}/roles").status_code == 200


def test_20_signing_in_never_changes_anybodys_authority(api, db_session, monkeypatch):
    """Authentication answers "who", and writes nothing about "what may you do"."""
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    person = f.make_person(db_session, church=church)
    membership = f.make_membership(db_session, person=person, ministry=ministry)
    person.email = _address("unchanged")
    db_session.flush()

    _sign_in(api, monkeypatch, _address("unchanged"))
    db_session.refresh(person)
    db_session.refresh(membership)

    assert person.is_admin is False
    assert membership.is_ministry_head is False


# ==========================================================================
# 21-25 -- Impersonation is refused
# ==========================================================================


def test_21_a_dev_actor_header_cannot_switch_actor_mid_session(
    api, db_session, monkeypatch
):
    """Even with the header enabled, the session wins."""
    monkeypatch.setenv("CHURCH_SCHEDULING_DEV_AUTH", "1")
    get_settings.cache_clear()

    church = f.make_church(db_session)
    normal = f.make_person(db_session, church=church, name="Normal")
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    normal.email = _address("no.escalation")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("no.escalation"))

    body = api.get(ME, headers={"X-Dev-Actor-Person-Id": str(admin.id)}).json()

    assert body["person_id"] == normal.id
    assert body["is_admin"] is False


def test_22_no_request_field_can_name_a_different_actor(api, db_session, monkeypatch):
    """Query parameters, body fields and invented headers are all ignored: the
    actor comes from the session and from nowhere else.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    normal = f.make_person(db_session, church=church)
    admin = f.make_person(db_session, church=church, is_admin=True)
    f.make_membership(db_session, person=normal, ministry=ministry, is_head=False)
    normal.email = _address("no.spoof")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("no.spoof"))

    response = api.post(
        f"/api/v1/ministries/{ministry.id}/roles"
        f"?actor_person_id={admin.id}&person_id={admin.id}&is_admin=true",
        json={
            "name": "Spoofed Role",
            "description": None,
            "actor_person_id": admin.id,
            "is_admin": True,
        },
        headers={
            "X-Actor-Person-Id": str(admin.id),
            "X-User-Id": str(admin.id),
            "X-Forwarded-User": _address("admin"),
        },
    )

    assert response.status_code in (403, 422)
    assert api.get(ME).json()["person_id"] == normal.id


def test_23_another_persons_session_cannot_be_forged(api, db_session, monkeypatch):
    """The cookie is signed with SESSION_SECRET; editing it invalidates it."""
    from app.auth.session import SESSION_COOKIE_NAME

    church = f.make_church(db_session)
    normal = f.make_person(db_session, church=church)
    f.make_person(db_session, church=church, is_admin=True)
    normal.email = _address("victim")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("victim"))

    cookie = api.cookies[SESSION_COOKIE_NAME]
    api.cookies.set(SESSION_COOKIE_NAME, cookie[:-4] + "AAAA")

    assert api.get(ME).status_code == 401


def test_24_unlinking_denies_the_next_sign_in(api, db_session, monkeypatch):
    """How a pilot user is deauthorized."""
    import argparse

    from scripts import link_person_email as cli

    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church)
    person.email = _address("to.revoke")
    db_session.flush()
    _sign_in(api, monkeypatch, _address("to.revoke"))
    assert api.get(ME).status_code == 200
    api.post(LOGOUT)

    cli._unlink(db_session, argparse.Namespace(person_id=person.id, name=None))
    db_session.flush()

    response = _sign_in(api, monkeypatch, _address("to.revoke"))
    assert "auth_error=not_linked" in response.headers["location"]


def test_25_an_unauthenticated_request_reaches_no_church_data(api, db_session):
    """Nothing about the church is readable without a session -- checked across
    the real endpoints rather than assumed from one.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    period = f.make_period(db_session, ministry=ministry)

    for path in (
        ME,
        f"/api/v1/ministries/{ministry.id}/scheduling-periods",
        f"/api/v1/ministries/{ministry.id}/roles",
        f"/api/v1/scheduling-periods/{period.id}/events",
        f"/api/v1/scheduling-periods/{period.id}/serving-limits",
        f"/api/v1/scheduling-periods/{period.id}/scheduling-rules",
    ):
        response = api.get(path)
        assert response.status_code == 401, path
        assert ministry.name not in response.text
        assert response.json() == {"detail": "Not authenticated."}
