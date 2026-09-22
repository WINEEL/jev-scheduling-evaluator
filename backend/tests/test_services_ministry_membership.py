"""Membership lifecycle service tests (Task 79).

Offline: no PostgreSQL, no Neon, no network. Same strategy as
``tests/test_services_person_directory.py``: a real, unbound Session subclass
whose ``flush()`` assigns identities and whose ``execute()`` answers the one
lookup this module makes, with ``commit()`` and ``rollback()`` forbidden.

The rules under test are the ones that decide whether a Ministry Head's
authority stops at their own ministry, so most of this file is about refusals.

Every person and ministry name here is fictional.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AUDIT_TARGET_TABLES, AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.services.audit import (
    ACTION_MINISTRY_HEAD_GRANTED,
    ACTION_MINISTRY_HEAD_REVOKED,
    ACTION_MINISTRY_MEMBERSHIP_ADDED,
    ACTION_MINISTRY_MEMBERSHIP_CHANGED,
    ACTION_MINISTRY_MEMBERSHIP_REMOVED,
)
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.ministry_membership import (
    _membership_lookup_statement,
    add_person_to_ministry,
    remove_person_from_ministry,
    set_ministry_head_authority,
    update_membership,
)

UTC = datetime.timezone.utc
_THEN = datetime.datetime(2026, 1, 1, tzinfo=UTC)

AV = 100
KIDS = 200


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


class MembershipSession(Session):
    """A real, unbound Session answering the one membership lookup."""

    def __init__(
        self, *, next_id: int = 700, existing: MinistryMembership | None = None
    ) -> None:
        super().__init__()
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self._next_id = next_id
        self.existing = existing
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
        self.executed_sql.append(str(statement))
        return _Result(self.existing)


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.fixture
def session() -> MembershipSession:
    return MembershipSession()


def _person(
    person_id: int,
    name: str,
    *,
    is_admin: bool = False,
    deactivated: bool = False,
    heads: tuple[int, ...] = (),
    member_of: tuple[int, ...] = (),
) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
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


def _membership(
    membership_id: int,
    *,
    person: Person,
    ministry: Ministry,
    is_head: bool = False,
    deactivated: bool = False,
    notes: str | None = None,
    joined_on: datetime.date | None = None,
) -> MinistryMembership:
    """A membership with both relationships already populated.

    ``person`` and ``ministry`` are set so the audit summary can read their
    names without lazy-loading off an unbound Session.
    """
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
        notes=notes, joined_on=joined_on,
    )
    membership.id = membership_id
    membership.deactivated_at = _THEN if deactivated else None
    membership.person = person
    membership.ministry = ministry
    return membership


def _admin(person_id: int = 1) -> Person:
    return _person(person_id, "Admin Person", is_admin=True)


def _av_head(person_id: int = 2) -> Person:
    return _person(person_id, "AV Head", heads=(AV,))


def _admin_av_head(person_id: int = 5) -> Person:
    """An Admin who *also* actively heads AV.

    The distinction Task 79 turns on: they may operate AV because they lead
    it, not because ``is_admin`` is true. Every operational test that needs an
    Admin-shaped actor uses this one, and the tests that need an Admin who
    leads nothing use :func:`_admin` to prove the refusal.
    """
    return _person(person_id, "Admin And AV Head", is_admin=True, heads=(AV,))


def _volunteer(person_id: int = 4) -> Person:
    return _person(person_id, "Volunteer", member_of=(AV,))


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


# --------------------------------------------------------------------------
# Adding somebody to a ministry
# --------------------------------------------------------------------------


class TestAddPersonToMinistry:
    def test_a_head_may_add_an_existing_person_to_their_own_ministry(self, session):
        person = _person(20, "New Member")
        membership = add_person_to_ministry(
            session, actor=_av_head(), person=person, ministry=_ministry(AV, "AV")
        )
        assert membership.person_id == 20
        assert membership.ministry_id == AV
        assert membership.deactivated_at is None

    def test_a_head_may_not_add_to_an_unrelated_ministry(self, session):
        """The core cross-ministry refusal."""
        with pytest.raises(AuthorizationError):
            add_person_to_ministry(
                session, actor=_av_head(), person=_person(20, "New Member"),
                ministry=_ministry(KIDS, "Kids"),
            )

    def test_an_admin_who_heads_nothing_may_not_add_to_a_ministry(self, session):
        """**Task 79 §1.** Admin status alone is oversight, not operation:
        rostering a team belongs to whoever runs it. An Admin who needs
        somebody on a team asks that team's Head, or appoints one."""
        with pytest.raises(AuthorizationError) as caught:
            add_person_to_ministry(
                session, actor=_admin(), person=_person(20, "New Member"),
                ministry=_ministry(KIDS, "Kids"),
            )
        assert "Ministry Head" in str(caught.value)

    def test_an_admin_who_heads_the_ministry_may_add_to_it(self, session):
        """And does so through the Head membership, not the Admin flag -- which
        is why they still may not add to Kids, below."""
        membership = add_person_to_ministry(
            session, actor=_admin_av_head(), person=_person(20, "New Member"),
            ministry=_ministry(AV, "AV"),
        )
        assert membership.ministry_id == AV

    def test_an_admin_who_heads_av_still_may_not_add_to_kids(self, session):
        with pytest.raises(AuthorizationError):
            add_person_to_ministry(
                session, actor=_admin_av_head(), person=_person(20, "New Member"),
                ministry=_ministry(KIDS, "Kids"),
            )

    def test_a_volunteer_may_not_add_anybody(self, session):
        with pytest.raises(AuthorizationError):
            add_person_to_ministry(
                session, actor=_volunteer(), person=_person(20, "New Member"),
                ministry=_ministry(AV, "AV"),
            )

    def test_a_deactivated_head_may_not_add_anybody(self, session):
        stale = _person(5, "Former Head", heads=(AV,), deactivated=True)
        with pytest.raises(AuthorizationError):
            add_person_to_ministry(
                session, actor=stale, person=_person(20, "New Member"),
                ministry=_ministry(AV, "AV"),
            )

    def test_adding_somebody_already_on_the_team_is_idempotent(self):
        """A duplicate request, not an error -- and no audit row, because no
        domain state changed."""
        person = _person(20, "Member")
        ministry = _ministry(AV, "AV")
        existing = _membership(700, person=person, ministry=ministry)
        session = MembershipSession(existing=existing)

        returned = add_person_to_ministry(
            session, actor=_av_head(), person=person, ministry=ministry
        )
        assert returned is existing
        assert _audit_rows(session) == []

    def test_rejoining_reactivates_the_original_row(self):
        """Never a second row: their serving history points at this one."""
        person = _person(20, "Returning Member")
        ministry = _ministry(AV, "AV")
        existing = _membership(700, person=person, ministry=ministry, deactivated=True)
        session = MembershipSession(existing=existing)

        returned = add_person_to_ministry(
            session, actor=_av_head(), person=person, ministry=ministry
        )
        assert returned is existing
        assert returned.id == 700
        assert returned.deactivated_at is None
        assert not [obj for obj in session.new if isinstance(obj, MinistryMembership)]

    def test_rejoining_does_not_restore_head_authority(self):
        person = _person(20, "Returning Member")
        ministry = _ministry(AV, "AV")
        existing = _membership(700, person=person, ministry=ministry, deactivated=True)
        session = MembershipSession(existing=existing)

        returned = add_person_to_ministry(
            session, actor=_av_head(), person=person, ministry=ministry
        )
        assert returned.is_ministry_head is False

    def test_rejoining_keeps_the_previous_notes_when_none_are_supplied(self):
        person = _person(20, "Returning Member")
        ministry = _ministry(AV, "AV")
        existing = _membership(
            700, person=person, ministry=ministry, deactivated=True,
            notes="Prefers the early service",
        )
        session = MembershipSession(existing=existing)

        add_person_to_ministry(
            session, actor=_av_head(), person=person, ministry=ministry
        )
        assert existing.notes == "Prefers the early service"

    def test_a_new_membership_never_carries_head_authority(self, session):
        """A Head reaching this function must not be able to promote by adding."""
        membership = add_person_to_ministry(
            session, actor=_av_head(), person=_person(20, "New Member"),
            ministry=_ministry(AV, "AV"),
        )
        assert membership.is_ministry_head is False

    def test_a_deactivated_ministry_is_refused(self, session):
        with pytest.raises(InvalidOperationError):
            add_person_to_ministry(
                session, actor=_av_head(), person=_person(20, "New Member"),
                ministry=_ministry(AV, "AV", deactivated=True),
            )

    def test_a_church_wide_deactivated_person_is_refused(self, session):
        """Otherwise a Head could undo an Admin's church-wide decision."""
        with pytest.raises(InvalidOperationError) as caught:
            add_person_to_ministry(
                session, actor=_av_head(), person=_person(20, "Left", deactivated=True),
                ministry=_ministry(AV, "AV"),
            )
        assert "reactivate" in str(caught.value)

    def test_a_blank_note_becomes_null(self, session):
        membership = add_person_to_ministry(
            session, actor=_av_head(), person=_person(20, "New Member"),
            ministry=_ministry(AV, "AV"), notes="   ",
        )
        assert membership.notes is None

    def test_adding_is_audited_against_the_ministry(self, session):
        add_person_to_ministry(
            session, actor=_av_head(), person=_person(20, "New Member"),
            ministry=_ministry(AV, "AV"),
        )
        row = _audit_rows(session)[0]
        assert row.action == ACTION_MINISTRY_MEMBERSHIP_ADDED
        assert row.target_table == "ministry_membership"
        assert row.ministry_id == AV
        assert "New Member" in row.summary
        assert "AV" in row.summary


# --------------------------------------------------------------------------
# Removing somebody from a ministry
# --------------------------------------------------------------------------


class TestRemovePersonFromMinistry:
    def test_a_head_may_remove_from_their_own_ministry(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        remove_person_from_ministry(session, actor=_av_head(), membership=membership)
        assert membership.deactivated_at is not None

    def test_a_head_may_not_remove_from_an_unrelated_ministry(self, session):
        """The other half of the cross-ministry refusal."""
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(KIDS, "Kids")
        )
        with pytest.raises(AuthorizationError):
            remove_person_from_ministry(
                session, actor=_av_head(), membership=membership
            )
        assert membership.deactivated_at is None

    def test_a_volunteer_may_not_remove_anybody(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        with pytest.raises(AuthorizationError):
            remove_person_from_ministry(
                session, actor=_volunteer(), membership=membership
            )

    def test_removing_a_head_requires_an_admin(self, session):
        """Core §5.1: removal necessarily revokes head authority, and revoking
        is Admin-only -- so a Head cannot depose a co-head."""
        co_head = _membership(
            700, person=_person(21, "Co-head"), ministry=_ministry(AV, "AV"),
            is_head=True,
        )
        with pytest.raises(AuthorizationError):
            remove_person_from_ministry(session, actor=_av_head(), membership=co_head)

        # Refused before anything was mutated.
        assert co_head.is_ministry_head is True
        assert co_head.deactivated_at is None
        assert _audit_rows(session) == []

    def test_an_admin_removing_a_head_writes_two_audit_rows(self, session):
        """Two things happened, and both are worth seeing."""
        head_membership = _membership(
            700, person=_person(21, "Co-head"), ministry=_ministry(AV, "AV"),
            is_head=True,
        )
        remove_person_from_ministry(
            session, actor=_admin_av_head(), membership=head_membership
        )

        assert head_membership.is_ministry_head is False
        assert head_membership.deactivated_at is not None
        actions = [row.action for row in _audit_rows(session)]
        assert actions == [
            ACTION_MINISTRY_HEAD_REVOKED,
            ACTION_MINISTRY_MEMBERSHIP_REMOVED,
        ]

    def test_removal_is_idempotent_and_keeps_the_first_instant(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV"),
            deactivated=True,
        )
        remove_person_from_ministry(session, actor=_av_head(), membership=membership)
        assert membership.deactivated_at == _THEN
        assert _audit_rows(session) == []

    def test_removal_never_issues_a_delete(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        remove_person_from_ministry(session, actor=_av_head(), membership=membership)
        assert list(session.deleted) == []
        assert not any("DELETE" in sql.upper() for sql in session.executed_sql)

    def test_removal_leaves_the_person_record_alone(self, session):
        """One ministry, not the church."""
        person = _person(20, "Member")
        membership = _membership(700, person=person, ministry=_ministry(AV, "AV"))
        remove_person_from_ministry(session, actor=_av_head(), membership=membership)
        assert person.deactivated_at is None

    def test_the_summary_names_the_ministry_it_changed(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        remove_person_from_ministry(session, actor=_av_head(), membership=membership)
        row = _audit_rows(session)[0]
        assert row.summary == "Removed Member from AV"
        assert row.ministry_id == AV


# --------------------------------------------------------------------------
# Editing the ministry-scoped detail
# --------------------------------------------------------------------------


class TestUpdateMembership:
    def test_a_head_may_edit_their_own_ministrys_membership(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        update_membership(
            session, actor=_av_head(), membership=membership,
            notes="Sound desk only", joined_on=datetime.date(2026, 3, 1),
        )
        assert membership.notes == "Sound desk only"
        assert membership.joined_on == datetime.date(2026, 3, 1)

    def test_a_head_may_not_edit_another_ministrys_membership(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(KIDS, "Kids")
        )
        with pytest.raises(AuthorizationError):
            update_membership(
                session, actor=_av_head(), membership=membership, notes="Anything"
            )
        assert membership.notes is None

    def test_an_unchanged_edit_writes_no_audit_row(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV"),
            notes="Same note",
        )
        update_membership(
            session, actor=_av_head(), membership=membership, notes="Same note"
        )
        assert _audit_rows(session) == []

    def test_an_edit_cannot_grant_head_authority(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        update_membership(session, actor=_av_head(), membership=membership, notes="x")
        assert membership.is_ministry_head is False

    def test_an_edit_cannot_remove_somebody(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        update_membership(session, actor=_av_head(), membership=membership, notes="x")
        assert membership.deactivated_at is None

    def test_dates_are_rendered_as_iso_strings_in_the_payload(self, session):
        """JSONB has no date type; ISO 8601 keeps the payload readable and
        comparable."""
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        update_membership(
            session, actor=_av_head(), membership=membership,
            joined_on=datetime.date(2026, 3, 1),
        )
        row = _audit_rows(session)[0]
        assert row.action == ACTION_MINISTRY_MEMBERSHIP_CHANGED
        assert row.after_values["joined_on"] == "2026-03-01"
        assert row.before_values["joined_on"] is None


# --------------------------------------------------------------------------
# Cross-cutting guarantees
# --------------------------------------------------------------------------


class TestSetMinistryHeadAuthority:
    """Task 79 §10: appointing a ministry's leader is Admin governance.

    The bootstrapping case is the reason this operation exists at all. Under
    :func:`~app.services.authorization.require_ministry_operator` a ministry
    nobody heads is a ministry nobody may add to -- so a newly created one
    could never acquire its first head if promotion needed a membership that
    only a head could create. An Admin appointing somebody is the governance
    act that breaks that circle.
    """

    def test_an_admin_may_appoint_a_head_where_no_membership_exists(self, session):
        person = _person(30, "New Leader")
        membership = set_ministry_head_authority(
            session, actor=_admin(), person=person,
            ministry=_ministry(KIDS, "Kids"), is_ministry_head=True,
        )
        assert membership.ministry_id == KIDS
        assert membership.is_ministry_head is True

    def test_appointing_writes_both_audit_rows(self, session):
        """The membership was created and authority was granted: two acts."""
        set_ministry_head_authority(
            session, actor=_admin(), person=_person(31, "New Leader"),
            ministry=_ministry(KIDS, "Kids"), is_ministry_head=True,
        )
        actions = [row.action for row in _audit_rows(session)]
        assert actions == [
            ACTION_MINISTRY_MEMBERSHIP_ADDED,
            ACTION_MINISTRY_HEAD_GRANTED,
        ]

    def test_appointing_somebody_already_on_the_team_writes_only_the_grant(self):
        person = _person(32, "Existing Member")
        ministry = _ministry(AV, "AV")
        existing = _membership(700, person=person, ministry=ministry)
        session = MembershipSession(existing=existing)

        set_ministry_head_authority(
            session, actor=_admin(), person=person, ministry=ministry,
            is_ministry_head=True,
        )
        actions = [row.action for row in _audit_rows(session)]
        assert actions == [ACTION_MINISTRY_HEAD_GRANTED]
        assert existing.is_ministry_head is True

    def test_a_head_may_not_appoint_anybody(self, session):
        """Not in their own ministry, and not in any other: head authority
        propagates only from an Admin (core §4.3)."""
        with pytest.raises(AuthorizationError):
            set_ministry_head_authority(
                session, actor=_av_head(), person=_person(33, "Somebody"),
                ministry=_ministry(AV, "AV"), is_ministry_head=True,
            )

    def test_a_head_may_not_appoint_themselves_elsewhere(self, session):
        actor = _av_head()
        with pytest.raises(AuthorizationError):
            set_ministry_head_authority(
                session, actor=actor, person=actor,
                ministry=_ministry(KIDS, "Kids"), is_ministry_head=True,
            )

    def test_a_volunteer_may_not_appoint_anybody(self, session):
        with pytest.raises(AuthorizationError):
            set_ministry_head_authority(
                session, actor=_volunteer(), person=_person(34, "Somebody"),
                ministry=_ministry(AV, "AV"), is_ministry_head=True,
            )

    def test_a_deactivated_admin_may_not_appoint_anybody(self, session):
        stale = _person(35, "Former Admin", is_admin=True, deactivated=True)
        with pytest.raises(AuthorizationError):
            set_ministry_head_authority(
                session, actor=stale, person=_person(36, "Somebody"),
                ministry=_ministry(AV, "AV"), is_ministry_head=True,
            )

    def test_revoking_keeps_the_ordinary_membership(self):
        """§10: they stay on the team and simply no longer lead it."""
        person = _person(37, "Stepping Down")
        ministry = _ministry(AV, "AV")
        existing = _membership(700, person=person, ministry=ministry, is_head=True)
        session = MembershipSession(existing=existing)

        returned = set_ministry_head_authority(
            session, actor=_admin(), person=person, ministry=ministry,
            is_ministry_head=False,
        )
        assert returned is existing
        assert existing.is_ministry_head is False
        assert existing.deactivated_at is None
        actions = [row.action for row in _audit_rows(session)]
        assert actions == [ACTION_MINISTRY_HEAD_REVOKED]

    def test_revoking_where_there_is_no_membership_is_refused(self, session):
        """Rather than creating one, which would be an odd way to demote."""
        with pytest.raises(InvalidOperationError) as caught:
            set_ministry_head_authority(
                session, actor=_admin(), person=_person(38, "Stranger"),
                ministry=_ministry(KIDS, "Kids"), is_ministry_head=False,
            )
        assert "no membership" in str(caught.value)
        assert not [obj for obj in session.new if isinstance(obj, MinistryMembership)]

    def test_appointing_into_a_deactivated_ministry_is_refused(self, session):
        with pytest.raises(InvalidOperationError):
            set_ministry_head_authority(
                session, actor=_admin(), person=_person(39, "Somebody"),
                ministry=_ministry(AV, "AV", deactivated=True),
                is_ministry_head=True,
            )

    def test_appointing_a_church_wide_deactivated_person_is_refused(self, session):
        with pytest.raises(InvalidOperationError):
            set_ministry_head_authority(
                session, actor=_admin(), person=_person(40, "Left", deactivated=True),
                ministry=_ministry(AV, "AV"), is_ministry_head=True,
            )

    def test_appointing_is_idempotent(self):
        person = _person(41, "Already Leading")
        ministry = _ministry(AV, "AV")
        existing = _membership(700, person=person, ministry=ministry, is_head=True)
        session = MembershipSession(existing=existing)

        set_ministry_head_authority(
            session, actor=_admin(), person=person, ministry=ministry,
            is_ministry_head=True,
        )
        assert _audit_rows(session) == []


class TestLookupAndDiscipline:
    def test_the_lookup_spans_active_and_inactive_rows(self):
        """A deactivated membership is still the row a rejoin must reactivate,
        so the lookup must not filter it out."""
        sql = str(
            _membership_lookup_statement(person_id=20, ministry_id=AV).compile(
                dialect=postgresql.dialect()
            )
        )
        where_clause = sql.split("WHERE", 1)[1]
        assert "person_id" in where_clause
        assert "ministry_id" in where_clause
        # The column is selected, of course; what matters is that it does not
        # narrow the lookup.
        assert "deactivated_at" not in where_clause

    def test_no_operation_commits_or_rolls_back(self, session):
        actor = _admin_av_head()
        membership = add_person_to_ministry(
            session, actor=actor, person=_person(20, "Member"),
            ministry=_ministry(AV, "AV"),
        )
        membership.person = _person(20, "Member")
        membership.ministry = _ministry(AV, "AV")
        update_membership(session, actor=actor, membership=membership, notes="x")
        remove_person_from_ministry(session, actor=actor, membership=membership)
        assert session.commit_calls == 0
        assert session.rollback_calls == 0

    def test_every_audit_target_table_is_one_the_database_accepts(self, session):
        membership = _membership(
            700, person=_person(20, "Member"), ministry=_ministry(AV, "AV")
        )
        update_membership(session, actor=_av_head(), membership=membership, notes="x")
        remove_person_from_ministry(session, actor=_av_head(), membership=membership)
        for row in _audit_rows(session):
            assert row.target_table in AUDIT_TARGET_TABLES
