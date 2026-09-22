"""People and membership management endpoints (Task 79).

``GET    /api/v1/people``
``POST   /api/v1/people``
``GET    /api/v1/people/{person_id}``
``PATCH  /api/v1/people/{person_id}``
``PUT    /api/v1/people/{person_id}/auth-link``
``PUT    /api/v1/people/{person_id}/church-membership-status``
``PUT    /api/v1/people/{person_id}/ministry-head``
``POST   /api/v1/people/{person_id}/deactivate``
``POST   /api/v1/people/{person_id}/reactivate``
``GET    /api/v1/people/{person_id}/memberships``
``POST   /api/v1/people/{person_id}/memberships``
``PATCH  /api/v1/ministry-memberships/{ministry_membership_id}``
``POST   /api/v1/ministry-memberships/{ministry_membership_id}/remove``

The people-management surface the product always described and never had: until
this task a Person could only be created by an import script and a membership
only by hand in psql.

**Every route stays thin**, exactly as :mod:`app.api.routes_ministry_roles`
does. Resolve the actor, resolve the URL's resource, call the service, map the
result. **No authorization decision is made in this file** -- not one ``if
actor.is_admin``. Who may read the directory, who may edit a Person, who may
touch which ministry's memberships and who may appoint a head are all decided
inside :mod:`app.services.person_directory`,
:mod:`app.services.ministry_membership` and
:mod:`app.services.ministry_authority`. A second client, a script or a future
endpoint therefore gets the same answers without re-deriving them, and the
frontend's hidden navigation is a courtesy rather than the control.

**Three levels of authority reach these routes** (core §4.3.1):

- **church-wide governance and oversight** -- an active Admin: the Person
  record, church membership status, the sign-in link, deactivation, and
  Ministry Head authority;
- **ministry operations** -- the active Head of *that* ministry: its roster.
  An Admin who does not lead it is refused, and an Admin who does passes
  through their Head membership rather than through ``is_admin``;
- **the directory read** -- an Admin, or an active Head of anything, because
  that is how somebody finds an existing Person instead of creating a second
  copy of one. A volunteer gets 403 from every route here.

**Cross-ministry management is refused by authorization, not by URL shape** --
the same reasoning :mod:`app.api.routes_ministry_roles` records. The membership
endpoints take only a membership id, with no
``/ministries/{id}/memberships/{id}`` nesting to keep two ids in agreement,
because the service's ``require_ministry_operator`` check scoped to the
membership's own ``ministry_id`` already makes a Head of a different ministry's
request a 403 whichever membership id they name.

**404 before 403, and why that is safe here.** ``_require_person`` and
``_require_membership`` answer 404 for a row that does not exist before the
service is reached, so an unauthorized caller can learn that person id 812
exists. That is a disclosure this endpoint has already made by design: every
caller who gets past :func:`get_current_actor` and the directory check may list
every person anyway, and a volunteer is refused the whole resource. The
identity boundary itself still refuses first -- an unauthenticated request is
401 from :func:`get_current_actor` before any of this runs.

No route commits: :func:`app.api.dependencies.get_session` is the transaction
boundary, so a successful request commits once on the way out and any failure
rolls the whole thing back -- including, for a create-with-ministry, both the
Person and the membership.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.people_schemas import (
    AddMembershipRequest,
    AuthLinkRequest,
    ChurchMembershipStatusRequest,
    CreatePersonRequest,
    DirectoryPersonResponse,
    MinistryHeadAuthorityRequest,
    MinistryServingCountResponse,
    PeopleDirectoryResponse,
    PersonMembershipResponse,
    PersonMembershipsResponse,
    PersonResponse,
    ReasonRequest,
    ServingConflictResponse,
    ServingSummaryResponse,
    UpdateMembershipRequest,
    UpdatePersonRequest,
)
from app.models.core import Ministry, MinistryMembership, Person
from app.services.ministry_membership import (
    add_person_to_ministry,
    remove_person_from_ministry,
    set_ministry_head_authority,
    update_membership,
)
from app.services.person_directory import (
    DEFAULT_DIRECTORY_LIMIT,
    MAX_DIRECTORY_LIMIT,
    DirectoryEntry,
    DirectoryMembership,
    create_person,
    deactivate_person,
    list_people,
    list_person_memberships,
    read_person,
    reactivate_person,
    set_church_membership_status,
    set_person_auth_link,
    update_person,
)
from app.services.serving_history import PersonServingSummary

__all__ = ["router"]

router = APIRouter(tags=["people"])

_PERSON_NOT_FOUND = "Person not found."
_MINISTRY_NOT_FOUND = "Ministry not found."
_MEMBERSHIP_NOT_FOUND = "Ministry membership not found."

_UNAUTHENTICATED = {"description": "No active actor could be established."}
_NOT_A_DIRECTORY_READER = {
    "description": "The actor may not view the people directory."
}
_ADMIN_ONLY = {"description": "The actor is not an active Admin."}


# --------------------------------------------------------------------------
# The directory
# --------------------------------------------------------------------------


@router.get(
    "/people",
    response_model=PeopleDirectoryResponse,
    summary="The church-wide people directory",
    responses={401: _UNAUTHENTICATED, 403: _NOT_A_DIRECTORY_READER},
)
def read_people(
    search: str | None = Query(
        default=None,
        max_length=200,
        description=(
            "Case-insensitive substring of the display name. Plain matching"
            " for human navigation -- never fuzzy, and never a ranking."
        ),
    ),
    include_inactive: bool = Query(
        default=False,
        description="Also return people who are deactivated church-wide.",
    ),
    limit: int = Query(
        default=DEFAULT_DIRECTORY_LIMIT,
        ge=1,
        le=MAX_DIRECTORY_LIMIT,
        description="Page size. The service clamps this in any case.",
    ),
    offset: int = Query(default=0, ge=0, description="How many rows to skip."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PeopleDirectoryResponse:
    """Read-only, bounded, and **Admin or Ministry Head only**.

    A normal volunteer receives 403 from the service, not an empty list: the
    church's membership roll is a real disclosure, and answering "nothing here"
    would be a lie that a future change could turn into a leak.
    """
    page = list_people(
        session,
        actor=actor,
        search=search,
        include_inactive=include_inactive,
        limit=limit,
        offset=offset,
    )
    return PeopleDirectoryResponse(
        people=[_directory_row(entry) for entry in page.people],
        total=page.total,
        limit=page.limit,
        offset=page.offset,
    )


@router.post(
    "/people",
    response_model=PersonResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a canonical church-wide Person",
    responses={
        401: _UNAUTHENTICATED,
        403: {
            "description": (
                "No ministry was named and the actor is not an Admin, or a"
                " ministry was named and the actor does not lead it."
            )
        },
        404: {"description": "No such ministry."},
        409: {
            "description": (
                "The name is blank, somebody already has this exact name and"
                " the duplicate was not acknowledged, the address belongs to"
                " somebody else, or the ministry is deactivated."
            )
        },
    },
)
def create_new_person(
    body: CreatePersonRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonResponse:
    """**201.** One human, one Person -- and never a merge.

    An exact same-name person makes the first attempt a **409 with an
    explanation**, and the caller repeats the request with
    ``acknowledge_duplicate_name`` once a human has checked. The rule lives in
    the service so no client can skip it; nothing here or there fuzzy-matches,
    ranks or selects a candidate (ADR 0001, Task 79 §4).

    When ``initial_ministry_id`` is given the person is created *and* added to
    that ministry in the same transaction -- two audit rows, one commit.

    **Who may do which.** Creating somebody with no ministry is a church-wide
    act and needs an **Admin**. Creating them *into* a ministry is an
    operational write on that ministry and needs its active **Head** -- an
    Admin who does not lead it is refused, and may instead create the Person
    unattached and let that ministry's Head add them (Task 79 §1).
    """
    ministry = (
        None
        if body.initial_ministry_id is None
        else _require_ministry(session, body.initial_ministry_id)
    )

    person = create_person(
        session,
        actor=actor,
        display_name=body.display_name,
        email=body.email,
        phone=body.phone,
        initial_ministry=ministry,
        acknowledge_duplicate_name=body.acknowledge_duplicate_name,
        reason=body.reason,
    )

    if ministry is not None:
        # Authorized again, against the same ministry, by the membership
        # service's own check. Deliberately not skipped as "already checked":
        # one operation, one authorization rule, wherever it is called from.
        add_person_to_ministry(
            session,
            actor=actor,
            person=person,
            ministry=ministry,
            reason=body.reason,
        )

    return _person_response(session, actor=actor, person=person)


@router.get(
    "/people/{person_id}",
    response_model=PersonResponse,
    summary="One person, and every ministry they belong to",
    responses={
        401: _UNAUTHENTICATED,
        403: _NOT_A_DIRECTORY_READER,
        404: {"description": "No such person."},
    },
)
def read_one_person(
    person_id: int = Path(ge=1, description="The person to read."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonResponse:
    """Read-only. Includes deactivated memberships, unlike the directory --
    "they used to be on this team" is what a detail screen is for.
    """
    person = _require_person(session, person_id)
    return _person_response(session, actor=actor, person=person)


@router.patch(
    "/people/{person_id}",
    response_model=PersonResponse,
    summary="Edit a person's safe church-wide fields",
    responses={
        401: _UNAUTHENTICATED,
        403: _ADMIN_ONLY,
        404: {"description": "No such person."},
        409: {"description": "The display name is blank."},
    },
)
def update_one_person(
    person_id: int = Path(ge=1, description="The person to edit."),
    body: UpdatePersonRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonResponse:
    """**Admin only.** A full replace of name and phone.

    The sign-in address, the active state and Admin authority are all
    unreachable from this endpoint -- see
    :class:`app.api.people_schemas.UpdatePersonRequest`.
    """
    person = _require_person(session, person_id)
    person = update_person(
        session,
        actor=actor,
        person=person,
        display_name=body.display_name,
        phone=body.phone,
        reason=body.reason,
    )
    return _person_response(session, actor=actor, person=person)


@router.put(
    "/people/{person_id}/auth-link",
    response_model=PersonResponse,
    summary="Link, replace or remove the address a person signs in with",
    responses={
        401: _UNAUTHENTICATED,
        403: _ADMIN_ONLY,
        404: {"description": "No such person."},
        409: {
            "description": (
                "The address is malformed, or already belongs to another"
                " person."
            )
        },
    },
)
def set_auth_link(
    person_id: int = Path(ge=1, description="The person whose access is changing."),
    body: AuthLinkRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonResponse:
    """**Admin only**, and deliberately not available to a Ministry Head: this
    decides which Google account *is* this person (Task 79 §7).

    ``PUT`` because it sets the link to whatever is sent -- an address, or
    ``null`` to remove it. Validation is the very same
    :func:`app.auth.email_link.normalize_email` the OAuth callback and
    ``scripts.link_person_email`` use; no part of OAuth is redesigned here and
    the CLI remains in place for bulk work.
    """
    person = _require_person(session, person_id)
    person = set_person_auth_link(
        session, actor=actor, person=person, email=body.email, reason=body.reason
    )
    return _person_response(session, actor=actor, person=person)


@router.put(
    "/people/{person_id}/church-membership-status",
    response_model=PersonResponse,
    summary="Record whether this person is a formal member of the church",
    responses={
        401: _UNAUTHENTICATED,
        403: _ADMIN_ONLY,
        404: {"description": "No such person."},
        409: {"description": "The status is not one of the three."},
    },
)
def set_status(
    person_id: int = Path(ge=1, description="The person whose status is changing."),
    body: ChurchMembershipStatusRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonResponse:
    """**Admin only.** MEMBER, NON_MEMBER or UNKNOWN.

    **A different fact from ministry membership, with different authority.**
    Whether somebody is a formal member of the church is church-wide
    governance; who is on the AV team is that ministry's business. A Ministry
    Head reads this on the directory -- it is useful when deciding who to
    approach -- and cannot change it.

    ``PUT`` because it sets the status to whatever is sent. Idempotent:
    restating the current status writes no audit row.
    """
    person = _require_person(session, person_id)
    person = set_church_membership_status(
        session,
        actor=actor,
        person=person,
        status=body.church_membership_status,
        reason=body.reason,
    )
    return _person_response(session, actor=actor, person=person)


@router.post(
    "/people/{person_id}/deactivate",
    response_model=PersonResponse,
    summary="Deactivate a person church-wide, without deleting anything",
    responses={
        401: _UNAUTHENTICATED,
        403: _ADMIN_ONLY,
        404: {"description": "No such person."},
        409: {"description": "An Admin cannot deactivate themselves."},
    },
)
def deactivate_one_person(
    person_id: int = Path(ge=1, description="The person to deactivate."),
    body: ReasonRequest = Body(default_factory=ReasonRequest),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonResponse:
    """**Admin only, and church-wide.** Not the same act as taking somebody off
    one team -- that is ``/ministry-memberships/{id}/remove``.

    Idempotent. Nothing is deleted: every assignment, membership,
    qualification and audit row survives, which is what makes
    ``/reactivate`` able to restore the person exactly.
    """
    person = _require_person(session, person_id)
    person = deactivate_person(
        session, actor=actor, person=person, reason=body.reason
    )
    return _person_response(session, actor=actor, person=person)


@router.post(
    "/people/{person_id}/reactivate",
    response_model=PersonResponse,
    summary="Reactivate a previously deactivated person",
    responses={
        401: _UNAUTHENTICATED,
        403: _ADMIN_ONLY,
        404: {"description": "No such person."},
    },
)
def reactivate_one_person(
    person_id: int = Path(ge=1, description="The person to reactivate."),
    body: ReasonRequest = Body(default_factory=ReasonRequest),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonResponse:
    """**Admin only.** Idempotent, and restores exactly the memberships and
    head flags the person had, because deactivation cleared none of them.
    """
    person = _require_person(session, person_id)
    person = reactivate_person(
        session, actor=actor, person=person, reason=body.reason
    )
    return _person_response(session, actor=actor, person=person)


# --------------------------------------------------------------------------
# Memberships
# --------------------------------------------------------------------------


@router.get(
    "/people/{person_id}/memberships",
    response_model=PersonMembershipsResponse,
    summary="The ministries one person belongs to",
    responses={
        401: _UNAUTHENTICATED,
        403: _NOT_A_DIRECTORY_READER,
        404: {"description": "No such person."},
    },
)
def read_person_memberships(
    person_id: int = Path(ge=1, description="The person whose teams to list."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonMembershipsResponse:
    """Read-only, and the same disclosure as the directory itself.

    Exists so a detail screen can repaint this panel after an add or a remove
    without re-reading the whole Person.
    """
    person = _require_person(session, person_id)
    return _memberships_response(session, actor=actor, person=person)


@router.post(
    "/people/{person_id}/memberships",
    response_model=PersonMembershipsResponse,
    summary="Add a person to a ministry you manage",
    responses={
        401: _UNAUTHENTICATED,
        403: {
            "description": (
                "The actor is not an active Ministry Head of this ministry."
                " Church-wide Admin authority is not enough."
            )
        },
        404: {"description": "No such person, or no such ministry."},
        409: {
            "description": (
                "The ministry is deactivated, or the person is deactivated"
                " church-wide."
            )
        },
    },
)
def add_membership(
    person_id: int = Path(ge=1, description="The person to add."),
    body: AddMembershipRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonMembershipsResponse:
    """**The ministry's own active Head**, and the ministry is **named
    explicitly in the body**, never inferred from the actor -- a Head who leads
    three must say which one (Task 79 §1).

    **An Admin who does not lead this ministry is refused**, deliberately:
    rostering a team is the job of whoever runs it, and church-wide Admin
    authority is oversight rather than operation (§1, §18). An Admin who *is*
    its Head passes, through that membership.

    **Idempotent**: adding somebody already on the team changes nothing, writes
    no audit row and answers 200 with the current list. Somebody who was
    removed earlier has their original membership **reactivated**, never
    duplicated, so their serving history stays attached to them.
    """
    person = _require_person(session, person_id)
    ministry = _require_ministry(session, body.ministry_id)
    add_person_to_ministry(
        session,
        actor=actor,
        person=person,
        ministry=ministry,
        notes=body.notes,
        joined_on=body.joined_on,
        reason=body.reason,
    )
    return _memberships_response(session, actor=actor, person=person)


@router.patch(
    "/ministry-memberships/{ministry_membership_id}",
    response_model=PersonMembershipResponse,
    summary="Edit the ministry-specific detail of one membership",
    responses={
        401: _UNAUTHENTICATED,
        403: {
        "description": (
            "The actor is not an active Ministry Head of this membership's"
            " ministry."
        )
    },
        404: {"description": "No such membership."},
    },
)
def update_one_membership(
    ministry_membership_id: int = Path(ge=1, description="The membership to edit."),
    body: UpdateMembershipRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonMembershipResponse:
    """A full replace of the two ministry-scoped fields, notes and joined-on.

    Nothing church-wide is reachable here, and not because this route is
    careful: they are not fields on ``ministry_membership`` at all (ADR 0001).
    """
    membership = _require_membership(session, ministry_membership_id)
    membership = update_membership(
        session,
        actor=actor,
        membership=membership,
        notes=body.notes,
        joined_on=body.joined_on,
        reason=body.reason,
    )
    return _membership_from_row(membership)


@router.post(
    "/ministry-memberships/{ministry_membership_id}/remove",
    response_model=PersonMembershipResponse,
    summary="Remove a person from one ministry, keeping their history",
    responses={
        401: _UNAUTHENTICATED,
        403: {
            "description": (
                "The actor is not an active Ministry Head of this membership's"
                " ministry, or the membership holds head authority and the"
                " actor is not also an active Admin."
            )
        },
        404: {"description": "No such membership."},
    },
)
def remove_one_membership(
    ministry_membership_id: int = Path(ge=1, description="The membership to remove."),
    body: ReasonRequest = Body(default_factory=ReasonRequest),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonMembershipResponse:
    """**One ministry only**, and named ``remove`` rather than ``delete``
    because nothing is deleted: one timestamp is written and every assignment,
    availability answer, qualification and audit row survives.

    The ministry's own active Head, as with the add. Removing a membership that
    **holds head authority** additionally requires an active Admin (core §5.1)
    and emits two audit rows -- the revocation and the removal. Both checks
    apply, so an Admin who does not lead the ministry cannot depose its head
    this way either; taking authority away is
    ``PUT /people/{id}/ministry-head``, which is Admin-only and needs no
    membership of the ministry at all.
    """
    membership = _require_membership(session, ministry_membership_id)
    membership = remove_person_from_ministry(
        session, actor=actor, membership=membership, reason=body.reason
    )
    return _membership_from_row(membership)


# --------------------------------------------------------------------------
# Ministry Head authority -- church governance, Admin only
# --------------------------------------------------------------------------


@router.put(
    "/people/{person_id}/ministry-head",
    response_model=PersonMembershipResponse,
    summary="Appoint this person to lead a ministry, or revoke that authority",
    responses={
        401: _UNAUTHENTICATED,
        403: _ADMIN_ONLY,
        404: {"description": "No such person, or no such ministry."},
        409: {
            "description": (
                "Appointing into a deactivated ministry, appointing somebody"
                " deactivated church-wide, or revoking where the person has no"
                " membership of that ministry."
            )
        },
    },
)
def set_ministry_head(
    person_id: int = Path(ge=1, description="The person being appointed or demoted."),
    body: MinistryHeadAuthorityRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PersonMembershipResponse:
    """**Admin only** -- a Ministry Head cannot promote another, in their own
    ministry or any other, and cannot promote themselves (core §4.3).

    **Person, ministry, grant or revoke**: the three choices Task 79 §10
    requires an Admin to make explicitly. The person is the URL, the ministry
    and the direction are the body, and nothing is inferred from who is asking.

    ``is_ministry_head=true`` **creates the membership if there is not one.**
    That is not a way around the roster rules -- it is the only way a newly
    created ministry can ever get its first head, since under
    :func:`~app.services.authorization.require_ministry_operator` a ministry
    nobody leads is a ministry nobody can add to. Appointing its leader is the
    governance act that breaks the circle.

    ``is_ministry_head=false`` **leaves the ordinary membership intact**: they
    stay on the team with every qualification and every past assignment, and
    simply no longer lead it.

    Idempotent in both directions; restating what is already true writes no
    audit row.
    """
    person = _require_person(session, person_id)
    ministry = _require_ministry(session, body.ministry_id)
    membership = set_ministry_head_authority(
        session,
        actor=actor,
        person=person,
        ministry=ministry,
        is_ministry_head=body.is_ministry_head,
        reason=body.reason,
    )
    return _membership_from_row(membership)


# --------------------------------------------------------------------------
# Lookups and mapping
# --------------------------------------------------------------------------


def _require_person(session: Session, person_id: int) -> Person:
    """The person the URL names, or 404, read on the request Session."""
    person = session.execute(
        select(Person).where(Person.id == person_id)
    ).scalar_one_or_none()
    if person is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_PERSON_NOT_FOUND
        )
    return person


def _require_ministry(session: Session, ministry_id: int) -> Ministry:
    """The ministry the URL or body names, or 404."""
    ministry = session.execute(
        select(Ministry).where(Ministry.id == ministry_id)
    ).scalar_one_or_none()
    if ministry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_MINISTRY_NOT_FOUND
        )
    return ministry


def _require_membership(session: Session, membership_id: int) -> MinistryMembership:
    """The membership the URL names, or 404.

    No ministry filter and no actor filter: which ministries the actor may
    manage is the service's decision, and applying it here as a 404 would both
    duplicate the rule and answer "no such membership" to a caller who is
    merely unauthorized.
    """
    membership = session.execute(
        select(MinistryMembership).where(MinistryMembership.id == membership_id)
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_MEMBERSHIP_NOT_FOUND
        )
    return membership


def _person_response(
    session: Session, *, actor: Person, person: Person
) -> PersonResponse:
    """Build the full person payload, memberships included.

    Routed through :func:`app.services.person_directory.read_person` even
    straight after a mutation, so the response is assembled by the one function
    that knows which fields may leave the building -- and so a mutation's
    response can never carry a field its own read endpoint would withhold.
    """
    entry = read_person(session, actor=actor, person=person)
    return PersonResponse(
        person_id=entry.person_id,
        display_name=entry.display_name,
        email=entry.email,
        phone=entry.phone,
        is_admin=entry.is_admin,
        church_membership_status=entry.church_membership_status,
        deactivated_at=entry.deactivated_at,
        memberships=[_membership_response(row) for row in entry.memberships],
        recorded_serving_total=entry.recorded_serving_total,
        serving=_serving_response(entry.serving_summary),
    )


def _memberships_response(
    session: Session, *, actor: Person, person: Person
) -> PersonMembershipsResponse:
    """The memberships payload, shared by the read and the add.

    The add answers with the person's *whole* refreshed list rather than only
    the membership it touched: an add may have reactivated a row the caller's
    screen was showing as removed, and returning one row would leave the rest
    of that screen stale.
    """
    memberships = list_person_memberships(session, actor=actor, person=person)
    return PersonMembershipsResponse(
        person_id=person.id,
        memberships=[_membership_response(row) for row in memberships],
    )


def _directory_row(entry: DirectoryEntry) -> DirectoryPersonResponse:
    # No ``email`` and no ``phone``: the listing's model has no field for
    # either, and the service does not fetch them (Task 79 §5).
    return DirectoryPersonResponse(
        person_id=entry.person_id,
        display_name=entry.display_name,
        is_admin=entry.is_admin,
        church_membership_status=entry.church_membership_status,
        deactivated_at=entry.deactivated_at,
        memberships=[_membership_response(row) for row in entry.memberships],
        recorded_serving_total=entry.recorded_serving_total,
    )


def _membership_response(row: DirectoryMembership) -> PersonMembershipResponse:
    return PersonMembershipResponse(
        ministry_membership_id=row.ministry_membership_id,
        ministry_id=row.ministry_id,
        ministry_name=row.ministry_name,
        is_ministry_head=row.is_ministry_head,
        deactivated_at=row.deactivated_at,
        notes=row.notes,
        joined_on=row.joined_on,
    )


def _serving_response(
    summary: PersonServingSummary | None,
) -> ServingSummaryResponse | None:
    """The per-ministry breakdown, when the read bought one.

    ``None`` on the directory, where only the total was fetched (one aggregate
    for the whole page). Absent rather than an empty summary, so a client can
    tell "not asked for" from "no history" -- which are different facts, and a
    zero-filled object would make them look the same.
    """
    if summary is None:
        return None
    return ServingSummaryResponse(
        total=summary.total,
        by_ministry=[
            MinistryServingCountResponse(
                ministry_id=entry.ministry_id,
                ministry_name=entry.ministry_name,
                count=entry.count,
            )
            for entry in summary.by_ministry
        ],
        same_date_conflicts=[
            ServingConflictResponse(
                event_date=conflict.event_date,
                ministry_names=list(conflict.ministry_names),
            )
            for conflict in summary.same_date_conflicts
        ],
    )


def _membership_from_row(membership: MinistryMembership) -> PersonMembershipResponse:
    """The single-membership payload, built from the ORM row a service returned.

    ``membership.ministry.name`` lazy-loads if it is not already loaded -- one
    small query on a rare, human-driven mutation, and the same trade
    :mod:`app.services.ministry_authority` already makes for its audit summary.
    Field by field rather than serialized from the row, so nothing the model
    happens to carry can escape by accident.
    """
    return PersonMembershipResponse(
        ministry_membership_id=membership.id,
        ministry_id=membership.ministry_id,
        ministry_name=membership.ministry.name,
        is_ministry_head=membership.is_ministry_head,
        deactivated_at=membership.deactivated_at,
        notes=membership.notes,
        joined_on=membership.joined_on,
    )
