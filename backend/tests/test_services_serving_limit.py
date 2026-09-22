"""Serving-limit service tests (Task 47).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows :mod:`tests.test_services_availability` exactly, and
for the same reasons: this service flushes once (to obtain a new row's
identity for its audit row), looks a row up through ``session.execute``, and
deletes on the clearing path. So the lookup's SQL is compiled and inspected
with no session at all; the lookup's *use* is monkeypatched per test; and the
flush and delete run against a real, unbound ``Session`` subclass that
simulates identity assignment and records deletions.

Every person, ministry and period here is synthetic.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import MembershipServingLimit, SchedulingPeriod
from app.services import AuthorizationError, InvalidOperationError
from app.services.audit import (
    ACTION_SERVING_LIMIT_CHANGED,
    ACTION_SERVING_LIMIT_CLEARED,
    ACTION_SERVING_LIMIT_RECORDED,
)
from app.services.serving_limit import (
    _limit_statement,
    get_serving_limit,
    set_serving_limit,
)

AV = "Audiovisual"
SETUP = "Setup"


class LimitSession(Session):
    """A real, unbound Session: flush simulates identity, delete is recorded."""

    def __init__(self, *, next_id: int = 900) -> None:
        super().__init__()
        self.commit_calls = 0
        self.rollback_calls = 0
        self.flush_calls = 0
        self.deleted_objects: list = []
        self._next_id = next_id

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

    def delete(self, instance) -> None:
        self.deleted_objects.append(instance)


@pytest.fixture
def session() -> LimitSession:
    return LimitSession()


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
        membership.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return membership


def _period(period_id: int, *, ministry: Ministry, name: str = "AV Oct-Dec 2026",
            locked: bool = False) -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=name,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    if locked:
        period.availability_locked_at = datetime.datetime(
            2026, 9, 1, tzinfo=datetime.timezone.utc
        )
    return period


def _existing(limit_id: int, *, membership: MinistryMembership,
              period: SchedulingPeriod, max_assignments: int) -> MembershipServingLimit:
    limit = MembershipServingLimit(
        ministry_membership_id=membership.id,
        scheduling_period_id=period.id,
        ministry_id=period.ministry_id,
        max_assignments=max_assignments,
    )
    limit.id = limit_id
    return limit


def _stub_lookup(monkeypatch, existing: MembershipServingLimit | None) -> None:
    import app.services.serving_limit as module

    monkeypatch.setattr(
        module, "_find_existing_limit",
        lambda session, *, ministry_membership_id, scheduling_period_id: existing,
    )


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


@pytest.fixture
def world():
    """One AV ministry, an Admin, an AV head, and a target volunteer."""
    av = _ministry(1, AV)
    setup = _ministry(2, SETUP)
    admin = _person(10, "Synthetic Admin", is_admin=True)
    head = _person(11, "Synthetic Head")
    _actor_membership(100, person=head, ministry=av, is_head=True)
    volunteer = _person(12, "Volunteer A")
    membership = _target_membership(200, person=volunteer, ministry=av)
    period = _period(300, ministry=av)
    return {
        "av": av, "setup": setup, "admin": admin, "head": head,
        "volunteer": volunteer, "membership": membership, "period": period,
    }


# ==========================================================================
# 1-4 -- set, update, clear, and rejected values
# ==========================================================================


def test_01_a_head_sets_a_maximum_for_a_matching_membership_and_period(
    session, world, monkeypatch
):
    _stub_lookup(monkeypatch, None)

    limit = set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=4,
    )

    assert limit is not None
    assert limit.max_assignments == 4
    assert limit.ministry_membership_id == 200
    assert limit.scheduling_period_id == 300
    # Shared by both composite foreign keys, and taken from the period.
    assert limit.ministry_id == world["av"].id
    assert session.flush_calls == 1


def test_02_updating_an_existing_maximum_changes_the_row_in_place(
    session, world, monkeypatch
):
    existing = _existing(
        800, membership=world["membership"], period=world["period"], max_assignments=4
    )
    _stub_lookup(monkeypatch, existing)

    limit = set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=5,
    )

    assert limit is existing
    assert existing.max_assignments == 5
    # No second row, and no flush: the row already had an identity.
    assert not [o for o in session.new if isinstance(o, MembershipServingLimit)]
    assert session.flush_calls == 0


def test_03_clearing_removes_the_row_and_returns_no_maximum(
    session, world, monkeypatch
):
    existing = _existing(
        800, membership=world["membership"], period=world["period"], max_assignments=4
    )
    _stub_lookup(monkeypatch, existing)

    result = set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=None,
    )

    assert result is None
    assert session.deleted_objects == [existing]


@pytest.mark.parametrize("bad", [0, -1, -4])
def test_04_zero_and_negative_maxima_are_rejected(session, world, monkeypatch, bad):
    # Zero is not a limit, it is "never schedule this person" -- which
    # deactivation and UNAVAILABLE already say.
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="must be positive"):
        set_serving_limit(
            session, actor=world["head"], membership=world["membership"],
            scheduling_period=world["period"], max_assignments=bad,
        )
    assert not _audit_rows(session)


def test_04b_a_boolean_is_not_an_acceptable_maximum(session, world, monkeypatch):
    # bool subclasses int, so True would otherwise become a maximum of 1.
    _stub_lookup(monkeypatch, None)

    with pytest.raises(InvalidOperationError, match="must be an integer"):
        set_serving_limit(
            session, actor=world["head"], membership=world["membership"],
            scheduling_period=world["period"], max_assignments=True,
        )


def test_04c_no_op_calls_change_nothing_and_are_not_audited(
    session, world, monkeypatch
):
    # Clearing an absent limit, and setting the value it already has.
    _stub_lookup(monkeypatch, None)
    assert set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=None,
    ) is None
    assert not _audit_rows(session)

    existing = _existing(
        800, membership=world["membership"], period=world["period"], max_assignments=4
    )
    _stub_lookup(monkeypatch, existing)
    assert set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=4,
    ) is existing
    assert not _audit_rows(session)
    assert session.deleted_objects == []


# ==========================================================================
# 5 -- cross-ministry scope
# ==========================================================================


def test_05_a_membership_and_period_from_different_ministries_are_rejected(
    session, world, monkeypatch
):
    """An AV head must not be able to bind a Setup period.

    The database enforces this too, through the row's composite foreign keys;
    the service check exists so the caller gets a domain error naming the
    problem rather than an IntegrityError at commit.
    """
    setup_period = _period(400, ministry=world["setup"], name="Setup Q4 2026")
    _stub_lookup(monkeypatch, None)
    # Authorization is scoped to the *period's* ministry, so the actor has to
    # head Setup for the request to reach the mismatch rule at all. A Setup
    # head who tries to limit an AV volunteer is the case worth catching: they
    # are authorized for the period and still must not bind the membership.
    setup_head = _person(13, "Setup Head")
    _actor_membership(101, person=setup_head, ministry=world["setup"], is_head=True)

    with pytest.raises(InvalidOperationError, match="same ministry"):
        set_serving_limit(
            session, actor=setup_head, membership=world["membership"],
            scheduling_period=setup_period, max_assignments=4,
        )
    assert not _audit_rows(session)


def test_05b_the_cross_ministry_rule_is_also_a_database_constraint():
    """Not only a service check: both foreign keys route through ministry_id."""
    from app.db import Base

    table = Base.metadata.tables["membership_serving_limit"]
    composites = [
        fk for fk in table.foreign_key_constraints if len(fk.columns) == 2
    ]
    assert len(composites) == 2
    for fk in composites:
        assert "ministry_id" in {c.name for c in fk.columns}


# ==========================================================================
# 6-9 -- authorization
# ==========================================================================


def test_06_a_head_may_set_a_limit_within_their_own_ministry(
    session, world, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    limit = set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=4,
    )
    assert limit.max_assignments == 4


def test_07_a_head_of_another_ministry_may_not(session, world, monkeypatch):
    other_head = _person(13, "Synthetic Other Head")
    _actor_membership(101, person=other_head, ministry=world["setup"], is_head=True)
    _stub_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_serving_limit(
            session, actor=other_head, membership=world["membership"],
            scheduling_period=world["period"], max_assignments=4,
        )
    assert not _audit_rows(session)


def test_08_a_normal_user_may_not_set_anyones_limit(session, world, monkeypatch):
    normal = _person(14, "Synthetic Member")
    _actor_membership(102, person=normal, ministry=world["av"], is_head=False)
    _stub_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_serving_limit(
            session, actor=normal, membership=world["membership"],
            scheduling_period=world["period"], max_assignments=4,
        )


def test_08b_a_volunteer_may_not_set_their_own_limit(session, world, monkeypatch):
    """Self-service is explicitly not part of first-pass V1.

    Being the subject of the constraint grants no authority over it: the
    volunteer here is an ordinary member of the ministry and is refused for
    exactly the same reason any other ordinary member would be.
    """
    _actor_membership(103, person=world["volunteer"], ministry=world["av"])
    _stub_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_serving_limit(
            session, actor=world["volunteer"], membership=world["membership"],
            scheduling_period=world["period"], max_assignments=4,
        )


def test_09_an_admin_who_heads_nothing_may_not_set_a_limit(session, world, monkeypatch):
    """Task 80 reversed this. A serving limit is how often somebody serves in
    one ministry, for one period -- an operational decision belonging to that
    ministry's head. Church-wide Admin authority is oversight, and oversight
    does not extend to rewriting a team's rota rules."""
    _stub_lookup(monkeypatch, None)

    with pytest.raises(AuthorizationError):
        set_serving_limit(
            session, actor=world["admin"], membership=world["membership"],
            scheduling_period=world["period"], max_assignments=4,
        )
    assert not _audit_rows(session)


def test_09b_a_deactivated_target_cannot_receive_a_new_limit_but_can_be_cleared(
    session, world, monkeypatch
):
    gone = _person(15, "Synthetic Departed")
    membership = _target_membership(
        201, person=gone, ministry=world["av"], deactivated=True
    )
    _stub_lookup(monkeypatch, None)
    with pytest.raises(InvalidOperationError, match="deactivated membership"):
        set_serving_limit(
            session, actor=world["head"], membership=membership,
            scheduling_period=world["period"], max_assignments=4,
        )

    # Clearing stays possible, so a departed member's stray row can be tidied.
    existing = _existing(
        801, membership=membership, period=world["period"], max_assignments=4
    )
    _stub_lookup(monkeypatch, existing)
    assert set_serving_limit(
        session, actor=world["head"], membership=membership,
        scheduling_period=world["period"], max_assignments=None,
    ) is None
    assert session.deleted_objects == [existing]


# ==========================================================================
# 10 -- audit
# ==========================================================================


def test_10_recording_audits_the_new_value_with_no_personal_reason(
    session, world, monkeypatch
):
    _stub_lookup(monkeypatch, None)
    set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=4,
    )

    row = _one_audit_row(session)
    assert row.action == ACTION_SERVING_LIMIT_RECORDED
    assert row.target_table == "membership_serving_limit"
    assert row.ministry_id == world["av"].id
    assert row.before_values is None
    assert row.after_values == {
        "ministry_membership_id": 200,
        "scheduling_period_id": 300,
        "max_assignments": 4,
    }
    # No reason is stored, and none was asked for: the schedule needs the
    # number, not the volunteer's circumstances.
    assert row.reason is None


def test_10b_changing_audits_both_the_old_and_new_values(
    session, world, monkeypatch
):
    existing = _existing(
        800, membership=world["membership"], period=world["period"], max_assignments=4
    )
    _stub_lookup(monkeypatch, existing)

    set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=5,
    )

    row = _one_audit_row(session)
    assert row.action == ACTION_SERVING_LIMIT_CHANGED
    assert row.before_values["max_assignments"] == 4
    assert row.after_values["max_assignments"] == 5
    assert row.target_id == 800
    assert row.reason is None
    # The summary is the sentence a head reads in history.
    assert "from 4 to 5" in row.summary


def test_10c_clearing_audits_the_removed_value_and_no_after_state(
    session, world, monkeypatch
):
    existing = _existing(
        800, membership=world["membership"], period=world["period"], max_assignments=4
    )
    _stub_lookup(monkeypatch, existing)

    set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=None,
    )

    row = _one_audit_row(session)
    assert row.action == ACTION_SERVING_LIMIT_CLEARED
    assert row.before_values["max_assignments"] == 4
    # "No maximum" is the absence of a row, so there is no after-state to name.
    assert row.after_values is None
    assert row.reason is None


def test_10d_no_audit_payload_carries_a_free_text_explanation(
    session, world, monkeypatch
):
    """The stored context answers who/whose/which period/what number, and
    deliberately nothing about why the volunteer asked."""
    _stub_lookup(monkeypatch, None)
    set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=4,
    )
    row = _one_audit_row(session)
    assert set(row.after_values) == {
        "ministry_membership_id", "scheduling_period_id", "max_assignments"
    }


def test_10e_the_service_never_commits_or_rolls_back(session, world, monkeypatch):
    _stub_lookup(monkeypatch, None)
    set_serving_limit(
        session, actor=world["head"], membership=world["membership"],
        scheduling_period=world["period"], max_assignments=4,
    )
    assert session.commit_calls == 0
    assert session.rollback_calls == 0


# ==========================================================================
# Lookup SQL and the read helper
# ==========================================================================


def _compile_limit(stmt) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def test_the_lookup_is_scoped_to_one_membership_and_one_period():
    sql = _compile_limit(_limit_statement(200, 300))

    # ``IN (200)`` rather than ``= 200``: Task 63 made the set-based form the
    # single definition and the one-membership form delegate to it with a
    # one-element set. Which rows qualify is unchanged -- the predicate is the
    # same predicate -- and the two cannot drift apart.
    assert "membership_serving_limit.ministry_membership_id IN (200)" in sql
    assert "membership_serving_limit.scheduling_period_id = 300" in sql


def test_the_one_membership_and_set_based_lookups_are_one_definition():
    from app.services.serving_limit import _limits_query

    single = _compile_limit(_limit_statement(200, 300))
    batch = _compile_limit(
        _limits_query(ministry_membership_ids=(200,), scheduling_period_id=300)
    )

    assert single == batch


def test_the_set_based_lookup_widens_only_the_membership_predicate():
    from app.services.serving_limit import _limits_query

    sql = _compile_limit(
        _limits_query(ministry_membership_ids=(200, 201), scheduling_period_id=300)
    )

    assert "membership_serving_limit.ministry_membership_id IN (200, 201)" in sql
    # The period stays a single scalar test, because a limit is always read
    # for exactly one period.
    assert "membership_serving_limit.scheduling_period_id = 300" in sql


def test_get_serving_limits_for_keys_by_membership_and_omits_the_unlimited():
    """A missing key means "no maximum configured", exactly as ``None`` does
    from the one-membership form. It must never be materialized as zero.
    """
    from app.services.serving_limit import get_serving_limits_for

    class _Session:
        def __init__(self):
            self.statements = []

        def execute(self, statement):
            self.statements.append(statement)
            rows = [
                SimpleNamespace(ministry_membership_id=200, max_assignments=3),
                SimpleNamespace(ministry_membership_id=202, max_assignments=1),
            ]
            return SimpleNamespace(scalars=lambda: iter(rows))

    session = _Session()
    limits = get_serving_limits_for(
        session, ministry_membership_ids=[200, 201, 202], scheduling_period_id=300
    )

    assert limits == {200: 3, 202: 1}
    assert 201 not in limits
    # One query for the whole set, which is the entire point.
    assert len(session.statements) == 1


def test_get_serving_limits_for_asks_nothing_when_there_is_nobody_to_ask_about():
    from app.services.serving_limit import get_serving_limits_for

    class _Session:
        def execute(self, statement):  # pragma: no cover - must not run
            raise AssertionError("no memberships means no query")

    assert get_serving_limits_for(
        _Session(), ministry_membership_ids=[], scheduling_period_id=300
    ) == {}


def test_get_serving_limit_returns_none_when_no_row_exists(session, monkeypatch):
    _stub_lookup(monkeypatch, None)
    assert get_serving_limit(
        session, ministry_membership_id=200, scheduling_period_id=300
    ) is None


def test_get_serving_limit_returns_the_configured_number(
    session, world, monkeypatch
):
    _stub_lookup(
        monkeypatch,
        _existing(800, membership=world["membership"], period=world["period"],
                  max_assignments=4),
    )
    assert get_serving_limit(
        session, ministry_membership_id=200, scheduling_period_id=300
    ) == 4


def test_a_limit_is_scoped_to_one_period_and_does_not_span_ministries():
    """The model's own shape is the guarantee, so it is asserted directly.

    A limit names one membership and one period. There is no person-level
    column and no church-wide column, so 'four AV assignments this quarter'
    cannot accidentally become 'four assignments anywhere in the church'.
    """
    from app.db import Base

    columns = set(Base.metadata.tables["membership_serving_limit"].columns.keys())
    assert "ministry_membership_id" in columns
    assert "scheduling_period_id" in columns
    assert "person_id" not in columns


# ==========================================================================
# list_serving_limits -- the read/list side added for the API (Task 57)
# ==========================================================================

from app.services.serving_limit import (  # noqa: E402
    MembershipServingLimitEntry,
    _period_serving_limits_statement,
    list_serving_limits,
)


class _Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _ListSession:
    """Returns a fixed row set for the one statement list_serving_limits issues."""

    def __init__(self, rows):
        self._rows = rows
        self.statements = []

    def execute(self, statement, *a, **k):
        self.statements.append(str(statement))

        class _R:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        return _R(self._rows)


def test_list_statement_left_joins_and_filters_inactive_by_default():
    sql = str(
        _period_serving_limits_statement(300, 1, include_inactive=False).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "LEFT OUTER JOIN membership_serving_limit" in sql
    assert "membership_serving_limit.scheduling_period_id = 300" in sql
    assert "ministry_membership.ministry_id = 1" in sql
    assert "ministry_membership.deactivated_at IS NULL" in sql
    assert "person.deactivated_at IS NULL" in sql


def test_list_statement_includes_inactive_when_asked():
    sql = str(
        _period_serving_limits_statement(300, 1, include_inactive=True).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "deactivated_at IS NULL" not in sql


def test_list_serving_limits_requires_a_ministry_manager(world, monkeypatch):
    outsider = _person(99, "Other Head")
    _actor_membership(999, person=outsider, ministry=world["setup"], is_head=True)
    session = _ListSession([])
    with pytest.raises(AuthorizationError):
        list_serving_limits(
            session, actor=outsider, scheduling_period=world["period"],
        )


def test_list_serving_limits_reports_absence_as_none_and_a_number_as_itself(
    world, monkeypatch
):
    session = _ListSession(
        [
            _Row(
                ministry_membership_id=200, person_id=12,
                person_display_name="Volunteer A",
                membership_deactivated_at=None, person_deactivated_at=None,
                max_assignments=4,
            ),
            _Row(
                ministry_membership_id=201, person_id=13,
                person_display_name="Volunteer B",
                membership_deactivated_at=None, person_deactivated_at=None,
                max_assignments=None,
            ),
        ]
    )
    # An Admin, deliberately: listing is oversight, which Task 80 left with
    # them even though it took every write away.
    result = list_serving_limits(
        session, actor=world["admin"], scheduling_period=world["period"],
    )
    assert result.scheduling_period_id == 300
    assert result.scheduling_period_name == "AV Oct-Dec 2026"
    assert result.ministry_id == 1
    assert result.memberships == (
        MembershipServingLimitEntry(200, 12, "Volunteer A", None, None, 4),
        MembershipServingLimitEntry(201, 13, "Volunteer B", None, None, None),
    )
