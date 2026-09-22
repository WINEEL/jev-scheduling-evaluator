"""REVIEW -> FINALIZED transition tests (schedule-output §6, §16; ADR 0003).

Offline: no PostgreSQL, no Neon, no network.

A separate module from ``test_services_schedule_lifecycle.py`` (Task 24's
DRAFT -> REVIEW tests) so that suite stays exactly as it was; both exercise the
same service module through their own operation's front door.

**Test strategy** follows Tasks 22-26's precedent: the three lookups
(``_resolve_scheduling_period``, ``_newer_version_exists``, and Task 26's
``get_finalization_readiness`` as imported into this module's namespace) are
monkeypatched and record the session they were handed; ``_now_utc`` is pinned
so the timestamp written to the column and echoed into the audit payload can be
asserted exactly. Session discipline is checked against a real, unbound
``Session`` subclass that raises on ``flush``/``delete``/``commit``/``rollback``.

What this cannot prove -- that PostgreSQL stores the transition, and that the
Task 21 conflict query starts reading the newly finalized version -- is proven
in ``tests/integration/test_pg_schedule_finalization.py``.
"""

from __future__ import annotations

import datetime

import pytest
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
from app.services.audit import ACTION_SCHEDULE_VERSION_FINALIZED
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.finalization_readiness import (
    FinalizationIssue,
    FinalizationReadinessResult,
)
from app.services.schedule_lifecycle import finalize_schedule_version
from app.services.schedule_staleness import ScheduleVersionStalenessResult

UTC = datetime.timezone.utc
FINALIZED_AT = datetime.datetime(2026, 10, 5, 21, 10, 44, tzinfo=UTC)

_FRESH = ScheduleVersionStalenessResult(
    current_requirements=frozenset(), snapshot_requirements=frozenset()
)
READY = FinalizationReadinessResult(staleness=_FRESH, issues=())
NOT_READY = FinalizationReadinessResult(
    staleness=_FRESH,
    issues=(
        FinalizationIssue(
            code="UNFILLED_REQUIREMENT", message="Setup Lead on 2026-11-15 needs 2 and has 1",
            schedule_version_requirement_id=600,
        ),
    ),
)


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


class LifecycleSession(Session):
    """A real, unbound Session permitting only ``add`` (the one AuditEvent a
    real transition writes). Nothing else is allowed: this operation needs no
    flush, and a service never commits or rolls back.
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
        person.deactivated_at = datetime.datetime(2026, 1, 1, tzinfo=UTC)
    return person


def _ministry(ministry_id: int, name: str) -> Ministry:
    ministry = Ministry(name=name, church_id=1)
    ministry.id = ministry_id
    return ministry


def _actor_membership(
    membership_id: int, *, person: Person, ministry: Ministry, is_head: bool = False
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry.id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    person.ministry_memberships.append(membership)
    return membership


def _period(period_id: int, *, ministry: Ministry, name: str = "Q4 2026") -> SchedulingPeriod:
    period = SchedulingPeriod(
        ministry_id=ministry.id, name=name,
        start_date=datetime.date(2026, 10, 4), end_date=datetime.date(2026, 12, 27),
    )
    period.id = period_id
    period.ministry = ministry
    return period


def _version(
    version_id: int = 500, *, schedule_id: int = 400, scheduling_period_id: int = 11,
    version_number: int = 1, status: str = SCHEDULE_VERSION_STATUS_REVIEW,
    finalized_at: datetime.datetime | None = None,
) -> ScheduleVersion:
    version = ScheduleVersion(
        schedule_id=schedule_id, scheduling_period_id=scheduling_period_id,
        version_number=version_number, status=status, finalized_at=finalized_at,
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
def review_version() -> ScheduleVersion:
    return _version()


def _stub(
    monkeypatch, *, period: SchedulingPeriod, newer_version_exists: bool = False,
    readiness: FinalizationReadinessResult = READY,
    now: datetime.datetime | None = FINALIZED_AT,
) -> list[tuple[str, object, dict]]:
    """Patch the three lookups and (unless ``now`` is None) the clock,
    recording every call so call order, argument scoping and "same session"
    claims can be asserted.
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

    def fake_readiness(session, *, version):
        calls.append(("readiness", session, {"version": version}))
        return readiness

    monkeypatch.setattr(module, "_resolve_scheduling_period", fake_resolve)
    monkeypatch.setattr(module, "_newer_version_exists", fake_newer)
    monkeypatch.setattr(module, "get_finalization_readiness", fake_readiness)
    if now is not None:
        monkeypatch.setattr(module, "_now_utc", lambda: now)
    return calls


def _audit_rows(session: Session) -> list[AuditEvent]:
    return [obj for obj in session.new if isinstance(obj, AuditEvent)]


def _one_audit_row(session: Session) -> AuditEvent:
    rows = _audit_rows(session)
    assert len(rows) == 1, f"expected exactly one audit row, got {len(rows)}"
    return rows[0]


# --------------------------------------------------------------------------
# 1-7 -- Authorization / context
# --------------------------------------------------------------------------


def test_01_admin_can_finalize(session, head, setup_period, review_version, monkeypatch):
    _stub(monkeypatch, period=setup_period)

    result = finalize_schedule_version(session, actor=head, version=review_version)

    assert result is review_version
    assert result.status == SCHEDULE_VERSION_STATUS_FINALIZED


def test_02_own_ministry_head_can_finalize(
    session, setup_ministry, setup_period, review_version, monkeypatch
):
    head = _person(2, "Head")
    _actor_membership(200, person=head, ministry=setup_ministry, is_head=True)
    _stub(monkeypatch, period=setup_period)

    result = finalize_schedule_version(session, actor=head, version=review_version)

    assert result.status == SCHEDULE_VERSION_STATUS_FINALIZED


def test_03_head_of_another_ministry_is_rejected(
    session, setup_period, review_version, monkeypatch
):
    other_head = _person(5, "AV Head")
    _actor_membership(201, person=other_head, ministry=_ministry(4, "AV"), is_head=True)
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        finalize_schedule_version(session, actor=other_head, version=review_version)
    assert review_version.status == SCHEDULE_VERSION_STATUS_REVIEW
    assert review_version.finalized_at is None
    assert _audit_rows(session) == []


def test_04_normal_member_is_rejected(
    session, setup_ministry, setup_period, review_version, monkeypatch
):
    ordinary = _person(6, "Ordinary")
    _actor_membership(202, person=ordinary, ministry=setup_ministry, is_head=False)
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        finalize_schedule_version(session, actor=ordinary, version=review_version)
    assert review_version.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_05_deactivated_admin_is_rejected(session, setup_period, review_version, monkeypatch):
    former = _person(7, "Former Admin", is_admin=True, deactivated=True)
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        finalize_schedule_version(session, actor=former, version=review_version)
    assert review_version.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_06_ministry_is_derived_from_the_versions_period(session, review_version, monkeypatch):
    """No ministry_id parameter exists; authorization is scoped to whatever
    the resolved period says, which a Head of *that* ministry satisfies.
    """
    kids = _ministry(9, "Kids")
    kids_period = _period(12, ministry=kids, name="Q4 2026")
    head = _person(8, "Kids Head")
    _actor_membership(300, person=head, ministry=kids, is_head=True)
    _stub(monkeypatch, period=kids_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert _one_audit_row(session).ministry_id == kids.id


@pytest.mark.parametrize("missing", ["id", "schedule_id", "scheduling_period_id"])
def test_07_insufficient_version_context_is_rejected(session, head, missing):
    version = _version()
    setattr(version, missing, None)

    with pytest.raises(InvalidOperationError):
        finalize_schedule_version(session, actor=head, version=version)


# --------------------------------------------------------------------------
# 8-14 -- Latest-version rule
# --------------------------------------------------------------------------


def test_08_latest_review_may_finalize(session, head, setup_period, review_version, monkeypatch):
    _stub(monkeypatch, period=setup_period, newer_version_exists=False)

    assert finalize_schedule_version(
        session, actor=head, version=review_version
    ).status == SCHEDULE_VERSION_STATUS_FINALIZED


def test_09_superseded_review_is_rejected(session, head, setup_period, review_version, monkeypatch):
    _stub(monkeypatch, period=setup_period, newer_version_exists=True)

    with pytest.raises(InvalidOperationError, match="superseded"):
        finalize_schedule_version(session, actor=head, version=review_version)
    assert review_version.status == SCHEDULE_VERSION_STATUS_REVIEW
    assert review_version.finalized_at is None
    assert _audit_rows(session) == []


@pytest.mark.parametrize(
    "newer_status",
    [SCHEDULE_VERSION_STATUS_DRAFT, SCHEDULE_VERSION_STATUS_REVIEW, SCHEDULE_VERSION_STATUS_FINALIZED],
)
def test_10_11_12_any_newer_version_supersedes_regardless_of_its_status(
    session, head, setup_period, review_version, monkeypatch, newer_status
):
    """``_newer_version_exists`` is a plain "number > current" probe, so the
    newer row's own status is never consulted. Parametrized to prove the
    rejection does not depend on it.
    """
    _stub(monkeypatch, period=setup_period, newer_version_exists=True)

    with pytest.raises(InvalidOperationError):
        finalize_schedule_version(session, actor=head, version=review_version)


def test_13_14_the_latest_check_is_scoped_to_this_schedule_and_version_number(
    session, head, setup_period, monkeypatch
):
    version = _version(schedule_id=444, version_number=3)
    calls = _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=version)

    newer = next(c for c in calls if c[0] == "newer_version_exists")
    assert newer[2] == {"schedule_id": 444, "version_number": 3}


def test_the_superseded_check_runs_before_the_readiness_gate(
    session, head, setup_period, review_version, monkeypatch
):
    """A historical version must be refused as historical, not sent through a
    readiness check whose answer could not matter.
    """
    calls = _stub(
        monkeypatch, period=setup_period, newer_version_exists=True, readiness=READY,
    )

    with pytest.raises(InvalidOperationError):
        finalize_schedule_version(session, actor=head, version=review_version)

    assert [c[0] for c in calls] == ["resolve_period", "newer_version_exists"]


# --------------------------------------------------------------------------
# 15-22 -- Status rules and idempotency
# --------------------------------------------------------------------------


def test_15_review_to_finalized_succeeds(session, head, setup_period, review_version, monkeypatch):
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert review_version.status == "FINALIZED"
    assert review_version.finalized_at == FINALIZED_AT


def test_16_draft_cannot_be_finalized(session, head, setup_period, monkeypatch):
    """A version must pass human review first -- there is no shortcut."""
    draft = _version(status=SCHEDULE_VERSION_STATUS_DRAFT)
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError, match="REVIEW"):
        finalize_schedule_version(session, actor=head, version=draft)
    assert draft.status == SCHEDULE_VERSION_STATUS_DRAFT
    assert draft.finalized_at is None
    assert _audit_rows(session) == []


def test_17_unknown_status_is_rejected(session, head, setup_period, monkeypatch):
    weird = _version(status="ARCHIVED")
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError):
        finalize_schedule_version(session, actor=head, version=weird)
    assert weird.status == "ARCHIVED"


def test_18_latest_finalized_is_idempotent(session, head, setup_period, monkeypatch):
    already = _version(status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT)
    _stub(monkeypatch, period=setup_period)

    result = finalize_schedule_version(session, actor=head, version=already)

    assert result is already
    assert result.status == SCHEDULE_VERSION_STATUS_FINALIZED


def test_19_idempotent_call_preserves_the_exact_finalized_at(
    session, head, setup_period, monkeypatch
):
    """The historical moment authority transferred must not be rewritten -- a
    later clock value would silently relocate it.
    """
    original = datetime.datetime(2026, 9, 1, 8, 30, tzinfo=UTC)
    already = _version(status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=original)
    _stub(monkeypatch, period=setup_period, now=datetime.datetime(2027, 1, 1, tzinfo=UTC))

    finalize_schedule_version(session, actor=head, version=already)

    assert already.finalized_at == original


def test_20_idempotent_call_creates_no_audit_event(session, head, setup_period, monkeypatch):
    already = _version(status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT)
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=already, reason="Again.")

    assert _audit_rows(session) == []
    assert len(session.new) == 0


def test_21_superseded_finalized_is_rejected_not_idempotent(
    session, head, setup_period, monkeypatch
):
    """An amended, superseded FINALIZED version is history. Reporting success
    for it would misstate which version is authoritative.
    """
    old = _version(status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT)
    _stub(monkeypatch, period=setup_period, newer_version_exists=True)

    with pytest.raises(InvalidOperationError, match="superseded"):
        finalize_schedule_version(session, actor=head, version=old)
    assert old.finalized_at == FINALIZED_AT


def test_22_no_reverse_transition_exists():
    import app.services.schedule_lifecycle as module

    names = [n for n in vars(module) if not n.startswith("__")]
    assert not [n for n in names if "unfinalize" in n.lower()]
    assert not [n for n in names if "reopen" in n.lower()]
    assert not [n for n in names if "revert" in n.lower()]
    assert module.__all__ == ["finalize_schedule_version", "submit_schedule_version_for_review"]


def test_idempotent_call_still_authorizes(session, setup_period, monkeypatch):
    """Authorization is not skipped just because nothing will change."""
    outsider = _person(5, "AV Head")
    _actor_membership(201, person=outsider, ministry=_ministry(4, "AV"), is_head=True)
    already = _version(status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT)
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(AuthorizationError):
        finalize_schedule_version(session, actor=outsider, version=already)


# --------------------------------------------------------------------------
# 23-29 -- The Task 26 readiness gate
# --------------------------------------------------------------------------


def test_23_a_ready_review_version_finalizes(session, head, setup_period, review_version, monkeypatch):
    _stub(monkeypatch, period=setup_period, readiness=READY)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert review_version.status == SCHEDULE_VERSION_STATUS_FINALIZED


def test_24_a_not_ready_review_version_is_rejected(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period, readiness=NOT_READY)

    with pytest.raises(InvalidOperationError, match="cannot finalize"):
        finalize_schedule_version(session, actor=head, version=review_version)


def test_25_26_rejected_readiness_leaves_status_and_timestamp_untouched(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period, readiness=NOT_READY)

    with pytest.raises(InvalidOperationError):
        finalize_schedule_version(session, actor=head, version=review_version)

    assert review_version.status == SCHEDULE_VERSION_STATUS_REVIEW
    assert review_version.finalized_at is None


def test_27_rejected_readiness_creates_no_audit_event(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period, readiness=NOT_READY)

    with pytest.raises(InvalidOperationError):
        finalize_schedule_version(session, actor=head, version=review_version)

    assert _audit_rows(session) == []
    assert len(session.new) == 0


def test_27b_a_stale_snapshot_alone_blocks_finalization(
    session, head, setup_period, review_version, monkeypatch
):
    """Task 26 folds staleness into ``is_ready``; this service trusts that
    verdict rather than re-deriving it.
    """
    stale = FinalizationReadinessResult(
        staleness=ScheduleVersionStalenessResult(
            current_requirements=frozenset({"x"}), snapshot_requirements=frozenset()
        ),
        issues=(),
    )
    _stub(monkeypatch, period=setup_period, readiness=stale)

    with pytest.raises(InvalidOperationError, match="stale"):
        finalize_schedule_version(session, actor=head, version=review_version)
    assert review_version.status == SCHEDULE_VERSION_STATUS_REVIEW


def test_28_the_readiness_gate_receives_the_supplied_session_and_version(
    session, head, setup_period, review_version, monkeypatch
):
    calls = _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    readiness_call = next(c for c in calls if c[0] == "readiness")
    assert readiness_call[1] is session
    assert readiness_call[2] == {"version": review_version}


def test_29_an_idempotent_finalized_call_does_not_rerun_readiness(
    session, head, setup_period, monkeypatch
):
    """Re-validating a published schedule would let an unrelated later change
    make an idempotent call start failing.
    """
    already = _version(status=SCHEDULE_VERSION_STATUS_FINALIZED, finalized_at=FINALIZED_AT)
    calls = _stub(monkeypatch, period=setup_period, readiness=NOT_READY)

    result = finalize_schedule_version(session, actor=head, version=already)

    assert result.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert [c[0] for c in calls] == ["resolve_period", "newer_version_exists"]


# --------------------------------------------------------------------------
# 30-32 -- Timestamp and the model invariant
# --------------------------------------------------------------------------


def test_30_the_timestamp_is_timezone_aware_utc(session, head, setup_period, review_version, monkeypatch):
    """The real clock is used here (``now=None`` leaves ``_now_utc`` alone), so
    this asserts what production actually writes, not what a fixture supplied.
    """
    before = datetime.datetime.now(tz=UTC)
    _stub(monkeypatch, period=setup_period, now=None)

    finalize_schedule_version(session, actor=head, version=review_version)

    stamped = review_version.finalized_at
    assert stamped.tzinfo is not None
    assert stamped.utcoffset() == datetime.timedelta(0)
    assert before <= stamped <= datetime.datetime.now(tz=UTC)


def test_30b_the_real_clock_helper_returns_aware_utc():
    import app.services.schedule_lifecycle as module

    now = module._now_utc()

    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.timedelta(0)


def test_31_status_and_finalized_at_move_together(
    session, head, setup_period, review_version, monkeypatch
):
    """The model's ``status_finalized_at_agree`` CHECK is two-way, so neither
    field may be set without the other.
    """
    _stub(monkeypatch, period=setup_period)
    assert review_version.finalized_at is None

    finalize_schedule_version(session, actor=head, version=review_version)

    assert review_version.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert review_version.finalized_at is not None


def test_32_no_other_version_business_fields_change(
    session, head, setup_period, monkeypatch
):
    version = _version(version_number=3)
    version.notes = "Third attempt."
    version.amendment_reason = None
    before = {
        "schedule_id": version.schedule_id,
        "scheduling_period_id": version.scheduling_period_id,
        "version_number": version.version_number,
        "amends_version_id": version.amends_version_id,
        "amendment_reason": version.amendment_reason,
        "notes": version.notes,
    }
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=version)

    after = {key: getattr(version, key) for key in before}
    assert after == before


# --------------------------------------------------------------------------
# 33-40 -- Audit
# --------------------------------------------------------------------------


def test_33_exactly_one_finalization_audit_event(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert len(_audit_rows(session)) == 1
    assert _one_audit_row(session).action == ACTION_SCHEDULE_VERSION_FINALIZED
    assert _one_audit_row(session).action == "SCHEDULE_VERSION_FINALIZED"


def test_34_audit_target_and_ministry_are_correct(
    session, head, setup_ministry, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    audit = _one_audit_row(session)
    assert audit.target_table == "schedule_version"
    assert audit.target_id == review_version.id == 500
    assert audit.ministry_id == setup_ministry.id == 3
    assert audit.actor_type == "PERSON"
    assert audit.actor_person_id == head.id
    assert audit.actor_label == head.display_name


def test_35_before_values_record_review_and_a_null_timestamp(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert _one_audit_row(session).before_values == {
        "status": "REVIEW",
        "finalized_at": None,
    }


def test_36_after_values_record_finalized_and_the_exact_iso_timestamp(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period, now=FINALIZED_AT)

    finalize_schedule_version(session, actor=head, version=review_version)

    audit = _one_audit_row(session)
    assert audit.after_values == {
        "status": "FINALIZED",
        "finalized_at": FINALIZED_AT.isoformat(),
    }
    # Text, not a datetime: the payload is JSONB and must be serializable.
    assert isinstance(audit.after_values["finalized_at"], str)
    # And it is the very value written to the column.
    assert audit.after_values["finalized_at"] == review_version.finalized_at.isoformat()


def test_37_summary_uses_version_number_ministry_name_and_period_name(
    session, head, setup_ministry, setup_period, monkeypatch
):
    """setup_ministry.name == "Setup", setup_period.name == "Q4 2026" -- the
    period name deliberately does not embed the ministry name (Task 24's
    correction), so both must appear independently.
    """
    version = _version(version_number=4)
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=version)

    summary = _one_audit_row(session).summary
    assert "4" in summary
    assert setup_ministry.name in summary
    assert setup_period.name in summary
    assert summary == "Finalized version 4 for Setup Q4 2026"


def test_38_optional_reason_is_copied_to_the_audit_reason(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(
        session, actor=head, version=review_version, reason="Approved at the elders' meeting.",
    )

    assert _one_audit_row(session).reason == "Approved at the elders' meeting."


def test_38b_no_reason_leaves_the_audit_reason_none(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert _one_audit_row(session).reason is None


def test_39_a_whitespace_only_reason_is_rejected_before_any_mutation(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    with pytest.raises(InvalidOperationError):
        finalize_schedule_version(session, actor=head, version=review_version, reason="   ")

    assert review_version.status == SCHEDULE_VERSION_STATUS_REVIEW
    assert review_version.finalized_at is None
    assert _audit_rows(session) == []


def test_40_an_idempotent_call_writes_no_duplicate_history(
    session, head, setup_period, review_version, monkeypatch
):
    """One real transition, then a second call: still exactly one audit row."""
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)
    finalize_schedule_version(session, actor=head, version=review_version)

    assert len(_audit_rows(session)) == 1


# --------------------------------------------------------------------------
# 41-46 -- Transaction and session behavior
# --------------------------------------------------------------------------


def test_41_only_the_audit_event_is_added(session, head, setup_period, review_version, monkeypatch):
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert len(session.new) == 1
    assert isinstance(next(iter(session.new)), AuditEvent)
    assert session.delete_calls == 0


def test_42_43_44_never_flushes_commits_or_rolls_back(
    session, head, setup_period, review_version, monkeypatch
):
    _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert session.flush_calls == 0
    assert session.commit_calls == 0
    assert session.rollback_calls == 0


def test_45_every_lookup_uses_the_one_supplied_session(
    session, head, setup_period, review_version, monkeypatch
):
    calls = _stub(monkeypatch, period=setup_period)

    finalize_schedule_version(session, actor=head, version=review_version)

    assert [c[0] for c in calls] == ["resolve_period", "newer_version_exists", "readiness"]
    assert all(call[1] is session for call in calls)


def test_46_an_audit_failure_propagates_without_manual_restoration(
    session, head, setup_period, review_version, monkeypatch
):
    """The mutation is deliberately left in place: discarding it is the
    caller-owned transaction's job, and a hand-rolled restore here would be a
    second, untested code path doing what ROLLBACK already does correctly.
    """
    import app.services.schedule_lifecycle as module

    _stub(monkeypatch, period=setup_period)

    def boom(*args, **kwargs):
        raise RuntimeError("audit write exploded")

    monkeypatch.setattr(module, "record_audit_event", boom)

    with pytest.raises(RuntimeError, match="audit write exploded"):
        finalize_schedule_version(session, actor=head, version=review_version)

    assert review_version.status == SCHEDULE_VERSION_STATUS_FINALIZED
    assert review_version.finalized_at == FINALIZED_AT
    assert session.rollback_calls == 0


# --------------------------------------------------------------------------
# Authoritative semantics: nothing is stored, nothing else is touched
# --------------------------------------------------------------------------


def test_no_authoritative_pointer_column_is_written():
    """ADR 0003 derives the authoritative version from status and
    version_number; a stored pointer could be left stale by a failed
    transaction and is deliberately absent.
    """
    columns = set(ScheduleVersion.__table__.c.keys())

    assert "current_version_id" not in columns
    assert "authoritative_version_id" not in columns
    assert "is_authoritative" not in columns


def test_the_service_never_touches_assignments_or_commitments():
    """Finalization changes two columns on one row. Assignments are not
    rewritten and never mirrored into existing_commitment (ADR 0002).
    """
    import app.services.schedule_lifecycle as module

    # Checked by what the module can reach, not by grepping prose: the
    # docstring legitimately explains that these are *not* written.
    assert not hasattr(module, "ExistingCommitment")
    assert not hasattr(module, "Assignment")
    assert not hasattr(module, "ScheduleVersionRequirement")
    assert not [name for name in vars(module) if "assignment" in name.lower()]
    assert not [name for name in vars(module) if "commitment" in name.lower()]
