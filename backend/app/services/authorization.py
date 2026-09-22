"""Small, shared authorization checks.

Four rules live here, and they are deliberately not unified into one, because
they answer different questions:

- :func:`require_ministry_operator` -- "may ``actor`` *operate* this ministry?"
  An active Ministry Head **of that specific ministry**, and nobody else.
  Church-wide Admin authority does not satisfy it. **Every operational write
  in the product goes through this**, as of Task 80.
- :func:`require_ministry_reader` -- "may ``actor`` *see* this ministry?" An
  active Admin **or** an active Ministry Head of that specific ministry.
  Oversight is a read, so an Admin who heads nothing passes this and only
  this. (Task 80 renamed the old ``require_ministry_manager`` to this, having
  moved every one of its write callers to the operator rule -- the name said
  "manage" while the rule only ever meant "read or manage", which is exactly
  how it came to guard writes it should not have.)
- :func:`require_active_admin` -- "is ``actor`` an active Admin, full stop?"
  No Ministry Head fallback, ever, regardless of which or how many ministries
  they head.
- :func:`require_people_directory_reader` -- "may ``actor`` see the church-wide
  people directory at all?" Admin, or an active Ministry Head of **any**
  ministry.

:func:`can_operate_ministry` is a fifth entry point but not a fifth rule: it is
:func:`require_ministry_operator` asked as a question instead of as a gate, so
a read endpoint can *report* whether the caller would be allowed to write
without a second, drifting copy of the rule living in the HTTP layer or -- far
worse -- in the browser.

The governance model these express, in one line each
-----------------------------------------------------
- **Church-wide governance and oversight** -- Admin/Elder. The Person record,
  formal church membership status, sign-in linkage, Ministry Head authority,
  and read access to every ministry in the church.
- **Ministry operations** -- the active Head of *that* ministry. Rosters,
  roles, qualifications, availability, limits, rules, schedules, and the
  DRAFT -> REVIEW -> FINALIZED lifecycle.
- **Volunteer** -- their own serving information, which needs no check at all
  because its subject is never a parameter.

**An Admin is an overseer, not automatically every ministry's Head.** That is
why :func:`require_ministry_operator` exists beside
:func:`require_ministry_reader` rather than replacing it: an Admin who is
*also* an active Head of a ministry passes both, because their Head membership
is what authorizes them -- not their Admin flag. An Admin who heads nothing
passes the reader rule and is refused by the operator rule.

**Read authority and write authority are now two different functions
everywhere** (Task 80). Before it, one function answered both questions for
every service written before Task 79, and the answer it gave was the wider
one -- so ``is_admin=True`` alone silently conferred every ministry
configuration write in the product. Narrowing that was a deliberate,
separately reviewed change to who may operate a ministry, which is why it
happened in the lifecycle task rather than as a side effect of adding a people
screen. The two rules are now applied by what the operation *does*: a list or
get takes the reader, anything that writes a row takes the operator.

Each check is extracted here for the same reason: each is used by more than one
service, on the stated understanding (Tasks 14/15/16) that sharing a check is
premature until a second or third genuine use appears, at which point
duplicating it further would let the copies drift. This module stays **four
small functions and one predicate**, not a framework: no policy objects, no
decorators, no permission registry, no RBAC engine.

**Why the directory rule is not a special case of the ministry rules.** It is
tempting to read "a Head may browse the directory" as
``require_ministry_operator`` with the ministry argument left out, but the two
quantify differently: the ministry rules ask about *one named* ministry and the
directory rule asks whether the actor heads *anything at all*. Writing the
directory rule as a loop over the other would need a ministry id it does not
have, and writing the ministry rule in terms of the directory one would hand a
Head of one ministry authority over every other -- which is precisely the
mistake this module exists to make impossible.

**Not every authorization rule in this project lives here.** Some rules are
inherently one service's own -- for instance, which of these functions an
:mod:`app.services.existing_commitment` operation should use depends on
whether the commitment being managed has a source ministry, which is a
decision specific to that domain and stays local to it.
"""

from __future__ import annotations

from app.models.core import Person
from app.services.errors import AuthorizationError

__all__ = [
    "can_operate_ministry",
    "require_active_admin",
    "require_ministry_operator",
    "require_ministry_reader",
    "require_people_directory_reader",
]


def require_ministry_operator(actor: Person, *, ministry_id: int) -> None:
    """Raise unless ``actor`` is an active Ministry Head of ``ministry_id``.

    **The operational-write rule (Task 79 §1, §2, §18; Task 80 §2, §3).**
    Running a ministry -- who is on its roster, what its roles are, when its
    people serve, and whether its schedule is finalized -- belongs to the
    person who actually leads it. Church-wide Admin authority is oversight, and
    oversight is a read.

    Authorized:

    - an active Ministry Head **of this ministry specifically**, read from an
      active ``ministry_membership`` row with ``is_ministry_head=True`` via
      ``Person.ministry_memberships``.

    Not authorized, and this is the whole point:

    - an Admin who does not head this ministry. They may see everything about
      it (:func:`require_ministry_reader` guards the oversight reads) and may
      appoint somebody to lead it, but they may not quietly reorganize a team
      nobody asked them to run, and they may not publish its schedule.
    - a Head of a *different* ministry, however active.

    **An Admin who is also an active Head of this ministry passes**, and passes
    through the ordinary membership loop below rather than through a special
    case. Their authority here comes from leading the ministry, not from the
    Admin flag -- which is exactly the product rule, expressed as the absence
    of an ``if actor.is_admin`` branch.

    **There is deliberately no emergency override.** A ministry with no active
    Head cannot be operated by anybody until an Admin appoints one, which is a
    governance act with an audit row rather than a silent bypass (Task 79 §1).

    :raises AuthorizationError: ``actor`` is deactivated, or is not an active
        Ministry Head of ``ministry_id``.
    """
    if actor.deactivated_at is not None:
        raise AuthorizationError("a deactivated Person may not perform this action")

    if _heads_ministry(actor, ministry_id=ministry_id):
        return

    raise AuthorizationError(
        "only an active Ministry Head of this ministry may perform this"
        " action; church-wide Admin authority is oversight, not operation"
    )


def can_operate_ministry(actor: Person, *, ministry_id: int) -> bool:
    """Would :func:`require_ministry_operator` allow ``actor`` here?

    **The same rule, asked rather than enforced**, so that a read endpoint can
    tell its caller which operational controls are real for them. It exists
    for exactly one reason: without it, the answer would be re-derived
    somewhere else -- in a route, or in the browser from ``is_admin`` -- and a
    second copy of an authorization rule is a copy that will eventually
    disagree with the first.

    **It is not a security boundary and must never be treated as one.** Nothing
    is authorized by calling this. Every write still calls
    :func:`require_ministry_operator` on its own behalf, in the service, on
    every request; a caller who ignores a ``False`` here gets a 403 from the
    domain exactly as if they had never asked. What this buys is a UI that
    tells the truth -- no button that leads only to a refusal.
    """
    if actor.deactivated_at is not None:
        return False
    return _heads_ministry(actor, ministry_id=ministry_id)


def require_ministry_reader(actor: Person, *, ministry_id: int) -> None:
    """Raise unless ``actor`` may *read* ``ministry_id``'s configuration.

    **The oversight rule** (core §4.3.1). Reading a ministry is not operating
    it, so this one admits the Admin who heads nothing -- an Elder must be able
    to open any ministry's roster, rules and schedule and see exactly what a
    Head sees, without thereby acquiring the ability to change any of it.

    Authorized:

    - an active Admin (``actor.is_admin``) -- for any ministry, no membership
      required;
    - an active Ministry Head **of this ministry specifically** -- read from
      an active ``ministry_membership`` row with ``is_ministry_head=True``,
      via ``Person.ministry_memberships`` rather than a fresh query. In
      production this lazy-loads through the bound session on first access if
      not already loaded; nothing here issues SQL of its own. A Ministry Head
      of a *different* ministry is not authorized, however active.

    **This function guards reads only.** That is Task 80's correction and the
    reason it was renamed: under its old name, ``require_ministry_manager``, it
    guarded every configuration write in the product written before Task 79,
    and an Admin who led no ministry could therefore rewrite any ministry's
    roles, availability, limits and schedules. Those call sites now take
    :func:`require_ministry_operator`. If a new write appears to want this
    check, the write is in the wrong place, not the rule.

    :raises AuthorizationError: ``actor`` is deactivated, or is neither an
        active Admin nor an active Ministry Head of ``ministry_id``.
    """
    if actor.deactivated_at is not None:
        raise AuthorizationError("a deactivated Person may not perform this action")
    if actor.is_admin:
        return

    if _heads_ministry(actor, ministry_id=ministry_id):
        return

    raise AuthorizationError(
        "only an Admin, or an active Ministry Head of this ministry,"
        " may view this ministry"
    )


def require_active_admin(actor: Person) -> None:
    """Raise unless ``actor`` is an active Admin.

    Deliberately no Ministry Head fallback, unlike
    :func:`require_ministry_reader` -- some approved rules are Admin-only
    precisely because heading a ministry must confer no say in them.
    Extracted from :mod:`app.services.ministry_authority` (Task 13), which
    carried its own private copy, once :mod:`app.services.existing_commitment`
    (Task 18) needed the identical check for a church-wide commitment with no
    source ministry to derive management from.

    **[APPROVED]** Granting or revoking Ministry Head authority remains
    Admin-only (core §4.3) -- this extraction changes nothing about who is
    authorized, only where the check is written.

    :raises AuthorizationError: ``actor`` is not an Admin, or is deactivated.
    """
    if not actor.is_admin:
        raise AuthorizationError("only an Admin may perform this action")
    if actor.deactivated_at is not None:
        raise AuthorizationError("a deactivated Person may not act as an Admin")


def require_people_directory_reader(actor: Person) -> None:
    """Raise unless ``actor`` may read the church-wide people directory.

    Authorized:

    - an active Admin, and
    - an active Ministry Head of **any** ministry -- read from an active
      ``ministry_membership`` row carrying ``is_ministry_head``, through
      ``Person.ministry_memberships``, exactly as
      :func:`require_ministry_reader` does. Which ministry does not matter
      here; that a ministry is led at all does.

    **A normal volunteer is refused, and that is the whole point** (Task 79).
    Listing or searching every person in the church is a real disclosure --
    it is the church's membership roll -- and somebody who serves on a rota
    has no need of it. They reach their own schedule through
    ``/api/v1/me/schedule``, whose subject is not a parameter and which
    therefore needs no check of any kind.

    **Reading is church-wide; writing is not.** This function deliberately
    authorizes *only* the directory read. Every mutation past it re-checks with
    :func:`require_ministry_operator` against the specific ministry being
    changed, or with :func:`require_active_admin` for the church-wide Person
    record -- so a Head who can see the whole directory still cannot touch
    another ministry's memberships. Conflating the two would turn "may look up
    a person to add to my team" into "may manage everybody".

    :raises AuthorizationError: ``actor`` is deactivated, or is neither an
        active Admin nor an active Ministry Head of anything.
    """
    if actor.deactivated_at is not None:
        raise AuthorizationError("a deactivated Person may not perform this action")
    if actor.is_admin:
        return

    for membership in actor.ministry_memberships:
        if membership.deactivated_at is None and membership.is_ministry_head:
            return

    raise AuthorizationError(
        "only an Admin, or an active Ministry Head, may view the people directory"
    )


def _heads_ministry(actor: Person, *, ministry_id: int) -> bool:
    """Does ``actor`` hold an active head membership of ``ministry_id``?

    The one fact three of the rules above share, written once so the operator
    rule, the reader rule's fallback and :func:`can_operate_ministry` can never
    answer it differently. It says nothing about ``is_admin`` and nothing about
    whether the actor is deactivated -- both are the callers' own decisions,
    and they make them differently on purpose.
    """
    for membership in actor.ministry_memberships:
        if (
            membership.ministry_id == ministry_id
            and membership.deactivated_at is None
            and membership.is_ministry_head
        ):
            return True
    return False
