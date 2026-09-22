"""Audit-event schema metadata tests.

Offline only: these assert against ``Base.metadata`` and the compiled DDL, never
opening a connection. The suite still needs no PostgreSQL, no ``.env`` and no
network.

They protect the invariants ``docs/architecture/audit-event-data-model.md``
(revision 2) and ``docs/adr/0004-generic-audit-target-references.md`` call
load-bearing — and, just as importantly, the things the reviewed design says
must **not** be there. Several deliberate absences (no `target_label`, no
`created_at`/`updated_at`, no foreign key on `target_id`, no GIN index) would be
silently "helpful" additions that break the accepted architecture without
breaking a single import.
"""

import re
import warnings

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  (registers the mapped classes)
from app.db import Base
from app.models.audit import (
    AUDIT_ACTOR_TYPE_PERSON,
    AUDIT_ACTOR_TYPE_SYSTEM,
    AUDIT_TARGET_TABLES,
)

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
    # The per-person, per-period hard serving maximum (Task 47).
    "membership_serving_limit",
    # The per-pair, per-period hard same-date exclusion (Task 50).
    "membership_same_date_exclusion",
    # Task 74's two generic hard rules: a ministry's own member categories and
    # the per-event cap one period puts on a category, then the per-subject,
    # per-period same-event support requirement and its approved supporters.
    "member_group",
    "member_group_member",
    "member_group_event_limit",
    "membership_support_requirement",
    "membership_support_supporter",
    "scheduling_period",
    "staffing_requirement",
}

SCHEDULE_OUTPUT_TABLES = {
    "assignment",
    "schedule",
    "schedule_version",
    "schedule_version_requirement",
}

AUDIT_TABLES = {"audit_event"}

#: The reviewed target names (audit §5.2). Spelled out here rather than
#: imported, so that a change to the model's tuple has to be made deliberately
#: in two places.
EXPECTED_TARGET_TABLES = {
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
    # Task 47: a head recording, changing or clearing somebody's serving
    # maximum is audited like any other domain change.
    "membership_serving_limit",
    # Task 50: a head configuring or clearing a linked-pair same-date
    # exclusion is audited the same way. The row names the two memberships and
    # the period; it never names a relationship.
    "membership_same_date_exclusion",
    # Task 74: configuring a member group, who is in it, the per-event cap on
    # it, or a same-event support requirement is audited like any other domain
    # change.
    #
    # ``membership_support_supporter`` is deliberately **not** here: a
    # supporter row is only ever written as part of configuring the
    # requirement it belongs to, so that requirement is what the history
    # names, with the supporter set in the payload.
    "member_group",
    "member_group_member",
    "member_group_event_limit",
    "membership_support_requirement",
}


def _table(name):
    return Base.metadata.tables[name]


def _checks(table):
    return {
        con.name: str(con.sqltext)
        for con in table.constraints
        if isinstance(con, CheckConstraint)
    }


def _table_ddl(name):
    return str(CreateTable(_table(name)).compile(dialect=postgresql.dialect()))


def _index_ddl(table, index_name):
    index = next(ix for ix in table.indexes if ix.name == index_name)
    return str(CreateIndex(index).compile(dialect=postgresql.dialect()))


def _single_column_fks(table):
    """{local column: (referred table, referred column, ondelete)}."""
    return {
        tuple(c.name for c in con.columns)[0]: (
            con.elements[0].column.table.name,
            con.elements[0].column.name,
            con.ondelete,
        )
        for con in table.constraints
        if isinstance(con, ForeignKeyConstraint) and len(con.columns) == 1
    }


# --------------------------------------------------------------------------
# Table set
# --------------------------------------------------------------------------


def test_metadata_contains_exactly_the_twenty_four_application_tables():
    # Twenty-four since Task 74 added the five member-group and same-event
    # support tables. The count is asserted alongside the set so that adding a
    # table to the expected set without meaning to still fails here.
    assert set(Base.metadata.tables) == (
        CORE_TABLES | SCHEDULING_INPUT_TABLES | SCHEDULE_OUTPUT_TABLES | AUDIT_TABLES
    )
    assert len(Base.metadata.tables) == 24


def test_audit_event_table_exists():
    assert "audit_event" in Base.metadata.tables


def test_configuration_and_shadow_tables_remain_absent():
    """Still deferred to their own slices (audit §21)."""
    deferred = {
        "constraint_configuration",
        "preference_configuration",
        "shadow_assignment",
        "ministry_default_staffing",
        "schedule_version_diagnostic",
        "solver_run",
        "login_event",
        "notification",
        "audit_event_field_change",
    }

    assert deferred.isdisjoint(Base.metadata.tables)


def test_mappers_configure_without_warnings_or_errors():
    """Import alone does not catch relationship misconfiguration."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        configure_mappers()


# --------------------------------------------------------------------------
# Columns
# --------------------------------------------------------------------------


def test_audit_event_has_exactly_the_reviewed_columns():
    assert set(_table("audit_event").c.keys()) == {
        "id",
        "occurred_at",
        "actor_type",
        "actor_person_id",
        "actor_label",
        "action",
        "target_table",
        "target_id",
        "ministry_id",
        "summary",
        "reason",
        "before_values",
        "after_values",
    }


def test_audit_event_has_no_created_at_updated_at_or_target_label():
    """occurred_at is both times; the row is immutable; the summary names the
    target (audit §14, §15)."""
    columns = set(_table("audit_event").c.keys())

    assert columns.isdisjoint(
        {
            "created_at",
            "updated_at",
            "target_label",
            "changed_fields",
            "payload_version",
            "actor_user_account_id",
            "user_account_id",
            "succeeded",
        }
    )


def test_audit_event_has_a_bigint_identity_primary_key():
    pk = list(_table("audit_event").primary_key.columns)

    assert [c.name for c in pk] == ["id"]
    assert isinstance(pk[0].type, BigInteger)
    assert pk[0].identity is not None and pk[0].identity.always is True


def test_occurred_at_is_a_non_null_timestamptz_defaulting_to_now():
    occurred_at = _table("audit_event").c.occurred_at

    assert isinstance(occurred_at.type, DateTime)
    assert occurred_at.type.timezone is True
    assert not occurred_at.nullable
    assert occurred_at.server_default is not None
    # Immutable rows never get an ORM-maintained update timestamp.
    assert occurred_at.onupdate is None


def test_nullability_matches_the_reviewed_design():
    audit = _table("audit_event")

    for required in (
        "occurred_at",
        "actor_type",
        "actor_label",
        "action",
        "target_table",
        "target_id",
        "summary",
    ):
        assert not audit.c[required].nullable, required

    for optional in ("actor_person_id", "ministry_id", "reason",
                     "before_values", "after_values"):
        assert audit.c[optional].nullable, optional


# --------------------------------------------------------------------------
# Actor
# --------------------------------------------------------------------------


def test_actor_person_id_is_a_real_fk_to_person_with_restrict():
    fks = _single_column_fks(_table("audit_event"))

    assert fks["actor_person_id"] == ("person", "id", "RESTRICT")


def test_actor_never_references_user_account():
    """Person is the stable identity; user_account may be hard-deleted."""
    audit = _table("audit_event")

    for fk in audit.foreign_keys:
        assert fk.column.table.name != "user_account", fk.parent.name


def test_actor_type_allows_only_person_and_system():
    check = _checks(_table("audit_event"))["ck_audit_event_actor_type_valid"]

    assert "'PERSON'" in check
    assert "'SYSTEM'" in check
    for forbidden in ("SERVICE", "SERVICE_ACCOUNT", "ANONYMOUS", "UNKNOWN", "IMPORT"):
        assert forbidden not in check

    actor_type = _table("audit_event").c.actor_type
    assert isinstance(actor_type.type, Text)
    assert not actor_type.nullable


def test_actor_type_constants_match_the_check():
    assert AUDIT_ACTOR_TYPE_PERSON == "PERSON"
    assert AUDIT_ACTOR_TYPE_SYSTEM == "SYSTEM"


def test_the_two_way_actor_invariant_exists():
    """PERSON requires the reference; SYSTEM requires its absence.

    Deliberately an equality rather than a one-way implication, so NULL is never
    ambiguous between "the system did it" and "we forgot to record who".
    """
    check = _checks(_table("audit_event"))[
        "ck_audit_event_actor_type_matches_actor_person_id"
    ]

    assert "(actor_type = 'PERSON') = (actor_person_id IS NOT NULL)" in check


def test_actor_label_must_be_non_blank():
    """Whitespace-only labels must fail, not merely empty strings."""
    check = _checks(_table("audit_event"))["ck_audit_event_actor_label_not_blank"]

    assert "length(btrim(actor_label)) > 0" in check


def test_no_service_account_table_was_created():
    assert "service_account" not in Base.metadata.tables


# --------------------------------------------------------------------------
# Action
# --------------------------------------------------------------------------


def test_action_has_a_shape_check_not_a_frozen_value_list():
    """action is an open catalogue that is displayed and filtered, never
    branched on (audit §6.2)."""
    check = _checks(_table("audit_event"))["ck_audit_event_action_shape_valid"]

    assert "action ~ " in check
    # A regex, not an IN list.
    assert "IN (" not in check
    for example in ("MINISTRY_HEAD_GRANTED", "QUALIFICATION_APPROVED"):
        assert example not in check


def test_the_action_pattern_accepts_the_reviewed_examples_and_rejects_junk():
    check = _checks(_table("audit_event"))["ck_audit_event_action_shape_valid"]
    pattern = re.search(r"action ~ '(.+)'", check).group(1)

    for accepted in (
        "MINISTRY_HEAD_GRANTED",
        "QUALIFICATION_APPROVED",
        "ASSIGNMENT_OVERRIDE_APPLIED",
        "SCHEDULE_FINALIZED",
        "PERSON_CREATED",
        "ADMIN_REVOKED",
    ):
        assert re.match(pattern, accepted), accepted

    for rejected in (
        "person updated",
        "Updated",
        "lowercase_action",
        "TRAILING_",
        "DOUBLE__UNDERSCORE",
        "_LEADING",
        "9_STARTS_WITH_DIGIT",
        "",
    ):
        assert not re.match(pattern, rejected), rejected


def test_action_is_text_not_a_native_enum():
    action = _table("audit_event").c.action

    assert isinstance(action.type, Text)
    assert "CREATE TYPE" not in _table_ddl("audit_event").upper()


# --------------------------------------------------------------------------
# Target — the generic, unenforced reference (ADR 0004)
# --------------------------------------------------------------------------


def test_target_id_has_no_foreign_key():
    """The central trade-off of the slice, and the thing most likely to be
    'fixed' by mistake later."""
    audit = _table("audit_event")

    for fk in audit.foreign_keys:
        assert fk.parent.name != "target_id"

    target_id = audit.c.target_id
    assert isinstance(target_id.type, BigInteger)
    assert not target_id.nullable


def test_target_table_check_contains_exactly_the_nineteen_audited_names():
    check = _checks(_table("audit_event"))["ck_audit_event_target_table_valid"]
    names = set(re.findall(r"'([a-z_]+)'", check))

    assert names == EXPECTED_TARGET_TABLES
    assert len(names) == 19


def test_target_table_uses_sql_table_names_not_python_class_names():
    check = _checks(_table("audit_event"))["ck_audit_event_target_table_valid"]

    for class_name in (
        "MinistryMembership",
        "ScheduleVersion",
        "RoleQualification",
        "SchedulingPeriod",
    ):
        assert class_name not in check


def test_target_table_does_not_silently_include_unaudited_tables():
    """church, user_account, the requirement snapshot and audit_event itself."""
    check = _checks(_table("audit_event"))["ck_audit_event_target_table_valid"]

    for excluded in (
        "'church'",
        "'user_account'",
        "'schedule_version_requirement'",
        "'audit_event'",
        "'constraint_configuration'",
        "'preference_configuration'",
        "'shadow_assignment'",
    ):
        assert excluded not in check


def test_every_audited_target_name_is_a_real_table_in_the_metadata():
    """A target name that does not name a table would be unresolvable."""
    for name in EXPECTED_TARGET_TABLES:
        assert name in Base.metadata.tables, name


def test_the_model_constant_matches_the_check_list():
    assert set(AUDIT_TARGET_TABLES) == EXPECTED_TARGET_TABLES
    assert len(AUDIT_TARGET_TABLES) == 19


def test_target_id_must_be_positive():
    check = _checks(_table("audit_event"))["ck_audit_event_target_id_positive"]

    assert "target_id > 0" in check


# --------------------------------------------------------------------------
# Ministry context
# --------------------------------------------------------------------------


def test_ministry_id_is_a_real_nullable_fk_to_ministry_with_restrict():
    fks = _single_column_fks(_table("audit_event"))

    assert fks["ministry_id"] == ("ministry", "id", "RESTRICT")
    assert _table("audit_event").c.ministry_id.nullable


def test_no_composite_constraint_tries_to_pin_ministry_to_the_target():
    """PostgreSQL cannot prove ministry_id matches a per-row target table.

    That agreement is a service-layer invariant (audit §8); inventing a
    composite key here would be a design change.
    """
    audit = _table("audit_event")

    composite_fks = [
        con
        for con in audit.constraints
        if isinstance(con, ForeignKeyConstraint) and len(con.columns) > 1
    ]
    assert composite_fks == []

    # And audit introduces no parent unique keys of its own.
    uniques = [
        con for con in audit.constraints if isinstance(con, UniqueConstraint)
    ]
    assert uniques == []


# --------------------------------------------------------------------------
# Summary and reason
# --------------------------------------------------------------------------


def test_summary_is_required_and_non_blank():
    audit = _table("audit_event")
    check = _checks(audit)["ck_audit_event_summary_not_blank"]

    assert "length(btrim(summary)) > 0" in check
    assert not audit.c.summary.nullable


def test_reason_is_nullable_but_non_blank_when_present():
    audit = _table("audit_event")
    check = _checks(audit)["ck_audit_event_reason_not_blank"]

    assert "reason IS NULL OR length(btrim(reason)) > 0" in check
    assert audit.c.reason.nullable


def test_no_action_specific_reason_requirement_in_the_database():
    """Reason-required policy belongs to the domain service (audit §9).

    A CHECK naming actions would re-freeze the vocabulary in the one place a new
    required-reason rule is most likely to be added.
    """
    for name, check in _checks(_table("audit_event")).items():
        if "reason" in check:
            assert "action" not in check, name
            assert "OVERRIDE" not in check, name
            assert "AMENDED" not in check, name


# --------------------------------------------------------------------------
# JSONB payloads
# --------------------------------------------------------------------------


def test_payload_columns_use_postgresql_jsonb():
    audit = _table("audit_event")

    for column in ("before_values", "after_values"):
        assert isinstance(audit.c[column].type, JSONB), column

    assert "JSONB" in _table_ddl("audit_event")


def test_absent_payloads_are_configured_to_persist_as_sql_null():
    """``none_as_null=True`` on both payload columns, and why it matters.

    SQLAlchemy's JSONB defaults to ``none_as_null=False``, which sends Python
    ``None`` as the JSON scalar ``null`` instead of SQL ``NULL``. Since
    ``jsonb_typeof('null'::jsonb)`` is ``'null'`` and not ``'object'``, that
    default makes every creation audit (no before) and every removal audit (no
    after) violate the object-or-NULL CHECKs below -- which is exactly what
    the Task 25 PostgreSQL suite caught.

    This assertion protects the *configuration* only. It cannot prove what
    PostgreSQL stores; ``tests/integration/test_pg_audit_payload_null.py``
    does that against a real database.
    """
    audit = _table("audit_event")

    assert audit.c.before_values.type.none_as_null is True
    assert audit.c.after_values.type.none_as_null is True


def test_the_none_as_null_setting_changes_no_ddl():
    """It is a client-side bind behavior, so the column type and its CHECKs
    are unaffected -- which is why the fix needed no migration.
    """
    ddl = _table_ddl("audit_event")

    assert "before_values JSONB" in ddl
    assert "after_values JSONB" in ddl


def test_at_least_one_payload_must_be_non_null():
    check = _checks(_table("audit_event"))["ck_audit_event_payload_present"]

    assert "before_values IS NOT NULL OR after_values IS NOT NULL" in check


def test_neither_payload_is_individually_required():
    """A creation has no before; a deletion has no after."""
    audit = _table("audit_event")

    assert audit.c.before_values.nullable
    assert audit.c.after_values.nullable


def test_non_null_payloads_must_be_json_objects():
    """A payload is a field map, never an array or a bare scalar."""
    checks = _checks(_table("audit_event"))

    assert (
        "before_values IS NULL OR jsonb_typeof(before_values) = 'object'"
        in checks["ck_audit_event_before_values_is_object"]
    )
    assert (
        "after_values IS NULL OR jsonb_typeof(after_values) = 'object'"
        in checks["ck_audit_event_after_values_is_object"]
    )


def test_no_payload_normalization_table_or_json_schema_machinery():
    assert "audit_event_field_change" not in Base.metadata.tables
    assert "audit_event_value" not in Base.metadata.tables

    ddl = _table_ddl("audit_event").upper()
    assert "TRIGGER" not in ddl
    assert "GENERATED ALWAYS AS (" not in ddl


# --------------------------------------------------------------------------
# Indexes
# --------------------------------------------------------------------------


def test_exactly_the_four_reviewed_indexes_exist():
    names = {ix.name for ix in _table("audit_event").indexes}

    assert names == {
        "ix_audit_event_occurred_at_id",
        "ix_audit_event_target_table_target_id_occurred_at",
        "ix_audit_event_ministry_id_occurred_at",
        "ix_audit_event_actor_person_id_occurred_at",
    }


def test_the_timeline_index_is_newest_first_with_a_stable_tiebreaker():
    ddl = _index_ddl(_table("audit_event"), "ix_audit_event_occurred_at_id")

    assert "occurred_at DESC" in ddl
    assert "id DESC" in ddl
    assert "WHERE" not in ddl


def test_the_target_history_index_leads_on_target_table():
    ddl = _index_ddl(
        _table("audit_event"), "ix_audit_event_target_table_target_id_occurred_at"
    )

    assert re.search(r"\(target_table,\s*target_id,\s*occurred_at DESC\)", ddl)
    assert "WHERE" not in ddl


def test_the_ministry_history_index_is_partial():
    """Church-wide rows are never part of a ministry-scoped answer."""
    ddl = _index_ddl(_table("audit_event"), "ix_audit_event_ministry_id_occurred_at")

    assert "ministry_id" in ddl
    assert "occurred_at DESC" in ddl
    assert "WHERE ministry_id IS NOT NULL" in ddl


def test_the_actor_history_index_is_partial():
    """SYSTEM rows have no actor and are never the answer."""
    ddl = _index_ddl(
        _table("audit_event"), "ix_audit_event_actor_person_id_occurred_at"
    )

    assert "actor_person_id" in ddl
    assert "occurred_at DESC" in ddl
    assert "WHERE actor_person_id IS NOT NULL" in ddl


def test_no_action_gin_or_summary_index():
    audit = _table("audit_event")
    all_index_ddl = "".join(_index_ddl(audit, ix.name) for ix in audit.indexes)

    assert "GIN" not in all_index_ddl.upper()
    assert "summary" not in all_index_ddl
    for ix in audit.indexes:
        assert [c.name for c in ix.columns] != ["action"], ix.name


def test_no_unique_index_was_added():
    for ix in _table("audit_event").indexes:
        assert not ix.unique, ix.name


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


def test_audit_event_exposes_simple_actor_and_ministry_relationships():
    from sqlalchemy import inspect

    from app.models import AuditEvent

    relationships = inspect(AuditEvent).relationships
    assert set(relationships.keys()) == {"actor_person", "ministry"}
    assert relationships["actor_person"].mapper.class_.__name__ == "Person"
    assert relationships["ministry"].mapper.class_.__name__ == "Ministry"


# --------------------------------------------------------------------------
# Naming convention
# --------------------------------------------------------------------------


def test_constraint_and_index_names_follow_the_convention():
    audit = _table("audit_event")

    assert audit.primary_key.name.startswith("pk_")
    for constraint in audit.constraints:
        if isinstance(constraint, CheckConstraint):
            assert constraint.name.startswith("ck_"), constraint.name
        elif isinstance(constraint, ForeignKeyConstraint):
            assert constraint.name.startswith("fk_"), constraint.name
    for index in audit.indexes:
        assert index.name.startswith("ix_"), index.name


def test_no_identifier_exceeds_the_postgresql_limit():
    audit = _table("audit_event")
    identifiers = (
        [audit.name]
        + [c.name for c in audit.constraints if c.name]
        + [i.name for i in audit.indexes]
    )

    for identifier in identifiers:
        assert len(str(identifier)) <= 63, identifier
