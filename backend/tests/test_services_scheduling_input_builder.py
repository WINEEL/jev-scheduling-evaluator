"""Scheduling-input builder tests (Task 30).

Offline: no PostgreSQL, no Neon, no network.

**Test strategy** follows Tasks 21-29's precedent for a read-only service: the
builder's own fetch helpers and the two reused public services
(``get_schedule_version_staleness``, ``get_sunday_conflicts_for``, imported
by name into this module's namespace) are monkeypatched and record the session
they were handed, while each query is compiled against the PostgreSQL dialect
and inspected as text. Session discipline uses a real, unbound ``Session``
subclass that raises on every mutation method -- nothing is permitted, because
this builder only reads.

What only a real database can show -- that these queries actually return these
rows, and that a real church-wide conflict lands in ``blocked_dates`` -- is
proven in ``tests/integration/test_pg_scheduling_input.py``.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.audit import AuditEvent
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    ScheduleVersion,
)
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_BACKUP,
    AVAILABILITY_UNAVAILABLE,
    SchedulingPeriod,
)
from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    LinkedMembershipPair,
    RequirementInput,
    SchedulingInput,
)
from app.services.errors import InvalidOperationError
from app.services.schedule_staleness import (
    RequirementFingerprint,
    ScheduleVersionStalenessResult,
)
from app.services.scheduling_input_builder import (
    _availability_statement,
    _candidates_statement,
    _serving_limits_statement,
    _existing_assignments_statement,
    _newer_version_exists_statement,
    _qualifications_statement,
    _requirements_statement,
    _scheduling_period_statement,
    build_scheduling_input,
)
from app.services.sunday_conflict import SundayConflictResult

UTC = datetime.timezone.utc
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)

FRESH = ScheduleVersionStalenessResult(
    current_requirements=frozenset(), snapshot_requirements=frozenset()
)
STALE = ScheduleVersionStalenessResult(
    current_requirements=frozenset(
        {RequirementFingerprint(
            event_id=700, event_date=NOV_15, ministry_role_id=12, required_count=2,
        )}
    ),
    snapshot_requirements=frozenset(),
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


class BuilderSession(Session):
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
        raise AssertionError("a read-only builder must never add")

    def delete(self, instance) -> None:  # pragma: no cover - must never run
        self.delete_calls += 1
        raise AssertionError("a read-only builder must never delete")

    def flush(self, objects=None) -> None:  # pragma: no cover - must never run
        self.flush_calls += 1
        raise AssertionError("a read-only builder must never flush")

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a read-only builder must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a read-only builder must never roll back")


@pytest.fixture
def session() -> BuilderSession:
    return BuilderSession()


def _version(
    version_id: int = 500, *, schedule_id: int = 400, scheduling_period_id: int = 11,
    version_number: int = 1, status: str = SCHEDULE_VERSION_STATUS_DRAFT,
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=schedule_id, scheduling_period_id=scheduling_period_id,
        version_number=version_number, status=status,
        finalized_at=datetime.datetime(2026, 10, 1, tzinfo=UTC)
        if status == SCHEDULE_VERSION_STATUS_FINALIZED else None,
    )
    version.id = version_id
    return version


@pytest.fixture
def draft_version() -> ScheduleVersion:
    return _version()


def _period(period_id: int = 11, *, ministry_id: int = 3) -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry_id, name="Q4 2026",
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    return period


def _requirement_row(
    requirement_id=600, *, event_id=700, event_date=NOV_15, ministry_role_id=12,
    ministry_id=3, required_count=1, role_deactivated_at=None,
):
    """One row as ``_requirements_statement`` returns it."""
    return SimpleNamespace(
        requirement_id=requirement_id, event_id=event_id, event_date=event_date,
        ministry_role_id=ministry_role_id, ministry_id=ministry_id,
        required_count=required_count, role_deactivated_at=role_deactivated_at,
    )


def _membership_row(membership_id=118, *, person_id=42, display_name="John"):
    return SimpleNamespace(
        membership_id=membership_id, person_id=person_id, display_name=display_name,
    )


def _assignment_row(
    assignment_id=900, *, requirement_id=600, membership_id=118, event_id=700,
    is_override=False,
):
    return SimpleNamespace(
        assignment_id=assignment_id, requirement_id=requirement_id,
        membership_id=membership_id, event_id=event_id, is_override=is_override,
    )


def _stub(
    monkeypatch, *, requirement_rows=(), membership_rows=(), assignment_rows=(),
    qualified_pairs=frozenset(), availability=None, blocked_dates=frozenset(),
    staleness=ScheduleVersionStalenessResult(
        current_requirements=frozenset(), snapshot_requirements=frozenset()
    ),
    newer_version_exists=False, period=None, serving_limits=None,
    same_date_exclusion_rows=(),
    min_intervening_events=None, preceding_events=(), following_events=(),
    member_group_caps=(), support_requirements=(),
):
    """Configure the world this builder reads, recording every session handed
    to a collaborator so the "same Session" claims can be checked.
    """
    import app.services.scheduling_input_builder as module

    seen: dict[str, list] = {"staleness": [], "conflict": [], "calls": []}
    resolved_period = period if period is not None else _period()

    def record(name, session, **kwargs):
        seen["calls"].append((name, session, kwargs))

    def fake_period(session, version):
        record("period", session)
        return resolved_period

    def fake_requirements(session, *, schedule_version_id):
        record("requirements", session, schedule_version_id=schedule_version_id)
        return list(requirement_rows)

    def fake_candidates(session, *, ministry_id):
        record("candidates", session, ministry_id=ministry_id)
        return list(membership_rows)

    def fake_qualified(session, *, membership_ids, ministry_role_ids):
        record("qualified", session, membership_ids=membership_ids,
               ministry_role_ids=ministry_role_ids)
        return {
            pair for pair in qualified_pairs
            if pair[0] in membership_ids and pair[1] in ministry_role_ids
        }

    def fake_availability(session, *, membership_ids, event_ids):
        record("availability", session, membership_ids=membership_ids, event_ids=event_ids)
        return {
            key: AvailabilityState(state)
            for key, state in (availability or {}).items()
            if key[0] in membership_ids and key[1] in event_ids
        }

    def fake_serving_limits(session, *, membership_ids, scheduling_period_id):
        record("serving_limits", session, membership_ids=membership_ids,
               scheduling_period_id=scheduling_period_id)
        return {
            membership_id: maximum
            for membership_id, maximum in (serving_limits or {}).items()
            if membership_id in membership_ids
        }

    def fake_assignments(session, *, schedule_version_id):
        record("assignments", session, schedule_version_id=schedule_version_id)
        return list(assignment_rows)

    def fake_same_date_exclusions(session, *, scheduling_period_id):
        record("same_date_exclusions", session,
               scheduling_period_id=scheduling_period_id)
        return list(same_date_exclusion_rows)

    def fake_event_gap_rule(session, *, scheduling_period_id):
        record("min_intervening_events", session,
               scheduling_period_id=scheduling_period_id)
        return min_intervening_events

    def fake_adjacent_events(session, **kwargs):
        record("adjacent_events", session, **kwargs)
        return tuple(preceding_events), tuple(following_events)

    def fake_member_group_caps(session, *, scheduling_period_id):
        record("member_group_caps", session,
               scheduling_period_id=scheduling_period_id)
        return tuple(member_group_caps)

    def fake_support_requirements(session, *, scheduling_period_id):
        record("support_requirements", session,
               scheduling_period_id=scheduling_period_id)
        return tuple(support_requirements)

    def fake_newer(session, *, schedule_id, version_number):
        record("newer", session, schedule_id=schedule_id, version_number=version_number)
        return newer_version_exists

    def fake_staleness(session, *, version):
        seen["staleness"].append(session)
        return staleness

    def fake_conflicts_for(
        session, *, person_ids, conflict_dates, target_ministry_id
    ):
        seen["conflict"].append(
            (session, tuple(person_ids), tuple(conflict_dates), target_ministry_id)
        )
        return {
            (person_id, conflict_date): SundayConflictResult(
                existing_commitments=(SimpleNamespace(),)
                if conflict_date in blocked_dates else (),
                authoritative_assignments=(),
            )
            for person_id in person_ids
            for conflict_date in conflict_dates
        }

    monkeypatch.setattr(module, "_resolve_scheduling_period", fake_period)
    monkeypatch.setattr(module, "_fetch_requirement_rows", fake_requirements)
    monkeypatch.setattr(module, "_fetch_candidate_rows", fake_candidates)
    monkeypatch.setattr(module, "_fetch_qualified_pairs", fake_qualified)
    monkeypatch.setattr(module, "_fetch_availability", fake_availability)
    monkeypatch.setattr(module, "_fetch_serving_limits", fake_serving_limits)
    monkeypatch.setattr(module, "_fetch_existing_assignment_rows", fake_assignments)
    monkeypatch.setattr(
        module, "_fetch_same_date_exclusion_rows", fake_same_date_exclusions
    )
    monkeypatch.setattr(module, "get_min_intervening_events", fake_event_gap_rule)
    monkeypatch.setattr(
        module, "load_adjacent_ministry_events", fake_adjacent_events
    )
    monkeypatch.setattr(module, "load_member_group_caps", fake_member_group_caps)
    monkeypatch.setattr(
        module, "load_support_requirements", fake_support_requirements
    )
    monkeypatch.setattr(module, "_newer_version_exists", fake_newer)
    monkeypatch.setattr(module, "get_schedule_version_staleness", fake_staleness)
    monkeypatch.setattr(module, "get_sunday_conflicts_for", fake_conflicts_for)
    return seen


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --------------------------------------------------------------------------
# 1-6 -- Version and lifecycle gates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status", [SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW]
)
def test_01_02_a_latest_draft_or_review_version_builds(session, monkeypatch, status):
    version = _version(status=status)
    _stub(monkeypatch, requirement_rows=[_requirement_row()])

    result = build_scheduling_input(session, version=version)

    assert isinstance(result, SchedulingInput)
    assert result.schedule_version_id == 500
    assert result.scheduling_period_id == 11
    assert result.ministry_id == 3


def test_03_a_finalized_version_is_rejected(session, monkeypatch):
    """A published schedule is not something to invite a solver to rework."""
    version = _version(status=SCHEDULE_VERSION_STATUS_FINALIZED)
    _stub(monkeypatch)

    with pytest.raises(InvalidOperationError, match="DRAFT or REVIEW"):
        build_scheduling_input(session, version=version)


def test_03b_an_unrecognized_status_is_rejected(session, monkeypatch):
    version = _version(status="ARCHIVED")
    _stub(monkeypatch)

    with pytest.raises(InvalidOperationError, match="DRAFT or REVIEW"):
        build_scheduling_input(session, version=version)


def test_04_a_superseded_version_is_rejected(session, draft_version, monkeypatch):
    _stub(monkeypatch, newer_version_exists=True)

    with pytest.raises(InvalidOperationError, match="superseded"):
        build_scheduling_input(session, version=draft_version)


def test_05_a_stale_version_is_rejected(session, draft_version, monkeypatch):
    """The snapshot is immutable history; the repair is a successor version,
    not a quiet rebuild here.
    """
    _stub(monkeypatch, staleness=STALE, requirement_rows=[_requirement_row()])

    with pytest.raises(InvalidOperationError, match="no longer matches current"):
        build_scheduling_input(session, version=draft_version)


@pytest.mark.parametrize(
    "missing", ["id", "schedule_id", "scheduling_period_id", "version_number"]
)
def test_06_missing_persisted_context_is_rejected(session, missing):
    version = _version()
    setattr(version, missing, None)

    with pytest.raises(InvalidOperationError, match="must be persisted"):
        build_scheduling_input(session, version=version)


def test_06b_the_gates_run_before_any_expensive_read(session, draft_version, monkeypatch):
    """A superseded version is refused as superseded, not after loading a
    whole schedule's worth of candidates.
    """
    seen = _stub(monkeypatch, newer_version_exists=True)

    with pytest.raises(InvalidOperationError):
        build_scheduling_input(session, version=draft_version)

    assert seen["staleness"] == []
    assert seen["conflict"] == []


# --------------------------------------------------------------------------
# 7-11 -- Requirements from the snapshot
# --------------------------------------------------------------------------


def test_07_10_snapshot_rows_populate_requirements_with_their_counts(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch,
        requirement_rows=[
            _requirement_row(600, required_count=1),
            _requirement_row(601, ministry_role_id=13, required_count=3),
        ],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert [r.requirement_id for r in result.requirements] == [600, 601]
    assert [r.required_count for r in result.requirements] == [1, 3]
    assert result.total_required_positions == 4
    assert all(isinstance(r, RequirementInput) for r in result.requirements)


def test_08_09_the_snapshot_date_is_used_and_the_current_event_date_is_not(
    session, draft_version, monkeypatch
):
    """The builder never reads ``event.event_date``: the query does not select
    it, and the value carried is the snapshot's own.
    """
    _stub(monkeypatch, requirement_rows=[_requirement_row(event_date=NOV_15)])

    result = build_scheduling_input(session, version=draft_version)

    assert result.requirements[0].event_date == NOV_15
    compiled = _compile(_requirements_statement(500))
    assert "schedule_version_requirement.event_date" in compiled
    assert "event.event_date" not in compiled
    assert " event " not in f" {compiled} "  # the event table is not joined


def test_11_requirements_are_ordered_deterministically(session, draft_version, monkeypatch):
    """Date, then event, then role, then id -- never incidental row order."""
    _stub(
        monkeypatch,
        requirement_rows=[
            _requirement_row(603, event_id=701, event_date=NOV_22, ministry_role_id=12),
            _requirement_row(602, event_id=700, event_date=NOV_15, ministry_role_id=13),
            _requirement_row(601, event_id=700, event_date=NOV_15, ministry_role_id=12),
        ],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert [r.requirement_id for r in result.requirements] == [601, 602, 603]
    compiled = _compile(_requirements_statement(500))
    assert "ORDER BY" in compiled


def test_a_version_with_no_requirements_still_builds(session, draft_version, monkeypatch):
    """An empty snapshot is legitimate (Task 28), and the ministry still
    resolves -- which is why it comes from the period, not the requirements.
    """
    _stub(monkeypatch, requirement_rows=[], membership_rows=[_membership_row()])

    result = build_scheduling_input(session, version=draft_version)

    assert result.requirements == ()
    assert result.ministry_id == 3
    assert result.event_dates == ()


# --------------------------------------------------------------------------
# 12-16 -- Candidates
# --------------------------------------------------------------------------


def test_12_active_members_are_included(session, draft_version, monkeypatch):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118, person_id=42, display_name="John")],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert isinstance(candidate, CandidateInput)
    assert (candidate.membership_id, candidate.person_id) == (118, 42)
    assert candidate.display_name == "John"


def test_13_14_15_inactive_and_other_ministry_members_are_excluded_by_the_query(
    session, draft_version, monkeypatch
):
    """All three exclusions live in SQL, not in a post-filter -- loading them
    only to drop them would invite someone to use the wider list by mistake.
    """
    compiled = _compile(_candidates_statement(3))

    assert "ministry_membership.ministry_id = 3" in compiled
    assert "ministry_membership.deactivated_at IS NULL" in compiled
    assert "person.deactivated_at IS NULL" in compiled
    assert "JOIN person ON person.id = ministry_membership.person_id" in compiled


def test_16_candidates_are_ordered_deterministically(session, draft_version, monkeypatch):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[
            _membership_row(120, person_id=44, display_name="Ann"),
            _membership_row(118, person_id=42, display_name="John"),
            _membership_row(119, person_id=43, display_name="Mary"),
        ],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert [c.person_id for c in result.candidates] == [42, 43, 44]
    assert "ORDER BY" in _compile(_candidates_statement(3))


# --------------------------------------------------------------------------
# 17-21 -- Qualifications and role activity
# --------------------------------------------------------------------------


def test_17_an_approved_qualification_makes_a_candidate_eligible(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row(ministry_role_id=12)],
        membership_rows=[_membership_row(118)], qualified_pairs={(118, 12)},
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.candidates[0].qualified_role_ids == frozenset({12})
    assert result.eligible_candidates(result.requirements[0])[0].membership_id == 118


def test_18_19_an_explicit_false_or_absent_qualification_is_not_eligible(
    session, draft_version, monkeypatch
):
    """Both are one outcome here, exactly as Task 22 treats them -- the query
    asks for ``is_qualified IS TRUE``, so neither comes back.
    """
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)], qualified_pairs=set(),
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.candidates[0].qualified_role_ids == frozenset()
    assert result.eligible_candidates(result.requirements[0]) == ()
    assert "role_qualification.is_qualified IS true" in _compile(
        _qualifications_statement([118], [12])
    )


def test_20_multiple_role_qualifications_are_represented(session, draft_version, monkeypatch):
    _stub(
        monkeypatch,
        requirement_rows=[
            _requirement_row(600, ministry_role_id=12),
            _requirement_row(601, ministry_role_id=13),
        ],
        membership_rows=[_membership_row(118), _membership_row(119, person_id=43)],
        qualified_pairs={(118, 12), (118, 13), (119, 13)},
    )

    result = build_scheduling_input(session, version=draft_version)

    by_id = {c.membership_id: c for c in result.candidates}
    assert by_id[118].qualified_role_ids == frozenset({12, 13})
    assert by_id[119].qualified_role_ids == frozenset({13})
    lead, assist = result.requirements
    assert [c.membership_id for c in result.eligible_candidates(lead)] == [118]
    assert [c.membership_id for c in result.eligible_candidates(assist)] == [118, 119]


def test_21_a_deactivated_role_is_flagged_and_has_no_ordinary_candidates(
    session, draft_version, monkeypatch
):
    """Role activity is current state, deliberately mixed into the snapshot
    values because Task 23's fingerprint does not cover it -- so nothing else
    would tell the solver.
    """
    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(role_deactivated_at=DEACTIVATED)],
        membership_rows=[_membership_row(118)], qualified_pairs={(118, 12)},
    )

    result = build_scheduling_input(session, version=draft_version)

    requirement = result.requirements[0]
    assert requirement.role_is_active is False
    assert result.eligible_candidates(requirement) == ()
    # The candidate keeps their qualification; it is the role that is shut.
    assert result.candidates[0].qualified_role_ids == frozenset({12})


def test_21b_an_active_role_is_flagged_active(session, draft_version, monkeypatch):
    _stub(monkeypatch, requirement_rows=[_requirement_row(role_deactivated_at=None)])

    result = build_scheduling_input(session, version=draft_version)

    assert result.requirements[0].role_is_active is True
    assert "ministry_role" in _compile(_requirements_statement(500))


# --------------------------------------------------------------------------
# 22-26 -- Availability tri-state
# --------------------------------------------------------------------------


def test_22_23_24_the_three_states_are_preserved_distinctly(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch,
        requirement_rows=[
            _requirement_row(600, event_id=700),
            _requirement_row(601, event_id=701, event_date=NOV_22),
        ],
        membership_rows=[
            _membership_row(118, person_id=42),
            _membership_row(119, person_id=43),
            _membership_row(120, person_id=44),
        ],
        availability={
            (118, 700): AVAILABILITY_AVAILABLE,
            (119, 700): AVAILABILITY_UNAVAILABLE,
            # 120 answered nothing at all.
        },
    )

    result = build_scheduling_input(session, version=draft_version)

    by_id = {c.membership_id: c for c in result.candidates}
    assert by_id[118].availability_for(700) is AvailabilityState.AVAILABLE
    assert by_id[119].availability_for(700) is AvailabilityState.UNAVAILABLE
    assert by_id[120].availability_for(700) is AvailabilityState.NO_RESPONSE
    # ...and an unanswered second event is NO_RESPONSE even for someone who
    # answered the first.
    assert by_id[118].availability_for(701) is AvailabilityState.NO_RESPONSE


def test_the_backup_state_is_passed_through_unchanged(session, draft_version, monkeypatch):
    """Task 52: the builder does no translation of its own for BACKUP -- it
    already maps whatever stored state comes back through
    ``AvailabilityState(row.availability_state)``, so a new stored value
    reaches the pure model correctly with no builder change at all. This pins
    that claim rather than merely asserting it in review.
    """
    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(600, event_id=700)],
        membership_rows=[_membership_row(118, person_id=42)],
        availability={(118, 700): AVAILABILITY_BACKUP},
    )

    result = build_scheduling_input(session, version=draft_version)

    by_id = {c.membership_id: c for c in result.candidates}
    assert by_id[118].availability_for(700) is AvailabilityState.BACKUP


def test_25_no_response_is_never_written_back_and_has_no_stored_value(
    session, draft_version, monkeypatch
):
    """It exists only in the pure model: the database stores explicit
    answers (Task 52 widened that set to three), and this builder writes
    nothing at all.
    """
    from app.models import scheduling_input as models
    from app.models.scheduling_input import Availability

    # Checked against the schema, not by grepping prose: the model file
    # legitimately *documents* that no-response is not stored.
    assert not hasattr(models, "AVAILABILITY_NO_RESPONSE")
    stored_states = {
        name: value for name, value in vars(models).items()
        if name.startswith("AVAILABILITY_")
    }
    assert set(stored_states.values()) == {"AVAILABLE", "BACKUP", "UNAVAILABLE"}
    checks = " ".join(
        str(c.sqltext) for c in Availability.__table__.constraints
        if hasattr(c, "sqltext")
    )
    assert "NO_RESPONSE" not in checks

    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row()],
    )
    result = build_scheduling_input(session, version=draft_version)

    assert result.candidates[0].availability_by_event == {}
    assert result.candidates[0].availability_for(700) is AvailabilityState.NO_RESPONSE
    assert session.add_calls == 0


def test_26_availability_is_keyed_to_the_snapshots_event_ids(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(600, event_id=700), _requirement_row(601, event_id=701)],
        membership_rows=[_membership_row(118)],
        availability={(118, 701): AVAILABILITY_UNAVAILABLE},
    )

    result = build_scheduling_input(session, version=draft_version)

    candidate = result.candidates[0]
    assert candidate.availability_for(701) is AvailabilityState.UNAVAILABLE
    assert candidate.availability_for(700) is AvailabilityState.NO_RESPONSE
    compiled = _compile(_availability_statement([118], [700, 701]))
    assert "availability.event_id IN (700, 701)" in compiled
    assert "availability.ministry_membership_id IN (118)" in compiled


# --------------------------------------------------------------------------
# 27-30 -- Church-wide Sunday conflicts
# --------------------------------------------------------------------------


def test_27_the_conflict_query_is_called_with_the_snapshot_date_and_ministry(
    session, draft_version, monkeypatch
):
    seen = _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(event_date=NOV_15)],
        membership_rows=[_membership_row(118, person_id=42)],
    )

    build_scheduling_input(session, version=draft_version)

    assert len(seen["conflict"]) == 1
    used_session, person_ids, conflict_dates, ministry_id = seen["conflict"][0]
    assert used_session is session
    assert (person_ids, conflict_dates, ministry_id) == ((42,), (NOV_15,), 3)


def test_28_a_cross_ministry_block_appears_in_blocked_dates(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch,
        requirement_rows=[
            _requirement_row(600, event_id=700, event_date=NOV_15),
            _requirement_row(601, event_id=701, event_date=NOV_22),
        ],
        membership_rows=[_membership_row(118, person_id=42)],
        blocked_dates={NOV_15},
    )

    result = build_scheduling_input(session, version=draft_version)

    candidate = result.candidates[0]
    assert candidate.blocked_dates == frozenset({NOV_15})
    assert candidate.is_blocked_on(NOV_15) is True
    assert candidate.is_blocked_on(NOV_22) is False  # 29: unblocked date absent


def test_29_no_conflicts_means_an_empty_blocked_set(session, draft_version, monkeypatch):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row()], blocked_dates=frozenset(),
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.candidates[0].blocked_dates == frozenset()


def test_30_two_requirements_on_one_date_are_asked_about_once(
    session, draft_version, monkeypatch
):
    """A blocked date is a fact about the person and the date, not about a
    role -- so two roles on the same Sunday need one question, and both read
    the same answer.
    """
    seen = _stub(
        monkeypatch,
        requirement_rows=[
            _requirement_row(600, event_id=700, event_date=NOV_15, ministry_role_id=12),
            _requirement_row(601, event_id=700, event_date=NOV_15, ministry_role_id=13),
        ],
        membership_rows=[_membership_row(118, person_id=42)],
        blocked_dates={NOV_15},
    )

    result = build_scheduling_input(session, version=draft_version)

    assert len(seen["conflict"]) == 1  # one person, one distinct date
    assert result.candidates[0].blocked_dates == frozenset({NOV_15})


def test_the_conflict_query_is_asked_once_for_the_whole_candidate_date_set(
    session, draft_version, monkeypatch
):
    """The church-wide conflict rule is one batched call for every
    (candidate, distinct date) pair, not one call per pair."""
    seen = _stub(
        monkeypatch,
        requirement_rows=[
            _requirement_row(600, event_id=700, event_date=NOV_15),
            _requirement_row(601, event_id=701, event_date=NOV_22),
        ],
        membership_rows=[_membership_row(118, person_id=42), _membership_row(119, person_id=43)],
    )

    build_scheduling_input(session, version=draft_version)

    assert len(seen["conflict"]) == 1
    _used_session, person_ids, conflict_dates, ministry_id = seen["conflict"][0]
    assert sorted(person_ids) == [42, 43]
    assert sorted(conflict_dates) == [NOV_15, NOV_22]
    assert ministry_id == 3


def test_conflict_query_count_does_not_grow_with_candidates_or_dates(
    session, draft_version, monkeypatch
):
    """N+1 regression guard (Task 59): the number of church-wide conflict
    lookups is constant with respect to candidate x date combinations.

    Expressed as a property rather than a fixed statement count, following
    ``tests/test_services_readiness_batching.py``.
    """
    counts = []
    for n in (1, 3, 8):
        local_session = BuilderSession()
        seen = _stub(
            monkeypatch,
            requirement_rows=[
                _requirement_row(
                    600 + d,
                    event_id=700 + d,
                    event_date=NOV_15 + datetime.timedelta(days=7 * d),
                )
                for d in range(n)
            ],
            membership_rows=[
                _membership_row(118 + i, person_id=42 + i) for i in range(n)
            ],
        )
        build_scheduling_input(local_session, version=draft_version)
        counts.append(len(seen["conflict"]))

    assert counts == [1, 1, 1]


# --------------------------------------------------------------------------
# 31-34 -- Existing assignments
# --------------------------------------------------------------------------


def test_31_32_existing_assignments_are_represented_with_their_override_flag(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row()],
        assignment_rows=[
            _assignment_row(900, requirement_id=600, membership_id=118, is_override=False),
            _assignment_row(901, requirement_id=600, membership_id=119, is_override=True),
        ],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert [a.assignment_id for a in result.existing_assignments] == [900, 901]
    assert [a.is_override for a in result.existing_assignments] == [False, True]
    assert all(isinstance(a, ExistingAssignmentInput) for a in result.existing_assignments)
    assert len(result.existing_assignments_for(600)) == 2


def test_33_assignments_of_another_version_are_excluded_by_the_query(session):
    compiled = _compile(_existing_assignments_statement(500))

    assert "assignment.schedule_version_id = 500" in compiled
    assert "assignment.schedule_version_id = 501" not in compiled


def test_34_existing_assignments_are_ordered_deterministically(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row()],
        assignment_rows=[_assignment_row(900), _assignment_row(901)],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert [a.assignment_id for a in result.existing_assignments] == [900, 901]
    assert "ORDER BY assignment.id" in _compile(_existing_assignments_statement(500))


def test_no_override_reason_or_audit_history_reaches_the_solver_input(
    session, draft_version, monkeypatch
):
    """Task 26 reads override history where it lives; the solver gets only the
    flag that says a decision already exists.
    """
    import app.services.scheduling_input_builder as module

    assert not hasattr(module, "AuditEvent")
    assert not hasattr(module, "record_audit_event")
    assert not [n for n in vars(module) if "audit" in n.lower()]
    assert "override_reason" not in _compile(_existing_assignments_statement(500))


# --------------------------------------------------------------------------
# 36-39 -- Read-only, session and purity of the output
# --------------------------------------------------------------------------


def test_36_37_the_builder_writes_nothing_at_all(session, draft_version, monkeypatch):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row()], assignment_rows=[_assignment_row()],
    )

    build_scheduling_input(session, version=draft_version)

    assert session.add_calls == 0
    assert session.delete_calls == 0
    assert session.flush_calls == 0
    assert session.commit_calls == 0
    assert session.rollback_calls == 0
    assert [o for o in session.new if isinstance(o, AuditEvent)] == []
    assert len(session.new) == 0
    assert len(session.dirty) == 0
    assert len(session.deleted) == 0


def test_38_the_reused_services_receive_the_supplied_session(
    session, draft_version, monkeypatch
):
    seen = _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row()],
    )

    build_scheduling_input(session, version=draft_version)

    assert seen["staleness"] == [session]
    assert all(call[0] is session for call in seen["conflict"])


def test_39_the_output_contains_no_orm_instances(session, draft_version, monkeypatch):
    """Every value that leaves this builder is a plain Python one -- nothing a
    solver could lazy-load through.
    """
    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row()], membership_rows=[_membership_row()],
        assignment_rows=[_assignment_row()],
        availability={(118, 700): AVAILABILITY_AVAILABLE},
        qualified_pairs={(118, 12)}, blocked_dates={NOV_15},
    )

    result = build_scheduling_input(session, version=draft_version)

    def assert_plain(value, label):
        module_name = type(value).__module__
        assert not module_name.startswith("app.models"), f"{label}: {type(value)}"
        assert not module_name.startswith("sqlalchemy"), f"{label}: {type(value)}"

    assert_plain(result, "input")
    for requirement in result.requirements:
        assert_plain(requirement, "requirement")
        for field_name in requirement.__dataclass_fields__:
            assert_plain(getattr(requirement, field_name), field_name)
    for candidate in result.candidates:
        assert_plain(candidate, "candidate")
        for value in candidate.availability_by_event.values():
            assert isinstance(value, AvailabilityState)
        for value in candidate.blocked_dates:
            assert isinstance(value, datetime.date)
    for assignment in result.existing_assignments:
        assert_plain(assignment, "assignment")


# --------------------------------------------------------------------------
# Query shape (compiled SQL)
# --------------------------------------------------------------------------


def test_the_period_lookup_and_newer_version_probe_are_scoped():
    period = _compile(_scheduling_period_statement(11))
    newer = _compile(_newer_version_exists_statement(400, 1))

    assert "scheduling_period.id = 11" in period
    assert "schedule_version.schedule_id = 400" in newer
    assert "schedule_version.version_number > 1" in newer
    assert "LIMIT 1" in newer


def test_the_requirements_query_selects_the_snapshot_columns_and_role_activity():
    compiled = _compile(_requirements_statement(500))

    assert "schedule_version_requirement.schedule_version_id = 500" in compiled
    for column in ("event_id", "event_date", "ministry_role_id", "required_count"):
        assert f"AS {column}" in compiled
    assert "ministry_role.deactivated_at AS role_deactivated_at" in compiled


def test_the_qualification_and_availability_queries_are_batched_by_id():
    qualification = _compile(_qualifications_statement([118, 119], [12, 13]))
    availability = _compile(_availability_statement([118, 119], [700]))

    assert "IN (118, 119)" in qualification
    assert "IN (12, 13)" in qualification
    assert "IN (118, 119)" in availability


# --------------------------------------------------------------------------
# The person-period serving maximum (Task 47)
#
# The builder's job is translation: it reads the persisted limit and states it
# on the candidate, so the pure solver never queries anything. Enforcement is
# the solver's, and is tested there.
# --------------------------------------------------------------------------


def test_a_configured_serving_maximum_reaches_the_candidate(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
        serving_limits={118: 4},
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.candidates[0].max_assignments_in_period == 4


def test_a_candidate_with_no_configured_maximum_is_uncapped(
    session, draft_version, monkeypatch
):
    # Absence of a row is the whole representation of "no maximum"; it must
    # never arrive as zero, which would mean "never schedule this person".
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.candidates[0].max_assignments_in_period is None


def test_only_the_capped_members_are_capped(session, draft_version, monkeypatch):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[
            _membership_row(118, person_id=42, display_name="Volunteer A"),
            _membership_row(119, person_id=43, display_name="Volunteer B"),
        ],
        serving_limits={119: 2},
    )

    result = build_scheduling_input(session, version=draft_version)

    by_membership = {c.membership_id: c for c in result.candidates}
    assert by_membership[118].max_assignments_in_period is None
    assert by_membership[119].max_assignments_in_period == 2


def test_the_serving_limit_lookup_is_scoped_to_this_period(
    session, draft_version, monkeypatch
):
    """Scoped by scheduling period, never by membership alone.

    A limit expires with its period, so another period's row must not leak
    into this run.
    """
    seen = _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
        serving_limits={118: 4},
    )

    build_scheduling_input(session, version=draft_version)

    calls = [c for c in seen["calls"] if c[0] == "serving_limits"]
    assert len(calls) == 1
    assert calls[0][2]["scheduling_period_id"] == 11
    assert calls[0][2]["membership_ids"] == [118]
    # The same Session the caller supplied, like every other collaborator.
    assert calls[0][1] is session


def test_the_serving_limit_statement_filters_on_period_and_memberships():
    sql = _compile(_serving_limits_statement([118, 119], 11))

    assert "membership_serving_limit.scheduling_period_id = 11" in sql
    assert "IN (118, 119)" in sql


def test_the_serving_limit_query_is_skipped_when_there_are_no_members():
    """No members means no query at all -- an ``IN ()`` would be malformed."""
    import app.services.scheduling_input_builder as module

    class Boom:
        def execute(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("no query should be issued for an empty roster")

    assert module._fetch_serving_limits(
        Boom(), membership_ids=[], scheduling_period_id=11
    ) == {}


# ==========================================================================
# The linked-pair same-date exclusion (Task 50)
#
# Read current, like qualifications, availability and serving limits, and
# scoped to this period alone. Every person here is synthetic, and no pair is
# ever inferred from anything -- the builder reads configured rows and nothing
# else.
# ==========================================================================


def _exclusion_row(membership_a_id=118, membership_b_id=119, ministry_id=3):
    """One row as ``_same_date_exclusions_statement`` returns it."""
    return SimpleNamespace(
        membership_a_id=membership_a_id,
        membership_b_id=membership_b_id,
        ministry_id=ministry_id,
    )


def test_a_configured_pair_reaches_the_scheduling_input(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118), _membership_row(119, person_id=43)],
        same_date_exclusion_rows=[_exclusion_row()],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.same_date_exclusions == (LinkedMembershipPair(118, 119),)
    assert result.linked_membership_ids(118) == frozenset({119})
    assert result.linked_membership_ids(119) == frozenset({118})


def test_no_configured_pair_means_an_empty_tuple(session, draft_version, monkeypatch):
    """Absence is the whole representation of "no rule"; nothing is invented."""
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.same_date_exclusions == ()


def test_a_reversed_persisted_row_still_canonicalizes(
    session, draft_version, monkeypatch
):
    """Belt and braces: the database's CHECK makes the reversed row
    unwritable, and the pure value canonicalizes anyway, so the two
    guarantees cannot disagree.
    """
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118), _membership_row(119, person_id=43)],
        same_date_exclusion_rows=[
            _exclusion_row(membership_a_id=119, membership_b_id=118)
        ],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.same_date_exclusions == (LinkedMembershipPair(118, 119),)


def test_the_pair_lookup_is_scoped_to_this_period(session, draft_version, monkeypatch):
    """A rule belongs to one period and expires with it, so another period's
    rows must not leak into this run. Scoping by period also makes the
    ministry scope automatic -- the row's composite foreign keys guarantee the
    period and both memberships share a ministry.
    """
    seen = _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
        same_date_exclusion_rows=[_exclusion_row()],
    )

    build_scheduling_input(session, version=draft_version)

    calls = [c for c in seen["calls"] if c[0] == "same_date_exclusions"]
    assert len(calls) == 1
    assert calls[0][2]["scheduling_period_id"] == 11
    # The same Session the caller supplied, like every other collaborator.
    assert calls[0][1] is session


def test_a_pair_row_from_another_ministry_is_refused_loudly(
    session, draft_version, monkeypatch
):
    """State the database makes unreachable, refused rather than applied.

    A pair that mapped inconsistently would silently constrain the wrong
    people, so failing is the only safe response.
    """
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
        same_date_exclusion_rows=[_exclusion_row(ministry_id=4)],
    )

    with pytest.raises(InvalidOperationError, match="inconsistent"):
        build_scheduling_input(session, version=draft_version)


def test_a_pair_naming_a_non_candidate_is_kept_rather_than_dropped(
    session, draft_version, monkeypatch
):
    """A member deactivated part-way through the period stops being a
    candidate while their rule stays configured.

    The solver treats such a pair as inert -- somebody with no variables and
    no existing assignment can never be present -- and deciding the rule no
    longer applies is a head's call, not this builder's.
    """
    _stub(
        monkeypatch, requirement_rows=[_requirement_row()],
        # Only 118 is an active candidate.
        membership_rows=[_membership_row(118)],
        same_date_exclusion_rows=[_exclusion_row()],
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.same_date_exclusions == (LinkedMembershipPair(118, 119),)
    assert [c.membership_id for c in result.candidates] == [118]


def test_the_pair_statement_is_scoped_and_ordered():
    """The SQL itself: this period only, and a stable order so two runs over
    the same state build an identical model."""
    from app.services.scheduling_input_builder import (
        _same_date_exclusions_statement,
    )

    sql = _compile(_same_date_exclusions_statement(11))
    assert "membership_same_date_exclusion.scheduling_period_id = 11" in sql
    assert "ORDER BY" in sql


def test_the_builder_infers_no_pair_from_history():
    """Always explicitly configured, never inferred.

    The one query that produces pairs reads the constraint table and nothing
    else -- not assignments, not names, not addresses.
    """
    from app.services.scheduling_input_builder import (
        _same_date_exclusions_statement,
    )

    sql = _compile(_same_date_exclusions_statement(11)).lower()
    assert "membership_same_date_exclusion" in sql
    for table in ("assignment", "person", "availability"):
        assert f"from {table}" not in sql
        assert f"join {table}" not in sql


# --------------------------------------------------------------------------
# The ministry event-gap rule (Task 71)
# --------------------------------------------------------------------------


def test_no_configured_gap_means_no_rule_and_no_history_query(
    session, draft_version, monkeypatch
):
    """The ordinary case. The setting is read, and because it is absent the
    history loader is never called at all -- the cost of the rule is paid only
    by periods that asked for it.
    """
    seen = _stub(
        monkeypatch,
        requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
        min_intervening_events=None,
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.min_intervening_events is None
    assert result.preceding_events == ()
    assert result.following_events == ()
    assert [name for name, _, _ in seen["calls"] if name == "adjacent_events"] == []


def test_a_configured_gap_reaches_the_scheduling_input(
    session, draft_version, monkeypatch
):
    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
        min_intervening_events=2,
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.min_intervening_events == 2


def test_both_boundaries_come_from_the_snapshots_own_first_and_last_events(
    session, draft_version, monkeypatch
):
    """The boundaries are the version's own first and last scheduled events --
    taken from the snapshot, never from the period's calendar ``start_date`` or
    ``end_date``, and never from a live ``event`` row.
    """
    later = _requirement_row(
        requirement_id=601, event_id=702, event_date=datetime.date(2026, 10, 11)
    )
    earlier = _requirement_row(
        requirement_id=600, event_id=701, event_date=datetime.date(2026, 10, 4)
    )
    seen = _stub(
        monkeypatch,
        # Deliberately out of order, so a builder reading "the first row"
        # rather than the earliest event would pick the wrong boundary.
        requirement_rows=[later, earlier],
        membership_rows=[_membership_row(118)],
        min_intervening_events=1,
    )

    build_scheduling_input(session, version=draft_version)

    (call,) = [kwargs for name, _, kwargs in seen["calls"] if name == "adjacent_events"]
    assert call["first_event_date"] == datetime.date(2026, 10, 4)
    assert call["first_event_id"] == 701
    assert call["last_event_date"] == datetime.date(2026, 10, 11)
    assert call["last_event_id"] == 702
    assert call["min_intervening_events"] == 1
    assert call["ministry_id"] == 3


def test_adjacent_events_become_pure_values_with_their_assignments(
    session, draft_version, monkeypatch
):
    """The service type becomes the solver type unchanged, on both sides. That
    translation is a change of layer, not of meaning -- one may be read from a
    database and the other may not.
    """
    from app.services.event_gap import AdjacentMinistryEvent

    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
        min_intervening_events=1,
        preceding_events=[
            AdjacentMinistryEvent(
                event_id=699,
                event_date=datetime.date(2026, 9, 27),
                assigned_membership_ids=frozenset({118}),
            )
        ],
        following_events=[
            AdjacentMinistryEvent(
                event_id=704,
                event_date=datetime.date(2026, 11, 22),
                assigned_membership_ids=frozenset({119}),
            )
        ],
    )

    result = build_scheduling_input(session, version=draft_version)

    (preceding,) = result.preceding_events
    assert preceding.event_id == 699
    assert preceding.event_date == datetime.date(2026, 9, 27)
    assert preceding.assigned_membership_ids == frozenset({118})

    (following,) = result.following_events
    assert following.event_id == 704
    assert following.event_date == datetime.date(2026, 11, 22)
    assert following.assigned_membership_ids == frozenset({119})

    # Pure values: nothing in either is an ORM instance.
    for event in (preceding, following):
        assert type(event).__module__ == "app.scheduling.input"


def test_a_version_with_no_requirements_loads_no_history(
    session, draft_version, monkeypatch
):
    """There is no boundary to load history against, so none is loaded -- and
    the configured rule still travels, because it is configured."""
    seen = _stub(
        monkeypatch,
        requirement_rows=[],
        membership_rows=[_membership_row(118)],
        min_intervening_events=1,
    )

    result = build_scheduling_input(session, version=draft_version)

    assert result.min_intervening_events == 1
    assert result.preceding_events == ()
    assert result.following_events == ()
    assert [name for name, _, _ in seen["calls"] if name == "adjacent_events"] == []


def test_the_gap_lookup_is_scoped_to_this_period(session, draft_version, monkeypatch):
    """A rule belongs to one period and expires with it."""
    seen = _stub(
        monkeypatch,
        requirement_rows=[_requirement_row()],
        membership_rows=[_membership_row(118)],
        min_intervening_events=1,
    )

    build_scheduling_input(session, version=draft_version)

    (call,) = [
        kwargs for name, _, kwargs in seen["calls"] if name == "min_intervening_events"
    ]
    assert call == {"scheduling_period_id": 11}


# ==========================================================================
# Task 74: the member-group caps and the same-event support requirements
# ==========================================================================


def test_no_configured_task74_rule_reaches_the_solver_as_empty_tuples(monkeypatch):
    """The legacy-behaviour guarantee: an unconfigured period builds an input
    the solver treats exactly as it did before either rule existed.
    """
    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(600, event_id=700)],
        membership_rows=[_membership_row(118, person_id=42)],
    )

    result = build_scheduling_input(None, version=_version())

    assert result.member_group_caps == ()
    assert result.support_requirements == ()


def test_a_configured_cap_reaches_the_scheduling_input_without_its_name(monkeypatch):
    """The service form carries the group's name so a message can say which
    rule refused a placement; the solver neither shows a message nor needs a
    label, so the pure value drops it.
    """
    from app.services.member_group import MemberGroupCapConfig

    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(600, event_id=700)],
        membership_rows=[_membership_row(118, person_id=42)],
        member_group_caps=(
            MemberGroupCapConfig(
                member_group_id=400, member_group_name="Category A",
                max_per_event=2, member_membership_ids=frozenset({118, 119}),
            ),
        ),
    )

    result = build_scheduling_input(None, version=_version())

    (cap,) = result.member_group_caps
    assert cap.member_group_id == 400
    assert cap.max_per_event == 2
    assert cap.member_membership_ids == frozenset({118, 119})
    assert not hasattr(cap, "member_group_name")


def test_a_configured_support_requirement_reaches_the_scheduling_input(monkeypatch):
    from app.services.same_event_support import SupportRequirementConfig

    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(600, event_id=700)],
        membership_rows=[_membership_row(118, person_id=42)],
        support_requirements=(
            SupportRequirementConfig(
                support_requirement_id=1, subject_membership_id=118,
                min_supporters=1, supporter_membership_ids=frozenset({119}),
            ),
        ),
    )

    result = build_scheduling_input(None, version=_version())

    (requirement,) = result.support_requirements
    assert requirement.subject_membership_id == 118
    assert requirement.min_supporters == 1
    assert requirement.supporter_membership_ids == frozenset({119})


def test_an_unsatisfiable_support_requirement_is_carried_rather_than_dropped(
    monkeypatch
):
    """It means the subject can never be placed, which the solver reports
    honestly. Silently discarding it here would schedule somebody the ministry
    said must not serve alone.
    """
    from app.services.same_event_support import SupportRequirementConfig

    _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(600, event_id=700)],
        membership_rows=[_membership_row(118, person_id=42)],
        support_requirements=(
            SupportRequirementConfig(
                support_requirement_id=1, subject_membership_id=118,
                min_supporters=2, supporter_membership_ids=frozenset({119}),
            ),
        ),
    )

    result = build_scheduling_input(None, version=_version())

    (requirement,) = result.support_requirements
    assert not requirement.is_satisfiable


def test_both_task74_reads_are_scoped_to_this_period(monkeypatch):
    """A rule belongs to one period and expires with it, so another period's
    rows must not leak into this run.
    """
    seen = _stub(
        monkeypatch,
        requirement_rows=[_requirement_row(600, event_id=700)],
        membership_rows=[_membership_row(118, person_id=42)],
    )

    build_scheduling_input(None, version=_version())

    calls = {name: kwargs for name, _, kwargs in seen["calls"]}
    assert calls["member_group_caps"] == {"scheduling_period_id": 11}
    assert calls["support_requirements"] == {"scheduling_period_id": 11}
