"""Explicit database connectivity check.

Runs a harmless ``SELECT 1`` against the configured database (the Neon
development branch in local development). Read-only: it never modifies data or
schema. The database URL and credentials are never printed.

Usage::

    python -m app.db_check
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

from sqlalchemy import text

from app.config import get_settings, redact_url
from app.db import engine


def check_connectivity() -> None:
    """Open one connection and run ``SELECT 1``. Raises on failure."""
    with engine.connect() as connection:
        result = connection.execute(text("SELECT 1")).scalar_one()
    if result != 1:
        raise RuntimeError(f"Unexpected result from SELECT 1: {result!r}")


def _scrub(message: str, url: str) -> str:
    """Remove the password (if any) from a third-party error message."""
    password = urlsplit(url).password
    if password:
        message = message.replace(password, "***")
    return message


def main() -> int:
    database_url = get_settings().database_url
    target = redact_url(database_url)
    try:
        check_connectivity()
    except Exception as exc:  # noqa: BLE001 - surface a concise, credential-free message
        print(f"Database connectivity check FAILED for {target}")
        print(f"  {type(exc).__name__}: {_scrub(str(exc), database_url)}")
        return 1
    print(f"Database connectivity check OK for {target} (SELECT 1 -> 1)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
