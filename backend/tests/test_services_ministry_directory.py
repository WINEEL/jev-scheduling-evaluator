"""Church-wide ministry oversight service tests (Task 79 §4).

Offline: no PostgreSQL, no Neon, no network. The same split the rest of this
suite uses -- authorization against a real, unbound Session that never gets far
enough to issue SQL, and the queries themselves compiled and inspected.

The rule under test is a narrow one and it is easy to get subtly wrong: **an
Admin sees every ministry in their own church, and nobody else sees this list
at all**. A ministry head reaches the ministries they lead through
``/api/v1/me``; a church-wide inventory is a different thing.

Every ministry and person name here is fictional.
"""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.core import Ministry, MinistryMembership, Person
from app.services.errors import AuthorizationError
from app.services.ministry_directory import (
    MinistryOverview,
    MinistryPeriodSummary,
    _ministries_statement,
    list_church_ministries,
)

UTC = datetime.timezone.utc
_THEN = datetime.datetime(2026, 1, 1, tzinfo=UTC)


class OversightSession(Session):
    """A real, unbound Session that answers with no rows.

    Every authorization test here is refused before the first query, and the
    "an Admin may call it" test only needs the call to return. What the
    queries *mean* against real rows is
    ``tests/integration/test_pg_ministry_oversight.py``'s job.
    """

    def __init__(self) -> None:
        super().__init__()
        self.executed_sql: list[str] = []

    def execute(self, statement, *args, **kwargs):
        self.executed_sql.append(str(statement))
        return _Result()

    def commit(self):  # pragma: no cover - must never run
        raise AssertionError("a read-only service must never commit")

    def rollback(self):  # pragma: no cover - must never run
        raise AssertionError("a read-only service must never roll back")


class _Result:
    def all(self):
        return []

    def scalar_one_or_none(self):
        return "UTC"


@pytest.fixture
def session() -> OversightSession:
    return OversightSession()


def _person(
    person_id: int,
    name: str,
    *,
    is_admin: bool = False,
    deactivated: bool = False,
    heads: tuple[int, ...] = (),
    church_id: int = 1,
) -> Person:
    person = Person(display_name=name, is_admin=is_admin, church_id=church_id)
    person.id = person_id
    person.deactivated_at = _THEN if deactivated else None
    memberships = []
    for index, ministry_id in enumerate(heads):
        membership = MinistryMembership(
            person_id=person_id, ministry_id=ministry_id, is_ministry_head=True
        )
        membership.id = 9000 + index
        membership.deactivated_at = None
        memberships.append(membership)
    person.ministry_memberships = memberships
    return person


class TestWhoMaySeeEveryMinistry:
    """Church-wide oversight is Admin's, and only Admin's."""

    def test_an_admin_may_list_them(self, session):
        assert list_church_ministries(session, actor=_person(1, "Admin", is_admin=True)) == ()

    def test_a_ministry_head_may_not(self, session):
        """**Not a special case of "they lead one".** A head reaching this list
        would be reading a church-wide inventory on the strength of running one
        team; the ministries they actually lead reach them through
        ``/api/v1/me``."""
        with pytest.raises(AuthorizationError):
            list_church_ministries(session, actor=_person(2, "Head", heads=(100,)))

    def test_a_head_of_several_ministries_still_may_not(self, session):
        with pytest.raises(AuthorizationError):
            list_church_ministries(
                session, actor=_person(3, "Busy Head", heads=(100, 200, 300))
            )

    def test_a_volunteer_may_not(self, session):
        with pytest.raises(AuthorizationError):
            list_church_ministries(session, actor=_person(4, "Volunteer"))

    def test_a_deactivated_admin_may_not(self, session):
        with pytest.raises(AuthorizationError):
            list_church_ministries(
                session,
                actor=_person(5, "Former Admin", is_admin=True, deactivated=True),
            )

    def test_a_refusal_issues_no_query(self, session):
        """Refused before the church's ministries are read, so an unauthorized
        caller learns nothing -- not even how long the answer took."""
        with pytest.raises(AuthorizationError):
            list_church_ministries(session, actor=_person(6, "Volunteer"))
        assert session.executed_sql == []


class TestChurchScoping:
    """The one parameter that is deliberately not a parameter."""

    def test_the_church_comes_from_the_actor(self, session):
        actor = _person(7, "Admin", is_admin=True, church_id=42)
        list_church_ministries(session, actor=actor)
        assert any("ministry.church_id" in sql for sql in session.executed_sql)

    def test_there_is_no_church_parameter_to_supply(self):
        """An Admin of one church must have no way to name another's id --
        V1 is single-church (core §7), and the only safe number of ways to
        say "which church" is zero."""
        import inspect

        assert "church_id" not in inspect.signature(list_church_ministries).parameters


class TestMinistriesQuery:
    def test_it_is_ordered_for_a_stable_reading(self):
        sql = str(
            _ministries_statement(church_id=1, include_inactive=True).compile(
                dialect=postgresql.dialect()
            )
        )
        assert "ORDER BY lower(ministry.name), ministry.id" in sql

    def test_archived_ministries_are_included_by_default(self):
        """An archived ministry is part of what an overseer oversees, and
        omitting it silently would look like deletion."""
        sql = str(
            _ministries_statement(church_id=1, include_inactive=True).compile(
                dialect=postgresql.dialect()
            )
        )
        assert "deactivated_at IS NULL" not in sql

    def test_they_can_be_excluded_on_request(self):
        sql = str(
            _ministries_statement(church_id=1, include_inactive=False).compile(
                dialect=postgresql.dialect()
            )
        )
        assert "ministry.deactivated_at IS NULL" in sql


class TestOverviewValueObject:
    def test_a_ministry_with_nothing_configured_says_so(self):
        """Nothing is invented to fill a column."""
        overview = MinistryOverview(
            ministry_id=1, name="New Ministry", description=None,
            deactivated_at=None,
        )
        assert overview.heads == ()
        assert overview.active_member_count == 0
        assert overview.period is None
        assert overview.is_active is True

    def test_an_archived_ministry_is_not_active(self):
        overview = MinistryOverview(
            ministry_id=1, name="Archived", description=None, deactivated_at=_THEN
        )
        assert overview.is_active is False

    def test_a_period_between_quarters_is_not_current(self):
        """The list still shows the quarter that just finished, which is what
        somebody looking at it wants -- but it does not call it current."""
        period = MinistryPeriodSummary(
            scheduling_period_id=1, name="Q3", is_current=False,
            start_date=datetime.date(2026, 7, 1),
            end_date=datetime.date(2026, 9, 30),
        )
        assert period.is_current is False
        assert period.latest_version_status is None
