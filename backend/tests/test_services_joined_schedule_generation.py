"""Joined multi-ministry draft generation (Task 81).

Offline: no PostgreSQL, no Neon, no network.

This service is **orchestration and nothing else** -- it gates, it builds, it
hands the whole thing to the joined engine, it writes. So the tests are about
composition rather than scheduling: which gates run, in what order, what the
collaborators were handed, what happens when one of them refuses, and -- first
of all -- that nothing in the product can reach it.

The joined engine's own behaviour, including the church-wide rule itself, is
``tests/test_scheduling_joined.py``. The batch writer's behaviour is
``tests/test_services_generated_assignment.py``. Neither is re-asserted here.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from types import MappingProxyType

import pytest
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryMembership, Person
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_REVIEW,
    ScheduleVersion,
)
from app.models.scheduling_input import SchedulingPeriod
from app.scheduling.input import SchedulingInput
from app.scheduling.joined import JoinedSchedulingResult
from app.scheduling.result import ProposedAssignment, SchedulingResult
from app.scheduling.solver import SchedulingPolicy
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.joined_schedule_generation import (
    JoinedMinistryRequest,
    generate_joined_draft_schedules,
)

SETUP = 3
AV = 4
KIDS = 5
MINISTRY_IDS = (SETUP, AV, KIDS)


class JoinedSession(Session):
    """A real, unbound Session that refuses everything a service must not do."""

    def __init__(self) -> None:
        super().__init__(autoflush=False)
        self.commit_calls = 0
        self.rollback_calls = 0
        self.delete_calls = 0

    def commit(self) -> None:  # pragma: no cover - must never run
        self.commit_calls += 1
        raise AssertionError("a service must never commit")

    def rollback(self) -> None:  # pragma: no cover - must never run
        self.rollback_calls += 1
        raise AssertionError("a service must never roll back")

    def delete(self, instance) -> None:  # pragma: no cover - must never run
        self.delete_calls += 1
        raise AssertionError("joined generation must never delete")


@pytest.fixture
def session() -> JoinedSession:
    return JoinedSession()


def _ministry(ministry_id: int) -> Ministry:
    ministry = Ministry(name=f"Ministry {ministry_id}", church_id=1)
    ministry.id = ministry_id
    return ministry


def _period(ministry_id: int) -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry_id,
        name="Q1 2030",
        start_date=datetime.date(2030, 1, 6),
        end_date=datetime.date(2030, 3, 31),
    )
    period.id = 100 + ministry_id
    return period


def _version(
    ministry_id: int, *, status: str = SCHEDULE_VERSION_STATUS_DRAFT
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=400 + ministry_id,
        scheduling_period_id=100 + ministry_id,
        version_number=1,
        status=status,
    )
    version.id = 500 + ministry_id
    return version


def _head(*ministry_ids: int) -> Person:
    person = Person(display_name="A Head", is_admin=False, church_id=1)
    person.id = 1
    person.ministry_memberships = []
    for index, ministry_id in enumerate(ministry_ids):
        membership = MinistryMembership(
            person_id=person.id, ministry_id=ministry_id, is_ministry_head=True
        )
        membership.id = 9000 + index
        person.ministry_memberships.append(membership)
    return person


def _input(ministry_id: int) -> SchedulingInput:
    return SchedulingInput(
        schedule_version_id=500 + ministry_id,
        scheduling_period_id=100 + ministry_id,
        ministry_id=ministry_id,
    )


def _requests(*ministry_ids: int, status: str = SCHEDULE_VERSION_STATUS_DRAFT):
    return tuple(
        JoinedMinistryRequest(
            version=_version(ministry_id, status=status),
            policy=SchedulingPolicy(
                allow_no_response=False,
                target_assignments_per_candidate=ministry_id,
            ),
        )
        for ministry_id in ministry_ids
    )


def _joined_result(*ministry_ids: int) -> JoinedSchedulingResult:
    return JoinedSchedulingResult(
        results_by_ministry=MappingProxyType(
            {
                ministry_id: SchedulingResult(
                    proposed_assignments=(
                        ProposedAssignment(
                            requirement_id=ministry_id * 10,
                            membership_id=ministry_id * 100,
                            event_id=ministry_id,
                        ),
                    )
                )
                for ministry_id in ministry_ids
            }
        )
    )


def _stub(
    monkeypatch,
    *,
    joined_result: JoinedSchedulingResult | None = None,
    newer_version_exists: bool = False,
    builder_error: Exception | None = None,
    solver_error: Exception | None = None,
    write_error_on: int | None = None,
) -> dict:
    """Replace the four collaborators with recorders.

    Every gate, build, solve and write appends to one ``calls`` list, so the
    *order* the service does things in -- which is most of what it promises --
    is a plain assertion rather than an inference.
    """
    import app.services.joined_schedule_generation as module

    seen: dict[str, list] = {"calls": [], "built": [], "solved": [], "written": []}

    def fake_period(session, version):
        ministry_id = version.scheduling_period_id - 100
        seen["calls"].append(("period", ministry_id))
        return _period(ministry_id)

    def fake_latest_draft(session, version):
        seen["calls"].append(("latest-draft", version.scheduling_period_id - 100))
        if version.status != SCHEDULE_VERSION_STATUS_DRAFT:
            raise InvalidOperationError(
                "a schedule can only be generated into a DRAFT version, and"
                f" this one is {version.status}"
            )
        if newer_version_exists:
            raise InvalidOperationError(
                "cannot generate into a schedule version that has been"
                " superseded by a newer version"
            )

    def fake_builder(session, *, version):
        ministry_id = version.scheduling_period_id - 100
        seen["calls"].append(("build", ministry_id))
        seen["built"].append((session, version))
        if builder_error is not None:
            raise builder_error
        return _input(ministry_id)

    def fake_solver(ministries):
        entries = tuple(ministries)
        seen["calls"].append(("solve", tuple(e.ministry_id for e in entries)))
        seen["solved"].append(entries)
        if solver_error is not None:
            raise solver_error
        return joined_result or _joined_result(
            *(entry.ministry_id for entry in entries)
        )

    def fake_persist(session, *, actor, version, ministry_id, proposals):
        seen["calls"].append(("write", ministry_id))
        seen["written"].append((session, actor, version, ministry_id, tuple(proposals)))
        if write_error_on == ministry_id:
            raise InvalidOperationError("the writer refused this placement")
        return ()

    monkeypatch.setattr(module, "_resolve_scheduling_period", fake_period)
    monkeypatch.setattr(module, "_require_latest_draft", fake_latest_draft)
    monkeypatch.setattr(module, "build_scheduling_input", fake_builder)
    monkeypatch.setattr(module, "solve_joined_schedule", fake_solver)
    monkeypatch.setattr(module, "persist_generated_assignments", fake_persist)
    return seen


# --------------------------------------------------------------------------
# It is development-only, and that is checkable
# --------------------------------------------------------------------------


def test_no_api_module_reaches_joined_generation():
    """The claim in the module docstring, as a test.

    Task 81 §11: experimental joined scheduling is not exposed to production
    users. The way that stays true as the API grows is for this assertion to
    fail the moment a route imports it.
    """
    api_dir = Path(__file__).resolve().parents[1] / "app" / "api"
    # The two module names, not the word: ``joined_at`` is an ordinary column
    # on a membership schema and has nothing to do with this.
    forbidden = (
        "app.scheduling.joined",
        "app.services.joined_schedule_generation",
    )
    offenders = [
        path.name
        for path in sorted(api_dir.glob("*.py"))
        if any(name in path.read_text() for name in forbidden)
    ]
    assert offenders == []


def test_the_joined_engine_is_not_reachable_from_the_scheduling_packages_surface():
    """``app.scheduling`` exports the production engine, and only that. A
    joined run has to be imported by its own name, deliberately.
    """
    import app.scheduling as package

    assert "solve_joined_schedule" not in package.__all__
    assert not hasattr(package, "solve_joined_schedule")


# --------------------------------------------------------------------------
# Gates
# --------------------------------------------------------------------------


def test_every_ministry_is_gated_before_any_solving_work(session, monkeypatch):
    seen = _stub(monkeypatch)
    generate_joined_draft_schedules(
        session, actor=_head(*MINISTRY_IDS), requests=_requests(*MINISTRY_IDS)
    )

    kinds = [kind for kind, _ in seen["calls"]]
    assert kinds.index("build") > max(
        index for index, kind in enumerate(kinds) if kind == "latest-draft"
    )
    assert kinds.count("period") == 3
    assert kinds.count("latest-draft") == 3


def test_a_head_of_only_some_of_the_ministries_is_rejected(session, monkeypatch):
    """Authorization is per ministry, and one refusal refuses the run -- before
    a single input is built, let alone written.
    """
    seen = _stub(monkeypatch)
    with pytest.raises(AuthorizationError):
        generate_joined_draft_schedules(
            session, actor=_head(SETUP, AV), requests=_requests(*MINISTRY_IDS)
        )
    assert seen["built"] == []
    assert seen["written"] == []


def test_a_version_that_is_not_a_latest_draft_refuses_the_whole_run(
    session, monkeypatch
):
    seen = _stub(monkeypatch)
    requests = (
        *_requests(SETUP, AV),
        *_requests(KIDS, status=SCHEDULE_VERSION_STATUS_REVIEW),
    )
    with pytest.raises(InvalidOperationError, match="only be generated into a DRAFT"):
        generate_joined_draft_schedules(
            session, actor=_head(*MINISTRY_IDS), requests=requests
        )
    assert seen["written"] == []


def test_a_superseded_draft_refuses_the_whole_run(session, monkeypatch):
    seen = _stub(monkeypatch, newer_version_exists=True)
    with pytest.raises(InvalidOperationError, match="superseded"):
        generate_joined_draft_schedules(
            session, actor=_head(*MINISTRY_IDS), requests=_requests(*MINISTRY_IDS)
        )
    assert seen["written"] == []


def test_an_empty_run_is_rejected(session, monkeypatch):
    _stub(monkeypatch)
    with pytest.raises(InvalidOperationError, match="at least one ministry"):
        generate_joined_draft_schedules(session, actor=_head(), requests=())


def test_the_same_version_may_not_be_named_twice(session, monkeypatch):
    seen = _stub(monkeypatch)
    requests = _requests(SETUP)
    with pytest.raises(InvalidOperationError, match="each schedule version once"):
        generate_joined_draft_schedules(
            session, actor=_head(SETUP), requests=(*requests, *requests)
        )
    assert seen["calls"] == []


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def test_every_version_is_built_and_handed_to_one_joined_solve(session, monkeypatch):
    """One solve, not N. That is the whole difference from running three
    single-ministry generations back to back.
    """
    seen = _stub(monkeypatch)
    generate_joined_draft_schedules(
        session, actor=_head(*MINISTRY_IDS), requests=_requests(*MINISTRY_IDS)
    )

    assert [kind for kind, _ in seen["calls"]].count("solve") == 1
    assert len(seen["built"]) == 3
    assert {entry.ministry_id for entry in seen["solved"][0]} == set(MINISTRY_IDS)


def test_each_ministrys_own_policy_reaches_the_solve_unchanged(session, monkeypatch):
    """No policy is merged, defaulted or invented for a joined run: each
    ministry's preferences arrive exactly as the caller supplied them.
    """
    seen = _stub(monkeypatch)
    requests = _requests(*MINISTRY_IDS)
    generate_joined_draft_schedules(
        session, actor=_head(*MINISTRY_IDS), requests=requests
    )

    supplied = {request.version.scheduling_period_id - 100: request.policy for request in requests}
    for entry in seen["solved"][0]:
        assert entry.policy is supplied[entry.ministry_id]


def test_the_builder_receives_the_supplied_session_and_versions(session, monkeypatch):
    seen = _stub(monkeypatch)
    requests = _requests(*MINISTRY_IDS)
    generate_joined_draft_schedules(
        session, actor=_head(*MINISTRY_IDS), requests=requests
    )

    assert [built_session for built_session, _ in seen["built"]] == [session] * 3
    assert {version for _, version in seen["built"]} == {
        request.version for request in requests
    }


def test_proposals_are_written_per_ministry_in_ascending_ministry_id(
    session, monkeypatch
):
    seen = _stub(monkeypatch)
    actor = _head(*MINISTRY_IDS)
    generate_joined_draft_schedules(
        session, actor=actor, requests=_requests(KIDS, SETUP, AV)
    )

    written = [ministry_id for _, _, _, ministry_id, _ in seen["written"]]
    assert written == sorted(MINISTRY_IDS)
    for write_session, write_actor, version, ministry_id, proposals in seen["written"]:
        assert write_session is session
        assert write_actor is actor
        # Each ministry's rows go to that ministry's own version, never to
        # another's -- the one thing a joined run could plausibly get wrong.
        assert version.scheduling_period_id - 100 == ministry_id
        assert [p.membership_id for p in proposals] == [ministry_id * 100]


def test_the_result_reports_both_halves(session, monkeypatch):
    _stub(monkeypatch)
    result = generate_joined_draft_schedules(
        session, actor=_head(*MINISTRY_IDS), requests=_requests(*MINISTRY_IDS)
    )

    assert result.joined_result.ministry_ids == tuple(sorted(MINISTRY_IDS))
    assert sorted(result.created_by_ministry) == sorted(MINISTRY_IDS)
    assert result.created_count == 0  # the stub writer returns no rows


# --------------------------------------------------------------------------
# Failure is not caught, compensated or committed around
# --------------------------------------------------------------------------


def test_a_builder_refusal_propagates_and_nothing_is_written(session, monkeypatch):
    seen = _stub(
        monkeypatch, builder_error=InvalidOperationError("snapshot is stale")
    )
    with pytest.raises(InvalidOperationError, match="stale"):
        generate_joined_draft_schedules(
            session, actor=_head(*MINISTRY_IDS), requests=_requests(*MINISTRY_IDS)
        )
    assert seen["written"] == []


def test_a_solver_error_propagates_and_nothing_is_written(session, monkeypatch):
    seen = _stub(monkeypatch, solver_error=RuntimeError("CP-SAT gave up"))
    with pytest.raises(RuntimeError, match="CP-SAT"):
        generate_joined_draft_schedules(
            session, actor=_head(*MINISTRY_IDS), requests=_requests(*MINISTRY_IDS)
        )
    assert seen["written"] == []


def test_a_refusal_on_a_later_ministry_is_not_caught(session, monkeypatch):
    """The honest half of "all of it, or none of it": this function does not
    unwind the ministries it already wrote, it lets the exception reach the
    caller so the caller's transaction discards them. Nothing is caught,
    nothing is deleted, nothing is committed part-way.
    """
    seen = _stub(monkeypatch, write_error_on=KIDS)
    with pytest.raises(InvalidOperationError, match="refused this placement"):
        generate_joined_draft_schedules(
            session, actor=_head(*MINISTRY_IDS), requests=_requests(*MINISTRY_IDS)
        )

    written = [ministry_id for _, _, _, ministry_id, _ in seen["written"]]
    assert written == [SETUP, AV, KIDS]
    assert session.commit_calls == 0
    assert session.rollback_calls == 0
    assert session.delete_calls == 0


def test_the_versions_are_still_drafts_afterwards(session, monkeypatch):
    """Task 80 is untouched: a joined run creates assignments, never a
    lifecycle transition. Promoting any of these versions stays a separate,
    deliberate act.
    """
    _stub(monkeypatch)
    requests = _requests(*MINISTRY_IDS)
    generate_joined_draft_schedules(
        session, actor=_head(*MINISTRY_IDS), requests=requests
    )

    for request in requests:
        assert request.version.status == SCHEDULE_VERSION_STATUS_DRAFT
        assert request.version.finalized_at is None
