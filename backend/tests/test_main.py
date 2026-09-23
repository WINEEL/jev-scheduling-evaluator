"""The server: isolated, startable with nothing set, and still fail-closed.

Offline. Nothing here starts a server or opens a socket.

Three claims:

- **Importing it loads no database, no ORM and no settings framework.** Not
  "does not query one" -- does not *import* one. That is what makes "no stored
  record is involved anywhere in this project" a checkable statement rather
  than a docstring.
- **It starts with nothing set.** No credential, no environment variable, not
  even ``APP_ENV``. A demo that needed configuration to show invented
  volunteers would make its own claim false in its setup instructions.
- **Production is refused.** The one guard. This is an experiment with
  uncalibrated thresholds, and it should not quietly become part of somebody's
  infrastructure.

Import isolation is checked in a **subprocess**, deliberately. The pytest
process has already imported the application by the time any test runs, so
asserting on ``sys.modules`` in-process would prove nothing at all.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

import pytest

from app.main import APP_ENV_VAR, build_app

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Anything whose presence would mean this server can touch, or needs, storage,
# authentication or a settings framework. None of it is a dependency of this
# project; the point of the list is that none of it can arrive by accident
# either, through a transitive import nobody noticed.
FORBIDDEN_IMPORTS = [
    "sqlalchemy",
    "psycopg",
    "psycopg2",
    "alembic",
    "authlib",
    "pydantic_settings",
    "ortools",
    "sqlite3",
]


def _in_clean_interpreter(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a fresh interpreter with a deliberately bare environment."""
    program = textwrap.dedent(
        """
        import os, sys
        # Cleared rather than merely absent: a developer's shell may well
        # have these set for something else, and the claim is that this
        # server does not care either way.
        for name in ("DATABASE_URL", "APP_ENV", "TYPESAFE_API_KEY"):
            os.environ.pop(name, None)
        """
    ) + textwrap.dedent(body)
    return subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(BACKEND_ROOT),
    )


# --------------------------------------------------------------------------
# Isolation: what importing it does not pull in
# --------------------------------------------------------------------------


def test_importing_the_server_loads_no_database_orm_or_settings_framework():
    result = _in_clean_interpreter(
        f"""
        import app.main  # noqa: F401

        leaked = [name for name in {FORBIDDEN_IMPORTS!r} if name in sys.modules]
        print("LEAKED:" + ",".join(leaked))
        """
    )

    assert result.returncode == 0, result.stderr
    assert "LEAKED:\n" in result.stdout, result.stdout


def test_it_starts_with_no_environment_variables_at_all():
    """Nothing set: no credential, no database URL, not even APP_ENV."""
    result = _in_clean_interpreter(
        """
        from app.main import app

        print("PATHS:" + ",".join(sorted(app.openapi()["paths"])))
        """
    )

    assert result.returncode == 0, result.stderr
    assert (
        "PATHS:/api/v1/jev-demo/scenarios,/api/v1/jev-demo/scenarios/{scenario}/evaluate"
        in result.stdout
    )


def test_it_starts_without_a_typesafe_key():
    """The menu works with no key; only an evaluation needs one.

    Failing at start-up would hide the three scenarios and their arithmetic
    behind a credential that only the model call actually requires.
    """
    result = _in_clean_interpreter(
        """
        from fastapi.testclient import TestClient

        from app.main import app

        response = TestClient(app).get("/api/v1/jev-demo/scenarios")
        names = [entry["name"] for entry in response.json()["scenarios"]]
        print("STATUS:" + str(response.status_code) + " NAMES:" + ",".join(names))
        """
    )

    assert result.returncode == 0, result.stderr
    assert "STATUS:200 NAMES:imbalanced,balanced,ambiguous" in result.stdout


def test_it_serves_the_two_endpoints_and_nothing_else():
    """No /health, no sign-in, no identity endpoint. One router."""
    schema = build_app().openapi()

    assert sorted(schema["paths"]) == [
        "/api/v1/jev-demo/scenarios",
        "/api/v1/jev-demo/scenarios/{scenario}/evaluate",
    ]


def test_every_path_is_under_one_prefix():
    """One namespace, so the frontend proxy forwards without special cases."""
    paths = sorted(build_app().openapi()["paths"])

    assert all(path.startswith("/api/v1/jev-demo") for path in paths)


# --------------------------------------------------------------------------
# The guard: simple locally, fail-closed in production
# --------------------------------------------------------------------------


def test_there_is_no_flag_to_set(monkeypatch):
    """Running the server is the decision; a flag would be a second one."""
    monkeypatch.delenv(APP_ENV_VAR, raising=False)

    assert sorted(build_app().openapi()["paths"])


@pytest.mark.parametrize("app_env", ["", "development", "test", "local", "staging"])
def test_every_non_production_environment_starts(monkeypatch, app_env):
    monkeypatch.setenv(APP_ENV_VAR, app_env)

    assert sorted(build_app().openapi()["paths"])


@pytest.mark.parametrize("app_env", ["production", "Production", "  PRODUCTION  "])
def test_production_is_refused_however_it_is_spelled(monkeypatch, app_env):
    """A rule that withdraws a capability must be impossible to switch off by
    accident, so every plausible spelling counts."""
    monkeypatch.setenv(APP_ENV_VAR, app_env)

    with pytest.raises(RuntimeError, match="production"):
        build_app()


def test_the_refusal_names_the_setting_and_no_value(monkeypatch):
    """Error messages name settings, never values."""
    monkeypatch.setenv(APP_ENV_VAR, "production")
    monkeypatch.setenv("TYPESAFE_API_KEY", "apikey-should-never-be-echoed")

    with pytest.raises(RuntimeError) as failure:
        build_app()

    assert "apikey-should-never-be-echoed" not in str(failure.value)
    assert APP_ENV_VAR in str(failure.value)
