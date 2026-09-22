"""Linked-pair same-date exclusion service tests (Task 50).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows :mod:`tests.test_services_serving_limit` exactly, and
for the same reasons: this service flushes once (to obtain a new row's identity
for its audit row), looks a row up through ``session.execute``, and deletes on
the clearing path. So the lookups' SQL is compiled and inspected with no
session at all; the lookup's *use* is monkeypatched per test; and the flush and
delete run against a real, unbound ``Session`` subclass that simulates identity
assignment and records deletions.

Every person, ministry and period here is synthetic. Nothing in this file
describes a real couple, household or relationship of any kind -- the service
does not store one, and neither do its tests.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.db import Base
from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import (
    MembershipSameDateExclusion,
    SchedulingPeriod,
)
from app.services import AuthorizationError, InvalidOperationError
from app.services.audit import (
    ACTION_SAME_DATE_EXCLUSION_CLEARED,
    ACTION_SAME_DATE_EXCLUSION_RECORDED,
)
from app.services.same_date_exclusion import (
    SAME_DATE_LINKED_MEMBER_CONFLICT,
    _exclusion_statement,
    _member_exclusions_statement,
    _period_exclusions_statement,
    canonical_pair,
    clear_same_date_exclusion,
    get_linked_membership_ids,
    get_same_date_exclusion_pairs,
    set_same_date_exclusion,
)

KIDS = "Kids Ministry"
SETUP = "Setup"
UTC = datetime.timezone.utc


class ExclusionSession(Session):
    """A real, unbound Session: flush simulates identity, delete is recorded."""

    def __init__(self, *, next_id: int = 900) -> None:
        super().__init__()
        self.flush_calls = 0
        self.deleted_objects: list = []
        self._next_id = next_id

    def commit(self) -> None:  # pragma: no cover - must never run
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        raise AssertionError("a service must never roll back")

    def flush(self, objects=None) -> None:
        self.flush_calls += 1
        for obj in list(self.new):
            if getattr(obj, "id", None) is None:
                obj.id = self._next_id
                self._next_id += 1

    def delete(self, instance) -> None:
        self.deleted_objects.append(instance)


@pytest.fixture
def session() -> ExclusionSession:
    return ExclusionSession()


def _person(person_id: int, name: str, *, is_admin: bool = False,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    return person


def _ministry(ministry_id: int, name: str) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


def _actor_membership(membership_id: int, *, person: Person, ministry: Ministry,
                      is_head: bool = False) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _target_membership(membership_id: int, *, person: Person, ministry: Ministry,
                       deactivated: bool = False) -> MinistryMembership:
    membership = MinistryMembership(person_id=person.id, ministry_id=ministry.id)
    membership.id = membership_id
    membership.person = person
    membership.ministry = ministry
    if deactivated:
        membership.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    return membership


def _period(period_id: int, *, ministry: Ministry,
            name: str = "Kids Oct-Dec 2026") -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=name,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    period.ministry = ministry
    return period


def _existing(row_id: int, *, membership_a_id: int, membership_b_id: int,
              period: SchedulingPeriod) -> MembershipSameDateExclusion:
    row = MembershipSameDateExclusion(
        membership_a_id=membership_a_id,
        membership_b_id=membership_b_id,
        scheduling_period_id=period.id,
        ministry_id=period.ministry_id,
    )
    row.id = row_id
    return row


def _stub_lookup(monkeypatch, existing) -> list[dict]:
    """Replace the single-row lookup, recording what it was asked."""
    import app.services.same_date_exclusion as module

    seen: list[dict] = []

    def fake(session, *, membership_a_id, membership_b_id, scheduling_period_id):
        seen.append(
            {
                "membership_a_id": membership_a_id,
                "membership_b_id": membership_b_id,
                "scheduling_period_id": scheduling_period_id,
            }
        )
        return existing

    monkeypatch.setattr(module, "_find_existing_exclusion", fake)
    return seen


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


@pytest.fixture
def world():
    """One Kids ministry, an Admin, a Kids head, and two volunteers."""
    kids = _ministry(1, KIDS)
    setup = _ministry(2, SETUP)
    admin = _person(10, "Synthetic Admin", is_admin=True)
    kids_head = _person(11, "Synthetic Kids Head")
    _actor_membership(100, person=kids_head, ministry=kids, is_head=True)
    setup_head = _person(12, "Synthetic Setup Head")
    _actor_membership(101, person=setup_head, ministry=setup, is_head=True)
    volunteer_person = _person(13, "Volunteer A")
    ordinary = _person(14, "Volunteer B")
    membership_a = _target_membership(200, person=volunteer_person, ministry=kids)
    membership_b = _target_membership(201, person=ordinary, ministry=kids)
    # Being one of the linked volunteers confers no authority, so B is also an
    # ordinary member of Kids with no head flag anywhere.
    _actor_membership(201, person=ordinary, ministry=kids)
    period = _period(300, ministry=kids)
    return {
        "kids": kids, "setup": setup, "admin": admin, "head": kids_head,
        "setup_head": setup_head, "linked_volunteer": ordinary,
        "membership_a": membership_a, "membership_b": membership_b,
        "period": period,
    }


# ==========================================================================
# 1-2 -- create, and the unordered pair
# ==========================================================================


def test_01_a_head_creates_a_pair_rule_for_two_memberships_in_one_period(
    session, world, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    row = set_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_a"],
        membership_b=world["membership_b"], scheduling_period=world["period"],
    )

    assert row.membership_a_id == 200
    assert row.membership_b_id == 201
    assert row.scheduling_period_id == 300
    # Shared by all three composite foreign keys, and taken from the period.
    assert row.ministry_id == world["kids"].id
    assert session.flush_calls == 1


def test_02_the_reversed_pair_addresses_the_same_rule_and_makes_no_duplicate(
    session, world, monkeypatch
):
    """A+B and B+A are one logical rule, canonicalized before any lookup."""
    seen = _stub_lookup(monkeypatch, None)

    row = set_same_date_exclusion(
        session, actor=world["head"],
        # Deliberately the other way round.
        membership_a=world["membership_b"], membership_b=world["membership_a"],
        scheduling_period=world["period"],
    )

    assert (row.membership_a_id, row.membership_b_id) == (200, 201)
    assert seen == [
        {"membership_a_id": 200, "membership_b_id": 201, "scheduling_period_id": 300}
    ]


def test_02b_setting_an_existing_pair_again_is_an_idempotent_no_op(
    session, world, monkeypatch
):
    existing = _existing(
        800, membership_a_id=200, membership_b_id=201, period=world["period"]
    )
    _stub_lookup(monkeypatch, existing)

    row = set_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_b"],
        membership_b=world["membership_a"], scheduling_period=world["period"],
    )

    assert row is existing
    assert not [
        o for o in session.new if isinstance(o, MembershipSameDateExclusion)
    ]
    assert session.flush_calls == 0
    # Nothing changed, so nothing is recorded: an audit row here would claim a
    # head acted when they did not.
    assert not _audit_rows(session)


def test_02c_canonical_pair_is_order_insensitive():
    assert canonical_pair(201, 200) == (200, 201)
    assert canonical_pair(200, 201) == (200, 201)


# ==========================================================================
# 3 -- a membership cannot be paired with itself
# ==========================================================================


def test_03_pairing_a_membership_with_itself_is_rejected(session, world, monkeypatch):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="cannot be linked to itself"):
        set_same_date_exclusion(
            session, actor=world["head"], membership_a=world["membership_a"],
            membership_b=world["membership_a"], scheduling_period=world["period"],
        )
    assert not _audit_rows(session)


def test_03b_canonical_pair_refuses_equal_ids():
    with pytest.raises(InvalidOperationError, match="cannot be linked to itself"):
        canonical_pair(200, 200)


# ==========================================================================
# 4-5 -- cross-ministry pairs and foreign periods
# ==========================================================================


def test_04_a_cross_ministry_membership_pair_is_rejected(session, world, monkeypatch):
    """A Kids head may not link a Kids member to a Setup member."""
    _stub_lookup(monkeypatch, None)
    setup_member = _target_membership(
        202, person=_person(15, "Volunteer C"), ministry=world["setup"]
    )

    with pytest.raises(InvalidOperationError, match="same ministry"):
        set_same_date_exclusion(
            session, actor=world["head"], membership_a=world["membership_a"],
            membership_b=setup_member, scheduling_period=world["period"],
        )
    assert not _audit_rows(session)


def test_05_a_period_from_another_ministry_is_rejected(session, world, monkeypatch):
    """Kids memberships cannot be hung off a Setup period.

    The actor here is the Setup head, because authorization follows the
    *period's* ministry -- so this test is about the ministry-agreement rule
    and not about who may act.
    """
    _stub_lookup(monkeypatch, None)
    setup_period = _period(301, ministry=world["setup"], name="Setup Oct-Dec 2026")

    with pytest.raises(InvalidOperationError, match="same ministry"):
        set_same_date_exclusion(
            session, actor=world["setup_head"], membership_a=world["membership_a"],
            membership_b=world["membership_b"], scheduling_period=setup_period,
        )
    assert not _audit_rows(session)


# ==========================================================================
# 6-9 -- authorization
# ==========================================================================


def test_06_a_head_may_configure_their_own_ministry(session, world, monkeypatch):
    _stub_lookup(monkeypatch, None)

    row = set_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_a"],
        membership_b=world["membership_b"], scheduling_period=world["period"],
    )
    assert row is not None


def test_07_a_head_of_another_ministry_may_not_configure_this_one(
    session, world, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_same_date_exclusion(
            session, actor=world["setup_head"], membership_a=world["membership_a"],
            membership_b=world["membership_b"], scheduling_period=world["period"],
        )
    assert not _audit_rows(session)


def test_08_an_ordinary_volunteer_may_not_configure_the_rule(
    session, world, monkeypatch
):
    """And being one of the two linked volunteers confers nothing.

    ``linked_volunteer`` is the person behind ``membership_b`` -- one half of
    the very pair being configured -- and is refused exactly as any other
    non-manager is. Volunteer self-service is out of scope for V1.
    """
    _stub_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_same_date_exclusion(
            session, actor=world["linked_volunteer"],
            membership_a=world["membership_a"], membership_b=world["membership_b"],
            scheduling_period=world["period"],
        )
    assert not _audit_rows(session)


def test_08b_an_ordinary_volunteer_may_not_clear_the_rule_either(
    session, world, monkeypatch
):
    existing = _existing(
        800, membership_a_id=200, membership_b_id=201, period=world["period"]
    )
    _stub_lookup(monkeypatch, existing)

    with pytest.raises(AuthorizationError):
        clear_same_date_exclusion(
            session, actor=world["linked_volunteer"],
            membership_a=world["membership_a"], membership_b=world["membership_b"],
            scheduling_period=world["period"],
        )
    assert session.deleted_objects == []


def test_09_an_admin_who_heads_nothing_may_not_configure_a_ministry(
    session, world, monkeypatch
):
    """Task 80 reversed this. Linking two volunteers so they are never
    scheduled on one day is an arrangement their ministry made; recording it
    is that ministry's head's act, not a church-wide one."""
    _stub_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_same_date_exclusion(
            session, actor=world["admin"], membership_a=world["membership_a"],
            membership_b=world["membership_b"], scheduling_period=world["period"],
        )
    assert not _audit_rows(session)


def test_09b_a_deactivated_head_may_not_configure(session, world, monkeypatch):
    _stub_lookup(monkeypatch, None)
    world["head"].deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(AuthorizationError):
        set_same_date_exclusion(
            session, actor=world["head"], membership_a=world["membership_a"],
            membership_b=world["membership_b"], scheduling_period=world["period"],
        )


@pytest.mark.parametrize("which", ["membership_a", "membership_b"])
def test_09c_a_deactivated_target_cannot_be_linked(
    session, world, monkeypatch, which
):
    _stub_lookup(monkeypatch, None)
    world[which].deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)

    with pytest.raises(InvalidOperationError, match="deactivated membership"):
        set_same_date_exclusion(
            session, actor=world["head"], membership_a=world["membership_a"],
            membership_b=world["membership_b"], scheduling_period=world["period"],
        )


def test_09d_a_deactivated_person_cannot_be_linked(session, world, monkeypatch):
    _stub_lookup(monkeypatch, None)
    world["membership_b"].person.deactivated_at = datetime.datetime(
        2026, 1, 1, tzinfo=UTC
    )

    with pytest.raises(InvalidOperationError, match="deactivated person"):
        set_same_date_exclusion(
            session, actor=world["head"], membership_a=world["membership_a"],
            membership_b=world["membership_b"], scheduling_period=world["period"],
        )


# ==========================================================================
# 10-11 -- clearing
# ==========================================================================


def test_10_clearing_an_existing_rule_removes_the_row(session, world, monkeypatch):
    existing = _existing(
        800, membership_a_id=200, membership_b_id=201, period=world["period"]
    )
    _stub_lookup(monkeypatch, existing)

    result = clear_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_a"],
        membership_b=world["membership_b"], scheduling_period=world["period"],
    )

    assert result is None
    assert session.deleted_objects == [existing]


def test_10b_clearing_is_order_insensitive(session, world, monkeypatch):
    existing = _existing(
        800, membership_a_id=200, membership_b_id=201, period=world["period"]
    )
    seen = _stub_lookup(monkeypatch, existing)

    clear_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_b"],
        membership_b=world["membership_a"], scheduling_period=world["period"],
    )

    assert seen == [
        {"membership_a_id": 200, "membership_b_id": 201, "scheduling_period_id": 300}
    ]
    assert session.deleted_objects == [existing]


def test_10c_a_departed_members_rule_can_still_be_cleared(
    session, world, monkeypatch
):
    """Clearing is exempt from the activity checks ``set`` applies.

    The same asymmetry serving limits and head revocation use: a rule left
    behind by somebody who has left must always be removable.
    """
    existing = _existing(
        800, membership_a_id=200, membership_b_id=201, period=world["period"]
    )
    _stub_lookup(monkeypatch, existing)
    world["membership_b"].deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    world["membership_b"].person.deactivated_at = datetime.datetime(
        2026, 1, 1, tzinfo=UTC
    )

    clear_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_a"],
        membership_b=world["membership_b"], scheduling_period=world["period"],
    )
    assert session.deleted_objects == [existing]


def test_11_clearing_a_rule_that_does_not_exist_is_a_no_op(
    session, world, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    result = clear_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_a"],
        membership_b=world["membership_b"], scheduling_period=world["period"],
    )

    assert result is None
    assert session.deleted_objects == []
    # Nothing was removed, so nothing is recorded.
    assert not _audit_rows(session)


# ==========================================================================
# 12 -- audit: the pair and the period, and nothing personal
# ==========================================================================


#: Every word this system must never store about why two people are linked.
_FORBIDDEN_WORDS = (
    "spouse", "husband", "wife", "partner", "married", "marriage", "couple",
    "household", "family", "sibling", "housemate", "relationship", "parent",
)


def _assert_nothing_personal(row: AuditEvent) -> None:
    haystack = " ".join(
        [row.summary, row.action, str(row.before_values), str(row.after_values),
         str(row.reason)]
    ).lower()
    for word in _FORBIDDEN_WORDS:
        assert word not in haystack, f"audit row leaked {word!r}: {haystack}"


def test_12_creation_is_audited_with_the_pair_the_period_and_the_ministry(
    session, world, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    row = set_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_a"],
        membership_b=world["membership_b"], scheduling_period=world["period"],
    )

    audit = _one_audit_row(session)
    assert audit.action == ACTION_SAME_DATE_EXCLUSION_RECORDED
    assert audit.target_table == "membership_same_date_exclusion"
    assert audit.target_id == row.id
    assert audit.ministry_id == world["kids"].id
    assert audit.actor_person_id == world["head"].id
    assert audit.after_values == {
        "membership_a_id": 200,
        "membership_b_id": 201,
        "scheduling_period_id": 300,
        "ministry_id": 1,
    }
    # A creation has no before side.
    assert audit.before_values is None
    # No reason field exists on this operation at all, so none is recorded.
    assert audit.reason is None
    _assert_nothing_personal(audit)


def test_12b_clearing_is_audited_with_the_same_context(session, world, monkeypatch):
    existing = _existing(
        800, membership_a_id=200, membership_b_id=201, period=world["period"]
    )
    _stub_lookup(monkeypatch, existing)

    clear_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_a"],
        membership_b=world["membership_b"], scheduling_period=world["period"],
    )

    audit = _one_audit_row(session)
    assert audit.action == ACTION_SAME_DATE_EXCLUSION_CLEARED
    assert audit.target_id == 800
    assert audit.before_values == {
        "membership_a_id": 200,
        "membership_b_id": 201,
        "scheduling_period_id": 300,
        "ministry_id": 1,
    }
    # A deletion has no after side: "no rule" is exactly that absence.
    assert audit.after_values is None
    _assert_nothing_personal(audit)


def test_12c_the_summary_names_the_people_the_ministry_and_the_period(
    session, world, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    set_same_date_exclusion(
        session, actor=world["head"], membership_a=world["membership_a"],
        membership_b=world["membership_b"], scheduling_period=world["period"],
    )

    summary = _one_audit_row(session).summary
    assert "Volunteer A" in summary
    assert "Volunteer B" in summary
    assert KIDS in summary
    assert "Kids Oct-Dec 2026" in summary
    assert "must not both serve on the same date" in summary


def test_12d_the_service_stores_no_reason_or_relationship_field():
    """The table has no column that could hold why two people are linked."""
    columns = set(Base.metadata.tables["membership_same_date_exclusion"].columns.keys())
    assert columns == {
        "id", "membership_a_id", "membership_b_id", "scheduling_period_id",
        "ministry_id", "created_at", "updated_at",
    }
    for word in _FORBIDDEN_WORDS + ("reason", "note", "type", "kind", "strength"):
        assert not any(word in column for column in columns), word


def test_12e_the_set_and_clear_signatures_accept_no_reason():
    import inspect

    for function in (set_same_date_exclusion, clear_same_date_exclusion):
        parameters = set(inspect.signature(function).parameters)
        assert "reason" not in parameters
        assert parameters == {
            "session", "actor", "membership_a", "membership_b", "scheduling_period"
        }


# ==========================================================================
# Queries and reads
# ==========================================================================


def _compile(stmt) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_the_single_row_lookup_is_scoped_to_the_canonical_pair_and_period():
    sql = _compile(_exclusion_statement(200, 201, 300))
    assert "membership_same_date_exclusion.membership_a_id = 200" in sql
    assert "membership_same_date_exclusion.membership_b_id = 201" in sql
    assert "membership_same_date_exclusion.scheduling_period_id = 300" in sql


def test_the_period_lookup_is_scoped_to_one_period_and_ordered():
    sql = _compile(_period_exclusions_statement(300))
    assert "membership_same_date_exclusion.scheduling_period_id = 300" in sql
    assert "ORDER BY" in sql


def test_the_per_member_lookup_asks_both_sides_of_the_pair():
    """A membership may be stored as either half, so the predicate is an OR."""
    sql = _compile(_member_exclusions_statement(201, 300))
    assert "membership_a_id = 201" in sql
    assert "membership_b_id = 201" in sql
    assert " OR " in sql
    assert "scheduling_period_id = 300" in sql


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return iter(self._rows)


def test_get_same_date_exclusion_pairs_returns_canonical_sorted_tuples(
    world, monkeypatch
):
    rows = [
        _existing(801, membership_a_id=210, membership_b_id=211, period=world["period"]),
        _existing(800, membership_a_id=200, membership_b_id=201, period=world["period"]),
    ]
    session = type("S", (), {"execute": staticmethod(lambda stmt: _Rows(rows))})()

    assert get_same_date_exclusion_pairs(session, scheduling_period_id=300) == (
        (200, 201),
        (210, 211),
    )


@pytest.mark.parametrize(
    "membership_id, expected", [(200, {201}), (201, {200}), (999, set())]
)
def test_get_linked_membership_ids_answers_from_either_side(
    world, membership_id, expected
):
    rows = [
        _existing(800, membership_a_id=200, membership_b_id=201, period=world["period"])
    ]
    session = type("S", (), {"execute": staticmethod(lambda stmt: _Rows(rows))})()

    assert get_linked_membership_ids(
        session, ministry_membership_id=membership_id, scheduling_period_id=300
    ) == frozenset(expected)


def test_the_blocker_code_is_not_in_the_overridable_catalogue():
    """The rule is non-overridable, so its code must never be admitted to the
    bounded set an ``override_reason`` may bypass.
    """
    from app.services.assignment_policy import (
        BLOCKER_DESCRIPTIONS,
        OVERRIDABLE_BLOCKERS,
    )

    assert SAME_DATE_LINKED_MEMBER_CONFLICT not in OVERRIDABLE_BLOCKERS
    assert SAME_DATE_LINKED_MEMBER_CONFLICT not in BLOCKER_DESCRIPTIONS


def test_the_code_names_a_linked_member_and_not_a_relationship():
    lowered = SAME_DATE_LINKED_MEMBER_CONFLICT.lower()
    assert "linked" in lowered
    for word in _FORBIDDEN_WORDS:
        assert word not in lowered
