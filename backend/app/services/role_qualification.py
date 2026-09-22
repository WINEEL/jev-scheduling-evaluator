"""Recording Ministry Head decisions about role qualification.

Implements the accepted design in ``docs/architecture/core-data-model.md`` §8
and the authorization rules in §4.2--§4.3, and records each decision as an
audit event per ``docs/architecture/audit-event-data-model.md`` §20 ("RoleQualification
boundary"), §22.2. Section numbers below refer to the core document unless
stated.

**[APPROVED] Three states, and the absence of a row is one of them** (§8):

- no row                       -> never assessed
- row, ``is_qualified=False``  -> assessed and explicitly not approved
- row, ``is_qualified=True``   -> approved

Changing "no row" to ``False`` is therefore a real decision -- the first
explicit assessment -- and creates a row exactly as changing it to ``True``
does. Nothing here ever treats absence as a stand-in for ``False``.

**[APPROVED] This is the first Ministry-Head-scoped service** (§4.2--§4.3): an
actor may record a decision here only as an active Ministry Head *of the
ministry the membership and role both belong to*. Unlike Ministry Head
authority itself (:mod:`app.services.ministry_authority`), which only an Admin
may grant, day-to-day qualification decisions are exactly the kind of
ministry-scoped judgment call §4.3 gives to the head of that ministry -- and
§4.3.1 adds the converse Task 80 enforced: an Admin who does not head this
ministry may *read* its qualifications
(:func:`app.services.authorization.require_ministry_reader`) and may decide
none of them (:func:`app.services.authorization.require_ministry_operator`).

Training/shadow state is out of scope here, as it is in the model itself (core
§8): training is a person's progress, qualification is a head's authorization
decision, and they get separate tables when training arrives.

**[Task 55] Listing is read-only and never invents a fourth state.** A
Ministry Head choosing "who may I assign to this role?" needs to see every
member of the ministry against exactly the same three-state reading the write
side uses -- :func:`list_role_qualifications` reports ``is_qualified`` as
``None`` for "never assessed", never a stored default. **There is no "clear
back to no row" operation, on either side.** The model's own docstring is
explicit that revoking is an UPDATE, never a DELETE, precisely so the row keeps
a stable id for the audit history to reference; once a decision exists, only
:func:`set_role_qualification`'s ``True``/``False`` setter applies to it, and
this module adds no way to delete a ``RoleQualification`` row.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, MinistryRole, Person, RoleQualification
from app.services.audit import (
    ACTION_QUALIFICATION_APPROVED,
    ACTION_QUALIFICATION_REVOKED,
    record_audit_event,
)
from app.services.authorization import (
    require_ministry_operator,
    require_ministry_reader,
)
from app.services.errors import InvalidOperationError

#: [REVIEWED] audit §6.1's initial vocabulary already carries the pair this
#: service needs, so no new action name is introduced. ``QUALIFICATION_REVOKED``
#: is used for every result of ``False`` -- both an actual revocation of a prior
#: ``True`` and a first-time explicit "not qualified" -- and the two are told
#: apart by the summary sentence (§7.3), not by a third action name. See
#: :func:`_summary` and the module-level note in :func:`set_role_qualification`.
#:
#: ``ACTION_QUALIFICATION_APPROVED`` / ``ACTION_QUALIFICATION_REVOKED`` are
#: defined centrally in :mod:`app.services.audit`, imported above rather than
#: redefined here -- matching how Task 13 and Task 15 already keep the action
#: vocabulary in one place. Still re-exported under this module's own name
#: (nothing further to do: an imported name is already an attribute of the
#: importing module), since existing callers and tests import them from here.

#: The audited table these operations target (audit §5.2).
_TARGET_TABLE = "role_qualification"


@dataclass(frozen=True, slots=True)
class MembershipQualification:
    """One membership of the role's ministry, and its standing decision for
    that role, if any.

    ``is_qualified`` is ``None`` for "never assessed" (§8) -- never a stored
    default, and never collapsed into ``False`` the way the *solver's*
    eligibility reading does (:mod:`app.services.scheduling_input_builder`
    folds both into "not eligible" for its own different purpose; a head
    choosing who to assess needs to see the real three-way distinction).

    ``membership_deactivated_at`` and ``person_deactivated_at`` are reported
    separately, exactly as :class:`MinistryMembership` and
    :class:`~app.models.core.Person` keep them separately (core §6): a person
    can leave one ministry without leaving the church, and the two facts mean
    different things to a head deciding whether a decision could even take
    effect.
    """

    ministry_membership_id: int
    person_id: int
    person_display_name: str
    membership_deactivated_at: datetime.datetime | None
    person_deactivated_at: datetime.datetime | None
    #: ``None`` = never assessed; ``False`` = explicitly not qualified;
    #: ``True`` = qualified. Never a fourth value.
    is_qualified: bool | None
    #: ``None`` exactly when ``is_qualified`` is ``None`` -- there is nothing
    #: to have decided yet.
    decided_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class RoleQualifications:
    """One role's whole qualification picture, for the head deciding it."""

    ministry_role_id: int
    role_name: str
    ministry_id: int
    memberships: tuple[MembershipQualification, ...]


def list_role_qualifications(
    session: Session,
    *,
    actor: Person,
    role: MinistryRole,
    include_inactive: bool = False,
) -> RoleQualifications:
    """``role``'s ministry's memberships, each with its qualification state
    for ``role``.

    **Read-only.** Ordered by ``lower(person.display_name)`` then
    ``ministry_membership_id``, so two reads of unchanged data return the
    same list rather than whatever order PostgreSQL finds convenient.

    ``include_inactive=False`` (the default) returns only active memberships
    of currently active people -- the ordinary case of "who could I assign
    right now?". Passing ``True`` additionally returns deactivated
    memberships and memberships of deactivated people, so a head can still
    see (and, per :func:`set_role_qualification`'s own state-repair path,
    still revoke) a standing decision against someone who has since left.

    :raises AuthorizationError: the actor is not an active Admin and not an
        active Ministry Head of ``role.ministry_id``.
    """
    require_ministry_reader(actor, ministry_id=role.ministry_id)

    rows = session.execute(
        _role_qualifications_statement(
            role.id, role.ministry_id, include_inactive=include_inactive
        )
    ).all()
    return RoleQualifications(
        ministry_role_id=role.id,
        role_name=role.name,
        ministry_id=role.ministry_id,
        memberships=tuple(
            MembershipQualification(
                ministry_membership_id=row.ministry_membership_id,
                person_id=row.person_id,
                person_display_name=row.person_display_name,
                membership_deactivated_at=row.membership_deactivated_at,
                person_deactivated_at=row.person_deactivated_at,
                is_qualified=row.is_qualified,
                decided_at=row.decided_at,
            )
            for row in rows
        ),
    )


def set_role_qualification(
    session: Session,
    *,
    actor: Person,
    membership: MinistryMembership,
    role: MinistryRole,
    is_qualified: bool,
    reason: str | None = None,
) -> RoleQualification:
    """Record ``actor``'s qualification decision for ``membership`` and ``role``.

    One explicit setter rather than separate approve/revoke functions: the
    domain fact is a single boolean decision (§8), and a setter states that
    directly rather than asking the caller to know which of two functions
    "approve" and "revoke" corresponds to "no row yet, decide False" -- a case
    neither verb describes well.

    **Idempotent, with one deliberate exception.** If a decision already exists
    with the requested value, it is normally returned unchanged: no
    ``AuditEvent``, and ``decided_at`` / ``decided_by_person_id`` are left
    exactly as they are, since rewriting them would fabricate a new decision
    moment for a decision that did not change. **No row is never idempotent
    with a requested ``False``** -- that is the first explicit negative
    assessment, and it creates a row and an audit event exactly as a first
    ``True`` does.

    The exception: **a request whose desired state is ``True`` is never allowed
    to short-circuit on idempotency before its target has been proven active**
    -- see the activity rule immediately below. An existing ``True`` decision
    does not make a repeated ``True`` request automatically valid once the
    membership, person, or role it names has since been deactivated; it is
    inconsistent state to be reported, not a success to be confirmed silently.
    This mirrors the Task 13.1 correction to
    :func:`~app.services.ministry_authority.grant_ministry_head`, which the
    original revision of this function did not carry over -- see the decision
    register in the Task 14.1 correction report for why that was a real gap
    rather than a stylistic choice.

    **Target activity, and the deliberate asymmetry (§8, §13).** A decision that
    makes the resulting state ``True`` -- a brand-new row, a first explicit
    ``False``, turning an existing ``False`` into ``True``, or simply repeating
    an existing ``True`` -- is a genuine grant (or standing re-confirmation) of
    qualification, so it **always** requires the membership, the person, and
    the role to all be currently active, checked before anything else about the
    request is decided. The single exception is changing an **existing
    `True`** to ``False``: that only ever *removes* standing, so it is
    permitted even against a membership, person, or role that has since been
    deactivated -- it is the state-repair path, exactly as
    :func:`~app.services.ministry_authority.revoke_ministry_head` is permissive
    for the same reason. Repeating an existing ``False`` is likewise exempt:
    nothing changes, so there is nothing to validate against, and requiring
    activity there would make it impossible to merely re-confirm an existing
    negative decision once its target has become inactive -- the opposite of
    what state repair is for. A brand-new ``False`` row against an inactive
    target is **not** exempt: nothing in the accepted model distinguishes
    "never assessed, decide False" from "never assessed, decide True" as a
    matter of urgency the way an existing decision's confirmation or removal
    is distinguished, so both require an active target. This is the preferred
    rule stated in the Task 14 brief, implemented as written rather than as an
    independent judgment call.

    The flag change and its audit row are added to the same ``session`` and are
    written by the caller's commit, so they land together or not at all
    (:mod:`app.services`). **This function flushes exactly once, and only when
    it creates a brand-new** :class:`RoleQualification` **row** -- never on an
    update, and never on the idempotent no-op path. It never commits or rolls
    back. See the note below on why this is the first service in the project
    that legitimately needs to.

    :raises AuthorizationError: the actor is not an active Ministry Head
        of the membership's and role's ministry.
    :raises InvalidOperationError: the membership and role belong to different
        ministries; the requested decision would grant, renew, or merely
        re-confirm standing (``is_qualified=True``) against a deactivated
        membership, person, or role; the request would newly assess
        ``is_qualified=False`` against one; or ``reason`` was supplied but
        blank.
    """
    _require_same_ministry(membership, role)
    require_ministry_operator(actor, ministry_id=membership.ministry_id)
    reason = _validate_optional_reason(reason)
    if actor.id is None:
        # decided_by_person_id is a NOT NULL foreign key written directly on
        # this row (below), independent of record_audit_event's own actor
        # check, so it is validated here too rather than left to surface only
        # once the audit helper runs.
        raise InvalidOperationError("actor Person must be persisted (id is None)")

    existing = _find_existing_qualification(session, membership, role)

    if is_qualified:
        # Every path that ends in True -- new row, False -> True, or a repeat
        # of an existing True -- is a grant (or re-confirmation) of standing,
        # and must be proven active *before* the idempotency check below can
        # short-circuit it. Checking activity first is what makes a stale
        # "already True" unable to mask a target that has since been
        # deactivated (Task 14.1 correction, mirroring
        # grant_ministry_head's Task 13.1 correction).
        _require_active_target(membership, role)
        if existing is not None and existing.is_qualified is True:
            return existing
    else:
        if existing is not None and existing.is_qualified is False:
            # Idempotent no-op. No activity check: nothing changes, so there
            # is nothing to validate against, and requiring activity here
            # would make it impossible to merely re-confirm an existing
            # negative decision once its target has become inactive.
            return existing
        if existing is None:
            # No prior row: a brand-new negative assessment, held to the same
            # active-target requirement as a new positive one (see the
            # asymmetry documented above). An existing True being changed to
            # False is the one case that reaches here needing no check at
            # all -- that is the state-repair path.
            _require_active_target(membership, role)

    now = datetime.datetime.now(datetime.timezone.utc)
    before_value = existing.is_qualified if existing is not None else None

    if existing is None:
        qualification = RoleQualification(
            ministry_membership_id=membership.id,
            ministry_role_id=role.id,
            # Shared by both composite foreign keys on the model; taken from
            # the membership, which target-integrity above has already proven
            # agrees with the role (core §7.2).
            ministry_id=membership.ministry_id,
            is_qualified=is_qualified,
            decided_at=now,
            decided_by_person_id=actor.id,
        )
        session.add(qualification)
        # The audit row below needs a real target_id, and a new identity
        # bigint does not exist until the INSERT actually runs. This is the
        # minimum flush that makes that true -- scoped to the one pending row
        # that needs it, not a blanket session.flush(). See app.services'
        # module docstring for why a flush here does not compromise the
        # caller-owned transaction: it sends the pending INSERT within the
        # caller's already-open transaction and commits nothing.
        session.flush([qualification])
    else:
        existing.is_qualified = is_qualified
        existing.decided_at = now
        existing.decided_by_person_id = actor.id
        qualification = existing

    record_audit_event(
        session,
        actor=actor,
        action=(
            ACTION_QUALIFICATION_APPROVED
            if is_qualified
            else ACTION_QUALIFICATION_REVOKED
        ),
        target_table=_TARGET_TABLE,
        target_id=qualification.id,
        ministry_id=membership.ministry_id,
        summary=_summary(is_qualified=is_qualified, membership=membership, role=role),
        reason=reason,
        # Only the one changed business field (audit §7.2). ``None`` on the
        # before side is not a stand-in for False -- it is the audit payload's
        # honest expression of "no prior assessment existed" (core §8).
        before_values={"is_qualified": before_value},
        after_values={"is_qualified": is_qualified},
    )
    return qualification


def _require_same_ministry(membership: MinistryMembership, role: MinistryRole) -> None:
    """The database enforces this too, via the composite foreign keys routed
    through ``role_qualification.ministry_id`` (core §7.2). Checking here turns
    an ``IntegrityError`` at commit -- far from its cause, and aborting the
    caller's whole transaction -- into a meaningful domain error raised before
    anything is mutated.
    """
    if membership.ministry_id != role.ministry_id:
        raise InvalidOperationError(
            "membership and role must belong to the same ministry"
        )


def _require_active_target(membership: MinistryMembership, role: MinistryRole) -> None:
    """Every party to a new grant of standing must currently be active.

    Not called on the state-repair path (an existing ``True`` becoming
    ``False``) -- see the asymmetry documented on
    :func:`set_role_qualification`.
    """
    if membership.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot record a new qualification decision for a deactivated membership"
        )
    if membership.person.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot record a new qualification decision for a deactivated person"
        )
    if role.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot record a new qualification decision for a deactivated role"
        )


def _validate_optional_reason(reason: str | None) -> str | None:
    """A reason is optional here, but blank is not a reason.

    [REVIEWED] audit §9: no new universal reason requirement is invented for
    qualification decisions; a reason may be supplied and is recorded when it
    is.
    """
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


def _role_qualifications_statement(
    ministry_role_id: int, ministry_id: int, *, include_inactive: bool
) -> Select:
    """Every membership of ``ministry_id``, left-joined to any
    ``RoleQualification`` it has for ``ministry_role_id``.

    A ``LEFT OUTER JOIN``, not an inner one: a membership never assessed for
    this role is exactly the case this listing exists to show, and an inner
    join would silently drop it. Joined to ``Person`` for the display name and
    that person's own ``deactivated_at`` -- two different activity facts (core
    §6), neither derivable from the other.
    """
    stmt = (
        select(
            MinistryMembership.id.label("ministry_membership_id"),
            Person.id.label("person_id"),
            Person.display_name.label("person_display_name"),
            MinistryMembership.deactivated_at.label("membership_deactivated_at"),
            Person.deactivated_at.label("person_deactivated_at"),
            RoleQualification.is_qualified.label("is_qualified"),
            RoleQualification.decided_at.label("decided_at"),
        )
        .select_from(MinistryMembership)
        .join(Person, Person.id == MinistryMembership.person_id)
        .outerjoin(
            RoleQualification,
            (RoleQualification.ministry_membership_id == MinistryMembership.id)
            & (RoleQualification.ministry_role_id == ministry_role_id),
        )
        .where(MinistryMembership.ministry_id == ministry_id)
    )
    if not include_inactive:
        stmt = stmt.where(
            MinistryMembership.deactivated_at.is_(None),
            Person.deactivated_at.is_(None),
        )
    return stmt.order_by(
        func.lower(Person.display_name), MinistryMembership.id
    )


def _qualification_lookup_statement(
    membership_id: int, role_id: int
) -> Select[tuple[RoleQualification]]:
    """The query, split out from its execution so it can be tested without a
    database: a compiled ``Select`` is inspectable on its own (see
    ``tests/test_services_role_qualification.py``).
    """
    return select(RoleQualification).where(
        RoleQualification.ministry_membership_id == membership_id,
        RoleQualification.ministry_role_id == role_id,
    )


def _find_existing_qualification(
    session: Session, membership: MinistryMembership, role: MinistryRole
) -> RoleQualification | None:
    """The standing decision for this (membership, role) pair, if one exists.

    Queried on the model's own integrity columns -- ``ministry_membership_id``
    and ``ministry_role_id``, exactly the pair
    ``uq_role_qualification_membership_role`` is built from (core §7) -- via a
    plain SQLAlchemy ``select()``, no repository abstraction. Absence is a real
    domain state, "never assessed" (core §8), and is returned as ``None``
    rather than assumed to mean ``False``.
    """
    stmt = _qualification_lookup_statement(membership.id, role.id)
    return session.execute(stmt).scalar_one_or_none()


def _summary(
    *, is_qualified: bool, membership: MinistryMembership, role: MinistryRole
) -> str:
    """Names the person, the role and the ministry (audit §7.3, §14): the audit
    row carries no ``target_label``, so the summary is what stays readable once
    the qualification row's context has to be reconstructed from history alone.

    Deliberately one phrasing for every ``False`` result, whether it reverses an
    existing ``True`` or is the first assessment ever made: "marked ... as not
    qualified" is accurate either way and never implies a prior approval that
    may not have existed.
    """
    person_name = membership.person.display_name
    role_name = role.name
    ministry_name = membership.ministry.name
    if is_qualified:
        return f"Approved {person_name} for {role_name} in {ministry_name}"
    return f"Marked {person_name} as not qualified for {role_name} in {ministry_name}"
