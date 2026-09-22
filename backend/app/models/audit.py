"""Audit event schema.

Implements the reviewed design in
``docs/architecture/audit-event-data-model.md`` (revision 2) and the decision
recorded in ``docs/adr/0004-generic-audit-target-references.md``. Section
numbers in the comments refer to the audit-event document.

One table: audit_event. The audit recording service, domain mutation services,
APIs, authorization, runtime database roles, ConstraintConfiguration,
PreferenceConfiguration and ShadowAssignment are later slices and appear nowhere
here (§21).

**This is not event sourcing** (§3). The ordinary domain tables remain the
authoritative current state; ``audit_event`` explains how they got that way.
Nothing in the application may reconstruct a person, a membership or an
assignment by folding audit rows.

Conventions are inherited from the implemented slices unchanged:

- ``bigint GENERATED ALWAYS AS IDENTITY`` primary keys.
- ``timestamptz`` timestamps.
- Every foreign key ``ON DELETE RESTRICT``.
- ``text`` + ``CHECK`` for closed value sets; no native PostgreSQL ``ENUM``.

Three deliberate departures from those conventions, each reviewed and each
justified in the design document:

- **No ``created_at`` / ``updated_at``** (§15). ``occurred_at`` is both the act's
  time and the row's insertion time, because the audit row is written in the
  same transaction as the change it describes (§12). ``updated_at`` is absent
  because the row is immutable (§10), and its absence is a structural signal.
- **JSONB** for ``before_values`` / ``after_values`` (§7.1), as a deliberate
  exception to the project's relational-storage preference: the payload shape
  genuinely varies by target and action, and these are historical explanatory
  payloads that nothing joins on, aggregates, or reads to make a decision.
- **A generic, unenforced target reference** — ``target_table`` + ``target_id``
  with no foreign key (§5, ADR 0004). This is the only place in the schema where
  a reference is not database-enforced.

**Append-only is service discipline, not a database guarantee** (§10). There is
no trigger blocking UPDATE/DELETE, and no runtime-role privilege change is made
here; the application currently connects with an owner-capable development role,
so PostgreSQL does not yet make mutation impossible. Future service code simply
must not expose audit mutation operations.
"""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.core import Ministry, Person

#: The only values ``audit_event.actor_type`` may take (§4.1).
#:
#: ``SYSTEM`` covers acts with no human author -- the solver creating a draft
#: version, a scheduled job, a bulk import. There is deliberately no
#: service-account table: distinguishing the solver from an import is
#: ``actor_label``'s job, at no schema cost.
AUDIT_ACTOR_TYPE_PERSON = "PERSON"
AUDIT_ACTOR_TYPE_SYSTEM = "SYSTEM"

#: The V1 audited target tables (§5.2).
#:
#: These are the **actual stable SQL table names**, never Python class names:
#: ``ministry_membership``, not ``MinistryMembership``. The ORM class name is an
#: application detail that may be renamed without a migration; the table name is
#: the durable identifier, and it is what a reader querying in psql needs.
#:
#: Deliberately absent: ``church`` (configuration root, single row),
#: ``user_account`` (login history is a separate concern with a different
#: audience, retention and privacy posture -- §21),
#: ``schedule_version_requirement`` (an immutable snapshot nobody edits), and
#: ``audit_event`` itself. Extending auditing to a new target type requires a
#: later migration that widens the CHECK below.
AUDIT_TARGET_TABLES = (
    "person",
    "ministry",
    "ministry_membership",
    "ministry_role",
    "role_qualification",
    "scheduling_period",
    "event",
    "staffing_requirement",
    "availability",
    "existing_commitment",
    "schedule",
    "schedule_version",
    "assignment",
    "membership_serving_limit",
    # Task 50: a head configuring or clearing a linked-pair same-date
    # exclusion is audited like any other domain change. The row names the
    # two memberships and the period; it never names a relationship.
    "membership_same_date_exclusion",
    # Task 74: the two generic hard rules a head configures for a ministry --
    # a member-group cap on one event, and a same-event support requirement.
    #
    # Four target tables, not five. ``membership_support_supporter`` is
    # deliberately absent: a supporter row is never changed on its own, only as
    # part of configuring the requirement it belongs to, so the audited target
    # is that requirement and the supporter set travels in the payload. A fifth
    # target would invite two history rows for one act.
    "member_group",
    "member_group_member",
    "member_group_event_limit",
    "membership_support_requirement",
)

#: Rendered once from the tuple above so the SQL list and the Python constant
#: can never drift apart.
_TARGET_TABLE_VALUES = ", ".join(f"'{name}'" for name in AUDIT_TARGET_TABLES)

#: Uppercase snake case: ``MINISTRY_HEAD_GRANTED``, ``SCHEDULE_FINALIZED``.
#: Rejects lowercase, spaces, a leading digit, a trailing underscore and a
#: doubled underscore (§6.2).
AUDIT_ACTION_PATTERN = r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$"


class AuditEvent(Base):
    """One committed domain change: who did it, when, to what, and why.

    **Only successful, committed domain state changes are recorded** (§11).
    Unauthorized attempts, validation failures, HTTP request logs, login and
    security telemetry, solver failures and application exceptions belong to
    application/security/diagnostic logging, not here. Mixing them in would make
    every existing query wrong unless it remembered to filter -- "how many
    overrides has this head applied?" would count attempts.

    **The audit row is written in the same transaction as the change it
    describes** (§12). If the domain write fails the audit row is not written;
    if the audit insert fails the domain mutation rolls back, so a change the
    system cannot explain does not happen. There is deliberately no asynchronous
    queueing and no global ORM event listener: a listener cannot know the actor,
    cannot know the reason, cannot tell a promotion from a note edit, and would
    fire during migrations and fixtures. Audit generation is explicit in the
    domain service -- which is a later slice.

    **The target reference is generic and unenforced.** ``target_table`` +
    ``target_id`` may name a row in any of the audited tables listed above, and
    PostgreSQL cannot foreign-key a column whose parent table is chosen per row.
    Target existence, and the agreement between ``ministry_id`` and the target,
    are **service-layer invariants** (§5.3, §8) -- single-point responsibilities
    because one ``record()`` function is intended to write every row.
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('PERSON', 'SYSTEM')", name="actor_type_valid"
        ),
        # The two-way actor invariant (§4.1). Deliberately an equality rather
        # than a one-way implication: PERSON requires the reference, and SYSTEM
        # requires its absence. NULL is therefore never ambiguous between "the
        # system did it" and "we forgot to record who". Same device
        # schedule_version uses for status / finalized_at.
        CheckConstraint(
            "(actor_type = 'PERSON') = (actor_person_id IS NOT NULL)",
            name="actor_type_matches_actor_person_id",
        ),
        CheckConstraint(
            "length(btrim(actor_label)) > 0", name="actor_label_not_blank"
        ),
        # A *shape*, not a value list (§6.2). action is an open, growing
        # catalogue that is displayed and filtered but never branched on -- so
        # unlike event_kind or schedule_version.status, freezing it in a
        # CHECK IN list would mean a migration every time a screen learns to
        # record what it does. The vocabulary itself will be a centralized
        # constants module in the application.
        CheckConstraint(
            f"action ~ '{AUDIT_ACTION_PATTERN}'", name="action_shape_valid"
        ),
        # Name integrity, which is the part of target integrity that *is*
        # available (§5.2). The realistic failure mode is not a fabricated
        # target_id -- the service writes it from the row it just changed -- but
        # a typo or naming-convention slip in the table name ('assignments',
        # 'MinistryMembership'), which produces rows silently invisible to every
        # query filtering on the correct spelling. This makes that a write-time
        # error.
        CheckConstraint(
            f"target_table IN ({_TARGET_TABLE_VALUES})", name="target_table_valid"
        ),
        CheckConstraint("target_id > 0", name="target_id_positive"),
        CheckConstraint("length(btrim(summary)) > 0", name="summary_not_blank"),
        # A blank reason is not a reason; its absence is NULL (§9). There is
        # deliberately no action-specific check such as "OVERRIDE requires a
        # reason": that would re-freeze the action vocabulary in the one place a
        # new required-reason rule is most likely to be added. Reason-required
        # policy belongs to the domain service.
        CheckConstraint(
            "reason IS NULL OR length(btrim(reason)) > 0", name="reason_not_blank"
        ),
        # A row recording neither a prior nor a resulting value describes
        # nothing (§4). Both are never required: a creation has no before, a
        # deletion has no after (§7.2).
        CheckConstraint(
            "before_values IS NOT NULL OR after_values IS NOT NULL",
            name="payload_present",
        ),
        # A payload is a field map, never an array or a bare scalar (§7.2).
        CheckConstraint(
            "before_values IS NULL OR jsonb_typeof(before_values) = 'object'",
            name="before_values_is_object",
        ),
        CheckConstraint(
            "after_values IS NULL OR jsonb_typeof(after_values) = 'object'",
            name="after_values_is_object",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), primary_key=True
    )
    #: When the act happened -- and, identically, when the row was inserted,
    #: since the two share a transaction (§12, §15). ``now()`` is PostgreSQL's
    #: transaction-start time, so every row written in one transaction shares a
    #: timestamp: one act is one instant even when it touches several tables.
    #:
    #: There is deliberately no separate ``created_at`` (it would hold the same
    #: value) and no ``updated_at`` (the row is immutable).
    occurred_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    #: PERSON or SYSTEM (§4.1).
    actor_type: Mapped[str] = mapped_column(Text)
    #: The canonical Person who acted -- **never a UserAccount** (§4.1). Domain
    #: authority lives on Person (core §4.1); ``user_account`` may legitimately
    #: be hard-deleted (core §6), so a foreign key to it would either block that
    #: deletion or leave history pointing at nothing; and a Person may act
    #: before ever logging in.
    #:
    #: A real foreign key, unlike the target: the actor is always a Person, so
    #: the reference is relationally enforceable and is enforced. The accepted
    #: consequence is that a Person who has acted can no longer be hard-deleted
    #: -- consistent with core §6, which deactivates people rather than deleting
    #: them and only ever allowed never-referenced rows to be removed.
    actor_person_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("person.id", ondelete="RESTRICT")
    )
    #: Required, non-blank, immutable display snapshot: "Demo Admin",
    #: "Scheduling engine", "Availability import". It does **not** replace
    #: ``actor_person_id`` for PERSON rows -- both are written. The foreign key
    #: is the identity to join on and authorize against; the label is the
    #: historical rendering, and is the only actor information a SYSTEM row has.
    actor_label: Mapped[str] = mapped_column(Text)
    #: A domain-oriented action name in the church's vocabulary --
    #: ``MINISTRY_HEAD_GRANTED``, not ``UPDATE`` (§6.1). Nothing in the
    #: application may read this column to decide what to do: that would be a
    #: domain fact living in the audit log, which §3 forbids. It is displayed
    #: and filtered only.
    action: Mapped[str] = mapped_column(Text)
    #: The audited SQL table name, from the closed list above.
    target_table: Mapped[str] = mapped_column(Text)
    #: The audited row's primary key. **Deliberately no foreign key**
    #: (ADR 0004): PostgreSQL cannot foreign-key a column whose parent table is
    #: chosen per row, and the alternatives all cost more than the guarantee is
    #: worth here -- one nullable RESTRICT key per audited table would make every audited
    #: row permanently undeletable, repealing core §6 as a side effect of
    #: turning auditing on.
    #:
    #: A dangling target is therefore a supported state, not a corruption: the
    #: row stays readable through ``target_table``, ``target_id``, ``summary``
    #: and the payloads (§13).
    target_id: Mapped[int] = mapped_column(BigInteger)
    #: Authorization and query scope (§8). NULL means **church-wide**, not
    #: unknown -- a distinction §17's filter depends on, since a ministry-scoped
    #: viewer sees rows matching their ministries and *not* the NULL ones.
    #:
    #: A real foreign key, so the database guarantees the Ministry exists. It
    #: cannot guarantee this is the Ministry associated with the target, because
    #: the target's table is chosen per row; that agreement is a service-layer
    #: invariant, and no composite key is invented here to approximate it (§4).
    ministry_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ministry.id", ondelete="RESTRICT")
    )
    #: The primary human-readable description, required and non-blank (§7.3).
    #: The UI must not have to reconstruct ordinary audit-history sentences from
    #: raw JSONB. Because revision 1's ``target_label`` was withdrawn, the
    #: summary also carries the target's human identity, so it must *name* the
    #: thing it is about: "Granted Setup Ministry Head authority to Sheldon",
    #: never "Granted Ministry Head authority".
    summary: Mapped[str] = mapped_column(Text)
    #: The reason supplied for this act, where one was supplied (§9). Not every
    #: event requires one: for V1 a reason is mandatory only where accepted
    #: product behaviour already requires it -- a manual assignment override and
    #: a schedule amendment. That policy lives in the domain service.
    #:
    #: Not redundant with ``assignment.override_reason`` /
    #: ``schedule_version.amendment_reason``: those are the *current standing*
    #: explanation, overwritten in place, while this is the reason given for one
    #: historical act. Edit a standing override reason and the domain column
    #: keeps only the latest sentence; the original survives only here.
    reason: Mapped[str | None] = mapped_column(Text)
    #: Changed/relevant business fields as they were, and as they became (§7.2).
    #: Always JSON objects, never arrays or bare scalars. NULL on the side that
    #: does not apply: a creation has no before, a deletion has no after.
    #:
    #: Only changed or genuinely relevant fields go in -- never a complete ORM
    #: object, never passwords or OAuth tokens or secrets, never unrelated PII,
    #: and never bookkeeping columns such as ``updated_at`` merely because they
    #: changed. Old payloads are historical records and are never rewritten when
    #: schemas evolve, so readers must tolerate unknown and missing keys (§7.4).
    #:
    #: The key sets identify the changed fields, so there is deliberately no
    #: ``changed_fields`` column and no ``payload_version``.
    #: ``none_as_null=True`` is load-bearing, not a style choice. SQLAlchemy's
    #: JSONB type defaults to ``none_as_null=False``, which serializes Python
    #: ``None`` as the JSON scalar ``null`` rather than SQL ``NULL`` -- and
    #: ``jsonb_typeof('null'::jsonb)`` is ``'null'``, not ``'object'``, so
    #: every creation (no before) and every removal (no after) would be
    #: refused by the CHECK constraints above. The absent side of a payload is
    #: SQL NULL, exactly as those constraints and §7.2 describe. This is a
    #: client-side serialization setting only: the column's DDL is unchanged.
    before_values: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )
    after_values: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True)
    )

    # Simple one-sided relationships. Both foreign keys are single-column and
    # unambiguous, so neither needs an explicit primaryjoin -- unlike the
    # composite-key tables in the scheduling slices. No relationship is or can
    # be created from target_table/target_id: the target's class is chosen per
    # row, and resolving it is application code that must tolerate "not found".
    actor_person: Mapped[Person | None] = relationship()
    ministry: Mapped[Ministry | None] = relationship()


# The church-wide timeline, newest first (§16). id is the tiebreaker so
# pagination is stable when several rows share a transaction timestamp.
Index(
    "ix_audit_event_occurred_at_id",
    AuditEvent.occurred_at.desc(),
    AuditEvent.id.desc(),
)

# "What has happened to this object?" -- the history panel on a person,
# membership or assignment. Leading target_table keeps it selective and matches
# how a generic-target query is written (ADR 0004).
Index(
    "ix_audit_event_target_table_target_id_occurred_at",
    AuditEvent.target_table,
    AuditEvent.target_id,
    AuditEvent.occurred_at.desc(),
)

# "Show Setup history" (§8, §17). Partial, because church-wide rows are never
# part of a ministry-scoped answer and would be dead weight here.
#
# The predicate is text() rather than the mapped attribute: a bare ORM attribute
# has no valid Python repr, so Alembic autogenerate renders it as an object
# address and emits a migration that will not parse.
Index(
    "ix_audit_event_ministry_id_occurred_at",
    AuditEvent.ministry_id,
    AuditEvent.occurred_at.desc(),
    postgresql_where=text("ministry_id IS NOT NULL"),
)

# "What has this person done?" Partial, since SYSTEM rows have no actor and are
# never the answer.
Index(
    "ix_audit_event_actor_person_id_occurred_at",
    AuditEvent.actor_person_id,
    AuditEvent.occurred_at.desc(),
    postgresql_where=text("actor_person_id IS NOT NULL"),
)
