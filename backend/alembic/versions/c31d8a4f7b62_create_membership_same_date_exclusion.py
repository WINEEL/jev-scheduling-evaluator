"""create membership same date exclusion

Adds one additive table, ``membership_same_date_exclusion``: the per-pair,
per-ministry, per-scheduling-period **hard** rule that two linked members must
not both serve on the same calendar date (requirements §4.4.2).

Additive and non-destructive. No existing table is altered, no column is
dropped or renamed, and no data is written or migrated: before this runs no
pair carries an exclusion, and afterwards none does either until a Ministry
Head configures one. Absence of a row *is* "no rule", so an empty table is the
correct starting state and no backfill exists to get wrong. Nothing infers a
link from names, addresses or historical schedules -- there is no inference
here to run.

Two parts of the shape carry the guarantees:

- **Three composite foreign keys through one ``ministry_id``**, so both
  memberships and the scheduling period must agree on the ministry. A
  cross-ministry pair, or a period belonging to another ministry, is refused
  by PostgreSQL and not only by the service layer -- the same pattern
  ``availability``, ``staffing_requirement`` and ``membership_serving_limit``
  already use (core §7.2).
- **``membership_a_id < membership_b_id``**, which makes the pair unordered by
  construction: A+B and B+A cannot both exist, and a membership cannot be
  paired with itself. The unique constraint over the pair and the period then
  gives exactly one effective exclusion per unordered pair per period.

No relationship type, household, spouse, soft-preference, transportation or
church-wide column is created. The scheduler needs the exclusion, not the
circumstance behind it (requirements §4.4.2, §4.7), and a nullable column
nothing reads would be a claim that a feature exists.

Revision ID: c31d8a4f7b62
Revises: b7f2c41d83ae
Create Date: 2026-09-08 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c31d8a4f7b62'
down_revision: Union[str, Sequence[str], None] = 'b7f2c41d83ae'
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
    'schedule', 'schedule_version', 'assignment', 'membership_serving_limit',
)
_TARGET_TABLES_AFTER = _TARGET_TABLES_BEFORE + ('membership_same_date_exclusion',)


def _target_table_check(names: tuple[str, ...]) -> str:
    return "target_table IN (" + ", ".join(f"'{n}'" for n in names) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'membership_same_date_exclusion',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('membership_a_id', sa.BigInteger(), nullable=False),
        sa.Column('membership_b_id', sa.BigInteger(), nullable=False),
        sa.Column('scheduling_period_id', sa.BigInteger(), nullable=False),
        sa.Column('ministry_id', sa.BigInteger(), nullable=False),
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
            'membership_a_id < membership_b_id',
            name=op.f('ck_membership_same_date_exclusion_pair_canonically_ordered'),
        ),
        sa.ForeignKeyConstraint(
            ['membership_a_id', 'ministry_id'],
            ['ministry_membership.id', 'ministry_membership.ministry_id'],
            name=op.f('fk_same_date_exclusion_membership_a_ministry'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['membership_b_id', 'ministry_id'],
            ['ministry_membership.id', 'ministry_membership.ministry_id'],
            name=op.f('fk_same_date_exclusion_membership_b_ministry'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['scheduling_period_id', 'ministry_id'],
            ['scheduling_period.id', 'scheduling_period.ministry_id'],
            name=op.f('fk_same_date_exclusion_period_ministry'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint(
            'id', name=op.f('pk_membership_same_date_exclusion')
        ),
        sa.UniqueConstraint(
            'membership_a_id',
            'membership_b_id',
            'scheduling_period_id',
            name='uq_same_date_exclusion_pair_period',
        ),
    )
    op.create_index(
        'ix_same_date_exclusion_scheduling_period_id',
        'membership_same_date_exclusion',
        ['scheduling_period_id'],
        unique=False,
    )
    op.create_index(
        'ix_same_date_exclusion_membership_b_id',
        'membership_same_date_exclusion',
        ['membership_b_id'],
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
    # Narrowing the CHECK again would fail if any pair-exclusion history has
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
        'ix_same_date_exclusion_membership_b_id',
        table_name='membership_same_date_exclusion',
    )
    op.drop_index(
        'ix_same_date_exclusion_scheduling_period_id',
        table_name='membership_same_date_exclusion',
    )
    op.drop_table('membership_same_date_exclusion')
