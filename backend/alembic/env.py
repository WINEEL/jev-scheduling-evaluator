import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy import pool

from alembic import context

# Make the backend application importable regardless of the working directory
# Alembic is invoked from (alembic/ -> parents[1] is backend/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402

# Importing the models package registers every mapped class against Base, so
# ``Base.metadata`` below is populated. This import is explicit and is the only
# thing autogenerate relies on: no API or service module is imported for its
# side effects.
import app.models  # noqa: E402,F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for 'autogenerate' support, populated by the app.models
# import above.
target_metadata = Base.metadata


def _database_url() -> str:
    """Database URL from application configuration (repository-root .env / env).

    Read at call time and never written back into the Alembic config, so no
    credential is committed and no ConfigParser interpolation is applied to it.
    """
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no DBAPI connection)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a real connection."""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
