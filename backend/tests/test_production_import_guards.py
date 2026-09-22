"""The production import guard (Task 76 Continuation B).

Offline: no PostgreSQL, no network. Every host here is synthetic.

**Why this guard gets its own test file.** ``scripts/import_production_ministry.py``
is the only path by which real church data reaches the production database, and
the guard is the only thing standing between "import the Setup roster" and
"write a real roster into the development branch while reporting success". The
development guard (``demo_guards``) is tested separately and is deliberately
untouched by Task 76 -- these two must never be merged into one set of
conditions, because a mistake in either direction would then be a mistake in
both.

The tests are written against :func:`scripts.production_guards.evaluate`, which
is pure and takes its environment as an argument, so every rule can be exercised
exactly as it will run without setting a single real variable.
"""

from __future__ import annotations

import pytest

from scripts import production_guards as guards

PROD = "postgresql+psycopg://user:pw@prod.example.test/neondb?sslmode=require"
DEV = "postgresql+psycopg://user:pw@dev.example.test/neondb?sslmode=require"
TEST = "postgresql+psycopg://user:pw@test.example.test/neondb?sslmode=require"

SATISFIED = {
    guards.ALLOW_PRODUCTION_IMPORT: "1",
    guards.APP_ENV: "production",
    guards.PRODUCTION_DATABASE_HOST: "prod.example.test",
}


def _evaluate(env=None, **overrides):
    kwargs = {"database_url": PROD, "confirmed": True}
    kwargs.update(overrides)
    return guards.evaluate(SATISFIED if env is None else env, **kwargs)


# ==========================================================================
# 1-3 -- The permitted case
# ==========================================================================


def test_01_every_condition_satisfied_is_allowed():
    """Without this passing, every refusal below proves nothing -- they could
    all be green because the guard refuses unconditionally.
    """
    assert _evaluate().allowed is True


def test_02_the_allowed_case_lists_no_reasons():
    assert _evaluate().reasons == ()


def test_03_a_matching_host_is_success_not_a_warning():
    """The target host is *supposed* to equal the named one. Unlike the demo
    guard's forbidden-host checks, a match here is the point.
    """
    env = {**SATISFIED, guards.PRODUCTION_DATABASE_HOST: "prod.example.test"}
    assert guards.evaluate(env, database_url=PROD, confirmed=True).allowed


# ==========================================================================
# 4-9 -- Each condition, refused on its own
# ==========================================================================


def test_04_the_confirmation_flag_is_required():
    """A command-line flag, not an environment variable, precisely so it cannot
    be exported once and inherited by every later shell.
    """
    outcome = _evaluate(confirmed=False)

    assert not outcome.allowed
    assert any("--confirm-production" in reason for reason in outcome.reasons)


@pytest.mark.parametrize("value", ["", "0", "true", "True", "yes", "on", " 1", "1 ", "01"])
def test_05_the_opt_in_turns_on_for_exactly_one_value(value):
    """No truthy surprises, the same rule the dev-auth flag follows."""
    env = {**SATISFIED, guards.ALLOW_PRODUCTION_IMPORT: value}

    assert not guards.evaluate(env, database_url=PROD, confirmed=True).allowed


@pytest.mark.parametrize("app_env", ["", "development", "test", "staging", "prod"])
def test_06_app_env_must_say_production(app_env):
    env = {**SATISFIED, guards.APP_ENV: app_env}

    assert not guards.evaluate(env, database_url=PROD, confirmed=True).allowed


def test_07_a_missing_database_url_refuses_rather_than_guessing():
    outcome = _evaluate(database_url=None)

    assert not outcome.allowed
    assert any("nothing to verify" in reason for reason in outcome.reasons)


def test_08_the_target_host_must_be_named_explicitly():
    env = {k: v for k, v in SATISFIED.items() if k != guards.PRODUCTION_DATABASE_HOST}
    outcome = guards.evaluate(env, database_url=PROD, confirmed=True)

    assert not outcome.allowed
    assert any(guards.PRODUCTION_DATABASE_HOST in reason for reason in outcome.reasons)


def test_09_the_named_host_and_the_url_must_agree():
    """Naming one branch and pointing at another is refused, in either
    direction -- which is what makes the name a statement of intent rather
    than decoration.
    """
    outcome = _evaluate(database_url=DEV)

    assert not outcome.allowed
    assert any("does not point at the host named by" in r for r in outcome.reasons)


# ==========================================================================
# 10-14 -- The exclusions, which are the point
# ==========================================================================


def test_10_the_development_branch_is_refused_even_when_named_as_the_target():
    """**The realistic accident.** Every condition is satisfied, the operator
    has explicitly named the development host as the production target, and it
    is still refused -- because the machine knows that host is development.
    """
    env = {**SATISFIED, guards.PRODUCTION_DATABASE_HOST: "dev.example.test"}

    outcome = guards.evaluate(
        env, database_url=DEV, confirmed=True, development_database_url=DEV
    )

    assert not outcome.allowed
    assert any("same host as the development branch" in r for r in outcome.reasons)


def test_11_the_integration_test_branch_is_refused_the_same_way():
    env = {**SATISFIED, guards.PRODUCTION_DATABASE_HOST: "test.example.test"}

    outcome = guards.evaluate(
        env, database_url=TEST, confirmed=True, test_database_url=TEST
    )

    assert not outcome.allowed
    assert any("same host as the integration-test branch" in r for r in outcome.reasons)


def test_12_the_exclusions_are_skipped_when_this_machine_does_not_know_them():
    """A machine with no development URL configured must still be able to
    import. The exclusion is a check where it can be made, not a requirement
    that every other branch be known.
    """
    assert guards.evaluate(
        SATISFIED, database_url=PROD, confirmed=True,
        development_database_url=None, test_database_url=None,
    ).allowed


def test_13_a_matching_production_host_is_unaffected_by_the_exclusions():
    """The production host is not accidentally caught by its own exclusions."""
    assert guards.evaluate(
        SATISFIED, database_url=PROD, confirmed=True,
        development_database_url=DEV, test_database_url=TEST,
    ).allowed


# ==========================================================================
# 15-18 -- What a refusal may say
# ==========================================================================


def test_15_no_refusal_ever_names_a_host_or_a_credential():
    """A refusal explains which *setting* disagreed. Printing either host, or
    any part of a URL, would put a hostname somewhere it does not belong.
    """
    outcomes = [
        _evaluate(confirmed=False),
        _evaluate(database_url=DEV),
        guards.evaluate({}, database_url=None, confirmed=False),
        guards.evaluate(
            {**SATISFIED, guards.PRODUCTION_DATABASE_HOST: "dev.example.test"},
            database_url=DEV, confirmed=True, development_database_url=DEV,
        ),
    ]

    for outcome in outcomes:
        joined = " ".join(outcome.reasons)
        for leak in ("prod.example.test", "dev.example.test", "test.example.test",
                     "user", "pw", "postgresql"):
            assert leak not in joined, (leak, joined)


def test_16_every_failing_condition_is_reported_at_once():
    """So a misconfigured run is fixed in one pass rather than one refusal at
    a time.
    """
    outcome = guards.evaluate({}, database_url=None, confirmed=False)

    assert len(outcome.reasons) >= 5


def test_17_the_development_guard_was_not_weakened_to_allow_production():
    """Task 76's rule: the existing development-only tooling keeps its guard.

    Checked against the module rather than its prose -- demo_guards must still
    require APP_ENV=development, and must not have grown a production mode.
    """
    from scripts import demo_guards

    assert demo_guards.REQUIRED_APP_ENV == "development"
    assert not guards.evaluate is demo_guards.evaluate
    source = __import__("pathlib").Path(demo_guards.__file__).read_text()
    assert "ALLOW_PRODUCTION" not in source
    assert "confirm_production" not in source
    # And the production module did not simply re-export the dev one.
    assert guards.ALLOW_PRODUCTION_IMPORT != demo_guards.ALLOW_DEMO_SEED


def test_18_the_production_command_exposes_no_http_route():
    """It is an admin command and must never become an endpoint."""
    import ast
    from pathlib import Path

    from scripts import import_production_ministry as command

    tree = ast.parse(Path(command.__file__).read_text())
    decorators = [
        ast.unparse(d)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for d in node.decorator_list
    ]
    assert decorators == []
    for module_file in Path("app").rglob("*.py"):
        assert "import_production_ministry" not in module_file.read_text()
