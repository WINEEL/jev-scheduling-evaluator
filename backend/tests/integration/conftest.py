"""Opt-in PostgreSQL integration-test harness (Task 25).

Everything in ``tests/integration/`` runs against a **real PostgreSQL
database** and is skipped unless explicitly configured. The offline suite in
``tests/`` is unaffected and still needs no database at all.

Safety, which is the whole point of this module
-----------------------------------------------
These tests write rows. They must therefore never be able to reach the
development or production database, so **three independent gates** must all
pass before a single connection is opened:

1. ``CHURCH_SCHEDULING_RUN_PG_INTEGRATION`` must be exactly ``"1"`` -- an
   explicit human opt-in that no ordinary ``pytest`` run sets by accident.
2. ``TEST_DATABASE_URL`` must be supplied. There is **no fallback to
   ``DATABASE_URL``**: if the dedicated URL is absent the suite skips, it does
   not "helpfully" use the application's own database.
3. The supplied target must be **demonstrably not** the development target.
   The development ``DATABASE_URL`` is read from the repository-root ``.env``
   (read-only, for comparison only) and refused if it resolves to the same
   host *and* database. A misconfiguration here is a hard error, not a skip --
   silently skipping would hide exactly the mistake that matters most.

Configuration is read from the process environment first and, failing that,
from a git-ignored repository-root ``.env.test``. That file is **test-only**:
nothing in ``app/`` reads it, and no value from it is ever printed, logged,
or written into source, README or fixtures.

Isolation, and why nothing is ever committed
--------------------------------------------
Each test runs inside a connection-scoped transaction that is **always rolled
back**, using SQLAlchemy's standard "join an external transaction" pattern.
The Session is bound to that Connection with
``join_transaction_mode="create_savepoint"``, so even a ``session.commit()``
inside a service or test only releases a SAVEPOINT -- the outer transaction
still owns the real one, and rolling it back discards everything the test
wrote. No test leaves committed rows behind, and **nothing here truncates,
drops or deletes pre-existing data**: isolation is transactional, never
destructive.

The one intentionally durable operation is ``alembic upgrade head`` against
the dedicated test database, run once per session. There is no downgrade, and
no schema is ever dropped.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

# tests/integration/conftest.py -> integration -> tests -> backend -> repo root
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_DIR.parent
_ENV_TEST_FILE = _REPO_ROOT / ".env.test"
_ENV_DEV_FILE = _REPO_ROOT / ".env"

_OPT_IN_VAR = "CHURCH_SCHEDULING_RUN_PG_INTEGRATION"
_URL_VAR = "TEST_DATABASE_URL"


@dataclass(frozen=True)
class IntegrationConfig:
    """The two values the harness needs. Never rendered anywhere."""

    url: str
    opt_in: str


def _read_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=value`` reader.

    Deliberately not :mod:`pydantic_settings`: this is test-only
    configuration, and routing it through the application's Settings would be
    exactly the coupling this module exists to avoid -- ``app.config`` must
    never learn about ``.env.test``.
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def _resolve_config() -> IntegrationConfig | None:
    """Process environment first, then the git-ignored ``.env.test``.

    Returns ``None`` when the suite is simply not configured, which is the
    ordinary case for a developer running the offline tests.
    """
    file_values = _read_env_file(_ENV_TEST_FILE)
    url = os.environ.get(_URL_VAR) or file_values.get(_URL_VAR)
    opt_in = os.environ.get(_OPT_IN_VAR) or file_values.get(_OPT_IN_VAR)
    if not url or not opt_in:
        return None
    return IntegrationConfig(url=url, opt_in=opt_in)


def _target_identity(url: str) -> tuple[str, str]:
    """``(host, database)`` for comparison. Never includes credentials."""
    parts = urlsplit(url)
    return (parts.hostname or "", parts.path.lstrip("/").split("?")[0])


def _assert_not_the_development_database(url: str) -> None:
    """Refuse to run against the application's own database.

    The development URL is read from the repository-root ``.env`` **file**
    rather than ``os.environ["DATABASE_URL"]``, because ``tests/conftest.py``
    deliberately overwrites that variable with a synthetic value -- comparing
    against it would compare against a placeholder and prove nothing.

    Raises rather than skips: an integration run pointed at the development
    database is a safety failure that must be loud.
    """
    dev_url = _read_env_file(_ENV_DEV_FILE).get("DATABASE_URL")
    if not dev_url:
        return  # Nothing to compare against; the opt-in gates remain.

    if url == dev_url:
        raise RuntimeError(
            f"{_URL_VAR} is identical to the development DATABASE_URL."
            " Integration tests must use a dedicated database."
        )
    test_host, test_db = _target_identity(url)
    dev_host, dev_db = _target_identity(dev_url)
    if test_host == dev_host and test_db == dev_db:
        raise RuntimeError(
            f"{_URL_VAR} resolves to the same host and database as the"
            " development DATABASE_URL. Integration tests must use a"
            " dedicated database."
        )


def _alembic_upgrade_head(url: str) -> None:
    """Run ``alembic upgrade head`` against the **test** database only.

    ``alembic/env.py`` reads the URL from the application configuration, and
    :mod:`pydantic_settings` gives the process environment precedence over the
    ``.env`` file -- so passing ``DATABASE_URL`` in the *child process
    environment only* points this one migration run at the test database
    without touching any file, any application default, or this process's own
    configuration. No production code is modified to make this work.

    Upgrade only. There is deliberately no downgrade, and no schema is
    dropped or truncated anywhere in this harness.
    """
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=_BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": url},
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        # stderr can legitimately contain Alembic's own logging; it never
        # contains the URL, which env.py reads but does not print.
        raise RuntimeError(
            "alembic upgrade head failed against the test database"
            f" (exit {result.returncode}):\n{result.stderr[-4000:]}"
        )


@pytest.fixture(scope="session")
def integration_config() -> IntegrationConfig:
    """The gate every integration test passes through.

    Skips (does not fail) when unconfigured, so an ordinary offline run stays
    green with no PostgreSQL anywhere.
    """
    config = _resolve_config()
    if config is None:
        pytest.skip(
            f"PostgreSQL integration tests need {_URL_VAR} and"
            f" {_OPT_IN_VAR}=1 (process environment or a git-ignored"
            " repository-root .env.test)",
            allow_module_level=True,
        )
    if config.opt_in != "1":
        pytest.skip(f"{_OPT_IN_VAR} is not exactly '1'", allow_module_level=True)
    _assert_not_the_development_database(config.url)
    return config


@pytest.fixture(scope="session")
def integration_engine(integration_config: IntegrationConfig) -> Engine:
    """One Engine for the whole session, migrated to head before first use."""
    _alembic_upgrade_head(integration_config.url)
    engine = create_engine(integration_config.url, pool_pre_ping=True)
    # Prove the connection actually works before any test blames its own SQL.
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(integration_engine: Engine) -> Session:
    """A real Session whose every write is rolled back when the test ends.

    The Connection owns the outer transaction; the Session merely joins it.
    ``join_transaction_mode="create_savepoint"`` means a ``commit()`` -- from a
    test, or from any service that ever wrongly made one -- releases a
    SAVEPOINT instead of ending the real transaction, so the rollback below
    still discards everything. ``autoflush=False`` mirrors the application's
    own ``SessionLocal``.
    """
    connection = integration_engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection, autoflush=False, join_transaction_mode="create_savepoint"
    )
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()
