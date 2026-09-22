"""Member groups and their per-event caps: the service (Task 74).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows :mod:`tests.test_services_same_date_exclusion`
exactly, and for the same reasons: this service flushes once (to obtain a new
row's identity for its audit row), looks rows up through ``session.execute``,
and deletes on the clearing paths. So the lookups' SQL is compiled and
inspected with no session at all; each lookup's *use* is monkeypatched per
test; and the flush and delete run against a real, unbound ``Session`` subclass
that simulates identity assignment and records deletions.

Every person, ministry, period and group here is synthetic. The groups are
named for nothing in particular, because the rule is generic: nothing in the
application knows or may know what a category means.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import (
    MemberGroup,
    MemberGroupEventLimit,
    MemberGroupMember,
    SchedulingPeriod,
)
from app.services import AuthorizationError, InvalidOperationError
from app.services.audit import (
    ACTION_MEMBER_GROUP_CREATED,
    ACTION_MEMBER_GROUP_LIMIT_CHANGED,
    ACTION_MEMBER_GROUP_LIMIT_CLEARED,
    ACTION_MEMBER_GROUP_LIMIT_RECORDED,
    ACTION_MEMBER_GROUP_MEMBER_ADDED,
    ACTION_MEMBER_GROUP_MEMBER_REMOVED,
)
from app.services.member_group import (
    MEMBER_GROUP_EVENT_LIMIT_CONFLICT,
    MemberGroupCapConfig,
    _event_limit_statement,
    _group_by_name_statement,
    _group_member_statement,
    _group_members_statement,
    _ministry_groups_statement,
    _period_caps_statement,
    _period_group_limits_statement,
    count_group_members_present,
    create_member_group,
    describe_member_group_cap,
    list_member_groups,
    list_period_member_group_limits,
    load_member_group_caps,
    set_member_group_event_limit,
    set_member_group_membership,
)

GROUP_NAME = "Category A"
UTC = datetime.timezone.utc


class GroupSession(Session):
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
def session() -> GroupSession:
    return GroupSession()


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
            name: str = "Oct-Dec 2026") -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=name,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    period.ministry = ministry
    return period


def _group(group_id: int, *, ministry: Ministry, name: str = GROUP_NAME) -> MemberGroup:
    group = MemberGroup(ministry_id=ministry.id, name=name)
    group.id = group_id
    group.ministry = ministry
    return group


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _compile(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.fixture
def world():
    """One ministry, an Admin, its head, another ministry's head, two members."""
    setup = _ministry(1, "Ministry One")
    other = _ministry(2, "Ministry Two")
    admin = _person(10, "Synthetic Admin", is_admin=True)
    head = _person(11, "Synthetic Head")
    _actor_membership(100, person=head, ministry=setup, is_head=True)
    other_head = _person(12, "Synthetic Other Head")
    _actor_membership(101, person=other_head, ministry=other, is_head=True)
    member_person = _person(13, "Volunteer A")
    second_person = _person(14, "Volunteer B")
    membership = _target_membership(200, person=member_person, ministry=setup)
    second = _target_membership(201, person=second_person, ministry=setup)
    # Being in a group confers no authority, so A is an ordinary member with no
    # head flag anywhere.
    _actor_membership(200, person=member_person, ministry=setup)
    foreign_person = _person(15, "Volunteer C")
    foreign = _target_membership(202, person=foreign_person, ministry=other)
    return {
        "setup": setup, "other": other, "admin": admin, "head": head,
        "other_head": other_head, "membership": membership, "second": second,
        "foreign": foreign, "period": _period(300, ministry=setup),
        "group": _group(400, ministry=setup),
        "grouped_volunteer": member_person,
    }


def _stub_group_lookup(monkeypatch, existing) -> list[dict]:
    import app.services.member_group as module

    seen: list[dict] = []

    def fake(session, *, ministry_id, name):
        seen.append({"ministry_id": ministry_id, "name": name})
        return existing

    monkeypatch.setattr(module, "_find_group_by_name", fake)
    return seen


def _stub_member_lookup(monkeypatch, existing) -> list[dict]:
    import app.services.member_group as module

    seen: list[dict] = []

    def fake(session, *, member_group_id, ministry_membership_id):
        seen.append(
            {
                "member_group_id": member_group_id,
                "ministry_membership_id": ministry_membership_id,
            }
        )
        return existing

    monkeypatch.setattr(module, "_find_group_member", fake)
    return seen


def _stub_limit_lookup(monkeypatch, existing) -> list[dict]:
    import app.services.member_group as module

    seen: list[dict] = []

    def fake(session, *, member_group_id, scheduling_period_id):
        seen.append(
            {
                "member_group_id": member_group_id,
                "scheduling_period_id": scheduling_period_id,
            }
        )
        return existing

    monkeypatch.setattr(module, "_find_event_limit", fake)
    return seen


# ==========================================================================
# Creating a group
# ==========================================================================


def test_an_admin_who_heads_nothing_may_not_create_a_group(session, world, monkeypatch):
    """Task 80 reversed this. A member group is a category a ministry defines
    about its own people; defining one is running the ministry, which an
    overseer does not do on its head's behalf."""
    _stub_group_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        create_member_group(
            session, actor=world["admin"], ministry=world["setup"], name=GROUP_NAME
        )

    assert _audit_rows(session) == []


def test_the_ministrys_own_head_may_create_a_group(session, world, monkeypatch):
    _stub_group_lookup(monkeypatch, None)

    create_member_group(
        session, actor=world["head"], ministry=world["setup"], name=GROUP_NAME
    )

    assert _one_audit_row(session).ministry_id == world["setup"].id


def test_another_ministrys_head_may_not(session, world, monkeypatch):
    _stub_group_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        create_member_group(
            session, actor=world["other_head"], ministry=world["setup"],
            name=GROUP_NAME,
        )

    assert _audit_rows(session) == []


def test_being_in_a_group_confers_no_authority(session, world, monkeypatch):
    """The volunteer in the group is an ordinary member of the ministry."""
    _stub_group_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        create_member_group(
            session, actor=world["grouped_volunteer"], ministry=world["setup"],
            name=GROUP_NAME,
        )


def test_creating_a_group_that_exists_is_an_unaudited_no_op(session, world, monkeypatch):
    existing = world["group"]
    _stub_group_lookup(monkeypatch, existing)

    result = create_member_group(
        session, actor=world["head"], ministry=world["setup"], name=GROUP_NAME
    )

    assert result is existing
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_a_blank_group_name_is_refused(session, world, monkeypatch):
    _stub_group_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="must not be blank"):
        create_member_group(
            session, actor=world["head"], ministry=world["setup"], name="   "
        )


def test_the_name_lookup_is_case_insensitive_and_ministry_scoped():
    sql = _compile(_group_by_name_statement(1, "Category A"))

    assert "lower(member_group.name) = 'category a'" in sql
    assert "member_group.ministry_id = 1" in sql


# ==========================================================================
# Group membership
# ==========================================================================


def test_adding_a_member_records_the_row_and_audits_it(session, world, monkeypatch):
    _stub_member_lookup(monkeypatch, None)

    row = set_member_group_membership(
        session, actor=world["head"], member_group=world["group"],
        membership=world["membership"], is_member=True,
    )

    assert isinstance(row, MemberGroupMember)
    assert row.member_group_id == world["group"].id
    assert row.ministry_membership_id == world["membership"].id
    # Shared by both composite foreign keys; taken from the group.
    assert row.ministry_id == world["setup"].id
    audit = _one_audit_row(session)
    assert audit.action == ACTION_MEMBER_GROUP_MEMBER_ADDED
    assert audit.after_values["ministry_membership_id"] == world["membership"].id


def test_adding_somebody_already_in_the_group_is_an_unaudited_no_op(
    session, world, monkeypatch
):
    existing = MemberGroupMember(
        member_group_id=world["group"].id,
        ministry_membership_id=world["membership"].id,
        ministry_id=world["setup"].id,
    )
    existing.id = 555
    _stub_member_lookup(monkeypatch, existing)

    result = set_member_group_membership(
        session, actor=world["head"], member_group=world["group"],
        membership=world["membership"], is_member=True,
    )

    assert result is existing
    assert _audit_rows(session) == []
    assert session.flush_calls == 0


def test_removing_a_member_deletes_the_row_and_audits_it(session, world, monkeypatch):
    existing = MemberGroupMember(
        member_group_id=world["group"].id,
        ministry_membership_id=world["membership"].id,
        ministry_id=world["setup"].id,
    )
    existing.id = 555
    _stub_member_lookup(monkeypatch, existing)

    result = set_member_group_membership(
        session, actor=world["head"], member_group=world["group"],
        membership=world["membership"], is_member=False,
    )

    assert result is None
    assert session.deleted_objects == [existing]
    audit = _one_audit_row(session)
    assert audit.action == ACTION_MEMBER_GROUP_MEMBER_REMOVED
    assert audit.before_values["ministry_membership_id"] == world["membership"].id
    # "Not in this group" is the absence, never an after-state to record.
    assert audit.after_values is None


def test_removing_somebody_who_is_not_in_the_group_is_a_pure_no_op(
    session, world, monkeypatch
):
    _stub_member_lookup(monkeypatch, None)

    set_member_group_membership(
        session, actor=world["head"], member_group=world["group"],
        membership=world["membership"], is_member=False,
    )

    assert session.deleted_objects == []
    assert _audit_rows(session) == []


def test_a_deactivated_membership_may_not_be_added(session, world, monkeypatch):
    _stub_member_lookup(monkeypatch, None)
    departed = _target_membership(
        210, person=_person(20, "Departed"), ministry=world["setup"],
        deactivated=True,
    )

    with pytest.raises(InvalidOperationError, match="deactivated membership"):
        set_member_group_membership(
            session, actor=world["head"], member_group=world["group"],
            membership=departed, is_member=True,
        )


def test_a_departed_members_stray_group_row_may_still_be_removed(
    session, world, monkeypatch
):
    """The removal asymmetry: clearing is exempt from the activity checks."""
    departed = _target_membership(
        210, person=_person(20, "Departed"), ministry=world["setup"],
        deactivated=True,
    )
    existing = MemberGroupMember(
        member_group_id=world["group"].id,
        ministry_membership_id=departed.id,
        ministry_id=world["setup"].id,
    )
    existing.id = 556
    _stub_member_lookup(monkeypatch, existing)

    set_member_group_membership(
        session, actor=world["head"], member_group=world["group"],
        membership=departed, is_member=False,
    )

    assert session.deleted_objects == [existing]


def test_a_membership_from_another_ministry_may_not_join_the_group(
    session, world, monkeypatch
):
    _stub_member_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="same ministry"):
        set_member_group_membership(
            session, actor=world["head"], member_group=world["group"],
            membership=world["foreign"], is_member=True,
        )


def test_another_ministrys_head_may_not_change_group_membership(
    session, world, monkeypatch
):
    _stub_member_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_member_group_membership(
            session, actor=world["other_head"], member_group=world["group"],
            membership=world["membership"], is_member=True,
        )


# ==========================================================================
# The per-event cap
# ==========================================================================


def test_recording_a_cap_writes_the_row_and_audits_it(session, world, monkeypatch):
    _stub_limit_lookup(monkeypatch, None)

    limit = set_member_group_event_limit(
        session, actor=world["head"], member_group=world["group"],
        scheduling_period=world["period"], max_per_event=2,
    )

    assert isinstance(limit, MemberGroupEventLimit)
    assert limit.max_per_event == 2
    assert limit.ministry_id == world["setup"].id
    audit = _one_audit_row(session)
    assert audit.action == ACTION_MEMBER_GROUP_LIMIT_RECORDED
    assert audit.after_values["max_per_event"] == 2
    assert audit.before_values is None


def test_changing_a_cap_records_both_sides(session, world, monkeypatch):
    existing = MemberGroupEventLimit(
        member_group_id=world["group"].id,
        scheduling_period_id=world["period"].id,
        ministry_id=world["setup"].id,
        max_per_event=3,
    )
    existing.id = 777
    _stub_limit_lookup(monkeypatch, existing)

    set_member_group_event_limit(
        session, actor=world["head"], member_group=world["group"],
        scheduling_period=world["period"], max_per_event=2,
    )

    assert existing.max_per_event == 2
    audit = _one_audit_row(session)
    assert audit.action == ACTION_MEMBER_GROUP_LIMIT_CHANGED
    assert audit.before_values["max_per_event"] == 3
    assert audit.after_values["max_per_event"] == 2


def test_setting_the_cap_it_already_has_is_an_unaudited_no_op(
    session, world, monkeypatch
):
    existing = MemberGroupEventLimit(
        member_group_id=world["group"].id,
        scheduling_period_id=world["period"].id,
        ministry_id=world["setup"].id,
        max_per_event=2,
    )
    existing.id = 777
    _stub_limit_lookup(monkeypatch, existing)

    result = set_member_group_event_limit(
        session, actor=world["head"], member_group=world["group"],
        scheduling_period=world["period"], max_per_event=2,
    )

    assert result is existing
    assert _audit_rows(session) == []


def test_clearing_a_cap_deletes_the_row_and_audits_it(session, world, monkeypatch):
    existing = MemberGroupEventLimit(
        member_group_id=world["group"].id,
        scheduling_period_id=world["period"].id,
        ministry_id=world["setup"].id,
        max_per_event=2,
    )
    existing.id = 777
    _stub_limit_lookup(monkeypatch, existing)

    result = set_member_group_event_limit(
        session, actor=world["head"], member_group=world["group"],
        scheduling_period=world["period"], max_per_event=None,
    )

    assert result is None
    assert session.deleted_objects == [existing]
    audit = _one_audit_row(session)
    assert audit.action == ACTION_MEMBER_GROUP_LIMIT_CLEARED
    assert audit.before_values["max_per_event"] == 2
    assert audit.after_values is None


def test_clearing_an_absent_cap_is_a_pure_no_op(session, world, monkeypatch):
    _stub_limit_lookup(monkeypatch, None)

    result = set_member_group_event_limit(
        session, actor=world["head"], member_group=world["group"],
        scheduling_period=world["period"], max_per_event=None,
    )

    assert result is None
    assert session.deleted_objects == []
    assert _audit_rows(session) == []


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_cap_is_refused(session, world, monkeypatch, value):
    _stub_limit_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="must be positive"):
        set_member_group_event_limit(
            session, actor=world["head"], member_group=world["group"],
            scheduling_period=world["period"], max_per_event=value,
        )


def test_a_boolean_cap_is_refused_rather_than_read_as_one(session, world, monkeypatch):
    _stub_limit_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="integer or None"):
        set_member_group_event_limit(
            session, actor=world["head"], member_group=world["group"],
            scheduling_period=world["period"], max_per_event=True,
        )


def test_a_group_and_period_from_different_ministries_are_refused(
    session, world, monkeypatch
):
    _stub_limit_lookup(monkeypatch, None)
    foreign_period = _period(301, ministry=world["other"])

    with pytest.raises(AuthorizationError):
        # The head of the first ministry cannot manage the second's period at
        # all, so authorization refuses before the ministry mismatch is reached.
        set_member_group_event_limit(
            session, actor=world["head"], member_group=world["group"],
            scheduling_period=foreign_period, max_per_event=2,
        )

    # And the mismatch rule itself, reached by an actor who *is* authorized
    # for the foreign period -- its own head, capping a group that is not
    # theirs.
    with pytest.raises(InvalidOperationError, match="same ministry"):
        set_member_group_event_limit(
            session, actor=world["other_head"], member_group=world["group"],
            scheduling_period=foreign_period, max_per_event=2,
        )


def test_another_ministrys_head_may_not_set_a_cap(session, world, monkeypatch):
    _stub_limit_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_member_group_event_limit(
            session, actor=world["other_head"], member_group=world["group"],
            scheduling_period=world["period"], max_per_event=2,
        )

    assert _audit_rows(session) == []


def test_a_cap_below_what_the_draft_contains_is_allowed_and_repairs_nothing(
    session, world, monkeypatch
):
    """Configuring a stricter rule after a draft exists never deletes a row.

    The service has no way to reach an assignment at all, which is the
    structural form of the guarantee -- asserted here so a future edit that
    gave it one would fail.
    """
    existing = MemberGroupEventLimit(
        member_group_id=world["group"].id,
        scheduling_period_id=world["period"].id,
        ministry_id=world["setup"].id,
        max_per_event=5,
    )
    existing.id = 777
    _stub_limit_lookup(monkeypatch, existing)

    set_member_group_event_limit(
        session, actor=world["head"], member_group=world["group"],
        scheduling_period=world["period"], max_per_event=1,
    )

    assert session.deleted_objects == []


# ==========================================================================
# The rule's own arithmetic and wording
# ==========================================================================


def _cap(max_per_event: int, members) -> MemberGroupCapConfig:
    return MemberGroupCapConfig(
        member_group_id=400, member_group_name=GROUP_NAME,
        max_per_event=max_per_event, member_membership_ids=frozenset(members),
    )


def test_counting_group_members_present_counts_people_not_rows():
    cap = _cap(2, (1, 2, 3))

    assert count_group_members_present(cap, {1, 2, 9}) == 2
    assert count_group_members_present(cap, set()) == 0
    assert count_group_members_present(cap, {9}) == 0


def test_the_cap_wording_is_written_once_and_reads_naturally():
    assert describe_member_group_cap(GROUP_NAME, 1) == (
        f"at most one member of {GROUP_NAME} may serve any one event"
    )
    assert describe_member_group_cap(GROUP_NAME, 3) == (
        f"at most 3 members of {GROUP_NAME} may serve any one event"
    )


def test_the_conflict_code_is_not_an_overridable_blocker():
    """Adding it to that catalogue would make the rule bypassable by
    definition, which is the one thing this rule must never be.
    """
    from app.services.assignment_policy import OVERRIDABLE_BLOCKERS

    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT not in OVERRIDABLE_BLOCKERS


# ==========================================================================
# The reads
# ==========================================================================


def test_loading_caps_costs_one_query_when_no_cap_is_configured():
    class _Recording:
        def __init__(self) -> None:
            self.statements: list = []

        def execute(self, statement):
            self.statements.append(statement)
            return _EmptyResult()

    class _EmptyResult:
        def all(self):
            return []

    session = _Recording()

    assert load_member_group_caps(session, scheduling_period_id=300) == ()
    assert len(session.statements) == 1


def test_loading_caps_costs_two_queries_however_many_groups_are_capped():
    from types import SimpleNamespace

    class _Recording:
        def __init__(self) -> None:
            self.statements: list = []

        def execute(self, statement):
            self.statements.append(statement)
            if len(self.statements) == 1:
                return _Rows(
                    [
                        SimpleNamespace(member_group_id=g, max_per_event=2,
                                        name=f"Group {g}")
                        for g in (400, 401, 402)
                    ]
                )
            return _Rows(
                [
                    SimpleNamespace(member_group_id=400, ministry_membership_id=1),
                    SimpleNamespace(member_group_id=401, ministry_membership_id=2),
                    SimpleNamespace(member_group_id=402, ministry_membership_id=3),
                ]
            )

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    session = _Recording()
    caps = load_member_group_caps(session, scheduling_period_id=300)

    assert len(session.statements) == 2
    assert [cap.member_group_id for cap in caps] == [400, 401, 402]
    assert caps[0].member_membership_ids == frozenset({1})


def test_the_cap_query_is_scoped_to_one_period_and_joins_the_group_for_its_name():
    sql = _compile(_period_caps_statement(300))

    assert "member_group_event_limit.scheduling_period_id = 300" in sql
    assert "JOIN member_group" in sql
    assert "member_group.name" in sql
    assert "ORDER BY member_group_event_limit.member_group_id" in sql


def test_the_member_query_uses_in_rather_than_one_query_per_group():
    sql = _compile(_group_members_statement([400, 401]))

    assert "IN (400, 401)" in sql


def test_the_period_listing_left_joins_so_an_uncapped_group_still_appears():
    sql = _compile(_period_group_limits_statement(300, 1))

    assert "LEFT OUTER JOIN member_group_event_limit" in sql
    assert "member_group.ministry_id = 1" in sql
    assert "count(member_group_member.id)" in sql


def test_the_ministry_listing_is_ordered_by_lowered_name():
    sql = _compile(_ministry_groups_statement(1))

    assert "ORDER BY lower(member_group.name), member_group.id" in sql


def test_the_single_row_lookups_are_scoped_exactly():
    member_sql = _compile(_group_member_statement(400, 200))
    limit_sql = _compile(_event_limit_statement(400, 300))

    assert "member_group_member.member_group_id = 400" in member_sql
    assert "member_group_member.ministry_membership_id = 200" in member_sql
    assert "member_group_event_limit.member_group_id = 400" in limit_sql
    assert "member_group_event_limit.scheduling_period_id = 300" in limit_sql


def test_listing_groups_requires_authority(session, world, monkeypatch):
    import app.services.member_group as module

    monkeypatch.setattr(
        module, "_ministry_groups_statement", lambda ministry_id: None
    )

    with pytest.raises(AuthorizationError):
        list_member_groups(
            session, actor=world["other_head"], ministry=world["setup"]
        )


def test_listing_period_limits_requires_authority(session, world):
    with pytest.raises(AuthorizationError):
        list_period_member_group_limits(
            session, actor=world["other_head"], scheduling_period=world["period"]
        )


def test_nothing_in_this_service_stores_why_a_category_exists():
    """The model carries a name and nothing else: no description, no meaning,
    no eligibility semantics. A column added later would be a privacy cost with
    no benefit, so its absence is asserted rather than assumed.
    """
    columns = set(MemberGroup.__table__.columns.keys())

    assert columns == {"id", "ministry_id", "name", "created_at", "updated_at"}
