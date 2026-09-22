"""Tests for the shared authorization rules.

Offline: no PostgreSQL, no Neon, no network. No Session is needed at all --
none of these functions reads anything beyond attributes already on the Person
(and, for the ministry-scoped ones, Ministry) objects they are given, so these
are plain unit tests.

**The difference between the two ministry rules is the point of half this
file.** ``require_ministry_reader`` is the oversight rule and admits an Admin
who heads nothing; ``require_ministry_operator`` is the operational-write rule
and does not. The same actor is deliberately put through both in places,
because "an Admin passes one and is refused by the other" is exactly the
distinction that a careless later edit would erase -- and since Task 80 it is
the distinction the whole product's write surface rests on, not just the people
screen Task 79 added it for.

``can_operate_ministry`` is tested against ``require_ministry_operator`` rather
than on its own, because the only property that matters about it is that the
two never disagree: it is the rule a screen renders from, and the rule a write
is judged by, and a divergence between them would show a head a button that
refuses them or hide one that would have worked.
"""

from __future__ import annotations

import datetime

import pytest

from app.models.core import Ministry, MinistryMembership, Person
from app.services import AuthorizationError
from app.services.authorization import (
    can_operate_ministry,
    require_active_admin,
    require_ministry_operator,
    require_ministry_reader,
)


def _person(person_id: int, name: str, *, is_admin: bool = False,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=1)
    person.id = person_id
    person.ministry_memberships = []
    if deactivated:
        person.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    return person


def _membership(
    membership_id: int,
    *,
    person: Person,
    ministry_id: int,
    is_head: bool = False,
    deactivated: bool = False,
) -> MinistryMembership:
    membership = MinistryMembership(
        person_id=person.id, ministry_id=ministry_id, is_ministry_head=is_head,
    )
    membership.id = membership_id
    if deactivated:
        membership.deactivated_at = datetime.datetime(
            2026, 1, 1, tzinfo=datetime.timezone.utc
        )
    person.ministry_memberships.append(membership)
    return membership


def test_active_admin_is_authorized_for_any_ministry():
    admin = _person(1, "Demo Admin", is_admin=True)

    require_ministry_reader(admin, ministry_id=3)  # must not raise


def test_admin_needs_no_membership_at_all():
    admin = _person(1, "Demo Admin", is_admin=True)
    assert admin.ministry_memberships == []

    require_ministry_reader(admin, ministry_id=3)  # must not raise


def test_active_ministry_head_of_the_target_ministry_is_authorized():
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry_id=3, is_head=True)

    require_ministry_reader(head, ministry_id=3)  # must not raise


def test_ministry_head_of_a_different_ministry_is_rejected():
    head = _person(9, "AV Head")
    _membership(201, person=head, ministry_id=4, is_head=True)  # heads AV, not Setup

    with pytest.raises(AuthorizationError):
        require_ministry_reader(head, ministry_id=3)


def test_an_ordinary_member_of_the_target_ministry_is_rejected():
    member = _person(7, "Ada")
    _membership(202, person=member, ministry_id=3, is_head=False)

    with pytest.raises(AuthorizationError):
        require_ministry_reader(member, ministry_id=3)


def test_a_person_with_no_membership_at_all_is_rejected():
    stranger = _person(11, "Stranger")

    with pytest.raises(AuthorizationError):
        require_ministry_reader(stranger, ministry_id=3)


def test_globally_deactivated_actor_is_rejected_even_if_admin():
    departed_admin = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        require_ministry_reader(departed_admin, ministry_id=3)


def test_globally_deactivated_actor_is_rejected_even_if_head():
    departed_head = _person(9, "Departed Head", deactivated=True)
    _membership(200, person=departed_head, ministry_id=3, is_head=True)

    with pytest.raises(AuthorizationError):
        require_ministry_reader(departed_head, ministry_id=3)


def test_deactivated_head_membership_is_rejected():
    """The Person is active; the specific membership carrying head status is not."""
    person = _person(9, "Former Head")
    _membership(200, person=person, ministry_id=3, is_head=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        require_ministry_reader(person, ministry_id=3)


def test_a_person_heading_one_ministry_is_not_authorized_for_another_even_with_a_second_ordinary_membership():
    """Two memberships: head of AV, ordinary member of Setup -- Setup must still
    reject them."""
    person = _person(9, "Multi-ministry Person")
    _membership(200, person=person, ministry_id=4, is_head=True)  # AV head
    _membership(201, person=person, ministry_id=3, is_head=False)  # Setup member

    with pytest.raises(AuthorizationError):
        require_ministry_reader(person, ministry_id=3)


def test_a_person_heading_two_ministries_is_authorized_for_both():
    person = _person(9, "Dual Head")
    _membership(200, person=person, ministry_id=3, is_head=True)
    _membership(201, person=person, ministry_id=4, is_head=True)

    require_ministry_reader(person, ministry_id=3)  # must not raise
    require_ministry_reader(person, ministry_id=4)  # must not raise


# --------------------------------------------------------------------------
# require_active_admin
# --------------------------------------------------------------------------


def test_active_admin_is_authorized():
    admin = _person(1, "Demo Admin", is_admin=True)

    require_active_admin(admin)  # must not raise


def test_active_admin_needs_no_membership_at_all():
    admin = _person(1, "Demo Admin", is_admin=True)
    assert admin.ministry_memberships == []

    require_active_admin(admin)  # must not raise


def test_a_ministry_head_is_not_authorized_by_require_active_admin():
    """The whole point of this stricter helper: heading any number of
    ministries, however actively, confers no admin authority."""
    head = _person(9, "Head Person")
    _membership(200, person=head, ministry_id=3, is_head=True)

    with pytest.raises(AuthorizationError):
        require_active_admin(head)


def test_an_ordinary_member_is_rejected():
    member = _person(7, "Ada")
    _membership(202, person=member, ministry_id=3, is_head=False)

    with pytest.raises(AuthorizationError):
        require_active_admin(member)


def test_a_person_with_no_membership_at_all_is_rejected_by_require_active_admin():
    stranger = _person(11, "Stranger")

    with pytest.raises(AuthorizationError):
        require_active_admin(stranger)


def test_deactivated_admin_is_rejected():
    """is_admin is not cleared on deactivation, so activity is checked too."""
    departed_admin = _person(5, "Departed Admin", is_admin=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        require_active_admin(departed_admin)


# --------------------------------------------------------------------------
# require_ministry_operator -- Task 79's operational-write rule
# --------------------------------------------------------------------------


def test_an_active_head_of_the_ministry_may_operate_it():
    head = _person(20, "AV Head")
    _membership(300, person=head, ministry_id=1, is_head=True)

    require_ministry_operator(head, ministry_id=1)


def test_an_admin_who_heads_nothing_may_not_operate_a_ministry():
    """**The Task 79 change.** Running a ministry belongs to whoever leads it;
    church-wide Admin authority is oversight, and oversight is a read."""
    admin = _person(21, "Admin", is_admin=True)

    with pytest.raises(AuthorizationError) as caught:
        require_ministry_operator(admin, ministry_id=1)
    assert "Ministry Head" in str(caught.value)


def test_the_same_admin_still_passes_the_read_rule():
    """Which is why both functions exist: oversight is a read, and an Elder
    must be able to open every ministry in the church without thereby being
    able to change any of it."""
    admin = _person(22, "Admin", is_admin=True)

    require_ministry_reader(admin, ministry_id=1)
    with pytest.raises(AuthorizationError):
        require_ministry_operator(admin, ministry_id=1)


def test_an_admin_who_also_heads_the_ministry_may_operate_it():
    """And passes through the Head membership, not through ``is_admin`` --
    which is why the next test refuses them for a ministry they do not lead."""
    both = _person(23, "Admin And Head", is_admin=True)
    _membership(301, person=both, ministry_id=1, is_head=True)

    require_ministry_operator(both, ministry_id=1)


def test_an_admin_who_heads_one_ministry_may_not_operate_another():
    both = _person(24, "Admin And AV Head", is_admin=True)
    _membership(302, person=both, ministry_id=1, is_head=True)

    with pytest.raises(AuthorizationError):
        require_ministry_operator(both, ministry_id=2)


def test_a_head_of_a_different_ministry_may_not_operate_this_one():
    head = _person(25, "Kids Head")
    _membership(303, person=head, ministry_id=2, is_head=True)

    with pytest.raises(AuthorizationError):
        require_ministry_operator(head, ministry_id=1)


def test_an_ordinary_member_may_not_operate_their_own_ministry():
    member = _person(26, "Volunteer")
    _membership(304, person=member, ministry_id=1, is_head=False)

    with pytest.raises(AuthorizationError):
        require_ministry_operator(member, ministry_id=1)


def test_a_deactivated_head_may_not_operate_anything():
    """Activity is checked before authority, as everywhere else in this
    module -- deactivation does not clear the flag, it makes it inert."""
    stale = _person(27, "Former Head", deactivated=True)
    _membership(305, person=stale, ministry_id=1, is_head=True)

    with pytest.raises(AuthorizationError):
        require_ministry_operator(stale, ministry_id=1)


def test_a_head_whose_membership_was_deactivated_may_not_operate_it():
    stale = _person(28, "Stood Down")
    _membership(306, person=stale, ministry_id=1, is_head=True, deactivated=True)

    with pytest.raises(AuthorizationError):
        require_ministry_operator(stale, ministry_id=1)


def test_a_person_heading_two_ministries_may_operate_both():
    head = _person(29, "Two Teams")
    _membership(307, person=head, ministry_id=1, is_head=True)
    _membership(308, person=head, ministry_id=2, is_head=True)

    require_ministry_operator(head, ministry_id=1)
    require_ministry_operator(head, ministry_id=2)


def test_there_is_no_emergency_override():
    """A ministry with no active Head cannot be operated by anybody until an
    Admin appoints one -- a governance act with an audit row, never a silent
    bypass (Task 79 §1). Pinned as a test because an override is exactly the
    kind of thing added later "just for emergencies"."""
    import inspect

    source = inspect.getsource(require_ministry_operator)
    assert "is_admin" not in source.split('"""')[2]


# --------------------------------------------------------------------------
# can_operate_ministry -- the operator rule, asked instead of enforced
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda: _person(40, "Volunteer"), id="volunteer"),
        pytest.param(
            lambda: _person(41, "Admin Only", is_admin=True), id="admin-heading-nothing"
        ),
        pytest.param(
            lambda: _with(
                _person(42, "Other Head"), ministry_id=2, is_head=True
            ),
            id="head-of-another-ministry",
        ),
        pytest.param(
            lambda: _with(_person(43, "Member"), ministry_id=1, is_head=False),
            id="ordinary-member",
        ),
        pytest.param(
            lambda: _with(_person(44, "Head"), ministry_id=1, is_head=True),
            id="head",
        ),
        pytest.param(
            lambda: _with(
                _person(45, "Admin And Head", is_admin=True),
                ministry_id=1,
                is_head=True,
            ),
            id="admin-who-also-heads-it",
        ),
        pytest.param(
            lambda: _with(
                _person(46, "Departed Head", deactivated=True),
                ministry_id=1,
                is_head=True,
            ),
            id="deactivated-head",
        ),
        pytest.param(
            lambda: _with(
                _person(47, "Stood Down"),
                ministry_id=1,
                is_head=True,
                deactivated=True,
            ),
            id="deactivated-head-membership",
        ),
    ],
)
def test_can_operate_ministry_always_agrees_with_the_rule_it_reports(build):
    """The only property that matters: the predicate and the gate never
    disagree.

    A screen renders from the predicate and a write is judged by the gate, so a
    divergence would either show a head a button that refuses them or hide one
    that would have worked. Every actor shape the two rules distinguish is put
    through both, rather than each being given its own expected answer -- an
    expected-value table would let both drift together and still pass.
    """
    actor = build()

    allowed = can_operate_ministry(actor, ministry_id=1)
    try:
        require_ministry_operator(actor, ministry_id=1)
    except AuthorizationError:
        assert allowed is False
    else:
        assert allowed is True


def test_can_operate_ministry_is_false_where_the_read_rule_still_passes():
    """The asymmetry Task 80 rests on, asserted directly: an Admin who heads
    nothing may open this ministry and may write nothing in it."""
    admin = _person(48, "Elder", is_admin=True)

    require_ministry_reader(admin, ministry_id=1)  # must not raise
    assert can_operate_ministry(admin, ministry_id=1) is False


def _with(person: Person, *, ministry_id: int, is_head: bool,
          deactivated: bool = False) -> Person:
    """A one-membership Person, so the table above stays readable."""
    _membership(
        400 + person.id,
        person=person,
        ministry_id=ministry_id,
        is_head=is_head,
        deactivated=deactivated,
    )
    return person
