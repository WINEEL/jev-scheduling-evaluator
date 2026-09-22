"""The period's scheduling-rules endpoints over HTTP (Tasks 71 and 74).

Offline: no database. The Session is a recording stand-in answering the
statements these routes issue, and the three rule services are stubbed where
the subject is HTTP mapping rather than domain behaviour -- that behaviour has
its own dedicated coverage in ``tests/test_services_event_gap.py``,
``tests/test_services_member_group.py`` and
``tests/test_services_same_event_support.py``.

What is pinned here is the contract a Ministry Head's screen depends on:
``null`` means "no rule" and never ``0``; ``PUT`` takes a positive number and
nothing else; ``DELETE`` is how a rule is cleared; and the service's own
refusals keep their status codes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import dependencies as deps
from app.api import routes_scheduling_rules as routes
from app.config import get_settings
from app.main import app
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.event_gap import PeriodEventGapRule
from app.services.member_group import PeriodMemberGroupLimits
from app.services.same_event_support import PeriodSupportRequirements

DEV_FLAG = "CHURCH_SCHEDULING_DEV_AUTH"
HEADER = "X-Dev-Actor-Person-Id"
RULES_URL = "/api/v1/scheduling-periods/{id}/scheduling-rules"
GAP_URL = "/api/v1/scheduling-periods/{id}/scheduling-rules/min-intervening-events"


class _StubPerson:
    def __init__(self, id: int = 1) -> None:
        self.id = id
        self.display_name = "Ada Head"
        self.is_admin = False
        self.deactivated_at = None
        self.ministry_memberships: list = []


class _StubPeriod:
    def __init__(self, id: int = 500, **overrides) -> None:
        self.id = id
        self.ministry_id = 900
        self.name = "Q4 2026"
        self.min_intervening_events = None
        for key, value in overrides.items():
            setattr(self, key, value)


class _ScalarResult:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class RecordingSession:
    def __init__(self, *, person=None, period=None) -> None:
        self.person = _StubPerson() if person is None else person
        self.period = _StubPeriod() if period is None else period
        self.transaction_events: list[str] = []

    def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "FROM scheduling_period" in sql:
            return _ScalarResult(self.period)
        if "FROM person" in sql:
            return _ScalarResult(self.person)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    def commit(self) -> None:
        self.transaction_events.append("commit")

    def rollback(self) -> None:
        self.transaction_events.append("rollback")

    def close(self) -> None:
        self.transaction_events.append("close")


@pytest.fixture
def session(monkeypatch) -> RecordingSession:
    recording = RecordingSession()
    monkeypatch.setattr(deps, "SessionLocal", lambda: recording)
    return recording


class _CallLog(list):
    behaviour: dict


def _stub(monkeypatch, name: str, *, result):
    calls = _CallLog()
    behaviour: dict = {"result": result, "raises": None}

    def fake(session, *, actor, **kwargs):
        calls.append({"session": session, "actor": actor, **kwargs})
        if behaviour["raises"] is not None:
            raise behaviour["raises"]
        return behaviour["result"]

    monkeypatch.setattr(routes, name, fake)
    calls.behaviour = behaviour
    return calls


@pytest.fixture(autouse=True)
def other_rule_readers(monkeypatch):
    """Task 74's two rules read as unconfigured unless a test says otherwise.

    Autouse, because the collection ``GET`` reports all three rules and every
    mutation re-reads all three afterwards; a test about the event gap should
    not have to know that.
    """
    limits = _stub(
        monkeypatch,
        "list_period_member_group_limits",
        result=PeriodMemberGroupLimits(
            scheduling_period_id=500,
            scheduling_period_name="Q4 2026",
            ministry_id=900,
            groups=(),
        ),
    )
    support = _stub(
        monkeypatch,
        "list_support_requirements",
        result=PeriodSupportRequirements(
            scheduling_period_id=500,
            scheduling_period_name="Q4 2026",
            ministry_id=900,
            requirements=(),
        ),
    )
    return {"limits": limits, "support": support}


@pytest.fixture
def reader(monkeypatch):
    return _stub(
        monkeypatch,
        "get_event_gap_rule",
        result=PeriodEventGapRule(
            scheduling_period_id=500,
            scheduling_period_name="Q4 2026",
            ministry_id=900,
            min_intervening_events=1,
        ),
    )


@pytest.fixture
def setter(monkeypatch):
    return _stub(monkeypatch, "set_min_intervening_events", result=None)


@pytest.fixture
def api(monkeypatch) -> TestClient:
    monkeypatch.setenv(DEV_FLAG, "1")
    get_settings.cache_clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def _get(api, url, *, actor_id: int = 1, **kwargs):
    return api.get(url, headers={HEADER: str(actor_id)}, **kwargs)


def _put(api, url, *, actor_id: int = 1, **kwargs):
    return api.put(url, headers={HEADER: str(actor_id)}, **kwargs)


def _delete(api, url, *, actor_id: int = 1, **kwargs):
    return api.delete(url, headers={HEADER: str(actor_id)}, **kwargs)


# ==========================================================================
# GET
# ==========================================================================


def test_a_head_may_read_the_rule(api, session, reader):
    response = _get(api, RULES_URL.format(id=500))

    assert response.status_code == 200
    assert response.json() == {
        "scheduling_period_id": 500,
        "scheduling_period_name": "Q4 2026",
        "ministry_id": 900,
        "min_intervening_events": 1,
        # Task 74: both lists are present and empty, because this period
        # configures neither of the two rules they report.
        "member_group_limits": [],
        "same_event_support_requirements": [],
        # Task 80: whether this reader may change any of the above. The stub
        # actor heads nothing, so it is a read.
        "can_operate": False,
    }


def test_an_unconfigured_rule_reads_as_null_never_zero(api, session, reader):
    """The contract the whole feature rests on: absence of a rule is ``null``,
    and ``0`` would be a second spelling of the same fact.
    """
    reader.behaviour["result"] = PeriodEventGapRule(
        scheduling_period_id=500,
        scheduling_period_name="Q4 2026",
        ministry_id=900,
        min_intervening_events=None,
    )

    response = _get(api, RULES_URL.format(id=500))

    assert response.status_code == 200
    assert response.json()["min_intervening_events"] is None


def test_a_missing_period_is_404(api, session, reader):
    session.period = None

    assert _get(api, RULES_URL.format(id=500)).status_code == 404
    assert reader == []


def test_an_unauthorized_actor_is_403(api, session, reader):
    reader.behaviour["raises"] = AuthorizationError("not your ministry")

    assert _get(api, RULES_URL.format(id=500)).status_code == 403


def test_the_read_needs_an_actor(api, session, reader):
    assert api.get(RULES_URL.format(id=500)).status_code == 401


# ==========================================================================
# PUT
# ==========================================================================


def test_setting_a_positive_gap_reaches_the_service(api, session, setter):
    response = _put(api, GAP_URL.format(id=500), json={"min_intervening_events": 2})

    assert response.status_code == 200
    assert response.json()["min_intervening_events"] == 2
    assert setter[0]["min_intervening_events"] == 2
    assert setter[0]["scheduling_period"] is session.period


def test_the_response_echoes_the_period_it_changed(api, session, setter):
    response = _put(api, GAP_URL.format(id=500), json={"min_intervening_events": 1})

    assert response.json() == {
        "scheduling_period_id": 500,
        "scheduling_period_name": "Q4 2026",
        "ministry_id": 900,
        "min_intervening_events": 1,
        # Task 74: both lists are present and empty, because this period
        # configures neither of the two rules they report.
        "member_group_limits": [],
        "same_event_support_requirements": [],
        # Task 80: whether this reader may change any of the above. The stub
        # actor heads nothing, so it is a read.
        "can_operate": False,
    }


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_gap_is_refused_before_the_service(api, session, setter, value):
    """Pydantic's own ``ge=1`` refuses it, so the service is never reached --
    and ``0`` never becomes a stored second spelling of "no rule".
    """
    response = _put(api, GAP_URL.format(id=500), json={"min_intervening_events": value})

    assert response.status_code == 422
    assert setter == []


def test_null_is_not_accepted_in_the_put_body(api, session, setter):
    """Clearing is ``DELETE``. Keeping ``null`` out of the body is what stops
    two ways of saying the same thing reaching the service.
    """
    response = _put(
        api, GAP_URL.format(id=500), json={"min_intervening_events": None}
    )

    assert response.status_code == 422
    assert setter == []


def test_an_unknown_field_is_refused(api, session, setter):
    response = _put(
        api,
        GAP_URL.format(id=500),
        json={"min_intervening_events": 1, "min_intervening_days": 7},
    )

    assert response.status_code == 422
    assert setter == []


def test_a_missing_period_is_404_on_put(api, session, setter):
    session.period = None

    assert _put(
        api, GAP_URL.format(id=500), json={"min_intervening_events": 1}
    ).status_code == 404
    assert setter == []


def test_an_unauthorized_head_is_403_on_put(api, session, setter):
    setter.behaviour["raises"] = AuthorizationError("not your ministry")

    assert _put(
        api, GAP_URL.format(id=500), json={"min_intervening_events": 1}
    ).status_code == 403


def test_a_domain_refusal_is_409(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError("must be positive")

    assert _put(
        api, GAP_URL.format(id=500), json={"min_intervening_events": 1}
    ).status_code == 409


# ==========================================================================
# DELETE
# ==========================================================================


def test_clearing_the_rule_passes_none_to_the_service(api, session, setter):
    response = _delete(api, GAP_URL.format(id=500))

    assert response.status_code == 204
    assert setter[0]["min_intervening_events"] is None


def test_clearing_an_absent_rule_is_still_204(api, session, setter):
    """The period still exists; only the rule between them does not. A 404
    would claim the URL named nothing.
    """
    session.period.min_intervening_events = None

    assert _delete(api, GAP_URL.format(id=500)).status_code == 204


def test_a_missing_period_is_404_on_delete(api, session, setter):
    session.period = None

    assert _delete(api, GAP_URL.format(id=500)).status_code == 404
    assert setter == []


def test_an_unauthorized_head_is_403_on_delete(api, session, setter):
    setter.behaviour["raises"] = AuthorizationError("not your ministry")

    assert _delete(api, GAP_URL.format(id=500)).status_code == 403


# ==========================================================================
# The transaction boundary
# ==========================================================================


def test_a_successful_mutation_commits_once(api, session, setter):
    _put(api, GAP_URL.format(id=500), json={"min_intervening_events": 1})

    assert session.transaction_events.count("commit") == 1
    assert "rollback" not in session.transaction_events


def test_a_refused_mutation_rolls_back(api, session, setter):
    setter.behaviour["raises"] = InvalidOperationError("nope")

    _put(api, GAP_URL.format(id=500), json={"min_intervening_events": 1})

    assert "commit" not in session.transaction_events
    assert session.transaction_events.count("rollback") == 1


def test_the_read_route_never_commits_a_change(api, session, reader):
    _get(api, RULES_URL.format(id=500))

    assert "rollback" not in session.transaction_events


# ==========================================================================
# The route module stays thin
# ==========================================================================


def test_the_route_module_holds_no_domain_rule():
    """Every rule -- who may act, what a valid gap is, what clearing means --
    lives in the service. A copy here would be a second opinion that could
    disagree with the first.
    """
    import ast
    from pathlib import Path

    source = Path(routes.__file__).read_text()
    tree = ast.parse(source)
    imported = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    for schema_name in ("Base", "mapped_column", "Mapped", "relationship"):
        assert schema_name not in imported
    # The rule's own vocabulary is not restated here either.
    assert "min_intervening_events > 0" not in source


# ==========================================================================
# Task 74: the member-group cap and the same-event support requirement
# ==========================================================================

GROUP_LIMIT_URL = (
    "/api/v1/scheduling-periods/{id}/scheduling-rules/member-group-limits/{group}"
)
SUPPORT_URL = (
    "/api/v1/scheduling-periods/{id}/scheduling-rules/same-event-support/{subject}"
)
GROUPS_URL = "/api/v1/ministries/{ministry}/member-groups"
GROUP_MEMBER_URL = "/api/v1/member-groups/{group}/members/{membership}"


class _StubGroup:
    def __init__(self, group_id: int = 400) -> None:
        self.id = group_id
        self.ministry_id = 900
        self.name = "Category A"


class _StubMinistry:
    def __init__(self, ministry_id: int = 900) -> None:
        self.id = ministry_id
        self.name = "Ministry One"


class _StubMembership:
    def __init__(self, membership_id: int = 200) -> None:
        self.id = membership_id
        self.ministry_id = 900
        self.deactivated_at = None


@pytest.fixture
def rows(session):
    """Widen the recording Session so the new routes' lookups are answered."""
    group = _StubGroup()
    ministry = _StubMinistry()
    membership = _StubMembership()

    def execute(statement, *args, **kwargs):
        sql = str(statement)
        if "FROM scheduling_period" in sql:
            return _ScalarResult(session.period)
        if "FROM member_group" in sql:
            return _ScalarResult(group)
        if "FROM ministry_membership" in sql:
            return _ScalarResult(membership)
        if "FROM ministry" in sql:
            return _ScalarResult(ministry)
        if "FROM person" in sql:
            return _ScalarResult(session.person)
        raise AssertionError(f"unexpected query at the API boundary: {sql}")

    session.execute = execute
    return {"group": group, "ministry": ministry, "membership": membership}


def test_a_configured_cap_is_reported_by_the_collection_read(
    api, session, reader, other_rule_readers
):
    from app.services.member_group import (
        PeriodMemberGroupLimit,
        PeriodMemberGroupLimits,
    )

    other_rule_readers["limits"].behaviour["result"] = PeriodMemberGroupLimits(
        scheduling_period_id=500, scheduling_period_name="Q4 2026",
        ministry_id=900,
        groups=(
            PeriodMemberGroupLimit(
                member_group_id=400, name="Category A", member_count=8,
                max_per_event=2,
            ),
            # A group with no cap is shown too: that is the case the screen
            # exists to surface.
            PeriodMemberGroupLimit(
                member_group_id=401, name="Category B", member_count=3,
                max_per_event=None,
            ),
        ),
    )

    body = _get(api, RULES_URL.format(id=500)).json()

    assert body["member_group_limits"] == [
        {"member_group_id": 400, "name": "Category A", "member_count": 8,
         "max_per_event": 2},
        {"member_group_id": 401, "name": "Category B", "member_count": 3,
         "max_per_event": None},
    ]


def test_a_configured_support_requirement_is_reported_with_names(
    api, session, reader, other_rule_readers
):
    from app.services.same_event_support import (
        PeriodSupportRequirementEntry,
        PeriodSupportRequirements,
    )

    other_rule_readers["support"].behaviour["result"] = PeriodSupportRequirements(
        scheduling_period_id=500, scheduling_period_name="Q4 2026",
        ministry_id=900,
        requirements=(
            PeriodSupportRequirementEntry(
                subject_membership_id=200, subject_display_name="Volunteer A",
                min_supporters=1, supporter_membership_ids=(201, 202),
                supporter_display_names=("Volunteer B", "Volunteer C"),
            ),
        ),
    )

    body = _get(api, RULES_URL.format(id=500)).json()

    assert body["same_event_support_requirements"] == [
        {
            "subject_membership_id": 200,
            "subject_display_name": "Volunteer A",
            "min_supporters": 1,
            "supporter_membership_ids": [201, 202],
            "supporter_display_names": ["Volunteer B", "Volunteer C"],
        }
    ]


def test_the_support_contract_carries_no_reason_field(api, session, reader):
    """The rule records that a condition exists and who satisfies it. A reason
    in the transport contract would invite storing one (requirements §4.7).
    """
    spec = api.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["SetSameEventSupportRequest"]

    assert set(schema["properties"]) == {
        "min_supporters", "supporter_membership_ids"
    }


def test_putting_a_cap_reaches_the_service(api, session, reader, rows, monkeypatch):
    setter = _stub(monkeypatch, "set_member_group_event_limit", result=None)

    response = _put(
        api, GROUP_LIMIT_URL.format(id=500, group=400), json={"max_per_event": 2}
    )

    assert response.status_code == 200
    assert setter[0]["max_per_event"] == 2
    assert setter[0]["member_group"] is rows["group"]


@pytest.mark.parametrize("value", [0, -1, None])
def test_a_non_positive_cap_never_reaches_the_service(
    api, session, reader, rows, monkeypatch, value
):
    setter = _stub(monkeypatch, "set_member_group_event_limit", result=None)

    response = _put(
        api, GROUP_LIMIT_URL.format(id=500, group=400),
        json={"max_per_event": value},
    )

    assert response.status_code == 422
    assert setter == []


def test_deleting_a_cap_clears_it_and_answers_204(
    api, session, reader, rows, monkeypatch
):
    setter = _stub(monkeypatch, "set_member_group_event_limit", result=None)

    response = _delete(api, GROUP_LIMIT_URL.format(id=500, group=400))

    assert response.status_code == 204
    assert setter[0]["max_per_event"] is None


def test_putting_a_support_requirement_reaches_the_service(
    api, session, reader, rows, monkeypatch
):
    setter = _stub(monkeypatch, "set_same_event_support_requirement", result=None)

    response = _put(
        api, SUPPORT_URL.format(id=500, subject=200),
        json={"min_supporters": 1, "supporter_membership_ids": [201]},
    )

    assert response.status_code == 200
    assert setter[0]["min_supporters"] == 1
    assert len(setter[0]["supporter_memberships"]) == 1


def test_an_empty_supporter_set_never_reaches_the_service(
    api, session, reader, rows, monkeypatch
):
    setter = _stub(monkeypatch, "set_same_event_support_requirement", result=None)

    response = _put(
        api, SUPPORT_URL.format(id=500, subject=200),
        json={"min_supporters": 1, "supporter_membership_ids": []},
    )

    assert response.status_code == 422
    assert setter == []


def test_deleting_a_support_requirement_answers_204(
    api, session, reader, rows, monkeypatch
):
    clearer = _stub(
        monkeypatch, "clear_same_event_support_requirement", result=None
    )

    response = _delete(api, SUPPORT_URL.format(id=500, subject=200))

    assert response.status_code == 204
    assert len(clearer) == 1


def test_a_service_refusal_keeps_its_status_code(
    api, session, reader, rows, monkeypatch
):
    setter = _stub(monkeypatch, "set_member_group_event_limit", result=None)
    setter.behaviour["raises"] = AuthorizationError("not your ministry")

    response = _put(
        api, GROUP_LIMIT_URL.format(id=500, group=400), json={"max_per_event": 2}
    )

    assert response.status_code == 403


def test_an_invalid_operation_keeps_the_projects_own_status_code(
    api, session, reader, rows, monkeypatch
):
    """409, the code :mod:`app.api.errors` maps ``InvalidOperationError`` to
    everywhere -- asserted here rather than assumed so a route that mapped it
    differently would fail.
    """
    setter = _stub(monkeypatch, "set_same_event_support_requirement", result=None)
    setter.behaviour["raises"] = InvalidOperationError("could never be scheduled")

    response = _put(
        api, SUPPORT_URL.format(id=500, subject=200),
        json={"min_supporters": 2, "supporter_membership_ids": [201]},
    )

    assert response.status_code == 409


def test_a_missing_member_group_is_404(api, session, reader, monkeypatch):
    def execute(statement, *args, **kwargs):
        sql = str(statement)
        if "FROM scheduling_period" in sql:
            return _ScalarResult(session.period)
        if "FROM member_group" in sql:
            return _ScalarResult(None)
        if "FROM person" in sql:
            return _ScalarResult(session.person)
        raise AssertionError(sql)

    session.execute = execute
    setter = _stub(monkeypatch, "set_member_group_event_limit", result=None)

    response = _put(
        api, GROUP_LIMIT_URL.format(id=500, group=400), json={"max_per_event": 2}
    )

    assert response.status_code == 404
    assert setter == []


def test_listing_member_groups_reaches_the_service(
    api, session, reader, rows, monkeypatch
):
    from app.services.member_group import MemberGroupSummary

    lister = _stub(
        monkeypatch,
        "list_member_groups",
        result=(
            MemberGroupSummary(
                member_group_id=400, ministry_id=900, name="Category A",
                member_membership_ids=(200, 201),
            ),
        ),
    )

    response = _get(api, GROUPS_URL.format(ministry=900))

    assert response.status_code == 200
    assert response.json() == {
        "ministry_id": 900,
        "member_groups": [
            {
                "member_group_id": 400,
                "ministry_id": 900,
                "name": "Category A",
                "member_membership_ids": [200, 201],
            }
        ],
        "can_operate": False,
    }
    assert len(lister) == 1


def test_putting_and_deleting_group_membership_answer_204(
    api, session, reader, rows, monkeypatch
):
    setter = _stub(monkeypatch, "set_member_group_membership", result=None)

    added = _put(api, GROUP_MEMBER_URL.format(group=400, membership=200))
    removed = _delete(api, GROUP_MEMBER_URL.format(group=400, membership=200))

    assert added.status_code == 204
    assert removed.status_code == 204
    assert [call["is_member"] for call in setter] == [True, False]
