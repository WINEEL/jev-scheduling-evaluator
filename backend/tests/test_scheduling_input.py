"""The pure solver-input model (Task 30).

These tests need no database, no Session and no fixtures beyond plain values --
which is the point. If this file ever needs an import from ``sqlalchemy`` or
``app.models``, the model has stopped being pure.
"""

from __future__ import annotations

import datetime
import sys

import pytest

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    RequirementInput,
    SchedulingInput,
)

NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)


def _requirement(requirement_id=600, *, event_id=700, event_date=NOV_15,
                 ministry_role_id=12, required_count=1, role_is_active=True):
    return RequirementInput(
        requirement_id=requirement_id, event_id=event_id, event_date=event_date,
        ministry_role_id=ministry_role_id, ministry_id=3,
        required_count=required_count, role_is_active=role_is_active,
    )


def _candidate(membership_id=118, *, person_id=42, name="John",
               qualified=(12,), availability=None, blocked=()):
    return CandidateInput(
        membership_id=membership_id, person_id=person_id, display_name=name,
        qualified_role_ids=frozenset(qualified),
        availability_by_event=availability or {},
        blocked_dates=frozenset(blocked),
    )


# --------------------------------------------------------------------------
# 35 -- Purity
# --------------------------------------------------------------------------


def test_35_the_pure_module_imports_no_sqlalchemy_session_or_orm():
    """The load-bearing property: a solver built on this can run with no
    database at all.
    """
    import app.scheduling.input as module

    for name in vars(module):
        value = getattr(module, name)
        module_name = getattr(value, "__module__", "") or ""
        assert not module_name.startswith("sqlalchemy"), name
        assert not module_name.startswith("app.models"), name
        assert not module_name.startswith("app.services"), name

    # Checked against the actual import statements, not by grepping prose:
    # the docstring legitimately explains what is *not* imported.
    import ast
    from pathlib import Path

    tree = ast.parse(Path(module.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported, "expected at least the stdlib imports"
    for name in imported:
        assert not name.startswith("sqlalchemy"), name
        assert not name.startswith("app.models"), name
        assert not name.startswith("app.services"), name
        assert not name.startswith("app.db"), name


def test_35b_the_scheduling_package_pulls_in_no_database_machinery():
    """Importing the package in a fresh interpreter must not drag in the ORM
    layer -- checked as a subprocess so this suite's own imports cannot mask
    it.
    """
    import subprocess

    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; import app.scheduling;"
            " leaked = [m for m in sys.modules if m.startswith('app.models')"
            "           or m.startswith('app.services')];"
            " print(leaked)",
        ],
        capture_output=True, text=True, cwd=".",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]", result.stdout


def test_the_value_types_are_frozen_and_slotted():
    requirement = _requirement()
    candidate = _candidate()
    existing = ExistingAssignmentInput(
        assignment_id=900, requirement_id=600, membership_id=118, event_id=700,
    )
    scheduling_input = SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
    )

    for value in (requirement, candidate, existing, scheduling_input):
        with pytest.raises(Exception):
            value.__dict__  # slots: no instance dict
    with pytest.raises(Exception):
        requirement.required_count = 9
    with pytest.raises(Exception):
        scheduling_input.requirements = ()


# --------------------------------------------------------------------------
# Availability tri-state
# --------------------------------------------------------------------------


def test_the_four_availability_states_are_distinct():
    """Task 52 added BACKUP alongside the original three."""
    assert len(set(AvailabilityState)) == 4
    assert AvailabilityState.AVAILABLE is not AvailabilityState.NO_RESPONSE
    assert AvailabilityState.UNAVAILABLE is not AvailabilityState.NO_RESPONSE
    assert AvailabilityState.BACKUP is not AvailabilityState.AVAILABLE
    assert AvailabilityState.BACKUP is not AvailabilityState.UNAVAILABLE
    assert AvailabilityState.BACKUP is not AvailabilityState.NO_RESPONSE


def test_24_an_absent_answer_reads_as_no_response():
    candidate = _candidate(availability={700: AvailabilityState.AVAILABLE})

    assert candidate.availability_for(700) is AvailabilityState.AVAILABLE
    assert candidate.availability_for(701) is AvailabilityState.NO_RESPONSE


def test_no_response_is_never_collapsed_into_available():
    """A generic builder must not answer for someone who did not answer."""
    candidate = _candidate(availability={})

    assert candidate.availability_for(700) is not AvailabilityState.AVAILABLE
    assert candidate.availability_for(700) is AvailabilityState.NO_RESPONSE


def test_the_availability_mapping_cannot_be_mutated_through_the_candidate():
    from types import MappingProxyType

    candidate = CandidateInput(
        membership_id=118, person_id=42, display_name="John",
        availability_by_event=MappingProxyType({700: AvailabilityState.UNAVAILABLE}),
    )

    with pytest.raises(TypeError):
        candidate.availability_by_event[701] = AvailabilityState.AVAILABLE


# --------------------------------------------------------------------------
# Eligibility
# --------------------------------------------------------------------------


def test_eligibility_is_qualification_and_role_activity_only():
    """Availability and blocked dates are constraints the solver weighs, not
    reasons someone is not a candidate.
    """
    requirement = _requirement(ministry_role_id=12)
    qualified_but_unavailable = _candidate(
        118, person_id=42, availability={700: AvailabilityState.UNAVAILABLE},
    )
    qualified_but_blocked = _candidate(119, person_id=43, blocked=[NOV_15])
    unqualified = _candidate(120, person_id=44, qualified=())
    scheduling_input = SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=(requirement,),
        candidates=(qualified_but_unavailable, qualified_but_blocked, unqualified),
    )

    eligible = scheduling_input.eligible_candidates(requirement)

    assert [c.membership_id for c in eligible] == [118, 119]


def test_21_a_deactivated_role_has_no_ordinary_eligible_candidates():
    """The solver must never place an ordinary assignment into a role that has
    since been deactivated -- and should not have to remember to check.
    """
    requirement = _requirement(role_is_active=False)
    scheduling_input = SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=(requirement,), candidates=(_candidate(qualified=(12,)),),
    )

    assert scheduling_input.eligible_candidates(requirement) == ()


def test_helpers_read_the_stated_facts():
    candidate = _candidate(
        qualified=(12, 13), availability={700: AvailabilityState.AVAILABLE},
        blocked=[NOV_22],
    )

    assert candidate.is_qualified_for(12) is True
    assert candidate.is_qualified_for(99) is False
    assert candidate.is_blocked_on(NOV_22) is True
    assert candidate.is_blocked_on(NOV_15) is False


def test_scheduling_input_summaries():
    scheduling_input = SchedulingInput(
        schedule_version_id=500, scheduling_period_id=11, ministry_id=3,
        requirements=(
            _requirement(600, event_date=NOV_22, required_count=2),
            _requirement(601, event_date=NOV_15, required_count=1),
        ),
        candidates=(_candidate(),),
        existing_assignments=(
            ExistingAssignmentInput(
                assignment_id=900, requirement_id=601, membership_id=118, event_id=700,
            ),
        ),
    )

    assert scheduling_input.event_dates == (NOV_15, NOV_22)
    assert scheduling_input.total_required_positions == 3
    assert scheduling_input.candidate_by_membership_id(118).display_name == "John"
    assert scheduling_input.candidate_by_membership_id(999) is None
    assert len(scheduling_input.existing_assignments_for(601)) == 1
    assert scheduling_input.existing_assignments_for(600) == ()


def test_requirement_sort_key_orders_by_date_then_event_then_role_then_id():
    a = _requirement(600, event_id=700, event_date=NOV_15, ministry_role_id=12)
    b = _requirement(601, event_id=700, event_date=NOV_15, ministry_role_id=13)
    c = _requirement(602, event_id=701, event_date=NOV_22, ministry_role_id=12)

    assert sorted([c, b, a], key=lambda r: r.sort_key) == [a, b, c]


def test_an_existing_assignment_records_its_override_flag_and_nothing_more():
    """No reason, no audit, no lineage: Task 26 reads the history where it
    lives, and Task 31 decides what the solver may do with the row.
    """
    fields = set(ExistingAssignmentInput.__dataclass_fields__)

    assert fields == {
        "assignment_id", "requirement_id", "membership_id", "event_id", "is_override",
    }
