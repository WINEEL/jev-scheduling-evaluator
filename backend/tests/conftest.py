"""Shared test setup.

The unit-test suite runs fully offline. A synthetic ``DATABASE_URL`` is placed
in the environment *before* the application package is imported, so
``app.config`` / ``app.db`` never read the real repository-root ``.env`` and
never connect to PostgreSQL. The real Neon check is the separate
``python -m app.db_check`` command.
"""

import os

# Synthetic, non-routable target. The ``.invalid`` TLD never resolves
# (RFC 6761); nothing in the suite opens a connection to it — tests only parse
# this URL and construct SQLAlchemy objects.
TEST_DATABASE_URL = (
    "postgresql+psycopg://test_user:test_password@db.test.invalid:5432/church_test"
)

# Force (not setdefault) so the suite is deterministic even when a real
# DATABASE_URL happens to be exported in the developer's shell.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """Ensure each test sees a fresh settings read (tests monkeypatch the env)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
