"""The church-wide people directory, and the Person record itself (Task 79).

This module owns the **church-wide half** of people management: reading the
directory, creating a canonical Person, editing the safe identity fields,
deactivating and reactivating somebody, and linking the address they sign in
with. The **ministry-scoped half** -- who is on which team -- lives next door in
:mod:`app.services.ministry_membership`, and the split follows ADR 0001
exactly: one human is one Person, and participation is a separate relation.

Implements the authorization rules in
``docs/architecture/core-data-model.md`` §4.1--§4.3 and §6, and records each
mutation per ``docs/architecture/audit-event-data-model.md``.

Who may do what, and why the split is asymmetric
------------------------------------------------
**Reading is church-wide. Writing the Person is Admin-only.**

- :func:`list_people` and :func:`read_person` -- Admin, or an active Ministry
  Head of any ministry (:func:`~app.services.authorization
  .require_people_directory_reader`). A Head genuinely needs to look up someone
  outside their own team: that is exactly how they find an existing Person
  instead of inventing a second one.
- :func:`update_person`, :func:`deactivate_person`, :func:`reactivate_person`,
  :func:`set_person_auth_link`, :func:`set_church_membership_status` --
  **active Admin only**. These change the church-wide record, and a Ministry
  Head's authority is scoped to a ministry (core §4.3). A Head who could
  deactivate a Person church-wide could remove somebody from every other
  ministry's rota without any of those ministries agreeing; a Head who could
  set formal church membership status would be recording a church-wide fact on
  the strength of leading one team.
- :func:`create_person` -- an active **Admin** when no ministry is named, and
  the named ministry's active **Head** when one is. See its own docstring: a
  Head's reason for creating a person is always "to put them on my team", and
  requiring the ministry makes that explicit -- while an Admin who leads that
  ministry passes through their Head membership rather than their Admin flag.

What a directory read now carries
---------------------------------
Two facts beyond name and memberships, both of which the church actually asks
for and neither of which is derived from the other:

- ``church_membership_status`` -- MEMBER, NON_MEMBER or UNKNOWN. **Formal
  membership of the church**, which is not ministry participation and is never
  inferred from it (core's status constants say why).
- ``recorded_serving_total`` -- how many past dates the authoritative record
  shows this person serving on, from :mod:`app.services.serving_history`.
  :func:`list_people` buys the total alone, in one aggregate for the whole
  page; :func:`read_person` additionally buys the per-ministry breakdown. The
  term is **"recorded serving"** and never "attended": the domain records
  commitments, not attendance.

**Nothing here deletes a Person, and no operation can be made to.** Every
foreign key into ``person`` is ``ON DELETE RESTRICT`` (core §6) and no statement
in this module is a ``DELETE``. Deactivation is a timestamp; assignments,
qualifications, availability, memberships and audit rows are untouched by it,
which is what "preserve history" means here concretely.

Identity is never inferred
--------------------------
**No merge, no fuzzy match, no auto-select.** :func:`create_person` refuses
once when an exact same-name Person already exists, and matches exactly after
case-folding and trimming -- the same rule :mod:`scripts.link_person_email`
refuses to loosen, for the same reason: two people genuinely share a name, and
guessing which one is meant is not a mistake that shows up until the wrong
person is reading somebody's schedule. Creation stays an explicit human
decision; this module tells a caller a duplicate is likely and never acts on
that belief itself.

Finding people is :func:`list_people` and nothing else. There is deliberately
no second "look this name up" entry point beside it: two ways to ask who is
already here is two answers that can drift apart, and the one that drifted
would be the one somebody trusted before creating a duplicate.

The directory read, and its query budget
----------------------------------------
:func:`list_people` is **five queries, whatever the page size** -- the count,
one bounded page of people, one pass over the memberships of exactly those
people, the church's timezone, and one aggregate for their serving totals.
Never a lazy ``person.ministry_memberships`` per row, never a serving query per
person, and never an unbounded scan: ``limit`` is clamped here rather than
trusted from the caller.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.auth.email_link import (
    EmailFormatError,
    find_person_by_email,
    normalize_email,
)
from app.models.core import (
    CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
    CHURCH_MEMBERSHIP_STATUSES,
    Ministry,
    MinistryMembership,
    Person,
)
from app.services.audit import (
    ACTION_PERSON_AUTH_LINK_CHANGED,
    ACTION_PERSON_CHURCH_MEMBERSHIP_STATUS_CHANGED,
    ACTION_PERSON_CREATED,
    ACTION_PERSON_DEACTIVATED,
    ACTION_PERSON_REACTIVATED,
    ACTION_PERSON_UPDATED,
    record_audit_event,
)
from app.services.authorization import (
    require_active_admin,
    require_ministry_operator,
    require_people_directory_reader,
)
from app.services.errors import AuthorizationError, InvalidOperationError
from app.services.serving_history import (
    PersonServingSummary,
    recorded_serving_summaries,
    recorded_serving_totals,
)

__all__ = [
    "DEFAULT_DIRECTORY_LIMIT",
    "MAX_DIRECTORY_LIMIT",
    "DirectoryEntry",
    "DirectoryMembership",
    "DirectoryPage",
    "create_person",
    "deactivate_person",
    "list_people",
    "list_person_memberships",
    "read_person",
    "reactivate_person",
    "set_church_membership_status",
    "set_person_auth_link",
    "update_person",
]

_TARGET_TABLE = "person"

#: How many people one directory page returns when the caller says nothing.
#: Large enough that a church of this size is one or two pages, small enough
#: that the second query stays a short ``IN`` list.
DEFAULT_DIRECTORY_LIMIT = 100

#: The ceiling, applied to whatever the caller asks for. A caller cannot widen
#: the page past this, so "give me everybody" cannot become an unbounded scan
#: of the church's membership roll (§10's bounded-query requirement).
MAX_DIRECTORY_LIMIT = 500


@dataclass(frozen=True)
class DirectoryMembership:
    """One ministry a directory entry belongs to.

    Carries the membership's *own* id and activity, not the person's: the two
    deactivations are separate facts (core §6), and a screen that showed only
    one of them would be lying about the other.
    """

    ministry_membership_id: int
    ministry_id: int
    ministry_name: str
    is_ministry_head: bool
    deactivated_at: datetime.datetime | None
    notes: str | None = None
    joined_on: datetime.date | None = None


@dataclass(frozen=True)
class DirectoryEntry:
    """One person in the directory, with the ministries they belong to.

    **The fields here are the whole list, and it is deliberately short.** Name,
    formal church membership status, church-wide Admin authority, active
    state, memberships, recorded serving, and -- on the detail read only --
    the contact details already in the domain. No age, gender, birthday, child
    flag, affinity or avoidance: nothing in this application's scheduling
    requires any of them, so none is collected (Task 79 §17).
    """

    person_id: int
    display_name: str
    #: Contact details, and **only the detail read fetches them**. The listing
    #: leaves both ``None``, because the church's whole roll is readable by
    #: every Ministry Head and that screen shows neither (Task 79 §5). The
    #: listing's response model has no field for either, so a client cannot
    #: read this ``None`` as "they have no address".
    email: str | None
    phone: str | None
    is_admin: bool
    #: MEMBER, NON_MEMBER or UNKNOWN. **Formal membership of the church, and
    #: nothing to do with** ``memberships`` **below**, which is participation
    #: in ministries. Readable by any directory reader; writable only by an
    #: Admin, through :func:`set_church_membership_status`.
    church_membership_status: str
    deactivated_at: datetime.datetime | None
    memberships: tuple[DirectoryMembership, ...] = field(default_factory=tuple)
    #: How many past dates this person's authoritative record shows them
    #: serving on. See :mod:`app.services.serving_history` for the exact rule
    #: and for why it is never called "times attended".
    recorded_serving_total: int = 0
    #: The per-ministry breakdown, and any dates on which the record
    #: contradicts the one-ministry-per-Sunday rule. Populated by
    #: :func:`read_person` and left ``None`` by :func:`list_people`, which
    #: deliberately buys only the total (Task 79 §12).
    serving_summary: PersonServingSummary | None = None

    @property
    def is_active(self) -> bool:
        return self.deactivated_at is None


@dataclass(frozen=True)
class DirectoryPage:
    """One bounded page of the directory, and how much more there is.

    ``total`` is the count matching the filters, not the page length, so a
    screen can say "showing 100 of 214" rather than leaving somebody to guess
    whether the list ended or was truncated.
    """

    people: tuple[DirectoryEntry, ...]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def list_people(
    session: Session,
    *,
    actor: Person,
    search: str | None = None,
    include_inactive: bool = False,
    limit: int = DEFAULT_DIRECTORY_LIMIT,
    offset: int = 0,
) -> DirectoryPage:
    """A bounded page of the church-wide directory, for an authorized reader.

    **Read-only**, and ordered by ``lower(display_name)`` then ``id`` so two
    people who share a name appear in a stable order rather than whatever the
    database returns.

    ``search`` is **plain substring matching on the display name**, case
    insensitive -- human navigation, nothing more. It is deliberately not fuzzy
    and deliberately not a ranking: this box exists so somebody can type three
    letters and find the row they already know is there, and a search that
    guessed would be the first step toward a system that merges identities on
    its own (module docstring). Blank or whitespace-only is treated as no
    search at all.

    ``include_inactive=False`` (the default) returns only people who are active
    church-wide. It says nothing about membership activity: somebody active in
    the church with every membership deactivated still appears, which is
    exactly the state an Admin needs to be able to see.

    **A fixed number of queries, whatever the page size** -- the count, the
    page, one pass over those people's memberships, the church's timezone, and
    one serving-totals aggregate. Nothing lazy-loads per row, and nothing here
    grows with the number of people (module docstring).

    :raises AuthorizationError: the actor is deactivated, or is neither an
        active Admin nor an active Ministry Head of anything.
    :raises InvalidOperationError: ``offset`` is negative.
    """
    require_people_directory_reader(actor)

    if offset < 0:
        raise InvalidOperationError("offset must not be negative")
    limit = _clamp_limit(limit)
    term = _search_term(search)

    total = session.execute(
        _directory_count_statement(
            term, church_id=actor.church_id, include_inactive=include_inactive
        )
    ).scalar_one()

    rows = session.execute(
        _directory_page_statement(
            term,
            church_id=actor.church_id,
            include_inactive=include_inactive,
            limit=limit,
            offset=offset,
        )
    ).all()

    person_ids = [row.person_id for row in rows]
    memberships = _memberships_by_person(session, person_ids)
    # One aggregate for the whole page, never one query per person
    # (Task 79 §14). The breakdown is deliberately not fetched here: the
    # directory shows the total, and the detail screen is where "which
    # ministries?" is asked.
    serving_totals = recorded_serving_totals(
        session, person_ids=person_ids, church_id=actor.church_id
    )

    people = tuple(
        DirectoryEntry(
            person_id=row.person_id,
            display_name=row.display_name,
            # Never fetched by the listing -- see
            # :func:`_directory_page_statement`. ``None`` here means "not
            # read", which is why the listing's response model has no field
            # for either: an absent field cannot be mistaken for "they have
            # no address".
            email=None,
            phone=None,
            is_admin=row.is_admin,
            church_membership_status=row.church_membership_status,
            deactivated_at=row.deactivated_at,
            memberships=tuple(memberships.get(row.person_id, ())),
            recorded_serving_total=serving_totals.get(row.person_id, 0),
        )
        for row in rows
    )
    return DirectoryPage(people=people, total=total, limit=limit, offset=offset)


def read_person(session: Session, *, actor: Person, person: Person) -> DirectoryEntry:
    """One person and every ministry they belong to, active or not.

    The same authorization as :func:`list_people` -- being able to open a
    person's detail is the same disclosure as being able to list them, so the
    two must not disagree.

    Unlike the listing, this **always includes deactivated memberships**: the
    detail screen is where somebody looks to find out that a person used to be
    on a team and is not any more, and hiding that would make a removal look
    like a person who had never joined.

    :raises AuthorizationError: the actor may not read the directory.
    """
    require_people_directory_reader(actor)
    _require_same_church(actor, person)

    memberships = _memberships_by_person(session, [person.id])
    summary = recorded_serving_summaries(
        session, person_ids=[person.id], church_id=person.church_id
    )[person.id]
    return DirectoryEntry(
        person_id=person.id,
        display_name=person.display_name,
        email=person.email,
        phone=person.phone,
        is_admin=person.is_admin,
        church_membership_status=person.church_membership_status,
        deactivated_at=person.deactivated_at,
        memberships=tuple(memberships.get(person.id, ())),
        recorded_serving_total=summary.total,
        serving_summary=summary,
    )


def list_person_memberships(
    session: Session, *, actor: Person, person: Person
) -> tuple[DirectoryMembership, ...]:
    """Just the memberships half of :func:`read_person`.

    Split out because the membership panel refreshes on its own after an
    add or a remove, and re-reading the whole Person to repaint one list
    would be a second query for data that did not change.

    :raises AuthorizationError: the actor may not read the directory.
    """
    require_people_directory_reader(actor)
    _require_same_church(actor, person)
    return tuple(_memberships_by_person(session, [person.id]).get(person.id, ()))


# --------------------------------------------------------------------------
# Writing the church-wide Person
# --------------------------------------------------------------------------


def create_person(
    session: Session,
    *,
    actor: Person,
    display_name: str,
    email: str | None = None,
    phone: str | None = None,
    initial_ministry: Ministry | None = None,
    acknowledge_duplicate_name: bool = False,
    reason: str | None = None,
) -> Person:
    """Create one canonical Person, as ``actor``.

    **Who may call this, and the one asymmetry.** Creating somebody with **no**
    ministry is a church-wide act and needs an active **Admin**. Creating them
    **into** a ministry is an operational write on that ministry and needs its
    active **Head** (:func:`~app.services.authorization
    .require_ministry_operator`) -- an Admin who does not lead it is refused,
    and an Admin who does lead it passes through that Head membership.

    The asymmetry is the product rule, not a technicality: a Head's reason for
    creating somebody is always "to put them on my team", so naming the team
    makes the act explicit -- and it means no Head can mint church-wide
    identities that belong to nothing and that nobody else is watching. An
    Admin administers the church roll itself, so an unattached Person is a
    legitimate thing for them to create -- and putting that person on a team is
    then that team's Head's decision, not theirs.

    **The membership is not created here.** This function creates the Person and
    authorizes against ``initial_ministry``; the caller then calls
    :func:`app.services.ministry_membership.add_person_to_ministry` in the same
    transaction, so exactly one module writes ``ministry_membership`` rows and
    exactly one audit action describes joining a ministry.

    **A same-name person is a refusal once, never a merge.** Two people may
    genuinely share a name, and ``person.display_name`` carries no unique index
    for exactly that reason (core §7) -- so this cannot be a hard rule. What it
    is instead: if an exact same-name Person already exists, case-folded, the
    first call is refused with a message saying so, and a caller who has
    checked repeats the request with ``acknowledge_duplicate_name=True``.

    That keeps creation an **explicit human decision** (Task 79 §4) while making
    the likely mistake loud. Nothing here merges, fuzzy-matches, ranks or
    auto-selects a candidate, and the flag means only "a human looked" -- it
    never causes an existing Person to be reused (module docstring).

    **The rule lives here rather than in the route** so it cannot be bypassed
    by a second client that forgets to ask.

    The church is ``actor.church_id`` and is deliberately not a parameter: V1 is
    a single-church deployment (core §7), and a caller-supplied church id would
    be a way to file somebody under a church nobody meant.

    ``email`` is the sign-in link and is normalized and checked for collision
    the same way :func:`set_person_auth_link` does -- one address belongs to one
    person, enforced by ``uq_person_email_lower`` and checked here first so the
    caller gets a domain error rather than an ``IntegrityError`` at commit.

    :raises AuthorizationError: no ministry was named and the actor is not an
        active Admin, or a ministry was named and the actor is not its active
        Ministry Head.
    :raises InvalidOperationError: ``display_name`` is blank; somebody already
        has that exact name and ``acknowledge_duplicate_name`` was not set;
        ``email`` is malformed or already belongs to somebody else;
        ``initial_ministry`` is deactivated; or ``reason`` was supplied but
        blank.
    """
    _authorize_creation(actor, initial_ministry=initial_ministry)
    reason = _validate_optional_reason(reason)

    display_name = _require_display_name(display_name)
    phone = _optional_text(phone)

    if not acknowledge_duplicate_name and _exact_name_matches(session, display_name):
        raise InvalidOperationError(
            f"a person named {display_name!r} already exists."
            " Check whether this is the same human before creating another;"
            " confirm to create a second person with this name"
        )

    normalized_email = (
        None if email is None else _normalize_unclaimed_email(session, email, owner=None)
    )

    if initial_ministry is not None and initial_ministry.deactivated_at is not None:
        raise InvalidOperationError(
            "cannot add a person to a deactivated ministry"
        )

    person = Person(
        church_id=actor.church_id,
        display_name=display_name,
        email=normalized_email,
        phone=phone,
        # Never accepted as a parameter. Admin authority is granted by editing
        # the church-wide record, not smuggled in at creation time by whoever
        # happens to be filling in the form -- and a Ministry Head may reach
        # this function, which makes the distinction load-bearing rather than
        # stylistic.
        is_admin=False,
        # Likewise never a parameter, and set explicitly rather than left to
        # the column's server default. Two reasons: a Ministry Head reaches
        # this function and must not be able to declare somebody a member of
        # the church, and the attribute would otherwise read ``None`` in memory
        # until the row round-trips -- so a status change in the same
        # transaction would record a "before" of nothing rather than UNKNOWN.
        church_membership_status=CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
    )
    session.add(person)
    # The minimum needed to get an identity for the audit row that must
    # reference it. The caller still owns the commit (see :mod:`app.services`).
    session.flush()

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_PERSON_CREATED,
        target_table=_TARGET_TABLE,
        target_id=person.id,
        summary=f"Created person {display_name}",
        reason=reason,
        after_values={
            "display_name": display_name,
            # Whether they can sign in, never the address itself -- see
            # ACTION_PERSON_AUTH_LINK_CHANGED's own note.
            "email_linked": normalized_email is not None,
        },
    )
    return person


def update_person(
    session: Session,
    *,
    actor: Person,
    person: Person,
    display_name: str,
    phone: str | None = None,
    reason: str | None = None,
) -> Person:
    """Replace ``person``'s safe church-wide identity fields, as ``actor``.

    **Active Admin only** (module docstring).

    **Two fields, and the boundary around them is the point.** ``display_name``
    and ``phone`` are the safe editable identity; everything else about a
    Person is reached through the operation that owns it --

    - ``email`` is the sign-in link, not a contact field to retype in an edit
      form: it decides *who a Google account is*. It has its own operation,
      :func:`set_person_auth_link`, with its own audit action and its own
      collision rule.
    - ``deactivated_at`` is a lifecycle act with its own two operations, so an
      edit form cannot deactivate somebody as a side effect of a typo.
    - ``is_admin`` is **not writable through this API at all**. See
      :func:`set_person_auth_link`'s sibling note and Task 79's report: the
      domain supports the column, but no reviewed product rule yet says who may
      promote an Admin or what happens to the last one, and inventing that rule
      inside an edit form is not the place to start.

    A full replace of the two fields rather than a per-field patch -- the same
    shape :func:`app.services.ministry_role.update_ministry_role` uses, for the
    same reason: the caller submits the record's complete editable content, as
    an edit form does, and no per-field PATCH semantics are invented here.

    **Idempotent.** If neither field differs, nothing is changed and **no audit
    row is written**: no domain state changed, so there is no act to record.

    :raises AuthorizationError: the actor is not an active Admin.
    :raises InvalidOperationError: ``display_name`` is blank, or ``reason`` was
        supplied but blank.
    """
    require_active_admin(actor)
    reason = _validate_optional_reason(reason)

    display_name = _require_display_name(display_name)
    phone = _optional_text(phone)

    before = {"display_name": person.display_name, "phone": person.phone}
    after = {"display_name": display_name, "phone": phone}
    if before == after:
        return person

    person.display_name = display_name
    person.phone = phone

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_PERSON_UPDATED,
        target_table=_TARGET_TABLE,
        target_id=person.id,
        summary=f"Updated person {display_name}",
        reason=reason,
        # Only the changed business fields (audit §7.2) -- and only the two
        # this operation can change, so a reader is never left wondering
        # whether an unlisted field moved too.
        before_values=before,
        after_values=after,
    )
    return person


def deactivate_person(
    session: Session,
    *,
    actor: Person,
    person: Person,
    reason: str | None = None,
    now: datetime.datetime | None = None,
) -> Person:
    """Deactivate ``person`` church-wide, preserving everything they did.

    **Active Admin only**, and this is the operation a Ministry Head must never
    reach: it removes somebody from *every* ministry's pool at once, including
    ministries the actor has nothing to do with. A Head who wants somebody off
    their own team calls
    :func:`app.services.ministry_membership.remove_person_from_ministry`, which
    changes one membership and leaves the rest of the church alone.

    **This is not a delete, and the UI must not call it one.** One timestamp is
    written. Assignments, availability, qualifications, serving limits, audit
    rows and every ``ministry_membership`` row survive untouched, and
    :func:`reactivate_person` restores the person to exactly the state they
    were in -- which is only true *because* nothing was cleared.

    **Head authority is deliberately left in place**, and it is inert while the
    person is deactivated: every check in
    :mod:`app.services.authorization` tests ``actor.deactivated_at`` before it
    tests anything else, so a deactivated Head can perform no action at all.
    Clearing the flags instead would silently discard who leads what, and
    reactivating would then quietly restore a person with less authority than
    they had -- a data loss disguised as a lifecycle operation. The database
    agrees: its ``head_requires_active_membership`` check constrains the
    *membership*'s activity, not the person's.

    **Idempotent**: an already-deactivated person is returned unchanged with no
    audit row, and the original timestamp is preserved rather than refreshed.

    :raises AuthorizationError: the actor is not an active Admin.
    :raises InvalidOperationError: ``reason`` was supplied but blank.
    """
    require_active_admin(actor)
    reason = _validate_optional_reason(reason)

    if person.deactivated_at is not None:
        return person

    # Refused before anything is written. An Admin who deactivated themselves
    # would be locked out of the one role that can undo it, and if they were
    # the only Admin the church would have no way back in without database
    # access. Cheap to check, unrecoverable to get wrong.
    if person.id == actor.id:
        raise InvalidOperationError(
            "an Admin cannot deactivate themselves; ask another Admin to do it"
        )

    person.deactivated_at = now if now is not None else _now()

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_PERSON_DEACTIVATED,
        target_table=_TARGET_TABLE,
        target_id=person.id,
        summary=f"Deactivated person {person.display_name}",
        reason=reason,
        before_values={"active": True},
        after_values={"active": False},
    )
    return person


def reactivate_person(
    session: Session,
    *,
    actor: Person,
    person: Person,
    reason: str | None = None,
) -> Person:
    """Reactivate a previously deactivated ``person``, as ``actor``.

    **Active Admin only.** Clears the one timestamp and nothing else -- every
    membership, every head flag and every qualification is exactly where it was
    left, because :func:`deactivate_person` cleared none of them.

    **Idempotent**: an already-active person is returned unchanged with no
    audit row.

    :raises AuthorizationError: the actor is not an active Admin.
    :raises InvalidOperationError: ``reason`` was supplied but blank.
    """
    require_active_admin(actor)
    reason = _validate_optional_reason(reason)

    if person.deactivated_at is None:
        return person

    person.deactivated_at = None

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_PERSON_REACTIVATED,
        target_table=_TARGET_TABLE,
        target_id=person.id,
        summary=f"Reactivated person {person.display_name}",
        reason=reason,
        before_values={"active": False},
        after_values={"active": True},
    )
    return person


def set_person_auth_link(
    session: Session,
    *,
    actor: Person,
    person: Person,
    email: str | None,
    reason: str | None = None,
) -> Person:
    """Link, replace or remove the address ``person`` signs in with.

    **Active Admin only, and a Ministry Head may never call it.** This decides
    which Google account *is* this person; heading a ministry confers no say in
    who may sign in as whom (core §4.3, and Task 79 §7 says so explicitly).

    **No OAuth is redesigned here.** The validation is
    :func:`app.auth.email_link.normalize_email` and the collision lookup is
    :func:`app.auth.email_link.find_person_by_email` -- the same two functions
    the OAuth callback and ``scripts.link_person_email`` already use, called
    rather than re-implemented, so an address an Admin links through the UI and
    an address the CLI links behave identically at sign-in. The CLI remains,
    unchanged and still the tool for bulk work.

    ``email=None`` removes the link. The Person row is otherwise untouched: no
    deletion, no deactivation, and every membership, assignment and audit row
    stays exactly as it was. Re-linking restores access.

    **Replacement needs no separate flag here, unlike the CLI's ``--replace``.**
    The CLI guards a one-shot command typed into a terminal where the current
    value is invisible; an Admin in the UI is looking at the current address on
    the screen they are editing, and the audit row records that it changed.

    **Deactivated people may be linked**, deliberately: preparing an account
    before somebody is reactivated is legitimate, and sign-in is refused at the
    callback, which checks the Person is active. This matches the CLI's own
    behaviour exactly.

    **Idempotent**: linking the address somebody already has changes nothing
    and writes no audit row.

    :raises AuthorizationError: the actor is not an active Admin.
    :raises InvalidOperationError: the address is malformed, already belongs to
        another Person, or ``reason`` was supplied but blank.
    """
    require_active_admin(actor)
    reason = _validate_optional_reason(reason)

    normalized = (
        None
        if email is None
        else _normalize_unclaimed_email(session, email, owner=person)
    )

    was_linked = person.email is not None
    if person.email == normalized:
        return person

    person.email = normalized

    if normalized is None:
        summary = (
            f"Removed the sign-in address of {person.display_name};"
            " they can no longer sign in"
        )
    elif was_linked:
        summary = f"Replaced the sign-in address of {person.display_name}"
    else:
        summary = f"Linked a sign-in address to {person.display_name}"

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_PERSON_AUTH_LINK_CHANGED,
        target_table=_TARGET_TABLE,
        target_id=person.id,
        summary=summary,
        reason=reason,
        # The addresses themselves are deliberately absent -- see
        # ACTION_PERSON_AUTH_LINK_CHANGED in :mod:`app.services.audit`.
        before_values={"email_linked": was_linked},
        after_values={"email_linked": normalized is not None},
    )
    return person


def set_church_membership_status(
    session: Session,
    *,
    actor: Person,
    person: Person,
    status: str,
    reason: str | None = None,
) -> Person:
    """Record whether ``person`` is a formal member of the church.

    **Active Admin only** (Task 79 §6). A Ministry Head may *see* this on the
    directory -- it is useful context when they are deciding who to ask -- and
    may never change it: whether somebody is a member of the church is not a
    fact about one team's rota, and a Head who could set it would be recording
    a church-wide status on the strength of leading one ministry.

    **This is not** :class:`~app.models.core.MinistryMembership`, and the two
    are never derived from one another. Somebody may serve every Sunday and not
    be a formal member; somebody may be a member and serve nowhere. The status
    changes because an Admin says so and for no other reason -- not from
    ministry participation, serving history, a name, an address, attendance or
    an assignment (core's ``CHURCH_MEMBERSHIP_STATUS_MEMBER`` note).

    Three values, and ``UNKNOWN`` is a real one: "nobody has said" is the
    honest state for most people most of the time, and setting somebody *back*
    to it is a legitimate correction rather than a deletion.

    **Idempotent**: restating the status somebody already has changes nothing
    and writes no audit row.

    :raises AuthorizationError: the actor is not an active Admin.
    :raises InvalidOperationError: ``status`` is not one of the three, or
        ``reason`` was supplied but blank.
    """
    require_active_admin(actor)
    reason = _validate_optional_reason(reason)
    status = _require_church_membership_status(status)

    before = person.church_membership_status
    if before == status:
        return person

    person.church_membership_status = status

    record_audit_event(
        session,
        actor=actor,
        action=ACTION_PERSON_CHURCH_MEMBERSHIP_STATUS_CHANGED,
        target_table=_TARGET_TABLE,
        target_id=person.id,
        summary=(
            f"Set the church membership status of {person.display_name}"
            f" to {status}"
        ),
        reason=reason,
        # The two statuses and nothing else: no contact details, no ministry
        # list, and nothing that would let this row be read as evidence for
        # the decision rather than a record of it (audit §7.2).
        before_values={"church_membership_status": before},
        after_values={"church_membership_status": status},
    )
    return person


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def _directory_page_statement(
    term: str | None,
    *,
    church_id: int,
    include_inactive: bool,
    limit: int,
    offset: int,
) -> Select:
    """One bounded page of people, ordered for a human to read.

    Explicit columns rather than whole ORM entities, the same shape
    :func:`app.services.role_qualification._role_qualifications_statement`
    uses: the caller builds its own value objects, and no attribute access on
    the result can wander off into a lazy load.
    """
    # **Contact details are deliberately not selected here** (Task 79 §5).
    # The listing is the whole church's roll, readable by every Ministry Head,
    # and it does not render an address or a phone number -- so it does not
    # fetch one. Not fetching is a stronger guarantee than not rendering: a
    # payload that carried them would hand every Head everybody's contact
    # details whatever the table chose to show. :func:`read_person` fetches
    # them, for the one screen that needs them.
    stmt = select(
        Person.id.label("person_id"),
        Person.display_name.label("display_name"),
        Person.is_admin.label("is_admin"),
        Person.church_membership_status.label("church_membership_status"),
        Person.deactivated_at.label("deactivated_at"),
    )
    stmt = _apply_directory_filters(
        stmt, term, church_id=church_id, include_inactive=include_inactive
    )
    return stmt.order_by(func.lower(Person.display_name), Person.id).limit(limit).offset(
        offset
    )


def _directory_count_statement(
    term: str | None, *, church_id: int, include_inactive: bool
) -> Select:
    """How many people match, ignoring the page window.

    A separate statement rather than a window function on the page query: the
    page is already ordered and limited, and counting alongside it would make
    the plan pay for the ordering twice.
    """
    stmt = select(func.count()).select_from(Person)
    return _apply_directory_filters(
        stmt, term, church_id=church_id, include_inactive=include_inactive
    )


def _apply_directory_filters(
    stmt: Select, term: str | None, *, church_id: int, include_inactive: bool
) -> Select:
    """The ``WHERE`` clause both directory statements must agree on.

    Shared so the count and the page can never disagree about what "matching"
    means -- a discrepancy that would render as "showing 12 of 9".

    **Scoped to the actor's own church**, matching
    :func:`app.services.ministry_directory.list_church_ministries`. V1 is a
    single-church deployment (core §7) so today this narrows nothing, and that
    is the point: the filter is here before a second church exists rather than
    after somebody notices it missing.
    """
    stmt = stmt.where(Person.church_id == church_id)
    if not include_inactive:
        stmt = stmt.where(Person.deactivated_at.is_(None))
    if term is not None:
        # ``lower()`` on both sides, matching the collation-independent
        # comparison used everywhere else in this project, and escaped so a
        # name containing % or _ is searched for literally.
        stmt = stmt.where(
            func.lower(Person.display_name).like(f"%{_escape_like(term)}%", escape="\\")
        )
    return stmt


def _exact_name_matches(session: Session, display_name: str) -> tuple[Person, ...]:
    """Every Person whose display name equals ``display_name``, case-folded.

    The unauthorized core of :func:`find_people_by_exact_name`, shared with
    :func:`create_person`'s duplicate warning so the two can never disagree
    about what "the same name" means -- one of them warning about a collision
    the other would not report would be worse than neither doing it.

    ``lower()`` on both sides, the same comparison
    :func:`app.auth.email_link.find_person_by_email` uses for addresses.
    """
    candidate = display_name.strip()
    if not candidate:
        return ()
    rows = (
        session.execute(
            select(Person)
            .where(func.lower(Person.display_name) == func.lower(candidate))
            .order_by(Person.id)
        )
        .scalars()
        .all()
    )
    return tuple(rows)


def _memberships_by_person(
    session: Session, person_ids: list[int]
) -> dict[int, list[DirectoryMembership]]:
    """Every membership of the given people, in one query, grouped by person.

    **This is the N+1 that the directory would otherwise be.** Reading
    ``person.ministry_memberships`` per row would issue one statement per
    person, and each of those would lazy-load ``membership.ministry`` for the
    name -- two per person, on a page of a hundred. One join, one pass.

    An empty ``person_ids`` short-circuits rather than issuing ``IN ()``.
    """
    if not person_ids:
        return {}

    rows = session.execute(
        select(
            MinistryMembership.person_id.label("person_id"),
            MinistryMembership.id.label("ministry_membership_id"),
            MinistryMembership.ministry_id.label("ministry_id"),
            Ministry.name.label("ministry_name"),
            MinistryMembership.is_ministry_head.label("is_ministry_head"),
            MinistryMembership.deactivated_at.label("deactivated_at"),
            MinistryMembership.notes.label("notes"),
            MinistryMembership.joined_on.label("joined_on"),
        )
        .join(Ministry, Ministry.id == MinistryMembership.ministry_id)
        .where(MinistryMembership.person_id.in_(person_ids))
        .order_by(
            MinistryMembership.person_id,
            func.lower(Ministry.name),
            MinistryMembership.id,
        )
    ).all()

    grouped: dict[int, list[DirectoryMembership]] = {}
    for row in rows:
        grouped.setdefault(row.person_id, []).append(
            DirectoryMembership(
                ministry_membership_id=row.ministry_membership_id,
                ministry_id=row.ministry_id,
                ministry_name=row.ministry_name,
                is_ministry_head=row.is_ministry_head,
                deactivated_at=row.deactivated_at,
                notes=row.notes,
                joined_on=row.joined_on,
            )
        )
    return grouped


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _authorize_creation(actor: Person, *, initial_ministry: Ministry | None) -> None:
    """Two questions, answered by whether a ministry was named.

    - **No ministry named** -- this is a church-wide act, creating an entry in
      the church's own roll that belongs to no team.
      :func:`require_active_admin`. A Ministry Head cannot mint unattached
      identities that nobody is watching.
    - **A ministry named** -- the person is being created *into* that team, so
      the act is an operational write on it:
      :func:`require_ministry_operator`, against that specific ministry.

    **An Admin who is not that ministry's Head is refused the second**, and
    that is the Task 79 change: church-wide governance does not include putting
    people onto a team somebody else runs. An Admin who genuinely needs to may
    create the Person with no ministry, and that ministry's Head adds them.
    An Admin who *is* the Head passes, through their Head membership.

    The check is deliberately not "Admin OR operator": an Admin naming a
    ministry they do not lead should be refused, not waved through on the
    strength of the branch they would otherwise match.
    """
    if initial_ministry is None:
        require_active_admin(actor)
        return

    require_ministry_operator(actor, ministry_id=initial_ministry.id)


def _normalize_unclaimed_email(
    session: Session, email: str, *, owner: Person | None
) -> str:
    """The normalized address, once it is certain nobody else holds it.

    ``owner`` is the Person the address is being assigned to, so re-linking
    somebody's own address is not mistaken for a collision. ``None`` means the
    Person does not exist yet.

    The database's ``uq_person_email_lower`` index enforces the same rule, so
    a race between two requests ends in an ``IntegrityError`` rather than a
    duplicate -- checking here turns the ordinary case into a readable domain
    error raised before anything is mutated.
    """
    try:
        normalized = normalize_email(email)
    except EmailFormatError as error:
        raise InvalidOperationError(str(error)) from error

    holder = find_person_by_email(session, normalized)
    if holder is not None and (owner is None or holder.id != owner.id):
        # The holder's name is deliberately not in the message. The caller is
        # an authorized directory reader who can look the address up, and an
        # error string is the wrong place to disclose who has an address.
        raise InvalidOperationError(
            "that address is already linked to another person;"
            " one address belongs to one person"
        )
    return normalized


def _require_church_membership_status(status: str) -> str:
    """One of the three, exactly as spelled.

    Not case-folded and not trimmed into shape: these are enum values a client
    copies from the API, not prose a human typed, and quietly accepting
    ``"member"`` would mean the one rejected spelling is whichever one nobody
    tested. The database's ``church_membership_status_valid`` CHECK enforces
    the same set, so this turns an ``IntegrityError`` at commit into a readable
    domain error raised before anything is mutated.
    """
    if status not in CHURCH_MEMBERSHIP_STATUSES:
        allowed = ", ".join(CHURCH_MEMBERSHIP_STATUSES)
        raise InvalidOperationError(
            f"church_membership_status must be one of: {allowed}"
        )
    return status


def _require_same_church(actor: Person, person: Person) -> None:
    """Refuse a read of somebody in a different church.

    V1 is a single-church deployment (core §7), so this refuses nothing today.
    It is here because the listing is church-scoped and a detail read that was
    not would be a way round the listing -- an inconsistency that is far
    cheaper to prevent now than to notice later.

    :raises AuthorizationError: the person belongs to another church.
    """
    if person.church_id != actor.church_id:
        raise AuthorizationError("that person belongs to another church")


def _require_display_name(display_name: str) -> str:
    """Non-blank once trimmed, matching ``display_name_not_blank`` in the
    database. Stored trimmed, so two people do not differ by a space.
    """
    candidate = display_name.strip()
    if not candidate:
        raise InvalidOperationError("display_name must not be blank")
    return candidate


def _optional_text(value: str | None) -> str | None:
    """Trimmed, with whitespace-only collapsing to ``None``.

    An empty text box means "no value", not "a value made of spaces" -- the
    same convention the API client already applies to notes and descriptions.
    """
    if value is None:
        return None
    candidate = value.strip()
    return candidate if candidate else None


def _validate_optional_reason(reason: str | None) -> str | None:
    """A reason is optional for these operations, but blank is not a reason.

    Identical to :func:`app.services.ministry_authority._validate_optional_reason`
    and left duplicated for the reason that module's own note gives: audit §9
    makes a reason mandatory only where accepted product behaviour already
    requires one, and no new universal requirement is invented here.
    """
    if reason is None:
        return None
    if not reason.strip():
        raise InvalidOperationError("reason must not be blank when supplied")
    return reason


def _search_term(search: str | None) -> str | None:
    """The trimmed, case-folded search term, or ``None`` for "no search"."""
    if search is None:
        return None
    candidate = search.strip()
    return candidate.lower() if candidate else None


def _escape_like(term: str) -> str:
    """Escape the wildcards so a literal ``%`` or ``_`` searches for itself.

    The backslash is escaped first; doing it last would double the backslashes
    this function had just introduced.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp_limit(limit: int) -> int:
    """A page size between 1 and :data:`MAX_DIRECTORY_LIMIT`.

    Clamped rather than rejected: a caller asking for more than the ceiling
    wants "as much as I can have", and refusing the request would only make
    them ask again with a smaller number.
    """
    if limit < 1:
        return 1
    return min(limit, MAX_DIRECTORY_LIMIT)


def _now() -> datetime.datetime:
    """Timezone-aware UTC, matching every other ``deactivated_at`` writer."""
    return datetime.datetime.now(datetime.timezone.utc)
