"""Scheduling input schema.

Implements the reviewed design in
``docs/architecture/scheduling-input-data-model.md`` (revision 2) and the
decision recorded in ``docs/adr/0002-sunday-conflict-sources-of-truth.md``.
Section numbers in the comments refer to the scheduling-input document.

The solver's *input* concepts, in two groups. The five original tables --
scheduling_period, event, staffing_requirement, availability,
existing_commitment -- describe what must be staffed and who could do it. The
structured **hard constraint configuration** tables added since sit alongside
them: membership_serving_limit (§4.4.1), membership_same_date_exclusion
(§4.4.2), member_group / member_group_member / member_group_event_limit, and
membership_support_requirement / membership_support_supporter. Schedule,
ScheduleVersion, Assignment, ShadowAssignment, AuditEvent and every *soft*
preference configuration remain later slices and appear nowhere here.

Conventions are inherited from :mod:`app.models.core` unchanged:

- ``bigint GENERATED ALWAYS AS IDENTITY`` primary keys.
- ``created_at`` / ``updated_at`` ``timestamptz`` on every table; a nullable
  timestamp for a binary state (``availability_locked_at``, ``cancelled_at``).
- Every foreign key ``ON DELETE RESTRICT``.
- Case-insensitive uniqueness as a unique index on ``lower(column)``.
- ``text`` + ``CHECK`` for closed value sets; no native PostgreSQL ``ENUM``,
  no JSONB.
- Same-ministry integrity enforced by composite foreign keys routed through a
  shared ``ministry_id`` column (core §7.2).

Calendar dates are ``Date``, never ``DateTime``: an event date is a civil day in
the church's timezone, and the whole Sunday-conflict rule is date equality, so a
timezone-shifted date would be a correctness bug (§4). There is deliberately no
``sunday_key``, ``week_key``, or derived Sunday column: a Saturday special event
must not consume the adjacent Sunday, and date equality already gives that
answer (§11).
"""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
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
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.core import Ministry, MinistryMembership, MinistryRole, Person

#: The only values ``event.event_kind`` may take.
EVENT_KIND_SUNDAY_SERVICE = "SUNDAY_SERVICE"
EVENT_KIND_SPECIAL = "SPECIAL"

#: The only values ``availability.availability_state`` may take. There is no
#: UNKNOWN / NO_RESPONSE value: absence of a row *is* no response (§8).
AVAILABILITY_AVAILABLE = "AVAILABLE"
AVAILABILITY_UNAVAILABLE = "UNAVAILABLE"
#: A feasible but lower-priority answer: "use me only if an ordinarily
#: AVAILABLE candidate cannot fill this position" (Task 52). Still a real,
#: explicit answer someone gave -- not a third flavor of no-response -- so it
#: is a stored state alongside the other two, never a fallback for a blank
#: cell (§9 stays about AVAILABLE/no-row, not this).
AVAILABILITY_BACKUP = "BACKUP"


class SchedulingPeriod(Base):
    """A window one ministry schedules for — commonly ~13 consecutive Sundays.

    Periods are not fixed calendar quarters: each ministry picks its own start
    and end Sundays, so Setup's Q4 need not line up with anyone else's.

    Only the *availability collection* stage of the approved workflow lives
    here, as ``availability_locked_at``. Draft / Review / Finalized / Amended
    describe a produced schedule, not this input container, and belong to the
    future ScheduleVersion — a period must be able to hold a finalized v1 and a
    draft v2 at once, which a single workflow column could not express (§3).

    V1 semantics: creating a period means availability is open. There is no
    separate pre-open state.

    Overlapping periods for one ministry are deliberately **not** forbidden by
    the database. An overlap is a workflow mistake rather than a corruption --
    every event still belongs to exactly one period -- so it is left to
    service-layer validation and warnings (§4).

    **One optional scheduling rule is configured here rather than per person**
    (Task 71): ``min_intervening_events``. It is the first rule whose scope is
    the *ministry and period* rather than a member or a pair, which is exactly
    why it is a column on this row and not a new table -- see the field's own
    comment.
    """

    __tablename__ = "scheduling_period"
    __table_args__ = (
        # Parent key for event's composite foreign key, which is what stops an
        # event under a Setup period from claiming to belong to AV.
        UniqueConstraint(
            "id", "ministry_id", name="uq_scheduling_period_id_ministry_id"
        ),
        CheckConstraint("start_date <= end_date", name="start_not_after_end"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        # Zero is not a gap rule, it is "consecutive assignments are allowed",
        # which is precisely what NULL already means. Permitting both would
        # give one fact two representations free to disagree -- the same
        # reasoning that keeps ``membership_serving_limit.max_assignments``
        # strictly positive with absence meaning "no maximum" (§4.4.1).
        CheckConstraint(
            "min_intervening_events IS NULL OR min_intervening_events > 0",
            name="min_intervening_events_positive",
        ),
        Index("ix_scheduling_period_ministry_id", "ministry_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    ministry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ministry.id", ondelete="RESTRICT")
    )
    #: A ministry-chosen label, e.g. "Setup Q4 2026" — not a derived quarter.
    name: Mapped[str] = mapped_column(Text)
    start_date: Mapped[datetime.date] = mapped_column(Date)
    end_date: Mapped[datetime.date] = mapped_column(Date)
    #: NULL = availability is open; set = locked, and when. Locking is a
    #: precondition for producing a draft, which is a service-layer rule: no
    #: constraint here can govern writes to the availability table (§3).
    availability_locked_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    #: How many of **this ministry's own events** must fall between two
    #: assignments of the same person in this period (Task 71). ``NULL`` is the
    #: ordinary case and means there is no such rule: the same person may serve
    #: two events in a row. ``1`` means one event must be skipped -- no
    #: consecutive assignments; ``2`` means two must be skipped, and so on.
    #:
    #: **Counted in events, never in days.** The sequence is this ministry's
    #: own non-cancelled events in chronological order, ordinary Sundays and
    #: ad-hoc special events alike; two events with nothing between them are
    #: consecutive whether they are seven days or one day apart. A day-based
    #: reading would be a different rule, and it would be wrong the first time
    #: a special event landed mid-week (§4.8).
    #:
    #: **Ministry- and period-scoped by construction.** A period belongs to one
    #: ministry, so a rule recorded here can no more reach another ministry's
    #: schedule than this period's staffing can. It is deliberately not a
    #: church-wide rest rule, and it is never consulted for, nor copied into, a
    #: later period -- like a serving limit, it expires with the period.
    #:
    #: **Hard, and not overridable** (requirements §6), for the same reason the
    #: serving maximum is not: it is applied as a solver constraint before any
    #: objective, as an absolute check in manual assignment, and as a
    #: finalization gate. A head who needs the placement changes this number
    #: first -- which is audited -- rather than working around it.
    min_intervening_events: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ministry: Mapped[Ministry] = relationship()
    events: Mapped[list[Event]] = relationship(back_populates="scheduling_period")


# One "Setup Q4 2026" per ministry, case-insensitively — matching how the core
# model treats ministry and role names.
Index(
    "uq_scheduling_period_ministry_id_name_lower",
    SchedulingPeriod.ministry_id,
    func.lower(SchedulingPeriod.name),
    unique=True,
)


class Event(Base):
    """A dated occasion a ministry staffs: an ordinary Sunday, or a special event.

    Generated Sundays are ordinary rows in this table. No recurrence rule is
    stored: the set is tiny and every Sunday needs its own staffing,
    availability and possible cancellation anyway, so a rule would have to be
    expanded into rows immediately and leave two representations of the same
    Sundays (§5).

    **Multiple events for one ministry on one date are structurally allowed.**
    The approved church-wide rule is one *ministry per person* per Sunday — a
    constraint on people, not on how many events a ministry may hold — so a
    morning and an evening service, each with its own crew, must remain
    possible. Guarding against accidentally generating a Sunday twice belongs
    to the generation operation, not to a universal database rule (§4).

    There are deliberately no start/end timestamps: the hard cross-ministry rule
    is date-level, and nothing in the product currently reads a time of day.
    """

    __tablename__ = "event"
    __table_args__ = (
        # Event and its period must agree on the ministry. ministry_id exists
        # here so the child tables can route their composite foreign keys
        # through it, which makes it a column two other tables depend on -- so
        # it must not be independently settable. Routing the period reference
        # through it means a row that disagrees with its period is rejected,
        # and the period cannot change its own ministry_id while an event
        # points at it (§4).
        #
        # Named explicitly: the convention's generated name for this composite
        # foreign key would exceed PostgreSQL's 63-character identifier limit.
        ForeignKeyConstraint(
            ["scheduling_period_id", "ministry_id"],
            ["scheduling_period.id", "scheduling_period.ministry_id"],
            name="fk_event_period_ministry",
            ondelete="RESTRICT",
        ),
        # Parent key for staffing_requirement and availability.
        UniqueConstraint("id", "ministry_id", name="uq_event_id_ministry_id"),
        # Parent key for schedule_version_requirement, added additively by the
        # schedule-output slice (schedule-output-data-model.md §1, §9). It is a
        # new parent key exactly like the one above, changing no column, no
        # semantics and no existing constraint. It is what lets the database
        # guarantee that a version requirement's event belongs to the
        # schedule's scheduling period; the ministry key above serves the
        # separate ministry integrity chain, and both are needed.
        UniqueConstraint(
            "id", "scheduling_period_id", name="uq_event_id_scheduling_period_id"
        ),
        CheckConstraint(
            "event_kind IN ('SUNDAY_SERVICE', 'SPECIAL')", name="event_kind_valid"
        ),
        # A special event must be named ("Community Families"); an ordinary
        # Sunday needs no name, and requiring one would produce noise like
        # "Sunday 2026-10-04" in every row.
        CheckConstraint(
            "event_kind <> 'SPECIAL'"
            " OR (name IS NOT NULL AND length(btrim(name)) > 0)",
            name="special_requires_name",
        ),
        Index("ix_event_scheduling_period_id", "scheduling_period_id"),
        Index("ix_event_event_date", "event_date"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    scheduling_period_id: Mapped[int] = mapped_column(BigInteger)
    #: Carried so child tables route their composite foreign keys through one
    #: ministry. Pinned to the period's ministry by the constraint above.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    #: The civil date. Whether it falls on a Sunday is simply what the date
    #: says; nothing derives a Sunday from a nearby day.
    event_date: Mapped[datetime.date] = mapped_column(Date)
    event_kind: Mapped[str] = mapped_column(Text)
    #: Required for SPECIAL events, unused for ordinary Sundays.
    name: Mapped[str | None] = mapped_column(Text)
    #: A cancelled event keeps its row and the availability already gathered
    #: against it; cancelled events are excluded from solver input (§13).
    cancelled_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # This is the one composite foreign key in the slice that a relationship can
    # manage safely: it is the only relationship on Event that writes
    # ministry_id, so assigning ``event.scheduling_period`` correctly populates
    # both scheduling_period_id and ministry_id. Contrast with
    # StaffingRequirement and Availability below, where two composite keys share
    # the column and the ORM must not manage it.
    scheduling_period: Mapped[SchedulingPeriod] = relationship(
        back_populates="events"
    )
    staffing_requirements: Mapped[list[StaffingRequirement]] = relationship(
        back_populates="event",
        primaryjoin="Event.id == StaffingRequirement.event_id",
        foreign_keys="StaffingRequirement.event_id",
    )
    availabilities: Mapped[list[Availability]] = relationship(
        back_populates="event",
        primaryjoin="Event.id == Availability.event_id",
        foreign_keys="Availability.event_id",
    )


class StaffingRequirement(Base):
    """How many people one ministry role needs at one event.

    These rows are the **authoritative per-event snapshot**. There is no shared
    mutable template: changing Setup's staffing from 5 to 6 edits future events'
    rows and cannot reach a past event, because each event owns its own rows and
    no common parent exists to edit. That is how the approved requirement — a
    historical event must not silently change — is met structurally rather than
    by a rule someone has to remember (§6).

    A per-ministry default, if one is ever wanted, is a later convenience that
    *materializes copies* into events at creation time. It must never become
    something the solver reads, or history becomes mutable again through the
    back door. No such table exists in this slice.

    **``ministry_id`` must be set explicitly when writing a row.** It is shared
    by both composite foreign keys below, so the ORM relationships deliberately
    manage only their own single id column (see the relationships). A row whose
    ministry_id disagrees with either parent is rejected by the database.
    """

    __tablename__ = "staffing_requirement"
    __table_args__ = (
        # A Setup event cannot require an AV role: both foreign keys route
        # through this row's single ministry_id, so a mismatched pair cannot be
        # inserted at all -- not by the application, not by a backfill, not by
        # hand in psql (§7).
        ForeignKeyConstraint(
            ["event_id", "ministry_id"],
            ["event.id", "event.ministry_id"],
            name="fk_staffing_requirement_event_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ministry_role_id", "ministry_id"],
            ["ministry_role.id", "ministry_role.ministry_id"],
            name="fk_staffing_requirement_role_ministry",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "event_id",
            "ministry_role_id",
            name="uq_staffing_requirement_event_id_ministry_role_id",
        ),
        # A requirement row asserts a real need. "This role is not needed at
        # this event" is expressed by the absence of a row, so a zero would be a
        # second representation of the same thing (§7).
        CheckConstraint("required_count > 0", name="required_count_positive"),
        Index("ix_staffing_requirement_ministry_role_id", "ministry_role_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    event_id: Mapped[int] = mapped_column(BigInteger)
    ministry_role_id: Mapped[int] = mapped_column(BigInteger)
    #: Shared by both composite foreign keys above; not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    required_count: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Each relationship names its own single foreign-key column explicitly.
    # Left to infer, both would claim the shared ministry_id column and the
    # mapper would have two relationships writing one column.
    event: Mapped[Event] = relationship(
        back_populates="staffing_requirements",
        primaryjoin="StaffingRequirement.event_id == Event.id",
        foreign_keys="StaffingRequirement.event_id",
    )
    ministry_role: Mapped[MinistryRole] = relationship(
        primaryjoin="StaffingRequirement.ministry_role_id == MinistryRole.id",
        foreign_keys="StaffingRequirement.ministry_role_id",
    )


class Availability(Base):
    """One person's answer about one event, in the context of one ministry.

    Four situations, and only three of them are rows (§8, widened by Task 52):

    - no row                              -> no response
    - row, availability_state=AVAILABLE   -> they said they can serve
    - row, availability_state=BACKUP      -> they can serve, but only if an
      ordinarily AVAILABLE candidate cannot fill the position (Task 52)
    - row, availability_state=UNAVAILABLE -> they said they cannot

    BACKUP is still a feasible answer, never a blocker: the solver's hard
    eligibility rules treat it exactly like AVAILABLE (it just costs a soft
    preference), and manual assignment / finalization readiness never flag it
    -- only UNAVAILABLE does that (:mod:`app.services.assignment_policy`).

    There is no UNKNOWN / NO_RESPONSE value. Storing one would mean a row per
    member per event -- hundreds of rows of pure absence per period, maintained
    as people join and leave -- and would give absence two representations free
    to disagree.

    The link is to :class:`~app.models.core.MinistryMembership`, not Person: a
    person in both Setup and AV may answer differently for each on the same
    Sunday, which a person-level row could not express.

    **Setup's "blank means available" policy is not encoded here.** The importer
    writes a row only for an explicit mark, leaving a blank cell as no row; the
    assumption about what silence means is applied when building solver input,
    and later becomes per-ministry configuration (§9). Storing blanks as
    AVAILABLE would record a decision the person never made.

    **``ministry_id`` must be set explicitly when writing a row**, for the same
    reason as :class:`StaffingRequirement`.
    """

    __tablename__ = "availability"
    __table_args__ = (
        # A Setup membership cannot answer for an AV event: both foreign keys
        # route through this row's single ministry_id (§8).
        ForeignKeyConstraint(
            ["ministry_membership_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_availability_membership_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["event_id", "ministry_id"],
            ["event.id", "event.ministry_id"],
            name="fk_availability_event_ministry",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "ministry_membership_id",
            "event_id",
            name="uq_availability_ministry_membership_id_event_id",
        ),
        CheckConstraint(
            "availability_state IN ('AVAILABLE', 'BACKUP', 'UNAVAILABLE')",
            name="availability_state_valid",
        ),
        Index("ix_availability_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    ministry_membership_id: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[int] = mapped_column(BigInteger)
    #: Shared by both composite foreign keys above; not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    availability_state: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ministry_membership: Mapped[MinistryMembership] = relationship(
        primaryjoin=(
            "Availability.ministry_membership_id == MinistryMembership.id"
        ),
        foreign_keys="Availability.ministry_membership_id",
    )
    event: Mapped[Event] = relationship(
        back_populates="availabilities",
        primaryjoin="Availability.event_id == Event.id",
        foreign_keys="Availability.event_id",
    )


class ExistingCommitment(Base):
    """A known commitment that blocks one person church-wide on one date.

    This is the explicit half of the Sunday-conflict rule recorded in ADR 0002:
    a commitment this system does not manage — already serving AV, Admin duty,
    preaching, another known church responsibility — entered or imported so the
    scheduler can treat it as a hard block.

    **It is not a personal-absence or general availability table.** Ordinary
    personal availability ("I can't do the 12th", "I'm away that weekend") is
    represented through :class:`Availability`, in the ministry context where it
    was given. Filing absences here would create a second competing
    representation of ordinary availability with no rule for which one wins
    (§10).

    The block is **church-wide**, so it references the canonical Person rather
    than a membership. ``source_ministry_id`` is *provenance, not scope*: it
    records why the person is blocked and never narrows who the block applies
    to. A commitment sourced from AV blocks Setup exactly as it blocks anything
    else.

    There is deliberately **no assignment_id**. Future finalized assignments are
    the other source of conflict information and are never copied into this
    table; the blocked set is the union of the two, computed at query /
    solver-input time (ADR 0002).
    """

    __tablename__ = "existing_commitment"
    __table_args__ = (
        # NULLS NOT DISTINCT so a re-run import cannot insert the same block
        # twice, including for the rows with no source ministry -- plain UNIQUE
        # treats each NULL as distinct and would let unlimited duplicates
        # through. PostgreSQL 15+; the development branch is 18.6 (§4).
        #
        # Named explicitly: the convention's generated name would exceed
        # PostgreSQL's 63-character identifier limit.
        UniqueConstraint(
            "person_id",
            "commitment_date",
            "source_ministry_id",
            name="uq_existing_commitment_person_date_source",
            postgresql_nulls_not_distinct=True,
        ),
        # Every block must be explicable. One with neither a source ministry nor
        # a reason is an unexplained "no" that nobody can later audit or
        # overturn (§4).
        CheckConstraint(
            "source_ministry_id IS NOT NULL"
            " OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="provenance_required",
        ),
        Index(
            "ix_existing_commitment_person_id_commitment_date",
            "person_id",
            "commitment_date",
        ),
        Index("ix_existing_commitment_commitment_date", "commitment_date"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    person_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("person.id", ondelete="RESTRICT")
    )
    #: A plain date. A Saturday commitment blocks Saturday, not the neighbouring
    #: Sunday -- which is why no Sunday key is derived anywhere (§11).
    commitment_date: Mapped[datetime.date] = mapped_column(Date)
    #: Provenance only. NULL when the responsibility is not a ministry in this
    #: system (preaching, a church duty modelled nowhere else).
    source_ministry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ministry.id", ondelete="RESTRICT")
    )
    reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    person: Mapped[Person] = relationship()
    source_ministry: Mapped[Ministry | None] = relationship()


class MembershipServingLimit(Base):
    """The most assignments one member may receive in one scheduling period.

    The first *person-specific structured scheduling constraint*
    (requirements §4.4.1). It answers a question availability cannot: not
    "can this person serve on this date?" but "how many times, at most,
    during this period?"

    **Scope is (MinistryMembership x SchedulingPeriod), and that is the whole
    point.** The link is to a membership, not a Person, for the same reason
    :class:`Availability` is: the same human may accept four AV Sundays and
    two Setup Sundays in one quarter, and a person-level row could not say so.
    A limit recorded here binds *this ministry's* schedule only -- an AV head
    setting a limit can no more affect Setup's schedule than they can edit
    Setup's availability. A church-wide total-serving cap would be a different
    concept with different ownership, and is deliberately not this table
    (requirements §4.4.1).

    **Absence means no limit.** There is no row meaning "unlimited" and no
    sentinel value: clearing the constraint deletes the row, exactly as
    clearing an availability answer does. That keeps "no limit" with one
    representation rather than two free to disagree.

    **Period-scoped, and it expires with the period.** A limit is never
    consulted for, nor copied into, a later period: someone who could serve
    four times this quarter has said nothing about the next one. Nothing here
    carries forward, and no "copy last period" behaviour exists.

    **Hard, and deliberately not overridable** (requirements §6). Every other
    scheduling blocker a head may bypass with a reason describes a fact about
    the world; this one records what a volunteer said they could manage, and
    only they can revise it. The head raises the recorded number after
    agreeing it with the person -- an audited change to this row -- rather
    than overriding the rule. No soft preference may exceed it either: the
    solver applies it as a constraint before any optimisation pass.

    **``ministry_id`` must be set explicitly when writing a row**, for the same
    reason as :class:`Availability` and :class:`StaffingRequirement`: it is
    shared by both composite foreign keys below, which is what makes a
    Setup membership physically unable to carry a limit for an AV period.

    Only the *hard maximum* is stored. A soft "prefer about N" preference is
    designed but not implemented, and no column is reserved for it here --
    a nullable column nothing reads would be a claim that the feature exists.
    """

    __tablename__ = "membership_serving_limit"
    __table_args__ = (
        # A Setup membership cannot carry a limit for an AV period: both
        # foreign keys route through this row's single ministry_id, the same
        # pattern availability and staffing use (core §7.2). Cross-ministry
        # scope is therefore a database guarantee, not only a service check.
        ForeignKeyConstraint(
            ["ministry_membership_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_membership_serving_limit_membership_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scheduling_period_id", "ministry_id"],
            ["scheduling_period.id", "scheduling_period.ministry_id"],
            name="fk_membership_serving_limit_period_ministry",
            ondelete="RESTRICT",
        ),
        # One effective limit per member per period. A second row would be a
        # second answer to a question that has one.
        UniqueConstraint(
            "ministry_membership_id",
            "scheduling_period_id",
            name="uq_membership_serving_limit_membership_period",
        ),
        # Zero is not a limit, it is "never schedule this person", which is
        # what deactivating a membership or answering UNAVAILABLE says --
        # said once, in the place that already means it.
        CheckConstraint("max_assignments > 0", name="max_assignments_positive"),
        Index(
            "ix_membership_serving_limit_scheduling_period_id",
            "scheduling_period_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    ministry_membership_id: Mapped[int] = mapped_column(BigInteger)
    scheduling_period_id: Mapped[int] = mapped_column(BigInteger)
    #: Shared by both composite foreign keys above; not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    #: The most assignments this membership may hold in this period. Always
    #: positive; absence of the row means no maximum at all.
    max_assignments: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ministry_membership: Mapped[MinistryMembership] = relationship(
        primaryjoin=(
            "MembershipServingLimit.ministry_membership_id"
            " == MinistryMembership.id"
        ),
        foreign_keys="MembershipServingLimit.ministry_membership_id",
    )
    scheduling_period: Mapped[SchedulingPeriod] = relationship(
        primaryjoin=(
            "MembershipServingLimit.scheduling_period_id == SchedulingPeriod.id"
        ),
        foreign_keys="MembershipServingLimit.scheduling_period_id",
    )


class MembershipSameDateExclusion(Base):
    """Two members of one ministry who must not both serve on one calendar date.

    The second *person-specific structured scheduling constraint*, and the
    first one that is about a **pair** rather than an individual
    (requirements §4.4.2). It answers a question neither availability nor
    :class:`MembershipServingLimit` can: not "may this person serve on this
    date?" and not "how many times this period?", but "may these two both be
    on the roster for the same day?"

    **Neutral by construction.** The row says two memberships are *linked* for
    scheduling and nothing else. There is no relationship type, no spouse,
    household, sibling or housemate column, and no free-text rationale: the
    scheduler needs the exclusion, not the circumstance behind it, and a
    personal fact stored without a use is a privacy cost with no benefit
    (requirements §4.4.2, §4.7). Nothing infers a link either -- not from
    surnames, not from an address, not from who has historically served
    together. Absence of a row *is* "no rule".

    **Scope is (MinistryMembership x MinistryMembership x SchedulingPeriod),
    and every part of that is load-bearing.** The link is between two
    *memberships*, not two people, so a Kids Ministry head configuring an
    exclusion cannot touch either person's Setup, AV or Nursery schedule --
    the same boundary :class:`Availability` and :class:`MembershipServingLimit`
    draw. A church-wide linked-person rule would be a different concept with
    different ownership, and is deliberately not this table.

    **Period-scoped, and it expires with the period.** Like a serving limit, a
    pair exclusion is never consulted for, nor copied into, a later period. Two
    people coordinating this quarter have said nothing about the next one, and
    no "copy last period" behaviour exists.

    **Same calendar date, not merely the same event.** The rule the solver,
    manual assignment and finalization readiness all enforce is date equality
    on ``event.event_date``: a period may hold a morning and an evening service
    on one Sunday, and putting one linked member at each would break the rule
    just as squarely as putting them both in one event. Nothing here stores a
    date -- the exclusion applies to every date in the period -- which is
    exactly why no date column exists to disagree with the events.

    **Hard, and deliberately not overridable** (requirements §6). Every
    ``override_reason`` in this system bypasses a bounded blocker that
    describes a *fact about the world*; this row records an arrangement two
    volunteers made, and only they can revise it. A head who needs the pairing
    removes or changes this row -- an audited act -- and then makes the
    assignment.

    **The pair is unordered, and canonical order is a database guarantee.**
    ``membership_a_id < membership_b_id`` is a CHECK, so A+B and B+A cannot
    both exist and a membership cannot be paired with itself; the uniqueness
    constraint then makes one effective exclusion per unordered pair per
    period. Callers canonicalize through
    :mod:`app.services.same_date_exclusion` rather than sorting by hand.

    **``ministry_id`` must be set explicitly when writing a row**, for the
    same reason as :class:`Availability`, :class:`StaffingRequirement` and
    :class:`MembershipServingLimit`: it is the single column all three
    composite foreign keys route through, which is what makes it physically
    impossible to link a Kids membership to a Setup membership, or to hang
    either off another ministry's scheduling period.
    """

    __tablename__ = "membership_same_date_exclusion"
    __table_args__ = (
        # Three composite foreign keys, one shared ministry_id. Both
        # memberships and the period must therefore agree on the ministry, so
        # a cross-ministry pair is rejected by PostgreSQL and not only by the
        # service layer (core §7.2).
        #
        # Named explicitly: the convention's generated names for these would
        # exceed PostgreSQL's 63-character identifier limit.
        ForeignKeyConstraint(
            ["membership_a_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_same_date_exclusion_membership_a_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["membership_b_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_same_date_exclusion_membership_b_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scheduling_period_id", "ministry_id"],
            ["scheduling_period.id", "scheduling_period.ministry_id"],
            name="fk_same_date_exclusion_period_ministry",
            ondelete="RESTRICT",
        ),
        # One effective exclusion per unordered pair per period. Reversed
        # duplicates cannot reach this constraint at all, because the CHECK
        # below already refuses the reversed row -- the two work together, and
        # neither alone is enough.
        UniqueConstraint(
            "membership_a_id",
            "membership_b_id",
            "scheduling_period_id",
            name="uq_same_date_exclusion_pair_period",
        ),
        # Canonical order, and the self-pairing ban, in one predicate. A
        # strict ``<`` makes A+B and B+A the same row by construction rather
        # than by a service remembering to sort, and makes "linked to
        # themselves" -- which would mean a person may never serve at all --
        # unrepresentable.
        CheckConstraint(
            "membership_a_id < membership_b_id",
            name="pair_canonically_ordered",
        ),
        Index(
            "ix_same_date_exclusion_scheduling_period_id",
            "scheduling_period_id",
        ),
        # "Which memberships is this one linked to in this period?" is asked
        # from both sides -- manual assignment asks it for the member being
        # assigned, who may be either half of the pair. The unique constraint
        # above already indexes the ``membership_a_id`` side.
        Index("ix_same_date_exclusion_membership_b_id", "membership_b_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    #: The numerically lower membership id of the unordered pair.
    membership_a_id: Mapped[int] = mapped_column(BigInteger)
    #: The numerically higher membership id of the unordered pair.
    membership_b_id: Mapped[int] = mapped_column(BigInteger)
    scheduling_period_id: Mapped[int] = mapped_column(BigInteger)
    #: Shared by all three composite foreign keys above; not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Each relationship names its own single foreign-key column explicitly, as
    # in StaffingRequirement and Availability: left to infer, all three would
    # claim the shared ministry_id column.
    membership_a: Mapped[MinistryMembership] = relationship(
        primaryjoin=(
            "MembershipSameDateExclusion.membership_a_id == MinistryMembership.id"
        ),
        foreign_keys="MembershipSameDateExclusion.membership_a_id",
    )
    membership_b: Mapped[MinistryMembership] = relationship(
        primaryjoin=(
            "MembershipSameDateExclusion.membership_b_id == MinistryMembership.id"
        ),
        foreign_keys="MembershipSameDateExclusion.membership_b_id",
    )
    scheduling_period: Mapped[SchedulingPeriod] = relationship(
        primaryjoin=(
            "MembershipSameDateExclusion.scheduling_period_id"
            " == SchedulingPeriod.id"
        ),
        foreign_keys="MembershipSameDateExclusion.scheduling_period_id",
    )


class MemberGroup(Base):
    """A named category of a ministry's own members.

    The **membership-group** half of the fourth structured hard scheduling
    constraint (Task 74): *at most N members of group G may serve one event.*
    This table is only the category and who is in it; the number lives on
    :class:`MemberGroupEventLimit`, because a category is a standing fact about
    people while a cap is a rule one scheduling period adopts.

    **Ministry-scoped, and a plain label.** Groups are data, exactly as
    :class:`~app.models.core.MinistryRole` is: nothing in the application
    branches on a group's name, no group is created by code, and no group name
    appears in application logic. A ministry that wants a different category
    creates one; a ministry that wants none has none.

    **Nothing is inferred.** Membership of a group is recorded because a head
    said so, never derived from an age, a roster column this system does not
    hold, historical assignments or anything else. Absence of a row *is* "not
    in this group".

    **No meaning beyond scheduling is stored.** There is no description of why
    the category exists, no eligibility semantics and no ordering: the
    scheduler needs to count members of a group at an event, and the values
    that reach it must not be able to tell it anything else (requirements
    §4.7).
    """

    __tablename__ = "member_group"
    __table_args__ = (
        # Parent key for member_group_member and member_group_event_limit,
        # which route their composite foreign keys through one ministry_id --
        # the pattern the whole schema uses to make cross-ministry
        # configuration unrepresentable (core §7.2).
        UniqueConstraint("id", "ministry_id", name="uq_member_group_id_ministry_id"),
        CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        Index("ix_member_group_ministry_id", "ministry_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    ministry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ministry.id", ondelete="RESTRICT")
    )
    #: A ministry-chosen label. Deliberately plain: it is shown to the head who
    #: configured it and never interpreted.
    name: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    ministry: Mapped[Ministry] = relationship()


# One group of a given name per ministry, case-insensitively — the same
# treatment ministry and role names get.
Index(
    "uq_member_group_ministry_id_name_lower",
    MemberGroup.ministry_id,
    func.lower(MemberGroup.name),
    unique=True,
)


class MemberGroupMember(Base):
    """One membership's place in one :class:`MemberGroup`.

    A plain join row and nothing more: no start date, no strength, no reason.
    Absence of a row is "not in this group", which keeps that fact with one
    representation rather than two free to disagree — the same choice
    :class:`Availability` makes about no-response.

    **``ministry_id`` must be set explicitly when writing a row**, for the same
    reason as :class:`Availability` and :class:`MembershipServingLimit`: it is
    shared by both composite foreign keys below, which is what makes it
    physically impossible to put a Kids membership into a Setup group.
    """

    __tablename__ = "member_group_member"
    __table_args__ = (
        ForeignKeyConstraint(
            ["member_group_id", "ministry_id"],
            ["member_group.id", "member_group.ministry_id"],
            name="fk_member_group_member_group_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ministry_membership_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_member_group_member_membership_ministry",
            ondelete="RESTRICT",
        ),
        # One membership is in a group once. A second row would be a second
        # answer to a question that has one.
        UniqueConstraint(
            "member_group_id",
            "ministry_membership_id",
            name="uq_member_group_member_group_membership",
        ),
        # "Which groups is this membership in?" is asked for every candidate
        # when solver input is built; the unique constraint above indexes only
        # the other direction.
        Index(
            "ix_member_group_member_ministry_membership_id",
            "ministry_membership_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    member_group_id: Mapped[int] = mapped_column(BigInteger)
    ministry_membership_id: Mapped[int] = mapped_column(BigInteger)
    #: Shared by both composite foreign keys above; not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    member_group: Mapped[MemberGroup] = relationship(
        primaryjoin="MemberGroupMember.member_group_id == MemberGroup.id",
        foreign_keys="MemberGroupMember.member_group_id",
    )
    ministry_membership: Mapped[MinistryMembership] = relationship(
        primaryjoin=(
            "MemberGroupMember.ministry_membership_id == MinistryMembership.id"
        ),
        foreign_keys="MemberGroupMember.ministry_membership_id",
    )


class MemberGroupEventLimit(Base):
    """The most members of one :class:`MemberGroup` that may serve one event.

    The fourth *structured hard scheduling constraint*, and the first one about
    **an event's composition** rather than about a person, a pair or a
    sequence. It answers a question none of the others can: not "may this
    person serve?", not "how many times this period?", not "may these two share
    a day?" and not "how soon again?", but *"how many of these people may be on
    one crew at once?"*

    **Counted per event, and each member counted once whatever role they
    serve.** A group's members hold ordinary roles like anybody else; the rule
    counts *people from the group present at the event*, so somebody leading
    counts exactly as much as somebody not.

    **Scope is (MemberGroup x SchedulingPeriod).** The group belongs to one
    ministry and so does the period, so a cap recorded here binds that
    ministry's schedule for that period and nothing else. Like a serving limit
    and a pair exclusion, it is never consulted for, nor copied into, a later
    period: a ministry that wants the rule next quarter adopts it again, which
    is one audited row rather than a rule that quietly outlives the decision.

    **Absence means no cap**, and there is no stored value meaning "unlimited".
    Zero is excluded by the CHECK for the same reason a serving maximum of zero
    is: "no member of this group may serve at all" is what deactivating a
    membership or withholding a qualification already says, said once in the
    place that means it.

    **Hard, and deliberately not overridable** (requirements §6). It is applied
    as a solver constraint before any objective, as an absolute check in manual
    assignment, and as a finalization gate. A head who needs a third member of
    the group on one event raises the number here — which is audited — rather
    than working around the rule for one Sunday.

    **``ministry_id`` must be set explicitly when writing a row**, for the same
    reason as every other configuration row in this module.
    """

    __tablename__ = "member_group_event_limit"
    __table_args__ = (
        ForeignKeyConstraint(
            ["member_group_id", "ministry_id"],
            ["member_group.id", "member_group.ministry_id"],
            name="fk_member_group_event_limit_group_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scheduling_period_id", "ministry_id"],
            ["scheduling_period.id", "scheduling_period.ministry_id"],
            name="fk_member_group_event_limit_period_ministry",
            ondelete="RESTRICT",
        ),
        # One effective cap per group per period.
        UniqueConstraint(
            "member_group_id",
            "scheduling_period_id",
            name="uq_member_group_event_limit_group_period",
        ),
        CheckConstraint("max_per_event > 0", name="max_per_event_positive"),
        Index(
            "ix_member_group_event_limit_scheduling_period_id",
            "scheduling_period_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    member_group_id: Mapped[int] = mapped_column(BigInteger)
    scheduling_period_id: Mapped[int] = mapped_column(BigInteger)
    #: Shared by both composite foreign keys above; not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    #: The most members of this group that may serve one event in this period.
    #: Always positive; absence of the row means no cap at all.
    max_per_event: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    member_group: Mapped[MemberGroup] = relationship(
        primaryjoin="MemberGroupEventLimit.member_group_id == MemberGroup.id",
        foreign_keys="MemberGroupEventLimit.member_group_id",
    )
    scheduling_period: Mapped[SchedulingPeriod] = relationship(
        primaryjoin=(
            "MemberGroupEventLimit.scheduling_period_id == SchedulingPeriod.id"
        ),
        foreign_keys="MemberGroupEventLimit.scheduling_period_id",
    )


class MembershipSupportRequirement(Base):
    """One member who may serve an event only if others serve it too.

    The fifth *structured hard scheduling constraint* (Task 74). It answers a
    question none of the others can: not whether somebody may serve, but *on
    what condition* — this membership may hold an assignment at an event only
    when at least ``min_supporters`` of a configured set of other memberships
    hold one at the **same event**.

    **Only the scheduling fact is stored.** There is no reason, no category and
    no relationship type: not transportation, not a lift, not family, not
    anything. The scheduler needs to know that the condition exists and who
    satisfies it; *why* the ministry agreed it is a private arrangement between
    people, and a personal fact stored without a use is a privacy cost with no
    benefit (requirements §4.7). Nothing is inferred either — not from
    surnames, not from addresses, not from who has historically served
    together.

    **Same event, not the same date.** The supporters must be on the same crew,
    which is a stronger and different statement from the date-level rule
    :class:`MembershipSameDateExclusion` makes. A period may hold a morning and
    an evening service on one Sunday, and somebody at the other one is not
    present here.

    **Directional, and the subject cannot satisfy their own requirement.** The
    rule constrains the subject only; the supporters are unconstrained by it
    and may serve any event, in any role, with or without the subject. That the
    subject is not one of their own supporters is a database guarantee, through
    the CHECK on :class:`MembershipSupportSupporter`.

    **Scope is (MinistryMembership x SchedulingPeriod)**, like a serving limit:
    the requirement binds one ministry's schedule for one period, expires with
    the period, and is never copied forward.

    **Hard, and deliberately not overridable** (requirements §6) — a solver
    constraint, an absolute check in manual assignment, and a finalization
    gate. A head who needs the placement changes or clears the requirement
    here, which is audited.

    **``ministry_id`` must be set explicitly when writing a row**, for the same
    reason as every other configuration row in this module.
    """

    __tablename__ = "membership_support_requirement"
    __table_args__ = (
        ForeignKeyConstraint(
            ["subject_membership_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_support_requirement_subject_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["scheduling_period_id", "ministry_id"],
            ["scheduling_period.id", "scheduling_period.ministry_id"],
            name="fk_support_requirement_period_ministry",
            ondelete="RESTRICT",
        ),
        # One effective requirement per subject per period.
        UniqueConstraint(
            "subject_membership_id",
            "scheduling_period_id",
            name="uq_support_requirement_subject_period",
        ),
        # Parent key for the supporter rows, carrying the subject with it so
        # the "a supporter is not the subject" CHECK there has both ids on one
        # row to compare -- a database guarantee rather than a service rule.
        UniqueConstraint(
            "id",
            "subject_membership_id",
            "ministry_id",
            name="uq_support_requirement_id_subject_ministry",
        ),
        # Zero supporters is not a requirement, it is the absence of one --
        # which is exactly what having no row already says.
        CheckConstraint("min_supporters > 0", name="min_supporters_positive"),
        Index(
            "ix_support_requirement_scheduling_period_id",
            "scheduling_period_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    #: The constrained membership. Nothing about them is recorded beyond this.
    subject_membership_id: Mapped[int] = mapped_column(BigInteger)
    scheduling_period_id: Mapped[int] = mapped_column(BigInteger)
    #: Shared by both composite foreign keys above; not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    #: How many of the configured supporters must serve the same event.
    #: Always positive; one is the ordinary value.
    min_supporters: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    subject_membership: Mapped[MinistryMembership] = relationship(
        primaryjoin=(
            "MembershipSupportRequirement.subject_membership_id"
            " == MinistryMembership.id"
        ),
        foreign_keys="MembershipSupportRequirement.subject_membership_id",
    )
    scheduling_period: Mapped[SchedulingPeriod] = relationship(
        primaryjoin=(
            "MembershipSupportRequirement.scheduling_period_id"
            " == SchedulingPeriod.id"
        ),
        foreign_keys="MembershipSupportRequirement.scheduling_period_id",
    )
    supporters: Mapped[list[MembershipSupportSupporter]] = relationship(
        back_populates="support_requirement",
        primaryjoin=(
            "MembershipSupportRequirement.id"
            " == MembershipSupportSupporter.support_requirement_id"
        ),
        foreign_keys="MembershipSupportSupporter.support_requirement_id",
    )


class MembershipSupportSupporter(Base):
    """One membership approved to satisfy a :class:`MembershipSupportRequirement`.

    The approved set, one row per member, and nothing else: no ordering, no
    preference, no reason. Any one of them counts exactly as much as any other,
    which is what makes ``min_supporters`` a plain count.

    **Being a supporter constrains nobody.** A supporter is free to serve, or
    not, at any event; the rule only ever refuses the *subject* a placement.
    Nothing about their own schedule changes by appearing here.

    **``subject_membership_id`` is carried deliberately**, and is pinned to the
    parent requirement by the composite foreign key below. It exists so the
    CHECK can state, in the database, that a supporter is never the subject --
    the structural form of "the subject cannot satisfy their own requirement".

    **``ministry_id`` must be set explicitly when writing a row**, for the same
    reason as every other configuration row in this module.
    """

    __tablename__ = "membership_support_supporter"
    __table_args__ = (
        # Routed through the parent's (id, subject_membership_id, ministry_id)
        # key rather than its id alone, so the subject copied onto this row
        # cannot disagree with the requirement it belongs to.
        ForeignKeyConstraint(
            ["support_requirement_id", "subject_membership_id", "ministry_id"],
            [
                "membership_support_requirement.id",
                "membership_support_requirement.subject_membership_id",
                "membership_support_requirement.ministry_id",
            ],
            name="fk_support_supporter_requirement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["supporter_membership_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_support_supporter_membership_ministry",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "support_requirement_id",
            "supporter_membership_id",
            name="uq_support_supporter_requirement_member",
        ),
        # The subject cannot satisfy their own requirement. A CHECK rather than
        # a service rule, because a self-supporting row would make the whole
        # constraint vacuous in a way nothing downstream would notice.
        CheckConstraint(
            "supporter_membership_id <> subject_membership_id",
            name="supporter_is_not_subject",
        ),
        Index(
            "ix_support_supporter_supporter_membership_id",
            "supporter_membership_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    support_requirement_id: Mapped[int] = mapped_column(BigInteger)
    #: Copied from the parent and pinned to it by the composite foreign key
    #: above; present only so the CHECK has both ids on one row.
    subject_membership_id: Mapped[int] = mapped_column(BigInteger)
    supporter_membership_id: Mapped[int] = mapped_column(BigInteger)
    #: Shared by both composite foreign keys above; not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    support_requirement: Mapped[MembershipSupportRequirement] = relationship(
        back_populates="supporters",
        primaryjoin=(
            "MembershipSupportSupporter.support_requirement_id"
            " == MembershipSupportRequirement.id"
        ),
        foreign_keys="MembershipSupportSupporter.support_requirement_id",
    )
    supporter_membership: Mapped[MinistryMembership] = relationship(
        primaryjoin=(
            "MembershipSupportSupporter.supporter_membership_id"
            " == MinistryMembership.id"
        ),
        foreign_keys="MembershipSupportSupporter.supporter_membership_id",
    )
