"""Database foundation: a single Engine, a session factory and a declarative Base.

No schema is created here. All schema evolution goes through Alembic; nothing
in this module creates database objects or connects at import time.

The ORM models live in :mod:`app.models`. This module deliberately does not
import them: models import ``Base`` from here, so importing them here too would
be circular. Anything that needs a populated ``Base.metadata`` imports
``app.models`` explicitly.
"""

from __future__ import annotations

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

# Deterministic constraint and index names. Without this, PostgreSQL invents
# names for unnamed constraints and a migration cannot reliably drop or alter
# one by name. Set on the MetaData before any model is defined so every
# constraint in the schema is covered.
#
# ``column_0_N_name`` joins all constrained column names with ``_``, which can
# exceed PostgreSQL's 63-character identifier limit for wide composite keys;
# those few constraints are given explicit names at their definition site.
NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for the application's ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _build_engine() -> Engine:
    settings = get_settings()
    # ``pool_pre_ping`` keeps pooled connections healthy against Neon, which
    # closes idle server-side connections. Creating the Engine does not open a
    # connection.
    return create_engine(settings.database_url, pool_pre_ping=True)


engine: Engine = _build_engine()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
