"""Scheduling-input schema metadata tests.

Offline only: these assert against ``Base.metadata`` and the compiled DDL, never
opening a connection. The suite still needs no PostgreSQL, no ``.env`` and no
network.

They protect the invariants ``docs/architecture/scheduling-input-data-model.md``
calls load-bearing — the ones whose silent loss would break no import but would
let the solver read wrong rows.
"""

from sqlalchemy import (
    BigInteger,
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


def test_the_scheduling_input_tables_are_registered():
    """A subset check: the schedule-output slice added four more tables.

    The exact total is asserted in test_models_schedule_output.py.
    """
    assert CORE_TABLES | SCHEDULING_INPUT_TABLES <= set(Base.metadata.tables)


def test_all_five_scheduling_input_tables_exist():
    assert SCHEDULING_INPUT_TABLES <= set(Base.metadata.tables)


def test_configuration_and_shadow_tables_remain_absent():
    """The schedule-output and audit tables arrived in their own slices; these
    have not.

    ``ministry_default_staffing`` in particular stays absent: a shared mutable
    staffing template would make history editable through the back door, which
    is why staffing_requirement rows are per-event (§6).
    """
    deferred = {
        "constraint_configuration",
        "ministry_default_staffing",
        "preference_configuration",
        "shadow_assignment",
    }

    assert deferred.isdisjoint(Base.metadata.tables)


def test_mappers_configure_without_error():
    """Catches relationship misconfiguration, which import alone does not."""
    configure_mappers()


# --------------------------------------------------------------------------
# Conventions shared with the core slice
# --------------------------------------------------------------------------


def test_every_new_table_has_a_bigint_identity_primary_key():
    for name in SCHEDULING_INPUT_TABLES:
        pk = list(_table(name).primary_key.columns)

        assert [c.name for c in pk] == ["id"], name
        assert isinstance(pk[0].type, BigInteger), name
        assert pk[0].identity is not None and pk[0].identity.always is True, name


def test_every_new_table_has_timezone_aware_timestamps():
    for name in SCHEDULING_INPUT_TABLES:
        table = _table(name)

        for column_name in ("created_at", "updated_at"):
            column = table.c[column_name]
            assert column.type.timezone is True, f"{name}.{column_name}"
            assert not column.nullable, f"{name}.{column_name}"
            assert column.server_default is not None, f"{name}.{column_name}"

        assert table.c.updated_at.onupdate is not None, name


def test_every_new_foreign_key_restricts_deletes():
    """Preserves the ON DELETE behaviour of the reviewed design."""
    for name in SCHEDULING_INPUT_TABLES:
        for fk in _table(name).foreign_keys:
            assert fk.ondelete == "RESTRICT", f"{name}.{fk.parent.name}"


def test_calendar_dates_are_date_columns_not_timestamps():
    """A civil date shifted by a timezone would be a conflict-logic bug."""
    assert isinstance(_table("event").c.event_date.type, Date)
    assert isinstance(_table("existing_commitment").c.commitment_date.type, Date)
    assert isinstance(_table("scheduling_period").c.start_date.type, Date)
    assert isinstance(_table("scheduling_period").c.end_date.type, Date)


def test_no_derived_sunday_or_week_key_anywhere():
    """A Saturday event must not consume the adjacent Sunday."""
    for name in SCHEDULING_INPUT_TABLES:
        columns = set(_table(name).c.keys())
        assert columns.isdisjoint(
            {"sunday_key", "sunday_date", "week_key", "week_start", "recurrence_rule"}
        ), name


# --------------------------------------------------------------------------
# SchedulingPeriod
# --------------------------------------------------------------------------


def test_scheduling_period_start_is_not_after_end():
    check = _checks(_table("scheduling_period"))[
        "ck_scheduling_period_start_not_after_end"
    ]

    assert "start_date <= end_date" in check


def test_scheduling_period_exposes_the_composite_fk_parent_key():
    assert ("id", "ministry_id") in _unique_column_sets(_table("scheduling_period"))


def test_scheduling_period_name_is_case_insensitively_unique_in_the_ministry():
    ddl = _index_ddl(
        _table("scheduling_period"), "uq_scheduling_period_ministry_id_name_lower"
    )

    assert "UNIQUE" in ddl
    assert "ministry_id" in ddl
    assert "lower(name)" in ddl


def test_scheduling_period_tracks_lock_state_as_a_nullable_timestamp_only():
    """No status enum, and no Draft/Review/Finalized/Amended column here."""
    period = _table("scheduling_period")

    assert period.c.availability_locked_at.nullable
    assert period.c.availability_locked_at.type.timezone is True
    for forbidden in (
        "availability_state",
        "status",
        "state",
        "workflow_state",
        "availability_opens_at",
    ):
        assert forbidden not in period.c


def test_overlapping_periods_are_not_forbidden_by_the_database():
    """Overlap is a workflow mistake, left to service-layer validation."""
    ddl = _table_ddl("scheduling_period")

    assert "EXCLUDE" not in ddl.upper()


# --------------------------------------------------------------------------
# Event
# --------------------------------------------------------------------------


def test_event_ministry_is_pinned_to_its_scheduling_period():
    """An event under a Setup period cannot claim to belong to AV."""
    assert _composite_fks(_table("event")) == {
        "fk_event_period_ministry": (
            ("scheduling_period_id", "ministry_id"),
            "scheduling_period",
            ("id", "ministry_id"),
            "RESTRICT",
        )
    }


def test_event_exposes_the_composite_fk_parent_key():
    assert ("id", "ministry_id") in _unique_column_sets(_table("event"))


def test_event_has_no_one_sunday_per_ministry_uniqueness_rule():
    """Morning/evening services must remain structurally possible.

    The approved rule is one ministry per *person* per Sunday — a constraint on
    people, not on how many events a ministry may hold.
    """
    event = _table("event")

    for columns in _unique_column_sets(event):
        assert "event_date" not in columns, columns

    ddl = _table_ddl("event") + "".join(
        _index_ddl(event, ix.name) for ix in event.indexes
    )
    assert "UNIQUE INDEX" not in ddl.upper()


def test_event_kind_allows_only_the_two_reviewed_values():
    check = _checks(_table("event"))["ck_event_event_kind_valid"]

    assert "'SUNDAY_SERVICE'" in check
    assert "'SPECIAL'" in check


def test_special_events_must_be_named_and_sundays_need_not_be():
    event = _table("event")

    assert event.c.name.nullable
    check = _checks(event)["ck_event_special_requires_name"]
    assert "SPECIAL" in check
    assert "name IS NOT NULL" in check


def test_event_has_no_time_of_day_or_recurrence_columns():
    columns = set(_table("event").c.keys())

    assert columns.isdisjoint(
        {"start_time", "end_time", "starts_at", "ends_at", "rrule", "recurrence"}
    )


def test_event_is_cancelled_not_deleted():
    cancelled_at = _table("event").c.cancelled_at

    assert cancelled_at.nullable
    assert cancelled_at.type.timezone is True


# --------------------------------------------------------------------------
# StaffingRequirement
# --------------------------------------------------------------------------


def test_staffing_requirement_is_one_row_per_event_and_role():
    assert ("event_id", "ministry_role_id") in _unique_column_sets(
        _table("staffing_requirement")
    )


def test_staffing_requirement_count_must_be_positive():
    """Not needed is the absence of a row, never a zero."""
    check = _checks(_table("staffing_requirement"))[
        "ck_staffing_requirement_required_count_positive"
    ]

    assert "required_count > 0" in check
    assert isinstance(_table("staffing_requirement").c.required_count.type, Integer)


def test_staffing_requirement_cannot_pair_an_event_with_another_ministrys_role():
    assert _composite_fks(_table("staffing_requirement")) == {
        "fk_staffing_requirement_event_ministry": (
            ("event_id", "ministry_id"),
            "event",
            ("id", "ministry_id"),
            "RESTRICT",
        ),
        "fk_staffing_requirement_role_ministry": (
            ("ministry_role_id", "ministry_id"),
            "ministry_role",
            ("id", "ministry_id"),
            "RESTRICT",
        ),
    }


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


def test_availability_allows_only_available_and_unavailable():
    """No UNKNOWN / NO_RESPONSE value: absence of a row is no response."""
    check = _checks(_table("availability"))["ck_availability_availability_state_valid"]

    assert "'AVAILABLE'" in check
    assert "'UNAVAILABLE'" in check
    for forbidden in ("UNKNOWN", "NO_RESPONSE", "TENTATIVE"):
        assert forbidden not in check

    state = _table("availability").c.availability_state
    assert isinstance(state.type, Text)
    assert not state.nullable


def test_availability_is_one_standing_answer_per_membership_and_event():
    assert ("ministry_membership_id", "event_id") in _unique_column_sets(
        _table("availability")
    )


def test_availability_is_scoped_to_a_membership_not_a_person():
    """One person in Setup and AV may answer differently for each."""
    availability = _table("availability")

    assert "ministry_membership_id" in availability.c
    assert "person_id" not in availability.c


def test_availability_cannot_answer_for_another_ministrys_event():
    assert _composite_fks(_table("availability")) == {
        "fk_availability_membership_ministry": (
            ("ministry_membership_id", "ministry_id"),
            "ministry_membership",
            ("id", "ministry_id"),
            "RESTRICT",
        ),
        "fk_availability_event_ministry": (
            ("event_id", "ministry_id"),
            "event",
            ("id", "ministry_id"),
            "RESTRICT",
        ),
    }


def test_availability_does_not_encode_a_blank_means_available_policy():
    """That policy belongs to the importer and solver input, not the table."""
    availability = _table("availability")

    assert availability.c.availability_state.server_default is None
    assert availability.c.availability_state.default is None
    assert "assumed" not in set(availability.c.keys())


# --------------------------------------------------------------------------
# ExistingCommitment
# --------------------------------------------------------------------------


def test_existing_commitment_requires_provenance():
    check = _checks(_table("existing_commitment"))[
        "ck_existing_commitment_provenance_required"
    ]

    assert "source_ministry_id IS NOT NULL" in check
    assert "reason IS NOT NULL" in check


def test_existing_commitment_uniqueness_uses_nulls_not_distinct():
    """Plain UNIQUE would let duplicate NULL-source blocks through."""
    constraint = next(
        con
        for con in _table("existing_commitment").constraints
        if isinstance(con, UniqueConstraint)
        and con.name == "uq_existing_commitment_person_date_source"
    )

    assert tuple(c.name for c in constraint.columns) == (
        "person_id",
        "commitment_date",
        "source_ministry_id",
    )
    assert constraint.dialect_options["postgresql"]["nulls_not_distinct"] is True
    assert "UNIQUE NULLS NOT DISTINCT" in _table_ddl("existing_commitment")


def test_existing_commitment_is_church_wide_and_has_no_assignment_link():
    """The block references the canonical Person; assignments are never mirrored."""
    commitment = _table("existing_commitment")

    assert "person_id" in commitment.c
    assert commitment.c.source_ministry_id.nullable
    assert "assignment_id" not in commitment.c
    assert "ministry_membership_id" not in commitment.c


# --------------------------------------------------------------------------
# Naming convention
# --------------------------------------------------------------------------


def test_new_constraint_and_index_names_follow_the_convention():
    for name in SCHEDULING_INPUT_TABLES:
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
    for name in SCHEDULING_INPUT_TABLES:
        table = _table(name)
        identifiers = (
            [table.name]
            + [c.name for c in table.constraints if c.name]
            + [i.name for i in table.indexes]
        )

        for identifier in identifiers:
            assert len(str(identifier)) <= 63, identifier
