"""Schedule output and versioning schema.

Implements the reviewed design in
``docs/architecture/schedule-output-data-model.md`` (revision 2) and the
decision recorded in ``docs/adr/0003-authoritative-schedule-version.md``.
Section numbers in the comments refer to the schedule-output document.

Four tables: schedule, schedule_version, schedule_version_requirement,
assignment. The solver, the version-creation and finalization services, the
staleness comparison, the authoritative-version query, AuditEvent,
ConstraintConfiguration, PreferenceConfiguration and ShadowAssignment are later
slices and appear nowhere here (§20).

Conventions are inherited from :mod:`app.models.core` and
:mod:`app.models.scheduling_input` unchanged:

- ``bigint GENERATED ALWAYS AS IDENTITY`` primary keys.
- ``created_at`` / ``updated_at`` ``timestamptz`` on every table — except
  ``schedule_version_requirement``, which is immutable and therefore carries
  ``created_at`` only (§3, decision 10).
- Every foreign key ``ON DELETE RESTRICT``.
- ``text`` + ``CHECK`` for closed value sets; no native PostgreSQL ``ENUM``,
  no JSONB.
- Period and ministry integrity enforced by composite foreign keys routed
  through shared columns (core §7.2, §9 here).
- Explicit constraint names wherever the metadata naming convention would
  exceed PostgreSQL's 63-character identifier limit. ``schedule_version_requirement``
  is 28 characters on its own, so most of its constraints are named by hand.

Two things this module deliberately does **not** contain, both load-bearing:

- **No stored authoritative-version pointer.** No ``current_version_id``, no
  ``finalized_version_id``, no ``is_authoritative``. The authoritative version
  is the highest-numbered ``FINALIZED`` version of a schedule, derived by query
  (ADR 0003, §15).
- **No trigger or ORM listener anywhere.** Version immutability (§7) and the
  requirement snapshot's write-once rule (§8) are service-layer invariants. The
  database enforces structure, not lifecycle.
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
from app.models.core import MinistryMembership, MinistryRole
from app.models.scheduling_input import Event, SchedulingPeriod

#: The only values ``schedule_version.status`` may take (§6).
#:
#: ``AMENDED`` is deliberately absent. An accepted amendment *is* the live
#: schedule, so its status is ``FINALIZED``; marking it ``AMENDED`` would leave
#: the schedule with no finalized version and the authoritative-version rule
#: would find nothing. That an older version has been superseded is derivable
#: from the version numbers.
SCHEDULE_VERSION_STATUS_DRAFT = "DRAFT"
SCHEDULE_VERSION_STATUS_REVIEW = "REVIEW"
SCHEDULE_VERSION_STATUS_FINALIZED = "FINALIZED"


class Schedule(Base):
    """The ongoing scheduling artifact for one ministry's scheduling period.

    It holds almost nothing, and that is the design (§3). A Schedule is an
    identity that versions hang off:

    - **No status.** Draft / Review / Finalized describe a *version*, and a
      period must be able to hold a finalized v1 and a draft v2 at once.
    - **No current-version or finalized-version pointer.** The authoritative
      version is derived (ADR 0003, §15); a stored pointer could disagree with
      the statuses it summarises, and would create a referential cycle between
      this table and ``schedule_version``.
    - **No availability state.** That is ``scheduling_period.availability_locked_at``.
    - **No ministry_id.** The period already carries it, one join reaches it,
      and a third copy would earn nothing.

    Zero-or-one schedule per period (§4): a period legitimately exists with no
    scheduling work started, so a schedule is created when work begins rather
    than with the period. Because a period belongs to exactly one ministry,
    ``UNIQUE (scheduling_period_id)`` also delivers one schedule per ministry
    per period without a ministry column.
    """

    __tablename__ = "schedule"
    __table_args__ = (
        # Zero-or-one schedule per period (§4).
        UniqueConstraint(
            "scheduling_period_id", name="uq_schedule_scheduling_period_id"
        ),
        # Parent key for schedule_version's composite foreign key -- the top of
        # the period integrity spine (§9).
        UniqueConstraint(
            "id", "scheduling_period_id", name="uq_schedule_id_scheduling_period_id"
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    scheduling_period_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("scheduling_period.id", ondelete="RESTRICT")
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    scheduling_period: Mapped[SchedulingPeriod] = relationship()
    # Explicit join and foreign_keys: schedule_version carries two composite
    # keys sharing schedule_id (see ScheduleVersion), so every relationship
    # touching that column names it rather than leaving it to inference.
    versions: Mapped[list[ScheduleVersion]] = relationship(
        back_populates="schedule",
        primaryjoin="Schedule.id == ScheduleVersion.schedule_id",
        foreign_keys="ScheduleVersion.schedule_id",
    )


class ScheduleVersion(Base):
    """A meaningful, preserved state of a schedule (§3, §5, §6).

    Creating Version 2 never overwrites Version 1: each version owns its own
    requirement snapshot and its own assignments, which is what keeps generated
    draft, reviewed draft, finalized version and emergency amendment
    historically inspectable.

    **Mutability is a service rule, not a constraint (§7).** A version is
    editable only while it is the latest working version of its schedule *and*
    not yet finalized -- so Version 1 is frozen once Version 2 exists, even if
    Version 1 never left ``DRAFT``. PostgreSQL cannot condition writes to
    ``assignment`` on another table's row without a trigger, and this project
    does not use triggers. What the database contributes is that finalization is
    one-way and timestamped and that version numbering is monotonic, so a
    violation is detectable rather than invisible.

    **``scheduling_period_id`` must be set explicitly when writing a row.** It
    is pinned to the schedule's own period by the composite foreign key below,
    but no relationship manages it: ``schedule`` and ``amends`` both touch
    ``schedule_id``, so each relationship manages only its own single column
    (the RoleQualification / StaffingRequirement pattern). A row whose
    ``scheduling_period_id`` disagrees with its schedule is rejected by the
    database.
    """

    __tablename__ = "schedule_version"
    __table_args__ = (
        # The version's period really is its schedule's period, so the column
        # is pinned rather than free-floating -- and it is the anchor the whole
        # period spine routes through (§9).
        #
        # Named explicitly: the convention's generated name for this composite
        # foreign key would run to 61 characters, close enough to PostgreSQL's
        # limit that a later column rename would silently truncate it.
        ForeignKeyConstraint(
            ["schedule_id", "scheduling_period_id"],
            ["schedule.id", "schedule.scheduling_period_id"],
            name="fk_schedule_version_schedule_period",
            ondelete="RESTRICT",
        ),
        # An amendment amends a version of the *same* schedule (§9, §14). The
        # self-referential composite key is what makes a cross-schedule
        # amendment impossible; that the amended version is itself FINALIZED is
        # service validation, not a database constraint.
        ForeignKeyConstraint(
            ["amends_version_id", "schedule_id"],
            ["schedule_version.id", "schedule_version.schedule_id"],
            name="fk_schedule_version_amends_same_schedule",
            ondelete="RESTRICT",
        ),
        # Version numbers are unique within a schedule. This is what makes a
        # concurrent double-create fail loudly rather than silently producing
        # two "Version 3"s (§5).
        UniqueConstraint(
            "schedule_id",
            "version_number",
            name="uq_schedule_version_schedule_id_version_number",
        ),
        # Parent key for schedule_version_requirement (§9).
        UniqueConstraint(
            "id",
            "scheduling_period_id",
            name="uq_schedule_version_id_scheduling_period_id",
        ),
        # Parent key for the self-reference above (§9).
        UniqueConstraint(
            "id", "schedule_id", name="uq_schedule_version_id_schedule_id"
        ),
        CheckConstraint("version_number >= 1", name="version_number_positive"),
        CheckConstraint(
            "status IN ('DRAFT', 'REVIEW', 'FINALIZED')", name="status_valid"
        ),
        # Status and timestamp can never contradict each other (§6). The
        # equality is deliberately two-way: FINALIZED implies a timestamp, and
        # a timestamp implies FINALIZED.
        CheckConstraint(
            "(status = 'FINALIZED') = (finalized_at IS NOT NULL)",
            name="status_finalized_at_agree",
        ),
        # An emergency change to a schedule people have already been told about
        # should never be unexplained (§14).
        CheckConstraint(
            "amends_version_id IS NULL"
            " OR (amendment_reason IS NOT NULL"
            " AND length(btrim(amendment_reason)) > 0)",
            name="amendment_reason_required",
        ),
        CheckConstraint(
            "amends_version_id IS NULL OR amends_version_id <> id",
            name="amends_not_self",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    schedule_id: Mapped[int] = mapped_column(BigInteger)
    #: Pinned to the schedule's actual period by the composite foreign key
    #: above; the anchor for the whole period spine (§9). Not ORM-managed.
    scheduling_period_id: Mapped[int] = mapped_column(BigInteger)
    #: 1, 2, 3 ... within the schedule. A human label, not a count: gaps are
    #: legal, because an abandoned draft is deleted and its number is not
    #: reused (§5). The service assigns MAX(version_number) + 1 inside the
    #: creating transaction.
    version_number: Mapped[int] = mapped_column(Integer)
    #: DRAFT, REVIEW or FINALIZED (§6). FINALIZED is terminal: correcting a
    #: finalized schedule means a new version, which is what preserves history.
    status: Mapped[str] = mapped_column(Text)
    #: Set exactly when status becomes FINALIZED, and never cleared.
    finalized_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    #: The version this one amends (§14). Named ``amends`` rather than
    #: ``supersedes``: while the amendment is still DRAFT or REVIEW it has
    #: superseded nothing -- the old finalized version is still the live
    #: schedule and still authoritative for conflict queries.
    amends_version_id: Mapped[int | None] = mapped_column(BigInteger)
    #: Why the amendment was necessary. Required whenever amends_version_id is
    #: set, by the check constraint above.
    amendment_reason: Mapped[str | None] = mapped_column(Text)
    #: Free prose from the head about this version.
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Each relationship names its own single foreign-key column explicitly.
    # Left to infer, ``schedule`` and ``amends`` would both claim schedule_id
    # and the mapper would have two relationships writing one column.
    schedule: Mapped[Schedule] = relationship(
        back_populates="versions",
        primaryjoin="ScheduleVersion.schedule_id == Schedule.id",
        foreign_keys="ScheduleVersion.schedule_id",
    )
    amends: Mapped[ScheduleVersion | None] = relationship(
        primaryjoin="ScheduleVersion.amends_version_id == ScheduleVersion.id",
        foreign_keys="ScheduleVersion.amends_version_id",
        remote_side="ScheduleVersion.id",
    )
    requirements: Mapped[list[ScheduleVersionRequirement]] = relationship(
        back_populates="schedule_version",
        primaryjoin=(
            "ScheduleVersion.id"
            " == ScheduleVersionRequirement.schedule_version_id"
        ),
        foreign_keys="ScheduleVersionRequirement.schedule_version_id",
    )


# The authoritative-version lookup (§15, ADR 0003) -- the hottest query this
# slice adds, run for every church-wide conflict check:
#
#   SELECT DISTINCT ON (schedule_id) id, schedule_id
#   FROM schedule_version WHERE status = 'FINALIZED'
#   ORDER BY schedule_id, version_number DESC
#
# Partial on FINALIZED because drafts never participate in the answer, and
# descending on version_number because the highest is the one wanted.
#
# The predicate is text() rather than the mapped attribute: a bare ORM
# attribute has no valid Python repr, so Alembic autogenerate renders it as an
# object address and emits a migration that will not parse.
Index(
    "ix_schedule_version_authoritative",
    ScheduleVersion.schedule_id,
    ScheduleVersion.version_number.desc(),
    postgresql_where=text("status = 'FINALIZED'"),
)


class ScheduleVersionRequirement(Base):
    """The immutable snapshot of what one version was built against (§8).

    One row per event and role the version had to staff, carrying the required
    count *and the event's date* as they stood when the version was created.
    Written once, in the same transaction as the version insert, and never
    changed thereafter -- not when a ``staffing_requirement`` is edited, not
    when one is deleted, not when an event moves, and not even while the
    version is still a DRAFT (§13).

    **This is not a second staffing configuration system.** Nobody edits it, no
    UI points at it, and no head ever sets a count here. Changing staffing means
    editing ``staffing_requirement`` -- the single mutable configuration surface
    -- and then creating a new version. The approved rule against a second
    *mutable* staffing system is preserved precisely because this table is
    immutable: a copy that can never diverge from what it recorded is a
    historical record, not a competing source of truth.

    **No foreign key to ``staffing_requirement``.** Deliberate, and it is what
    makes the snapshot work: a reference would make the mutable input row
    undeletable for as long as any version referred to it, which is exactly the
    coupling the snapshot exists to break. The snapshot is historically
    self-sufficient -- it names the event, the role and the count directly.

    **No ``updated_at``.** The row is immutable, so an ``updated_at`` could only
    ever equal ``created_at`` -- a column that looks like it means something and
    never does (decision 10).

    **``scheduling_period_id`` and ``ministry_id`` must be set explicitly when
    writing a row.** Each is shared by two composite foreign keys, so the ORM
    relationships manage only their own single id column.
    """

    __tablename__ = "schedule_version_requirement"
    __table_args__ = (
        # The requirement belongs to the same scheduling period as its version
        # (§9).
        ForeignKeyConstraint(
            ["schedule_version_id", "scheduling_period_id"],
            ["schedule_version.id", "schedule_version.scheduling_period_id"],
            name="fk_schedule_version_requirement_version_period",
            ondelete="RESTRICT",
        ),
        # ... and the event belongs to that same period, because both keys
        # route through this row's single scheduling_period_id. A snapshot row
        # for an event outside the version's period cannot be inserted (§9).
        ForeignKeyConstraint(
            ["event_id", "scheduling_period_id"],
            ["event.id", "event.scheduling_period_id"],
            name="fk_schedule_version_requirement_event_period",
            ondelete="RESTRICT",
        ),
        # The ministry spine: event and role must agree on the ministry,
        # because both keys route through this row's single ministry_id. A
        # Setup event cannot be paired with an AV role (§9).
        ForeignKeyConstraint(
            ["event_id", "ministry_id"],
            ["event.id", "event.ministry_id"],
            name="fk_schedule_version_requirement_event_ministry",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["ministry_role_id", "ministry_id"],
            ["ministry_role.id", "ministry_role.ministry_id"],
            name="fk_schedule_version_requirement_role_ministry",
            ondelete="RESTRICT",
        ),
        # One snapshot row per version, event and role.
        UniqueConstraint(
            "schedule_version_id",
            "event_id",
            "ministry_role_id",
            name="uq_schedule_version_requirement_version_event_role",
        ),
        # Parent key for assignment's single four-column composite foreign key
        # (§10). The column order here is the order assignment references.
        UniqueConstraint(
            "id",
            "schedule_version_id",
            "event_id",
            "ministry_id",
            name="uq_schedule_version_requirement_id_version_event_ministry",
        ),
        CheckConstraint("required_count > 0", name="required_count_positive"),
        Index("ix_schedule_version_requirement_event_id", "event_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    schedule_version_id: Mapped[int] = mapped_column(BigInteger)
    event_id: Mapped[int] = mapped_column(BigInteger)
    #: **The event's date as it stood when this snapshot was taken**, and the
    #: single carried column in this slice that is deliberately *not* pinned to
    #: its source (§9). There is no foreign key, no trigger, no generated
    #: column and no ORM synchronisation forcing it to equal
    #: ``event.event_date``, and there must not be: divergence is the whole
    #: point. If Version 3 was finalized with a service on 15 November and the
    #: event row is afterwards moved to 22 November, Version 3 must go on
    #: meaning 15 November (§13).
    #:
    #: The event's *identity* is pinned by the composite foreign keys above;
    #: the date it was scheduled for is remembered. This column, not
    #: ``event.event_date``, is what the ADR 0002 conflict query reads (§15).
    #:
    #: It is also what makes a moved event register as a change in the
    #: staleness comparison, which compares
    #: ``(event_id, event_date, ministry_role_id, required_count)`` on both
    #: sides (§13, decision 14).
    event_date: Mapped[datetime.date] = mapped_column(Date)
    ministry_role_id: Mapped[int] = mapped_column(BigInteger)
    #: Integrity spine (§9); shared by two composite foreign keys, not
    #: ORM-managed.
    scheduling_period_id: Mapped[int] = mapped_column(BigInteger)
    #: Integrity spine (§9); shared by two composite foreign keys, not
    #: ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    #: Copied from ``staffing_requirement.required_count`` at version creation
    #: and never changed. Required capacity for a version comes from here,
    #: never from the current staffing_requirement table (§12).
    required_count: Mapped[int] = mapped_column(Integer)

    #: When the snapshot was taken. There is deliberately no ``updated_at``.
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Each relationship names its own single foreign-key column explicitly.
    # Left to infer, the event and role relationships would both claim
    # ministry_id, and the version and event relationships would both claim
    # scheduling_period_id.
    schedule_version: Mapped[ScheduleVersion] = relationship(
        back_populates="requirements",
        primaryjoin=(
            "ScheduleVersionRequirement.schedule_version_id"
            " == ScheduleVersion.id"
        ),
        foreign_keys="ScheduleVersionRequirement.schedule_version_id",
    )
    event: Mapped[Event] = relationship(
        primaryjoin="ScheduleVersionRequirement.event_id == Event.id",
        foreign_keys="ScheduleVersionRequirement.event_id",
    )
    ministry_role: Mapped[MinistryRole] = relationship(
        primaryjoin=(
            "ScheduleVersionRequirement.ministry_role_id == MinistryRole.id"
        ),
        foreign_keys="ScheduleVersionRequirement.ministry_role_id",
    )
    assignments: Mapped[list[Assignment]] = relationship(
        back_populates="schedule_version_requirement",
        primaryjoin=(
            "ScheduleVersionRequirement.id"
            " == Assignment.schedule_version_requirement_id"
        ),
        foreign_keys="Assignment.schedule_version_requirement_id",
    )


class Assignment(Base):
    """One membership filling one snapshot requirement (§10).

    That is the whole semantic model. The requirement supplies the event, the
    role, the version, the period and the ministry, so this row restates none of
    them as free-standing facts:

    - **No ``ministry_role_id``.** The role is the requirement's role, one join
      away; duplicating it would add a column that must then be pinned to the
      requirement for no query that needs it here.
    - **No ``person_id``.** Person is reached through the membership (§11),
      which is also what carries the ministry the §9 spine routes through. If
      the conflict query ever proves costly, a ``person_id`` can be added later
      with a ``(ministry_membership_id, person_id)`` composite foreign key into
      a new ``UNIQUE (id, person_id)`` on ``ministry_membership`` -- additive
      and pinned. Not needed now.
    - **No ``scheduling_period_id``.** The requirement carries it.
    - **No ``origin``.** Generated / manual provenance drives no current product
      behaviour, and AuditEvent will preserve meaningful change history.

    **Over-filling is not database-enforceable.** Four assignments against
    ``required_count = 3`` violate no single-row rule; it is an aggregate
    compared with another table, and belongs to finalization validation (§12,
    §16). Unfilled capacity is likewise derived --
    ``required_count - count(assignments)`` -- with no ``unfilled_slot`` rows.

    **``schedule_version_id``, ``event_id`` and ``ministry_id`` must be set
    explicitly when writing a row.** All three are pinned by the four-column
    composite foreign key below, but no relationship manages them.
    """

    __tablename__ = "assignment"
    __table_args__ = (
        # One composite foreign key pinning all three carried columns at once
        # (§10): the assignment belongs to its requirement's version, names its
        # requirement's event, and shares its requirement's ministry. The
        # column order matches the parent unique key on
        # schedule_version_requirement exactly.
        ForeignKeyConstraint(
            [
                "schedule_version_requirement_id",
                "schedule_version_id",
                "event_id",
                "ministry_id",
            ],
            [
                "schedule_version_requirement.id",
                "schedule_version_requirement.schedule_version_id",
                "schedule_version_requirement.event_id",
                "schedule_version_requirement.ministry_id",
            ],
            name="fk_assignment_requirement_version_event_ministry",
            ondelete="RESTRICT",
        ),
        # The membership is in the requirement's ministry, because both keys
        # route through this row's single ministry_id (§9).
        ForeignKeyConstraint(
            ["ministry_membership_id", "ministry_id"],
            ["ministry_membership.id", "ministry_membership.ministry_id"],
            name="fk_assignment_membership_ministry",
            ondelete="RESTRICT",
        ),
        # One membership at most once per event per version -- one constraint
        # satisfying two approved rules at once (§10): a person cannot occupy
        # two required roles in one event, and cannot be assigned twice to the
        # same role, because either repeats this triple whatever the roles are.
        #
        # schedule_version_requirement_id is deliberately NOT unique here:
        # required_count may exceed 1, so three greeters are three rows against
        # one requirement row.
        UniqueConstraint(
            "schedule_version_id",
            "event_id",
            "ministry_membership_id",
            name="uq_assignment_version_event_membership",
        ),
        # A manual override must say why (§14). The reason is free prose, not a
        # rule code: which rule was overridden is deliberately not stored, since
        # that is the beginning of a generic override engine the approved scope
        # rules out.
        CheckConstraint(
            "(NOT is_override)"
            " OR (override_reason IS NOT NULL"
            " AND length(btrim(override_reason)) > 0)",
            name="override_reason_required",
        ),
        # The filled-count aggregate (§12).
        Index(
            "ix_assignment_schedule_version_requirement_id",
            "schedule_version_requirement_id",
        ),
        # The ADR 0002 conflict query (§15).
        Index(
            "ix_assignment_ministry_membership_id_event_id",
            "ministry_membership_id",
            "event_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    #: The requirement being filled. Supplies event, role, version, period and
    #: ministry context.
    schedule_version_requirement_id: Mapped[int] = mapped_column(BigInteger)
    #: Not person_id (§11). The membership carries the ministry, makes
    #: qualification a direct (membership, role) lookup, puts "active member"
    #: immediately at hand as ``deactivated_at IS NULL``, and says which
    #: capacity someone serving in two ministries is serving in here.
    ministry_membership_id: Mapped[int] = mapped_column(BigInteger)
    #: Carried so the uniqueness rule below can exist; pinned to the
    #: requirement's version. Not ORM-managed.
    schedule_version_id: Mapped[int] = mapped_column(BigInteger)
    #: Carried for the same uniqueness rule; pinned to the requirement's event.
    #: Not ORM-managed.
    event_id: Mapped[int] = mapped_column(BigInteger)
    #: The shared column tying the membership's ministry to the requirement's.
    #: Not ORM-managed.
    ministry_id: Mapped[int] = mapped_column(BigInteger)
    #: This person is in this slot despite a rule that would normally have
    #: prevented it -- a standing property of the current schedule, which a head
    #: must see without querying an audit log (§14). Copied along when a
    #: version's assignments are copied into a new version: the exception still
    #: stands.
    is_override: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    #: Required when is_override, by the check constraint above.
    override_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Each relationship names its own single foreign-key column explicitly.
    # Left to infer, both would claim the shared ministry_id column.
    schedule_version_requirement: Mapped[ScheduleVersionRequirement] = relationship(
        back_populates="assignments",
        primaryjoin=(
            "Assignment.schedule_version_requirement_id"
            " == ScheduleVersionRequirement.id"
        ),
        foreign_keys="Assignment.schedule_version_requirement_id",
    )
    ministry_membership: Mapped[MinistryMembership] = relationship(
        primaryjoin="Assignment.ministry_membership_id == MinistryMembership.id",
        foreign_keys="Assignment.ministry_membership_id",
    )
