"""add person.church_membership_status

Adds the church-wide formal membership status (Task 79 §6): MEMBER,
NON_MEMBER or UNKNOWN.

**This is not ``ministry_membership``, and the two must never be confused.**
``ministry_membership`` says who serves on which team; this column says whether
the church regards the person as a formal member. A long-serving volunteer may
not be a member, and a member may serve nowhere. Neither is ever derived from
the other, and nothing in this revision backfills one from the other.

**Every existing row becomes UNKNOWN**, which is the honest answer: nobody has
stated a status for anybody yet, and inferring one from ministry participation,
serving history, a name, an address or an assignment would be the application
inventing a fact about a human being. That is exactly what the task forbids, so
the upgrade is a constant default and nothing else.

``NOT NULL`` with a server default rather than a nullable column: "we do not
know" already has a spelling here, and leaving NULL available would give the
same fact two representations -- the mistake ``7d3ca5b1e820`` avoided from the
other direction, where NULL was itself the meaningful state.

The downgrade refuses if anybody has been given a real status, following this
directory's own convention (``615c88b0a6f3``, ``c31d8a4f7b62``,
``7d3ca5b1e820``): dropping the column would discard a governance decision an
Admin recorded, leaving only an audit row describing a field that no longer
exists.

Revision ID: c5a70d19e8b3
Revises: f41c9ad2e7b5
Create Date: 2026-09-19 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5a70d19e8b3'
down_revision: Union[str, Sequence[str], None] = 'f41c9ad2e7b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "person"
_COLUMN = "church_membership_status"
_DEFAULT = "UNKNOWN"
#: The final constraint name, wrapped in ``op.f`` at every use so Alembic does
#: not apply the ``ck_%(table_name)s_%(constraint_name)s`` convention a second
#: time -- which would produce ``ck_person_ck_person_...`` and be silently
#: truncated at PostgreSQL's 63-character identifier limit. This is the name
#: the model's ``church_membership_status_valid`` constraint resolves to
#: through that same convention.
_CHECK = "ck_person_church_membership_status_valid"


def upgrade() -> None:
    """Upgrade schema.

    The server default is kept on the column, not dropped after the backfill:
    a Person created by any writer that does not mention the column -- a
    script, a fixture, a future endpoint -- should start as UNKNOWN rather than
    fail, because "nobody has said" is the correct state for somebody nobody
    has said anything about.
    """
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Text(),
            nullable=False,
            server_default=sa.text(f"'{_DEFAULT}'"),
        ),
    )
    op.create_check_constraint(
        op.f(_CHECK),
        _TABLE,
        f"{_COLUMN} IN ('MEMBER', 'NON_MEMBER', 'UNKNOWN')",
    )


def downgrade() -> None:
    """Downgrade schema.

    Refuses if anybody has a status other than the default. An Admin stating
    that somebody is or is not a formal member of the church is a governance
    decision, and dropping the column would destroy every one of those
    decisions at once.
    """
    connection = op.get_bind()
    stated = connection.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {_COLUMN} <> :default"),
        {"default": _DEFAULT},
    ).scalar_one()
    if stated:
        raise RuntimeError(
            f"cannot downgrade: {stated} person row(s) carry a stated"
            f" {_COLUMN} this revision introduced; reset them to"
            f" {_DEFAULT} before dropping the column"
        )

    op.drop_constraint(op.f(_CHECK), _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
