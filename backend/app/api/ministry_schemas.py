"""Transport models for the Admin's church-wide ministry list (Task 79 §4).

Read-only, and there is deliberately no request model in this module: the
endpoint takes no body, and no ministry create, edit or archive operation
exists to model. The domain has no reviewed product rule for one (see the
Task 79 report), and inventing a request shape here would be the first half of
a feature nobody approved.

Hand-built with ``extra="forbid"`` and no ``from_attributes``, like every other
schema module here, so an ORM row can never be serialized by accident.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MinistryHeadResponse",
    "MinistryOverviewResponse",
    "MinistryPeriodResponse",
    "MinistryListResponse",
]


class MinistryHeadResponse(BaseModel):
    """One person who actively leads this ministry.

    Name and id only -- this is an oversight list, not a contact directory. An
    Admin who needs somebody's details opens their Person record, where the
    rules about who may see what already live.
    """

    model_config = ConfigDict(extra="forbid")

    person_id: int
    display_name: str


class MinistryPeriodResponse(BaseModel):
    """The scheduling period this ministry is currently in.

    ``null`` on the ministry response when it has no periods at all, which is
    the honest state of one nobody has configured yet -- reported as an absence
    rather than as an invented blank row.
    """

    model_config = ConfigDict(extra="forbid")

    scheduling_period_id: int
    name: str
    start_date: datetime.date
    end_date: datetime.date
    #: Whether today falls inside it. ``false`` means this is simply the most
    #: recently started period; saying "current" for a quarter that ended
    #: months ago would be wrong by one word in the place it matters.
    is_current: bool
    #: The highest-numbered version of this period's schedule, in **any**
    #: status. ``null`` means no version exists yet. Unlike the authoritative
    #: version used for scheduling decisions, a draft is exactly the
    #: interesting answer here: this column reports progress, not commitment.
    latest_version_number: int | None = None
    latest_version_status: str | None = None


class MinistryOverviewResponse(BaseModel):
    """One ministry, as an overseer sees it.

    Every field is a fact the domain already stores. A ministry with no head,
    no members or no period says so, and nothing here is filled in to keep a
    table looking complete.
    """

    model_config = ConfigDict(extra="forbid")

    ministry_id: int
    name: str
    description: str | None = None
    #: ``null`` while the ministry is active. Non-null means archived -- never
    #: deleted, because past schedules still name it.
    deactivated_at: datetime.datetime | None = None
    heads: list[MinistryHeadResponse] = Field(default_factory=list)
    #: People with an active membership right now, not everyone who has ever
    #: been on it.
    active_member_count: int = 0
    period: MinistryPeriodResponse | None = None


class MinistryListResponse(BaseModel):
    """Every ministry in the church.

    Unpaged, deliberately: a church has tens of ministries, not thousands, and
    the list is bounded by the church itself rather than by a caller-supplied
    limit. ``total`` is present so a client never has to decide whether a short
    list was truncated.
    """

    model_config = ConfigDict(extra="forbid")

    ministries: list[MinistryOverviewResponse] = Field(default_factory=list)
    total: int
