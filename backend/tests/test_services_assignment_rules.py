"""One rule definition, two writers (Task 63).

Offline: no PostgreSQL, no network.

:mod:`app.services.assignment_rules` exists for one reason -- manual
assignment and generated-schedule persistence must apply the *same* checks,
and the only way to guarantee that is for there to be one copy of them. These
tests pin the properties that guarantee depends on:

- the rules module reads nothing, so neither writer can smuggle a query into a
  rule and make the other's answer different;
- both writers really do call it, rather than keeping a private copy;
- both fact sources expose the same interface, so a rule added tomorrow
  reaches both;
- the order of the absolute rules, which decides which of two simultaneous
  violations is reported, is the module's contract and not an accident.

Every person, ministry and date here is synthetic.
"""

from __future__ import annotations

import ast
import datetime
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import assignment_rules as rules
from app.services.assignment_policy import (
    BLOCKER_CAPACITY_FULL,
    BLOCKER_NOT_QUALIFIED,
    BLOCKER_ROLE_DEACTIVATED,
    BLOCKER_SUNDAY_CONFLICT,
    BLOCKER_UNAVAILABLE,
    OVERRIDABLE_BLOCKERS,
)
from app.services.errors import InvalidOperationError

NOV_15 = datetime.date(2026, 11, 15)
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------
# Builders -- plain namespaces, because the rules take values, not rows
# --------------------------------------------------------------------------


def _requirement(
    *,
    ministry_id: int = 3,
    required_count: int = 1,
    role_deactivated: bool = False,
    event_cancelled: bool = False,
):
    return SimpleNamespace(
        id=600,
        schedule_version_id=500,
        event_id=700,
        ministry_id=ministry_id,
        ministry_role_id=12,
        required_count=required_count,
        event_date=NOV_15,
        event=SimpleNamespace(cancelled_at=DEACTIVATED if event_cancelled else None),
        ministry_role=SimpleNamespace(
            name="Lead",
            deactivated_at=DEACTIVATED if role_deactivated else None,
            ministry=SimpleNamespace(name="Setup"),
        ),
    )


def _membership(
    *,
    ministry_id: int = 3,
    deactivated: bool = False,
    person_deactivated: bool = False,
):
    return SimpleNamespace(
        id=118,
        person_id=42,
        ministry_id=ministry_id,
        deactivated_at=DEACTIVATED if deactivated else None,
        person=SimpleNamespace(
            display_name="John",
            deactivated_at=DEACTIVATED if person_deactivated else None,
        ),
    )


class _Facts:
    """A recording :class:`~app.services.assignment_rules.PairFactSource`.

    Records *which* facts were asked for, which is how the short-circuiting
    property below is measured without counting queries.
    """

    def __init__(
        self,
        *,
        fills_other: bool = False,
        maximum: int | None = None,
        held: int = 0,
        linked_on_date: bool = False,
        is_qualified: bool = True,
        is_unavailable: bool = False,
        has_conflict: bool = False,
        filled: int = 0,
        min_intervening_events: int | None = None,
        conflicting_event_date=None,
        member_group_event_limits=(),
        support_min_supporters: int | None = None,
        supporters_present: int = 0,
        approved_supporter_count: int = 0,
    ) -> None:
        self.asked: list[str] = []
        self._fills_other = fills_other
        self._limit = rules.ServingLimitFacts(maximum=maximum, held=held)
        self._linked_on_date = linked_on_date
        # Task 71: ``None`` is "this period configures no event-gap rule",
        # which is the default world these tests describe.
        self._event_gap = rules.EventGapFacts(
            min_intervening_events=min_intervening_events,
            conflicting_event_date=conflicting_event_date,
        )
        # Task 74: an empty tuple is "no capped member group touches this
        # member", and ``None`` is "no same-event support requirement" -- the
        # default world these tests describe.
        self._member_group_event_limits = tuple(member_group_event_limits)
        self._support = rules.SameEventSupportFacts(
            min_supporters=support_min_supporters,
            supporters_present=supporters_present,
            approved_supporter_count=approved_supporter_count,
        )
        # The church-wide hard rule's own fact, since it stopped being
        # overridable: asked by its own method, as an absolute rule.
        self._has_conflict = has_conflict
        self._overridable = rules.OverridableFacts(
            is_qualified=is_qualified,
            is_unavailable=is_unavailable,
            current_filled_count=filled,
        )

    def fills_other_position_in_event(self) -> bool:
        self.asked.append("fills_other_position_in_event")
        return self._fills_other

    def serving_limit(self) -> rules.ServingLimitFacts:
        self.asked.append("serving_limit")
        return self._limit

    def linked_member_assigned_on_date(self) -> bool:
        self.asked.append("linked_member_assigned_on_date")
        return self._linked_on_date

    def event_gap(self) -> rules.EventGapFacts:
        self.asked.append("event_gap")
        return self._event_gap

    def member_group_event_limits(self) -> tuple[rules.MemberGroupEventFacts, ...]:
        self.asked.append("member_group_event_limits")
        return self._member_group_event_limits

    def same_event_support(self) -> rules.SameEventSupportFacts:
        self.asked.append("same_event_support")
        return self._support

    def has_cross_ministry_sunday_conflict(self) -> bool:
        self.asked.append("has_cross_ministry_sunday_conflict")
        return self._has_conflict

    def overridable_facts(self) -> rules.OverridableFacts:
        self.asked.append("overridable_facts")
        return self._overridable


# ==========================================================================
# The module reads nothing -- the property both writers depend on
# ==========================================================================


def test_the_rules_module_imports_nothing_that_can_query():
    """Not a style rule. A rule that read the database would have to be
    re-read per row, and the batch writer would need its own copy -- which is
    precisely the drift this module exists to make impossible.
    """
    source = Path(rules.__file__).read_text()
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
            imported.update(f"{node.module}.{a.name}" for a in node.names)

    assert not [name for name in imported if name.startswith("sqlalchemy")]
    assert "app.db" not in imported
    for banned in ("select", "Session", "Select", "engine"):
        assert not [name for name in imported if name.endswith(f".{banned}")], banned


def test_nothing_is_ever_called_on_the_one_session_parameter():
    """The single ``session`` this module accepts is handed straight to
    ``record_audit_event``, which issues no SQL of its own. Nothing here calls
    a method on it -- no ``execute``, ``add``, ``flush``, ``commit``,
    ``rollback`` or ``delete``.
    """
    tree = ast.parse(Path(rules.__file__).read_text())
    on_session = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "session"
    }

    assert on_session == set()


# ==========================================================================
# Both writers use it, and both fact sources answer the same questions
# ==========================================================================


def _protocol_methods() -> set[str]:
    return {
        name
        for name in vars(rules.PairFactSource)
        if not name.startswith("_")
    }


def test_both_fact_sources_implement_the_whole_protocol():
    """A rule added to the protocol tomorrow must reach both writers or fail
    loudly here -- never reach one and silently not the other.
    """
    from app.services.assignment import _RowFactSource
    from app.services.generated_assignment import _PairFacts

    expected = _protocol_methods()
    assert expected  # the protocol is not empty, or this proves nothing

    for source in (_RowFactSource, _PairFacts):
        for name in expected:
            assert hasattr(source, name), f"{source.__name__} lacks {name}"
            assert (
                list(inspect.signature(getattr(source, name)).parameters) == ["self"]
            ), f"{source.__name__}.{name} takes arguments the rules cannot supply"


def test_both_writers_call_the_shared_rules():
    """Checked against each module's actual calls, not by grepping prose."""
    import app.services.assignment as manual
    import app.services.generated_assignment as batch

    for module in (manual, batch):
        called = {
            node.func.id
            for node in ast.walk(ast.parse(Path(module.__file__).read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "require_absolute_rules" in called, module.__name__
        assert "collect_overridable_blockers" in called, module.__name__
        assert "build_assignment" in called, module.__name__
        assert "record_assignment_created" in called, module.__name__


def _raised_messages(module) -> str:
    """Every string literal either writer actually raises, prose excluded.

    Docstrings legitimately *describe* the rules -- both modules explain them
    at length -- so comparing source text would only measure how much they
    explain. What must not be duplicated is the rule itself, which is a
    ``raise``.
    """
    tree = ast.parse(Path(module.__file__).read_text())
    parts: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                parts.append(inner.value)
    return " ".join(parts)


def test_neither_writer_keeps_a_private_copy_of_a_rule():
    """The messages most at risk of being re-typed are the serving-maximum
    refusal, the linked-member refusal and the override refusal. Neither
    writer may *raise* any of them: those sentences exist once.
    """
    import app.services.assignment as manual
    import app.services.generated_assignment as batch

    for module in (manual, batch):
        raised = _raised_messages(module)
        assert "serving maximum" not in raised, module.__name__
        assert "same-date exclusion" not in raised, module.__name__
        assert "without an override" not in raised, module.__name__
        assert "deactivated membership" not in raised, module.__name__
        assert "cancelled event" not in raised, module.__name__
    # And the rules module does raise them, so the assertions above mean
    # "moved", not "deleted".
    moved = _raised_messages(rules)
    for sentence in (
        "serving maximum", "same-date exclusion", "without an override",
        "deactivated membership", "cancelled event",
    ):
        assert sentence in moved, sentence


# ==========================================================================
# The absolute rules, in order
# ==========================================================================


def test_a_ministry_mismatch_is_rejected_before_any_fact_is_read():
    with pytest.raises(InvalidOperationError, match="same ministry"):
        rules.require_absolute_rules(
            requirement=_requirement(ministry_id=3),
            membership=_membership(ministry_id=4),
            facts=(facts := _Facts()),
        )
    assert facts.asked == []


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"deactivated": True}, "deactivated membership"),
        ({"person_deactivated": True}, "deactivated person"),
    ],
)
def test_inactive_membership_and_person_are_rejected(kwargs, message):
    with pytest.raises(InvalidOperationError, match=message):
        rules.require_absolute_rules(
            requirement=_requirement(),
            membership=_membership(**kwargs),
            facts=(facts := _Facts()),
        )
    assert facts.asked == []


def test_a_cancelled_event_is_rejected():
    with pytest.raises(InvalidOperationError, match="cancelled event"):
        rules.require_absolute_rules(
            requirement=_requirement(event_cancelled=True),
            membership=_membership(),
            facts=(facts := _Facts()),
        )
    assert facts.asked == []


def test_a_second_position_in_one_event_is_rejected():
    facts = _Facts(fills_other=True)
    with pytest.raises(InvalidOperationError, match="already fills a different"):
        rules.require_absolute_rules(
            requirement=_requirement(), membership=_membership(), facts=facts
        )
    # And the reads a later rule would have needed were never taken.
    assert facts.asked == ["fills_other_position_in_event"]


def test_the_serving_maximum_is_enforced_and_says_how_to_move_it():
    facts = _Facts(maximum=2, held=2)
    with pytest.raises(InvalidOperationError) as excinfo:
        rules.require_absolute_rules(
            requirement=_requirement(), membership=_membership(), facts=facts
        )

    message = str(excinfo.value)
    assert "serving maximum" in message
    assert "(2 of 2 already assigned)" in message
    assert "not overridable" in message
    assert "set_serving_limit" in message
    assert facts.asked == ["fills_other_position_in_event", "serving_limit"]


def test_no_configured_maximum_never_behaves_like_zero():
    """Absence of a limit is the ordinary case, and ``held`` is not even
    required to be accurate when there is no maximum.
    """
    rules.require_absolute_rules(
        requirement=_requirement(),
        membership=_membership(),
        facts=_Facts(maximum=None, held=99),
    )


def test_a_maximum_with_room_left_is_allowed():
    rules.require_absolute_rules(
        requirement=_requirement(),
        membership=_membership(),
        facts=_Facts(maximum=3, held=2),
    )


def test_a_linked_member_on_the_same_date_is_rejected():
    facts = _Facts(linked_on_date=True)
    with pytest.raises(InvalidOperationError) as excinfo:
        rules.require_absolute_rules(
            requirement=_requirement(), membership=_membership(), facts=facts
        )

    message = str(excinfo.value)
    assert "same-date exclusion" in message
    assert "John" in message            # who the rule is about
    assert "2026-11-15" in message      # the snapshot date, not the live event
    assert "not overridable" in message
    assert facts.asked == [
        "fills_other_position_in_event", "serving_limit",
        "linked_member_assigned_on_date",
    ]


def test_the_rule_order_is_the_contract():
    """Two violations at once report the earlier rule, both times -- so the
    two writers cannot disagree about which problem is "the" problem.
    """
    with pytest.raises(InvalidOperationError, match="deactivated membership"):
        rules.require_absolute_rules(
            requirement=_requirement(event_cancelled=True),
            membership=_membership(deactivated=True),
            facts=_Facts(fills_other=True, maximum=1, held=5, linked_on_date=True),
        )


def test_a_clean_pair_passes_and_asks_exactly_the_absolute_rules():
    """Six facts, in the module's declared order. The list is asserted rather
    than its length so a rule inserted in the middle -- which would change
    *which* violation is reported first -- fails here.

    ``has_cross_ministry_sunday_conflict`` is **last**, and deliberately:
    every rule before it is a fact about this ministry and this version, and
    it is the only one that consults the rest of the church. Placing it last
    keeps every pre-existing rejection message and ordering exactly as it was
    when the rule moved here from the overridable catalogue.

    ``same_event_support`` is deliberately absent: it is not one of the rules
    this function applies, because it is not a property of one placement
    against existing state (see the function's own docstring). Each writer
    applies it itself, at the moment its state is complete.
    """
    facts = _Facts()
    rules.require_absolute_rules(
        requirement=_requirement(), membership=_membership(), facts=facts
    )
    assert facts.asked == [
        "fills_other_position_in_event", "serving_limit",
        "linked_member_assigned_on_date", "event_gap",
        "member_group_event_limits", "has_cross_ministry_sunday_conflict",
    ]


# ==========================================================================
# The overridable catalogue
# ==========================================================================


def test_no_blockers_when_everything_is_in_order():
    assert rules.collect_overridable_blockers(
        requirement=_requirement(), facts=_Facts().overridable_facts()
    ) == frozenset()


@pytest.mark.parametrize(
    "requirement_kwargs,fact_kwargs,expected",
    [
        ({"role_deactivated": True}, {}, BLOCKER_ROLE_DEACTIVATED),
        ({}, {"is_qualified": False}, BLOCKER_NOT_QUALIFIED),
        ({}, {"is_unavailable": True}, BLOCKER_UNAVAILABLE),
        ({"required_count": 1}, {"filled": 1}, BLOCKER_CAPACITY_FULL),
    ],
)
def test_each_overridable_check_has_its_own_code(
    requirement_kwargs, fact_kwargs, expected
):
    blockers = rules.collect_overridable_blockers(
        requirement=_requirement(**requirement_kwargs),
        facts=_Facts(**fact_kwargs).overridable_facts(),
    )
    assert blockers == frozenset({expected})
    assert expected in OVERRIDABLE_BLOCKERS


def test_every_violated_check_is_named_at_once_not_one_at_a_time():
    blockers = rules.collect_overridable_blockers(
        requirement=_requirement(role_deactivated=True, required_count=1),
        facts=_Facts(
            is_qualified=False, is_unavailable=True, filled=1
        ).overridable_facts(),
    )
    assert blockers == OVERRIDABLE_BLOCKERS


def test_the_cross_ministry_conflict_can_never_be_an_overridable_blocker():
    """**The correction, stated where it cannot be undone quietly.**

    One Person serves at most one ministry per Sunday is a hard church-wide
    rule. It used to be the fifth bounded blocker, so an ``override_reason``
    bypassed it; it is now an absolute rule, and this catalogue has no way to
    name it at all -- ``OverridableFacts`` carries no such field, and the code
    is not in the set an override may authorize.
    """
    import dataclasses

    fields = {f.name for f in dataclasses.fields(rules.OverridableFacts)}
    assert "has_sunday_conflict" not in fields
    assert BLOCKER_SUNDAY_CONFLICT not in OVERRIDABLE_BLOCKERS

    # And a conflicting pair produces no blocker here, whatever else is true.
    assert rules.collect_overridable_blockers(
        requirement=_requirement(),
        facts=_Facts(has_conflict=True).overridable_facts(),
    ) == frozenset()


def test_the_cross_ministry_conflict_is_refused_regardless_of_any_reason():
    """It is raised by :func:`require_absolute_rules`, which never sees an
    ``override_reason`` -- so there is no argument that reaches it."""
    with pytest.raises(InvalidOperationError) as caught:
        rules.require_absolute_rules(
            requirement=_requirement(),
            membership=_membership(),
            facts=_Facts(has_conflict=True),
        )
    message = str(caught.value)
    assert rules.CROSS_MINISTRY_SUNDAY_CONFLICT in message
    assert "not overridable" in message


def test_the_cross_ministry_conflict_code_is_still_recognised_history():
    """The string is persisted audit history and must keep parsing: rows
    written while it *was* overridable are truthful records, not corruption.
    """
    from app.services.assignment_policy import KNOWN_BLOCKERS

    assert BLOCKER_SUNDAY_CONFLICT == "sunday_conflict"
    assert BLOCKER_SUNDAY_CONFLICT in KNOWN_BLOCKERS
    assert OVERRIDABLE_BLOCKERS < KNOWN_BLOCKERS


def test_capacity_is_compared_against_the_snapshot_required_count():
    """``required_count`` is the version's own frozen capacity, never current
    staffing input, and an over-filled requirement stays blocked.
    """
    assert rules.collect_overridable_blockers(
        requirement=_requirement(required_count=3), facts=_Facts(filled=2).overridable_facts()
    ) == frozenset()
    assert rules.collect_overridable_blockers(
        requirement=_requirement(required_count=3), facts=_Facts(filled=4).overridable_facts()
    ) == frozenset({BLOCKER_CAPACITY_FULL})


def test_the_serving_maximum_is_not_in_the_overridable_catalogue():
    """It must never be mapped into the bounded vocabulary: those codes are
    persisted in audit history as the authorization for an override.
    """
    source = Path(rules.__file__).read_text()
    catalogue = source[source.index("def collect_overridable_blockers"):]
    catalogue = catalogue[: catalogue.index("def describe_blockers")]

    assert "serving_maximum" not in catalogue
    assert "linked_member" not in catalogue


# ==========================================================================
# The override symmetry
# ==========================================================================


def test_a_blocked_placement_without_a_reason_is_refused_naming_every_check():
    with pytest.raises(InvalidOperationError) as excinfo:
        rules.resolve_override(
            blockers=frozenset({BLOCKER_UNAVAILABLE, BLOCKER_NOT_QUALIFIED}),
            override_reason=None,
        )
    message = str(excinfo.value)
    assert "cannot assign without an override" in message
    assert "not currently qualified" in message
    assert "marked unavailable" in message


def test_a_reason_with_nothing_to_override_is_refused_too():
    with pytest.raises(InvalidOperationError, match="no overridable rule was violated"):
        rules.resolve_override(blockers=frozenset(), override_reason="because")


def test_the_two_ordinary_outcomes():
    assert rules.resolve_override(blockers=frozenset(), override_reason=None) is False
    assert (
        rules.resolve_override(
            blockers=frozenset({BLOCKER_UNAVAILABLE}), override_reason="agreed"
        )
        is True
    )


def test_blocker_descriptions_are_rendered_in_a_deterministic_order():
    first = rules.describe_blockers(OVERRIDABLE_BLOCKERS)
    second = rules.describe_blockers(frozenset(reversed(sorted(OVERRIDABLE_BLOCKERS))))
    assert first == second


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_optional_text_is_refused(blank):
    with pytest.raises(InvalidOperationError, match="must not be blank"):
        rules.validate_optional_text(blank, field="override_reason")


def test_absent_optional_text_stays_absent():
    assert rules.validate_optional_text(None, field="override_reason") is None
    assert rules.validate_optional_text("ok", field="override_reason") == "ok"


# ==========================================================================
# The row and its audit payload
# ==========================================================================


def test_the_built_row_carries_every_pinned_column():
    assignment = rules.build_assignment(
        requirement=_requirement(), membership=_membership(),
        is_override=False, override_reason=None,
    )
    assert assignment.schedule_version_requirement_id == 600
    assert assignment.ministry_membership_id == 118
    assert assignment.schedule_version_id == 500
    assert assignment.event_id == 700
    assert assignment.ministry_id == 3
    assert assignment.is_override is False
    assert assignment.override_reason is None


def test_the_summary_names_the_snapshot_date_not_the_live_event():
    summary = rules.assign_summary(
        membership=_membership(), requirement=_requirement(), is_override=False
    )
    assert summary == "Assigned John to Lead for Setup on 2026-11-15"
    assert rules.assign_summary(
        membership=_membership(), requirement=_requirement(), is_override=True
    ).endswith(" with override")


def test_an_ordinary_assignment_never_claims_overridden_blockers():
    payload = rules.assign_after_values(
        requirement=_requirement(), membership=_membership(),
        is_override=False, override_reason=None, blockers=frozenset(),
    )
    assert "overridden_blockers" not in payload
    assert payload["is_override"] is False
    assert payload["override_reason"] is None


def test_an_override_records_exactly_which_rules_were_bypassed_sorted():
    payload = rules.assign_after_values(
        requirement=_requirement(), membership=_membership(),
        is_override=True, override_reason="agreed with the head",
        blockers=frozenset({BLOCKER_UNAVAILABLE, BLOCKER_NOT_QUALIFIED}),
    )
    assert payload["overridden_blockers"] == sorted(
        [BLOCKER_NOT_QUALIFIED, BLOCKER_UNAVAILABLE]
    )
    assert payload["override_reason"] == "agreed with the head"


def test_the_payload_is_business_state_not_the_whole_row():
    payload = rules.assign_after_values(
        requirement=_requirement(), membership=_membership(),
        is_override=False, override_reason=None, blockers=frozenset(),
    )
    assert set(payload) == {
        "schedule_version_requirement_id",
        "ministry_membership_id",
        "schedule_version_id",
        "event_id",
        "is_override",
        "override_reason",
    }


# ==========================================================================
# The version-status half of the mutability rule
# ==========================================================================


@pytest.mark.parametrize("status", ["DRAFT", "REVIEW"])
def test_a_working_version_may_be_written_to(status):
    rules.require_mutable_working_version_status(SimpleNamespace(status=status))


def test_a_finalized_version_may_not():
    with pytest.raises(InvalidOperationError, match="finalized schedule version"):
        rules.require_mutable_working_version_status(
            SimpleNamespace(status="FINALIZED")
        )


def test_the_status_check_cannot_answer_the_superseded_question():
    """Deliberately: "is there a newer version?" lives on other rows, so each
    writer queries it and this function is not given the chance to pretend.
    """
    assert list(
        inspect.signature(rules.require_mutable_working_version_status).parameters
    ) == ["schedule_version"]


# ==========================================================================
# Task 74: the member-group cap and the same-event support requirement
# ==========================================================================


def _limit(max_per_event: int, present: int, *, name: str = "Category A"):
    return rules.MemberGroupEventFacts(
        member_group_name=name, max_per_event=max_per_event,
        members_present=present,
    )


def test_no_capped_group_never_behaves_like_a_cap_of_zero():
    """An empty tuple is "no group they are in carries a cap", which is the
    ordinary case and must never refuse anything.
    """
    facts = _Facts(member_group_event_limits=())

    rules.require_absolute_rules(
        requirement=_requirement(), membership=_membership(), facts=facts
    )


def test_a_cap_with_room_left_is_allowed():
    facts = _Facts(member_group_event_limits=(_limit(2, 1),))

    rules.require_absolute_rules(
        requirement=_requirement(), membership=_membership(), facts=facts
    )


def test_a_cap_already_reached_refuses_and_names_the_group():
    facts = _Facts(member_group_event_limits=(_limit(1, 1),))

    with pytest.raises(InvalidOperationError) as raised:
        rules.require_absolute_rules(
            requirement=_requirement(), membership=_membership(), facts=facts
        )

    message = str(raised.value)
    assert "MEMBER_GROUP_EVENT_LIMIT_CONFLICT" in message
    assert "Category A" in message
    assert "not overridable" in message


def test_the_first_breached_cap_refuses_and_the_rest_are_not_evaluated():
    """A member in two capped groups must satisfy both; reporting one is enough
    to act on, and continuing would cost reads a rejection made unnecessary.
    """
    facts = _Facts(
        member_group_event_limits=(
            _limit(1, 1, name="Category A"),
            _limit(1, 1, name="Category B"),
        )
    )

    with pytest.raises(InvalidOperationError) as raised:
        rules.require_absolute_rules(
            requirement=_requirement(), membership=_membership(), facts=facts
        )

    assert "Category A" in str(raised.value)
    assert "Category B" not in str(raised.value)


def test_no_support_requirement_never_behaves_like_a_requirement_of_zero():
    rules.require_same_event_support(
        rules.SameEventSupportFacts(min_supporters=None),
        subject_display_name="John",
        event_date=datetime.date(2026, 11, 8),
    )


def test_enough_supporters_present_is_allowed():
    rules.require_same_event_support(
        rules.SameEventSupportFacts(
            min_supporters=1, supporters_present=1, approved_supporter_count=2
        ),
        subject_display_name="John",
        event_date=datetime.date(2026, 11, 8),
    )


def test_too_few_supporters_present_refuses_and_points_at_the_roster():
    with pytest.raises(InvalidOperationError) as raised:
        rules.require_same_event_support(
            rules.SameEventSupportFacts(
                min_supporters=1, supporters_present=0, approved_supporter_count=2
            ),
            subject_display_name="John",
            event_date=datetime.date(2026, 11, 8),
        )

    message = str(raised.value)
    assert "SAME_EVENT_SUPPORT_CONFLICT" in message
    assert "John" in message
    assert "2026-11-08" in message
    assert "assign one of their approved supporting members" in message
    assert "not overridable" in message


def test_too_few_supporters_approved_points_at_the_configuration_instead():
    """Two different failures a head fixes in two different places."""
    with pytest.raises(InvalidOperationError) as raised:
        rules.require_same_event_support(
            rules.SameEventSupportFacts(
                min_supporters=2, supporters_present=1, approved_supporter_count=1
            ),
            subject_display_name="John",
            event_date=datetime.date(2026, 11, 8),
        )

    assert "fewer than the requirement asks for" in str(raised.value)


def test_the_support_rule_is_not_one_of_the_absolute_rules():
    """It is the one rule that is not a property of a single placement against
    existing state, so each writer applies it at the moment its state is
    complete -- see ``require_absolute_rules``' own docstring.
    """
    facts = _Facts(support_min_supporters=1, supporters_present=0)

    rules.require_absolute_rules(
        requirement=_requirement(), membership=_membership(), facts=facts
    )

    assert "same_event_support" not in facts.asked


def test_both_writers_call_the_shared_support_rule():
    """Checked against each module's actual calls, not by grepping prose: the
    rule must reach both writers or the two would drift.
    """
    import app.services.assignment as manual
    import app.services.generated_assignment as batch

    for module in (manual, batch):
        called = {
            node.func.id
            for node in ast.walk(ast.parse(Path(module.__file__).read_text()))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "require_same_event_support" in called, module.__name__


def test_neither_writer_keeps_a_private_copy_of_the_new_rules():
    """Their sentences exist once, in this module."""
    import app.services.assignment as manual
    import app.services.generated_assignment as batch

    for module in (manual, batch):
        raised = _raised_messages(module)
        assert "member group" not in raised, module.__name__
        assert "approved supporting" not in raised, module.__name__

    moved = _raised_messages(rules)
    assert "member group" in moved
    assert "approved supporting" in moved
