"""Core schema metadata tests.

Offline only: these assert against ``Base.metadata`` and the compiled DDL. They
construct SQLAlchemy objects and never open a connection, so the suite still
needs no PostgreSQL and no network.

What they protect is the set of invariants the design document
(``docs/architecture/core-data-model.md``) calls out as load-bearing — the ones
whose silent loss would not break any import but would let bad rows into the
database.
"""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import configure_mappers
from sqlalchemy.schema import CreateIndex

import app.models  # noqa: F401  (registers the mapped classes)
from app.db import Base

EXPECTED_TABLES = {
    "church",
    "ministry",
    "ministry_membership",
    "ministry_role",
    "person",
    "role_qualification",
    "user_account",
}


def _table(name):
    return Base.metadata.tables[name]


def _unique_column_sets(table):
    """Column-name tuples of every UNIQUE constraint and unique index."""
    sets = {
        tuple(c.name for c in con.columns)
        for con in table.constraints
        if isinstance(con, UniqueConstraint)
    }
    sets |= {
        tuple(c.name for c in ix.columns) for ix in table.indexes if ix.unique
    }
    return sets


def _checks(table):
    """Map of check-constraint name -> its SQL text."""
    return {
        con.name: str(con.sqltext)
        for con in table.constraints
        if isinstance(con, CheckConstraint)
    }


def _index_ddl(table, index_name):
    index = next(ix for ix in table.indexes if ix.name == index_name)
    return str(CreateIndex(index).compile(dialect=postgresql.dialect()))


# --------------------------------------------------------------------------
# Table set
# --------------------------------------------------------------------------


def test_all_seven_core_tables_are_registered():
    """The core slice's own tables.

    A subset check, not equality: later slices add their own tables to the same
    metadata. The exact total is asserted in test_models_scheduling_input.py.
    """
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_no_configuration_or_shadow_tables_are_defined_yet():
    """Later slices must not leak in.

    The scheduling *input*, schedule *output* and *audit* tables each arrived
    with their own slice and are asserted in their own modules. What remains
    deferred is configuration and shadow assignments.
    """
    deferred = {
        "constraint_configuration",
        "preference_configuration",
        "shadow_assignment",
    }

    assert deferred.isdisjoint(Base.metadata.tables)


def test_mappers_configure_without_error():
    """Catches relationship misconfiguration, which import alone does not."""
    configure_mappers()


# --------------------------------------------------------------------------
# Primary keys and timestamps
# --------------------------------------------------------------------------


def test_every_table_has_a_bigint_identity_primary_key():
    for name in EXPECTED_TABLES:
        table = _table(name)
        pk = list(table.primary_key.columns)

        assert [c.name for c in pk] == ["id"], name
        assert isinstance(pk[0].type, BigInteger), name
        assert pk[0].identity is not None, name
        assert pk[0].identity.always is True, name


def test_every_table_has_timezone_aware_created_and_updated_timestamps():
    for name in EXPECTED_TABLES:
        table = _table(name)

        for column_name in ("created_at", "updated_at"):
            column = table.c[column_name]
            assert column.type.timezone is True, f"{name}.{column_name}"
            assert not column.nullable, f"{name}.{column_name}"
            assert column.server_default is not None, f"{name}.{column_name}"

        assert table.c.updated_at.onupdate is not None, name


def test_deactivated_at_exists_only_where_deactivation_is_a_domain_state():
    expected = {"person", "ministry", "ministry_role", "ministry_membership"}
    actual = {
        name
        for name in EXPECTED_TABLES
        if "deactivated_at" in _table(name).c
    }

    assert actual == expected


def test_every_foreign_key_restricts_deletes():
    """History must not be orphanable (design section 6)."""
    for name in EXPECTED_TABLES:
        for fk in _table(name).foreign_keys:
            assert fk.ondelete == "RESTRICT", f"{name}.{fk.parent.name}"


# --------------------------------------------------------------------------
# Person / UserAccount
# --------------------------------------------------------------------------


def test_person_requires_display_name_and_has_no_structured_name_columns():
    person = _table("person")

    assert not person.c.display_name.nullable
    assert "first_name" not in person.c
    assert "last_name" not in person.c


def test_person_display_name_is_not_unique():
    """Two people may share a name; the schema must not forbid it."""
    assert ("display_name",) not in _unique_column_sets(_table("person"))


def test_person_stores_admin_authority_as_a_boolean_and_no_tier_column():
    person = _table("person")

    assert not person.c.is_admin.nullable
    assert isinstance(person.c.is_admin.type, Boolean)
    for forbidden in ("authorization_tier", "role", "tier"):
        assert forbidden not in person.c


def test_person_email_is_optional_and_case_insensitively_unique_when_present():
    person = _table("person")

    assert person.c.email.nullable

    ddl = _index_ddl(person, "uq_person_email_lower")
    assert "UNIQUE" in ddl
    assert "lower(email)" in ddl
    assert "WHERE email IS NOT NULL" in ddl


def test_user_account_is_one_to_one_with_person():
    assert ("person_id",) in _unique_column_sets(_table("user_account"))


def test_user_account_google_subject_is_required_and_unique():
    user_account = _table("user_account")

    assert not user_account.c.google_subject.nullable
    assert ("google_subject",) in _unique_column_sets(user_account)


def test_user_account_email_is_unique_but_is_not_the_identity_key():
    """Email is metadata: unique for hygiene, never the join key for identity."""
    ddl = _index_ddl(_table("user_account"), "uq_user_account_email_lower")

    assert "UNIQUE" in ddl
    assert "lower(email)" in ddl


def test_user_account_has_no_oauth_token_columns():
    columns = set(_table("user_account").c.keys())

    assert columns.isdisjoint(
        {"access_token", "refresh_token", "id_token", "scopes", "provider"}
    )


# --------------------------------------------------------------------------
# Ministry and MinistryRole
# --------------------------------------------------------------------------


def test_ministry_name_is_case_insensitively_unique_within_the_church():
    ddl = _index_ddl(_table("ministry"), "uq_ministry_church_id_name_lower")

    assert "UNIQUE" in ddl
    assert "church_id" in ddl
    assert "lower(name)" in ddl


def test_ministry_role_name_is_case_insensitively_unique_within_the_ministry():
    ddl = _index_ddl(
        _table("ministry_role"), "uq_ministry_role_ministry_id_name_lower"
    )

    assert "UNIQUE" in ddl
    assert "ministry_id" in ddl
    assert "lower(name)" in ddl


def test_ministry_role_display_order_is_non_negative_with_a_zero_default():
    ministry_role = _table("ministry_role")
    display_order = ministry_role.c.display_order

    assert not display_order.nullable
    # Exact type, not isinstance: BigInteger subclasses Integer and would pass.
    assert type(display_order.type) is Integer
    assert "0" in str(display_order.server_default.arg)

    check = _checks(ministry_role)["ck_ministry_role_display_order_non_negative"]
    assert "display_order >= 0" in check


# --------------------------------------------------------------------------
# MinistryMembership
# --------------------------------------------------------------------------


def test_a_person_cannot_hold_two_memberships_in_one_ministry():
    assert ("person_id", "ministry_id") in _unique_column_sets(
        _table("ministry_membership")
    )


def test_head_authority_requires_an_active_membership():
    check = _checks(_table("ministry_membership"))[
        "ck_ministry_membership_head_requires_active_membership"
    ]

    assert "is_ministry_head" in check
    assert "deactivated_at IS NULL" in check


def test_multiple_heads_per_ministry_are_allowed():
    """A unique constraint on the head flag would be a design violation."""
    membership = _table("ministry_membership")

    for columns in _unique_column_sets(membership):
        assert "is_ministry_head" not in columns

    for index in membership.indexes:
        if index.unique:
            assert "is_ministry_head" not in _index_ddl(membership, index.name)


def test_head_lookup_index_is_partial_on_the_head_flag():
    ddl = _index_ddl(_table("ministry_membership"), "ix_ministry_membership_head")

    assert "WHERE is_ministry_head" in ddl
    assert "UNIQUE" not in ddl


# --------------------------------------------------------------------------
# RoleQualification — cross-ministry integrity
# --------------------------------------------------------------------------


def test_role_qualification_has_one_standing_decision_per_membership_and_role():
    assert ("ministry_membership_id", "ministry_role_id") in _unique_column_sets(
        _table("role_qualification")
    )


def test_role_qualification_composite_fks_route_through_one_ministry():
    """The database-level protection against cross-ministry qualifications.

    Both foreign keys must include this row's ``ministry_id``, so a membership
    from ministry A cannot be paired with a role from ministry B.
    """
    composite = {
        constraint.name: (
            tuple(c.name for c in constraint.columns),
            tuple(e.column.table.name for e in constraint.elements),
            tuple(e.column.name for e in constraint.elements),
        )
        for constraint in _table("role_qualification").constraints
        if isinstance(constraint, ForeignKeyConstraint) and len(constraint.columns) > 1
    }

    assert composite == {
        "fk_role_qualification_membership_ministry": (
            ("ministry_membership_id", "ministry_id"),
            ("ministry_membership", "ministry_membership"),
            ("id", "ministry_id"),
        ),
        "fk_role_qualification_role_ministry": (
            ("ministry_role_id", "ministry_id"),
            ("ministry_role", "ministry_role"),
            ("id", "ministry_id"),
        ),
    }


def test_composite_fk_parent_keys_exist_on_both_parents():
    """Without these unique keys the composite foreign keys cannot be created."""
    assert ("id", "ministry_id") in _unique_column_sets(_table("ministry_membership"))
    assert ("id", "ministry_id") in _unique_column_sets(_table("ministry_role"))


def test_role_qualification_records_a_decision_not_a_training_state():
    role_qualification = _table("role_qualification")

    assert not role_qualification.c.is_qualified.nullable
    assert isinstance(role_qualification.c.is_qualified.type, Boolean)
    assert not role_qualification.c.decided_at.nullable
    assert not role_qualification.c.decided_by_person_id.nullable

    for forbidden in ("is_training", "shadow", "training_state", "status"):
        assert forbidden not in role_qualification.c


# --------------------------------------------------------------------------
# Naming convention
# --------------------------------------------------------------------------


def test_constraint_and_index_names_follow_the_naming_convention():
    prefixes = {"pk": "pk_", "fk": "fk_", "uq": "uq_", "ck": "ck_"}

    for name in EXPECTED_TABLES:
        table = _table(name)

        assert table.primary_key.name.startswith(prefixes["pk"]), name
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint):
                assert constraint.name.startswith(prefixes["uq"]), constraint.name
            elif isinstance(constraint, CheckConstraint):
                assert constraint.name.startswith(prefixes["ck"]), constraint.name
            elif isinstance(constraint, ForeignKeyConstraint):
                assert constraint.name.startswith(prefixes["fk"]), constraint.name
        for index in table.indexes:
            assert index.name.startswith(("ix_", "uq_")), index.name


def test_no_identifier_exceeds_the_postgresql_limit():
    """PostgreSQL silently truncates names over 63 characters."""
    for name in EXPECTED_TABLES:
        table = _table(name)
        identifiers = (
            [table.name]
            + [c.name for c in table.constraints if c.name]
            + [i.name for i in table.indexes]
        )

        for identifier in identifiers:
            assert len(str(identifier)) <= 63, identifier
