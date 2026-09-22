"""Today's date, in the church's own timezone.

One function, and it exists because two very different questions now need the
same answer and must not disagree about it:

- *"where am I expected?"* -- :mod:`app.api.v1` decides what counts as
  **upcoming**;
- *"how often has this person served?"* -- :mod:`app.services.serving_history`
  decides what counts as **past**.

If those two drew the boundary in different places, an event could be neither
upcoming nor past, or both. They are the same boundary, so it is written once.

**Not UTC, and that is the whole point.** A date is a statement about the
reader's calendar. For a church west of Greenwich a UTC date rolls over
mid-evening, so a service happening today would drop off "upcoming" hours
before it started -- and would be counted as already served by an evening
report. ``church.timezone`` carries an IANA zone name for exactly this
(core §3.2).

**It falls back to UTC rather than failing.** A schedule a few hours out at a
midnight boundary is a far better failure than a 500 on the one screen a
volunteer opens, and the unrecognised zone is logged so somebody can correct
it.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.core import Church

__all__ = ["church_today"]

logger = logging.getLogger(__name__)


def church_today(session: Session, *, church_id: int) -> datetime.date:
    """Today's date in ``church_id``'s configured timezone.

    One small query. Read-only: this function never adds, flushes, commits or
    rolls back, and performs no authorization check -- what "today" is cannot
    be a disclosure, and every caller has already decided who may ask its own
    question.

    :returns: the local date, or the UTC date if the church has no recognisable
        timezone configured.
    """
    zone_name = session.execute(
        select(Church.timezone).where(Church.id == church_id)
    ).scalar_one_or_none()

    if zone_name:
        try:
            return datetime.datetime.now(zoneinfo.ZoneInfo(zone_name)).date()
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "Church %s has an unrecognised timezone; using UTC for today's"
                " date.",
                church_id,
            )

    return datetime.datetime.now(datetime.timezone.utc).date()
