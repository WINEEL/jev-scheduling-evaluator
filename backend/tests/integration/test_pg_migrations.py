"""Integration Test A -- the migrations really apply to PostgreSQL.

The offline suite can prove a migration file *parses*. Only a real database
can prove the four migrations actually run, in order, and leave the schema the
models expect. ``alembic upgrade head`` is run once by the session fixture;
these tests assert what it produced.

Upgrade only: there is no downgrade here, and nothing drops or truncates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

import app.models  # noqa: F401  -- registers every mapped class on Base.metadata
from app.db import Base

pytestmark = pytest.mark.integration


def _repository_head_revision() -> str:
    """The head Alembic knows about, read from the scripts themselves.

    Derived rather than hard-coded, so adding a fifth migration does not
    silently leave this test asserting a stale revision id.
    """
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single head, got {heads}"
    return heads[0]


def test_a1_alembic_version_matches_the_repository_head(db_session):
    applied = db_session.execute(text("SELECT version_num FROM alembic_version")).scalars().all()

    assert applied == [_repository_head_revision()]


def test_a2_every_model_table_exists_in_the_database(db_session):
    # Robust against future schema growth: the expectation is "every table the
    # models declare", not a frozen count or list.
    expected = set(Base.metadata.tables)
    actual = set(inspect(db_session.get_bind()).get_table_names(schema="public"))

    assert expected, "Base.metadata is empty; app.models did not import"
    assert expected <= actual, f"missing tables: {sorted(expected - actual)}"


def test_a3_the_four_expected_schema_slices_are_present(db_session):
    # A readable spot-check of the four migrations' headline tables, so a
    # failure names the slice that did not apply rather than a diff of 13 sets.
    names = set(inspect(db_session.get_bind()).get_table_names(schema="public"))

    assert {"church", "person", "ministry", "ministry_membership"} <= names  # core
    assert {"scheduling_period", "event", "staffing_requirement"} <= names  # input
    assert {"schedule", "schedule_version", "assignment"} <= names  # output
    assert "audit_event" in names  # audit


def test_a4_the_snapshot_table_and_its_immutable_date_column_exist(db_session):
    columns = {
        c["name"]: c
        for c in inspect(db_session.get_bind()).get_columns(
            "schedule_version_requirement", schema="public"
        )
    }

    assert {"event_id", "event_date", "ministry_role_id", "required_count"} <= set(columns)
    # The snapshot deliberately has no updated_at: it is written once (§8).
    assert "updated_at" not in columns
