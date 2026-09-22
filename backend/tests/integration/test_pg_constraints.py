"""Integration Tests B and C -- constraints the offline suite can only read.

The unit suite compiles these constructs and inspects their DDL. Neither
proves PostgreSQL *enforces* them. These tests do, by attempting the violation
and requiring the database to refuse it.

Every expected failure is wrapped in a SAVEPOINT (``session.begin_nested()``),
because a failed statement poisons a PostgreSQL transaction until it is rolled
back -- releasing only the savepoint lets each test go on to prove the
*positive* case on the same connection.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.models.scheduling_input import (
    ExistingCommitment,
    MembershipSameDateExclusion,
    MembershipServingLimit,
)
from tests.integration import factories as f

pytestmark = pytest.mark.integration

UTC = datetime.timezone.utc
COMMITMENT_DATE = datetime.date(2026, 11, 15)


# --------------------------------------------------------------------------
# Test B -- the RoleQualification cross-ministry composite foreign keys
# --------------------------------------------------------------------------


def test_b1_role_qualification_cannot_pair_a_membership_and_role_from_different_ministries(
    db_session,
):
    """core §7.2: both composite FKs route through the row's single
    ``ministry_id``, so a Ministry A membership can never hold a Ministry B
    role -- "not by the application, not by a backfill, not by hand in psql".
    """
    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    ministry_a = f.make_ministry(db_session, church=church, name="Setup")
    ministry_b = f.make_ministry(db_session, church=church, name="AV")
    member = f.make_person(db_session, church=church, name="Member")
    membership_in_a = f.make_membership(db_session, person=member, ministry=ministry_a)
    role_in_b = f.make_role(db_session, ministry=ministry_b, name="Sound")

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            # ministry_id = A satisfies the membership key and must break the
            # role key, because role_in_b lives in ministry B.
            f.make_qualification(
                db_session, membership=membership_in_a, role=role_in_b,
                decided_by=admin, ministry_id=ministry_a.id,
            )
            db_session.flush()

    # The database, not the service layer, is what refused it.
    assert "fk_role_qualification_role_ministry" in str(exc_info.value.orig)


def test_b2_the_same_pairing_is_rejected_from_the_other_direction_too(db_session):
    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    ministry_a = f.make_ministry(db_session, church=church, name="Setup")
    ministry_b = f.make_ministry(db_session, church=church, name="AV")
    member = f.make_person(db_session, church=church, name="Member")
    membership_in_a = f.make_membership(db_session, person=member, ministry=ministry_a)
    role_in_b = f.make_role(db_session, ministry=ministry_b, name="Sound")

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            # ministry_id = B now satisfies the role key and must break the
            # membership key. There is no value of ministry_id that works.
            f.make_qualification(
                db_session, membership=membership_in_a, role=role_in_b,
                decided_by=admin, ministry_id=ministry_b.id,
            )
            db_session.flush()

    assert "fk_role_qualification_membership_ministry" in str(exc_info.value.orig)


def test_b3_a_same_ministry_qualification_is_accepted(db_session):
    """The positive case, on the same connection the rejections poisoned --
    proving the savepoints above left the transaction usable, and that the
    constraint blocks only the cross-ministry pairing.
    """
    church = f.make_church(db_session)
    admin = f.make_person(db_session, church=church, name="Admin", is_admin=True)
    ministry = f.make_ministry(db_session, church=church, name="Setup")
    member = f.make_person(db_session, church=church, name="Member")
    membership = f.make_membership(db_session, person=member, ministry=ministry)
    role = f.make_role(db_session, ministry=ministry, name="Setup Lead")

    qualification = f.make_qualification(
        db_session, membership=membership, role=role, decided_by=admin,
    )
    db_session.flush()

    assert qualification.id is not None
    stored = db_session.execute(
        text(
            "SELECT ministry_id, is_qualified FROM role_qualification WHERE id = :id"
        ),
        {"id": qualification.id},
    ).one()
    assert stored.ministry_id == ministry.id
    assert stored.is_qualified is True


# --------------------------------------------------------------------------
# Test C -- ExistingCommitment NULLS NOT DISTINCT uniqueness
# --------------------------------------------------------------------------


def _commitment(session, *, person, source_ministry_id, reason):
    commitment = ExistingCommitment(
        person_id=person.id,
        commitment_date=COMMITMENT_DATE,
        source_ministry_id=source_ministry_id,
        reason=reason,
    )
    session.add(commitment)
    return commitment


def test_c1_a_second_source_less_commitment_for_the_same_person_and_date_is_rejected(
    db_session,
):
    """scheduling-input §4: the unique index is ``NULLS NOT DISTINCT``, so two
    rows with a NULL ``source_ministry_id`` collide. A plain UNIQUE would treat
    each NULL as distinct and let unlimited duplicates through -- which is the
    behavior this test exists to disprove. PostgreSQL 15+ only; nothing about
    this is emulated in Python.
    """
    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church, name="Preacher")

    _commitment(db_session, person=person, source_ministry_id=None, reason="Preaching")
    db_session.flush()

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            _commitment(
                db_session, person=person, source_ministry_id=None,
                reason="Preaching again",
            )
            db_session.flush()

    assert "uq_existing_commitment_person_date_source" in str(exc_info.value.orig)


def test_c2_the_index_really_is_nulls_not_distinct_in_the_database(db_session):
    # Reading the catalogue as well as the behavior: indnullsnotdistinct is the
    # column that makes C1's rejection possible at all.
    nulls_not_distinct = db_session.execute(
        text(
            "SELECT i.indnullsnotdistinct FROM pg_index i"
            " JOIN pg_class c ON c.oid = i.indexrelid"
            " WHERE c.relname = 'uq_existing_commitment_person_date_source'"
        )
    ).scalar_one()

    assert nulls_not_distinct is True


def test_c3_a_different_date_or_a_source_ministry_is_still_allowed(db_session):
    """The uniqueness is on the whole logical key, not on the person."""
    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church, name="Preacher")
    ministry = f.make_ministry(db_session, church=church, name="AV")

    _commitment(db_session, person=person, source_ministry_id=None, reason="Preaching")
    db_session.flush()

    # Same person and date, but a real source ministry -> a different key.
    _commitment(db_session, person=person, source_ministry_id=ministry.id, reason=None)
    # Same person, no source, different date -> also a different key.
    other = ExistingCommitment(
        person_id=person.id,
        commitment_date=COMMITMENT_DATE + datetime.timedelta(days=7),
        source_ministry_id=None,
        reason="Preaching elsewhere",
    )
    db_session.add(other)
    db_session.flush()

    count = db_session.execute(
        text("SELECT count(*) FROM existing_commitment WHERE person_id = :p"),
        {"p": person.id},
    ).scalar_one()
    assert count == 3


def test_c4_a_commitment_with_neither_source_nor_reason_is_rejected(db_session):
    """The sibling ``provenance_required`` CHECK, proven the same way."""
    church = f.make_church(db_session)
    person = f.make_person(db_session, church=church, name="Preacher")

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            _commitment(db_session, person=person, source_ministry_id=None, reason=None)
            db_session.flush()

    assert "provenance_required" in str(exc_info.value.orig)


# --------------------------------------------------------------------------
# Test G -- the MembershipServingLimit cross-ministry composite foreign keys
# and the positivity CHECK (Task 47)
#
# The offline suite inspects this DDL; only PostgreSQL can prove it enforced.
# --------------------------------------------------------------------------


def test_g1_a_serving_limit_cannot_pair_a_membership_and_period_from_different_ministries(
    db_session,
):
    """The scope rule, made physical.

    An AV head must not be able to record a limit that binds a Setup period.
    The service layer refuses it too, but that check protects against mistakes
    in application code; this one holds against a backfill or a hand-written
    statement in psql.
    """
    church = f.make_church(db_session)
    ministry_a = f.make_ministry(db_session, church=church, name="Setup")
    ministry_b = f.make_ministry(db_session, church=church, name="AV")
    member = f.make_person(db_session, church=church, name="Member")
    membership_in_a = f.make_membership(db_session, person=member, ministry=ministry_a)
    period_in_b = f.make_period(db_session, ministry=ministry_b, name="AV Q4 2026")

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            # ministry_id = A satisfies the membership key and must break the
            # period key, because period_in_b lives in ministry B.
            db_session.add(
                MembershipServingLimit(
                    ministry_membership_id=membership_in_a.id,
                    scheduling_period_id=period_in_b.id,
                    ministry_id=ministry_a.id,
                    max_assignments=4,
                )
            )
            db_session.flush()

    assert "fk_membership_serving_limit_period_ministry" in str(exc_info.value.orig)


def test_g2_the_same_pairing_is_rejected_from_the_other_direction_too(db_session):
    """There is no value of ``ministry_id`` that lets the pairing through."""
    church = f.make_church(db_session)
    ministry_a = f.make_ministry(db_session, church=church, name="Setup")
    ministry_b = f.make_ministry(db_session, church=church, name="AV")
    member = f.make_person(db_session, church=church, name="Member")
    membership_in_a = f.make_membership(db_session, person=member, ministry=ministry_a)
    period_in_b = f.make_period(db_session, ministry=ministry_b, name="AV Q4 2026")

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.add(
                MembershipServingLimit(
                    ministry_membership_id=membership_in_a.id,
                    scheduling_period_id=period_in_b.id,
                    ministry_id=ministry_b.id,
                    max_assignments=4,
                )
            )
            db_session.flush()

    assert "fk_membership_serving_limit_membership_ministry" in str(exc_info.value.orig)


def test_g3_a_matching_membership_and_period_is_accepted(db_session):
    """The positive case, on the same connection: the constraints refuse the
    cross-ministry pairing without refusing the ordinary one."""
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="AV")
    member = f.make_person(db_session, church=church, name="Member")
    membership = f.make_membership(db_session, person=member, ministry=ministry)
    period = f.make_period(db_session, ministry=ministry, name="AV Q4 2026")

    limit = MembershipServingLimit(
        ministry_membership_id=membership.id,
        scheduling_period_id=period.id,
        ministry_id=ministry.id,
        max_assignments=4,
    )
    db_session.add(limit)
    db_session.flush()

    assert limit.id is not None


def test_g4_a_non_positive_maximum_is_refused_by_the_database(db_session):
    """Zero is not a limit; it is "never schedule this person", which
    deactivation and UNAVAILABLE already say."""
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="AV")
    member = f.make_person(db_session, church=church, name="Member")
    membership = f.make_membership(db_session, person=member, ministry=ministry)
    period = f.make_period(db_session, ministry=ministry, name="AV Q4 2026")

    for bad in (0, -1):
        with pytest.raises(IntegrityError) as exc_info:
            with db_session.begin_nested():
                db_session.add(
                    MembershipServingLimit(
                        ministry_membership_id=membership.id,
                        scheduling_period_id=period.id,
                        ministry_id=ministry.id,
                        max_assignments=bad,
                    )
                )
                db_session.flush()
        assert "max_assignments_positive" in str(exc_info.value.orig)


def test_g5_one_effective_limit_per_membership_per_period(db_session):
    """A second row would be a second answer to a question that has one."""
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="AV")
    member = f.make_person(db_session, church=church, name="Member")
    membership = f.make_membership(db_session, person=member, ministry=ministry)
    period = f.make_period(db_session, ministry=ministry, name="AV Q4 2026")

    db_session.add(
        MembershipServingLimit(
            ministry_membership_id=membership.id,
            scheduling_period_id=period.id,
            ministry_id=ministry.id, max_assignments=4,
        )
    )
    db_session.flush()

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.add(
                MembershipServingLimit(
                    ministry_membership_id=membership.id,
                    scheduling_period_id=period.id,
                    ministry_id=ministry.id, max_assignments=5,
                )
            )
            db_session.flush()

    assert "uq_membership_serving_limit_membership_period" in str(exc_info.value.orig)


def test_g6_the_same_member_may_hold_different_limits_in_two_ministries(db_session):
    """The scope rule's positive half.

    Volunteer A may accept four AV Sundays and two Setup Sundays in the same
    quarter. A person-level limit could not represent that, and this is the
    schema proving the membership-level one does.
    """
    church = f.make_church(db_session)
    av = f.make_ministry(db_session, church=church, name="AV")
    setup = f.make_ministry(db_session, church=church, name="Setup")
    member = f.make_person(db_session, church=church, name="Member")
    av_membership = f.make_membership(db_session, person=member, ministry=av)
    setup_membership = f.make_membership(db_session, person=member, ministry=setup)
    av_period = f.make_period(db_session, ministry=av, name="AV Q4 2026")
    setup_period = f.make_period(db_session, ministry=setup, name="Setup Q4 2026")

    db_session.add_all(
        [
            MembershipServingLimit(
                ministry_membership_id=av_membership.id,
                scheduling_period_id=av_period.id,
                ministry_id=av.id, max_assignments=4,
            ),
            MembershipServingLimit(
                ministry_membership_id=setup_membership.id,
                scheduling_period_id=setup_period.id,
                ministry_id=setup.id, max_assignments=2,
            ),
        ]
    )
    db_session.flush()

    rows = (
        db_session.query(MembershipServingLimit)
        .filter(
            MembershipServingLimit.ministry_membership_id.in_(
                [av_membership.id, setup_membership.id]
            )
        )
        .all()
    )
    assert sorted(r.max_assignments for r in rows) == [2, 4]


# --------------------------------------------------------------------------
# Group H (Task 50) -- the linked-pair same-date exclusion
#
# Three composite foreign keys through one ministry_id, one CHECK for
# canonical order, and one uniqueness constraint. The offline suite compiles
# the DDL; these tests prove PostgreSQL enforces it.
#
# Every person and ministry here is synthetic.
# --------------------------------------------------------------------------


def test_h1_a_reversed_duplicate_pair_cannot_be_created(db_session):
    """A+B and B+A are one rule, and the database is what makes that true.

    Canonical order is a CHECK, not a service convention, so the reversed row
    is refused before uniqueness is even consulted -- which is why no reversed
    duplicate can exist however a writer reaches the table.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="Kids")
    person_a = f.make_person(db_session, church=church, name="Volunteer A")
    person_b = f.make_person(db_session, church=church, name="Volunteer B")
    membership_a = f.make_membership(db_session, person=person_a, ministry=ministry)
    membership_b = f.make_membership(db_session, person=person_b, ministry=ministry)
    period = f.make_period(db_session, ministry=ministry, name="Kids Q4 2026")
    low, high = sorted([membership_a.id, membership_b.id])

    db_session.add(
        MembershipSameDateExclusion(
            membership_a_id=low, membership_b_id=high,
            scheduling_period_id=period.id, ministry_id=ministry.id,
        )
    )
    db_session.flush()

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.add(
                MembershipSameDateExclusion(
                    membership_a_id=high, membership_b_id=low,
                    scheduling_period_id=period.id, ministry_id=ministry.id,
                )
            )
            db_session.flush()

    assert "pair_canonically_ordered" in str(exc_info.value.orig)


def test_h2_the_same_canonical_pair_cannot_be_recorded_twice_in_one_period(
    db_session,
):
    """One effective exclusion per unordered pair per period."""
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="Kids")
    person_a = f.make_person(db_session, church=church, name="Volunteer A")
    person_b = f.make_person(db_session, church=church, name="Volunteer B")
    membership_a = f.make_membership(db_session, person=person_a, ministry=ministry)
    membership_b = f.make_membership(db_session, person=person_b, ministry=ministry)
    period = f.make_period(db_session, ministry=ministry, name="Kids Q4 2026")
    low, high = sorted([membership_a.id, membership_b.id])

    def row():
        return MembershipSameDateExclusion(
            membership_a_id=low, membership_b_id=high,
            scheduling_period_id=period.id, ministry_id=ministry.id,
        )

    db_session.add(row())
    db_session.flush()

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.add(row())
            db_session.flush()

    assert "uq_same_date_exclusion_pair_period" in str(exc_info.value.orig)


def test_h3_a_membership_cannot_be_linked_to_itself(db_session):
    """"Linked to themselves" would mean "may never serve", which
    deactivating the membership already says -- so it is unrepresentable."""
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="Kids")
    person = f.make_person(db_session, church=church, name="Volunteer A")
    membership = f.make_membership(db_session, person=person, ministry=ministry)
    period = f.make_period(db_session, ministry=ministry, name="Kids Q4 2026")

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.add(
                MembershipSameDateExclusion(
                    membership_a_id=membership.id, membership_b_id=membership.id,
                    scheduling_period_id=period.id, ministry_id=ministry.id,
                )
            )
            db_session.flush()

    assert "pair_canonically_ordered" in str(exc_info.value.orig)


def test_h4_a_cross_ministry_pair_is_refused_whichever_ministry_id_is_written(
    db_session,
):
    """No value of ``ministry_id`` lets a Kids member be linked to a Setup one.

    Writing Kids satisfies the first membership's key and breaks the second's;
    writing Setup breaks the first. The pairing is therefore impossible at the
    database level, not only in the service.
    """
    church = f.make_church(db_session)
    kids = f.make_ministry(db_session, church=church, name="Kids")
    setup = f.make_ministry(db_session, church=church, name="Setup")
    person_a = f.make_person(db_session, church=church, name="Volunteer A")
    person_b = f.make_person(db_session, church=church, name="Volunteer B")
    kids_membership = f.make_membership(db_session, person=person_a, ministry=kids)
    setup_membership = f.make_membership(db_session, person=person_b, ministry=setup)
    period = f.make_period(db_session, ministry=kids, name="Kids Q4 2026")
    low, high = sorted([kids_membership.id, setup_membership.id])

    for ministry in (kids, setup):
        with pytest.raises(IntegrityError) as exc_info:
            with db_session.begin_nested():
                db_session.add(
                    MembershipSameDateExclusion(
                        membership_a_id=low, membership_b_id=high,
                        scheduling_period_id=period.id, ministry_id=ministry.id,
                    )
                )
                db_session.flush()
        assert "fk_same_date_exclusion_" in str(exc_info.value.orig)


def test_h5_a_period_from_another_ministry_is_refused(db_session):
    """Two Kids memberships cannot be hung off a Setup period."""
    church = f.make_church(db_session)
    kids = f.make_ministry(db_session, church=church, name="Kids")
    setup = f.make_ministry(db_session, church=church, name="Setup")
    person_a = f.make_person(db_session, church=church, name="Volunteer A")
    person_b = f.make_person(db_session, church=church, name="Volunteer B")
    membership_a = f.make_membership(db_session, person=person_a, ministry=kids)
    membership_b = f.make_membership(db_session, person=person_b, ministry=kids)
    setup_period = f.make_period(db_session, ministry=setup, name="Setup Q4 2026")
    low, high = sorted([membership_a.id, membership_b.id])

    with pytest.raises(IntegrityError) as exc_info:
        with db_session.begin_nested():
            db_session.add(
                MembershipSameDateExclusion(
                    membership_a_id=low, membership_b_id=high,
                    scheduling_period_id=setup_period.id, ministry_id=kids.id,
                )
            )
            db_session.flush()

    assert "fk_same_date_exclusion_period_ministry" in str(exc_info.value.orig)


def test_h6_the_same_pair_may_be_recorded_for_two_different_periods(db_session):
    """The positive case, on the same connection.

    A rule is period-scoped and expires with its period, so the same two
    people may be linked again for the next quarter -- deliberately as a
    separate, separately configured row, never a carry-forward.
    """
    church = f.make_church(db_session)
    ministry = f.make_ministry(db_session, church=church, name="Kids")
    person_a = f.make_person(db_session, church=church, name="Volunteer A")
    person_b = f.make_person(db_session, church=church, name="Volunteer B")
    membership_a = f.make_membership(db_session, person=person_a, ministry=ministry)
    membership_b = f.make_membership(db_session, person=person_b, ministry=ministry)
    q4 = f.make_period(db_session, ministry=ministry, name="Kids Q4 2026")
    q1 = f.make_period(db_session, ministry=ministry, name="Kids Q1 2027")
    low, high = sorted([membership_a.id, membership_b.id])

    db_session.add_all(
        [
            MembershipSameDateExclusion(
                membership_a_id=low, membership_b_id=high,
                scheduling_period_id=q4.id, ministry_id=ministry.id,
            ),
            MembershipSameDateExclusion(
                membership_a_id=low, membership_b_id=high,
                scheduling_period_id=q1.id, ministry_id=ministry.id,
            ),
        ]
    )
    db_session.flush()

    rows = (
        db_session.query(MembershipSameDateExclusion)
        .filter(MembershipSameDateExclusion.membership_a_id == low)
        .all()
    )
    assert sorted(r.scheduling_period_id for r in rows) == sorted([q4.id, q1.id])
