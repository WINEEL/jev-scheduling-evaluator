"""The ministry event-gap rule service (Task 71).

Offline: no PostgreSQL, no network. Every statement is compiled and inspected;
every orchestration test stubs the two execution helpers, which is the same
split every other service module in this project uses.

What is pinned here:

- **the pure sequence arithmetic** -- windows, the conflict window around one
  position, and the pair reporting readiness uses -- which is where the rule's
  actual meaning lives;
- **the loaders**, and in particular that they load *only enough* history and
  cost a fixed number of queries;
- **the setter**: authorization, validation, the no-op cases, and the three
  audit actions;
- **the SQL**, so the ADR 0003 authoritative reading and the
  ``(event_date, event_id)`` ordering cannot quietly change.

Every person, ministry and date here is synthetic.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.models.audit import AuditEvent
from app.services import event_gap
from app.services.audit import (
    ACTION_EVENT_GAP_RULE_CHANGED,
    ACTION_EVENT_GAP_RULE_CLEARED,
    ACTION_EVENT_GAP_RULE_RECORDED,
)
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.event_gap import (
    AdjacentMinistryEvent,
    MinistryEventSequence,
    describe_event_gap,
    gap_windows,
    get_event_gap_rule,
    load_adjacent_ministry_events,
    load_event_gap_sequence,
    positions_within_gap,
    set_min_intervening_events,
)

MINISTRY_ID = 3
PERIOD_ID = 11
VERSION_ID = 500

SEP_27 = datetime.date(2026, 9, 27)
OCT_4 = datetime.date(2026, 10, 4)
OCT_11 = datetime.date(2026, 10, 11)
OCT_18 = datetime.date(2026, 10, 18)
OCT_25 = datetime.date(2026, 10, 25)
SEP_20 = datetime.date(2026, 9, 20)
NOV_1 = datetime.date(2026, 11, 1)


def _compile(stmt) -> str:
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _person(person_id: int = 1, *, is_admin: bool = False, head_of=None,
            deactivated: bool = False):
    memberships = []
    for ministry_id in head_of or ():
        memberships.append(
            SimpleNamespace(
                ministry_id=ministry_id, is_ministry_head=True, deactivated_at=None
            )
        )
    return SimpleNamespace(
        id=person_id,
        display_name=f"Synthetic Person {person_id}",
        is_admin=is_admin,
        deactivated_at=(
            datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
            if deactivated else None
        ),
        ministry_memberships=memberships,
    )


class _Period:
    """A persisted ``SchedulingPeriod``, as a plain object.

    The setter mutates one attribute and reads three; it never queries, so a
    namespace is a faithful stand-in and keeps these tests free of a database.
    """

    def __init__(self, *, min_intervening_events=None, period_id=PERIOD_ID):
        self.id = period_id
        self.ministry_id = MINISTRY_ID
        self.name = "Q4 2026"
        self.min_intervening_events = min_intervening_events
        self.ministry = SimpleNamespace(name="Synthetic Ministry")


class _Session:
    """Records added rows; refuses anything that would end a transaction."""

    def __init__(self):
        self.added: list[object] = []

    def add(self, obj):
        self.added.append(obj)

    def flush(self, objects=None):  # pragma: no cover - must never run
        raise AssertionError("the setter must not flush: it mutates an existing row")

    def commit(self):  # pragma: no cover - must never run
        raise AssertionError("a service must never commit")

    def rollback(self):  # pragma: no cover - must never run
        raise AssertionError("a service must never roll back")

    def delete(self, obj):  # pragma: no cover - must never run
        raise AssertionError("the setter must never delete")

    @property
    def audits(self) -> list[AuditEvent]:
        return [row for row in self.added if isinstance(row, AuditEvent)]


@pytest.fixture
def session() -> _Session:
    return _Session()


@pytest.fixture
def admin():
    return _person(1, is_admin=True)


@pytest.fixture
def head():
    return _person(2, head_of=(MINISTRY_ID,))


# ==========================================================================
# Pure sequence arithmetic
# ==========================================================================


@pytest.mark.parametrize(
    "length,gap,expected",
    [
        (0, 1, []),
        (1, 1, []),
        (2, 1, [range(0, 2)]),
        (3, 1, [range(0, 2), range(1, 3)]),
        (3, 2, [range(0, 3)]),
        (4, 2, [range(0, 3), range(1, 4)]),
        (2, 5, [range(0, 2)]),
    ],
)
def test_gap_windows(length, gap, expected):
    """A window is ``gap + 1`` consecutive positions, and a sequence shorter
    than one window still gets a single window covering all of it -- rather
    than no rule at all.
    """
    assert gap_windows(length, gap) == expected


def test_every_window_covers_every_adjacent_pair():
    """The property the constraint depends on: two positions closer together
    than the gap always share a window, so window-sum <= 1 really is the rule.
    """
    length, gap = 9, 2
    windows = gap_windows(length, gap)
    for earlier in range(length):
        for later in range(earlier + 1, min(earlier + gap + 1, length)):
            assert any(
                earlier in window and later in window for window in windows
            ), (earlier, later)


@pytest.mark.parametrize(
    "position,length,gap,expected",
    [
        (0, 5, 1, range(0, 2)),
        (2, 5, 1, range(1, 4)),
        (4, 5, 1, range(3, 5)),
        (2, 5, 2, range(0, 5)),
        (0, 3, 9, range(0, 3)),
    ],
)
def test_positions_within_gap_is_symmetric_and_clamped(position, length, gap, expected):
    assert positions_within_gap(
        position, length=length, min_intervening_events=gap
    ) == expected


def test_describe_event_gap_reads_naturally_for_one_and_stays_accurate_above_it():
    assert "consecutive events" in describe_event_gap(1)
    assert "at least 2" in describe_event_gap(2)
    assert "at least 5" in describe_event_gap(5)


# ==========================================================================
# MinistryEventSequence
# ==========================================================================


def _sequence(gap: int = 1, *, historic=None) -> MinistryEventSequence:
    """Four events: one from before the period, then three scheduled ones."""
    return MinistryEventSequence(
        min_intervening_events=gap,
        events=((699, SEP_27), (701, OCT_4), (702, OCT_11), (703, OCT_18)),
        adjacent_events_by_membership=historic or {},
    )


def test_a_clear_placement_reports_no_conflict():
    assert _sequence().conflicting_event_date(
        target_event_id=703, occupied_event_ids={701}
    ) is None


def test_the_event_before_the_target_conflicts():
    assert _sequence().conflicting_event_date(
        target_event_id=702, occupied_event_ids={701}
    ) == OCT_4


def test_the_event_after_the_target_conflicts_too():
    """The rule is symmetric: assigning *before* something the person already
    serves breaks it exactly as squarely as assigning after.
    """
    assert _sequence().conflicting_event_date(
        target_event_id=702, occupied_event_ids={703}
    ) == OCT_18


def test_the_target_event_is_never_its_own_conflict():
    """Somebody already assigned to this exact position is an idempotency case
    the caller settles, not a gap violation.
    """
    assert _sequence().conflicting_event_date(
        target_event_id=702, occupied_event_ids={702}
    ) is None


def test_history_before_the_period_conflicts_with_its_first_event():
    assert _sequence().conflicting_event_date(
        target_event_id=701, occupied_event_ids={699}
    ) == SEP_27


def test_an_event_outside_the_sequence_yields_no_conflict():
    """The rule can only speak about the sequence it was built for; inventing a
    position for an unknown event would be inventing a fact.
    """
    assert _sequence().conflicting_event_date(
        target_event_id=9999, occupied_event_ids={701, 702}
    ) is None


def test_the_nearest_conflict_is_the_one_reported():
    assert _sequence(gap=2).conflicting_event_date(
        target_event_id=703, occupied_event_ids={699, 702}
    ) == OCT_11


def test_a_larger_gap_reaches_further():
    assert _sequence(gap=1).conflicting_event_date(
        target_event_id=703, occupied_event_ids={701}
    ) is None
    assert _sequence(gap=2).conflicting_event_date(
        target_event_id=703, occupied_event_ids={701}
    ) == OCT_4


def test_occupied_events_unions_the_version_with_the_history():
    sequence = _sequence(historic={118: frozenset({699})})
    assert sequence.occupied_events_for(118, version_event_ids={702}) == frozenset(
        {699, 702}
    )
    # Somebody with no history gets exactly what the caller supplied.
    assert sequence.occupied_events_for(119, version_event_ids={702}) == frozenset(
        {702}
    )


def test_conflicting_pairs_reports_each_clash_once_earlier_date_first():
    pairs = _sequence().conflicting_event_dates({701, 702, 703})
    assert pairs == ((OCT_4, OCT_11), (OCT_11, OCT_18))


def test_conflicting_pairs_is_empty_when_the_gap_is_respected():
    assert _sequence().conflicting_event_dates({701, 703}) == ()


def test_conflicting_pairs_sees_across_the_period_boundary():
    assert _sequence().conflicting_event_dates({699, 701}) == ((SEP_27, OCT_4),)


# ==========================================================================
# Loading the surrounding events -- only enough, on both sides, bounded queries
# ==========================================================================


class _RecordingSession:
    """Answers each statement from a queue, recording what was asked."""

    def __init__(self, answers):
        self.statements: list[object] = []
        self._answers = list(answers)

    def execute(self, statement):
        self.statements.append(statement)
        answer = self._answers.pop(0) if self._answers else []
        return SimpleNamespace(
            all=lambda: answer,
            scalars=lambda: iter(answer),
            scalar_one_or_none=lambda: answer,
        )


def _load_adjacent(session, *, gap=1):
    """The loader with the boundaries these tests use throughout: the window
    runs from event 701 on 4 October to event 703 on 18 October."""
    return load_adjacent_ministry_events(
        session,
        ministry_id=MINISTRY_ID,
        min_intervening_events=gap,
        first_event_date=OCT_4,
        first_event_id=701,
        last_event_date=OCT_18,
        last_event_id=703,
    )


def test_the_adjacent_load_costs_three_queries_whatever_the_gap():
    """One for the earlier events, one for the later ones, and **one** for the
    authoritative assignments at all of them -- never one per event, never one
    per side, and never one per candidate.
    """
    for gap in (1, 2, 5):
        session = _RecordingSession(
            [
                [SimpleNamespace(event_id=699, event_date=SEP_27)],
                [SimpleNamespace(event_id=704, event_date=OCT_25)],
                [SimpleNamespace(event_id=699, membership_id=118)],
            ]
        )
        _load_adjacent(session, gap=gap)
        assert len(session.statements) == 3, gap


def test_one_assignment_read_covers_both_sides():
    """Splitting it would double the round trips to answer one question about
    one set of events. The single read is asked about every loaded event.
    """
    session = _RecordingSession(
        [
            [SimpleNamespace(event_id=699, event_date=SEP_27)],
            [SimpleNamespace(event_id=704, event_date=OCT_25)],
            [
                SimpleNamespace(event_id=699, membership_id=118),
                SimpleNamespace(event_id=704, membership_id=119),
            ],
        ]
    )
    preceding, following = _load_adjacent(session)

    assignments_sql = _compile(session.statements[2])
    assert "assignment.event_id IN (699, 704)" in assignments_sql
    assert preceding[0].assigned_membership_ids == frozenset({118})
    assert following[0].assigned_membership_ids == frozenset({119})


def test_each_side_asks_for_exactly_the_gap_and_no_more():
    """"Inspect only the minimum number of events needed" is a ``LIMIT``, and
    this is where it is pinned on both sides: a gap of three asks the database
    for three events before and three after.
    """
    session = _RecordingSession([[], []])
    _load_adjacent(session, gap=3)

    assert "LIMIT 3" in _compile(session.statements[0])
    assert "LIMIT 3" in _compile(session.statements[1])


def test_no_surrounding_events_at_all_means_no_assignment_query():
    """Nothing to ask about, so nothing is asked -- the common case at a
    ministry's very first period, when it has no neighbours either side.
    """
    session = _RecordingSession([[], []])
    assert _load_adjacent(session) == ((), ())
    assert len(session.statements) == 2


def test_the_preceding_side_comes_back_oldest_first():
    """Its query orders newest-first so ``LIMIT`` takes the nearest events; the
    sequence runs the other way, and the reversal happens once, in the loader.
    """
    session = _RecordingSession(
        [
            [
                SimpleNamespace(event_id=699, event_date=SEP_27),
                SimpleNamespace(event_id=698, event_date=SEP_20),
            ],
            [],
            [],
        ]
    )
    preceding, _ = _load_adjacent(session, gap=2)

    assert [event.event_id for event in preceding] == [698, 699]


def test_the_following_side_comes_back_earliest_first():
    """Its query already returns the nearest first *and* in sequence order, so
    nothing reverses it -- the asymmetry is in the SQL, not in the meaning.
    """
    session = _RecordingSession(
        [
            [],
            [
                SimpleNamespace(event_id=704, event_date=OCT_25),
                SimpleNamespace(event_id=705, event_date=NOV_1),
            ],
            [],
        ]
    )
    _, following = _load_adjacent(session, gap=2)

    assert [event.event_id for event in following] == [704, 705]


def test_each_adjacent_event_carries_who_serves_there():
    session = _RecordingSession(
        [
            [SimpleNamespace(event_id=699, event_date=SEP_27)],
            [],
            [
                SimpleNamespace(event_id=699, membership_id=118),
                SimpleNamespace(event_id=699, membership_id=119),
            ],
        ]
    )
    (event,), following = _load_adjacent(session)

    assert event == AdjacentMinistryEvent(
        event_id=699, event_date=SEP_27, assigned_membership_ids=frozenset({118, 119})
    )
    assert following == ()


def test_an_event_nobody_served_still_comes_back():
    """It blocks nobody, but it is still an intervening event -- which is the
    whole reason absence is an empty set rather than a dropped row. True on
    either side: an unfinalized next quarter still separates this one from the
    one after.
    """
    session = _RecordingSession(
        [
            [SimpleNamespace(event_id=699, event_date=SEP_27)],
            [SimpleNamespace(event_id=704, event_date=OCT_25)],
            [],
        ]
    )
    (preceding,), (following,) = _load_adjacent(session)

    assert preceding.assigned_membership_ids == frozenset()
    assert following.assigned_membership_ids == frozenset()


def test_loading_surrounding_events_with_no_rule_is_refused():
    """A caller here has confused "no rule" with "a gap of zero", and returning
    empty tuples would hide it.
    """
    with pytest.raises(InvalidOperationError, match="must be positive"):
        _load_adjacent(_RecordingSession([]), gap=0)


# ==========================================================================
# Assembling the whole sequence
# ==========================================================================


def test_an_unconfigured_period_yields_no_sequence_and_one_query():
    """The ordinary case: one lookup, no history, and ``None`` -- which every
    caller reads as "this rule does not apply", never as an empty sequence.
    """
    session = _RecordingSession([None])
    assert load_event_gap_sequence(
        session,
        scheduling_period_id=PERIOD_ID,
        ministry_id=MINISTRY_ID,
        schedule_version_id=VERSION_ID,
    ) is None
    assert len(session.statements) == 1


def test_a_configured_period_loads_the_rule_the_events_and_both_sides():
    session = _RecordingSession(
        [
            1,  # the configured gap
            [
                SimpleNamespace(event_id=701, event_date=OCT_4),
                SimpleNamespace(event_id=702, event_date=OCT_11),
            ],
            [SimpleNamespace(event_id=699, event_date=SEP_27)],   # preceding
            [SimpleNamespace(event_id=704, event_date=OCT_18)],   # following
            [
                SimpleNamespace(event_id=699, membership_id=118),
                SimpleNamespace(event_id=704, membership_id=119),
            ],
        ]
    )
    sequence = load_event_gap_sequence(
        session,
        scheduling_period_id=PERIOD_ID,
        ministry_id=MINISTRY_ID,
        schedule_version_id=VERSION_ID,
    )

    # The rule, the snapshot events, each side, and one assignment read.
    assert len(session.statements) == 5
    assert sequence.min_intervening_events == 1
    # One chronological line: earlier event, the version's own, later event.
    assert sequence.events == (
        (699, SEP_27), (701, OCT_4), (702, OCT_11), (704, OCT_18),
    )
    assert sequence.adjacent_events_by_membership == {
        118: frozenset({699}),
        119: frozenset({704}),
    }


def test_the_boundaries_are_the_first_and_last_scheduled_events():
    """Each side is loaded against its own end of the window -- the earlier
    query against the first scheduled event, the later one against the last.
    Using one boundary for both would look right on a one-event version and be
    wrong on every real one.
    """
    session = _RecordingSession(
        [
            1,
            [
                SimpleNamespace(event_id=701, event_date=OCT_4),
                SimpleNamespace(event_id=702, event_date=OCT_11),
            ],
            [],
            [],
        ]
    )
    load_event_gap_sequence(
        session,
        scheduling_period_id=PERIOD_ID,
        ministry_id=MINISTRY_ID,
        schedule_version_id=VERSION_ID,
    )

    preceding_sql = _compile(session.statements[2])
    following_sql = _compile(session.statements[3])
    assert "event.event_date < '2026-10-04'" in preceding_sql
    assert "event.id < 701" in preceding_sql
    assert "event.event_date > '2026-10-11'" in following_sql
    assert "event.id > 702" in following_sql


def test_supplied_scheduled_events_are_used_instead_of_a_query():
    """Readiness already holds the version's requirements, so it hands them
    over rather than paying for the same read twice.
    """
    session = _RecordingSession(
        [1, [SimpleNamespace(event_id=699, event_date=SEP_27)], [], []]
    )
    sequence = load_event_gap_sequence(
        session,
        scheduling_period_id=PERIOD_ID,
        ministry_id=MINISTRY_ID,
        schedule_version_id=VERSION_ID,
        scheduled_events=((701, OCT_4), (702, OCT_11)),
    )

    # The rule, both sides and their assignments -- but not the snapshot read
    # the caller already did.
    assert len(session.statements) == 4
    assert sequence.events == ((699, SEP_27), (701, OCT_4), (702, OCT_11))


def test_a_version_with_no_events_yields_an_empty_sequence_not_none():
    """An empty snapshot is a legitimate state. There is no boundary to load
    history against, and nothing for the rule to constrain -- but the rule
    *is* configured, and saying ``None`` would claim otherwise.
    """
    session = _RecordingSession([2])
    sequence = load_event_gap_sequence(
        session,
        scheduling_period_id=PERIOD_ID,
        ministry_id=MINISTRY_ID,
        schedule_version_id=VERSION_ID,
        scheduled_events=(),
    )

    assert sequence.min_intervening_events == 2
    assert sequence.events == ()
    assert len(session.statements) == 1


# ==========================================================================
# Reading the rule
# ==========================================================================


def test_an_admin_may_read_the_rule(session, admin):
    period = _Period(min_intervening_events=1)
    rule = get_event_gap_rule(session, actor=admin, scheduling_period=period)
    assert rule.min_intervening_events == 1
    assert rule.scheduling_period_id == PERIOD_ID
    assert rule.ministry_id == MINISTRY_ID


def test_a_head_of_another_ministry_may_not_read_the_rule(session):
    with pytest.raises(AuthorizationError):
        get_event_gap_rule(
            session, actor=_person(9, head_of=(99,)), scheduling_period=_Period()
        )


def test_an_unconfigured_rule_reads_as_none_never_zero(session, admin):
    rule = get_event_gap_rule(session, actor=admin, scheduling_period=_Period())
    assert rule.min_intervening_events is None


# ==========================================================================
# Setting, changing and clearing
# ==========================================================================


def test_recording_a_rule_sets_the_column_and_audits_it(session, head):
    period = _Period()
    set_min_intervening_events(
        session, actor=head, scheduling_period=period, min_intervening_events=1
    )

    assert period.min_intervening_events == 1
    (audit,) = session.audits
    assert audit.action == ACTION_EVENT_GAP_RULE_RECORDED
    assert audit.target_table == "scheduling_period"
    assert audit.target_id == PERIOD_ID
    assert audit.ministry_id == MINISTRY_ID
    assert audit.before_values == {"min_intervening_events": None}
    assert audit.after_values == {"min_intervening_events": 1}


def test_changing_a_rule_records_both_sides(session, head):
    period = _Period(min_intervening_events=1)
    set_min_intervening_events(
        session, actor=head, scheduling_period=period, min_intervening_events=3
    )

    assert period.min_intervening_events == 3
    (audit,) = session.audits
    assert audit.action == ACTION_EVENT_GAP_RULE_CHANGED
    assert audit.before_values == {"min_intervening_events": 1}
    assert audit.after_values == {"min_intervening_events": 3}


def test_clearing_a_rule_restores_none_and_audits_it(session, head):
    period = _Period(min_intervening_events=2)
    set_min_intervening_events(
        session, actor=head, scheduling_period=period, min_intervening_events=None
    )

    assert period.min_intervening_events is None
    (audit,) = session.audits
    assert audit.action == ACTION_EVENT_GAP_RULE_CLEARED
    assert audit.before_values == {"min_intervening_events": 2}
    assert audit.after_values == {"min_intervening_events": None}


def test_setting_the_value_it_already_has_is_a_silent_no_op(session, head):
    period = _Period(min_intervening_events=1)
    set_min_intervening_events(
        session, actor=head, scheduling_period=period, min_intervening_events=1
    )

    assert period.min_intervening_events == 1
    assert session.audits == []


def test_clearing_an_absent_rule_is_a_silent_no_op(session, head):
    """Nothing changed, so nothing is recorded: writing a history row for a
    non-event would make the trail claim a head acted when they did not.
    """
    period = _Period()
    set_min_intervening_events(
        session, actor=head, scheduling_period=period, min_intervening_events=None
    )

    assert period.min_intervening_events is None
    assert session.audits == []


def test_authorization_is_checked_even_on_the_no_op_path(session):
    """Whether a call changes anything is a separate question from whether the
    caller was allowed to ask.
    """
    with pytest.raises(AuthorizationError):
        set_min_intervening_events(
            session,
            actor=_person(9, head_of=(99,)),
            scheduling_period=_Period(),
            min_intervening_events=None,
        )


def test_a_head_of_another_ministry_may_not_set_the_rule(session):
    with pytest.raises(AuthorizationError):
        set_min_intervening_events(
            session,
            actor=_person(9, head_of=(99,)),
            scheduling_period=_Period(),
            min_intervening_events=1,
        )


def test_an_ordinary_member_may_not_set_the_rule(session):
    with pytest.raises(AuthorizationError):
        set_min_intervening_events(
            session,
            actor=_person(9),
            scheduling_period=_Period(),
            min_intervening_events=1,
        )


@pytest.mark.parametrize("value", [0, -1])
def test_a_non_positive_gap_is_refused(session, head, value):
    """Zero would mean "consecutive assignments are allowed", which the absence
    of a rule already says.
    """
    period = _Period()
    with pytest.raises(InvalidOperationError, match="must be positive"):
        set_min_intervening_events(
            session, actor=head, scheduling_period=period,
            min_intervening_events=value,
        )
    assert period.min_intervening_events is None


def test_a_boolean_gap_is_refused(session, head):
    """``bool`` is an ``int`` subclass, and ``True`` would silently become 1."""
    with pytest.raises(InvalidOperationError, match="must be an integer"):
        set_min_intervening_events(
            session, actor=head, scheduling_period=_Period(),
            min_intervening_events=True,
        )


def test_a_transient_period_is_refused(session, head):
    period = _Period()
    period.id = None
    with pytest.raises(InvalidOperationError, match="persisted"):
        set_min_intervening_events(
            session, actor=head, scheduling_period=period, min_intervening_events=1
        )


def test_a_blank_reason_is_refused(session, head):
    with pytest.raises(InvalidOperationError, match="must not be blank"):
        set_min_intervening_events(
            session, actor=head, scheduling_period=_Period(),
            min_intervening_events=1, reason="   ",
        )


def test_a_supplied_reason_is_recorded(session, head):
    set_min_intervening_events(
        session, actor=head, scheduling_period=_Period(),
        min_intervening_events=1, reason="Agreed at the ministry meeting",
    )
    (audit,) = session.audits
    assert audit.reason == "Agreed at the ministry meeting"


def test_the_summary_never_mentions_days(session, head):
    """Generic product copy: the rule counts events, and the history a person
    reads must not suggest otherwise.
    """
    set_min_intervening_events(
        session, actor=head, scheduling_period=_Period(), min_intervening_events=1
    )
    (audit,) = session.audits
    assert "day" not in audit.summary.lower()
    assert "event" in audit.summary.lower()


# ==========================================================================
# The SQL
# ==========================================================================


def test_the_rule_lookup_selects_one_column_of_one_period():
    sql = _compile(event_gap._min_intervening_events_statement(PERIOD_ID))
    assert "scheduling_period.min_intervening_events" in sql
    assert f"scheduling_period.id = {PERIOD_ID}" in sql


def test_the_preceding_events_query_is_ministry_scoped_and_skips_cancelled():
    sql = _compile(
        event_gap._preceding_events_statement(
            ministry_id=MINISTRY_ID,
            before_event_date=OCT_4,
            before_event_id=701,
            limit=1,
        )
    )
    assert f"event.ministry_id = {MINISTRY_ID}" in sql
    assert "event.cancelled_at IS NULL" in sql


def test_the_preceding_events_query_compares_date_then_id():
    """The boundary is a ``(date, id)`` pair because two events may share a
    date and the sequence must still have a defined order.
    """
    sql = _compile(
        event_gap._preceding_events_statement(
            ministry_id=MINISTRY_ID,
            before_event_date=OCT_4,
            before_event_id=701,
            limit=1,
        )
    )
    assert "event.event_date < '2026-10-04'" in sql
    assert "event.event_date = '2026-10-04' AND event.id < 701" in sql
    assert "ORDER BY event.event_date DESC, event.id DESC" in sql


def test_the_following_events_query_mirrors_the_preceding_one():
    """Predicate for predicate, with the comparison and the ordering reversed.
    An asymmetry here would enforce the rule in one direction only.
    """
    sql = _compile(
        event_gap._following_events_statement(
            ministry_id=MINISTRY_ID,
            after_event_date=OCT_18,
            after_event_id=703,
            limit=2,
        )
    )
    assert f"event.ministry_id = {MINISTRY_ID}" in sql
    assert "event.cancelled_at IS NULL" in sql
    assert "event.event_date > '2026-10-18'" in sql
    assert "event.event_date = '2026-10-18' AND event.id > 703" in sql
    assert "ORDER BY event.event_date, event.id" in sql
    assert "LIMIT 2" in sql


def test_the_two_side_queries_differ_only_in_direction():
    """Compiled side by side, the only differences are the comparison operators
    and the ordering -- so a predicate added to one side and forgotten on the
    other shows up here rather than as a rule that works one way.
    """
    preceding = _compile(
        event_gap._preceding_events_statement(
            ministry_id=MINISTRY_ID,
            before_event_date=OCT_4,
            before_event_id=701,
            limit=2,
        )
    )
    following = _compile(
        event_gap._following_events_statement(
            ministry_id=MINISTRY_ID,
            after_event_date=OCT_4,
            after_event_id=701,
            limit=2,
        )
    )
    normalized = (
        following.replace("event.event_date > ", "event.event_date < ")
        .replace("event.id > ", "event.id < ")
        .replace("ORDER BY event.event_date, event.id", "ORDER BY event.event_date DESC, event.id DESC")
    )
    assert normalized == preceding


def test_the_history_assignments_query_uses_the_authoritative_reading():
    """ADR 0003, shared rather than rewritten: the highest-numbered FINALIZED
    version per schedule, never "any FINALIZED" and never "the latest".
    """
    sql = _compile(
        event_gap._authoritative_assignments_statement(
            ministry_id=MINISTRY_ID, event_ids=[699]
        )
    )
    assert "DISTINCT ON (schedule_version.schedule_id)" in sql
    assert "schedule_version.status = 'FINALIZED'" in sql
    assert "ORDER BY schedule_version.schedule_id, schedule_version.version_number DESC" in sql
    assert f"assignment.ministry_id = {MINISTRY_ID}" in sql


def test_the_authoritative_subquery_is_the_shared_one():
    """It is the same definition the church-wide conflict rule uses. Two copies
    could disagree about what "who served" means, which is exactly what ADR
    0003 exists to prevent.
    """
    from app.services import sunday_conflict

    assert _compile(
        sunday_conflict.authoritative_version_subquery().select()
    ) == _compile(event_gap.authoritative_version_subquery().select())


def test_the_snapshot_events_query_orders_by_date_then_event():
    sql = _compile(event_gap._scheduled_events_statement(VERSION_ID))
    assert f"schedule_version_requirement.schedule_version_id = {VERSION_ID}" in sql
    assert (
        "ORDER BY schedule_version_requirement.event_date,"
        " schedule_version_requirement.event_id" in sql
    )
