"""Ministry role management service tests (Task 53).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Tasks 14/19's precedent exactly, for the same
reasons: creating a role needs a ``session.flush()`` (to obtain a new row's
identity before the audit row referencing it can be built) and a database
lookup (``session.execute(select(...))``, to find an existing role by name).
Neither can be exercised against a real, unbound
:class:`~sqlalchemy.orm.Session` -- both would try to issue real SQL and raise
``UnboundExecutionError``.

- **The lookups' SQL** is tested directly and mechanically, with no session or
  database at all: ``_role_name_lookup_statement`` returns a plain SQLAlchemy
  ``Select`` compiled with literal binds and inspected.
- **The lookups' use** (found vs. not-found) is exercised via ``monkeypatch``
  in every orchestration test.
- **The flush** uses a real, unbound ``Session`` subclass whose ``flush()``
  simulates identity assignment without touching a database. ``commit()`` and
  ``rollback()`` remain forbidden.

Every ministry, role, and person name here is fictional.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.services import AuthorizationError, InvalidOperationError
from app.services.ministry_role import (
    ACTION_MINISTRY_ROLE_CHANGED,
    ACTION_MINISTRY_ROLE_CREATED,
    ACTION_MINISTRY_ROLE_DEACTIVATED,
    ACTION_MINISTRY_ROLE_REACTIVATED,
    _role_name_lookup_statement,
    create_ministry_role,
    deactivate_ministry_role,
    list_ministry_roles,
    reactivate_ministry_role,
    update_ministry_role,
)

# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class MinistryRoleSession(Session):
    """A real, unbound Session for these tests.

    ``flush()`` is not forbidden -- creating a role legitimately flushes once
    to obtain the identity its audit row needs. ``commit()`` and
    ``rollback()`` remain forbidden.
    """

    def __init__(self, *, next_id: int = 900) -> None:
        super().__init__()
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self._next_id = next_id
        self.executed_statements: list = []

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def flush(self, objects=None) -> None:
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1


@pytest.fixture
def session() -> MinistryRoleSession:
    return MinistryRoleSession()


def _person(person_id: int, name: str, *, is_admin: bool = False,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return person


def _ministry(ministry_id: int, name: str, *, deactivated: bool = False) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    if deactivated:
        ministry.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return ministry


def _membership(
    membership_id: int, *, person: Person, ministry: Ministry,
    is_head: bool = False, deactivated: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    membership.person = person
    membership.ministry = ministry
    if deactivated:
        membership.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    person.ministry_memberships.append(membership)
    return membership


def _role(
    role_id: int, name: str, *, ministry: Ministry, description: str | None = None,
    display_order: int = 0, deactivated: bool = False,
) -> MinistryRole:
    role = MinistryRole(
        ministry_id=ministry.id, name=name, description=description,
        display_order=display_order,
    )
    role.id = role_id
    role.ministry = ministry
    if deactivated:
        role.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return role


@pytest.fixture
def head(kids_ministry: Ministry) -> Person:
    """The actor every operational write in this module is performed by.

    **An active Ministry Head of the ministry being written to** -- which,
    since Task 80, is the only kind of person who may perform any of these
    operations. This fixture used to be an Admin who headed nothing; that
    person now gets 403 from every write here, and the tests that assert so
    say Admin in their own names.
    """
    person = _person(1, "Demo Admin")
    _membership(9001, person=person, ministry=kids_ministry, is_head=True)
    return person


@pytest.fixture
def kids_ministry() -> Ministry:
    return _ministry(3, "Kids")


@pytest.fixture
def other_ministry() -> Ministry:
    return _ministry(4, "Greeting")


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _stub_role_lookup(monkeypatch, existing: MinistryRole | None) -> None:
    import app.services.ministry_role as module

    monkeypatch.setattr(
        module, "_find_role_by_name",
        lambda session, *, ministry_id, name: existing,
    )


def _stub_next_display_order(monkeypatch, value: int) -> None:
    import app.services.ministry_role as module

    monkeypatch.setattr(module, "_next_display_order", lambda session, ministry_id: value)


def _stub_roles(session: Session, monkeypatch, roles: list[MinistryRole]) -> None:
    """Bypass the query in ``list_ministry_roles`` entirely: the query's shape
    is pinned separately below (``test_the_lookup_statement_...``), and here
    only orchestration (authorization, which roles come back) is under test.

    Patches ``execute`` on this one ``session`` instance, not the ``Session``
    class -- so it cannot leak into any other test's session.
    """

    class _Scalars:
        def all(self):
            return roles

    class _Result:
        def scalars(self):
            return _Scalars()

    monkeypatch.setattr(session, "execute", lambda stmt: _Result())


# ==========================================================================
# list_ministry_roles
# ==========================================================================


def test_admin_can_list_roles(session, head, kids_ministry, monkeypatch):
    roles = [_role(10, "Check-in", ministry=kids_ministry)]
    _stub_roles(session, monkeypatch, roles)

    result = list_ministry_roles(session, actor=head, ministry=kids_ministry)

    assert result == tuple(roles)


def test_own_ministry_head_can_list_roles(session, kids_ministry, monkeypatch):
    _stub_roles(session, monkeypatch, [])
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=kids_ministry, is_head=True)

    result = list_ministry_roles(session, actor=head, ministry=kids_ministry)

    assert result == ()


def test_other_ministry_head_cannot_list_roles(session, kids_ministry, other_ministry, monkeypatch):
    _stub_roles(session, monkeypatch, [])
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=other_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        list_ministry_roles(session, actor=head, ministry=kids_ministry)


def test_normal_member_cannot_list_roles(session, kids_ministry, monkeypatch):
    _stub_roles(session, monkeypatch, [])
    member = _person(9, "Member Person")
    _membership(200, person=member, ministry=kids_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        list_ministry_roles(session, actor=member, ministry=kids_ministry)


# ==========================================================================
# create_ministry_role
# ==========================================================================


def test_admin_can_create_a_role(session, head, kids_ministry, monkeypatch):
    _stub_role_lookup(monkeypatch, None)
    _stub_next_display_order(monkeypatch, 0)

    role = create_ministry_role(
        session, actor=head, ministry=kids_ministry, name="Check-in",
        description="Greets families and signs kids in.",
    )

    assert role.ministry_id == 3
    assert role.name == "Check-in"
    assert role.description == "Greets families and signs kids in."
    assert role.display_order == 0
    assert role.deactivated_at is None
    assert role.id == 900


def test_ministry_head_can_create_a_role_for_their_own_ministry(
    session, kids_ministry, monkeypatch
):
    _stub_role_lookup(monkeypatch, None)
    _stub_next_display_order(monkeypatch, 0)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=kids_ministry, is_head=True)

    role = create_ministry_role(session, actor=head, ministry=kids_ministry, name="Check-in")

    assert role.ministry_id == 3


def test_ministry_head_cannot_create_a_role_for_another_ministry(
    session, kids_ministry, other_ministry, monkeypatch
):
    _stub_role_lookup(monkeypatch, None)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=other_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        create_ministry_role(session, actor=head, ministry=kids_ministry, name="Check-in")

    assert _audit_rows(session) == []


def test_normal_member_cannot_create_a_role(session, kids_ministry, monkeypatch):
    _stub_role_lookup(monkeypatch, None)
    member = _person(9, "Member Person")
    _membership(200, person=member, ministry=kids_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        create_ministry_role(session, actor=member, ministry=kids_ministry, name="Check-in")


def test_deactivated_actor_cannot_create_a_role(session, kids_ministry, monkeypatch):
    _stub_role_lookup(monkeypatch, None)
    deactivated_admin = _person(1, "Demo Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        create_ministry_role(
            session, actor=deactivated_admin, ministry=kids_ministry, name="Check-in"
        )


def test_deactivated_ministry_is_rejected(session, monkeypatch):
    """Its own head is refused, which is the only way to prove the *ministry*
    is what was rejected: anybody else would be refused by authorization
    first, and the test would pass without the rule existing."""
    _stub_role_lookup(monkeypatch, None)
    inactive_ministry = _ministry(5, "Retired Ministry", deactivated=True)
    retired_head = _person(4, "Retired Head")
    _membership(9002, person=retired_head, ministry=inactive_ministry, is_head=True)

    with pytest.raises(InvalidOperationError):
        create_ministry_role(
            session, actor=retired_head, ministry=inactive_ministry, name="Check-in"
        )

    assert _audit_rows(session) == []


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_role_name_is_rejected(session, head, kids_ministry, monkeypatch, blank):
    _stub_role_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError):
        create_ministry_role(session, actor=head, ministry=kids_ministry, name=blank)

    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_duplicate_role_name_is_rejected_case_insensitively(
    session, head, kids_ministry, monkeypatch
):
    existing = _role(10, "Check-in", ministry=kids_ministry)
    _stub_role_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        create_ministry_role(session, actor=head, ministry=kids_ministry, name="CHECK-IN")

    assert _audit_rows(session) == []


def test_duplicate_role_name_is_rejected_even_against_a_deactivated_role(
    session, head, kids_ministry, monkeypatch
):
    """The uniqueness index has no partial predicate (module docstring):
    reusing a retired role's name means reactivating or renaming it.
    """
    existing = _role(10, "Check-in", ministry=kids_ministry, deactivated=True)
    _stub_role_lookup(monkeypatch, existing)

    with pytest.raises(InvalidOperationError):
        create_ministry_role(session, actor=head, ministry=kids_ministry, name="Check-in")


def test_create_role_uses_exactly_one_flush(session, head, kids_ministry, monkeypatch):
    _stub_role_lookup(monkeypatch, None)
    _stub_next_display_order(monkeypatch, 0)

    create_ministry_role(session, actor=head, ministry=kids_ministry, name="Check-in")

    assert session.flush_calls == 1


def test_create_role_never_commits_or_rolls_back(session, head, kids_ministry, monkeypatch):
    _stub_role_lookup(monkeypatch, None)
    _stub_next_display_order(monkeypatch, 0)

    create_ministry_role(session, actor=head, ministry=kids_ministry, name="Check-in")

    assert session.commit_calls == 0
    assert session.rollback_calls == 0


def test_create_role_records_a_correct_audit_row(session, head, kids_ministry, monkeypatch):
    _stub_role_lookup(monkeypatch, None)
    _stub_next_display_order(monkeypatch, 2)

    role = create_ministry_role(
        session, actor=head, ministry=kids_ministry, name="Check-in",
        description="Greets families.",
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_MINISTRY_ROLE_CREATED
    assert audit.actor_type == "PERSON"
    assert audit.actor_person_id == 1
    assert audit.actor_label == "Demo Admin"
    assert audit.target_table == "ministry_role"
    assert audit.target_id == role.id == 900
    assert audit.ministry_id == 3
    assert audit.summary == "Created role Check-in for Kids"
    assert audit.before_values is None
    assert audit.after_values == {
        "name": "Check-in", "description": "Greets families.", "display_order": 2,
    }
    assert audit.reason is None


def test_new_role_display_order_is_one_past_current_maximum():
    """Pinned against the real (non-stubbed) helper, not just its stub."""
    import app.services.ministry_role as module

    class FakeScalarResult:
        def __init__(self, value):
            self._value = value

        def scalar_one(self):
            return self._value

    class FakeSession:
        def __init__(self, value):
            self._value = value

        def execute(self, stmt):
            return FakeScalarResult(self._value)

    assert module._next_display_order(FakeSession(None), 3) == 0
    assert module._next_display_order(FakeSession(5), 3) == 6


# ==========================================================================
# update_ministry_role
# ==========================================================================


def test_admin_can_update_name_and_description(session, head, kids_ministry, monkeypatch):
    role = _role(10, "Check-in", ministry=kids_ministry, description="Old text.")
    _stub_role_lookup(monkeypatch, None)

    result = update_ministry_role(
        session, actor=head, role=role, name="Registration", description="New text.",
    )

    assert result is role
    assert role.name == "Registration"
    assert role.description == "New text."


def test_ministry_head_cannot_update_a_role_in_another_ministry(
    session, kids_ministry, other_ministry, monkeypatch
):
    role = _role(10, "Check-in", ministry=other_ministry)
    _stub_role_lookup(monkeypatch, None)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=kids_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        update_ministry_role(session, actor=head, role=role, name="Registration")

    assert role.name == "Check-in"


def test_normal_member_cannot_update_a_role(session, kids_ministry, monkeypatch):
    role = _role(10, "Check-in", ministry=kids_ministry)
    _stub_role_lookup(monkeypatch, None)
    member = _person(9, "Member Person")
    _membership(200, person=member, ministry=kids_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        update_ministry_role(session, actor=member, role=role, name="Registration")


def test_update_to_identical_values_is_a_no_op(session, head, kids_ministry, monkeypatch):
    role = _role(10, "Check-in", ministry=kids_ministry, description="Same text.")
    _stub_role_lookup(monkeypatch, role)  # looking itself up by its own current name

    result = update_ministry_role(
        session, actor=head, role=role, name="Check-in", description="Same text.",
    )

    assert result is role
    assert _audit_rows(session) == []


def test_renaming_to_a_different_case_of_its_own_name_is_allowed(
    session, head, kids_ministry, monkeypatch
):
    role = _role(10, "Check-in", ministry=kids_ministry)
    _stub_role_lookup(monkeypatch, role)  # the lookup finds this same row

    update_ministry_role(session, actor=head, role=role, name="CHECK-IN")

    assert role.name == "CHECK-IN"
    assert _one_audit_row(session).action == ACTION_MINISTRY_ROLE_CHANGED


def test_renaming_to_a_different_roles_name_is_rejected(
    session, head, kids_ministry, monkeypatch
):
    role = _role(10, "Check-in", ministry=kids_ministry)
    other_role = _role(11, "Registration", ministry=kids_ministry)
    _stub_role_lookup(monkeypatch, other_role)

    with pytest.raises(InvalidOperationError):
        update_ministry_role(session, actor=head, role=role, name="Registration")

    assert role.name == "Check-in"
    assert _audit_rows(session) == []


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_name_is_rejected_on_update(session, head, kids_ministry, monkeypatch, blank):
    role = _role(10, "Check-in", ministry=kids_ministry)

    with pytest.raises(InvalidOperationError):
        update_ministry_role(session, actor=head, role=role, name=blank)

    assert role.name == "Check-in"


def test_updating_a_deactivated_role_is_allowed(session, head, kids_ministry, monkeypatch):
    role = _role(10, "Check-in", ministry=kids_ministry, deactivated=True)
    _stub_role_lookup(monkeypatch, role)

    update_ministry_role(session, actor=head, role=role, name="Registration")

    assert role.name == "Registration"
    assert role.deactivated_at is not None  # untouched by this operation


def test_update_uses_no_flush(session, head, kids_ministry, monkeypatch):
    role = _role(10, "Check-in", ministry=kids_ministry)
    _stub_role_lookup(monkeypatch, role)

    update_ministry_role(session, actor=head, role=role, name="Registration")

    assert session.flush_calls == 0


def test_update_records_a_correct_audit_row(session, head, kids_ministry, monkeypatch):
    role = _role(10, "Check-in", ministry=kids_ministry, description="Old text.")
    _stub_role_lookup(monkeypatch, role)

    update_ministry_role(
        session, actor=head, role=role, name="Registration", description="New text.",
        reason="Renamed to match the sign-up sheet.",
    )
    audit = _one_audit_row(session)

    assert audit.action == ACTION_MINISTRY_ROLE_CHANGED
    assert audit.target_table == "ministry_role"
    assert audit.target_id == 10
    assert audit.ministry_id == 3
    assert audit.summary == "Changed role Check-in to Registration for Kids"
    assert audit.before_values == {"name": "Check-in", "description": "Old text."}
    assert audit.after_values == {"name": "Registration", "description": "New text."}
    assert audit.reason == "Renamed to match the sign-up sheet."


# ==========================================================================
# deactivate_ministry_role / reactivate_ministry_role
# ==========================================================================


def test_admin_can_deactivate_a_role(session, head, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry)

    result = deactivate_ministry_role(session, actor=head, role=role)

    assert result is role
    assert role.deactivated_at is not None


def test_ministry_head_can_deactivate_their_own_ministrys_role(session, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=kids_ministry, is_head=True)

    deactivate_ministry_role(session, actor=head, role=role)

    assert role.deactivated_at is not None


def test_ministry_head_cannot_deactivate_another_ministrys_role(
    session, kids_ministry, other_ministry
):
    role = _role(10, "Check-in", ministry=other_ministry)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=kids_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        deactivate_ministry_role(session, actor=head, role=role)

    assert role.deactivated_at is None


def test_normal_member_cannot_deactivate_a_role(session, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry)
    member = _person(9, "Member Person")
    _membership(200, person=member, ministry=kids_ministry, is_head=False)

    with pytest.raises(AuthorizationError):
        deactivate_ministry_role(session, actor=member, role=role)


def test_deactivating_an_already_deactivated_role_is_a_no_op(session, head, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry, deactivated=True)
    original_timestamp = role.deactivated_at

    result = deactivate_ministry_role(session, actor=head, role=role)

    assert result is role
    assert role.deactivated_at == original_timestamp
    assert _audit_rows(session) == []


def test_deactivate_records_a_correct_audit_row(session, head, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry)

    deactivate_ministry_role(session, actor=head, role=role, reason="No longer needed.")
    audit = _one_audit_row(session)

    assert audit.action == ACTION_MINISTRY_ROLE_DEACTIVATED
    assert audit.target_table == "ministry_role"
    assert audit.target_id == 10
    assert audit.ministry_id == 3
    assert audit.summary == "Deactivated role Check-in for Kids"
    assert audit.before_values == {"deactivated_at": None}
    assert audit.after_values["deactivated_at"] is not None
    assert audit.reason == "No longer needed."


def test_deactivate_uses_no_flush(session, head, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry)

    deactivate_ministry_role(session, actor=head, role=role)

    assert session.flush_calls == 0


def test_admin_can_reactivate_a_role(session, head, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry, deactivated=True)

    result = reactivate_ministry_role(session, actor=head, role=role)

    assert result is role
    assert role.deactivated_at is None


def test_ministry_head_cannot_reactivate_another_ministrys_role(
    session, kids_ministry, other_ministry
):
    role = _role(10, "Check-in", ministry=other_ministry, deactivated=True)
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry=kids_ministry, is_head=True)

    with pytest.raises(AuthorizationError):
        reactivate_ministry_role(session, actor=head, role=role)

    assert role.deactivated_at is not None


def test_reactivating_an_already_active_role_is_a_no_op(session, head, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry)

    result = reactivate_ministry_role(session, actor=head, role=role)

    assert result is role
    assert _audit_rows(session) == []


def test_reactivate_records_a_correct_audit_row(session, head, kids_ministry):
    role = _role(10, "Check-in", ministry=kids_ministry, deactivated=True)

    reactivate_ministry_role(session, actor=head, role=role)
    audit = _one_audit_row(session)

    assert audit.action == ACTION_MINISTRY_ROLE_REACTIVATED
    assert audit.target_table == "ministry_role"
    assert audit.target_id == 10
    assert audit.ministry_id == 3
    assert audit.summary == "Reactivated role Check-in for Kids"
    assert audit.before_values["deactivated_at"] is not None
    assert audit.after_values == {"deactivated_at": None}


def test_reactivate_can_never_collide_with_its_own_name(session, head, kids_ministry, monkeypatch):
    """No lookup is even performed: reactivating changes no name field, so
    there is nothing for a uniqueness check to catch (module docstring).
    """
    role = _role(10, "Check-in", ministry=kids_ministry, deactivated=True)

    # If reactivate_ministry_role ever queried by name, this would blow up --
    # proving instead that it does not need to.
    reactivate_ministry_role(session, actor=head, role=role)

    assert role.deactivated_at is None


# --------------------------------------------------------------------------
# Reason validation shared by all four mutating operations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_whitespace_only_reason_is_rejected_everywhere(session, head, kids_ministry, monkeypatch, blank):
    _stub_role_lookup(monkeypatch, None)
    role = _role(10, "Check-in", ministry=kids_ministry)

    with pytest.raises(InvalidOperationError):
        create_ministry_role(session, actor=head, ministry=kids_ministry, name="X", reason=blank)
    with pytest.raises(InvalidOperationError):
        update_ministry_role(session, actor=head, role=role, name="Y", reason=blank)
    with pytest.raises(InvalidOperationError):
        deactivate_ministry_role(session, actor=head, role=role, reason=blank)
    with pytest.raises(InvalidOperationError):
        reactivate_ministry_role(session, actor=head, role=role, reason=blank)


# --------------------------------------------------------------------------
# Historical references are never touched
# --------------------------------------------------------------------------


def test_deactivate_never_deletes_or_touches_anything_but_the_flag(session, head, kids_ministry):
    """Past StaffingRequirement, RoleQualification and Assignment rows name a
    role by id and must remain readable (module docstring): this proves
    deactivation never calls ``session.delete`` on anything.
    """
    role = _role(10, "Check-in", ministry=kids_ministry)
    deleted: list = []
    session.delete = lambda obj: deleted.append(obj)  # type: ignore[method-assign]

    deactivate_ministry_role(session, actor=head, role=role)

    assert deleted == []
    assert role.name == "Check-in"  # name, description, display_order untouched


# ==========================================================================
# The lookup statement itself
# ==========================================================================


def test_the_lookup_statement_filters_case_insensitively_on_ministry_and_name():
    stmt = _role_name_lookup_statement(3, "Check-In")
    compiled = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    assert "ministry_role.ministry_id = 3" in compiled
    assert "lower(ministry_role.name)" in compiled
    assert "'check-in'" in compiled


def test_the_lookup_statement_carries_no_deactivated_at_predicate():
    """The uniqueness index it mirrors has none either (module docstring).

    ``deactivated_at`` legitimately appears in the selected column list --
    this is a full-entity ``select(MinistryRole)`` -- so what matters is that
    the *WHERE clause* names nothing but ministry and name.
    """
    stmt = _role_name_lookup_statement(3, "Check-In")
    compiled = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    where_clause = compiled.split("WHERE", 1)[1]

    assert "deactivated_at" not in where_clause
