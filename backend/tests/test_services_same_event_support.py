"""The same-event support requirement: the service (Task 74).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows :mod:`tests.test_services_same_date_exclusion`
exactly, and for the same reasons: this service flushes once (to obtain a new
requirement's identity for its supporter rows and its audit row), looks rows up
through ``session.execute``, and deletes on the replace and clear paths. So the
lookups' SQL is compiled and inspected with no session at all; each lookup's
*use* is monkeypatched per test; and the flush and delete run against a real,
unbound ``Session`` subclass that simulates identity assignment and records
deletions.

Every person, ministry and period here is synthetic. **Nothing in this file
describes a real arrangement of any kind** -- the service stores the condition
and the approved set, never the circumstance behind them, and neither do its
tests.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import (
    MembershipSupportRequirement,
    MembershipSupportSupporter,
    SchedulingPeriod,
)
from app.services import AuthorizationError, InvalidOperationError
from app.services.audit import (
    ACTION_SUPPORT_REQUIREMENT_CHANGED,
    ACTION_SUPPORT_REQUIREMENT_CLEARED,
    ACTION_SUPPORT_REQUIREMENT_RECORDED,
)
from app.services.same_event_support import (
    SAME_EVENT_SUPPORT_CONFLICT,
    SupportRequirementConfig,
    _period_requirements_statement,
    _requirement_statement,
    _supporters_statement,
    clear_same_event_support_requirement,
    count_supporters_present,
    describe_support_requirement,
    list_support_requirements,
    load_support_requirements,
    set_same_event_support_requirement,
)

UTC = datetime.timezone.utc


class SupportSession(Session):
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
def session() -> SupportSession:
    return SupportSession()


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


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _supporter_rows(session: Session) -> list[MembershipSupportSupporter]:
    return [
        obj for obj in session.new if isinstance(obj, MembershipSupportSupporter)
    ]


def _compile(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


@pytest.fixture
def world():
    """One ministry, an Admin, its head, another ministry's head, four members."""
    setup = _ministry(1, "Ministry One")
    other = _ministry(2, "Ministry Two")
    admin = _person(10, "Synthetic Admin", is_admin=True)
    head = _person(11, "Synthetic Head")
    _actor_membership(100, person=head, ministry=setup, is_head=True)
    other_head = _person(12, "Synthetic Other Head")
    _actor_membership(101, person=other_head, ministry=other, is_head=True)

    subject_person = _person(13, "Volunteer A")
    subject = _target_membership(200, person=subject_person, ministry=setup)
    # Being the subject of the rule confers no authority over it.
    _actor_membership(200, person=subject_person, ministry=setup)

    supporter_a = _target_membership(201, person=_person(14, "Volunteer B"),
                                     ministry=setup)
    supporter_b = _target_membership(202, person=_person(15, "Volunteer C"),
                                     ministry=setup)
    foreign = _target_membership(203, person=_person(16, "Volunteer D"),
                                 ministry=other)
    return {
        "setup": setup, "other": other, "admin": admin, "head": head,
        "other_head": other_head, "subject": subject, "subject_person": subject_person,
        "supporter_a": supporter_a, "supporter_b": supporter_b,
        "foreign": foreign, "period": _period(300, ministry=setup),
    }


def _stub_requirement_lookup(monkeypatch, existing) -> list[dict]:
    import app.services.same_event_support as module

    seen: list[dict] = []

    def fake(session, *, subject_membership_id, scheduling_period_id):
        seen.append(
            {
                "subject_membership_id": subject_membership_id,
                "scheduling_period_id": scheduling_period_id,
            }
        )
        return existing

    monkeypatch.setattr(module, "_find_requirement", fake)
    return seen


def _existing_requirement(world, *, min_supporters: int = 1, row_id: int = 555):
    requirement = MembershipSupportRequirement(
        subject_membership_id=world["subject"].id,
        scheduling_period_id=world["period"].id,
        ministry_id=world["setup"].id,
        min_supporters=min_supporters,
    )
    requirement.id = row_id
    return requirement


# ==========================================================================
# Recording a requirement
# ==========================================================================


def test_an_admin_who_heads_nothing_may_not_record_a_requirement(
    session, world, monkeypatch
):
    """Task 80 reversed this. Who a member may serve alongside is a standing
    arrangement of their own ministry, and recording it is operating that
    ministry."""
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_same_event_support_requirement(
            session, actor=world["admin"], subject_membership=world["subject"],
            scheduling_period=world["period"],
            supporter_memberships=[world["supporter_a"]],
        )
    assert not _audit_rows(session)


def test_the_ministrys_own_head_may_record_a_requirement(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    requirement = set_same_event_support_requirement(
        session, actor=world["head"], subject_membership=world["subject"],
        scheduling_period=world["period"],
        supporter_memberships=[world["supporter_a"]],
    )

    assert isinstance(requirement, MembershipSupportRequirement)
    assert requirement.min_supporters == 1
    assert requirement.ministry_id == world["setup"].id
    supporters = _supporter_rows(session)
    assert [row.supporter_membership_id for row in supporters] == [
        world["supporter_a"].id
    ]
    # The subject is copied onto the supporter row so the database's own
    # "a supporter is not the subject" CHECK has both ids to compare.
    assert supporters[0].subject_membership_id == world["subject"].id
    audit = _one_audit_row(session)
    assert audit.action == ACTION_SUPPORT_REQUIREMENT_RECORDED
    assert audit.after_values["supporter_membership_ids"] == [
        world["supporter_a"].id
    ]


def test_the_ministrys_own_head_may_record_one(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    set_same_event_support_requirement(
        session, actor=world["head"], subject_membership=world["subject"],
        scheduling_period=world["period"],
        supporter_memberships=[world["supporter_a"]],
    )

    assert _one_audit_row(session).ministry_id == world["setup"].id


def test_another_ministrys_head_may_not(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_same_event_support_requirement(
            session, actor=world["other_head"], subject_membership=world["subject"],
            scheduling_period=world["period"],
            supporter_memberships=[world["supporter_a"]],
        )

    assert _audit_rows(session) == []


def test_being_the_subject_confers_no_authority(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_same_event_support_requirement(
            session, actor=world["subject_person"],
            subject_membership=world["subject"],
            scheduling_period=world["period"],
            supporter_memberships=[world["supporter_a"]],
        )


def test_a_requirement_for_two_needs_two_approved_supporters(
    session, world, monkeypatch
):
    _stub_requirement_lookup(monkeypatch, None)

    requirement = set_same_event_support_requirement(
        session, actor=world["head"], subject_membership=world["subject"],
        scheduling_period=world["period"],
        supporter_memberships=[world["supporter_a"], world["supporter_b"]],
        min_supporters=2,
    )

    assert requirement.min_supporters == 2
    assert len(_supporter_rows(session)) == 2


def test_a_requirement_nobody_could_satisfy_is_refused(session, world, monkeypatch):
    """Fewer approved supporters than the count would mean the subject could
    never be scheduled -- a configuration mistake, not a stricter rule.
    """
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="could never be scheduled"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=world["subject"],
            scheduling_period=world["period"],
            supporter_memberships=[world["supporter_a"]],
            min_supporters=2,
        )


def test_an_empty_supporter_set_is_refused(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="could never be scheduled"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=world["subject"],
            scheduling_period=world["period"], supporter_memberships=[],
        )


def test_the_subject_may_not_be_their_own_supporter(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="its own supporter"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=world["subject"],
            scheduling_period=world["period"],
            supporter_memberships=[world["subject"], world["supporter_a"]],
        )


def test_a_supporter_from_another_ministry_is_refused(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="same ministry"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=world["subject"],
            scheduling_period=world["period"],
            supporter_memberships=[world["foreign"]],
        )


def test_a_subject_from_another_ministry_is_refused(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="same ministry"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=world["foreign"],
            scheduling_period=world["period"],
            supporter_memberships=[world["supporter_a"]],
        )


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_count_is_refused(session, world, monkeypatch, value):
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="must be positive"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=world["subject"],
            scheduling_period=world["period"],
            supporter_memberships=[world["supporter_a"]],
            min_supporters=value,
        )


def test_a_boolean_count_is_refused_rather_than_read_as_one(
    session, world, monkeypatch
):
    _stub_requirement_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="must be an integer"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=world["subject"],
            scheduling_period=world["period"],
            supporter_memberships=[world["supporter_a"]],
            min_supporters=True,
        )


def test_a_deactivated_subject_may_not_be_given_a_requirement(
    session, world, monkeypatch
):
    _stub_requirement_lookup(monkeypatch, None)
    departed = _target_membership(
        210, person=_person(20, "Departed"), ministry=world["setup"],
        deactivated=True,
    )

    with pytest.raises(InvalidOperationError, match="deactivated membership"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=departed,
            scheduling_period=world["period"],
            supporter_memberships=[world["supporter_a"]],
        )


def test_a_deactivated_supporter_may_not_be_approved(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)
    departed = _target_membership(
        211, person=_person(21, "Departed"), ministry=world["setup"],
        deactivated=True,
    )

    with pytest.raises(InvalidOperationError, match="deactivated membership"):
        set_same_event_support_requirement(
            session, actor=world["head"], subject_membership=world["subject"],
            scheduling_period=world["period"], supporter_memberships=[departed],
        )


def test_duplicates_in_the_supplied_set_are_collapsed(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    set_same_event_support_requirement(
        session, actor=world["head"], subject_membership=world["subject"],
        scheduling_period=world["period"],
        supporter_memberships=[world["supporter_a"], world["supporter_a"]],
    )

    assert len(_supporter_rows(session)) == 1


def test_nothing_about_why_the_support_is_needed_is_stored(session, world, monkeypatch):
    """The model carries ids and a count. A reason column added later would be
    a privacy cost with no benefit, so its absence is asserted rather than
    assumed (requirements §4.7).
    """
    requirement_columns = set(
        MembershipSupportRequirement.__table__.columns.keys()
    )
    supporter_columns = set(MembershipSupportSupporter.__table__.columns.keys())

    assert requirement_columns == {
        "id", "subject_membership_id", "scheduling_period_id", "ministry_id",
        "min_supporters", "created_at", "updated_at",
    }
    assert supporter_columns == {
        "id", "support_requirement_id", "subject_membership_id",
        "supporter_membership_id", "ministry_id", "created_at", "updated_at",
    }
    for columns in (requirement_columns, supporter_columns):
        for forbidden in ("reason", "notes", "relationship", "category", "kind"):
            assert forbidden not in columns


# ==========================================================================
# Revising a requirement
# ==========================================================================


def test_revising_replaces_the_approved_set_wholesale(session, world, monkeypatch):
    """A merge would leave somebody approved who was meant to be removed --
    silently, and only discoverable by reading the audit trail.
    """
    existing = _existing_requirement(world)
    _stub_requirement_lookup(monkeypatch, existing)
    old_row = MembershipSupportSupporter(
        support_requirement_id=existing.id,
        subject_membership_id=world["subject"].id,
        supporter_membership_id=world["supporter_a"].id,
        ministry_id=world["setup"].id,
    )
    old_row.id = 601
    monkeypatch.setattr(
        SupportSession, "execute", lambda self, stmt: _FakeResult([old_row])
    )

    set_same_event_support_requirement(
        session, actor=world["head"], subject_membership=world["subject"],
        scheduling_period=world["period"],
        supporter_memberships=[world["supporter_b"]],
    )

    assert old_row in session.deleted_objects
    assert [row.supporter_membership_id for row in _supporter_rows(session)] == [
        world["supporter_b"].id
    ]
    audit = _one_audit_row(session)
    assert audit.action == ACTION_SUPPORT_REQUIREMENT_CHANGED
    assert audit.before_values["supporter_membership_ids"] == [
        world["supporter_a"].id
    ]
    assert audit.after_values["supporter_membership_ids"] == [
        world["supporter_b"].id
    ]


def test_setting_the_requirement_it_already_has_is_an_unaudited_no_op(
    session, world, monkeypatch
):
    existing = _existing_requirement(world)
    _stub_requirement_lookup(monkeypatch, existing)
    old_row = MembershipSupportSupporter(
        support_requirement_id=existing.id,
        subject_membership_id=world["subject"].id,
        supporter_membership_id=world["supporter_a"].id,
        ministry_id=world["setup"].id,
    )
    old_row.id = 601
    monkeypatch.setattr(
        SupportSession, "execute", lambda self, stmt: _FakeResult([old_row])
    )

    result = set_same_event_support_requirement(
        session, actor=world["head"], subject_membership=world["subject"],
        scheduling_period=world["period"],
        supporter_memberships=[world["supporter_a"]],
    )

    assert result is existing
    assert _audit_rows(session) == []
    assert session.deleted_objects == []
    assert session.flush_calls == 0


class _FakeResult:
    """Answers both supporter reads: ``.all()`` yields id-bearing rows and
    ``.scalars()`` yields the ORM rows themselves."""

    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows

    def scalars(self):
        return iter(self._rows)


# ==========================================================================
# Clearing a requirement
# ==========================================================================


def test_clearing_deletes_the_supporters_and_the_requirement(
    session, world, monkeypatch
):
    existing = _existing_requirement(world)
    _stub_requirement_lookup(monkeypatch, existing)
    old_row = MembershipSupportSupporter(
        support_requirement_id=existing.id,
        subject_membership_id=world["subject"].id,
        supporter_membership_id=world["supporter_a"].id,
        ministry_id=world["setup"].id,
    )
    old_row.id = 601
    monkeypatch.setattr(
        SupportSession, "execute", lambda self, stmt: _FakeResult([old_row])
    )

    clear_same_event_support_requirement(
        session, actor=world["head"], subject_membership=world["subject"],
        scheduling_period=world["period"],
    )

    # Supporters first, then the parent: the composite foreign key is ON DELETE
    # RESTRICT, so the order is the guarantee rather than a convention.
    assert session.deleted_objects == [old_row, existing]
    audit = _one_audit_row(session)
    assert audit.action == ACTION_SUPPORT_REQUIREMENT_CLEARED
    assert audit.before_values["supporter_membership_ids"] == [
        world["supporter_a"].id
    ]
    assert audit.after_values is None


def test_clearing_an_absent_requirement_is_a_pure_no_op(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, None)

    clear_same_event_support_requirement(
        session, actor=world["head"], subject_membership=world["subject"],
        scheduling_period=world["period"],
    )

    assert session.deleted_objects == []
    assert _audit_rows(session) == []


def test_a_departed_members_stray_requirement_may_still_be_cleared(
    session, world, monkeypatch
):
    """The clearing asymmetry: it is exempt from the activity checks."""
    departed = _target_membership(
        210, person=_person(20, "Departed"), ministry=world["setup"],
        deactivated=True,
    )
    existing = _existing_requirement(world)
    existing.subject_membership_id = departed.id
    _stub_requirement_lookup(monkeypatch, existing)
    monkeypatch.setattr(
        SupportSession, "execute", lambda self, stmt: _FakeResult([])
    )

    clear_same_event_support_requirement(
        session, actor=world["head"], subject_membership=departed,
        scheduling_period=world["period"],
    )

    assert existing in session.deleted_objects


def test_another_ministrys_head_may_not_clear_one(session, world, monkeypatch):
    _stub_requirement_lookup(monkeypatch, _existing_requirement(world))

    with pytest.raises(AuthorizationError):
        clear_same_event_support_requirement(
            session, actor=world["other_head"],
            subject_membership=world["subject"],
            scheduling_period=world["period"],
        )

    assert session.deleted_objects == []


# ==========================================================================
# The rule's own arithmetic and wording
# ==========================================================================


def _config(min_supporters: int, supporters) -> SupportRequirementConfig:
    return SupportRequirementConfig(
        support_requirement_id=1, subject_membership_id=200,
        min_supporters=min_supporters,
        supporter_membership_ids=frozenset(supporters),
    )


def test_counting_supporters_present_counts_approved_people_only():
    config = _config(1, (201, 202))

    assert count_supporters_present(config, {201, 999}) == 1
    assert count_supporters_present(config, {999}) == 0
    assert count_supporters_present(config, {201, 202}) == 2


def test_satisfiability_is_about_the_approved_set_not_the_roster():
    assert _config(1, (201,)).is_satisfiable
    assert _config(2, (201, 202)).is_satisfiable
    assert not _config(2, (201,)).is_satisfiable
    assert not _config(1, ()).is_satisfiable


def test_the_requirement_wording_is_written_once_and_says_nothing_about_why():
    one = describe_support_requirement(1)
    two = describe_support_requirement(2)

    assert "at least one of their approved supporting members" in one
    assert "at least 2 of" in two
    for wording in (one, two):
        for forbidden in ("transport", "lift", "ride", "drive", "family",
                          "spouse", "partner"):
            assert forbidden not in wording.lower()


def test_the_conflict_code_is_not_an_overridable_blocker():
    from app.services.assignment_policy import OVERRIDABLE_BLOCKERS

    assert SAME_EVENT_SUPPORT_CONFLICT not in OVERRIDABLE_BLOCKERS


# ==========================================================================
# The reads
# ==========================================================================


def test_loading_requirements_costs_one_query_when_none_is_configured():
    class _Recording:
        def __init__(self) -> None:
            self.statements: list = []

        def execute(self, statement):
            self.statements.append(statement)
            return _FakeResult([])

    session = _Recording()

    assert load_support_requirements(session, scheduling_period_id=300) == ()
    assert len(session.statements) == 1


def test_loading_requirements_costs_two_queries_however_many_there_are():
    from types import SimpleNamespace

    class _Recording:
        def __init__(self) -> None:
            self.statements: list = []

        def execute(self, statement):
            self.statements.append(statement)
            if len(self.statements) == 1:
                return _FakeResult(
                    [
                        SimpleNamespace(support_requirement_id=1,
                                        subject_membership_id=200,
                                        min_supporters=1),
                        SimpleNamespace(support_requirement_id=2,
                                        subject_membership_id=210,
                                        min_supporters=2),
                    ]
                )
            return _FakeResult(
                [
                    SimpleNamespace(support_requirement_id=1,
                                    supporter_membership_id=201),
                    SimpleNamespace(support_requirement_id=2,
                                    supporter_membership_id=202),
                    SimpleNamespace(support_requirement_id=2,
                                    supporter_membership_id=203),
                ]
            )

    session = _Recording()
    configs = load_support_requirements(session, scheduling_period_id=300)

    assert len(session.statements) == 2
    assert [c.subject_membership_id for c in configs] == [200, 210]
    assert configs[1].supporter_membership_ids == frozenset({202, 203})


def test_a_requirement_with_no_supporter_rows_is_returned_not_dropped():
    from types import SimpleNamespace

    class _Recording:
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, statement):
            self.calls += 1
            if self.calls == 1:
                return _FakeResult(
                    [
                        SimpleNamespace(support_requirement_id=1,
                                        subject_membership_id=200,
                                        min_supporters=1)
                    ]
                )
            return _FakeResult([])

    (config,) = load_support_requirements(_Recording(), scheduling_period_id=300)

    assert config.supporter_membership_ids == frozenset()
    assert not config.is_satisfiable


def test_the_requirement_query_is_scoped_to_one_period_and_ordered():
    sql = _compile(_period_requirements_statement(300))

    assert "membership_support_requirement.scheduling_period_id = 300" in sql
    assert "ORDER BY membership_support_requirement.subject_membership_id" in sql


def test_the_supporter_query_uses_in_rather_than_one_query_per_requirement():
    sql = _compile(_supporters_statement([1, 2]))

    assert "IN (1, 2)" in sql


def test_the_single_row_lookup_is_scoped_exactly():
    sql = _compile(_requirement_statement(200, 300))

    assert "membership_support_requirement.subject_membership_id = 200" in sql
    assert "membership_support_requirement.scheduling_period_id = 300" in sql


def test_listing_requirements_requires_authority(session, world):
    with pytest.raises(AuthorizationError):
        list_support_requirements(
            session, actor=world["other_head"], scheduling_period=world["period"]
        )
