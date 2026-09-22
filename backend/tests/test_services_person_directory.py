"""People directory and Person lifecycle service tests (Task 79).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Task 53's precedent exactly, and for the same
reasons. Creating a Person flushes once (to obtain the identity its audit row
needs) and issues lookups (the duplicate-name warning, the email collision
check), neither of which a real *unbound* Session can execute.

- **Authorization** is tested against a real, unbound Session and never gets
  far enough to issue SQL: every refusal happens before the first query, which
  is itself part of the contract.
- **The lookups' SQL** is tested mechanically -- a plain ``Select`` compiled
  with literal binds and inspected, no session and no database.
- **The lookups' results** are supplied by a scripted Session subclass, so
  "this name is taken" and "this address belongs to somebody else" can each be
  exercised without a row existing anywhere.
- **``commit()`` and ``rollback()`` remain forbidden**, so a service that grew
  an internal transaction boundary fails loudly here.

The matrix that genuinely needs a database -- real constraints, real audit
rows, real query counts -- is ``tests/integration/test_pg_people_management.py``.

Every person, ministry and address here is fictional and uses ``example.test``.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AUDIT_TARGET_TABLES, AuditEvent
from app.models.core import (
    CHURCH_MEMBERSHIP_STATUS_MEMBER,
    CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
    Ministry,
    MinistryMembership,
    Person,
)
from app.services.audit import (
    ACTION_PERSON_AUTH_LINK_CHANGED,
    ACTION_PERSON_CHURCH_MEMBERSHIP_STATUS_CHANGED,
    ACTION_PERSON_CREATED,
    ACTION_PERSON_DEACTIVATED,
    ACTION_PERSON_REACTIVATED,
    ACTION_PERSON_UPDATED,
)
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.person_directory import (
    DEFAULT_DIRECTORY_LIMIT,
    MAX_DIRECTORY_LIMIT,
    DirectoryEntry,
    DirectoryMembership,
    _clamp_limit,
    _directory_count_statement,
    _directory_page_statement,
    _escape_like,
    create_person,
    deactivate_person,
    list_people,
    reactivate_person,
    read_person,
    set_church_membership_status,
    set_person_auth_link,
    update_person,
)

UTC = datetime.timezone.utc
_THEN = datetime.datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class DirectorySession(Session):
    """A real, unbound Session with scripted query results.

    ``flush()`` assigns identities the way the database would, because
    :func:`create_person` legitimately needs one before it can build the audit
    row that references it. ``commit()`` and ``rollback()`` stay forbidden.

    ``execute()`` dispatches on the compiled SQL rather than on call order, so
    a test that adds a query in a different place does not silently receive
    somebody else's canned answer.
    """

    def __init__(
        self,
        *,
        next_id: int = 500,
        name_matches: tuple[Person, ...] = (),
        email_holder: Person | None = None,
        membership_rows: tuple = (),
        page_rows: tuple = (),
        total: int = 0,
        serving_rows: tuple = (),
        conflict_rows: tuple = (),
        timezone: str = "UTC",
    ) -> None:
        super().__init__()
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self._next_id = next_id
        self.name_matches = name_matches
        self.email_holder = email_holder
        self.membership_rows = membership_rows
        self.page_rows = page_rows
        self.total = total
        #: Rows the serving-history aggregates answer with. Scripted rather
        #: than computed: this suite is offline, and what those queries *mean*
        #: is tested against a real database in the integration suite.
        self.serving_rows = serving_rows
        self.conflict_rows = conflict_rows
        self.timezone = timezone
        self.executed_sql: list[str] = []

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

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        self.executed_sql.append(sql)
        # The serving-history aggregates are matched *first*, because they
        # contain ``count(*)`` and would otherwise be answered by the directory
        # total below -- a canned answer to the wrong question.
        if "array_agg" in sql:
            return _Result(rows=self.conflict_rows)
        if "assignment" in sql:
            return _Result(rows=self.serving_rows)
        if "FROM church" in sql:
            return _Result(scalar_one_or_none=self.timezone)
        if "count(" in sql:
            return _Result(scalar_one=self.total)
        if "FROM ministry_membership" in sql:
            return _Result(rows=self.membership_rows)
        if "lower(person.display_name) = lower(" in sql:
            return _Result(rows=self.name_matches)
        if "lower(person.email) = lower(" in sql:
            return _Result(scalar_one_or_none=self.email_holder)
        if "FROM person" in sql:
            return _Result(rows=self.page_rows)
        raise AssertionError(f"unscripted query: {sql}")


class _Result:
    """The two or three Result methods these services actually call."""

    def __init__(self, *, rows=(), scalar_one=None, scalar_one_or_none=None) -> None:
        self._rows = tuple(rows)
        self._scalar_one = scalar_one
        self._scalar_one_or_none = scalar_one_or_none

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def scalar_one(self):
        return self._scalar_one

    def scalar_one_or_none(self):
        return self._scalar_one_or_none


@pytest.fixture
def session() -> DirectorySession:
    return DirectorySession()


def _person(
    person_id: int,
    name: str,
    *,
    is_admin: bool = False,
    deactivated: bool = False,
    email: str | None = None,
    heads: tuple[int, ...] = (),
    member_of: tuple[int, ...] = (),
    church_membership_status: str = CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
) -> Person:
    """A Person with its memberships already populated.

    ``ministry_memberships`` is set explicitly so the authorization checks read
    a list rather than trying to lazy-load one off an unbound Session.
    """
    person = Person(
        display_name=name,
        is_admin=is_admin,
        church_id=1,
        email=email,
        church_membership_status=church_membership_status,
    )
    person.id = person_id
    person.deactivated_at = _THEN if deactivated else None
    memberships = []
    for index, ministry_id in enumerate(dict.fromkeys((*heads, *member_of))):
        membership = MinistryMembership(
            person_id=person_id,
            ministry_id=ministry_id,
            is_ministry_head=ministry_id in heads,
        )
        membership.id = 9000 + index
        membership.deactivated_at = None
        memberships.append(membership)
    person.ministry_memberships = memberships
    return person


def _ministry(ministry_id: int, name: str, *, deactivated: bool = False) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    ministry.deactivated_at = _THEN if deactivated else None
    return ministry


def _admin(person_id: int = 1) -> Person:
    return _person(person_id, "Admin Person", is_admin=True)


def _head(person_id: int = 2, ministry_id: int = 100) -> Person:
    return _person(person_id, "Head Person", heads=(ministry_id,))


def _volunteer(person_id: int = 3) -> Person:
    return _person(person_id, "Volunteer Person", member_of=(100,))


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


# --------------------------------------------------------------------------
# Who may read the directory
# --------------------------------------------------------------------------


class TestDirectoryAuthorization:
    """Task 79 §1: Admin and Ministry Head may read; a volunteer may not."""

    def test_an_admin_may_list_people(self, session):
        page = list_people(session, actor=_admin())
        assert page.total == 0

    def test_a_ministry_head_may_list_people(self, session):
        page = list_people(session, actor=_head())
        assert page.people == ()

    def test_a_head_of_any_ministry_may_list_people(self, session):
        """Not only heads of some particular ministry -- the directory is
        church-wide, and a head needs it to find people outside their team."""
        list_people(session, actor=_person(7, "Other Head", heads=(999,)))

    def test_a_volunteer_may_not_list_people(self, session):
        with pytest.raises(AuthorizationError):
            list_people(session, actor=_volunteer())

    def test_a_volunteer_may_not_read_one_person(self, session):
        with pytest.raises(AuthorizationError):
            read_person(session, actor=_volunteer(), person=_person(4, "Someone"))

    def test_a_deactivated_admin_loses_directory_access(self, session):
        """Authority is inert the moment somebody is deactivated -- every
        check tests ``deactivated_at`` before anything else."""
        with pytest.raises(AuthorizationError):
            list_people(
                session, actor=_person(5, "Former Admin", is_admin=True, deactivated=True)
            )

    def test_a_deactivated_head_loses_directory_access(self, session):
        stale = _person(6, "Former Head", heads=(100,), deactivated=True)
        with pytest.raises(AuthorizationError):
            list_people(session, actor=stale)

    def test_a_head_whose_membership_was_deactivated_loses_access(self, session):
        """The head flag alone is not authority; the membership carrying it
        must be active. The database's own check constraint agrees."""
        stale = _person(8, "Stood Down", heads=(100,))
        stale.ministry_memberships[0].deactivated_at = _THEN
        with pytest.raises(AuthorizationError):
            list_people(session, actor=stale)

    def test_the_refusal_says_nothing_about_who_exists(self, session):
        with pytest.raises(AuthorizationError) as caught:
            list_people(session, actor=_volunteer())
        message = str(caught.value)
        assert "Volunteer Person" not in message
        assert "Admin" in message or "Ministry Head" in message


# --------------------------------------------------------------------------
# The directory query itself
# --------------------------------------------------------------------------


class TestDirectoryQuery:
    """Bounded, ordered, and never a wildcard injection."""

    def test_the_page_is_ordered_by_name_then_id(self):
        sql = str(
            _directory_page_statement(None, church_id=1, include_inactive=False, limit=10, offset=0)
            .compile(dialect=postgresql.dialect())
        )
        assert "ORDER BY lower(person.display_name), person.id" in sql

    def test_the_page_is_limited_and_offset(self):
        sql = str(
            _directory_page_statement(None, church_id=1, include_inactive=False, limit=10, offset=5)
            .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "LIMIT 10" in sql
        assert "OFFSET 5" in sql

    def test_inactive_people_are_excluded_by_default(self):
        sql = str(
            _directory_page_statement(None, church_id=1, include_inactive=False, limit=10, offset=0)
            .compile(dialect=postgresql.dialect())
        )
        assert "person.deactivated_at IS NULL" in sql

    def test_include_inactive_drops_the_filter(self):
        sql = str(
            _directory_page_statement(None, church_id=1, include_inactive=True, limit=10, offset=0)
            .compile(dialect=postgresql.dialect())
        )
        assert "deactivated_at IS NULL" not in sql

    def test_search_compares_lowercased_names(self):
        sql = str(
            _directory_page_statement("ada", church_id=1, include_inactive=False, limit=10, offset=0)
            .compile(dialect=postgresql.dialect())
        )
        assert "lower(person.display_name) LIKE" in sql

    def test_search_wildcards_are_escaped(self):
        """A name containing % or _ is searched for literally, so typing one
        cannot turn a narrow lookup into "everybody"."""
        assert _escape_like("100%") == "100\\%"
        assert _escape_like("a_b") == "a\\_b"
        assert _escape_like("back\\slash") == "back\\\\slash"

    def test_the_page_size_is_clamped_rather_than_trusted(self):
        assert _clamp_limit(10_000) == MAX_DIRECTORY_LIMIT
        assert _clamp_limit(0) == 1
        assert _clamp_limit(-5) == 1
        assert _clamp_limit(25) == 25

    def test_a_negative_offset_is_refused(self, session):
        with pytest.raises(InvalidOperationError):
            list_people(session, actor=_admin(), offset=-1)

    def test_the_default_page_size_is_within_the_ceiling(self):
        assert 1 <= DEFAULT_DIRECTORY_LIMIT <= MAX_DIRECTORY_LIMIT

    def test_a_blank_search_is_no_search_at_all(self, session):
        """Whitespace in the box means the person cleared it, not that they
        are looking for somebody named "   "."""
        list_people(session, actor=_admin(), search="   ")
        assert not any("LIKE" in sql for sql in session.executed_sql)


class TestChurchScoping:
    """The directory is one church's roll, and only one church's.

    V1 is a single-church deployment (core §7), so none of this narrows
    anything today. That is why it is worth pinning: a filter that is absent
    because nobody needed it yet is indistinguishable from one somebody
    removed.
    """

    def test_the_listing_filters_on_the_actors_church(self):
        sql = str(
            _directory_page_statement(
                None, church_id=42, include_inactive=False, limit=10, offset=0
            ).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "person.church_id = 42" in sql

    def test_the_count_filters_on_the_same_church(self):
        """Or the screen would read "showing 12 of 200"."""
        sql = str(
            _directory_count_statement(
                None, church_id=42, include_inactive=False
            ).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert "person.church_id = 42" in sql

    def test_the_church_is_never_a_caller_parameter(self):
        """It comes from the actor. A caller-supplied church id would be the
        one way to read another church's roll."""
        import inspect

        assert "church_id" not in inspect.signature(list_people).parameters

    def test_reading_somebody_in_another_church_is_refused(self, session):
        """Otherwise the detail read would be a way round the listing."""
        stranger = _person(70, "Other Church Person")
        stranger.church_id = 99
        with pytest.raises(AuthorizationError):
            read_person(session, actor=_admin(), person=stranger)


class TestDirectoryEntryDerivations:
    """The one derived property, tested without a session at all."""

    def test_a_person_with_no_deactivation_is_active(self):
        entry = DirectoryEntry(
            person_id=1, display_name="A", email=None, phone=None, is_admin=False,
            church_membership_status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
            deactivated_at=None,
            memberships=(
                DirectoryMembership(
                    ministry_membership_id=1, ministry_id=10, ministry_name="AV",
                    is_ministry_head=True, deactivated_at=None,
                ),
            ),
        )
        assert entry.is_active is True

    def test_a_deactivated_person_is_not_active(self):
        entry = DirectoryEntry(
            person_id=1, display_name="A", email=None, phone=None, is_admin=True,
            church_membership_status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
            deactivated_at=_THEN,
        )
        assert entry.is_active is False

    def test_a_membership_carries_its_own_activity_not_the_persons(self):
        """The two deactivations are separate facts (core section 6), and the
        value objects keep them apart rather than deriving one from the other.
        """
        entry = DirectoryEntry(
            person_id=1, display_name="A", email=None, phone=None, is_admin=False,
            church_membership_status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
            deactivated_at=None,
            memberships=(
                DirectoryMembership(
                    ministry_membership_id=1, ministry_id=10, ministry_name="AV",
                    is_ministry_head=False, deactivated_at=_THEN,
                ),
            ),
        )
        assert entry.is_active is True
        assert entry.memberships[0].deactivated_at == _THEN


# --------------------------------------------------------------------------
# Creating a Person
# --------------------------------------------------------------------------


class TestCreatePerson:
    """Task 79 §4: identity is never inferred, and creation stays explicit."""

    def test_an_admin_may_create_an_unattached_person(self, session):
        person = create_person(session, actor=_admin(), display_name="New Person")
        assert person.id is not None
        assert person.display_name == "New Person"
        assert person.is_admin is False
        assert person.deactivated_at is None

    def test_a_head_must_name_a_ministry_they_lead(self, session):
        """An unattached Person is a church-wide act, so a Head is refused --
        an Admin administers the church roll, and a Head creates people *for*
        the team they run."""
        with pytest.raises(AuthorizationError) as caught:
            create_person(session, actor=_head(), display_name="New Person")
        assert "Admin" in str(caught.value)

    def test_an_admin_who_heads_nothing_may_not_create_into_a_ministry(self, session):
        """Task 79 §1: Admin status alone is oversight, not operation. Putting
        somebody onto a team is that team's Head's decision, so an Admin who
        does not lead it is refused -- they may create the Person unattached
        and let the Head add them."""
        with pytest.raises(AuthorizationError) as caught:
            create_person(
                session,
                actor=_admin(),
                display_name="New Person",
                initial_ministry=_ministry(100, "AV"),
            )
        assert "Ministry Head" in str(caught.value)

    def test_an_admin_who_heads_the_ministry_may_create_into_it(self, session):
        """And passes through the Head membership, not through ``is_admin``."""
        actor = _person(11, "Admin And Head", is_admin=True, heads=(100,))
        person = create_person(
            session,
            actor=actor,
            display_name="New Person",
            initial_ministry=_ministry(100, "AV"),
        )
        assert person.id is not None

    def test_a_head_may_create_into_their_own_ministry(self, session):
        person = create_person(
            session,
            actor=_head(ministry_id=100),
            display_name="New Person",
            initial_ministry=_ministry(100, "AV"),
        )
        assert person.id is not None

    def test_a_head_may_not_create_into_another_ministry(self, session):
        with pytest.raises(AuthorizationError):
            create_person(
                session,
                actor=_head(ministry_id=100),
                display_name="New Person",
                initial_ministry=_ministry(200, "Kids"),
            )

    def test_a_volunteer_may_not_create_anybody(self, session):
        with pytest.raises(AuthorizationError):
            create_person(
                session,
                actor=_volunteer(),
                display_name="New Person",
                initial_ministry=_ministry(100, "AV"),
            )

    def test_a_deactivated_admin_may_not_create_anybody(self, session):
        with pytest.raises(AuthorizationError):
            create_person(
                session,
                actor=_person(9, "Former", is_admin=True, deactivated=True),
                display_name="New Person",
            )

    def test_an_existing_exact_name_is_refused_once_not_merged(self):
        """The heart of §4: a warning, and no automatic identity inference."""
        existing = _person(50, "Sam Taylor")
        session = DirectorySession(name_matches=(existing,))

        with pytest.raises(InvalidOperationError) as caught:
            create_person(session, actor=_admin(), display_name="Sam Taylor")

        message = str(caught.value)
        assert "already exists" in message
        # Refused, not merged: nothing was created and nothing was returned
        # that could be mistaken for the existing person.
        assert not [obj for obj in session.new if isinstance(obj, Person)]

    def test_the_duplicate_check_is_case_insensitive(self):
        session = DirectorySession(name_matches=(_person(50, "Sam Taylor"),))
        with pytest.raises(InvalidOperationError):
            create_person(session, actor=_admin(), display_name="sam taylor")

    def test_an_acknowledged_duplicate_creates_a_second_person(self):
        """Two people genuinely share a name. The decision stays human."""
        existing = _person(50, "Sam Taylor")
        session = DirectorySession(name_matches=(existing,))

        created = create_person(
            session,
            actor=_admin(),
            display_name="Sam Taylor",
            acknowledge_duplicate_name=True,
        )
        assert created.id != existing.id

    def test_a_blank_name_is_refused(self, session):
        with pytest.raises(InvalidOperationError):
            create_person(session, actor=_admin(), display_name="   ")

    def test_the_name_is_stored_trimmed(self, session):
        person = create_person(
            session, actor=_admin(), display_name="  Padded Name  ",
            acknowledge_duplicate_name=True,
        )
        assert person.display_name == "Padded Name"

    def test_a_blank_phone_becomes_null_rather_than_spaces(self, session):
        person = create_person(
            session, actor=_admin(), display_name="New Person", phone="   "
        )
        assert person.phone is None

    def test_the_person_belongs_to_the_actors_church(self, session):
        """Never a caller-supplied church id -- V1 is single-church."""
        actor = _admin()
        actor.church_id = 42
        person = create_person(session, actor=actor, display_name="New Person")
        assert person.church_id == 42

    def test_an_address_another_person_holds_is_refused(self):
        session = DirectorySession(email_holder=_person(60, "Other", email="a@example.test"))
        with pytest.raises(InvalidOperationError) as caught:
            create_person(
                session, actor=_admin(), display_name="New Person",
                email="a@example.test",
            )
        assert "already linked" in str(caught.value)
        # The holder is not named: an error message is the wrong place to
        # disclose who owns an address.
        assert "Other" not in str(caught.value)

    def test_a_malformed_address_is_refused(self, session):
        with pytest.raises(InvalidOperationError):
            create_person(
                session, actor=_admin(), display_name="New Person", email="not-an-address"
            )

    def test_the_address_is_stored_normalized(self, session):
        person = create_person(
            session, actor=_admin(), display_name="New Person",
            email="  Mixed.Case@Example.TEST  ",
        )
        assert person.email == "mixed.case@example.test"

    def test_a_deactivated_ministry_is_refused(self, session):
        """Checked by its Head -- an Admin who does not lead it never gets far
        enough to see the deactivation, being refused by authorization first.
        """
        actor = _person(12, "Retired Ministry Head", heads=(100,))
        with pytest.raises(InvalidOperationError):
            create_person(
                session, actor=actor, display_name="New Person",
                initial_ministry=_ministry(100, "Retired", deactivated=True),
            )

    def test_creation_is_audited(self, session):
        create_person(session, actor=_admin(), display_name="New Person")
        rows = _audit_rows(session)
        assert len(rows) == 1
        assert rows[0].action == ACTION_PERSON_CREATED
        assert rows[0].target_table == "person"
        assert "New Person" in rows[0].summary

    def test_the_audit_payload_records_whether_they_can_sign_in_not_the_address(
        self, session
    ):
        create_person(
            session, actor=_admin(), display_name="New Person",
            email="new.person@example.test",
        )
        row = _audit_rows(session)[0]
        assert row.after_values["email_linked"] is True
        assert "new.person@example.test" not in str(row.after_values)
        assert "new.person@example.test" not in row.summary


# --------------------------------------------------------------------------
# Editing, deactivating and reactivating
# --------------------------------------------------------------------------


class TestUpdatePerson:
    def test_only_an_admin_may_edit(self, session):
        with pytest.raises(AuthorizationError):
            update_person(
                session, actor=_head(), person=_person(20, "Someone"),
                display_name="Renamed",
            )

    def test_an_admin_may_edit_name_and_phone(self, session):
        person = _person(20, "Old Name")
        update_person(
            session, actor=_admin(), person=person, display_name="New Name",
            phone="555-0100",
        )
        assert person.display_name == "New Name"
        assert person.phone == "555-0100"

    def test_an_unchanged_edit_writes_no_audit_row(self, session):
        person = _person(20, "Same Name")
        update_person(session, actor=_admin(), person=person, display_name="Same Name")
        assert _audit_rows(session) == []

    def test_an_edit_cannot_change_the_sign_in_address(self, session):
        person = _person(20, "Someone", email="someone@example.test")
        update_person(session, actor=_admin(), person=person, display_name="Renamed")
        assert person.email == "someone@example.test"

    def test_an_edit_cannot_deactivate_somebody(self, session):
        person = _person(20, "Someone")
        update_person(session, actor=_admin(), person=person, display_name="Renamed")
        assert person.deactivated_at is None

    def test_an_edit_cannot_grant_admin_authority(self, session):
        person = _person(20, "Someone")
        update_person(session, actor=_admin(), person=person, display_name="Renamed")
        assert person.is_admin is False

    def test_an_edit_is_audited_with_both_sides(self, session):
        person = _person(20, "Old Name")
        update_person(
            session, actor=_admin(), person=person, display_name="New Name"
        )
        row = _audit_rows(session)[0]
        assert row.action == ACTION_PERSON_UPDATED
        assert row.before_values["display_name"] == "Old Name"
        assert row.after_values["display_name"] == "New Name"


class TestDeactivateAndReactivate:
    """Task 79 §5: church-wide, Admin-only, and never a delete."""

    def test_a_head_may_not_deactivate_a_person_church_wide(self, session):
        with pytest.raises(AuthorizationError):
            deactivate_person(session, actor=_head(), person=_person(20, "Someone"))

    def test_a_volunteer_may_not_deactivate_a_person(self, session):
        with pytest.raises(AuthorizationError):
            deactivate_person(session, actor=_volunteer(), person=_person(20, "Someone"))

    def test_an_admin_may_deactivate_and_reactivate(self, session):
        person = _person(20, "Someone")
        deactivate_person(session, actor=_admin(), person=person)
        assert person.deactivated_at is not None

        reactivate_person(session, actor=_admin(), person=person)
        assert person.deactivated_at is None

    def test_deactivation_preserves_memberships_and_head_authority(self, session):
        """Nothing is cleared, which is the only reason reactivation can
        restore the person exactly."""
        person = _person(20, "Someone", heads=(100,), member_of=(200,))
        before = [
            (m.ministry_id, m.is_ministry_head, m.deactivated_at)
            for m in person.ministry_memberships
        ]

        deactivate_person(session, actor=_admin(), person=person)

        after = [
            (m.ministry_id, m.is_ministry_head, m.deactivated_at)
            for m in person.ministry_memberships
        ]
        assert after == before

    def test_deactivation_is_idempotent_and_keeps_the_first_instant(self, session):
        person = _person(20, "Someone", deactivated=True)
        deactivate_person(session, actor=_admin(), person=person)
        assert person.deactivated_at == _THEN
        assert _audit_rows(session) == []

    def test_reactivating_an_active_person_writes_nothing(self, session):
        person = _person(20, "Someone")
        reactivate_person(session, actor=_admin(), person=person)
        assert _audit_rows(session) == []

    def test_an_admin_cannot_deactivate_themselves(self, session):
        """Otherwise the only role that can undo it is the one just removed."""
        admin = _admin(11)
        with pytest.raises(InvalidOperationError) as caught:
            deactivate_person(session, actor=admin, person=admin)
        assert "themselves" in str(caught.value)
        assert admin.deactivated_at is None

    def test_both_ends_are_audited(self, session):
        person = _person(20, "Someone")
        deactivate_person(session, actor=_admin(), person=person)
        reactivate_person(session, actor=_admin(), person=person)

        actions = [row.action for row in _audit_rows(session)]
        assert actions == [ACTION_PERSON_DEACTIVATED, ACTION_PERSON_REACTIVATED]

    def test_the_audit_action_is_never_a_deletion(self, session):
        person = _person(20, "Someone")
        deactivate_person(session, actor=_admin(), person=person)
        assert "DELETE" not in _audit_rows(session)[0].action


# --------------------------------------------------------------------------
# The sign-in link
# --------------------------------------------------------------------------


class TestAuthLink:
    """Task 79 §7: Admin-only, and reusing the existing validation."""

    def test_a_head_may_not_manage_sign_in_access(self, session):
        with pytest.raises(AuthorizationError):
            set_person_auth_link(
                session, actor=_head(), person=_person(20, "Someone"),
                email="someone@example.test",
            )

    def test_an_admin_may_link_an_address(self, session):
        person = _person(20, "Someone")
        set_person_auth_link(
            session, actor=_admin(), person=person, email="Someone@Example.TEST"
        )
        assert person.email == "someone@example.test"

    def test_an_admin_may_unlink_an_address(self, session):
        person = _person(20, "Someone", email="someone@example.test")
        set_person_auth_link(session, actor=_admin(), person=person, email=None)
        assert person.email is None
        # The Person itself survives untouched.
        assert person.deactivated_at is None
        assert person.display_name == "Someone"

    def test_relinking_the_same_address_writes_nothing(self, session):
        person = _person(20, "Someone", email="someone@example.test")
        set_person_auth_link(
            session, actor=_admin(), person=person, email="someone@example.test"
        )
        assert _audit_rows(session) == []

    def test_an_address_another_person_holds_is_refused(self):
        session = DirectorySession(
            email_holder=_person(60, "Other", email="taken@example.test")
        )
        with pytest.raises(InvalidOperationError):
            set_person_auth_link(
                session, actor=_admin(), person=_person(20, "Someone"),
                email="taken@example.test",
            )

    def test_a_person_may_keep_their_own_address(self):
        """Re-submitting an unchanged form is not a collision with oneself."""
        person = _person(20, "Someone", email="someone@example.test")
        session = DirectorySession(email_holder=person)
        set_person_auth_link(
            session, actor=_admin(), person=person, email="SOMEONE@example.test"
        )
        assert person.email == "someone@example.test"

    def test_a_malformed_address_is_refused_by_the_shared_validator(self, session):
        with pytest.raises(InvalidOperationError):
            set_person_auth_link(
                session, actor=_admin(), person=_person(20, "Someone"),
                email="two@@example.test",
            )

    def test_a_deactivated_person_may_still_be_prepared(self, session):
        """Matches the CLI exactly: the link is written and sign-in is still
        refused at the callback."""
        person = _person(20, "Someone", deactivated=True)
        set_person_auth_link(
            session, actor=_admin(), person=person, email="someone@example.test"
        )
        assert person.email == "someone@example.test"

    def test_the_address_never_reaches_the_audit_trail(self, session):
        person = _person(20, "Someone")
        set_person_auth_link(
            session, actor=_admin(), person=person, email="private@example.test"
        )
        row = _audit_rows(session)[0]
        assert row.action == ACTION_PERSON_AUTH_LINK_CHANGED
        assert "private@example.test" not in row.summary
        assert "private@example.test" not in str(row.before_values)
        assert "private@example.test" not in str(row.after_values)
        assert row.after_values["email_linked"] is True


# --------------------------------------------------------------------------
# Formal church membership status
# --------------------------------------------------------------------------


class TestChurchMembershipStatus:
    """Task 79 §6: MEMBER / NON_MEMBER / UNKNOWN, Admin-only, never inferred."""

    def test_an_admin_may_set_it(self, session):
        person = _person(20, "Someone")
        set_church_membership_status(
            session, actor=_admin(), person=person,
            status=CHURCH_MEMBERSHIP_STATUS_MEMBER,
        )
        assert person.church_membership_status == CHURCH_MEMBERSHIP_STATUS_MEMBER

    def test_a_head_may_not_set_it(self, session):
        """A Head reads it on the directory and cannot change it: whether
        somebody is a member of the church is not a fact about one team."""
        with pytest.raises(AuthorizationError):
            set_church_membership_status(
                session, actor=_head(), person=_person(21, "Someone"),
                status=CHURCH_MEMBERSHIP_STATUS_MEMBER,
            )

    def test_a_volunteer_may_not_set_it(self, session):
        with pytest.raises(AuthorizationError):
            set_church_membership_status(
                session, actor=_volunteer(), person=_person(22, "Someone"),
                status=CHURCH_MEMBERSHIP_STATUS_MEMBER,
            )

    def test_a_deactivated_admin_may_not_set_it(self, session):
        stale = _person(23, "Former Admin", is_admin=True, deactivated=True)
        with pytest.raises(AuthorizationError):
            set_church_membership_status(
                session, actor=stale, person=_person(24, "Someone"),
                status=CHURCH_MEMBERSHIP_STATUS_MEMBER,
            )

    def test_a_status_outside_the_three_is_refused(self, session):
        with pytest.raises(InvalidOperationError) as caught:
            set_church_membership_status(
                session, actor=_admin(), person=_person(25, "Someone"),
                status="VISITOR",
            )
        assert "MEMBER" in str(caught.value)

    def test_a_lowercase_spelling_is_refused_rather_than_coerced(self, session):
        """These are enum values a client copies, not prose somebody typed.
        Quietly accepting one spelling would leave the rejected one whichever
        nobody happened to test."""
        with pytest.raises(InvalidOperationError):
            set_church_membership_status(
                session, actor=_admin(), person=_person(26, "Someone"),
                status="member",
            )

    def test_unknown_is_a_real_value_somebody_can_be_set_back_to(self, session):
        person = _person(
            27, "Someone", church_membership_status=CHURCH_MEMBERSHIP_STATUS_MEMBER
        )
        set_church_membership_status(
            session, actor=_admin(), person=person,
            status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
        )
        assert person.church_membership_status == CHURCH_MEMBERSHIP_STATUS_UNKNOWN

    def test_it_is_audited_with_both_statuses_and_nothing_else(self, session):
        person = _person(28, "Someone", email="someone@example.test")
        set_church_membership_status(
            session, actor=_admin(), person=person,
            status=CHURCH_MEMBERSHIP_STATUS_MEMBER,
        )
        rows = _audit_rows(session)
        assert len(rows) == 1
        assert rows[0].action == ACTION_PERSON_CHURCH_MEMBERSHIP_STATUS_CHANGED
        assert rows[0].before_values == {
            "church_membership_status": CHURCH_MEMBERSHIP_STATUS_UNKNOWN
        }
        assert rows[0].after_values == {
            "church_membership_status": CHURCH_MEMBERSHIP_STATUS_MEMBER
        }
        # No contact PII rides along in a permanently retained payload.
        assert "someone@example.test" not in str(rows[0].after_values)

    def test_restating_the_current_status_writes_no_audit_row(self, session):
        person = _person(
            29, "Someone", church_membership_status=CHURCH_MEMBERSHIP_STATUS_MEMBER
        )
        set_church_membership_status(
            session, actor=_admin(), person=person,
            status=CHURCH_MEMBERSHIP_STATUS_MEMBER,
        )
        assert _audit_rows(session) == []

    def test_ministry_participation_does_not_change_it(self, session):
        """The one thing this status must never be derived from. Somebody who
        heads two ministries is still UNKNOWN until an Admin says otherwise."""
        busy = _person(30, "Very Involved", heads=(100, 200))
        assert busy.church_membership_status == CHURCH_MEMBERSHIP_STATUS_UNKNOWN

    def test_a_new_person_starts_unknown(self, session):
        person = create_person(session, actor=_admin(), display_name="New Person")
        assert person.church_membership_status == CHURCH_MEMBERSHIP_STATUS_UNKNOWN

    def test_creation_never_accepts_a_status(self, session):
        """Not a parameter at all: a Ministry Head can reach ``create_person``,
        and a church-wide governance value must not be settable by whoever
        happens to be filling in the form."""
        import inspect

        assert "church_membership_status" not in inspect.signature(
            create_person
        ).parameters
        assert "church_membership_status" not in inspect.signature(
            update_person
        ).parameters


# --------------------------------------------------------------------------
# Recorded serving on a directory read
# --------------------------------------------------------------------------


class TestDirectoryServingTotals:
    """§14: bounded aggregate querying, never one query per person."""

    def test_the_total_reaches_the_directory_entry(self):
        rows = (
            SimpleNamespace(
                person_id=1, display_name="A", email=None, phone=None,
                is_admin=False,
                church_membership_status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
                deactivated_at=None,
            ),
        )
        session = DirectorySession(
            page_rows=rows,
            total=1,
            serving_rows=(SimpleNamespace(person_id=1, recorded_serving=7),),
        )
        page = list_people(session, actor=_admin())
        assert page.people[0].recorded_serving_total == 7

    def test_somebody_with_no_history_reads_zero_not_missing(self):
        rows = (
            SimpleNamespace(
                person_id=2, display_name="B", email=None, phone=None,
                is_admin=False,
                church_membership_status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
                deactivated_at=None,
            ),
        )
        session = DirectorySession(page_rows=rows, total=1)
        page = list_people(session, actor=_admin())
        assert page.people[0].recorded_serving_total == 0

    def test_the_breakdown_is_not_fetched_by_the_listing(self):
        """The directory buys the total alone. ``serving_summary`` stays None
        so a client can tell "not asked for" from "no history"."""
        rows = (
            SimpleNamespace(
                person_id=3, display_name="C", email=None, phone=None,
                is_admin=False,
                church_membership_status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
                deactivated_at=None,
            ),
        )
        session = DirectorySession(page_rows=rows, total=1)
        page = list_people(session, actor=_admin())
        assert page.people[0].serving_summary is None
        assert not any("array_agg" in sql for sql in session.executed_sql)

    def test_a_page_of_many_people_issues_one_serving_query(self):
        """Not one per person, and not one per ministry (§14)."""
        rows = tuple(
            SimpleNamespace(
                person_id=index, display_name=f"P{index}", email=None, phone=None,
                is_admin=False,
                church_membership_status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
                deactivated_at=None,
            )
            for index in range(1, 26)
        )
        session = DirectorySession(page_rows=rows, total=25)
        list_people(session, actor=_admin())
        serving_queries = [sql for sql in session.executed_sql if "assignment" in sql]
        assert len(serving_queries) == 1

    def test_a_detail_read_carries_the_breakdown_and_the_conflicts(self):
        session = DirectorySession(
            serving_rows=(
                SimpleNamespace(
                    person_id=40, ministry_id=100, ministry_name="AV",
                    recorded_serving=9,
                ),
                SimpleNamespace(
                    person_id=40, ministry_id=200, ministry_name="Setup",
                    recorded_serving=12,
                ),
            ),
        )
        entry = read_person(session, actor=_admin(), person=_person(40, "Someone"))
        assert entry.recorded_serving_total == 21
        assert [item.ministry_name for item in entry.serving_summary.by_ministry] == [
            "AV",
            "Setup",
        ]
        assert entry.serving_summary.same_date_conflicts == ()

    def test_the_total_is_the_sum_of_the_breakdown_by_construction(self):
        session = DirectorySession(
            serving_rows=(
                SimpleNamespace(
                    person_id=41, ministry_id=100, ministry_name="AV",
                    recorded_serving=3,
                ),
            ),
        )
        entry = read_person(session, actor=_admin(), person=_person(41, "Someone"))
        assert entry.recorded_serving_total == sum(
            item.count for item in entry.serving_summary.by_ministry
        )


# --------------------------------------------------------------------------
# Cross-cutting guarantees
# --------------------------------------------------------------------------


class TestTransactionAndAuditDiscipline:
    def test_no_operation_commits_or_rolls_back(self, session):
        """The session forbids both; reaching here means none was called."""
        person = create_person(session, actor=_admin(), display_name="New Person")
        update_person(session, actor=_admin(), person=person, display_name="Renamed")
        deactivate_person(session, actor=_admin(), person=person)
        reactivate_person(session, actor=_admin(), person=person)
        assert session.commit_calls == 0
        assert session.rollback_calls == 0

    def test_every_audit_target_table_is_one_the_database_accepts(self, session):
        person = create_person(session, actor=_admin(), display_name="New Person")
        update_person(session, actor=_admin(), person=person, display_name="Renamed")
        deactivate_person(session, actor=_admin(), person=person)
        for row in _audit_rows(session):
            assert row.target_table in AUDIT_TARGET_TABLES

    def test_the_mutation_and_its_audit_row_share_one_session(self, session):
        person = create_person(session, actor=_admin(), display_name="New Person")
        assert person in session.new
        assert _audit_rows(session)[0] in session.new

    def test_no_operation_issues_a_delete(self, session):
        person = create_person(session, actor=_admin(), display_name="New Person")
        deactivate_person(session, actor=_admin(), person=person)
        assert not any("DELETE" in sql.upper() for sql in session.executed_sql)
        assert list(session.deleted) == []
