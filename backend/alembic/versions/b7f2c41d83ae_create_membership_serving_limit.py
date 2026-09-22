"""create membership serving limit

Adds one additive table, ``membership_serving_limit``: the per-person,
per-ministry, per-scheduling-period **hard** maximum number of assignments
(requirements §4.4.1).

Additive and non-destructive. No existing table is altered, no column is
dropped or renamed, and no data is written or migrated: before this runs
nobody has a limit, and afterwards nobody has one either until a Ministry
Head records it. Absence of a row *is* "no maximum", so an empty table is the
correct starting state and no backfill exists to get wrong.

The two composite foreign keys are the point of the shape: both route through
this row's single ``ministry_id``, so a Setup membership physically cannot
carry a limit for an AV scheduling period. That is the same pattern
``availability`` and ``staffing_requirement`` already use (core §7.2), and it
makes cross-ministry scope a database guarantee rather than only a service
check.

Revision ID: b7f2c41d83ae
Revises: a949a16195e3
Create Date: 2026-09-07 21:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7f2c41d83ae'
down_revision: Union[str, Sequence[str], None] = 'a949a16195e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: The audit target-table vocabulary as it stood before and after this
#: revision. Spelled out rather than imported from ``app.models.audit``: a
#: migration must keep describing the schema of *its own moment*, and importing
#: a live constant would make this file silently follow future edits to that
#: tuple.
_TARGET_TABLES_BEFORE = (
    'person', 'ministry', 'ministry_membership', 'ministry_role',
    'role_qualification', 'scheduling_period', 'event',
    'staffing_requirement', 'availability', 'existing_commitment',
    'schedule', 'schedule_version', 'assignment',
)
_TARGET_TABLES_AFTER = _TARGET_TABLES_BEFORE + ('membership_serving_limit',)


def _target_table_check(names: tuple[str, ...]) -> str:
    return "target_table IN (" + ", ".join(f"'{n}'" for n in names) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'membership_serving_limit',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('ministry_membership_id', sa.BigInteger(), nullable=False),
        sa.Column('scheduling_period_id', sa.BigInteger(), nullable=False),
        sa.Column('ministry_id', sa.BigInteger(), nullable=False),
        sa.Column('max_assignments', sa.Integer(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.CheckConstraint(
            'max_assignments > 0',
            name=op.f('ck_membership_serving_limit_max_assignments_positive'),
        ),
        sa.ForeignKeyConstraint(
            ['ministry_membership_id', 'ministry_id'],
            ['ministry_membership.id', 'ministry_membership.ministry_id'],
            name=op.f('fk_membership_serving_limit_membership_ministry'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['scheduling_period_id', 'ministry_id'],
            ['scheduling_period.id', 'scheduling_period.ministry_id'],
            name=op.f('fk_membership_serving_limit_period_ministry'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_membership_serving_limit')),
        sa.UniqueConstraint(
            'ministry_membership_id',
            'scheduling_period_id',
            name='uq_membership_serving_limit_membership_period',
        ),
    )
    op.create_index(
        'ix_membership_serving_limit_scheduling_period_id',
        'membership_serving_limit',
        ['scheduling_period_id'],
        unique=False,
    )

    # ``audit_event.target_table`` is a closed set enforced by a CHECK, so the
    # new table has to be admitted before any history can name it. Widening a
    # CHECK is additive: every row that satisfied the old constraint satisfies
    # the new one, so nothing existing can fail revalidation.
    op.drop_constraint(
        op.f('ck_audit_event_target_table_valid'), 'audit_event', type_='check'
    )
    op.create_check_constraint(
        op.f('ck_audit_event_target_table_valid'),
        'audit_event',
        _target_table_check(_TARGET_TABLES_AFTER),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Narrowing the CHECK again would fail if any serving-limit history has
    # been written, which is correct: those rows are real history, and a
    # downgrade that silently dropped them would be worse than one that stops.
    op.drop_constraint(
        op.f('ck_audit_event_target_table_valid'), 'audit_event', type_='check'
    )
    op.create_check_constraint(
        op.f('ck_audit_event_target_table_valid'),
        'audit_event',
        _target_table_check(_TARGET_TABLES_BEFORE),
    )
    op.drop_index(
        'ix_membership_serving_limit_scheduling_period_id',
        table_name='membership_serving_limit',
    )
    op.drop_table('membership_serving_limit')
