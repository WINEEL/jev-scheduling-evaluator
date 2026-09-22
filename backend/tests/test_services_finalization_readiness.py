"""Finalization-readiness tests (schedule-output §13, §16; Tasks 21-23).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy**, following Tasks 21-24's precedent for a read-only service:

- **Orchestration** runs through the public function with the module's own
  read helpers monkeypatched -- ``_fetch_requirements``, ``_fetch_assignments``,
  ``_find_qualification``, ``_find_availability``, and the two reused public
  services (``get_schedule_version_staleness``, ``get_person_sunday_conflicts``,
  imported by name into this module's namespace). Real ORM objects are built
  for the domain rows so relationship traversal (`assignment.ministry_membership.person`,
  `requirement.ministry_role`) is genuine.
- **Override history** is exercised with real ``AuditEvent`` rows returned by a
  fake execute, so the payload parsing is tested against the shape Task 22
  actually writes rather than a dict stand-in.
- **Each query's SQL** is compiled against the PostgreSQL dialect and inspected.
- **Session discipline** uses a real unbound ``Session`` subclass that raises on
  ``add``/``delete``/``flush``/``commit``/``rollback`` -- nothing is permitted,
  because this service is read-only.

Honest limitation, as ever: this proves control flow and query text, not that
PostgreSQL returns these rows. ``tests/integration/test_pg_finalization_readiness.py``
does that against real data.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_BACKUP,
    AVAILABILITY_UNAVAILABLE,
    EVENT_KIND_SUNDAY_SERVICE,
    Availability,
    Event,
)
from app.services.assignment_policy import (
    BLOCKER_CAPACITY_FULL,
    BLOCKER_NOT_QUALIFIED,
    BLOCKER_ROLE_DEACTIVATED,
    BLOCKER_SUNDAY_CONFLICT,
    BLOCKER_UNAVAILABLE,
)
from app.services.audit import ACTION_ASSIGNMENT_OVERRIDE_APPLIED
from app.services.errors import InvalidOperationError
from app.services.finalization_readiness import (
    ISSUE_AMBIGUOUS_OVERRIDE_AUDIT,
    ISSUE_CANCELLED_EVENT,
    ISSUE_INACTIVE_MEMBERSHIP,
    ISSUE_INACTIVE_PERSON,
    ISSUE_EXCEEDS_SERVING_LIMIT,
    ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT,
    ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT,
    ISSUE_INVALID_OVERRIDE_PAYLOAD,
    ISSUE_MISSING_OVERRIDE_AUDIT,
    ISSUE_STALE_REQUIREMENT_SNAPSHOT,
    ISSUE_UNAUTHORIZED_BLOCKER,
    ISSUE_UNAUTHORIZED_OVERFILL,
    ISSUE_UNFILLED_REQUIREMENT,
    FinalizationIssue,
    FinalizationReadinessResult,
    _assignments_statement,
    _availability_statement,
    _override_audit_statement,
    _qualification_statement,
    _requirements_statement,
    get_finalization_readiness,
)
from app.services.schedule_staleness import (
    RequirementFingerprint,
    ScheduleVersionStalenessResult,
)
from app.services.sunday_conflict import SundayConflictResult

import app.services.finalization_readiness as _finalization_module

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)

FRESH = ScheduleVersionStalenessResult(
    current_requirements=frozenset(), snapshot_requirements=frozenset()
)
_FP = RequirementFingerprint(
    event_id=700, event_date=NOV_15, ministry_role_id=12, required_count=1
)
STALE = ScheduleVersionStalenessResult(
    current_requirements=frozenset({_FP}), snapshot_requirements=frozenset()
)


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------


class ReadinessSession(Session):
    """A real, unbound Session that fails on every mutation method."""

    def __init__(self) -> None:
        super().__init__(autoflush=False)
        self.add_calls = 0
        self.delete_calls = 0
        self.flush_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

    def add(self, instance, _warn=True) -> None:  # pragma: no cover - must never run
        self.add_calls += 1
        raise AssertionError("a read-only service must never add")

    def delete(self, instance) -> None:  # pragma: no cover - must never run
        self.delete_calls += 1
        raise AssertionError("a read-only service must never delete")

    def flush(self, objects=None) -> None:  # pragma: no cover - must never run
        self.flush_calls += 1
        raise AssertionError("a read-only service must never flush")

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a read-only service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a read-only service must never roll back")


@pytest.fixture
def session() -> ReadinessSession:
    return ReadinessSession()


def _person(person_id: int, name: str, *, deactivated: bool = False) -> Person:
    person = Person(display_name=name, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = DEACTIVATED
    return person


def _ministry(ministry_id: int = 3, name: str = "Setup") -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


def _membership(
    membership_id: int, *, person: Person, ministry: Ministry, deactivated: bool = False
) -> MinistryMembership:
    membership = MinistryMembership(person_id=person.id, ministry_id=ministry.id)
    membership.id = membership_id
    membership.person = person
    if deactivated:
        membership.deactivated_at = DEACTIVATED
    return membership


def _role(
    role_id: int = 12, name: str = "Setup Lead", *, ministry: Ministry, deactivated: bool = False
) -> MinistryRole:
    role = MinistryRole(name=name, ministry_id=ministry.id)
    role.id = role_id
    role.ministry = ministry
    if deactivated:
        role.deactivated_at = DEACTIVATED
    return role


def _event(event_id: int = 700, *, ministry: Ministry, cancelled: bool = False) -> Event:
    event = Event(
        scheduling_period_id=11, ministry_id=ministry.id, event_date=NOV_15,
        event_kind=EVENT_KIND_SUNDAY_SERVICE,
    )
    event.id = event_id
    if cancelled:
        event.cancelled_at = DEACTIVATED
    return event


def _version(
    version_id: int = 500, *, status: str = SCHEDULE_VERSION_STATUS_REVIEW
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=400, scheduling_period_id=11, version_number=1, status=status,
    )
    version.id = version_id
    return version


def _requirement(
    requirement_id: int = 600, *, event: Event, role: MinistryRole, required_count: int = 1,
    event_date: datetime.date = NOV_15,
) -> ScheduleVersionRequirement:
    requirement = ScheduleVersionRequirement(
        schedule_version_id=500, event_id=event.id, event_date=event_date,
        ministry_role_id=role.id, scheduling_period_id=11, ministry_id=role.ministry_id,
        required_count=required_count,
    )
    requirement.id = requirement_id
    requirement.event = event
    requirement.ministry_role = role
    return requirement


def _assignment(
    assignment_id: int, *, requirement: ScheduleVersionRequirement,
    membership: MinistryMembership, is_override: bool = False,
) -> Assignment:
    assignment = Assignment(
        schedule_version_requirement_id=requirement.id,
        ministry_membership_id=membership.id,
        schedule_version_id=requirement.schedule_version_id,
        event_id=requirement.event_id, ministry_id=requirement.ministry_id,
        is_override=is_override,
        override_reason="Reason." if is_override else None,
    )
    assignment.id = assignment_id
    assignment.schedule_version_requirement = requirement
    assignment.ministry_membership = membership
    return assignment


def _override_audit(assignment_id: int, blockers, *, audit_id: int = 1) -> AuditEvent:
    """A real AuditEvent shaped exactly as Task 22 writes one."""
    after = {
        "schedule_version_requirement_id": 600,
        "ministry_membership_id": 118,
        "schedule_version_id": 500,
        "event_id": 700,
        "is_override": True,
        "override_reason": "Reason.",
    }
    if blockers is not _OMIT:
        after["overridden_blockers"] = blockers
    event = AuditEvent(
        actor_type="PERSON", actor_person_id=1, actor_label="Admin",
        action=ACTION_ASSIGNMENT_OVERRIDE_APPLIED, target_table="assignment",
        target_id=assignment_id, summary="Assigned with override",
        after_values=after,
    )
    event.id = audit_id
    return event


_OMIT = object()


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


def _stub(
    monkeypatch, *, requirements=(), assignments=(), audits=(), staleness=FRESH,
    qualified=True, availability_state=None, blocked=False,
    serving_limits=None,  # membership_id -> max_assignments; None = none set
    same_date_pairs=None,  # canonical (a_id, b_id) pairs; None = none set
    event_sequence=None,  # MinistryEventSequence; None = no event-gap rule
    member_group_caps=(),  # MemberGroupCapConfig; empty = no group is capped
    support_requirements=(),  # SupportRequirementConfig; empty = no rule
):
    """Configure the world this service reads, recording every session handed
    to a collaborator so the "same Session" claims can be checked.
    """
    import app.services.finalization_readiness as module

    seen: dict[str, list] = {"staleness": [], "conflict": [], "audit": []}

    monkeypatch.setattr(
        module, "_fetch_requirements",
        lambda session, *, schedule_version_id: list(requirements),
    )
    monkeypatch.setattr(
        module, "_fetch_assignments",
        lambda session, *, schedule_version_id: list(assignments),
    )

    qualification = None if qualified is None else type(
        "Q", (), {"is_qualified": qualified}
    )()
    monkeypatch.setattr(
        module, "_find_qualification",
        lambda session, *, ministry_membership_id, ministry_role_id: qualification,
    )

    availability = (
        None if availability_state is None
        else type("A", (), {"availability_state": availability_state})()
    )
    monkeypatch.setattr(
        module, "_find_availability",
        lambda session, *, ministry_membership_id, event_id: availability,
    )

    def fake_staleness(session, *, version):
        seen["staleness"].append(session)
        return staleness

    def fake_conflict(session, *, person_id, conflict_date, target_ministry_id):
        seen["conflict"].append((session, conflict_date, target_ministry_id))
        return SundayConflictResult(
            existing_commitments=(object(),) if blocked else (),
            authoritative_assignments=(),
        )

    def fake_execute(stmt, *args, **kwargs):
        seen["audit"].append(stmt)
        return _Result(audits)

    # Task 49: readiness now prefetches the current facts once per call
    # instead of querying per assignment, so the world these tests describe is
    # supplied there. Each lookup answers the same way for every key, exactly
    # as the per-assignment stubs it replaces did.
    class _SameAnswer(dict):
        def __init__(self, value):
            super().__init__()
            self._value = value

        def get(self, key, default=None):  # noqa: ARG002 - key is irrelevant
            return self._value

    monkeypatch.setattr(
        module,
        "_fetch_current_facts",
        lambda session, *, scheduling_period_id, schedule_version_id,
        assignments, requirements: (
            module._CurrentFacts(
                qualifications=_SameAnswer(qualification),
                availability=_SameAnswer(availability),
                serving_limits=_SameAnswer(None) if serving_limits is None
                else dict(serving_limits),
                # Left empty so the per-pair fallback runs and picks up the
                # conflict stub below, keeping these tests' conflict cases
                # exactly as they were.
                conflicts={},
                # Task 50: no linked pair configured, which is the world these
                # tests describe unless one names a pair explicitly.
                same_date_pairs=tuple(same_date_pairs or ()),
                # Task 71: no event-gap rule configured, which is the world
                # these tests describe. The gap gate has its own module
                # (``tests/test_services_readiness_event_gap.py``), where the
                # sequence is supplied explicitly.
                event_sequence=event_sequence,
                # Task 74: no member-group cap and no same-event support
                # requirement, which is the world these tests describe. Each
                # gate has its own module
                # (``tests/test_services_readiness_group_and_support.py``),
                # where the configuration is supplied explicitly.
                member_group_caps=tuple(member_group_caps),
                support_requirements=tuple(support_requirements),
            )
        ),
    )

    monkeypatch.setattr(module, "get_schedule_version_staleness", fake_staleness)
    monkeypatch.setattr(module, "get_person_sunday_conflicts", fake_conflict)
    # Task 47's serving maximum. ``None`` is "no maximum configured", which is
    # the world these tests describe unless one names a limit explicitly.
    monkeypatch.setattr(
        module, "get_serving_limit",
        lambda session, *, ministry_membership_id, scheduling_period_id: (
            (serving_limits or {}).get(ministry_membership_id)
        ),
    )
    monkeypatch.setattr(module, "_resolve_override_history", _real_history(fake_execute))
    return seen


#: Captured once, at import, so that calling ``_stub`` twice in one test
#: re-wraps the *genuine* implementation rather than the previous wrapper --
#: which would otherwise keep serving the first call's audit rows.
_REAL_RESOLVE_OVERRIDE_HISTORY = _finalization_module._resolve_override_history


def _real_history(fake_execute):
    """Keep the real history resolution but give it a fake ``execute``.

    The parsing logic is what these tests are about; only the database call
    underneath it is replaced.
    """

    def wrapper(session, *, assignments):
        proxy = type("S", (), {"execute": staticmethod(fake_execute)})()
        return _REAL_RESOLVE_OVERRIDE_HISTORY(proxy, assignments=assignments)

    return wrapper


def _codes(result: FinalizationReadinessResult) -> list[str]:
    return [issue.code for issue in result.issues]


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.fixture
def world():
    """One ministry, one role, one event, one member -- the common fixture."""
    ministry = _ministry()
    role = _role(ministry=ministry)
    event = _event(ministry=ministry)
    person = _person(42, "John")
    membership = _membership(118, person=person, ministry=ministry)
    requirement = _requirement(event=event, role=role)
    return {
        "ministry": ministry, "role": role, "event": event, "person": person,
        "membership": membership, "requirement": requirement, "version": _version(),
    }


# --------------------------------------------------------------------------
# 1-8 -- Baseline, staleness, completeness, absolute assignment state
# --------------------------------------------------------------------------


def test_01_fresh_and_fully_filled_is_ready(session, world, monkeypatch):
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[assignment])

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True
    assert result.issues == ()
    assert result.staleness is FRESH


def test_02_stale_snapshot_is_not_ready(session, world, monkeypatch):
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        staleness=STALE,
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_STALE_REQUIREMENT_SNAPSHOT in _codes(result)
    assert result.staleness is STALE


def test_03_unfilled_requirement_is_not_ready(session, world, monkeypatch):
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[])

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert _codes(result) == [ISSUE_UNFILLED_REQUIREMENT]
    issue = result.issues[0]
    assert issue.schedule_version_requirement_id == 600
    assert issue.assignment_id is None
    assert "needs 1 and has 0" in issue.message


def test_04_zero_requirements_and_fresh_is_ready(session, world, monkeypatch):
    _stub(monkeypatch, requirements=[], assignments=[])

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True
    assert result.issues == ()


def test_05_required_count_two_with_two_valid_members_is_ready(session, world, monkeypatch):
    requirement = _requirement(event=world["event"], role=world["role"], required_count=2)
    second = _membership(119, person=_person(43, "Mary"), ministry=world["ministry"])
    assignments = [
        _assignment(900, requirement=requirement, membership=world["membership"]),
        _assignment(901, requirement=requirement, membership=second),
    ]
    _stub(monkeypatch, requirements=[requirement], assignments=assignments)

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True


def test_06_inactive_membership_is_not_ready(session, world, monkeypatch):
    membership = _membership(
        118, person=world["person"], ministry=world["ministry"], deactivated=True
    )
    assignment = _assignment(900, requirement=world["requirement"], membership=membership)
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[assignment])

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_INACTIVE_MEMBERSHIP in _codes(result)
    assert result.issues[0].assignment_id == 900


def test_07_inactive_person_is_not_ready(session, world, monkeypatch):
    departed = _person(42, "John", deactivated=True)
    membership = _membership(118, person=departed, ministry=world["ministry"])
    assignment = _assignment(900, requirement=world["requirement"], membership=membership)
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[assignment])

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_INACTIVE_PERSON in _codes(result)


def test_08_cancelled_event_is_not_ready(session, world, monkeypatch):
    cancelled = _event(ministry=world["ministry"], cancelled=True)
    requirement = _requirement(event=cancelled, role=world["role"])
    assignment = _assignment(900, requirement=requirement, membership=world["membership"])
    _stub(monkeypatch, requirements=[requirement], assignments=[assignment])

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_CANCELLED_EVENT in _codes(result)


def test_08b_an_override_cannot_excuse_an_absolute_failure(session, world, monkeypatch):
    """Absolute checks stay absolute: a full override history does not cover a
    deactivated person, because Task 22 never allowed that either.
    """
    departed = _person(42, "John", deactivated=True)
    membership = _membership(118, person=departed, ministry=world["ministry"])
    assignment = _assignment(
        900, requirement=world["requirement"], membership=membership, is_override=True,
    )
    audit = _override_audit(900, sorted([
        BLOCKER_NOT_QUALIFIED, BLOCKER_UNAVAILABLE, BLOCKER_SUNDAY_CONFLICT,
        BLOCKER_ROLE_DEACTIVATED, BLOCKER_CAPACITY_FULL,
    ]))
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        audits=[audit],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_INACTIVE_PERSON in _codes(result)


# --------------------------------------------------------------------------
# 9-18 -- Current overridable conditions versus authorized history
# --------------------------------------------------------------------------


def test_09_normal_assignment_with_deactivated_role_is_not_ready(session, world, monkeypatch):
    role = _role(ministry=world["ministry"], deactivated=True)
    requirement = _requirement(event=world["event"], role=role)
    assignment = _assignment(900, requirement=requirement, membership=world["membership"])
    _stub(monkeypatch, requirements=[requirement], assignments=[assignment])

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert _codes(result) == [ISSUE_UNAUTHORIZED_BLOCKER]
    assert "deactivated" in result.issues[0].message
    assert "carries no override" in result.issues[0].message


def test_10_historical_role_deactivated_override_is_allowed(session, world, monkeypatch):
    role = _role(ministry=world["ministry"], deactivated=True)
    requirement = _requirement(event=world["event"], role=role)
    assignment = _assignment(
        900, requirement=requirement, membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[requirement], assignments=[assignment],
        audits=[_override_audit(900, [BLOCKER_ROLE_DEACTIVATED])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True


def test_11_normal_assignment_without_qualification_is_not_ready(session, world, monkeypatch):
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        qualified=None,  # no RoleQualification row at all
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert _codes(result) == [ISSUE_UNAUTHORIZED_BLOCKER]
    assert "not currently qualified" in result.issues[0].message


def test_11b_explicit_false_qualification_uses_the_same_blocker(session, world, monkeypatch):
    """One scheduling rule, exactly as Task 22 defined it -- no separate
    finalization code for "assessed no" versus "never assessed".
    """
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        qualified=False,
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert _codes(result) == [ISSUE_UNAUTHORIZED_BLOCKER]
    assert "not currently qualified" in result.issues[0].message


def test_12_historical_not_qualified_override_is_allowed(session, world, monkeypatch):
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        qualified=False, audits=[_override_audit(900, [BLOCKER_NOT_QUALIFIED])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True


def test_13_normal_assignment_marked_unavailable_is_not_ready(session, world, monkeypatch):
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_UNAVAILABLE,
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert "marked unavailable" in result.issues[0].message


def test_14_historical_unavailable_override_is_allowed(session, world, monkeypatch):
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_UNAVAILABLE,
        audits=[_override_audit(900, [BLOCKER_UNAVAILABLE])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True


@pytest.mark.parametrize("state", [None, AVAILABILITY_AVAILABLE, AVAILABILITY_BACKUP])
def test_15_no_availability_row_or_available_is_allowed(session, world, monkeypatch, state):
    """A missing row is "no response", not a block -- the same reading Task 22
    uses. AVAILABLE is likewise no blocker, and so is BACKUP (Task 52): only
    the explicit UNAVAILABLE state is ever a readiness issue.
    """
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=state,
    )

    assert get_finalization_readiness(session, version=world["version"]).is_ready is True


def test_15b_backup_assignment_needs_no_override_audit_to_be_ready(session, world, monkeypatch):
    """Task 52, pinned explicitly and separately from the parametrized case
    above: a BACKUP-availability assignment is ready with **no**
    override/audit trail at all -- unlike UNAVAILABLE (test 14), which is only
    ready when a matching override audit exists. BACKUP never enters the
    overridable-blocker vocabulary in the first place.
    """
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_BACKUP, audits=[],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True
    assert result.issues == ()


def test_16_a_cross_ministry_conflict_on_a_normal_assignment_is_not_ready(
    session, world, monkeypatch
):
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        blocked=True,
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert _codes(result) == [ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT]
    assert "one ministry on the same day" in result.issues[0].message


def test_17_a_historical_cross_ministry_override_is_NOT_allowed(
    session, world, monkeypatch
):
    """**This test asserted the opposite until Task 79's final correction.**

    An audit row naming ``sunday_conflict`` records a decision somebody really
    made while the rule was overridable. It is still truthful history -- and it
    no longer excuses anything, because one Person serving two ministries on
    one day is a hard church-wide rule. The version cannot be finalized until
    one of the two assignments is removed.
    """
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        blocked=True, audits=[_override_audit(900, [BLOCKER_SUNDAY_CONFLICT])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT in _codes(result)
    # And it is not reported as corrupt history: the payload parsed fine, it
    # simply authorizes nothing now.
    assert ISSUE_INVALID_OVERRIDE_PAYLOAD not in _codes(result)
    assert ISSUE_MISSING_OVERRIDE_AUDIT not in _codes(result)


def test_17b_an_unaudited_cross_ministry_conflict_is_also_rejected(
    session, world, monkeypatch
):
    """A row claiming ``is_override=True`` with no audit at all -- a hand-edit
    or a bad migration -- is refused twice over: once for the conflict itself,
    once for the missing authorization."""
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        blocked=True, audits=[],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT in _codes(result)
    assert ISSUE_MISSING_OVERRIDE_AUDIT in _codes(result)


def test_17c_an_override_for_another_blocker_still_covers_that_blocker(
    session, world, monkeypatch
):
    """**The global override mechanism is intact.** Making one rule absolute
    must not quietly disable the rest: an override granted for unavailability
    still covers unavailability, and the version is ready.
    """
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_UNAVAILABLE,
        audits=[_override_audit(900, [BLOCKER_UNAVAILABLE])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True
    assert result.issues == ()


def test_18_an_override_for_another_blocker_does_not_cover_a_cross_ministry_conflict(
    session, world, monkeypatch
):
    """The load-bearing rule, restated for the absolute case: the override was
    granted for unavailability, and a cross-ministry conflict has since
    appeared. Nobody authorized that -- and nobody could have.
    """
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        blocked=True, availability_state=AVAILABILITY_UNAVAILABLE,
        audits=[_override_audit(900, [BLOCKER_UNAVAILABLE])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    # The conflict alone -- the covered unavailability is not reported, which
    # is what shows the rest of the override system still works.
    assert _codes(result) == [ISSUE_CROSS_MINISTRY_SUNDAY_CONFLICT]


def test_18b_multiple_uncovered_blockers_are_each_reported_deterministically(
    session, world, monkeypatch
):
    role = _role(ministry=world["ministry"], deactivated=True)
    requirement = _requirement(event=world["event"], role=role)
    assignment = _assignment(900, requirement=requirement, membership=world["membership"])
    _stub(
        monkeypatch, requirements=[requirement], assignments=[assignment],
        qualified=False, availability_state=AVAILABILITY_UNAVAILABLE, blocked=True,
    )

    first = get_finalization_readiness(session, version=world["version"])
    second = get_finalization_readiness(session, version=world["version"])

    assert len(first.issues) == 4  # every current blocker except aggregate capacity
    assert [i.message for i in first.issues] == [i.message for i in second.issues]


# --------------------------------------------------------------------------
# 19-22 -- Override history integrity
# --------------------------------------------------------------------------


def test_19_override_without_an_audit_event_is_not_ready(session, world, monkeypatch):
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment], audits=[],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_MISSING_OVERRIDE_AUDIT in _codes(result)


def test_19b_two_override_audits_are_ambiguous_and_not_ready(session, world, monkeypatch):
    # An *overridable* blocker, so what this test exercises is the ambiguity
    # rather than the now-absolute cross-ministry rule, which would refuse the
    # version on its own and mask the thing under test.
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_UNAVAILABLE,
        audits=[
            _override_audit(900, [BLOCKER_UNAVAILABLE], audit_id=1),
            _override_audit(900, [BLOCKER_UNAVAILABLE], audit_id=2),
        ],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    codes = _codes(result)
    assert ISSUE_AMBIGUOUS_OVERRIDE_AUDIT in codes
    # An untrustworthy history authorizes nothing, so the conflict is reported too.
    assert ISSUE_UNAUTHORIZED_BLOCKER in codes


def test_20_override_audit_without_the_payload_key_is_not_ready(session, world, monkeypatch):
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        audits=[_override_audit(900, _OMIT)],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_INVALID_OVERRIDE_PAYLOAD in _codes(result)


@pytest.mark.parametrize(
    "malformed",
    [
        "sunday_conflict",              # a bare string, not a list
        [None],                          # non-string element
        [123],                           # non-string element
        ["not_a_real_blocker"],         # outside the bounded vocabulary
        [BLOCKER_UNAVAILABLE, "bogus"], # one good code cannot rescue the rest
        {"sunday_conflict": True},      # an object, not an array
    ],
)
def test_21_malformed_overridden_blockers_is_not_ready(session, world, monkeypatch, malformed):
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_UNAVAILABLE,
        audits=[_override_audit(900, malformed)],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    codes = _codes(result)
    assert ISSUE_INVALID_OVERRIDE_PAYLOAD in codes
    # Nothing is authorized by a payload that cannot be trusted.
    assert ISSUE_UNAUTHORIZED_BLOCKER in codes


def test_22_historical_blockers_that_no_longer_apply_are_not_failures(session, world, monkeypatch):
    """Overridden for unavailability; the member has since answered AVAILABLE.
    True history, no current problem.
    """
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_AVAILABLE,
        audits=[_override_audit(900, [BLOCKER_UNAVAILABLE, BLOCKER_SUNDAY_CONFLICT])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True
    assert result.issues == ()


def test_22b_a_normal_assignment_is_never_given_override_history(session, world, monkeypatch):
    """Even if a stray override audit exists for its id, an assignment with
    ``is_override=False`` authorizes nothing -- its history is not consulted.
    """
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_UNAVAILABLE,
        audits=[_override_audit(900, [BLOCKER_UNAVAILABLE])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert "carries no override" in result.issues[0].message


# --------------------------------------------------------------------------
# 23-27 -- Capacity and overfill
# --------------------------------------------------------------------------


def test_23_exactly_required_count_is_ready(session, world, monkeypatch):
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[assignment])

    assert get_finalization_readiness(session, version=world["version"]).is_ready is True


def _overfilled(world, *, required_count, members, override_ids):
    """``members`` assignments on one requirement, the listed ids marked as
    capacity overrides."""
    requirement = _requirement(
        event=world["event"], role=world["role"], required_count=required_count
    )
    assignments = []
    for index in range(members):
        assignment_id = 900 + index
        membership = _membership(
            118 + index, person=_person(42 + index, f"Member {index}"),
            ministry=world["ministry"],
        )
        assignments.append(
            _assignment(
                assignment_id, requirement=requirement, membership=membership,
                is_override=assignment_id in override_ids,
            )
        )
    audits = [_override_audit(i, [BLOCKER_CAPACITY_FULL]) for i in override_ids]
    return requirement, assignments, audits


def test_24_overfill_with_enough_capacity_override_history_is_allowed(session, world, monkeypatch):
    requirement, assignments, audits = _overfilled(
        world, required_count=2, members=3, override_ids=[902],
    )
    _stub(monkeypatch, requirements=[requirement], assignments=assignments, audits=audits)

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True


def test_25_overfill_without_capacity_override_history_is_not_ready(session, world, monkeypatch):
    requirement, assignments, audits = _overfilled(
        world, required_count=2, members=3, override_ids=[],
    )
    _stub(monkeypatch, requirements=[requirement], assignments=assignments, audits=audits)

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_UNAUTHORIZED_OVERFILL in _codes(result)
    assert "only 0 of the 1 extra" in result.issues[0].message


def test_25b_an_override_for_another_blocker_does_not_authorize_overfill(
    session, world, monkeypatch
):
    requirement, assignments, _ = _overfilled(
        world, required_count=2, members=3, override_ids=[902],
    )
    _stub(
        monkeypatch, requirements=[requirement], assignments=assignments,
        # The override exists, but it was granted for unavailability.
        audits=[_override_audit(902, [BLOCKER_UNAVAILABLE])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_UNAUTHORIZED_OVERFILL in _codes(result)


def test_26_excess_of_two_requires_two_capacity_authorized_assignments(session, world, monkeypatch):
    # One authorization for two extra people is not enough...
    requirement, assignments, audits = _overfilled(
        world, required_count=2, members=4, override_ids=[903],
    )
    _stub(monkeypatch, requirements=[requirement], assignments=assignments, audits=audits)
    short = get_finalization_readiness(session, version=world["version"])
    assert short.is_ready is False
    assert "only 1 of the 2 extra" in short.issues[0].message

    # ...two is.
    requirement, assignments, audits = _overfilled(
        world, required_count=2, members=4, override_ids=[902, 903],
    )
    _stub(monkeypatch, requirements=[requirement], assignments=assignments, audits=audits)
    enough = get_finalization_readiness(session, version=world["version"])
    assert enough.is_ready is True


def test_26b_capacity_authorization_is_counted_not_matched_by_position(session, world, monkeypatch):
    """Which rows carry the authorization must not matter -- only how many.
    The same three assignments are accepted whether the first or the last one
    holds the capacity override.
    """
    for authorized_id in (900, 901, 902):
        requirement, assignments, audits = _overfilled(
            world, required_count=2, members=3, override_ids=[authorized_id],
        )
        _stub(monkeypatch, requirements=[requirement], assignments=assignments, audits=audits)

        assert get_finalization_readiness(session, version=world["version"]).is_ready is True


def test_27_underfill_is_still_not_ready_even_with_a_capacity_override_elsewhere(
    session, world, monkeypatch
):
    """Two requirements: one overfilled with proper authorization, one short.
    The authorized overfill must not offset the shortfall.
    """
    filled = _requirement(600, event=world["event"], role=world["role"], required_count=1)
    other_role = _role(13, "Setup Assist", ministry=world["ministry"])
    short = _requirement(601, event=world["event"], role=other_role, required_count=2)
    member_b = _membership(119, person=_person(43, "Mary"), ministry=world["ministry"])
    member_c = _membership(120, person=_person(44, "Ann"), ministry=world["ministry"])
    assignments = [
        _assignment(900, requirement=filled, membership=world["membership"]),
        _assignment(901, requirement=filled, membership=member_b, is_override=True),
        _assignment(902, requirement=short, membership=member_c),
    ]
    _stub(
        monkeypatch, requirements=[filled, short], assignments=assignments,
        audits=[_override_audit(901, [BLOCKER_CAPACITY_FULL])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert _codes(result) == [ISSUE_UNFILLED_REQUIREMENT]
    assert result.issues[0].schedule_version_requirement_id == 601


def test_capacity_blocker_is_not_reported_per_assignment_on_a_full_requirement(
    session, world, monkeypatch
):
    """Aggregate capacity is a property of the requirement, so a legitimately
    filled requirement must not make each of its assignments report a blocker.
    """
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[assignment])

    result = get_finalization_readiness(session, version=world["version"])

    assert result.issues == ()


# --------------------------------------------------------------------------
# 28-33 -- Read-only, session, and context
# --------------------------------------------------------------------------


def test_28_creates_no_audit_event_and_leaves_the_session_clean(session, world, monkeypatch):
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[assignment], blocked=True)

    get_finalization_readiness(session, version=world["version"])

    assert [obj for obj in session.new if isinstance(obj, AuditEvent)] == []
    assert len(session.new) == 0
    assert len(session.dirty) == 0
    assert len(session.deleted) == 0


def test_29_30_31_never_flushes_commits_or_rolls_back(session, world, monkeypatch):
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[assignment])

    get_finalization_readiness(session, version=world["version"])

    assert session.flush_calls == 0
    assert session.commit_calls == 0
    assert session.rollback_calls == 0
    assert session.add_calls == 0
    assert session.delete_calls == 0


def test_32_the_conflict_check_receives_the_supplied_session_and_snapshot_date(
    session, world, monkeypatch
):
    moved_event = _event(ministry=world["ministry"])
    moved_event.event_date = datetime.date(2026, 11, 22)  # current row moved
    requirement = _requirement(event=moved_event, role=world["role"], event_date=NOV_15)
    assignment = _assignment(900, requirement=requirement, membership=world["membership"])
    seen = _stub(monkeypatch, requirements=[requirement], assignments=[assignment])

    get_finalization_readiness(session, version=world["version"])

    assert len(seen["conflict"]) == 1
    used_session, conflict_date, target_ministry_id = seen["conflict"][0]
    assert used_session is session
    assert conflict_date == NOV_15  # the snapshot, never the moved Event row
    assert target_ministry_id == world["requirement"].ministry_id


def test_33_the_staleness_check_receives_the_supplied_session(session, world, monkeypatch):
    seen = _stub(monkeypatch, requirements=[], assignments=[])

    get_finalization_readiness(session, version=world["version"])

    assert seen["staleness"] == [session]


@pytest.mark.parametrize(
    "status",
    [SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW, SCHEDULE_VERSION_STATUS_FINALIZED],
)
def test_any_status_can_be_inspected_descriptively(session, world, monkeypatch, status):
    """Requiring REVIEW is Task 27's rule, not this validator's."""
    assignment = _assignment(900, requirement=world["requirement"], membership=world["membership"])
    _stub(monkeypatch, requirements=[world["requirement"]], assignments=[assignment])

    result = get_finalization_readiness(session, version=_version(status=status))

    assert result.is_ready is True


@pytest.mark.parametrize("missing", ["id", "schedule_id", "scheduling_period_id"])
def test_insufficient_version_context_is_refused(session, world, monkeypatch, missing):
    _stub(monkeypatch, requirements=[], assignments=[])
    version = _version()
    setattr(version, missing, None)

    with pytest.raises(InvalidOperationError):
        get_finalization_readiness(session, version=version)


def test_result_and_issue_types_are_frozen():
    issue = FinalizationIssue(code="X", message="m")
    result = FinalizationReadinessResult(staleness=FRESH, issues=(issue,))

    with pytest.raises(Exception):
        result.issues = ()
    with pytest.raises(Exception):
        issue.code = "Y"


def test_is_ready_requires_both_freshness_and_no_issues():
    assert FinalizationReadinessResult(staleness=FRESH, issues=()).is_ready is True
    assert FinalizationReadinessResult(staleness=STALE, issues=()).is_ready is False
    assert FinalizationReadinessResult(
        staleness=FRESH, issues=(FinalizationIssue(code="X", message="m"),)
    ).is_ready is False


# --------------------------------------------------------------------------
# Query shape (compiled SQL)
# --------------------------------------------------------------------------


def test_requirements_query_is_scoped_to_the_version_and_ordered():
    compiled = _compile(_requirements_statement(500))

    assert "schedule_version_requirement.schedule_version_id = 500" in compiled
    assert "ORDER BY schedule_version_requirement.id" in compiled


def test_assignments_query_is_scoped_to_the_version_and_ordered():
    compiled = _compile(_assignments_statement(500))

    assert "assignment.schedule_version_id = 500" in compiled
    assert "ORDER BY assignment.id" in compiled


def test_override_audit_query_matches_table_action_and_ids():
    compiled = _compile(_override_audit_statement([900, 901]))

    assert "audit_event.target_table = 'assignment'" in compiled
    assert "audit_event.action = 'ASSIGNMENT_OVERRIDE_APPLIED'" in compiled
    assert "audit_event.target_id IN (900, 901)" in compiled


def test_qualification_and_availability_queries_match_task_22s_shape():
    qualification = _compile(_qualification_statement(118, 12))
    availability = _compile(_availability_statement(118, 700))

    assert "role_qualification.ministry_membership_id = 118" in qualification
    assert "role_qualification.ministry_role_id = 12" in qualification
    assert "availability.ministry_membership_id = 118" in availability
    assert "availability.event_id = 700" in availability


# --------------------------------------------------------------------------
# 34-35 -- The extracted blocker vocabulary
# --------------------------------------------------------------------------


def test_34_assignment_still_exposes_the_same_private_blocker_names():
    """Task 22's module-level names still exist and still mean the same
    thing, so its behavior and tests are unchanged by the extraction.
    """
    import app.services.assignment as assignment_module
    from app.services import assignment_policy

    assert assignment_module._BLOCKER_ROLE_DEACTIVATED is assignment_policy.BLOCKER_ROLE_DEACTIVATED
    assert assignment_module._BLOCKER_NOT_QUALIFIED is assignment_policy.BLOCKER_NOT_QUALIFIED
    assert assignment_module._BLOCKER_UNAVAILABLE is assignment_policy.BLOCKER_UNAVAILABLE
    assert assignment_module._BLOCKER_SUNDAY_CONFLICT is assignment_policy.BLOCKER_SUNDAY_CONFLICT
    assert assignment_module._BLOCKER_CAPACITY_FULL is assignment_policy.BLOCKER_CAPACITY_FULL
    assert assignment_module._BLOCKER_DESCRIPTIONS is assignment_policy.BLOCKER_DESCRIPTIONS


def test_35_the_persisted_blocker_strings_are_unchanged():
    """These exact values live in AuditEvent history. Renaming one would
    silently un-authorize every override already recorded with it.
    """
    from app.services import assignment_policy

    assert assignment_policy.BLOCKER_ROLE_DEACTIVATED == "role_deactivated"
    assert assignment_policy.BLOCKER_NOT_QUALIFIED == "not_qualified"
    assert assignment_policy.BLOCKER_UNAVAILABLE == "unavailable"
    assert assignment_policy.BLOCKER_SUNDAY_CONFLICT == "sunday_conflict"
    assert assignment_policy.BLOCKER_CAPACITY_FULL == "capacity_full"

    # **Every string survives; what changed is which of them authorize.**
    # ``sunday_conflict`` left the overridable set in Task 79's correction and
    # stayed in the known vocabulary, because rows written while it *was*
    # overridable must keep parsing as intact history rather than corruption.
    assert assignment_policy.OVERRIDABLE_BLOCKERS == frozenset(
        {"role_deactivated", "not_qualified", "unavailable", "capacity_full"}
    )
    assert assignment_policy.KNOWN_BLOCKERS == frozenset(
        {"role_deactivated", "not_qualified", "unavailable", "sunday_conflict", "capacity_full"}
    )
    assert assignment_policy.BLOCKER_SUNDAY_CONFLICT not in (
        assignment_policy.OVERRIDABLE_BLOCKERS
    )


def test_35b_a_historical_sunday_conflict_payload_parses_but_authorizes_nothing(
    session, world, monkeypatch
):
    """The two halves of the split, seen from the outside.

    The row is intact history -- no INVALID_OVERRIDE_PAYLOAD -- and the code it
    names excuses nothing, so the unavailability it does *not* name is still
    reported as unauthorized.
    """
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"], is_override=True,
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        availability_state=AVAILABILITY_UNAVAILABLE,
        audits=[_override_audit(900, [BLOCKER_SUNDAY_CONFLICT])],
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    assert ISSUE_INVALID_OVERRIDE_PAYLOAD not in _codes(result)
    assert _codes(result) == [ISSUE_UNAUTHORIZED_BLOCKER]
    assert "marked unavailable" in result.issues[0].message


def test_the_policy_module_holds_no_evaluation_logic():
    """Constants and descriptions only -- not the start of a rule engine."""
    from app.services import assignment_policy

    functions = [
        name for name, value in vars(assignment_policy).items()
        if callable(value) and not name.startswith("__")
    ]
    assert functions == []


# --------------------------------------------------------------------------
# 29-30 -- the person-period serving maximum (Task 47)
#
# Gate 4. Evaluated against the *currently configured* maximum and counted
# within *this* version, so it catches assignments made before a limit
# existed, a limit lowered after the fact, and rows carried forward from a
# predecessor -- because it asks what the version contains now, not how it got
# that way.
# --------------------------------------------------------------------------


def test_29_a_version_within_the_serving_maximum_is_ready(
    session, world, monkeypatch
):
    assignment = _assignment(
        900, requirement=world["requirement"], membership=world["membership"]
    )
    _stub(
        monkeypatch, requirements=[world["requirement"]], assignments=[assignment],
        serving_limits={world["membership"].id: 4},
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is True
    assert result.issues == ()


def test_29b_lowering_the_maximum_below_the_assignment_count_blocks_finalization(
    session, world, monkeypatch
):
    """The edge case Task 47 exists to handle safely.

    A head reduces somebody's maximum after a draft was built. Nothing is
    deleted -- and equally, the schedule that now breaks the agreed number
    cannot be finalized.
    """
    requirement = _requirement(
        event=world["event"], role=world["role"], required_count=3
    )
    assignments = [
        _assignment(900 + i, requirement=requirement, membership=world["membership"])
        for i in range(3)
    ]
    _stub(
        monkeypatch, requirements=[requirement], assignments=assignments,
        serving_limits={world["membership"].id: 2},
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert result.is_ready is False
    codes = [issue.code for issue in result.issues]
    assert ISSUE_EXCEEDS_SERVING_LIMIT in codes
    issue = next(i for i in result.issues if i.code == ISSUE_EXCEEDS_SERVING_LIMIT)
    assert "3 assignments" in issue.message
    assert "maximum for this period is 2" in issue.message


def test_29c_one_issue_per_member_not_one_per_assignment(
    session, world, monkeypatch
):
    """The rule is about a person's total, so the report is too.

    Attaching it to an arbitrary one of their assignments would suggest that
    assignment is the problem, when what is wrong is the sum.
    """
    requirement = _requirement(
        event=world["event"], role=world["role"], required_count=4
    )
    assignments = [
        _assignment(900 + i, requirement=requirement, membership=world["membership"])
        for i in range(4)
    ]
    _stub(
        monkeypatch, requirements=[requirement], assignments=assignments,
        serving_limits={world["membership"].id: 1},
    )

    result = get_finalization_readiness(session, version=world["version"])

    over = [i for i in result.issues if i.code == ISSUE_EXCEEDS_SERVING_LIMIT]
    assert len(over) == 1


def test_29d_an_override_does_not_authorize_exceeding_the_maximum(
    session, world, monkeypatch
):
    """No historical ``overridden_blockers`` payload can excuse this issue.

    The serving maximum was never one of Task 22's bounded overridable blockers, so
    no override ever authorized it -- not even one applied to these very rows.
    """
    requirement = _requirement(
        event=world["event"], role=world["role"], required_count=2
    )
    assignments = [
        _assignment(
            900 + i, requirement=requirement, membership=world["membership"],
            is_override=True,
        )
        for i in range(2)
    ]
    _stub(
        monkeypatch, requirements=[requirement], assignments=assignments,
        serving_limits={world["membership"].id: 1},
    )

    result = get_finalization_readiness(session, version=world["version"])

    assert ISSUE_EXCEEDS_SERVING_LIMIT in [i.code for i in result.issues]
    assert result.is_ready is False


def test_30_no_assignment_is_mutated_or_removed_by_the_check(
    session, world, monkeypatch
):
    """Reporting is the whole job; repairing is a decision for a person."""
    requirement = _requirement(
        event=world["event"], role=world["role"], required_count=3
    )
    assignments = [
        _assignment(900 + i, requirement=requirement, membership=world["membership"])
        for i in range(3)
    ]
    _stub(
        monkeypatch, requirements=[requirement], assignments=assignments,
        serving_limits={world["membership"].id: 1},
    )

    get_finalization_readiness(session, version=world["version"])

    # ReadinessSession raises on add/delete/flush, so reaching this line at all
    # proves none was attempted; the counters say so explicitly.
    assert session.delete_calls == 0
    assert session.add_calls == 0
    assert session.flush_calls == 0
    assert all(a.id is not None for a in assignments)


def test_30b_a_member_with_no_configured_maximum_raises_no_issue(
    session, world, monkeypatch
):
    requirement = _requirement(
        event=world["event"], role=world["role"], required_count=5
    )
    assignments = [
        _assignment(900 + i, requirement=requirement, membership=world["membership"])
        for i in range(5)
    ]
    _stub(monkeypatch, requirements=[requirement], assignments=assignments)

    result = get_finalization_readiness(session, version=world["version"])

    assert ISSUE_EXCEEDS_SERVING_LIMIT not in [i.code for i in result.issues]


# ==========================================================================
# 30-33 (Task 50) -- the linked-pair same-date exclusion gate
#
# Reported, never repaired: the whole design of this gate is that a head who
# records a pair rule after a draft exists gets an honest blocker instead of a
# silently deleted assignment. Every person here is synthetic, and no issue
# message says -- or could say -- why two members are linked.
# ==========================================================================


def _two_members_on_one_date(*, second_event_id: int = 700):
    """Two synthetic Setup members both assigned on 15 November.

    ``second_event_id`` lets a caller put the second assignment at a *different
    event on the same date*, which is the case a "same event" implementation
    would miss.
    """
    ministry = _ministry()
    role = _role(ministry=ministry)
    other_role = _role(13, "Setup Support", ministry=ministry)
    event = _event(700, ministry=ministry)
    other_event = _event(second_event_id, ministry=ministry)
    membership_a = _membership(
        200, person=_person(50, "Volunteer A"), ministry=ministry
    )
    membership_b = _membership(
        201, person=_person(51, "Volunteer B"), ministry=ministry
    )
    requirement_a = _requirement(600, event=event, role=role)
    requirement_b = _requirement(601, event=other_event, role=other_role)
    return {
        "requirements": [requirement_a, requirement_b],
        "assignments": [
            _assignment(900, requirement=requirement_a, membership=membership_a),
            _assignment(901, requirement=requirement_b, membership=membership_b),
        ],
    }


def test_30_an_existing_pair_and_date_violation_blocks_readiness(session, monkeypatch):
    world = _two_members_on_one_date()
    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=world["assignments"],
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())

    assert ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT in _codes(result)
    assert not result.is_ready


def test_30b_no_configured_pair_means_no_issue(session, monkeypatch):
    """Absence of a rule is not a hint. The same two assignments are fine."""
    world = _two_members_on_one_date()
    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=world["assignments"],
        same_date_pairs=[],
    )

    result = get_finalization_readiness(session, version=_version())

    assert ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT not in _codes(result)
    assert result.is_ready


def test_30c_a_pair_on_different_dates_raises_no_issue(session, monkeypatch):
    """The rule is about a coincidence, so two dates is simply not one."""
    ministry = _ministry()
    role = _role(ministry=ministry)
    event = _event(700, ministry=ministry)
    later_event = _event(701, ministry=ministry)
    membership_a = _membership(
        200, person=_person(50, "Volunteer A"), ministry=ministry
    )
    membership_b = _membership(
        201, person=_person(51, "Volunteer B"), ministry=ministry
    )
    requirement_a = _requirement(600, event=event, role=role)
    requirement_b = _requirement(
        601, event=later_event, role=role, event_date=NOV_15 + datetime.timedelta(days=7)
    )
    _stub(
        monkeypatch,
        requirements=[requirement_a, requirement_b],
        assignments=[
            _assignment(900, requirement=requirement_a, membership=membership_a),
            _assignment(901, requirement=requirement_b, membership=membership_b),
        ],
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())

    assert ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT not in _codes(result)


def test_30d_two_events_on_one_date_still_conflict(session, monkeypatch):
    """The case a "not both in one event" gate would wave through."""
    world = _two_members_on_one_date(second_event_id=701)
    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=world["assignments"],
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())

    assert ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT in _codes(result)


def test_31_adding_the_rule_after_the_assignments_exist_blocks_without_deleting(
    session, monkeypatch
):
    """The motivating edge case, stated exactly.

    A draft already contains both members on 15 November. The head then
    records the exclusion. Readiness must report the conflict, and **both
    assignments must still be there afterwards** -- this module reports and
    never repairs.
    """
    world = _two_members_on_one_date()
    assignments = world["assignments"]
    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=assignments,
        # The rule is configured *now*, after the rows were created.
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())

    assert ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT in _codes(result)
    assert not result.is_ready
    # Nothing removed, nothing flagged, nothing mutated. The session itself
    # raises on add/delete/flush, so a repair attempt could not even be quiet.
    assert [a.id for a in assignments] == [900, 901]
    assert session.delete_calls == 0
    assert session.add_calls == 0
    assert session.flush_calls == 0


def test_31b_an_override_does_not_authorize_a_pair_conflict(session, monkeypatch):
    """The exclusion is not one of Task 22's bounded blockers, so no stored
    ``overridden_blockers`` payload can excuse it.
    """
    world = _two_members_on_one_date()
    world["assignments"][0].is_override = True
    world["assignments"][0].override_reason = "Reason."
    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=world["assignments"],
        audits=[
            _override_audit(900, [BLOCKER_UNAVAILABLE, BLOCKER_SUNDAY_CONFLICT])
        ],
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())

    assert ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT in _codes(result)


def test_32_clearing_the_rule_removes_that_specific_blocker(session, monkeypatch):
    """The remedy a head may choose instead of editing the schedule.

    Same assignments, same everything -- only the constraint is gone, and the
    version becomes ready. Nothing else about the verdict changes, which is
    what makes this issue attributable to the rule and not a side effect.
    """
    world = _two_members_on_one_date()
    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=world["assignments"],
        same_date_pairs=[(200, 201)],
    )
    blocked = get_finalization_readiness(session, version=_version())
    assert ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT in _codes(blocked)

    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=world["assignments"],
        same_date_pairs=[],
    )
    cleared = get_finalization_readiness(session, version=_version())

    assert ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT not in _codes(cleared)
    assert cleared.is_ready


def test_33_one_issue_per_pair_and_date_not_one_per_assignment(session, monkeypatch):
    """Two positions on the date for one member, two for the other, and the
    problem is still *one* coincidence -- so it is reported once.

    A naive per-assignment reading would emit four issues for one problem, and
    would imply that some particular row is the wrong one, when which row a
    head removes is exactly the decision this module leaves to them.
    """
    ministry = _ministry()
    lead = _role(12, "Setup Lead", ministry=ministry)
    support = _role(13, "Setup Support", ministry=ministry)
    morning = _event(700, ministry=ministry)
    evening = _event(701, ministry=ministry)
    membership_a = _membership(
        200, person=_person(50, "Volunteer A"), ministry=ministry
    )
    membership_b = _membership(
        201, person=_person(51, "Volunteer B"), ministry=ministry
    )
    requirements = [
        _requirement(600, event=morning, role=lead),
        _requirement(601, event=morning, role=support),
        _requirement(602, event=evening, role=lead),
        _requirement(603, event=evening, role=support),
    ]
    _stub(
        monkeypatch,
        requirements=requirements,
        assignments=[
            _assignment(900, requirement=requirements[0], membership=membership_a),
            _assignment(901, requirement=requirements[1], membership=membership_a),
            _assignment(902, requirement=requirements[2], membership=membership_b),
            _assignment(903, requirement=requirements[3], membership=membership_b),
        ],
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())

    pair_issues = [
        issue
        for issue in result.issues
        if issue.code == ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT
    ]
    assert len(pair_issues) == 1


def test_33b_two_conflicting_dates_produce_one_issue_each(session, monkeypatch):
    ministry = _ministry()
    role = _role(ministry=ministry)
    first = _event(700, ministry=ministry)
    second = _event(701, ministry=ministry)
    later = NOV_15 + datetime.timedelta(days=7)
    membership_a = _membership(
        200, person=_person(50, "Volunteer A"), ministry=ministry
    )
    membership_b = _membership(
        201, person=_person(51, "Volunteer B"), ministry=ministry
    )
    requirements = [
        _requirement(600, event=first, role=role, required_count=2),
        _requirement(601, event=second, role=role, required_count=2, event_date=later),
    ]
    _stub(
        monkeypatch,
        requirements=requirements,
        assignments=[
            _assignment(900, requirement=requirements[0], membership=membership_a),
            _assignment(901, requirement=requirements[0], membership=membership_b),
            _assignment(902, requirement=requirements[1], membership=membership_a),
            _assignment(903, requirement=requirements[1], membership=membership_b),
        ],
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())

    pair_issues = [
        issue
        for issue in result.issues
        if issue.code == ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT
    ]
    assert len(pair_issues) == 2
    assert sorted(NOV_15.isoformat() in i.message for i in pair_issues) == [False, True]


def test_33c_the_issue_names_both_people_and_the_date_and_nothing_personal(
    session, monkeypatch
):
    """Enough context for a head to repair the schedule, and no more."""
    world = _two_members_on_one_date()
    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=world["assignments"],
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())
    issue = next(
        i for i in result.issues if i.code == ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT
    )

    assert "Volunteer A" in issue.message
    assert "Volunteer B" in issue.message
    assert NOV_15.isoformat() in issue.message
    assert "remove one of the assignments" in issue.message
    for word in (
        "spouse", "husband", "wife", "partner", "married", "couple",
        "household", "family", "sibling", "relationship",
    ):
        assert word not in issue.message.lower()


def test_33d_the_pair_issue_is_version_wide_not_attached_to_one_row(
    session, monkeypatch
):
    """The problem is the coincidence, so neither row is singled out."""
    world = _two_members_on_one_date()
    _stub(
        monkeypatch,
        requirements=world["requirements"],
        assignments=world["assignments"],
        same_date_pairs=[(200, 201)],
    )

    result = get_finalization_readiness(session, version=_version())
    issue = next(
        i for i in result.issues if i.code == ISSUE_SAME_DATE_LINKED_MEMBER_CONFLICT
    )

    assert issue.assignment_id is None
    assert issue.schedule_version_requirement_id is None
