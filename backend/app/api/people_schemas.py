"""Transport models for people and membership management (Task 79).

Same rules as every other schema module here -- hand-built, ``extra="forbid"``,
no ``from_attributes``, so an ORM row cannot be serialized by accident. That
last property matters more on this resource than on any other in the API: a
``Person`` row carries contact details and a sign-in address, and a response
model that could absorb one wholesale is a disclosure waiting for somebody to
add a column.

What a person response carries, and what it deliberately does not
-----------------------------------------------------------------
Name, contact details already in the domain, church-wide Admin authority,
active state, and the ministries they belong to. That is the whole list.

**No gender, birthday, age, child flag, affinity or avoidance field appears
anywhere in this module**, and none is accepted on any request. The reference
small-group application that prompted Task 79 has those concepts; this
scheduler has no current requirement that reads any of them, no stored
equivalent, and no solver input derived from one (see the task report). Adding
a field "because the other product has it" would mean a church collecting
personal data it has no use for, which is exactly the trade data minimization
exists to refuse.

**The auth-link address is on the *detail* response only, and only to somebody
who may already read it.** ``email`` is on :class:`PersonResponse` because an
Admin managing sign-in access has to see the address they are about to replace,
and because the whole resource is restricted to Admins and Ministry Heads
(:func:`app.services.authorization.require_people_directory_reader`). It is
deliberately **absent from the directory listing**, which is the whole church's
roll and shows no contact details: a screen that does not render an address has
no business fetching one for every person in the church (Task 79 §5). And it is
never on any *public* or self-service response: ``/api/v1/me`` still reports
only the actor's name, admin flag and headed ministries, and it is not extended
here.

**Two fields that read as one, and never are.**
``church_membership_status`` is formal membership of the church;
``memberships`` is which ministries somebody serves in. Neither is derived from
the other in either direction, and each has its own endpoint with its own
authority -- see :class:`ChurchMembershipStatusRequest`.

Person and Membership are separate in the payload, too
------------------------------------------------------
:class:`PersonResponse` nests :class:`PersonMembershipResponse` rather than
flattening ministry state onto the person. ADR 0001's distinction survives the
serialization boundary: a client reading this cannot accidentally treat "is a
head" or "is active" as a property of the human, because each is spelled on the
membership it belongs to, and the person's own ``deactivated_at`` sits at the
top level where it plainly means something else.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AddMembershipRequest",
    "AuthLinkRequest",
    "ChurchMembershipStatusRequest",
    "CreatePersonRequest",
    "DirectoryPersonResponse",
    "MinistryHeadAuthorityRequest",
    "MinistryServingCountResponse",
    "PeopleDirectoryResponse",
    "PersonMembershipResponse",
    "PersonMembershipsResponse",
    "PersonResponse",
    "ReasonRequest",
    "ServingConflictResponse",
    "ServingSummaryResponse",
    "UpdateMembershipRequest",
    "UpdatePersonRequest",
]


class PersonMembershipResponse(BaseModel):
    """One ministry a person belongs to, and their state in it.

    ``deactivated_at`` here is the *membership's*, never the person's -- the
    two are separate facts (core §6) and the response reports both, in their
    own places, rather than collapsing them into one "active" flag that would
    have to pick a meaning.
    """

    model_config = ConfigDict(extra="forbid")

    ministry_membership_id: int
    ministry_id: int
    ministry_name: str
    #: Head authority for this one ministry. There is no church-wide head
    #: permission for this to be a summary of (core §5).
    is_ministry_head: bool
    #: ``null`` while the person is on this team. Non-null means they were
    #: removed from it, and since when -- never deleted.
    deactivated_at: datetime.datetime | None = None
    #: Ministry-specific participation detail, readable by anyone who may read
    #: the directory and writable only by someone who manages this ministry.
    notes: str | None = None
    joined_on: datetime.date | None = None


class MinistryServingCountResponse(BaseModel):
    """How many past dates one person has recorded serving in one ministry.

    Only ministries the person actually has history in are listed (Task 79
    §12): a row reading ``"AV: 0"`` would assert an absence that the absence of
    the row already states, and would invite a reader to wonder whether it
    meant "never" or "not counted".
    """

    model_config = ConfigDict(extra="forbid")

    ministry_id: int
    ministry_name: str
    count: int


class ServingConflictResponse(BaseModel):
    """A date on which the record puts one person in two ministries at once.

    **Not two services.** The church-wide hard rule is one ministry per person
    per Sunday, so this is a contradiction in the record, reported rather than
    added up (Task 79 §13). Empty in every healthy state.
    """

    model_config = ConfigDict(extra="forbid")

    event_date: datetime.date
    ministry_names: list[str] = Field(default_factory=list)


class ServingSummaryResponse(BaseModel):
    """One person's recorded serving: total, breakdown, and any conflicts.

    ``total`` is the sum of ``by_ministry``, by construction -- both are built
    from one query in :mod:`app.services.serving_history`, so a screen showing
    them side by side cannot show two numbers that disagree.
    """

    model_config = ConfigDict(extra="forbid")

    total: int
    by_ministry: list[MinistryServingCountResponse] = Field(default_factory=list)
    same_date_conflicts: list[ServingConflictResponse] = Field(default_factory=list)


class DirectoryPersonResponse(BaseModel):
    """One row of the people directory.

    Carries the memberships rather than a pre-rendered "Setup, AV" string: the
    table shows ministry names *and* head badges *and* which of them are
    inactive, and a server-formatted summary could serve only one of those.

    **No ``email`` and no ``phone``** (Task 79 §5). This listing is the whole
    church's roll and every Ministry Head may read it; the table shows neither,
    so the payload carries neither, and the service does not even fetch them
    (:func:`app.services.person_directory._directory_page_statement`). They are
    on :class:`PersonResponse`, for the one screen that needs them. Omitting
    the fields rather than sending ``null`` matters: a ``null`` would be
    indistinguishable from "this person has no address".
    """

    model_config = ConfigDict(extra="forbid")

    person_id: int
    display_name: str
    #: Church-wide Admin authority (core §4.1) -- distinct from the per-ministry
    #: head flag on each membership below.
    is_admin: bool
    #: MEMBER, NON_MEMBER or UNKNOWN. **Formal membership of the church**, and
    #: a different fact from ``memberships`` below -- see
    #: :class:`ChurchMembershipStatusRequest`.
    church_membership_status: str
    #: ``null`` while the person is active in the church. Non-null means
    #: deactivated church-wide, and since when. Never deleted.
    deactivated_at: datetime.datetime | None = None
    memberships: list[PersonMembershipResponse] = Field(default_factory=list)
    #: How many past dates the authoritative record shows this person serving
    #: on. **"Recorded serving", never "attended"** -- the domain records
    #: commitments, not attendance (see
    #: :mod:`app.services.serving_history`). The directory carries the total
    #: only; the breakdown is on the person response.
    recorded_serving_total: int = 0


class PeopleDirectoryResponse(BaseModel):
    """A bounded page of the church-wide directory.

    ``total`` is how many people match the filters, not how many are in this
    page, so a screen can say "showing 100 of 214" rather than leaving the
    reader to guess whether the list ended or was cut off.
    """

    model_config = ConfigDict(extra="forbid")

    people: list[DirectoryPersonResponse] = Field(default_factory=list)
    total: int
    limit: int
    offset: int


class PersonResponse(BaseModel):
    """One person in full, with every membership, active or not.

    The same shape as a directory row on purpose -- one type for the client to
    model -- differing only in that the detail read never hides a deactivated
    membership, because "they used to be on this team" is precisely what the
    detail screen exists to show.
    """

    model_config = ConfigDict(extra="forbid")

    person_id: int
    display_name: str
    email: str | None = None
    phone: str | None = None
    is_admin: bool
    church_membership_status: str
    deactivated_at: datetime.datetime | None = None
    memberships: list[PersonMembershipResponse] = Field(default_factory=list)
    recorded_serving_total: int = 0
    #: The per-ministry breakdown, present only on the detail read.
    serving: ServingSummaryResponse | None = None


class PersonMembershipsResponse(BaseModel):
    """Just one person's memberships, for repainting that panel alone."""

    model_config = ConfigDict(extra="forbid")

    person_id: int
    memberships: list[PersonMembershipResponse] = Field(default_factory=list)


class CreatePersonRequest(BaseModel):
    """Everything a caller may say when creating a canonical Person.

    **No ``is_admin`` and no ``deactivated_at``.** Neither is writable at
    creation: a new person is an ordinary, active member of the church, and
    authority is a separate, deliberate act. A Ministry Head can reach this
    endpoint, which is what makes their absence load-bearing rather than tidy.

    ``initial_ministry_id`` is **required for a Ministry Head** and optional for
    an Admin -- the service explains why
    (:func:`app.services.person_directory.create_person`). When supplied, the
    person is created and added to that ministry in one transaction.

    ``acknowledge_duplicate_name`` is the explicit human decision Task 79 §4
    requires. The server never merges, never fuzzy-matches and never picks a
    candidate; when an exact same-name person already exists it refuses once
    and says so, and the caller may repeat the request with this flag set to
    say "yes, I checked, this is a different human". Nothing is inferred either
    way.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, description="Must not be blank once trimmed.")
    #: The sign-in address, if it is known already. Optional: most people are
    #: scheduled long before they ever sign in, and the absence of an address
    #: *is* the cannot-sign-in state (core §9).
    email: str | None = None
    phone: str | None = None
    initial_ministry_id: int | None = Field(default=None, ge=1)
    #: Set only after a human has seen the warning and decided anyway.
    acknowledge_duplicate_name: bool = False
    #: Free text for the audit trail. Omit it, or send ``null``; whitespace-only
    #: is rejected, because a blank reason is not a reason.
    reason: str | None = None


class UpdatePersonRequest(BaseModel):
    """The person's complete new safe identity -- a full replace, not a
    per-field patch, matching :class:`app.api.ministry_role_schemas
    .UpdateMinistryRoleRequest`.

    **Three fields are deliberately unreachable from here**: ``email`` is the
    sign-in link and has its own endpoint with its own collision rule;
    ``deactivated_at`` is a lifecycle act with its own two endpoints, so an
    edit form cannot deactivate somebody by mistake; and ``is_admin`` is not
    writable through this API at all (see the task report on global Admin
    promotion).
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, description="Must not be blank once trimmed.")
    phone: str | None = None
    reason: str | None = None


class AuthLinkRequest(BaseModel):
    """The address a person will sign in with, or ``null`` to remove it.

    One request model for link, replace and unlink, because the domain has one
    operation: whatever address is sent becomes the address, and ``null`` means
    they can no longer sign in. Validation is
    :func:`app.auth.email_link.normalize_email`, the same function the OAuth
    callback and the admin CLI use -- not a second, subtly different rule.
    """

    model_config = ConfigDict(extra="forbid")

    email: str | None = None
    reason: str | None = None


class AddMembershipRequest(BaseModel):
    """Which ministry to add this person to, said explicitly.

    ``ministry_id`` is required and is never inferred from the actor, even when
    the actor heads exactly one ministry: a Head who leads three must say which
    one they mean (Task 79 §1), and a rule that only applies to some actors is
    a rule that will be got wrong.
    """

    model_config = ConfigDict(extra="forbid")

    ministry_id: int = Field(ge=1, description="The ministry to add them to.")
    notes: str | None = None
    joined_on: datetime.date | None = None
    reason: str | None = None


class UpdateMembershipRequest(BaseModel):
    """The membership's complete new ministry-specific detail.

    ``is_ministry_head`` is absent: authority is Admin-only and has its own two
    endpoints, so a head editing a teammate's notes cannot promote them.
    """

    model_config = ConfigDict(extra="forbid")

    notes: str | None = None
    joined_on: datetime.date | None = None
    reason: str | None = None


class ChurchMembershipStatusRequest(BaseModel):
    """The formal church membership status an Admin is recording.

    **MEMBER, NON_MEMBER or UNKNOWN**, and validated against the same three
    values the database's CHECK constraint holds. ``UNKNOWN`` is a real value,
    not a missing one: most people most of the time have had nothing said about
    them, and setting somebody back to it is a correction rather than a
    deletion.

    **This is not ministry membership.** A person's ministries are
    ``memberships`` on the person response and are changed through entirely
    different endpoints with entirely different authority. Nothing in the API
    derives either from the other, in either direction -- not from serving
    history, not from a name, not from an address, not from attendance
    (Task 79 §6).

    Admin-only. A Ministry Head may read the status on the directory, because
    it is useful context when deciding who to approach, and may not change it.
    """

    model_config = ConfigDict(extra="forbid")

    church_membership_status: str = Field(
        description="MEMBER, NON_MEMBER or UNKNOWN.",
    )
    reason: str | None = None


class MinistryHeadAuthorityRequest(BaseModel):
    """Which ministry, and whether the person leads it. **Admin only.**

    Person comes from the URL, ministry and direction from the body -- the
    three things Task 79 §10 requires an Admin to choose explicitly. There is
    no "the actor's ministry" and nothing is inferred from the actor.

    Granting creates the membership when there is not one, which is the only
    way a newly created ministry can acquire its first head; revoking leaves
    the ordinary membership, every qualification and every past assignment
    exactly where they are (see
    :func:`app.services.ministry_membership.set_ministry_head_authority`).
    """

    model_config = ConfigDict(extra="forbid")

    ministry_id: int = Field(ge=1, description="The ministry they will lead.")
    is_ministry_head: bool = Field(
        description="True to appoint them, false to revoke the authority.",
    )
    reason: str | None = None


class ReasonRequest(BaseModel):
    """An optional audit reason, and nothing else.

    Used by deactivate/reactivate/remove, where *which* person or membership
    and *what happens* to them come entirely from the URL and the verb.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = None
