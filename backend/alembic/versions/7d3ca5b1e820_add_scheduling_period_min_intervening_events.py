"""add scheduling_period.min_intervening_events

Adds the first *ministry- and period-scoped* scheduling rule (Task 71): how
many of a ministry's own events must fall between two assignments of the same
person during one scheduling period.

``NULL`` is the ordinary case and means there is no such rule -- the behaviour
every existing period has today and keeps unchanged. ``1`` means one event must
be skipped (no consecutive assignments in that ministry's event sequence),
``2`` means two, and so on. Zero is deliberately unrepresentable: it would mean
"consecutive assignments are allowed", which is exactly what ``NULL`` already
says, and one fact with two spellings is a bug waiting to be written. The
``min_intervening_events > 0`` CHECK is what enforces that, mirroring
``membership_serving_limit.max_assignments > 0`` (revision b7f2c41d83ae).

Purely additive on upgrade. A nullable column with no server default, so every
existing row reads ``NULL`` -- no backfill, no rewrite, and no change to any
schedule already produced.

The downgrade is **not** symmetric, deliberately, and follows this
repository's own migration-safety convention (615c88b0a6f3 and c31d8a4f7b62
both refuse rather than destroy): dropping the column would silently discard a
rule a Ministry Head configured and that the solver, manual assignment and
finalization are all enforcing. So it counts the configured rows first and
raises if any exist.

Revision ID: 7d3ca5b1e820
Revises: 615c88b0a6f3
Create Date: 2026-09-14 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7d3ca5b1e820'
down_revision: Union[str, Sequence[str], None] = '615c88b0a6f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "scheduling_period"
_COLUMN = "min_intervening_events"
#: The final constraint name, wrapped in ``op.f`` at every use so Alembic does
#: not apply the ``ck_%(table_name)s_%(constraint_name)s`` naming convention a
#: second time -- which would produce ``ck_scheduling_period_ck_scheduling_...``
#: and be silently truncated at PostgreSQL's 63-character identifier limit.
#: This is the name the model's ``min_intervening_events_positive`` constraint
#: resolves to through that same convention.
_CHECK = "ck_scheduling_period_min_intervening_events_positive"


def upgrade() -> None:
    """Upgrade schema."""
    # Nullable and with no server default: NULL is the rule's "unconfigured"
    # state, so every existing period keeps exactly the behaviour it has now.
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f(_CHECK),
        _TABLE,
        f"{_COLUMN} IS NULL OR {_COLUMN} > 0",
    )


def downgrade() -> None:
    """Downgrade schema.

    Refuses if any period has the rule configured. Dropping the column would
    delete a scheduling rule a head recorded, with no trace in the schema that
    it ever existed -- the audit row would survive, but the rule the solver was
    obeying would not. Fail loudly rather than destroy, as elsewhere in this
    directory.
    """
    connection = op.get_bind()
    configured = connection.execute(
        sa.text(
            f"SELECT count(*) FROM {_TABLE} WHERE {_COLUMN} IS NOT NULL"
        )
    ).scalar_one()
    if configured:
        raise RuntimeError(
            f"cannot downgrade: {configured} scheduling period(s) configure the"
            f" {_COLUMN} rule this revision introduced; clear the rule on those"
            " periods before dropping the column"
        )

    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
