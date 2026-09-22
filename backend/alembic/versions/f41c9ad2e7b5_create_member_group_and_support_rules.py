"""create member group and same-event support rules

Adds the two generic hard scheduling rules of Task 74, as five additive tables:

- ``member_group`` / ``member_group_member`` -- a ministry's own named category
  of members, and who is in it;
- ``member_group_event_limit`` -- *at most N members of group G may serve one
  event*, for one scheduling period;
- ``membership_support_requirement`` / ``membership_support_supporter`` -- *this
  membership may serve an event only if at least N of these other memberships
  serve the same event*, for one scheduling period.

Additive and non-destructive. No existing table is altered, no column is dropped
or renamed, and no data is written or migrated: before this runs no group, cap
or support requirement exists, and afterwards none does either until a Ministry
Head configures one. Absence of a row *is* "no rule", so empty tables are the
correct starting state and there is no backfill to get wrong. Nothing infers a
group membership or a supporter from a name, an address, a roster column or a
historical schedule -- there is no inference here to run.

Four parts of the shape carry the guarantees:

- **Composite foreign keys through one ``ministry_id``** on every configuration
  row, so a group, its members, its cap, a subject and its supporters must all
  agree on the ministry. Cross-ministry configuration is refused by PostgreSQL
  and not only by the service layer -- the same pattern ``availability``,
  ``staffing_requirement``, ``membership_serving_limit`` and
  ``membership_same_date_exclusion`` already use (core §7.2).
- **``max_per_event > 0`` and ``min_supporters > 0``**, so "no cap" and "no
  requirement" keep exactly one representation -- the absence of a row -- rather
  than two free to disagree, exactly as ``membership_serving_limit`` and
  ``scheduling_period.min_intervening_events`` do.
- **``supporter_membership_id <> subject_membership_id``**, which makes "the
  subject cannot satisfy their own requirement" a database guarantee. The
  supporter row carries the subject and is routed back to its parent through
  ``(id, subject_membership_id, ministry_id)``, so the copied subject cannot
  disagree with the requirement it belongs to.
- **Uniqueness per period**: one cap per (group, period) and one requirement per
  (subject, period), so a second row cannot be a second answer to a question
  that has one.

No relationship type, reason, household, transportation, category or
soft-preference column is created anywhere here. The scheduler needs the count
and the set, never the circumstance behind them (requirements §4.7), and a
nullable column nothing reads would be a claim that a feature exists.

Revision ID: f41c9ad2e7b5
Revises: 7d3ca5b1e820
Create Date: 2026-09-16 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f41c9ad2e7b5'
down_revision: Union[str, Sequence[str], None] = '7d3ca5b1e820'
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
    'membership_same_date_exclusion',
)
_TARGET_TABLES_AFTER = _TARGET_TABLES_BEFORE + (
    'member_group',
    'member_group_member',
    'member_group_event_limit',
    'membership_support_requirement',
)

#: ``membership_support_supporter`` is deliberately **not** an audit target: a
#: supporter row is only ever written as part of configuring the requirement it
#: belongs to, and that requirement is what the history names.

#: Every constraint name this migration creates or drops by name, defined once.
#: Each is passed through ``op.f()`` at its use site so Alembic treats it as the
#: final name; a bare string would have the MetaData naming convention applied a
#: second time and be silently truncated at PostgreSQL's 63-character limit.
_AUDIT_TARGET_CHECK = 'ck_audit_event_target_table_valid'


def _target_table_check(names: tuple[str, ...]) -> str:
    return "target_table IN (" + ", ".join(f"'{n}'" for n in names) + ")"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'member_group',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('ministry_id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.Text(), nullable=False),
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
            'length(btrim(name)) > 0',
            name=op.f('ck_member_group_name_not_blank'),
        ),
        sa.ForeignKeyConstraint(
            ['ministry_id'],
            ['ministry.id'],
            name=op.f('fk_member_group_ministry_id_ministry'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_member_group')),
        sa.UniqueConstraint(
            'id', 'ministry_id', name='uq_member_group_id_ministry_id'
        ),
    )
    op.create_index(
        'ix_member_group_ministry_id', 'member_group', ['ministry_id'], unique=False
    )
    op.create_index(
        'uq_member_group_ministry_id_name_lower',
        'member_group',
        ['ministry_id', sa.text('lower(name)')],
        unique=True,
    )

    op.create_table(
        'member_group_member',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('member_group_id', sa.BigInteger(), nullable=False),
        sa.Column('ministry_membership_id', sa.BigInteger(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ['member_group_id', 'ministry_id'],
            ['member_group.id', 'member_group.ministry_id'],
            name=op.f('fk_member_group_member_group_ministry'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['ministry_membership_id', 'ministry_id'],
            ['ministry_membership.id', 'ministry_membership.ministry_id'],
            name=op.f('fk_member_group_member_membership_ministry'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_member_group_member')),
        sa.UniqueConstraint(
            'member_group_id',
            'ministry_membership_id',
            name='uq_member_group_member_group_membership',
        ),
    )
    op.create_index(
        'ix_member_group_member_ministry_membership_id',
        'member_group_member',
        ['ministry_membership_id'],
        unique=False,
    )

    op.create_table(
        'member_group_event_limit',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('member_group_id', sa.BigInteger(), nullable=False),
        sa.Column('scheduling_period_id', sa.BigInteger(), nullable=False),
        sa.Column('ministry_id', sa.BigInteger(), nullable=False),
        sa.Column('max_per_event', sa.Integer(), nullable=False),
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
            'max_per_event > 0',
            name=op.f('ck_member_group_event_limit_max_per_event_positive'),
        ),
        sa.ForeignKeyConstraint(
            ['member_group_id', 'ministry_id'],
            ['member_group.id', 'member_group.ministry_id'],
            name=op.f('fk_member_group_event_limit_group_ministry'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['scheduling_period_id', 'ministry_id'],
            ['scheduling_period.id', 'scheduling_period.ministry_id'],
            name=op.f('fk_member_group_event_limit_period_ministry'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_member_group_event_limit')),
        sa.UniqueConstraint(
            'member_group_id',
            'scheduling_period_id',
            name='uq_member_group_event_limit_group_period',
        ),
    )
    op.create_index(
        'ix_member_group_event_limit_scheduling_period_id',
        'member_group_event_limit',
        ['scheduling_period_id'],
        unique=False,
    )

    op.create_table(
        'membership_support_requirement',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('subject_membership_id', sa.BigInteger(), nullable=False),
        sa.Column('scheduling_period_id', sa.BigInteger(), nullable=False),
        sa.Column('ministry_id', sa.BigInteger(), nullable=False),
        sa.Column('min_supporters', sa.Integer(), nullable=False),
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
            'min_supporters > 0',
            name=op.f('ck_membership_support_requirement_min_supporters_positive'),
        ),
        sa.ForeignKeyConstraint(
            ['subject_membership_id', 'ministry_id'],
            ['ministry_membership.id', 'ministry_membership.ministry_id'],
            name=op.f('fk_support_requirement_subject_ministry'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['scheduling_period_id', 'ministry_id'],
            ['scheduling_period.id', 'scheduling_period.ministry_id'],
            name=op.f('fk_support_requirement_period_ministry'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint(
            'id', name=op.f('pk_membership_support_requirement')
        ),
        sa.UniqueConstraint(
            'subject_membership_id',
            'scheduling_period_id',
            name='uq_support_requirement_subject_period',
        ),
        sa.UniqueConstraint(
            'id',
            'subject_membership_id',
            'ministry_id',
            name='uq_support_requirement_id_subject_ministry',
        ),
    )
    op.create_index(
        'ix_support_requirement_scheduling_period_id',
        'membership_support_requirement',
        ['scheduling_period_id'],
        unique=False,
    )

    op.create_table(
        'membership_support_supporter',
        sa.Column('id', sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column('support_requirement_id', sa.BigInteger(), nullable=False),
        sa.Column('subject_membership_id', sa.BigInteger(), nullable=False),
        sa.Column('supporter_membership_id', sa.BigInteger(), nullable=False),
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
            'supporter_membership_id <> subject_membership_id',
            name=op.f('ck_membership_support_supporter_supporter_is_not_subject'),
        ),
        sa.ForeignKeyConstraint(
            ['support_requirement_id', 'subject_membership_id', 'ministry_id'],
            [
                'membership_support_requirement.id',
                'membership_support_requirement.subject_membership_id',
                'membership_support_requirement.ministry_id',
            ],
            name=op.f('fk_support_supporter_requirement'),
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['supporter_membership_id', 'ministry_id'],
            ['ministry_membership.id', 'ministry_membership.ministry_id'],
            name=op.f('fk_support_supporter_membership_ministry'),
            ondelete='RESTRICT',
        ),
        sa.PrimaryKeyConstraint(
            'id', name=op.f('pk_membership_support_supporter')
        ),
        sa.UniqueConstraint(
            'support_requirement_id',
            'supporter_membership_id',
            name='uq_support_supporter_requirement_member',
        ),
    )
    op.create_index(
        'ix_support_supporter_supporter_membership_id',
        'membership_support_supporter',
        ['supporter_membership_id'],
        unique=False,
    )

    # ``audit_event.target_table`` is a closed set enforced by a CHECK, so the
    # new tables have to be admitted before any history can name them. Widening
    # a CHECK is additive: every row that satisfied the old constraint satisfies
    # the new one, so nothing existing can fail revalidation.
    op.drop_constraint(op.f(_AUDIT_TARGET_CHECK), 'audit_event', type_='check')
    op.create_check_constraint(
        op.f(_AUDIT_TARGET_CHECK),
        'audit_event',
        _target_table_check(_TARGET_TABLES_AFTER),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Narrowing the CHECK again would fail if any member-group or support-rule
    # history has been written, which is correct: those rows are real history,
    # and a downgrade that silently dropped them would be worse than one that
    # stops.
    op.drop_constraint(op.f(_AUDIT_TARGET_CHECK), 'audit_event', type_='check')
    op.create_check_constraint(
        op.f(_AUDIT_TARGET_CHECK),
        'audit_event',
        _target_table_check(_TARGET_TABLES_BEFORE),
    )

    op.drop_index(
        'ix_support_supporter_supporter_membership_id',
        table_name='membership_support_supporter',
    )
    op.drop_table('membership_support_supporter')
    op.drop_index(
        'ix_support_requirement_scheduling_period_id',
        table_name='membership_support_requirement',
    )
    op.drop_table('membership_support_requirement')
    op.drop_index(
        'ix_member_group_event_limit_scheduling_period_id',
        table_name='member_group_event_limit',
    )
    op.drop_table('member_group_event_limit')
    op.drop_index(
        'ix_member_group_member_ministry_membership_id',
        table_name='member_group_member',
    )
    op.drop_table('member_group_member')
    op.drop_index(
        'uq_member_group_ministry_id_name_lower', table_name='member_group'
    )
    op.drop_index('ix_member_group_ministry_id', table_name='member_group')
    op.drop_table('member_group')
