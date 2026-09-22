"""Core identity and ministry schema.

Implements the reviewed design in ``docs/architecture/core-data-model.md``
(revision 3) and the decision recorded in
``docs/adr/0001-canonical-person-and-ministry-membership.md``. Section numbers in
the comments below refer to the architecture document.

Seven tables: church, person, user_account, ministry, ministry_role,
ministry_membership, role_qualification. Scheduling, audit, and authorization
services are later slices and appear nowhere here.

Conventions used throughout, all from the design document:

- Primary keys are ``bigint GENERATED ALWAYS AS IDENTITY`` (§3.1).
- Timestamps are ``timestamptz``; ``created_at``/``updated_at`` on every table,
  and a nullable ``deactivated_at`` only where deactivation is a real domain
  state (§3.2, §6). ``NULL`` deactivated_at means active.
- Every foreign key is ``ON DELETE RESTRICT`` so that history can never be
  orphaned (§6).
- Case-insensitive uniqueness is a unique index on ``lower(column)``; the
  ``citext`` extension is deliberately not used (§3.3).
- Authority is stored as two booleans — ``person.is_admin`` and
  ``ministry_membership.is_ministry_head``. There is no stored
  ADMIN/MINISTRY_HEAD/NORMAL_USER tier column: the effective application role is
  derived in the service layer, which is a later slice (§4.2).
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Church(Base):
    """The church, and the configuration root the rest of the schema hangs off.

    V1 is a single-church deployment (§7): exactly one row is expected. The
    table exists because ``timezone`` is a real church-wide setting that later
    scheduling must not have to guess — not as multi-tenant machinery, which the
    design explicitly defers.
    """

    __tablename__ = "church"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    name: Mapped[str] = mapped_column(Text)
    #: IANA zone name, e.g. "America/Chicago". Timestamps are stored in UTC and
    #: rendered in this zone.
    timezone: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    people: Mapped[list[Person]] = relationship(back_populates="church")
    ministries: Mapped[list[Ministry]] = relationship(back_populates="church")


#: The three values ``person.church_membership_status`` may take (Task 79 §6).
#:
#: **Formal membership of the church, and nothing else.** This is not
#: :class:`MinistryMembership`, which says who serves on which team, and it is
#: never derived from one: a long-serving volunteer may not be a formal member,
#: and a member may serve nowhere. It is never inferred from participation,
#: serving history, a name, an address, attendance or an assignment -- only an
#: Admin stating it makes it so.
#:
#: ``UNKNOWN`` is the default and the honest starting point for every row that
#: existed before the column did: nobody has said, so the application does not
#: pretend to know.
CHURCH_MEMBERSHIP_STATUS_MEMBER = "MEMBER"
CHURCH_MEMBERSHIP_STATUS_NON_MEMBER = "NON_MEMBER"
CHURCH_MEMBERSHIP_STATUS_UNKNOWN = "UNKNOWN"

#: Ordered for a stable rendering, not by rank -- there is no ranking here.
CHURCH_MEMBERSHIP_STATUSES = (
    CHURCH_MEMBERSHIP_STATUS_MEMBER,
    CHURCH_MEMBERSHIP_STATUS_NON_MEMBER,
    CHURCH_MEMBERSHIP_STATUS_UNKNOWN,
)


class Person(Base):
    """The canonical, church-wide identity: exactly one row per human.

    A Person is never duplicated because they serve in several ministries —
    that is the decision recorded in ADR 0001. Ministry-scoped state lives on
    :class:`MinistryMembership`.

    A Person may exist with no :class:`UserAccount` at all: someone can be
    scheduled long before they ever sign in, and the absence of an account row
    *is* the not-yet-logged-in state (§9).
    """

    __tablename__ = "person"
    __table_args__ = (
        CheckConstraint(
            "length(btrim(display_name)) > 0", name="display_name_not_blank"
        ),
        # Three values, spelled out rather than left to the application. A
        # fourth spelling of "we do not know" -- NULL, an empty string, a typo
        # -- would be a second answer to a question that has exactly one.
        CheckConstraint(
            "church_membership_status IN ('MEMBER', 'NON_MEMBER', 'UNKNOWN')",
            name="church_membership_status_valid",
        ),
        Index("ix_person_church_id", "church_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    church_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("church.id", ondelete="RESTRICT")
    )
    #: The primary scheduling and display name, and the only name the schema
    #: needs. Structured given/family-name fields are deferred (§7 `person`).
    #: Deliberately not unique: two people may share a name.
    display_name: Mapped[str] = mapped_column(Text)
    #: Contact address, and the one-time match key when this person first signs
    #: in with Google (§9). Nullable: many people have no email.
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    #: Church-wide Admin authority (§4.1). Only an Admin may change it, which
    #: is a service-layer rule — no database constraint can see who is writing.
    is_admin: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    #: Formal membership of the church: MEMBER, NON_MEMBER or UNKNOWN
    #: (Task 79 §6). **Entirely separate from :class:`MinistryMembership`**,
    #: which is participation in a team -- see
    #: :data:`CHURCH_MEMBERSHIP_STATUS_MEMBER` above for why neither is ever
    #: derived from the other.
    #:
    #: Not nullable, and defaulted to UNKNOWN rather than left NULL: "nobody
    #: has said" is a real answer this column can give, and giving it a value
    #: means no reader has to decide what a NULL meant.
    #:
    #: Admin-only to write, which is a service-layer rule -- no database
    #: constraint can see who is writing.
    church_membership_status: Mapped[str] = mapped_column(
        Text, server_default=text("'UNKNOWN'")
    )
    deactivated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    church: Mapped[Church] = relationship(back_populates="people")
    user_account: Mapped[UserAccount | None] = relationship(
        back_populates="person"
    )
    ministry_memberships: Mapped[list[MinistryMembership]] = relationship(
        back_populates="person"
    )


# Contact emails must not collide, since email is the first-login match key —
# but only where one is present, hence the partial index (§7.1).
Index(
    "uq_person_email_lower",
    func.lower(Person.email),
    unique=True,
    postgresql_where=Person.email.isnot(None),
)


class UserAccount(Base):
    """Authentication identity for a Person. Optional, and at most one per Person.

    This table carries *no* authorization state: Google proves identity, the
    database decides authority, and authority lives on :class:`Person` and
    :class:`MinistryMembership` (§4.1).

    OAuth itself is not implemented in this slice. These are the minimum columns
    that let Google sign-in be added later without migrating the table; tokens,
    scopes, and sessions belong to the auth slice and are deliberately absent.
    """

    __tablename__ = "user_account"
    __table_args__ = (
        # At most one canonical account per Person (§9).
        UniqueConstraint("person_id", name="uq_user_account_person_id"),
        UniqueConstraint("google_subject", name="uq_user_account_google_subject"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("person.id", ondelete="RESTRICT")
    )
    #: The OIDC ``sub`` claim: the stable external identity, unique and
    #: unchanging for a given Google account. Every session lookup joins on
    #: this column alone (§9).
    google_subject: Mapped[str] = mapped_column(Text)
    #: Account metadata, *not* the authentication identity. A person can change
    #: the address on their Google account, so this value is mutable and must
    #: never be used as the identity key. The unique index below is data
    #: hygiene — two accounts should not claim one address (§9).
    email: Mapped[str] = mapped_column(Text)
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    #: Suspends login without deleting the row, and without touching the
    #: Person's identity or any schedule history (§6).
    disabled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    person: Mapped[Person] = relationship(back_populates="user_account")


Index("uq_user_account_email_lower", func.lower(UserAccount.email), unique=True)


class Ministry(Base):
    """A ministry within the church — Setup, AV, and whatever else is created.

    Ministries are data, not code: an Admin creates them at runtime and no
    ministry name ever appears in application logic.
    """

    __tablename__ = "ministry"
    __table_args__ = (
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        Index("ix_ministry_church_id", "church_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    church_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("church.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    #: Archived ministries are deactivated, never deleted: past schedules still
    #: refer to them (§6).
    deactivated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    church: Mapped[Church] = relationship(back_populates="ministries")
    roles: Mapped[list[MinistryRole]] = relationship(back_populates="ministry")
    memberships: Mapped[list[MinistryMembership]] = relationship(
        back_populates="ministry"
    )


# No two ministries share a name. Not partial on deactivated_at: reusing an
# archived ministry's name means reactivating or renaming it, both deliberate.
Index(
    "uq_ministry_church_id_name_lower",
    Ministry.church_id,
    func.lower(Ministry.name),
    unique=True,
)


class MinistryRole(Base):
    """A role a ministry defines for itself — "Setup Lead", "Sound", "Slides".

    Role names are data. Nothing in the application branches on them, which is
    what lets a new ministry be configured without a code change.
    """

    __tablename__ = "ministry_role"
    __table_args__ = (
        # Parent key for the composite foreign key on role_qualification. This
        # is what makes a cross-ministry qualification impossible (§7.2).
        UniqueConstraint("id", "ministry_id", name="uq_ministry_role_id_ministry_id"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        CheckConstraint("display_order >= 0", name="display_order_non_negative"),
        Index("ix_ministry_role_ministry_id", "ministry_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    ministry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ministry.id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    #: Stable human-facing ordering only — configuration screens, schedule
    #: views, master-schedule presentation, and future exports, so a role
    #: appears in the same position everywhere it is listed.
    #:
    #: This is NOT solver input. It carries no scheduling priority and no
    #: importance ranking, and the scheduling engine must never read it. If
    #: role priority ever becomes a real scheduling concept it gets its own
    #: field, named for what it means (§7 `ministry_role`).
    #:
    #: Gaps and ties are permitted; ties break on lower(name) in the query.
    display_order: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    #: Roles are deactivated rather than deleted: past assignments name them.
    deactivated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ministry: Mapped[Ministry] = relationship(back_populates="roles")
    role_qualifications: Mapped[list[RoleQualification]] = relationship(
        back_populates="role",
        primaryjoin="MinistryRole.id == RoleQualification.ministry_role_id",
        foreign_keys="RoleQualification.ministry_role_id",
    )


# Role names are unique within their ministry: "Setup Lead" and "AV Lead"
# coexist, two "Sound" roles in AV do not.
Index(
    "uq_ministry_role_ministry_id_name_lower",
    MinistryRole.ministry_id,
    func.lower(MinistryRole.name),
    unique=True,
)


class MinistryMembership(Base):
    """One Person's participation in one Ministry, and all state scoped to it.

    This is the relation ADR 0001 is about: ministry-specific information hangs
    off the membership instead of duplicating the Person.

    Head authority lives here too, as ``is_ministry_head``. Putting it on the
    membership makes three things structural rather than conventional: a head is
    necessarily a member of what they head, authority is always scoped to one
    ministry (there is no global head permission), and authority cannot outlive
    the participation it came from — see the check constraint below.
    """

    __tablename__ = "ministry_membership"
    __table_args__ = (
        # No duplicate membership in the same ministry, active or not: someone
        # rejoining reactivates this row rather than inserting a second.
        UniqueConstraint(
            "person_id", "ministry_id", name="uq_ministry_membership_person_id_ministry_id"
        ),
        # Parent key for the composite foreign key on role_qualification (§7.2).
        UniqueConstraint(
            "id", "ministry_id", name="uq_ministry_membership_id_ministry_id"
        ),
        # An inactive membership can never carry head authority (§5.1).
        # Deactivating a membership clears the flag in the same operation; this
        # constraint is what makes that rule enforceable rather than merely
        # conventional, and it is why the effective-role derivation does not
        # need to re-test deactivated_at.
        CheckConstraint(
            "NOT is_ministry_head OR deactivated_at IS NULL",
            name="head_requires_active_membership",
        ),
        Index("ix_ministry_membership_ministry_id", "ministry_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("person.id", ondelete="RESTRICT")
    )
    ministry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ministry.id", ondelete="RESTRICT")
    )
    #: Head authority for this one ministry (§4.3, §5). Several memberships in
    #: the same ministry may set it — co-heads are allowed, and there is
    #: deliberately no constraint limiting a ministry to one head. One person
    #: may head several ministries through several memberships.
    #:
    #: Admin-only to write, which is a service-layer rule; that service is a
    #: later slice and is not implemented here.
    is_ministry_head: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    #: Ministry-specific notes. Free prose, so Text — not JSONB.
    notes: Mapped[str | None] = mapped_column(Text)
    joined_on: Mapped[datetime.date | None] = mapped_column(Date)
    #: Active/inactive in this ministry. NULL means active.
    deactivated_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    person: Mapped[Person] = relationship(back_populates="ministry_memberships")
    ministry: Mapped[Ministry] = relationship(back_populates="memberships")
    role_qualifications: Mapped[list[RoleQualification]] = relationship(
        back_populates="membership",
        primaryjoin=(
            "MinistryMembership.id == RoleQualification.ministry_membership_id"
        ),
        foreign_keys="RoleQualification.ministry_membership_id",
    )


# Serves the effective-role derivation: "does this person head anything?".
# No deactivated_at predicate is needed — the check constraint above already
# guarantees a true flag implies an active membership (§7.3).
#
# The predicate is text() rather than the mapped attribute: a bare ORM attribute
# has no valid Python repr, so Alembic autogenerate renders it as an object
# address and emits a migration that will not parse.
Index(
    "ix_ministry_membership_head",
    MinistryMembership.person_id,
    postgresql_where=text("is_ministry_head"),
)


class RoleQualification(Base):
    """A ministry head's standing decision about one membership and one role.

    Three states, and the absence of a row is one of them (§8):

    - no row                       -> never assessed
    - row with is_qualified=False  -> assessed and explicitly not approved
    - row with is_qualified=True   -> approved for this role

    Revoking is an UPDATE, never a DELETE, so the row keeps a stable id for the
    whole life of the membership/role pair — which is what the later Audit Event
    slice needs to reference.

    Training/shadow state is NOT represented here. It is a person's progress;
    this is a head's authorization decision. They are separate facts and get
    separate tables when training arrives.

    **``ministry_id`` must be set explicitly when writing a row.** It is shared
    by both composite foreign keys below, which is what makes it impossible to
    link a membership in one ministry to a role in another (§7.2). Because the
    column is shared, the ORM relationships intentionally manage only their own
    single id column and do not populate ``ministry_id``; a row whose
    ``ministry_id`` disagrees with either parent is rejected by the database.
    """

    __tablename__ = "role_qualification"
    __table_args__ = (
        # The cross-ministry protection (§7.2). Both foreign keys route through
        # the *same* ministry_id column on this row, so a membership from
        # ministry A can never be paired with a role from ministry B — not by
        # the application, not by a backfill, not by hand in psql.
        #
        # Named explicitly: the naming convention's generated name for a
        # composite foreign key would exceed PostgreSQL's 63-character
        # identifier limit and be silently truncated.
        ForeignKeyConstraint(
            ["ministry_membership_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_role_qualification_membership_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ministry_role_id", "ministry_id"],
            ["ministry_role.id", "ministry_role.ministry_id"],
            name="fk_role_qualification_role_ministry",
            ondelete="RESTRICT",
        ),
        # One standing decision per membership/role pair.
        UniqueConstraint(
            "ministry_membership_id",
            "ministry_role_id",
            name="uq_role_qualification_membership_role",
        ),
        Index("ix_role_qualification_ministry_role_id", "ministry_role_id"),
        Index("ix_role_qualification_decided_by_person_id", "decided_by_person_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    ministry_membership_id: Mapped[int] = mapped_column(BigInteger)
    ministry_role_id: Mapped[int] = mapped_column(BigInteger)
    #: Carried so both composite foreign keys above route through one ministry.
    #: Not independently writable in practice: any inconsistent value is
    #: rejected, and neither parent can change its own ministry_id while this
    #: row points at it.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    is_qualified: Mapped[bool] = mapped_column(Boolean)
    decided_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True))
    #: Who decided. Kept on the row — rather than left to the audit log — because
    #: the ministry head's screen must show it without querying audit history
    #: (§3.2).
    decided_by_person_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("person.id", ondelete="RESTRICT")
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Each relationship names its own single foreign-key column explicitly.
    # Left to infer, both would claim the shared ministry_id column and the
    # mapper would have two relationships writing one column.
    membership: Mapped[MinistryMembership] = relationship(
        back_populates="role_qualifications",
        primaryjoin=(
            "RoleQualification.ministry_membership_id == MinistryMembership.id"
        ),
        foreign_keys="RoleQualification.ministry_membership_id",
    )
    role: Mapped[MinistryRole] = relationship(
        back_populates="role_qualifications",
        primaryjoin="RoleQualification.ministry_role_id == MinistryRole.id",
        foreign_keys="RoleQualification.ministry_role_id",
    )
    decided_by: Mapped[Person] = relationship(
        foreign_keys="RoleQualification.decided_by_person_id"
    )
