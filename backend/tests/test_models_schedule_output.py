"""Schedule-output schema metadata tests.

Offline only: these assert against ``Base.metadata`` and the compiled DDL, never
opening a connection. The suite still needs no PostgreSQL, no ``.env`` and no
network.

They protect the invariants ``docs/architecture/schedule-output-data-model.md``
(revision 2) and ``docs/adr/0003-authoritative-schedule-version.md`` call
load-bearing — and, just as importantly, the things the reviewed design says
must **not** be there. Several of the deliberate absences (no
``current_version_id``, no ``AMENDED`` status, no foreign key from the snapshot
date to ``event.event_date``) would be silently "helpful" additions that break
historical correctness without breaking a single import.
"""

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  (registers the mapped classes)
from app.db import Base

CORE_TABLES = {
    "church",
    "ministry",
    "ministry_membership",
    "ministry_role",
    "person",
    "role_qualification",
    "user_account",
}

SCHEDULING_INPUT_TABLES = {
    "availability",
    "event",
    "existing_commitment",
    "scheduling_period",
    "staffing_requirement",
}

SCHEDULE_OUTPUT_TABLES = {
    "assignment",
    "schedule",
    "schedule_version",
    "schedule_version_requirement",
}


def _table(name):
    return Base.metadata.tables[name]


def _unique_column_sets(table):
    sets = {
        tuple(c.name for c in con.columns)
        for con in table.constraints
        if isinstance(con, UniqueConstraint)
    }
    sets |= {tuple(c.name for c in ix.columns) for ix in table.indexes if ix.unique}
    return sets


def _checks(table):
    return {
        con.name: str(con.sqltext)
        for con in table.constraints
        if isinstance(con, CheckConstraint)
    }


def _composite_fks(table):
    """{name: (local columns, referred table, referred columns, ondelete)}."""
    return {
        con.name: (
            tuple(c.name for c in con.columns),
            con.elements[0].column.table.name,
            tuple(e.column.name for e in con.elements),
            con.ondelete,
        )
        for con in table.constraints
        if isinstance(con, ForeignKeyConstraint) and len(con.columns) > 1
    }


def _table_ddl(name):
    return str(CreateTable(_table(name)).compile(dialect=postgresql.dialect()))


def _index_ddl(table, index_name):
    index = next(ix for ix in table.indexes if ix.name == index_name)
    return str(CreateIndex(index).compile(dialect=postgresql.dialect()))


# --------------------------------------------------------------------------
# Table set
# --------------------------------------------------------------------------


def test_the_core_input_and_output_tables_are_registered():
    """A subset check: the audit slice added a seventeenth table.

    The exact total is asserted in test_models_audit.py.
    """
    assert (
        CORE_TABLES | SCHEDULING_INPUT_TABLES | SCHEDULE_OUTPUT_TABLES
    ) <= set(Base.metadata.tables)


def test_all_four_schedule_output_tables_exist():
    assert SCHEDULE_OUTPUT_TABLES <= set(Base.metadata.tables)


def test_configuration_and_shadow_tables_remain_absent():
    """Still deferred to their own slices (§20). Audit arrived in Task 12."""
    deferred = {
        "constraint_configuration",
        "ministry_default_staffing",
        "preference_configuration",
        "schedule_version_diagnostic",
        "shadow_assignment",
        "solver_run",
        "unfilled_slot",
    }

    assert deferred.isdisjoint(Base.metadata.tables)


def test_mappers_configure_without_error():
    """Catches relationship misconfiguration, which import alone does not.

    Every composite foreign key in this slice shares a carried column with
    another one, so a relationship left to infer its own columns would produce
    two relationships writing the same column. That is a configure-time error,
    not an import-time one.
    """
    configure_mappers()


# --------------------------------------------------------------------------
# Conventions shared with the implemented slices
# --------------------------------------------------------------------------


def test_every_new_table_has_a_bigint_identity_primary_key():
    for name in SCHEDULE_OUTPUT_TABLES:
        pk = list(_table(name).primary_key.columns)

        assert [c.name for c in pk] == ["id"], name
        assert isinstance(pk[0].type, BigInteger), name
        assert pk[0].identity is not None and pk[0].identity.always is True, name


def test_every_new_table_has_a_timezone_aware_created_at():
    for name in SCHEDULE_OUTPUT_TABLES:
        created_at = _table(name).c.created_at

        assert created_at.type.timezone is True, name
        assert not created_at.nullable, name
        assert created_at.server_default is not None, name


def test_mutable_tables_have_updated_at_and_the_snapshot_does_not():
    """The snapshot is immutable, so an updated_at could only equal created_at."""
    for name in SCHEDULE_OUTPUT_TABLES - {"schedule_version_requirement"}:
        table = _table(name)

        assert table.c.updated_at.type.timezone is True, name
        assert not table.c.updated_at.nullable, name
        assert table.c.updated_at.server_default is not None, name
        assert table.c.updated_at.onupdate is not None, name

    assert "updated_at" not in _table("schedule_version_requirement").c


def test_every_new_foreign_key_restricts_deletes():
    """Preserves the ON DELETE behaviour of the reviewed design (§17).

    RESTRICT everywhere is what makes deleting an abandoned draft an ordered
    delete — assignments, then snapshot, then version — and what stops a
    finalized version, or a version an amendment refers to, being deleted at
    all.
    """
    for name in SCHEDULE_OUTPUT_TABLES:
        for fk in _table(name).foreign_keys:
            assert fk.ondelete == "RESTRICT", f"{name}.{fk.parent.name}"


def test_no_new_table_uses_a_native_enum_or_jsonb():
    for name in SCHEDULE_OUTPUT_TABLES:
        ddl = _table_ddl(name).upper()

        assert "JSONB" not in ddl, name
        assert "CREATE TYPE" not in ddl, name


def test_the_schema_uses_no_triggers_or_generated_columns():
    """Version immutability and the snapshot's write-once rule are service rules.

    A trigger or generated column tying the snapshot to current state is the
    specific mistake §3, §8 and §13 forbid.
    """
    for name in SCHEDULE_OUTPUT_TABLES:
        ddl = _table_ddl(name).upper()

        assert "TRIGGER" not in ddl, name
        assert "GENERATED ALWAYS AS (" not in ddl, name


# --------------------------------------------------------------------------
# The additive Event alteration
# --------------------------------------------------------------------------


def test_event_exposes_the_scheduling_period_parent_key():
    """The additive constraint this slice adds to the implemented event table."""
    assert ("id", "scheduling_period_id") in _unique_column_sets(_table("event"))


def test_event_keeps_its_existing_ministry_parent_key():
    """Both serve different composite-FK integrity chains; neither replaces the other."""
    unique_sets = _unique_column_sets(_table("event"))

    assert ("id", "ministry_id") in unique_sets
    assert ("id", "scheduling_period_id") in unique_sets


def test_event_is_otherwise_unchanged():
    """The alteration adds a parent key and nothing else."""
    event = _table("event")

    assert set(event.c.keys()) == {
        "id",
        "scheduling_period_id",
        "ministry_id",
        "event_date",
        "event_kind",
        "name",
        "cancelled_at",
        "created_at",
        "updated_at",
    }
    assert _composite_fks(event) == {
        "fk_event_period_ministry": (
            ("scheduling_period_id", "ministry_id"),
            "scheduling_period",
            ("id", "ministry_id"),
            "RESTRICT",
        )
    }


# --------------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------------


def test_schedule_is_zero_or_one_per_scheduling_period():
    """A period legitimately exists with no scheduling work started (§4)."""
    assert ("scheduling_period_id",) in _unique_column_sets(_table("schedule"))


def test_schedule_exposes_the_period_spine_parent_key():
    assert ("id", "scheduling_period_id") in _unique_column_sets(_table("schedule"))


def test_schedule_carries_no_state_at_all():
    """No status, no version pointer, no availability state, no ministry column."""
    schedule = _table("schedule")

    assert set(schedule.c.keys()) == {
        "id",
        "scheduling_period_id",
        "created_at",
        "updated_at",
    }


def test_schedule_does_not_duplicate_ministry():
    """The period already carries it, and one join reaches it (§3)."""
    assert "ministry_id" not in _table("schedule").c


# --------------------------------------------------------------------------
# ScheduleVersion
# --------------------------------------------------------------------------


def test_schedule_version_numbers_are_unique_within_a_schedule():
    """Makes a concurrent double-create fail loudly, not produce two "Version 3"s."""
    assert ("schedule_id", "version_number") in _unique_column_sets(
        _table("schedule_version")
    )


def test_schedule_version_number_must_be_positive():
    check = _checks(_table("schedule_version"))[
        "ck_schedule_version_version_number_positive"
    ]

    assert "version_number >= 1" in check
    assert isinstance(_table("schedule_version").c.version_number.type, Integer)


def test_schedule_version_number_is_not_part_of_the_primary_key():
    """Foreign keys reference one stable column (§5)."""
    pk = [c.name for c in _table("schedule_version").primary_key.columns]

    assert pk == ["id"]


def test_schedule_version_status_allows_only_the_three_reviewed_values():
    check = _checks(_table("schedule_version"))["ck_schedule_version_status_valid"]

    assert "'DRAFT'" in check
    assert "'REVIEW'" in check
    assert "'FINALIZED'" in check

    status = _table("schedule_version").c.status
    assert isinstance(status.type, Text)
    assert not status.nullable


def test_amended_is_deliberately_not_a_status():
    """An accepted amendment *is* the live schedule, so its status is FINALIZED.

    Marking it AMENDED would leave the schedule with no FINALIZED version and
    the authoritative-version rule would find nothing (§6).
    """
    check = _checks(_table("schedule_version"))["ck_schedule_version_status_valid"]

    for forbidden in ("AMENDED", "SUPERSEDED", "PUBLISHED", "ARCHIVED", "CANCELLED"):
        assert forbidden not in check


def test_schedule_version_status_and_finalized_at_can_never_disagree():
    """Deliberately a two-way equality, not a one-way implication (§6)."""
    check = _checks(_table("schedule_version"))[
        "ck_schedule_version_status_finalized_at_agree"
    ]

    assert "(status = 'FINALIZED') = (finalized_at IS NOT NULL)" in check

    finalized_at = _table("schedule_version").c.finalized_at
    assert finalized_at.nullable
    assert finalized_at.type.timezone is True


def test_schedule_version_period_is_pinned_to_its_schedules_period():
    """A version whose period is not its schedule's period is unrepresentable."""
    fks = _composite_fks(_table("schedule_version"))

    assert fks["fk_schedule_version_schedule_period"] == (
        ("schedule_id", "scheduling_period_id"),
        "schedule",
        ("id", "scheduling_period_id"),
        "RESTRICT",
    )


def test_an_amendment_must_amend_a_version_of_the_same_schedule():
    """A version from Schedule A cannot claim to amend one from Schedule B (§9)."""
    fks = _composite_fks(_table("schedule_version"))

    assert fks["fk_schedule_version_amends_same_schedule"] == (
        ("amends_version_id", "schedule_id"),
        "schedule_version",
        ("id", "schedule_id"),
        "RESTRICT",
    )


def test_schedule_version_exposes_both_parent_keys_its_children_need():
    unique_sets = _unique_column_sets(_table("schedule_version"))

    # For schedule_version_requirement's period spine.
    assert ("id", "scheduling_period_id") in unique_sets
    # For the self-referential amends foreign key above.
    assert ("id", "schedule_id") in unique_sets


def test_an_amendment_must_carry_a_reason():
    """An emergency change to a published schedule is never unexplained (§14)."""
    check = _checks(_table("schedule_version"))[
        "ck_schedule_version_amendment_reason_required"
    ]

    assert "amends_version_id IS NULL" in check
    assert "amendment_reason IS NOT NULL" in check
    assert "length(btrim(amendment_reason)) > 0" in check


def test_a_version_cannot_amend_itself():
    check = _checks(_table("schedule_version"))["ck_schedule_version_amends_not_self"]

    assert "amends_version_id <> id" in check


def test_amendment_columns_are_named_amends_not_supersedes():
    """While a draft amendment exists it has superseded nothing (§14)."""
    columns = set(_table("schedule_version").c.keys())

    assert "amends_version_id" in columns
    assert "amendment_reason" in columns
    assert "supersedes_version_id" not in columns
    assert "superseded_by_version_id" not in columns


def test_the_database_does_not_require_the_amended_version_to_be_finalized():
    """That is service validation; the database enforces only same-schedule (§14)."""
    ddl = _table_ddl("schedule_version")
    checks = _checks(_table("schedule_version"))

    assert "ck_schedule_version_amends_finalized" not in checks
    assert "amends_version_id IS NULL OR" in ddl  # the reason/self checks only
    for check in checks.values():
        if "amends_version_id" in check:
            assert "status" not in check


def test_the_authoritative_version_lookup_index_exists():
    """The hottest query this slice adds (§15, ADR 0003)."""
    ddl = _index_ddl(_table("schedule_version"), "ix_schedule_version_authoritative")

    assert "schedule_id" in ddl
    assert "version_number DESC" in ddl
    assert "WHERE status = 'FINALIZED'" in ddl


# --------------------------------------------------------------------------
# The authoritative version is derived, never stored
# --------------------------------------------------------------------------


def test_no_stored_authoritative_version_pointer_anywhere():
    """ADR 0003: highest-numbered FINALIZED version, derived by query.

    A stored pointer could disagree with the statuses it summarises, and would
    create a referential cycle between schedule and schedule_version.
    """
    forbidden = {
        "current_version_id",
        "finalized_version_id",
        "authoritative_version_id",
        "latest_version_id",
        "active_version_id",
        "is_authoritative",
        "is_current",
        "is_latest",
    }

    for name in SCHEDULE_OUTPUT_TABLES:
        assert forbidden.isdisjoint(set(_table(name).c.keys())), name


def test_schedule_version_stores_no_derived_lifecycle_flags():
    """Supersession and mutability are derivable from version numbers (§6, §7)."""
    columns = set(_table("schedule_version").c.keys())

    assert columns.isdisjoint(
        {"is_superseded", "is_mutable", "is_editable", "is_stale", "stale_at"}
    )


def test_no_is_stale_column_anywhere():
    """Staleness is an exact set comparison at query time (§13, decision 14)."""
    for name in SCHEDULE_OUTPUT_TABLES:
        assert "is_stale" not in _table(name).c, name


# --------------------------------------------------------------------------
# ScheduleVersionRequirement — the immutable snapshot
# --------------------------------------------------------------------------


def test_the_snapshot_is_one_row_per_version_event_and_role():
    assert (
        "schedule_version_id",
        "event_id",
        "ministry_role_id",
    ) in _unique_column_sets(_table("schedule_version_requirement"))


def test_snapshot_required_count_must_be_positive():
    check = _checks(_table("schedule_version_requirement"))[
        "ck_schedule_version_requirement_required_count_positive"
    ]

    assert "required_count > 0" in check
    assert isinstance(
        _table("schedule_version_requirement").c.required_count.type, Integer
    )


def test_the_snapshot_carries_exactly_the_reviewed_columns():
    assert set(_table("schedule_version_requirement").c.keys()) == {
        "id",
        "schedule_version_id",
        "event_id",
        "event_date",
        "ministry_role_id",
        "scheduling_period_id",
        "ministry_id",
        "required_count",
        "created_at",
    }


def test_the_snapshot_period_and_ministry_spines_are_complete():
    """Version, event and role must all agree, through shared carried columns."""
    assert _composite_fks(_table("schedule_version_requirement")) == {
        "fk_schedule_version_requirement_version_period": (
            ("schedule_version_id", "scheduling_period_id"),
            "schedule_version",
            ("id", "scheduling_period_id"),
            "RESTRICT",
        ),
        "fk_schedule_version_requirement_event_period": (
            ("event_id", "scheduling_period_id"),
            "event",
            ("id", "scheduling_period_id"),
            "RESTRICT",
        ),
        "fk_schedule_version_requirement_event_ministry": (
            ("event_id", "ministry_id"),
            "event",
            ("id", "ministry_id"),
            "RESTRICT",
        ),
        "fk_schedule_version_requirement_role_ministry": (
            ("ministry_role_id", "ministry_id"),
            "ministry_role",
            ("id", "ministry_id"),
            "RESTRICT",
        ),
    }


def test_the_snapshot_exposes_the_four_column_parent_key_assignment_needs():
    """Column order matters: assignment's composite FK references it positionally."""
    assert (
        "id",
        "schedule_version_id",
        "event_id",
        "ministry_id",
    ) in _unique_column_sets(_table("schedule_version_requirement"))


def test_the_snapshot_has_no_foreign_key_to_staffing_requirement():
    """A reference would make the mutable input row undeletable (§8).

    That coupling is precisely what the snapshot exists to break.
    """
    requirement = _table("schedule_version_requirement")

    assert "staffing_requirement_id" not in requirement.c
    for fk in requirement.foreign_keys:
        assert fk.column.table.name != "staffing_requirement"


def test_snapshot_event_date_is_a_non_null_date():
    """A civil date; a timezone-shifted one would be a conflict-logic bug."""
    event_date = _table("schedule_version_requirement").c.event_date

    assert isinstance(event_date.type, Date)
    assert not event_date.nullable


def test_snapshot_event_date_is_deliberately_not_pinned_to_the_event_row():
    """The single carried column in this slice that is intentionally free.

    No foreign key, no check, no generated column and no default may tie it to
    ``event.event_date``: divergence is the whole point (§3, §9, §13). If
    Version 3 was finalized for 15 November and the event is later moved to 22
    November, Version 3 must go on meaning 15 November.
    """
    requirement = _table("schedule_version_requirement")
    event_date = requirement.c.event_date

    # Not part of any foreign key.
    for fk in requirement.foreign_keys:
        assert fk.parent.name != "event_date"
    for constraint in requirement.constraints:
        if isinstance(constraint, ForeignKeyConstraint):
            assert "event_date" not in [c.name for c in constraint.columns]

    # Not tied by a check constraint either.
    for check in _checks(requirement).values():
        assert "event_date" not in check

    # And nothing computes or defaults it.
    assert event_date.server_default is None
    assert event_date.default is None
    assert event_date.onupdate is None
    assert event_date.server_onupdate is None
    assert event_date.computed is None


def test_the_snapshot_records_no_display_labels():
    """Only event_date is snapshotted; renames must show through everywhere (§3)."""
    columns = set(_table("schedule_version_requirement").c.keys())

    assert columns.isdisjoint(
        {
            "event_name",
            "ministry_role_name",
            "ministry_name",
            "role_name",
            "event_kind",
        }
    )


def test_the_staleness_comparison_tuple_is_available_on_both_sides():
    """(event_id, event_date, ministry_role_id, required_count) — §13, decision 14.

    The comparison is against current staffing_requirement rows joined to their
    events, so every element must exist on the snapshot and be reachable on the
    current side.
    """
    snapshot = _table("schedule_version_requirement").c
    current_requirement = _table("staffing_requirement").c
    event = _table("event").c

    for column in ("event_id", "event_date", "ministry_role_id", "required_count"):
        assert column in snapshot, column

    assert "event_id" in current_requirement
    assert "ministry_role_id" in current_requirement
    assert "required_count" in current_requirement
    assert "event_date" in event
    assert "cancelled_at" in event


# --------------------------------------------------------------------------
# Assignment
# --------------------------------------------------------------------------


def test_assignment_carries_exactly_the_reviewed_narrow_shape():
    assert set(_table("assignment").c.keys()) == {
        "id",
        "schedule_version_requirement_id",
        "ministry_membership_id",
        "schedule_version_id",
        "event_id",
        "ministry_id",
        "is_override",
        "override_reason",
        "created_at",
        "updated_at",
    }


def test_assignment_has_no_person_role_period_or_origin_columns():
    """Person comes through the membership, role through the requirement (§10, §11)."""
    columns = set(_table("assignment").c.keys())

    assert columns.isdisjoint(
        {
            "person_id",
            "ministry_role_id",
            "scheduling_period_id",
            "origin",
            "source",
            "generated_by",
            "existing_commitment_id",
        }
    )


def test_assignment_is_pinned_to_its_requirement_by_one_four_column_key():
    """One constraint guaranteeing version, event and ministry all match (§10)."""
    fks = _composite_fks(_table("assignment"))

    assert fks["fk_assignment_requirement_version_event_ministry"] == (
        (
            "schedule_version_requirement_id",
            "schedule_version_id",
            "event_id",
            "ministry_id",
        ),
        "schedule_version_requirement",
        ("id", "schedule_version_id", "event_id", "ministry_id"),
        "RESTRICT",
    )


def test_assignment_cannot_use_another_ministrys_membership():
    fks = _composite_fks(_table("assignment"))

    assert fks["fk_assignment_membership_ministry"] == (
        ("ministry_membership_id", "ministry_id"),
        "ministry_membership",
        ("id", "ministry_id"),
        "RESTRICT",
    )


def test_one_membership_serves_at_most_once_per_event_per_version():
    """One constraint satisfying two approved per-event person rules (§10)."""
    assert (
        "schedule_version_id",
        "event_id",
        "ministry_membership_id",
    ) in _unique_column_sets(_table("assignment"))


def test_several_assignments_may_fill_one_requirement():
    """required_count > 1 means three greeters are three rows against one row.

    Any uniqueness touching schedule_version_requirement_id alone — or that
    column with the version or event, which the FK already pins to it — would
    make a multi-person role unrepresentable.
    """
    for columns in _unique_column_sets(_table("assignment")):
        assert "schedule_version_requirement_id" not in columns, columns


def test_over_filling_is_left_to_service_validation():
    """An aggregate compared with another table; no single-row rule sees it (§12)."""
    ddl = _table_ddl("assignment")

    assert "required_count" not in ddl
    assert "count(" not in ddl.lower()


def test_an_override_must_carry_a_meaningful_reason():
    check = _checks(_table("assignment"))["ck_assignment_override_reason_required"]

    assert "NOT is_override" in check
    assert "override_reason IS NOT NULL" in check
    assert "length(btrim(override_reason)) > 0" in check


def test_override_defaults_to_false_and_stores_no_rule_code():
    """A required free-text reason, not the beginning of an override engine (§14)."""
    assignment = _table("assignment")

    assert isinstance(assignment.c.is_override.type, Boolean)
    assert not assignment.c.is_override.nullable
    assert assignment.c.is_override.server_default is not None
    assert assignment.c.override_reason.nullable
    assert set(assignment.c.keys()).isdisjoint(
        {"overridden_rule", "override_rule_code", "override_kind"}
    )


def test_the_assignment_indexes_serve_the_two_reviewed_queries():
    assignment = _table("assignment")
    index_columns = {
        ix.name: tuple(c.name for c in ix.columns) for ix in assignment.indexes
    }

    # The filled-count aggregate (§12).
    assert index_columns["ix_assignment_schedule_version_requirement_id"] == (
        "schedule_version_requirement_id",
    )
    # The ADR 0002 conflict query (§15).
    assert index_columns["ix_assignment_ministry_membership_id_event_id"] == (
        "ministry_membership_id",
        "event_id",
    )


# --------------------------------------------------------------------------
# The ADR 0002 / 0003 conflict query path
# --------------------------------------------------------------------------


def test_the_conflict_query_path_is_joinable_end_to_end():
    """authoritative version -> assignment -> snapshot date -> membership -> person.

    The date must come from the snapshot, so the test asserts the join can be
    built without ever touching ``event.event_date``.
    """
    assignment = _table("assignment")
    requirement = _table("schedule_version_requirement")
    version = _table("schedule_version")
    membership = _table("ministry_membership")

    joined = (
        assignment.join(
            requirement,
            assignment.c.schedule_version_requirement_id == requirement.c.id,
        )
        .join(version, assignment.c.schedule_version_id == version.c.id)
        .join(
            membership,
            assignment.c.ministry_membership_id == membership.c.id,
        )
    )

    compiled = str(joined.select().compile(dialect=postgresql.dialect()))
    assert "schedule_version_requirement.event_date" in compiled
    assert "person_id" in compiled  # reached through the membership, not stored
    assert isinstance(requirement.c.event_date.type, Date)
    assert requirement.c.event_date.type.python_type is datetime.date

    # Both halves of the ADR 0002 union are still separately available, with
    # assignments never mirrored into existing_commitment.
    assert "assignment_id" not in _table("existing_commitment").c


def test_assignments_are_not_linked_to_existing_commitments():
    """The blocked set is a union computed at query time, never mirrored (ADR 0002)."""
    assignment = _table("assignment")

    assert "existing_commitment_id" not in assignment.c
    for fk in assignment.foreign_keys:
        assert fk.column.table.name != "existing_commitment"


# --------------------------------------------------------------------------
# Naming convention
# --------------------------------------------------------------------------


def test_new_constraint_and_index_names_follow_the_convention():
    for name in SCHEDULE_OUTPUT_TABLES:
        table = _table(name)

        assert table.primary_key.name.startswith("pk_"), name
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                assert constraint.name.startswith("uq_"), constraint.name
            elif isinstance(constraint, CheckConstraint):
                assert constraint.name.startswith("ck_"), constraint.name
            elif isinstance(constraint, ForeignKeyConstraint):
                assert constraint.name.startswith("fk_"), constraint.name
        for index in table.indexes:
            assert index.name.startswith(("ix_", "uq_")), index.name


def test_no_new_identifier_exceeds_the_postgresql_limit():
    """schedule_version_requirement is 28 characters on its own.

    The convention's generated names for its wide composite keys would be
    silently truncated, so they are named explicitly at their definition site.
    """
    for name in SCHEDULE_OUTPUT_TABLES:
        table = _table(name)
        identifiers = (
            [table.name]
            + [c.name for c in table.constraints if c.name]
            + [i.name for i in table.indexes]
        )

        for identifier in identifiers:
            assert len(str(identifier)) <= 63, identifier

    assert len("uq_event_id_scheduling_period_id") <= 63
