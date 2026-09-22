"""Database-foundation tests.

Offline only: the synthetic DATABASE_URL from conftest.py lets ``app.db`` build
its Engine without a real ``.env`` and without connecting to PostgreSQL.
"""


def test_engine_uses_psycopg3_sync_driver():
    from app.db import engine

    assert engine.url.drivername == "postgresql+psycopg"


def test_base_metadata_is_populated_by_importing_the_models_package():
    """Metadata comes from an explicit ``app.models`` import, nothing implicit.

    Replaces an earlier assertion that the metadata was empty, which recorded a
    precondition of the database-foundation task: models now exist. The table
    set itself is asserted in test_models_core.py.
    """
    import app.models  # noqa: F401

    from app.db import Base

    assert Base.metadata.tables


def test_base_metadata_uses_a_naming_convention():
    from app.db import Base

    assert set(Base.metadata.naming_convention) >= {"pk", "fk", "uq", "ck", "ix"}


def test_session_factory_produces_a_session():
    from sqlalchemy.orm import Session

    from app.db import SessionLocal

    session = SessionLocal()
    try:
        assert isinstance(session, Session)
    finally:
        session.close()
