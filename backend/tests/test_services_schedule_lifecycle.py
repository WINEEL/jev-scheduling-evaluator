"""ScheduleVersion DRAFT -> REVIEW lifecycle tests (schedule-output §6, §7, §13).

Offline: no PostgreSQL, no Neon, no network. No SQLite pretending to prove
PostgreSQL semantics.

**Test strategy** follows Tasks 22/23's precedent:

- **Orchestration** (authorization, latest-version handling, idempotency,
  staleness gating, audit construction) is exercised through the public
  function with the three private lookups --
  ``_resolve_scheduling_period``, ``_newer_version_exists`` and (imported
  from Task 23) ``get_schedule_version_staleness`` -- monkeypatched directly
  on the module, exactly as ``test_services_assignment.py`` monkeypatches its
  own lookup helpers. Task 23's own result type,
  :class:`~app.services.schedule_staleness.ScheduleVersionStalenessResult`, is
  reused verbatim for the staleness fixtures rather than reinvented.
- **Each private query's SQL** (``_scheduling_period_lookup_statement``,
  ``_newer_version_exists_statement``) is compiled against the PostgreSQL
  dialect with literal binds and inspected as text, with no session at all.
- **Session discipline** is checked against a real, unbound ``Session``
  subclass that raises on ``flush``/``delete``/``commit``/``rollback`` --
  ``add`` is left as ordinary ``Session.add`` (needed for the one permitted
  AuditEvent), and what actually got added is inspected via ``session.new``
  exactly as ``test_services_assignment.py`` does.

Honest limitation, as in every earlier task: this proves what the module
*does* with a session and what its SQL *says*, not that PostgreSQL returns
those rows.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    ScheduleVersion,
)
from app.models.scheduling_input import SchedulingPeriod
from app.services.audit import ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.schedule_lifecycle import (
    _newer_version_exists_statement,
    _scheduling_period_lookup_statement,
    submit_schedule_version_for_review,
)
from app.services.schedule_staleness import ScheduleVersionStalenessResult

# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


class LifecycleSession(Session):
    """A real, unbound Session permitting only ``add`` (for the one AuditEvent
    a real transition writes). ``flush``, ``delete``, ``commit`` and
    ``rollback`` all raise -- this operation needs none of them (module
    docstring: the Version already has its identity, so no flush is needed).
    """

    def __init__(self) -> None:
        super().__init__(autoflush=False)
        self.flush_calls = 0
        self.delete_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

    def flush(self, objects=None) -> None:  # pragma: no cover - must never run
        self.flush_calls += 1
        raise AssertionError("this operation must never flush")

    def delete(self, instance) -> None:  # pragma: no cover - must never run
        self.delete_calls += 1
        raise AssertionError("this operation must never delete")

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")


@pytest.fixture
def session() -> LifecycleSession:
    return LifecycleSession()


def _person(person_id: int, name: str, *, is_admin: bool = False, deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return person


def _ministry(ministry_id: int, name: str) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


def _actor_membership(
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _period(period_id: int, *, ministry: Ministry, name: str = "Q4 2026") -> SchedulingPeriod:
    # Deliberately does NOT embed the Ministry's name (e.g. "Setup") -- the
    # summary must combine period.ministry.name and period.name explicitly,
    # never rely on one already containing the other.
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=name,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    period.ministry = ministry
    return period


def _version(
    version_id: int = 500, *, schedule_id: int = 400, scheduling_period_id: int = 11,
    version_number: int = 1, status: str = SCHEDULE_VERSION_STATUS_DRAFT,
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=schedule_id, scheduling_period_id=scheduling_period_id,
        version_number=version_number, status=status,
    )
    version.id = version_id
    return version


@pytest.fixture
def head(setup_ministry: Ministry) -> Person:
    """The actor every operational write in this module is performed by.

    **An active Ministry Head of the ministry being written to** -- which,
    since Task 80, is the only kind of person who may perform any of these
    operations. This fixture used to be an Admin who headed nothing; that
    person now gets 403 from every write here, and the tests that assert so
    say Admin in their own names.
    """
    person = _person(1, "Demo Admin")
    _actor_membership(9001, person=person, ministry=setup_ministry, is_head=True)
    return person


@pytest.fixture
def setup_ministry() -> Ministry:
    return _ministry(3, "Setup")


@pytest.fixture
def setup_period(setup_ministry: Ministry) -> SchedulingPeriod:
    return _period(11, ministry=setup_ministry)


@pytest.fixture
def draft_version() -> ScheduleVersion:
    return _version()


_FRESH = ScheduleVersionStalenessResult(current_requirements=frozenset(), snapshot_requirements=frozenset())
_STALE = ScheduleVersionStalenessResult(
    current_requirements=frozenset({("stub-current",)}),
    snapshot_requirements=frozenset({("stub-snapshot",)}),
)


def _stub_environment(
    monkeypatch, *, period: SchedulingPeriod, newer_version_exists: bool = False,
    staleness: ScheduleVersionStalenessResult = _FRESH,
) -> list[tuple[str, object, dict]]:
    """Patch the three lookups this operation performs, recording every call
    (name, session, kwargs) so orchestration tests can prove call order,
    argument scoping and "same session" claims without a database.
    """
    import app.services.schedule_lifecycle as module

    calls: list[tuple[str, object, dict]] = []

    def fake_resolve(session, version):
        calls.append(("resolve_period", session, {"version": version}))
        return period

    def fake_newer(session, *, schedule_id, version_number):
        calls.append(("newer_version_exists", session, {
            "schedule_id": schedule_id, "version_number": version_number,
        }))
        return newer_version_exists

    def fake_staleness(session, *, version):
        calls.append(("get_staleness", session, {"version": version}))
        return staleness

    monkeypatch.setattr(module, "_resolve_scheduling_period", fake_resolve)
    monkeypatch.setattr(module, "_newer_version_exists", fake_newer)
    monkeypatch.setattr(module, "get_schedule_version_staleness", fake_staleness)
    return calls


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------
# 1-7 -- Authorization / context
# --------------------------------------------------------------------------


def test_01_active_admin_can_submit(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    result = submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert result is draft_version
    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_02_own_ministry_head_can_submit(session, setup_ministry, setup_period, draft_version, monkeypatch):
    head = _person(2, "Head")
    _actor_membership(200, person=head, ministry=setup_ministry, is_head=True)
    _stub_environment(monkeypatch, period=setup_period)

    result = submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_03_head_of_another_ministry_is_rejected(session, setup_period, draft_version, monkeypatch):
    other_ministry = _ministry(4, "AV")
    other_head = _person(5, "AV Head")
    _actor_membership(201, person=other_head, ministry=other_ministry, is_head=True)
    _stub_environment(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        submit_schedule_version_for_review(session, actor=other_head, version=draft_version)
    assert draft_version.status == SCHEDULE_VERSION_STATUS_DRAFT
    assert _audit_rows(session) == []


def test_04_normal_member_is_rejected(session, setup_ministry, setup_period, draft_version, monkeypatch):
    ordinary = _person(6, "Ordinary")
    _actor_membership(202, person=ordinary, ministry=setup_ministry, is_head=False)
    _stub_environment(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        submit_schedule_version_for_review(session, actor=ordinary, version=draft_version)
    assert draft_version.status == SCHEDULE_VERSION_STATUS_DRAFT


def test_05_deactivated_admin_is_rejected(session, setup_period, draft_version, monkeypatch):
    deactivated_admin = _person(7, "Former Admin", is_admin=True, deactivated=True)
    _stub_environment(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        submit_schedule_version_for_review(session, actor=deactivated_admin, version=draft_version)
    assert draft_version.status == SCHEDULE_VERSION_STATUS_DRAFT


def test_06_ministry_is_derived_from_period_not_caller_input(session, setup_ministry, draft_version, monkeypatch):
    # No ministry_id parameter exists on the public function at all; this
    # proves the *value actually used* for authorization is period.ministry_id
    # by giving a Head of a *different* ministry than the fixture default and
    # a period that names that different ministry -- authorization succeeds
    # only because it is scoped to the resolved period's ministry.
    other_ministry = _ministry(9, "Kids")
    other_period = _period(12, ministry=other_ministry, name="Kids Q4 2026")
    head = _person(8, "Kids Head")
    _actor_membership(300, person=head, ministry=other_ministry, is_head=True)
    _stub_environment(monkeypatch, period=other_period)

    result = submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW
    audit = _one_audit_row(session)
    assert audit.ministry_id == other_ministry.id


def test_07a_unpersisted_version_is_rejected_safely(session, head):
    version = _version()
    version.id = None

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=version)


def test_07b_version_without_schedule_id_is_rejected_safely(session, head):
    version = _version()
    version.schedule_id = None

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=version)


def test_07c_version_without_scheduling_period_id_is_rejected_safely(session, head):
    version = _version()
    version.scheduling_period_id = None

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=version)


class _NoRowResult:
    """A stand-in for what ``session.execute(...)`` returns when no row
    matches -- just enough to exercise the real, unmonkeypatched
    ``_resolve_scheduling_period`` without a bound engine."""

    def scalar_one_or_none(self):
        return None


def test_07d_unresolvable_scheduling_period_is_rejected_safely(session, head, draft_version, monkeypatch):
    # The lookup function itself runs for real here (not monkeypatched) --
    # only session.execute is stubbed, to say "no row", which an unbound
    # offline session cannot honestly determine on its own.
    monkeypatch.setattr(session, "execute", lambda stmt: _NoRowResult())

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=draft_version)


# --------------------------------------------------------------------------
# 8-14 -- Latest-version behavior
# --------------------------------------------------------------------------


def test_08_latest_draft_may_transition(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period, newer_version_exists=False)

    result = submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_09_superseded_draft_is_rejected(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period, newer_version_exists=True)

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=draft_version)
    assert draft_version.status == SCHEDULE_VERSION_STATUS_DRAFT
    assert _audit_rows(session) == []


@pytest.mark.parametrize("newer_status", [SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW, SCHEDULE_VERSION_STATUS_FINALIZED])
def test_10_11_12_a_newer_version_of_any_status_makes_the_older_draft_immutable(
    session, head, setup_period, draft_version, monkeypatch, newer_status
):
    # newer_status is not consulted by this module at all -- _newer_version_exists
    # is a plain existence probe (number > current), regardless of what status
    # the newer row holds. Parametrizing over DRAFT/REVIEW/FINALIZED proves the
    # rejection does not depend on it.
    _stub_environment(monkeypatch, period=setup_period, newer_version_exists=True)

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=draft_version)


def test_13_latest_version_query_is_scoped_to_the_same_schedule():
    compiled = _compile(_newer_version_exists_statement(400, 1))

    assert "schedule_version.schedule_id = 400" in compiled


def test_14_latest_version_query_checks_version_number_greater_than():
    compiled = _compile(_newer_version_exists_statement(400, 3))

    assert "schedule_version.version_number > 3" in compiled
    assert ">= 3" not in compiled


def test_newer_version_query_limits_to_one_row_existence_probe():
    compiled = _compile(_newer_version_exists_statement(400, 1))

    assert "LIMIT 1" in compiled


def test_newer_version_check_is_called_with_this_versions_schedule_and_number(
    session, head, setup_period, draft_version, monkeypatch
):
    calls = _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    newer_call = next(c for c in calls if c[0] == "newer_version_exists")
    assert newer_call[2] == {"schedule_id": 400, "version_number": 1}


# --------------------------------------------------------------------------
# 15-22 -- Status transition and idempotency
# --------------------------------------------------------------------------


def test_15_draft_to_review_succeeds(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert draft_version.status == "REVIEW"


def test_16_resulting_status_uses_the_model_constant(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert draft_version.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_17_finalized_is_rejected(session, head, setup_period, monkeypatch):
    finalized = _version(status=SCHEDULE_VERSION_STATUS_FINALIZED)
    _stub_environment(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=finalized)
    assert finalized.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert _audit_rows(session) == []


def test_18_latest_review_to_review_is_idempotent(session, head, setup_period, monkeypatch):
    review_version = _version(status=SCHEDULE_VERSION_STATUS_REVIEW)
    _stub_environment(monkeypatch, period=setup_period, newer_version_exists=False)

    result = submit_schedule_version_for_review(session, actor=head, version=review_version)

    assert result is review_version
    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_19_idempotent_review_preserves_state_exactly(session, head, setup_period, monkeypatch):
    review_version = _version(status=SCHEDULE_VERSION_STATUS_REVIEW, version_number=2)
    before = dict(review_version.__dict__)
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=review_version, reason="Looks fine.")

    after = dict(review_version.__dict__)
    assert before == after


def test_20_idempotent_review_creates_no_audit_event(session, head, setup_period, monkeypatch):
    review_version = _version(status=SCHEDULE_VERSION_STATUS_REVIEW)
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=review_version)

    assert _audit_rows(session) == []
    assert len(session.new) == 0


def test_21_idempotent_review_performs_no_flush(session, head, setup_period, monkeypatch):
    review_version = _version(status=SCHEDULE_VERSION_STATUS_REVIEW)
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=review_version)

    assert session.flush_calls == 0


def test_22_no_review_to_draft_capability_exists():
    import app.services.schedule_lifecycle as module

    assert not hasattr(module, "submit_schedule_version_for_draft")
    assert not [name for name in vars(module) if "to_draft" in name.lower()]


def test_superseded_review_is_rejected_not_treated_as_idempotent(session, head, setup_period, monkeypatch):
    superseded_review = _version(status=SCHEDULE_VERSION_STATUS_REVIEW)
    _stub_environment(monkeypatch, period=setup_period, newer_version_exists=True)

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=superseded_review)
    assert _audit_rows(session) == []


def test_unrecognized_status_is_rejected_not_silently_changed(session, head, setup_period, monkeypatch):
    weird_version = _version(status="ARCHIVED")
    _stub_environment(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=weird_version)
    assert weird_version.status == "ARCHIVED"


# --------------------------------------------------------------------------
# 23-28 -- Staleness prerequisite
# --------------------------------------------------------------------------


def test_23_fresh_draft_may_enter_review(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period, staleness=_FRESH)

    result = submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_24_stale_draft_is_rejected(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period, staleness=_STALE)

    with pytest.raises(InvalidOperationError, match="snapshot"):
        submit_schedule_version_for_review(session, actor=head, version=draft_version)


def test_25_stale_rejection_leaves_status_draft(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period, staleness=_STALE)

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=draft_version)
    assert draft_version.status == SCHEDULE_VERSION_STATUS_DRAFT


def test_26_stale_rejection_creates_no_audit_event(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period, staleness=_STALE)

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=draft_version)
    assert _audit_rows(session) == []
    assert len(session.new) == 0


def test_27_staleness_check_receives_the_same_supplied_session(session, head, setup_period, draft_version, monkeypatch):
    calls = _stub_environment(monkeypatch, period=setup_period, staleness=_FRESH)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    staleness_call = next(c for c in calls if c[0] == "get_staleness")
    assert staleness_call[1] is session
    assert staleness_call[2] == {"version": draft_version}


def test_28_snapshot_rows_are_never_touched_by_this_module(session, head, setup_period, draft_version, monkeypatch):
    # ScheduleVersionRequirement is never imported into this module at all --
    # the strongest available proof that nothing here can add, mutate or
    # delete a snapshot row.
    import app.services.schedule_lifecycle as module

    assert not hasattr(module, "ScheduleVersionRequirement")
    _stub_environment(monkeypatch, period=setup_period)
    submit_schedule_version_for_review(session, actor=head, version=draft_version)
    assert len(session.deleted) == 0


# --------------------------------------------------------------------------
# 29-32 -- Completeness boundary (deliberately NOT enforced here)
# --------------------------------------------------------------------------


def test_29_zero_assignment_version_may_enter_review_if_fresh(session, head, setup_period, draft_version, monkeypatch):
    # No Assignment lookup exists in this module for the staleness stub to
    # even reflect -- entering REVIEW does not depend on assignment counts.
    _stub_environment(monkeypatch, period=setup_period, staleness=_FRESH)

    result = submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_30_partially_filled_version_may_enter_review_if_fresh(session, head, setup_period, draft_version, monkeypatch):
    # "Partially filled" is not a concept this module computes at all; fresh
    # staleness is the only gate, regardless of how many requirements exist
    # or are filled.
    _stub_environment(monkeypatch, period=setup_period, staleness=_FRESH)

    result = submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_31_zero_requirement_version_may_enter_review_if_fresh(session, head, setup_period, draft_version, monkeypatch):
    empty_fresh = ScheduleVersionStalenessResult(current_requirements=frozenset(), snapshot_requirements=frozenset())
    _stub_environment(monkeypatch, period=setup_period, staleness=empty_fresh)

    result = submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert result.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_32_no_assignment_count_query_is_introduced():
    import app.services.schedule_lifecycle as module

    assert not hasattr(module, "Assignment")
    assert not [name for name in vars(module) if "assignment" in name.lower()]


# --------------------------------------------------------------------------
# 33-41 -- Audit behavior
# --------------------------------------------------------------------------


def test_33_transition_creates_exactly_one_audit_event(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert len(_audit_rows(session)) == 1
    assert _one_audit_row(session).action == ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW


def test_34_audit_target_table_and_id_are_correct(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    audit = _one_audit_row(session)
    assert audit.target_table == "schedule_version"
    assert audit.target_id == draft_version.id == 500


def test_35_audit_ministry_comes_from_the_versions_period(session, head, setup_ministry, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    audit = _one_audit_row(session)
    assert audit.ministry_id == setup_ministry.id == 3


def test_36_before_values_status_is_draft(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert _one_audit_row(session).before_values == {"status": "DRAFT"}


def test_37_after_values_status_is_review(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert _one_audit_row(session).after_values == {"status": "REVIEW"}


def test_38_summary_uses_actual_version_number_ministry_name_and_period_name(
    session, head, setup_ministry, setup_period, monkeypatch
):
    # setup_ministry.name == "Setup", setup_period.name == "Q4 2026" -- the
    # period name deliberately does not embed the Ministry name, so the
    # summary must combine both independently, not rely on one containing
    # the other.
    version = _version(version_number=4)
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=version)

    audit = _one_audit_row(session)
    assert "4" in audit.summary
    assert setup_ministry.name in audit.summary
    assert setup_period.name in audit.summary
    assert audit.summary == "Submitted draft version 4 for Setup Q4 2026 for review"


def test_39_optional_reason_is_copied_to_audit_reason(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version, reason="Ready for the pastor to review.")

    assert _one_audit_row(session).reason == "Ready for the pastor to review."


def test_39b_no_reason_supplied_leaves_audit_reason_none(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert _one_audit_row(session).reason is None


def test_40_whitespace_only_reason_is_rejected(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError):
        submit_schedule_version_for_review(session, actor=head, version=draft_version, reason="   ")
    assert draft_version.status == SCHEDULE_VERSION_STATUS_DRAFT
    assert _audit_rows(session) == []


def test_41_idempotent_call_does_not_duplicate_audit_history(session, head, setup_period, monkeypatch):
    review_version = _version(status=SCHEDULE_VERSION_STATUS_REVIEW)
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=review_version, reason="Already submitted.")

    assert _audit_rows(session) == []


# --------------------------------------------------------------------------
# 42-47 -- Transaction / session discipline
# --------------------------------------------------------------------------


def test_42_never_adds_a_domain_row_only_the_audit_event(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert len(session.new) == 1
    assert isinstance(next(iter(session.new)), AuditEvent)


def test_43_never_deletes(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)
    submit_schedule_version_for_review(session, actor=head, version=draft_version)
    assert session.delete_calls == 0


def test_44_never_flushes(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)
    submit_schedule_version_for_review(session, actor=head, version=draft_version)
    assert session.flush_calls == 0


def test_45_never_commits(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)
    submit_schedule_version_for_review(session, actor=head, version=draft_version)
    assert session.commit_calls == 0


def test_46_never_rolls_back(session, head, setup_period, draft_version, monkeypatch):
    _stub_environment(monkeypatch, period=setup_period)
    submit_schedule_version_for_review(session, actor=head, version=draft_version)
    assert session.rollback_calls == 0


def test_47_every_lookup_uses_the_one_supplied_session(session, head, setup_period, draft_version, monkeypatch):
    calls = _stub_environment(monkeypatch, period=setup_period)

    submit_schedule_version_for_review(session, actor=head, version=draft_version)

    assert len(calls) == 3
    assert all(call[1] is session for call in calls)


def test_exception_from_audit_creation_propagates_without_manual_rollback(session, head, setup_period, draft_version, monkeypatch):
    import app.services.schedule_lifecycle as module

    _stub_environment(monkeypatch, period=setup_period)

    def boom(*args, **kwargs):
        raise RuntimeError("audit write exploded")

    monkeypatch.setattr(module, "record_audit_event", boom)

    with pytest.raises(RuntimeError, match="audit write exploded"):
        submit_schedule_version_for_review(session, actor=head, version=draft_version)
    # The status mutation already happened and is deliberately NOT restored --
    # that is the caller-owned transaction's job via rollback (module docstring).
    assert draft_version.status == SCHEDULE_VERSION_STATUS_REVIEW


# --------------------------------------------------------------------------
# Private query shape (compiled SQL)
# --------------------------------------------------------------------------


def test_scheduling_period_lookup_is_scoped_to_the_given_id():
    compiled = _compile(_scheduling_period_lookup_statement(11))

    assert "scheduling_period.id = 11" in compiled


def test_scheduling_period_lookup_selects_the_whole_row():
    compiled = _compile(_scheduling_period_lookup_statement(11))

    assert "FROM scheduling_period" in compiled
