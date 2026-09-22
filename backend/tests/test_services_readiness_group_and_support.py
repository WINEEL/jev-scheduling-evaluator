"""The two Task 74 finalization gates (readiness gates 7 and 8).

Offline: no PostgreSQL, no network.

Both gates report what is wrong and repair nothing -- the same contract every
other readiness gate keeps. The properties that matter, and are pinned
separately:

- an unconfigured period reports nothing, ever;
- a configured one catches a version that breaks the rule, **including one
  built before the rule existed**, because the gate asks about the version's
  current contents rather than how they got there;
- both gates count per **event**, not per date, which is the difference between
  them and the linked-pair gate;
- an override authorizes nothing in either, because neither rule is one of the
  bounded overridable blockers;
- gate 8 is what catches the case removal deliberately allows -- taking a
  supporter off an event leaves the subject unsupported, and nothing repairs it.

Both rules' own meaning lives in ``tests/test_services_member_group.py`` and
``tests/test_services_same_event_support.py``; the builders and the stub here
are the ones ``tests/test_services_finalization_readiness.py`` already uses,
imported rather than re-written so the suites cannot describe two different
worlds.

Every person, ministry, group and date here is synthetic, and nothing here
describes a real arrangement of any kind.
"""

from __future__ import annotations

import datetime

import pytest

from app.services.finalization_readiness import get_finalization_readiness
from app.services.member_group import (
    MEMBER_GROUP_EVENT_LIMIT_CONFLICT,
    MemberGroupCapConfig,
)
from app.services.same_event_support import (
    SAME_EVENT_SUPPORT_CONFLICT,
    SupportRequirementConfig,
)
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

NOV_8 = datetime.date(2026, 11, 8)
NOV_15 = datetime.date(2026, 11, 15)

GROUP_NAME = "Category A"


@pytest.fixture
def world():
    """One ministry, one role, two events -- and, on one date, two of them.

    Events 700 and 702 share a date so the per-event reading can be told apart
    from a per-date one; 701 is the following Sunday.
    """
    ministry = _ministry()
    role = _role(ministry=ministry)
    first = _event(700, ministry=ministry)
    second = _event(701, ministry=ministry)
    evening = _event(702, ministry=ministry)
    return {
        "ministry": ministry,
        "role": role,
        "version": _version(),
        "john": _membership(118, person=_person(42, "John"), ministry=ministry),
        "mary": _membership(119, person=_person(43, "Mary"), ministry=ministry),
        "ann": _membership(120, person=_person(44, "Ann"), ministry=ministry),
        # Two positions on the first event, one on the second, one on the
        # evening service that shares the first event's date.
        "first_a": _requirement(600, event=first, role=role, event_date=NOV_8),
        "first_b": _requirement(601, event=first, role=role, event_date=NOV_8),
        "first_c": _requirement(602, event=first, role=role, event_date=NOV_8),
        "second": _requirement(603, event=second, role=role, event_date=NOV_15),
        "evening": _requirement(604, event=evening, role=role, event_date=NOV_8),
    }


def _cap(max_per_event: int, members) -> MemberGroupCapConfig:
    return MemberGroupCapConfig(
        member_group_id=400, member_group_name=GROUP_NAME,
        max_per_event=max_per_event, member_membership_ids=frozenset(members),
    )


def _support(minimum: int, supporters, *, subject: int) -> SupportRequirementConfig:
    return SupportRequirementConfig(
        support_requirement_id=1, subject_membership_id=subject,
        min_supporters=minimum, supporter_membership_ids=frozenset(supporters),
    )


# ==========================================================================
# Gate 7: the member-group per-event cap
# ==========================================================================


def test_no_configured_cap_reports_nothing(world, monkeypatch):
    """Three group members on one event, and a period that never asked for a
    cap: perfectly ready, exactly as before Task 74.
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"], world["first_c"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["mary"]),
            _assignment(9002, requirement=world["first_c"], membership=world["ann"]),
        ],
        member_group_caps=(),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is True
    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT not in _codes(result)


def test_exactly_the_cap_is_ready(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["mary"]),
        ],
        member_group_caps=(_cap(2, (118, 119, 120)),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is True


def test_one_over_the_cap_blocks_finalization(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"], world["first_c"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["mary"]),
            _assignment(9002, requirement=world["first_c"], membership=world["ann"]),
        ],
        member_group_caps=(_cap(2, (118, 119, 120)),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is False
    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT in _codes(result)


def test_one_issue_per_event_not_one_per_assignment(world, monkeypatch):
    """The problem is the composition of the crew, not any one row. Emitting an
    issue against each member would say the same thing three times while
    implying that one in particular is wrong.
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"], world["first_c"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["mary"]),
            _assignment(9002, requirement=world["first_c"], membership=world["ann"]),
        ],
        member_group_caps=(_cap(1, (118, 119, 120)),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    cap_issues = [
        issue
        for issue in result.issues
        if issue.code == MEMBER_GROUP_EVENT_LIMIT_CONFLICT
    ]
    assert len(cap_issues) == 1
    assert "3 members" in cap_issues[0].message
    assert GROUP_NAME in cap_issues[0].message
    assert NOV_8.isoformat() in cap_issues[0].message


def test_the_cap_is_counted_per_event_not_per_date(world, monkeypatch):
    """Two services on one Sunday are two crews. One group member at each is
    within a cap of one; a date-level reading would report a violation.
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["evening"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["evening"], membership=world["mary"]),
        ],
        member_group_caps=(_cap(1, (118, 119)),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT not in _codes(result)


def test_a_version_built_before_the_cap_existed_is_still_caught(world, monkeypatch):
    """The gate asks about the version's current contents, never how they got
    there -- which is what makes recording a cap after a draft exists safe.
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["mary"]),
        ],
        member_group_caps=(_cap(1, (118, 119)),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT in _codes(result)


def test_an_override_authorizes_nothing_against_the_cap(world, monkeypatch):
    """The cap is not one of the bounded overridable blockers, so no historical
    ``overridden_blockers`` payload can excuse it.
    """
    overridden = _assignment(
        9001, requirement=world["first_b"], membership=world["mary"],
        is_override=True,
    )
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            overridden,
        ],
        audits=[_override_audit(9001, ["UNAVAILABLE"])],
        member_group_caps=(_cap(1, (118, 119)),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT in _codes(result)


def test_the_gate_repairs_nothing(world, monkeypatch):
    """Read-only is the whole contract: the assignments are still there
    afterwards, and the result is a report rather than a change.
    """
    assignments = [
        _assignment(9000, requirement=world["first_a"], membership=world["john"]),
        _assignment(9001, requirement=world["first_b"], membership=world["mary"]),
    ]
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"]],
        assignments=assignments,
        member_group_caps=(_cap(1, (118, 119)),),
    )

    get_finalization_readiness(None, version=world["version"])

    assert [a.id for a in assignments] == [9000, 9001]


# ==========================================================================
# Gate 8: the same-event support requirement
# ==========================================================================


def test_no_configured_requirement_reports_nothing(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first_a"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
        ],
        support_requirements=(),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is True
    assert SAME_EVENT_SUPPORT_CONFLICT not in _codes(result)


def test_a_subject_absent_from_an_event_is_not_a_problem(world, monkeypatch):
    """The rule never obliges anybody to serve: an event the subject is not on
    is unconstrained, whoever else is there.
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["ann"]),
        ],
        support_requirements=(_support(1, (119,), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is True


def test_a_subject_with_their_supporter_present_is_ready(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["mary"]),
        ],
        support_requirements=(_support(1, (119,), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is True


def test_a_subject_without_support_blocks_finalization(world, monkeypatch):
    """Exactly the state a supporter's removal leaves behind: the subject's row
    is untouched, and the version simply cannot be published.
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["ann"]),
        ],
        support_requirements=(_support(1, (119,), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert result.is_ready is False
    issues = [
        issue for issue in result.issues if issue.code == SAME_EVENT_SUPPORT_CONFLICT
    ]
    assert len(issues) == 1
    assert "John" in issues[0].message
    assert NOV_8.isoformat() in issues[0].message
    assert "0 of the 1 required" in issues[0].message


def test_the_requirement_is_counted_per_event_not_per_date(world, monkeypatch):
    """The supporter is at the *other* service on the same Sunday, so the
    subject's crew still lacks them -- the opposite reading from the
    linked-pair gate, and deliberately so.
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["evening"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["evening"], membership=world["mary"]),
        ],
        support_requirements=(_support(1, (119,), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert SAME_EVENT_SUPPORT_CONFLICT in _codes(result)


def test_a_requirement_for_two_with_only_one_present_is_caught(world, monkeypatch):
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["mary"]),
        ],
        support_requirements=(_support(2, (119, 120), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert SAME_EVENT_SUPPORT_CONFLICT in _codes(result)


def test_one_issue_per_subject_and_event(world, monkeypatch):
    """The subject serves two events unsupported: two issues, one each, and
    never one per assignment on those crews.
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["second"], world["first_b"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["second"], membership=world["john"]),
            _assignment(9002, requirement=world["first_b"], membership=world["ann"]),
        ],
        support_requirements=(_support(1, (119,), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    issues = [
        issue for issue in result.issues if issue.code == SAME_EVENT_SUPPORT_CONFLICT
    ]
    assert len(issues) == 2


def test_a_version_built_before_the_requirement_existed_is_still_caught(
    world, monkeypatch
):
    _stub(
        monkeypatch,
        requirements=[world["first_a"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
        ],
        support_requirements=(_support(1, (119,), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert SAME_EVENT_SUPPORT_CONFLICT in _codes(result)


def test_an_override_authorizes_nothing_against_the_requirement(world, monkeypatch):
    overridden = _assignment(
        9000, requirement=world["first_a"], membership=world["john"],
        is_override=True,
    )
    _stub(
        monkeypatch,
        requirements=[world["first_a"]],
        assignments=[overridden],
        audits=[_override_audit(9000, ["UNAVAILABLE"])],
        support_requirements=(_support(1, (119,), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    assert SAME_EVENT_SUPPORT_CONFLICT in _codes(result)


def test_the_support_message_says_nothing_about_why(world, monkeypatch):
    """The system does not know why the support is required and must not
    imply that it does (requirements §4.7).
    """
    _stub(
        monkeypatch,
        requirements=[world["first_a"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
        ],
        support_requirements=(_support(1, (119,), subject=118),),
    )

    result = get_finalization_readiness(None, version=world["version"])

    (issue,) = [
        issue for issue in result.issues if issue.code == SAME_EVENT_SUPPORT_CONFLICT
    ]
    for forbidden in ("transport", "lift", "ride", "drive", "family", "spouse",
                      "partner", "household"):
        assert forbidden not in issue.message.lower()


def test_both_gates_can_report_at_once(world, monkeypatch):
    """They are independent, and one being violated must not hide the other."""
    _stub(
        monkeypatch,
        requirements=[world["first_a"], world["first_b"]],
        assignments=[
            _assignment(9000, requirement=world["first_a"], membership=world["john"]),
            _assignment(9001, requirement=world["first_b"], membership=world["ann"]),
        ],
        member_group_caps=(_cap(1, (118, 120)),),
        support_requirements=(_support(1, (119,), subject=118),),
    )

    codes = _codes(get_finalization_readiness(None, version=world["version"]))

    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT in codes
    assert SAME_EVENT_SUPPORT_CONFLICT in codes
