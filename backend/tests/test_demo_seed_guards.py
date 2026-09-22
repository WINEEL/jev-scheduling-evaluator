"""The demo seed's fail-closed guards.

**The intended target is the existing Neon development branch.** There is no
separate demo branch, so these tests deliberately do *not* treat "the target
equals the development database" as something to refuse -- that is the
success case. What must still be refused: a host that disagrees with what was
explicitly configured, the integration-test branch, and anything short of the
full explicit opt-in.

This is the part of the demo tooling where a mistake would be expensive -- not
a failed command, but fictional people written into a real church's database.
So the tests are the rules themselves: it must refuse by default, refuse when
unsure, and never put a hostname or a credential into anything it says.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts import demo_guards
from scripts.demo_guards import (
    ALLOW_DEMO_SEED,
    APP_ENV,
    DEMO_DATABASE_HOST,
    evaluate,
    host_of,
    host_of_setting,
)
from scripts.seed_demo import resolve_database_url

#: Stands in for the real Neon development branch: this is what
#: CHURCH_SCHEDULING_DEMO_DATABASE_HOST is set to once developer's own host is
#: configured locally, and it is the intended, successful target.
TARGET_HOST = "dev-branch.example"
TARGET_URL = f"postgresql+psycopg://dev_user:dev_pw@{TARGET_HOST}/neondb?sslmode=require"
TEST_URL = "postgresql+psycopg://test_user:test_pw@test-branch.example/neondb?sslmode=require"
OTHER_URL = "postgresql+psycopg://other_user:other_pw@somewhere-else.example/neondb"

SECRETS = ("dev_pw", "test_pw", "other_pw", "dev_user", "test_user", "other_user")


def working_env(**overrides: str) -> dict[str, str]:
    """An environment where every guard is satisfied: the explicit target
    equals the (stand-in for the) development branch.
    """
    env = {
        ALLOW_DEMO_SEED: "1",
        APP_ENV: "development",
        DEMO_DATABASE_HOST: TARGET_HOST,
    }
    env.update(overrides)
    return env


def test_the_happy_path_is_allowed():
    """Established first, so every refusal below is known to be caused by the
    thing it names rather than by a permanently broken guard.

    This is also test 1 of the correction's required list: an explicitly
    configured development target succeeds through the guards.
    """
    outcome = evaluate(working_env(), database_url=TARGET_URL, test_database_url=TEST_URL)

    assert outcome.allowed
    assert outcome.reasons == ()


# ==========================================================================
# 1-4: the required opt-ins
# ==========================================================================


def test_01_refuses_without_the_explicit_opt_in():
    env = working_env()
    del env[ALLOW_DEMO_SEED]

    outcome = evaluate(env, database_url=TARGET_URL)

    assert not outcome.allowed
    assert any(ALLOW_DEMO_SEED in reason for reason in outcome.reasons)


@pytest.mark.parametrize("value", ["", "0", "true", "TRUE", "yes", "on", " 1", "2"])
def test_01b_the_opt_in_must_be_exactly_one(value: str):
    """Same rule as the dev-auth flag: a dangerous switch turns on for one
    value and nothing else.
    """
    outcome = evaluate(working_env(**{ALLOW_DEMO_SEED: value}), database_url=TARGET_URL)

    assert not outcome.allowed


def test_02_refuses_outside_a_development_environment():
    for app_env in ("production", "test", "staging", "Development"):
        outcome = evaluate(working_env(**{APP_ENV: app_env}), database_url=TARGET_URL)

        assert not outcome.allowed, app_env
        assert any(APP_ENV in reason for reason in outcome.reasons)


def test_03_refuses_without_a_configured_demo_host():
    env = working_env()
    del env[DEMO_DATABASE_HOST]

    outcome = evaluate(env, database_url=TARGET_URL)

    assert not outcome.allowed
    assert any(DEMO_DATABASE_HOST in reason for reason in outcome.reasons)


def test_03b_refuses_when_the_demo_host_is_blank():
    outcome = evaluate(working_env(**{DEMO_DATABASE_HOST: "   "}), database_url=TARGET_URL)

    assert not outcome.allowed


def test_03c_refuses_without_a_database_url():
    for url in (None, "", "not a url"):
        outcome = evaluate(working_env(), database_url=url)

        assert not outcome.allowed, url


def test_04_refuses_when_the_database_host_is_not_the_configured_target():
    """The central check: the seed writes only to the branch it was
    explicitly told about, even though the intended branch is the ordinary
    development one -- "development" alone is not the credential; the
    explicit host match is.
    """
    outcome = evaluate(working_env(), database_url=OTHER_URL)

    assert not outcome.allowed
    assert any("does not point at the host named by" in reason for reason in outcome.reasons)


# ==========================================================================
# 6-7: the two databases that must never be seeded
# ==========================================================================


def test_06_refuses_when_the_target_is_the_integration_test_branch():
    """Even if someone deliberately configured the demo host to be the test
    branch, knowing what the test branch is overrules them.
    """
    outcome = evaluate(
        working_env(**{DEMO_DATABASE_HOST: "test-branch.example"}),
        database_url=TEST_URL,
        test_database_url=TEST_URL,
    )

    assert not outcome.allowed
    assert any("TEST_DATABASE_URL" in reason for reason in outcome.reasons)


def test_07_the_development_branch_is_the_intended_target_not_a_forbidden_one():
    """The corrected model, stated as a test: there is no check anywhere that
    refuses because the target host happens to be the ordinary development
    database. Matching the explicitly configured host is success, full stop.
    """
    outcome = evaluate(working_env(), database_url=TARGET_URL, test_database_url=TEST_URL)

    assert outcome.allowed
    # And `evaluate`'s signature itself carries no such parameter any more.
    import inspect

    assert "development_database_url" not in inspect.signature(evaluate).parameters


def test_07b_an_unknown_test_branch_does_not_weaken_the_host_check():
    """A machine with no `.env.test` configured still gets the primary
    guarantee: the target must match the explicitly configured host.
    """
    outcome = evaluate(working_env(), database_url=OTHER_URL, test_database_url=None)

    assert not outcome.allowed


def test_06b_no_implicit_environment_file_fallback_for_database_url():
    """`resolve_database_url` (used by the script's ``main()``) reads only the
    process environment. A ``.env`` sitting on disk -- even one holding a
    perfectly valid ``DATABASE_URL`` -- must never silently become the seed's
    target: seeding is only ever deliberate, from an explicitly exported
    environment.
    """
    file_only_env: dict[str, str] = {}  # simulates "nothing exported this shell"

    assert resolve_database_url(file_only_env) is None

    # Only an explicit export in the process environment is honoured.
    assert resolve_database_url({"DATABASE_URL": TARGET_URL}) == TARGET_URL


def test_07c_refusals_accumulate_rather_than_stopping_at_the_first():
    """A misconfigured setup is told everything that is wrong at once."""
    outcome = evaluate({}, database_url=None)

    assert len(outcome.reasons) >= 3


# ==========================================================================
# 5: nothing sensitive is ever surfaced
# ==========================================================================


def test_05_no_reason_ever_contains_a_url_host_or_credential():
    """Every refusal path, checked together. Being told *which* setting
    disagreed is enough to fix the problem; printing either value would put a
    hostname or a password somewhere it does not belong.
    """
    outcomes = [
        evaluate({}, database_url=None),
        evaluate({}, database_url=TARGET_URL, test_database_url=TEST_URL),
        evaluate(working_env(), database_url=OTHER_URL),
        evaluate(working_env(**{DEMO_DATABASE_HOST: "test-branch.example"}),
                 database_url=TEST_URL, test_database_url=TEST_URL),
        evaluate(working_env(**{APP_ENV: "production"}), database_url=TARGET_URL),
    ]

    for outcome in outcomes:
        combined = " ".join(outcome.reasons)
        for secret in SECRETS:
            assert secret not in combined
        for host in (TARGET_HOST, "test-branch.example", "somewhere-else.example"):
            assert host not in combined
        assert "://" not in combined
        assert "@" not in combined


def test_05b_the_guard_module_reads_only_the_host_of_a_url():
    """`host_of` is the only way a URL is inspected, and it returns a host or
    nothing -- never a user, password or database name.
    """
    assert host_of(TARGET_URL) == TARGET_HOST
    assert host_of("postgresql://u:p@Host.Example:5432/db") == "host.example"
    assert host_of(None) is None
    assert host_of("") is None
    assert host_of("::::") is None


def test_05c_the_configured_host_accepts_the_shapes_people_actually_paste():
    for value, expected in [
        ("demo-branch.example", "demo-branch.example"),
        ("  demo-branch.example  ", "demo-branch.example"),
        ("DEMO-BRANCH.EXAMPLE", "demo-branch.example"),
        ("demo-branch.example:5432", "demo-branch.example"),
        ("demo-branch.example/", "demo-branch.example"),
        ("postgresql://u:p@demo-branch.example/neondb", "demo-branch.example"),
    ]:
        assert host_of_setting(value) == expected, value

    assert host_of_setting(None) is None
    assert host_of_setting("") is None


# ==========================================================================
# 17, 19: what the tooling must not contain
# ==========================================================================


def _demo_tooling_sources() -> list[Path]:
    root = Path(demo_guards.__file__).resolve().parent
    return sorted(path for path in root.glob("*.py") if path.name != "__init__.py")


def _code_of(path: Path) -> str:
    """Source with docstrings and comments removed, so prose explaining that
    something is absent cannot fail an assertion about behaviour.
    """
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


def test_17_the_demo_tooling_can_delete_nothing():
    """No reset, by design. The safe way to a clean slate is recreating the
    Neon demo branch, not teaching a script to remove church data.
    """
    sources = _demo_tooling_sources()
    assert sources, "expected to find the demo tooling"

    for path in sources:
        code = _code_of(path).upper()
        for destructive in ("DROP TABLE", "DROP SCHEMA", "DROP DATABASE", "TRUNCATE", "DELETE FROM"):
            assert destructive not in code, (path.name, destructive)

    for path in sources:
        tree = ast.parse(path.read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        # No ORM deletion, and no Alembic downgrade.
        for forbidden in ("delete", "downgrade", "drop", "drop_all"):
            assert forbidden not in called, (path.name, forbidden)


def test_19_the_demo_tooling_changes_no_model_or_migration():
    # Ten migrations since Task 79 added ``person.church_membership_status``.
    # The count is still asserted so the demo tooling adding one would fail
    # here.
    assert len(list(Path("alembic/versions").glob("*.py"))) == 10

    for path in _demo_tooling_sources():
        tree = ast.parse(path.read_text())
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        ]
        for schema_name in ("Base", "mapped_column", "Mapped", "relationship", "MetaData"):
            assert schema_name not in imported, (path.name, schema_name)


def test_19b_no_application_code_imports_the_demo_tooling():
    """Demo tooling is not shipped: ``pyproject`` installs only ``app``, and
    nothing in ``app`` may depend on this.
    """
    for path in Path("app").rglob("*.py"):
        source = path.read_text()
        assert "scripts.demo" not in source, path
        assert "from scripts" not in source, path
