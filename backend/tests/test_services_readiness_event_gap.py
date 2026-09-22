"""The event-gap finalization gate (Task 71, readiness gate 6).

Offline: no PostgreSQL, no network.

The gate reports what is wrong and repairs nothing -- the same contract every
other readiness gate keeps. Three properties matter and are pinned separately:

- an unconfigured period reports nothing, ever;
- a configured one catches assignments that are too close together, **including
  across the start of the period**, and including ones that predate the rule;
- an override authorizes nothing here, because the gap rule is not one of the
  bounded overridable blockers.

The rule's own meaning lives in ``tests/test_services_event_gap.py``; the
builders and the stub here are the ones
``tests/test_services_finalization_readiness.py`` already uses, imported rather
than re-written so the two suites cannot describe two different worlds.

Every person, ministry and date here is synthetic.
"""

from __future__ import annotations

import datetime

import pytest

from app.services.event_gap import MIN_EVENT_GAP_CONFLICT, MinistryEventSequence
from app.services.finalization_readiness import get_finalization_readiness
from tests.test_services_finalization_readiness import (
    _assignment,
    _codes,
    _event,
    _membership,
    _ministry,
    _override_audit,
    _person,
    _requirement,
    _role,
    _stub,
    _version,
)

NOV_1 = datetime.date(2026, 11, 1)
NOV_8 = datetime.date(2026, 11, 8)
NOV_15 = datetime.date(2026, 11, 15)
NOV_22 = datetime.date(2026, 11, 22)


@pytest.fixture
def world():
    """One ministry, one role, and two events a version schedules for.

    Event 699 is deliberately *not* in the version: it is the ministry event
    from before this period, and it appears only on the sequence.
    """
    ministry = _ministry()
    role = _role(ministry=ministry)
    first = _event(700, ministry=ministry)
    second = _event(701, ministry=ministry)
    john = _membership(118, person=_person(42, "John"), ministry=ministry)
    mary = _membership(119, person=_person(43, "Mary"), ministry=ministry)
    return {
        "ministry": ministry,
        "role": role,
        "john": john,
        "mary": mary,
        "version": _version(),
        "first": _requirement(600, event=first, role=role, event_date=NOV_8),
        "second": _requirement(601, event=second, role=role, event_date=NOV_15),
    }


def _sequence(gap: int = 1, *, adjacent=None) -> MinistryEventSequence:
    """699 before the period, the version's own two events, then 702 after it.

    One shape reaching both boundaries, so a gate that enforced only one of
    them would fail here rather than quietly pass.
    """
    return MinistryEventSequence(
        min_intervening_events=gap,
        events=((699, NOV_1), (700, NOV_8), (701, NOV_15), (702, NOV_22)),
        adjacent_events_by_membership=adjacent or {},
    )


def _both_events(world, membership):
    return [
        _assignment(9000, requirement=world["first"], membership=membership),
        _assignment(9001, requirement=world["second"], membership=membership),
    ]


# ==========================================================================
# Unconfigured -- the legacy guarantee
# ==========================================================================


def test_no_configured_rule_reports_nothing(world, monkeypatch):
    """Two consecutive events, same person, and a period that never asked for
    the rule: perfectly ready, exactly as before Task 71.
    """
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=_both_events(world, world["john"]),
        event_sequence=None,
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is True
    assert MIN_EVENT_GAP_CONFLICT not in _codes(result)


# ==========================================================================
# Configured -- the gate bites
# ==========================================================================


def test_two_consecutive_events_for_one_person_block_finalization(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=_both_events(world, world["john"]),
        event_sequence=_sequence(),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is False
    assert _codes(result) == [MIN_EVENT_GAP_CONFLICT]


def test_the_issue_names_both_dates_the_person_and_the_remedy(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=_both_events(world, world["john"]),
        event_sequence=_sequence(),
    )

    (issue,) = get_finalization_readiness(None, version=world["version"]).issues

    assert "John" in issue.message
    assert NOV_8.isoformat() in issue.message
    assert NOV_15.isoformat() in issue.message
    assert "consecutive events" in issue.message
    # Generic product copy: the rule counts events, never days.
    assert "day" not in issue.message.lower()


def test_the_issue_is_version_wide_not_attached_to_one_assignment(world, monkeypatch):
    """The problem is the coincidence of two placements, not either one of
    them. Attaching it to one would tell a head which to remove, and that is
    exactly the decision this module leaves to them.
    """
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=_both_events(world, world["john"]),
        event_sequence=_sequence(),
    )

    (issue,) = get_finalization_readiness(None, version=world["version"]).issues

    assert issue.assignment_id is None
    assert issue.schedule_version_requirement_id is None


def test_one_issue_per_clash_not_one_per_assignment(world, monkeypatch):
    """Two assignments, one clash, one issue -- the same shape gate 5 keeps."""
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=_both_events(world, world["john"]),
        event_sequence=_sequence(),
    )

    issues = [
        issue
        for issue in get_finalization_readiness(None, version=world["version"]).issues
        if issue.code == MIN_EVENT_GAP_CONFLICT
    ]
    assert len(issues) == 1


def test_different_people_on_consecutive_events_are_fine(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=[
            _assignment(9000, requirement=world["first"], membership=world["john"]),
            _assignment(9001, requirement=world["second"], membership=world["mary"]),
        ],
        event_sequence=_sequence(),
    )

    assert get_finalization_readiness(None, version=world["version"]).is_ready is True


def test_a_wider_gap_catches_what_a_narrower_one_allows(world, monkeypatch):
    """The version's two events have 699 between neither of them, so they are
    one apart. A gap of one permits nothing between them; a gap of two is
    needed to make a *non*-adjacent pair fail, which is what this checks by
    using the history event instead.
    """
    assignments = [
        _assignment(9000, requirement=world["second"], membership=world["john"]),
    ]
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=assignments,
        # John served 699, two positions before 701 -- inside a gap of two.
        event_sequence=_sequence(gap=2, adjacent={118: frozenset({699})}),
    )

    assert MIN_EVENT_GAP_CONFLICT in _codes(
        get_finalization_readiness(None, version=world["version"])
    )

    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=assignments,
        event_sequence=_sequence(gap=1, adjacent={118: frozenset({699})}),
    )

    assert MIN_EVENT_GAP_CONFLICT not in _codes(
        get_finalization_readiness(None, version=world["version"])
    )


# ==========================================================================
# The period boundaries
# ==========================================================================


def test_serving_the_event_before_the_period_blocks_its_first_event(
    world, monkeypatch
):
    """The case the history exists for. John holds one assignment in this
    version, and the clash is entirely with last period's finalized schedule.
    """
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=[
            _assignment(9000, requirement=world["first"], membership=world["john"]),
        ],
        event_sequence=_sequence(adjacent={118: frozenset({699})}),
    )

    (issue,) = [
        issue
        for issue in get_finalization_readiness(None, version=world["version"]).issues
        if issue.code == MIN_EVENT_GAP_CONFLICT
    ]

    assert NOV_1.isoformat() in issue.message
    assert NOV_8.isoformat() in issue.message


def test_history_for_somebody_else_does_not_block(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=[
            _assignment(9000, requirement=world["first"], membership=world["john"]),
        ],
        event_sequence=_sequence(adjacent={119: frozenset({699})}),
    )

    assert MIN_EVENT_GAP_CONFLICT not in _codes(
        get_finalization_readiness(None, version=world["version"])
    )


def test_history_alone_never_raises_an_issue(world, monkeypatch):
    """Somebody with history but nothing in this version has nothing to
    finalize, so there is nothing to report about them.
    """
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=[],
        event_sequence=_sequence(adjacent={118: frozenset({699})}),
    )

    assert MIN_EVENT_GAP_CONFLICT not in _codes(
        get_finalization_readiness(None, version=world["version"])
    )


# ==========================================================================
# Reported, never repaired -- and never authorized by an override
# ==========================================================================


def test_configuring_the_rule_after_the_fact_blocks_without_deleting(
    world, monkeypatch
):
    """The safety property that lets a head tighten a rule mid-draft: the
    version becomes unfinalizable, and both assignments survive untouched.
    """
    assignments = _both_events(world, world["john"])
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=assignments,
        event_sequence=_sequence(),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is False
    assert [a.id for a in assignments] == [9000, 9001]
    assert all(a.ministry_membership_id == 118 for a in assignments)


def test_clearing_the_rule_removes_that_specific_blocker(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=_both_events(world, world["john"]),
        event_sequence=None,
    )

    assert get_finalization_readiness(None, version=world["version"]).is_ready is True


def test_an_override_does_not_authorize_a_gap_conflict(world, monkeypatch):
    """The gap rule is not one of Task 22's bounded overridable blockers, so no
    historical ``overridden_blockers`` payload can excuse it.
    """
    override = _assignment(
        9001, requirement=world["second"], membership=world["john"], is_override=True
    )
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=[
            _assignment(9000, requirement=world["first"], membership=world["john"]),
            override,
        ],
        audits=[_override_audit(9001, ["unavailable"])],
        availability_state="UNAVAILABLE",
        event_sequence=_sequence(),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert MIN_EVENT_GAP_CONFLICT in _codes(result)


def test_the_check_mutates_nothing(world, monkeypatch):
    """Read-only, like every other gate: a violation is reported, never fixed
    by deleting somebody's assignment.
    """
    assignments = _both_events(world, world["john"])
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=assignments,
        event_sequence=_sequence(),
    )

    get_finalization_readiness(None, version=world["version"])

    assert len(assignments) == 2


def test_a_commitment_after_the_period_blocks_its_last_event(monkeypatch):
    """The forward boundary at the gate. Mary holds one assignment in this
    version, on its last event, and the clash is entirely with the quarter
    already published after it.
    """
    ministry = _ministry()
    role = _role(ministry=ministry)
    second = _event(701, ministry=ministry)
    mary = _membership(119, person=_person(43, "Mary"), ministry=ministry)
    last = _requirement(601, event=second, role=role, event_date=NOV_15)

    _stub(
        monkeypatch,
        requirements=[last],
        assignments=[_assignment(9001, requirement=last, membership=mary)],
        event_sequence=_sequence(adjacent={119: frozenset({702})}),
    )

    (issue,) = [
        issue
        for issue in get_finalization_readiness(None, version=_version()).issues
        if issue.code == MIN_EVENT_GAP_CONFLICT
    ]

    assert "Mary" in issue.message
    assert NOV_15.isoformat() in issue.message
    assert NOV_22.isoformat() in issue.message


def test_a_commitment_after_the_period_for_somebody_else_does_not_block(
    world, monkeypatch
):
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=[
            _assignment(9001, requirement=world["second"], membership=world["john"]),
        ],
        event_sequence=_sequence(adjacent={119: frozenset({702})}),
    )

    assert MIN_EVENT_GAP_CONFLICT not in _codes(
        get_finalization_readiness(None, version=world["version"])
    )


def test_both_boundaries_are_reported_in_one_pass(world, monkeypatch):
    """A version whose first event clashes backwards and whose last clashes
    forwards yields both issues -- neither side is being enforced at the
    other's expense, and each is its own clash to fix.
    """
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=[
            _assignment(9000, requirement=world["first"], membership=world["john"]),
            _assignment(9001, requirement=world["second"], membership=world["mary"]),
        ],
        event_sequence=_sequence(
            adjacent={118: frozenset({699}), 119: frozenset({702})}
        ),
    )

    issues = [
        issue
        for issue in get_finalization_readiness(None, version=world["version"]).issues
        if issue.code == MIN_EVENT_GAP_CONFLICT
    ]

    assert len(issues) == 2
    messages = " ".join(issue.message for issue in issues)
    assert "John" in messages and "Mary" in messages
    assert NOV_1.isoformat() in messages
    assert NOV_22.isoformat() in messages


def test_a_commitment_beyond_the_gap_after_the_period_does_not_block(
    world, monkeypatch
):
    """702 is two positions after the version's first event, so a gap of one
    does not reach it -- while a gap of two does.
    """
    assignments = [
        _assignment(9000, requirement=world["first"], membership=world["john"]),
    ]
    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=assignments,
        event_sequence=_sequence(gap=1, adjacent={118: frozenset({702})}),
    )
    assert MIN_EVENT_GAP_CONFLICT not in _codes(
        get_finalization_readiness(None, version=world["version"])
    )

    _stub(
        monkeypatch,
        requirements=[world["first"], world["second"]],
        assignments=assignments,
        event_sequence=_sequence(gap=2, adjacent={118: frozenset({702})}),
    )
    assert MIN_EVENT_GAP_CONFLICT in _codes(
        get_finalization_readiness(None, version=world["version"])
    )
