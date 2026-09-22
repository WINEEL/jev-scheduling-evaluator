"""Task 74's two hard rules against real PostgreSQL rows.

The offline suite proves each rule's meaning with the reads stubbed. What only
a real database can show is that the pieces meet:

- the ``max_per_event > 0`` and ``min_supporters > 0`` CHECKs really refuse a
  stored zero, so "no rule" keeps one representation;
- the ``supporter_membership_id <> subject_membership_id`` CHECK really makes
  "the subject cannot satisfy their own requirement" structural rather than a
  service rule;
- the composite foreign keys really refuse cross-ministry configuration;
- the uniqueness constraints really refuse a second cap per (group, period) and
  a second requirement per (subject, period);
- the audit ``target_table`` CHECK really admits the new tables, so a head's
  configuration change can be recorded at all;
- and, end to end, that a real generation run through the real builder, the
  real solver and the real batch writer respects both rules and reports what it
  could not fill -- with nothing mocked.

**The constraint *names* are asserted too.** This project's ``MetaData``
naming convention is applied to a bare string in a migration, so a name not
wrapped in ``op.f()`` is silently doubled and truncated at PostgreSQL's
63-character limit -- a failure nothing else notices, because the model's own
declared name still resolves. Reading the names back from ``pg_constraint`` is
the only check that catches it.

Every test is rollback-isolated by the shared harness; nothing is committed.
Every person, ministry, group and date is synthetic, and nothing here describes
a real category or a real arrangement of any kind.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.scheduling_input import (
    MemberGroup,
    MemberGroupEventLimit,
    MemberGroupMember,
    MembershipSupportRequirement,
    MembershipSupportSupporter,
)
from app.scheduling.result import (
    DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT,
    DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT,
)
from app.scheduling.solver import SchedulingPolicy
from app.services.assignment import assign_member
from app.services.errors import InvalidOperationError
from app.services.finalization_readiness import get_finalization_readiness
from app.services.member_group import (
    MEMBER_GROUP_EVENT_LIMIT_CONFLICT,
    create_member_group,
    list_period_member_group_limits,
    load_member_group_caps,
    set_member_group_event_limit,
    set_member_group_membership,
)
from app.services.same_event_support import (
    SAME_EVENT_SUPPORT_CONFLICT,
    clear_same_event_support_requirement,
    list_support_requirements,
    load_support_requirements,
    set_same_event_support_requirement,
)
from app.services.schedule_generation import generate_draft_schedule
from tests.integration import factories as f

pytestmark = pytest.mark.integration

OCT_4 = datetime.date(2026, 10, 4)
OCT_11 = datetime.date(2026, 10, 11)

LENIENT = SchedulingPolicy(allow_no_response=True)


# ==========================================================================
# The migration landed the names it meant to
# ==========================================================================


def _constraint_names(db_session, table: str) -> set[str]:
    return {
        row[0]
        for row in db_session.execute(
            text(
                "SELECT conname FROM pg_constraint"
                " WHERE conrelid = CAST(:table AS regclass)"
            ),
            {"table": table},
        ).all()
    }


@pytest.mark.parametrize(
    "table,expected",
    [
        (
            "member_group",
            {
                "pk_member_group",
                "uq_member_group_id_ministry_id",
                "ck_member_group_name_not_blank",
                "fk_member_group_ministry_id_ministry",
            },
        ),
        (
            "member_group_member",
            {
                "pk_member_group_member",
                "uq_member_group_member_group_membership",
                "fk_member_group_member_group_ministry",
                "fk_member_group_member_membership_ministry",
            },
        ),
        (
            "member_group_event_limit",
            {
                "pk_member_group_event_limit",
                "uq_member_group_event_limit_group_period",
                "ck_member_group_event_limit_max_per_event_positive",
                "fk_member_group_event_limit_group_ministry",
                "fk_member_group_event_limit_period_ministry",
            },
        ),
        (
            "membership_support_requirement",
            {
                "pk_membership_support_requirement",
                "uq_support_requirement_subject_period",
                "uq_support_requirement_id_subject_ministry",
                "ck_membership_support_requirement_min_supporters_positive",
                "fk_support_requirement_subject_ministry",
                "fk_support_requirement_period_ministry",
            },
        ),
        (
            "membership_support_supporter",
            {
                "pk_membership_support_supporter",
                "uq_support_supporter_requirement_member",
                "ck_membership_support_supporter_supporter_is_not_subject",
                "fk_support_supporter_requirement",
                "fk_support_supporter_membership_ministry",
            },
        ),
    ],
)
def test_the_migration_created_the_constraint_names_it_declared(
    db_session, table, expected
):
    """A name not wrapped in ``op.f()`` is silently doubled and truncated, and
    nothing but this check would notice.
    """
    assert expected <= _constraint_names(db_session, table)


def test_the_audit_check_admits_the_four_new_target_tables():
    from app.models.audit import AUDIT_TARGET_TABLES

    for name in (
        "member_group",
        "member_group_member",
        "member_group_event_limit",
        "membership_support_requirement",
    ):
        assert name in AUDIT_TARGET_TABLES
    # The supporter rows are deliberately not a target: they are only ever
    # written as part of configuring the requirement that owns them.
    assert "membership_support_supporter" not in AUDIT_TARGET_TABLES


# ==========================================================================
# Fixtures
# ==========================================================================


@pytest.fixture
def world(db_session):
    """One ministry, one role, two events, an Admin and four volunteers."""
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church)
    head = f.make_ministry_head(
        db_session, church=church, ministry=ministry, name="Head"
    )
    other_ministry = f.make_ministry(db_session, church=church, name="Other")
    role = f.make_role(db_session, ministry=ministry)
    period = f.make_period(db_session, ministry=ministry)
    other_period = f.make_period(db_session, ministry=other_ministry)
    first = f.make_event(db_session, period=period, event_date=OCT_4)
    second = f.make_event(db_session, period=period, event_date=OCT_11)

    memberships = []
    for index in range(4):
        person = f.make_person(db_session, church=church, name=f"Volunteer {index}")
        membership = f.make_membership(db_session, person=person, ministry=ministry)
        f.make_qualification(
            db_session, membership=membership, role=role, decided_by=head
        )
        memberships.append(membership)

    foreign_person = f.make_person(db_session, church=church, name="Foreign")
    foreign = f.make_membership(
        db_session, person=foreign_person, ministry=other_ministry
    )
    db_session.flush()
    return {
        "church": church, "head": head, "ministry": ministry,
        "other_ministry": other_ministry, "other_period": other_period,
        "role": role, "period": period, "first": first, "second": second,
        "memberships": memberships, "foreign": foreign,
    }


def _group_with_members(db_session, world, memberships, *, name="Category A"):
    group = create_member_group(
        db_session, actor=world["head"], ministry=world["ministry"], name=name
    )
    db_session.flush()
    for membership in memberships:
        set_member_group_membership(
            db_session, actor=world["head"], member_group=group,
            membership=membership, is_member=True,
        )
    db_session.flush()
    return group


def _staff(db_session, world, event, *, count: int):
    f.make_staffing_requirement(
        db_session, event=event, role=world["role"], required_count=count
    )


# ==========================================================================
# The database's own guarantees
# ==========================================================================


def test_a_cap_of_zero_is_refused_by_the_database(db_session, world):
    group = _group_with_members(db_session, world, world["memberships"][:2])

    db_session.add(
        MemberGroupEventLimit(
            member_group_id=group.id,
            scheduling_period_id=world["period"].id,
            ministry_id=world["ministry"].id,
            max_per_event=0,
        )
    )
    with pytest.raises(IntegrityError, match="max_per_event_positive"):
        db_session.flush()


def test_two_caps_for_one_group_and_period_are_refused(db_session, world):
    group = _group_with_members(db_session, world, world["memberships"][:2])
    set_member_group_event_limit(
        db_session, actor=world["head"], member_group=group,
        scheduling_period=world["period"], max_per_event=2,
    )
    db_session.flush()

    db_session.add(
        MemberGroupEventLimit(
            member_group_id=group.id,
            scheduling_period_id=world["period"].id,
            ministry_id=world["ministry"].id,
            max_per_event=1,
        )
    )
    with pytest.raises(IntegrityError, match="uq_member_group_event_limit_group_period"):
        db_session.flush()


def test_a_cap_cannot_be_hung_off_another_ministrys_period(db_session, world):
    group = _group_with_members(db_session, world, world["memberships"][:1])

    db_session.add(
        MemberGroupEventLimit(
            member_group_id=group.id,
            scheduling_period_id=world["other_period"].id,
            ministry_id=world["ministry"].id,
            max_per_event=1,
        )
    )
    with pytest.raises(IntegrityError, match="fk_member_group_event_limit_period_ministry"):
        db_session.flush()


def test_a_membership_from_another_ministry_cannot_join_a_group(db_session, world):
    group = _group_with_members(db_session, world, [])

    db_session.add(
        MemberGroupMember(
            member_group_id=group.id,
            ministry_membership_id=world["foreign"].id,
            ministry_id=world["ministry"].id,
        )
    )
    with pytest.raises(IntegrityError, match="fk_member_group_member_membership_ministry"):
        db_session.flush()


def test_two_groups_of_one_ministry_may_not_share_a_name(db_session, world):
    create_member_group(
        db_session, actor=world["head"], ministry=world["ministry"], name="Category A"
    )
    db_session.flush()

    db_session.add(MemberGroup(ministry_id=world["ministry"].id, name="category a"))
    with pytest.raises(IntegrityError, match="uq_member_group_ministry_id_name_lower"):
        db_session.flush()


def test_a_support_requirement_of_zero_is_refused_by_the_database(db_session, world):
    db_session.add(
        MembershipSupportRequirement(
            subject_membership_id=world["memberships"][0].id,
            scheduling_period_id=world["period"].id,
            ministry_id=world["ministry"].id,
            min_supporters=0,
        )
    )
    with pytest.raises(IntegrityError, match="min_supporters_positive"):
        db_session.flush()


def test_the_subject_cannot_be_their_own_supporter_in_the_database(db_session, world):
    """The service refuses it too, but this makes it structural: a row that
    supported itself would make the whole constraint vacuous in a way nothing
    downstream would notice.
    """
    subject = world["memberships"][0]
    requirement = set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"],
        supporter_memberships=[world["memberships"][1]],
    )
    db_session.flush()

    db_session.add(
        MembershipSupportSupporter(
            support_requirement_id=requirement.id,
            subject_membership_id=subject.id,
            supporter_membership_id=subject.id,
            ministry_id=world["ministry"].id,
        )
    )
    with pytest.raises(IntegrityError, match="supporter_is_not_subject"):
        db_session.flush()


def test_a_supporter_row_cannot_claim_a_different_subject(db_session, world):
    """The supporter row is routed back to its parent through
    ``(id, subject_membership_id, ministry_id)``, so the copied subject cannot
    disagree with the requirement it belongs to.
    """
    subject = world["memberships"][0]
    requirement = set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"],
        supporter_memberships=[world["memberships"][1]],
    )
    db_session.flush()

    db_session.add(
        MembershipSupportSupporter(
            support_requirement_id=requirement.id,
            subject_membership_id=world["memberships"][2].id,
            supporter_membership_id=world["memberships"][3].id,
            ministry_id=world["ministry"].id,
        )
    )
    with pytest.raises(IntegrityError, match="fk_support_supporter_requirement"):
        db_session.flush()


def test_two_requirements_for_one_subject_and_period_are_refused(db_session, world):
    subject = world["memberships"][0]
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"],
        supporter_memberships=[world["memberships"][1]],
    )
    db_session.flush()

    db_session.add(
        MembershipSupportRequirement(
            subject_membership_id=subject.id,
            scheduling_period_id=world["period"].id,
            ministry_id=world["ministry"].id,
            min_supporters=1,
        )
    )
    with pytest.raises(IntegrityError, match="uq_support_requirement_subject_period"):
        db_session.flush()


# ==========================================================================
# The services, against real rows
# ==========================================================================


def test_configuring_a_cap_is_readable_and_audited(db_session, world):
    from app.models.audit import AuditEvent

    group = _group_with_members(db_session, world, world["memberships"][:3])
    set_member_group_event_limit(
        db_session, actor=world["head"], member_group=group,
        scheduling_period=world["period"], max_per_event=2,
    )
    db_session.flush()

    (cap,) = load_member_group_caps(
        db_session, scheduling_period_id=world["period"].id
    )
    assert cap.max_per_event == 2
    assert len(cap.member_membership_ids) == 3

    listed = list_period_member_group_limits(
        db_session, actor=world["head"], scheduling_period=world["period"]
    )
    assert [(g.name, g.member_count, g.max_per_event) for g in listed.groups] == [
        (group.name, 3, 2)
    ]

    actions = {
        row.action
        for row in db_session.execute(
            text("SELECT action FROM audit_event WHERE ministry_id = :m"),
            {"m": world["ministry"].id},
        ).all()
    }
    assert "MEMBER_GROUP_CREATED" in actions
    assert "MEMBER_GROUP_MEMBER_ADDED" in actions
    assert "MEMBER_GROUP_LIMIT_RECORDED" in actions


def test_configuring_a_support_requirement_is_readable_and_audited(db_session, world):
    subject, supporter = world["memberships"][0], world["memberships"][1]
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"], supporter_memberships=[supporter],
    )
    db_session.flush()

    (config,) = load_support_requirements(
        db_session, scheduling_period_id=world["period"].id
    )
    assert config.subject_membership_id == subject.id
    assert config.supporter_membership_ids == frozenset({supporter.id})

    listed = list_support_requirements(
        db_session, actor=world["head"], scheduling_period=world["period"]
    )
    assert listed.requirements[0].subject_display_name == subject.person.display_name

    actions = {
        row.action
        for row in db_session.execute(
            text("SELECT action FROM audit_event WHERE ministry_id = :m"),
            {"m": world["ministry"].id},
        ).all()
    }
    assert "SUPPORT_REQUIREMENT_RECORDED" in actions


def test_revising_a_requirement_replaces_the_supporter_rows(db_session, world):
    subject = world["memberships"][0]
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"],
        supporter_memberships=[world["memberships"][1]],
    )
    db_session.flush()

    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"],
        supporter_memberships=[world["memberships"][2], world["memberships"][3]],
    )
    db_session.flush()

    (config,) = load_support_requirements(
        db_session, scheduling_period_id=world["period"].id
    )
    assert config.supporter_membership_ids == frozenset(
        {world["memberships"][2].id, world["memberships"][3].id}
    )


def test_clearing_a_requirement_removes_every_row(db_session, world):
    subject = world["memberships"][0]
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"],
        supporter_memberships=[world["memberships"][1], world["memberships"][2]],
    )
    db_session.flush()

    clear_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"],
    )
    db_session.flush()

    assert load_support_requirements(
        db_session, scheduling_period_id=world["period"].id
    ) == ()
    remaining = db_session.execute(
        text(
            "SELECT count(*) FROM membership_support_supporter"
            " WHERE subject_membership_id = :s"
        ),
        {"s": subject.id},
    ).scalar()
    assert remaining == 0


# ==========================================================================
# End to end: the real builder, the real solver, the real writer
# ==========================================================================


def test_a_generated_schedule_respects_the_member_group_cap(db_session, world):
    """Three positions on one event, three candidates, all in a group capped at
    two: the run fills two and reports the third with the cap's own diagnostic.
    """
    _staff(db_session, world, world["first"], count=3)
    for membership in world["memberships"][:3]:
        f.make_availability(
            db_session, membership=membership, event=world["first"],
            state="AVAILABLE",
        )
    group = _group_with_members(db_session, world, world["memberships"][:3])
    set_member_group_event_limit(
        db_session, actor=world["head"], member_group=group,
        scheduling_period=world["period"], max_per_event=2,
    )
    # The fourth volunteer is deliberately unavailable, so the group is the
    # only pool and the cap is the only thing that can leave a position open.
    f.make_availability(
        db_session, membership=world["memberships"][3], event=world["first"],
        state="UNAVAILABLE",
    )
    db_session.flush()

    schedule = f.make_schedule(db_session, period=world["period"])
    version = f.make_version(db_session, schedule=schedule, period=world["period"])
    f.make_version_requirement(
        db_session, version=version, event=world["first"], role=world["role"],
        required_count=3,
    )
    db_session.flush()

    result = generate_draft_schedule(
        db_session, actor=world["head"], version=version, policy=LENIENT
    )
    db_session.flush()

    assert len(result.created_assignments) == 2
    codes = {
        code
        for unfilled in result.scheduling_result.unfilled_requirements
        for code in unfilled.diagnostic_codes
    }
    assert DIAGNOSTIC_ALL_AT_GROUP_EVENT_LIMIT in codes

    readiness = get_finalization_readiness(db_session, version=version)
    # Short, but not *violating*: the cap is respected, the position is open.
    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT not in {i.code for i in readiness.issues}


def test_a_generated_schedule_places_a_supporter_beside_the_subject(db_session, world):
    """Two positions, a subject and their one approved supporter: both filled,
    and the finished version is ready.
    """
    _staff(db_session, world, world["first"], count=2)
    subject, supporter = world["memberships"][0], world["memberships"][1]
    for membership in (subject, supporter):
        f.make_availability(
            db_session, membership=membership, event=world["first"], state="AVAILABLE"
        )
    for membership in world["memberships"][2:]:
        f.make_availability(
            db_session, membership=membership, event=world["first"], state="UNAVAILABLE"
        )
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"], supporter_memberships=[supporter],
    )
    db_session.flush()

    schedule = f.make_schedule(db_session, period=world["period"])
    version = f.make_version(db_session, schedule=schedule, period=world["period"])
    f.make_version_requirement(
        db_session, version=version, event=world["first"], role=world["role"],
        required_count=2,
    )
    db_session.flush()

    result = generate_draft_schedule(
        db_session, actor=world["head"], version=version, policy=LENIENT
    )
    db_session.flush()

    placed = {a.ministry_membership_id for a in result.created_assignments}
    assert placed == {subject.id, supporter.id}
    assert get_finalization_readiness(db_session, version=version).is_ready is True


def test_a_subject_with_no_available_supporter_is_left_off(db_session, world):
    """One position, a subject whose only approved supporter is unavailable:
    the position comes back unfilled with the support rule's own diagnostic,
    and the subject is not placed alone.
    """
    _staff(db_session, world, world["first"], count=1)
    subject, supporter = world["memberships"][0], world["memberships"][1]
    f.make_availability(
        db_session, membership=subject, event=world["first"], state="AVAILABLE"
    )
    f.make_availability(
        db_session, membership=supporter, event=world["first"], state="UNAVAILABLE"
    )
    for membership in world["memberships"][2:]:
        f.make_availability(
            db_session, membership=membership, event=world["first"], state="UNAVAILABLE"
        )
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"], supporter_memberships=[supporter],
    )
    db_session.flush()

    schedule = f.make_schedule(db_session, period=world["period"])
    version = f.make_version(db_session, schedule=schedule, period=world["period"])
    f.make_version_requirement(
        db_session, version=version, event=world["first"], role=world["role"],
    )
    db_session.flush()

    result = generate_draft_schedule(
        db_session, actor=world["head"], version=version, policy=LENIENT
    )
    db_session.flush()

    assert result.created_assignments == ()
    codes = {
        code
        for unfilled in result.scheduling_result.unfilled_requirements
        for code in unfilled.diagnostic_codes
    }
    assert DIAGNOSTIC_ALL_WITHOUT_EVENT_SUPPORT in codes


def test_manual_assignment_refuses_a_subject_without_support(db_session, world):
    _staff(db_session, world, world["first"], count=2)
    subject, supporter = world["memberships"][0], world["memberships"][1]
    for membership in world["memberships"]:
        f.make_availability(
            db_session, membership=membership, event=world["first"], state="AVAILABLE"
        )
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"], supporter_memberships=[supporter],
    )
    db_session.flush()

    schedule = f.make_schedule(db_session, period=world["period"])
    version = f.make_version(db_session, schedule=schedule, period=world["period"])
    requirement = f.make_version_requirement(
        db_session, version=version, event=world["first"], role=world["role"],
        required_count=2,
    )
    db_session.flush()

    with pytest.raises(InvalidOperationError, match=SAME_EVENT_SUPPORT_CONFLICT):
        assign_member(
            db_session, actor=world["head"], requirement=requirement,
            membership=subject,
        )


def test_manual_assignment_allows_the_subject_once_the_supporter_is_there(
    db_session, world
):
    _staff(db_session, world, world["first"], count=2)
    subject, supporter = world["memberships"][0], world["memberships"][1]
    for membership in world["memberships"]:
        f.make_availability(
            db_session, membership=membership, event=world["first"], state="AVAILABLE"
        )
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"], supporter_memberships=[supporter],
    )
    db_session.flush()

    schedule = f.make_schedule(db_session, period=world["period"])
    version = f.make_version(db_session, schedule=schedule, period=world["period"])
    first_position = f.make_version_requirement(
        db_session, version=version, event=world["first"], role=world["role"],
        required_count=2,
    )
    db_session.flush()

    assign_member(
        db_session, actor=world["head"], requirement=first_position,
        membership=supporter,
    )
    db_session.flush()
    assignment = assign_member(
        db_session, actor=world["head"], requirement=first_position,
        membership=subject,
    )
    db_session.flush()

    assert assignment.ministry_membership_id == subject.id


def test_removing_a_supporter_leaves_the_version_unfinalizable(db_session, world):
    """Removal is the cleanup path and refuses almost nothing, so this is
    exactly the state gate 8 exists to catch -- reported, never repaired.
    """
    from app.services.assignment import remove_assignment

    _staff(db_session, world, world["first"], count=2)
    subject, supporter = world["memberships"][0], world["memberships"][1]
    for membership in world["memberships"]:
        f.make_availability(
            db_session, membership=membership, event=world["first"], state="AVAILABLE"
        )
    set_same_event_support_requirement(
        db_session, actor=world["head"], subject_membership=subject,
        scheduling_period=world["period"], supporter_memberships=[supporter],
    )
    db_session.flush()

    schedule = f.make_schedule(db_session, period=world["period"])
    version = f.make_version(db_session, schedule=schedule, period=world["period"])
    position = f.make_version_requirement(
        db_session, version=version, event=world["first"], role=world["role"],
        required_count=2,
    )
    db_session.flush()

    supporter_row = assign_member(
        db_session, actor=world["head"], requirement=position, membership=supporter
    )
    db_session.flush()
    assign_member(
        db_session, actor=world["head"], requirement=position, membership=subject
    )
    db_session.flush()

    remove_assignment(db_session, actor=world["head"], assignment=supporter_row)
    db_session.flush()

    readiness = get_finalization_readiness(db_session, version=version)
    assert SAME_EVENT_SUPPORT_CONFLICT in {issue.code for issue in readiness.issues}


def test_a_cap_recorded_after_a_draft_exists_blocks_finalization_without_deleting(
    db_session, world
):
    _staff(db_session, world, world["first"], count=2)
    for membership in world["memberships"]:
        f.make_availability(
            db_session, membership=membership, event=world["first"], state="AVAILABLE"
        )
    db_session.flush()

    schedule = f.make_schedule(db_session, period=world["period"])
    version = f.make_version(db_session, schedule=schedule, period=world["period"])
    position = f.make_version_requirement(
        db_session, version=version, event=world["first"], role=world["role"],
        required_count=2,
    )
    db_session.flush()

    for membership in world["memberships"][:2]:
        assign_member(
            db_session, actor=world["head"], requirement=position, membership=membership
        )
        db_session.flush()

    group = _group_with_members(db_session, world, world["memberships"][:2])
    set_member_group_event_limit(
        db_session, actor=world["head"], member_group=group,
        scheduling_period=world["period"], max_per_event=1,
    )
    db_session.flush()

    readiness = get_finalization_readiness(db_session, version=version)
    assert MEMBER_GROUP_EVENT_LIMIT_CONFLICT in {i.code for i in readiness.issues}
    # Nothing was deleted to make the new rule true.
    still_there = db_session.execute(
        text(
            "SELECT count(*) FROM assignment WHERE schedule_version_id = :v"
        ),
        {"v": version.id},
    ).scalar()
    assert still_there == 2
