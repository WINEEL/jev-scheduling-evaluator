"""add availability backup state

Widens ``availability.availability_state`` from two stored values to three,
adding ``BACKUP`` (Task 52): a feasible but lower-priority answer -- "use me
only if an ordinarily AVAILABLE candidate cannot fill this position" -- sitting
alongside the existing ``AVAILABLE`` and ``UNAVAILABLE``. ``NO_RESPONSE``
remains unchanged: it is still never a stored value, and absence of a row
still means no response (scheduling-input §8).

Purely additive on upgrade. No column is added, no existing row is touched,
and no backfill runs: every row that satisfied the old two-value CHECK
already satisfies the widened one unchanged, exactly as the same-date
exclusion revision (c31d8a4f7b62) widened ``audit_event.target_table``.
``AVAILABLE`` and ``UNAVAILABLE`` rows require no rewrite to remain valid.

The downgrade is **not** symmetric, deliberately. Narrowing the CHECK back to
two values would corrupt any row a Ministry Head has since recorded as
``BACKUP`` -- silently deleting or coercing it would destroy a real answer a
person gave. Following this repository's own migration-safety convention
(same-date exclusion's downgrade refuses rather than drops real history), this
downgrade queries for any ``BACKUP`` row first and raises rather than
narrowing the constraint if one exists.

Revision ID: 615c88b0a6f3
Revises: c31d8a4f7b62
Create Date: 2026-09-09 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '615c88b0a6f3'
down_revision: Union[str, Sequence[str], None] = 'c31d8a4f7b62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_STATES_BEFORE = ("AVAILABLE", "UNAVAILABLE")
_STATES_AFTER = ("AVAILABLE", "BACKUP", "UNAVAILABLE")


def _availability_state_check(states: tuple[str, ...]) -> str:
    values = ", ".join(f"'{s}'" for s in states)
    return f"availability_state IN ({values})"


def upgrade() -> None:
    """Upgrade schema."""
    # Widening a CHECK is additive: every row that satisfied the old
    # constraint satisfies the new one, so nothing existing can fail
    # revalidation, and no UPDATE is needed for AVAILABLE/UNAVAILABLE rows.
    op.drop_constraint(
        op.f('ck_availability_availability_state_valid'),
        'availability',
        type_='check',
    )
    op.create_check_constraint(
        op.f('ck_availability_availability_state_valid'),
        'availability',
        _availability_state_check(_STATES_AFTER),
    )


def downgrade() -> None:
    """Downgrade schema.

    Refuses if any row has recorded ``BACKUP`` -- narrowing the CHECK
    underneath such a row, or silently rewriting/deleting it first, would
    destroy a real answer a person gave. This is the same "fail loudly rather
    than delete or coerce" convention already used for the same-date
    exclusion revision's downgrade.
    """
    connection = op.get_bind()
    backup_count = connection.execute(
        sa.text("SELECT count(*) FROM availability WHERE availability_state = 'BACKUP'")
    ).scalar_one()
    if backup_count:
        raise RuntimeError(
            f"cannot downgrade: {backup_count} availability row(s) use the"
            " BACKUP state this revision introduced; resolve or remove them"
            " before narrowing the CHECK constraint back to two values"
        )

    op.drop_constraint(
        op.f('ck_availability_availability_state_valid'),
        'availability',
        type_='check',
    )
    op.create_check_constraint(
        op.f('ck_availability_availability_state_valid'),
        'availability',
        _availability_state_check(_STATES_BEFORE),
    )
