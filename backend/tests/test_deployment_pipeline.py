"""The deployment pipeline's one load-bearing ordering, pinned.

**Why this file exists.** Task 79 was pushed; Cloud Build built, pushed and
deployed the new API successfully; and the first sign-in afterwards returned a
500, because the deployed code read ``person.church_membership_status`` while
production Alembic was still at the previous revision. Nothing in the pipeline
had ever applied a migration -- the runbook said to do it by hand, and a hand
step is one somebody eventually does not do.

The fix is a Cloud Build step that runs ``alembic upgrade head`` from the image
about to be deployed, before that image is given traffic. The fix is also one
line in a YAML file, in a file nothing else tests, edited rarely and under
pressure -- exactly the shape of change that gets quietly dropped. So the
guarantee is asserted here instead of remembered:

1. the migration step exists and really runs ``alembic upgrade head``;
2. it runs the **image this build produced**, not some other Alembic;
3. **every deploy step is downstream of it** -- computed over the whole
   dependency graph rather than by matching a literal ``waitFor`` list, so a
   legitimate reordering still passes and a dropped edge still fails;
4. the database URL reaches it as a Secret Manager ``secretEnv``, and never as
   a substitution, a command-line argument, or a printed value;
5. the image can actually migrate -- Alembic is a runtime dependency and the
   migration scripts are copied in.

This is a static read of ``cloudbuild.yaml``, ``backend/Dockerfile`` and
``backend/pyproject.toml``. It cannot prove Google runs the graph the way the
file describes; it proves the file still describes the graph that was reviewed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip(
    "yaml", reason="pyyaml is a dev dependency; see backend/pyproject.toml"
)

# tests/ -> backend/ -> repository root
_BACKEND = Path(__file__).resolve().parents[1]
_ROOT = _BACKEND.parent
_CLOUDBUILD = _ROOT / "cloudbuild.yaml"
_DOCKERFILE = _BACKEND / "Dockerfile"
_PYPROJECT = _BACKEND / "pyproject.toml"

MIGRATION_STEP = "migrate-database"
#: The steps that put code in front of real users. Each must be downstream of
#: the migration.
DEPLOY_STEPS = ("deploy-api", "deploy-web")
#: The Secret Manager secret holding the production connection string. The same
#: one the API service already reads; this adds a reader, not a secret.
DATABASE_SECRET = "database-url"


@pytest.fixture(scope="module")
def pipeline() -> dict:
    return yaml.safe_load(_CLOUDBUILD.read_text())


@pytest.fixture(scope="module")
def steps(pipeline: dict) -> dict[str, dict]:
    """Every step by id, with the ids in file order preserved separately."""
    return {step["id"]: step for step in pipeline["steps"]}


def _script(step: dict) -> str:
    """The shell body of a step, as one string."""
    return "\n".join(str(arg) for arg in step.get("args", []))


def _dependencies(pipeline: dict) -> dict[str, set[str]]:
    """Each step's direct predecessors, following Cloud Build's own rules.

    Two cases that are easy to get wrong, and both matter here:

    - a step with **no** ``waitFor`` runs after *every* preceding step, which
      is Cloud Build's sequential default -- not "after nothing";
    - ``waitFor: ["-"]`` means "start immediately", which is the one way to
      declare no dependency at all.
    """
    order = [step["id"] for step in pipeline["steps"]]
    direct: dict[str, set[str]] = {}
    for index, step in enumerate(pipeline["steps"]):
        declared = step.get("waitFor")
        if declared is None:
            direct[step["id"]] = set(order[:index])
        else:
            direct[step["id"]] = {name for name in declared if name != "-"}
    return direct


def _ancestors(pipeline: dict, step_id: str) -> set[str]:
    """Every step that must finish before ``step_id`` starts, transitively."""
    direct = _dependencies(pipeline)
    seen: set[str] = set()
    pending = list(direct.get(step_id, ()))
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(direct.get(current, ()))
    return seen


# ==========================================================================
# 1. The step exists, and does what it says
# ==========================================================================


def test_the_pipeline_file_parses(pipeline):
    assert pipeline["steps"], "cloudbuild.yaml declares no steps"


def test_a_migration_step_exists(steps):
    assert MIGRATION_STEP in steps, (
        f"cloudbuild.yaml has no '{MIGRATION_STEP}' step. Deploying code"
        " without applying its migrations is what took production down after"
        " Task 79; if this step is being removed, that outage is being"
        " reintroduced."
    )


def test_the_migration_step_upgrades_to_head(steps):
    """``upgrade head`` specifically -- not ``stamp``, which would record a
    revision as applied without applying it, and not a named revision, which
    would go stale the next time somebody adds a migration."""
    script = _script(steps[MIGRATION_STEP])

    assert re.search(r"alembic\s+upgrade\s+head", script), (
        "the migration step no longer runs `alembic upgrade head`"
    )
    assert "downgrade" not in script, (
        "an automated deployment must never downgrade production"
    )
    assert not re.search(r"alembic\s+stamp", script), (
        "`alembic stamp` records a revision as applied without applying it,"
        " which is the failure this step exists to prevent, written down"
    )


def test_the_migration_runs_the_image_this_build_produced(steps, pipeline):
    """From the newly built API image, so the migration scripts are by
    construction the ones the deployed code expects.

    Checked three ways, because each alone could be satisfied by something
    else: the step runs a container, the reference it runs is built from the
    API service's image name, and it uses the tag this build resolved.
    """
    step = steps[MIGRATION_STEP]
    script = _script(step)

    assert "docker run" in script
    assert "_API_SERVICE" in script, (
        "the migration must run the API image, not an arbitrary Alembic"
    )
    assert "/workspace/.image_tag" in script, (
        "the migration must use this build's resolved tag, so it runs the"
        " exact image that deploy-api will deploy"
    )
    # And it is downstream of the build that produces that image.
    assert "build-api" in _ancestors(pipeline, MIGRATION_STEP)


# ==========================================================================
# 2. Nothing serving traffic overtakes it
# ==========================================================================


@pytest.mark.parametrize("deploy_step", DEPLOY_STEPS)
def test_every_deploy_step_waits_for_the_migration(pipeline, steps, deploy_step):
    """**The assertion this whole file is for.**

    Computed over the transitive dependency graph rather than by matching a
    literal ``waitFor`` list: reordering the pipeline for a good reason should
    not fail this test, and dropping the edge should -- however indirectly the
    edge was expressed.
    """
    assert deploy_step in steps, f"cloudbuild.yaml has no '{deploy_step}' step"

    ancestors = _ancestors(pipeline, deploy_step)

    assert MIGRATION_STEP in ancestors, (
        f"'{deploy_step}' can start before '{MIGRATION_STEP}' has finished."
        " That is exactly the Task 79 outage: new code serving traffic against"
        " a schema that has not been migrated."
    )


def test_the_migration_does_not_wait_for_a_deploy(pipeline):
    """The converse, which would be a cycle in intent if not in the graph:
    migrating *after* deploying is the same bug with extra steps."""
    ancestors = _ancestors(pipeline, MIGRATION_STEP)

    for deploy_step in DEPLOY_STEPS:
        assert deploy_step not in ancestors


# ==========================================================================
# 3. The secret, handled as a secret
# ==========================================================================


def test_the_database_url_comes_from_secret_manager(pipeline):
    available = pipeline.get("availableSecrets", {}).get("secretManager", [])
    mapped = {entry["env"]: entry["versionName"] for entry in available}

    assert "DATABASE_URL" in mapped, (
        "the migration step's DATABASE_URL must come from Secret Manager"
    )
    assert f"/secrets/{DATABASE_SECRET}/" in mapped["DATABASE_URL"], (
        f"the migration must read the existing '{DATABASE_SECRET}' secret --"
        " the same one the API service reads -- rather than a new copy of it"
    )


def test_the_migration_step_opts_into_that_secret(steps):
    assert steps[MIGRATION_STEP].get("secretEnv") == ["DATABASE_URL"]


def test_no_other_step_receives_the_database_url(steps):
    """Least privilege, by step. Only the one that migrates needs it."""
    holders = [
        step_id
        for step_id, step in steps.items()
        if "DATABASE_URL" in (step.get("secretEnv") or [])
    ]

    assert holders == [MIGRATION_STEP]


def test_the_database_url_is_never_printed_or_put_on_a_command_line(steps):
    """Three ways it could leak into a log, all refused.

    ``docker run -e DATABASE_URL`` -- the name with no ``=value`` -- passes the
    variable by inheritance, so the value never appears in the command the
    build logs. ``-e DATABASE_URL=...`` would put it there, and a build log is
    readable by anyone with build viewer access.
    """
    script = _script(steps[MIGRATION_STEP])

    assert not re.search(r"echo[^\n]*DATABASE_URL[^\n]*\$", script), (
        "the migration step must not echo the database URL's value"
    )
    assert "set -x" not in script, (
        "`set -x` would print every expanded command, including the one"
        " carrying the connection string"
    )
    assert not re.search(r"-e\s+DATABASE_URL=", script), (
        "pass the variable by name (`-e DATABASE_URL`), never by value"
    )
    assert re.search(r"-e\s+DATABASE_URL(?![=\w])", script), (
        "the migration container has to be given DATABASE_URL somehow"
    )


def test_no_connection_string_is_written_in_the_pipeline_file():
    """A last, blunt check on the file as text: no credential, no host, no
    substitution pretending to be a secret."""
    text = _CLOUDBUILD.read_text()

    assert not re.search(r"postgres(ql)?(\+\w+)?://", text), (
        "a database URL is written into cloudbuild.yaml"
    )
    assert not re.search(r"^\s*_DATABASE_URL\s*:", text, re.MULTILINE), (
        "a substitution is recorded in build metadata and printed in the"
        " build log; a secret must not be one"
    )


def test_the_step_fails_the_build_rather_than_continuing(steps):
    """``set -e`` is what turns a failed migration into a failed build, and a
    failed build is what stops deploy-api."""
    script = _script(steps[MIGRATION_STEP])

    assert re.search(r"set -euo pipefail", script), (
        "without `set -e` a failed `alembic upgrade` would leave the step"
        " green and the deployment would proceed against the old schema"
    )


# ==========================================================================
# 4. The image can actually migrate
# ==========================================================================


def test_alembic_is_a_runtime_dependency_of_the_api_package():
    """Not a dev extra. The Dockerfile installs the base dependencies only, so
    an Alembic listed under ``[project.optional-dependencies].dev`` would not
    be in the image the migration step runs -- which would fail the build on
    every deployment rather than silently, but fail it all the same."""
    text = _PYPROJECT.read_text()
    runtime_block = text.split("[project.optional-dependencies]", 1)[0]

    assert re.search(r'^\s*"alembic[><=~]', runtime_block, re.MULTILINE), (
        "alembic must be a runtime dependency: the deployed image is what"
        " runs the migration"
    )


def test_the_image_carries_the_migration_scripts():
    """An image that can start and cannot migrate is the same outage with a
    different error message."""
    dockerfile = _DOCKERFILE.read_text()

    assert "COPY backend/alembic " in dockerfile or "COPY backend/alembic\n" in dockerfile
    assert "alembic.ini" in dockerfile


def test_every_migration_on_disk_is_reachable_from_a_single_head():
    """`upgrade head` is only unambiguous while there is one head.

    Two heads -- the ordinary result of two branches each adding a migration --
    make ``alembic upgrade head`` fail at deploy time with a message about
    multiple heads, on a build that had no other problem. Catching it here
    turns that into a test failure on the branch that created it.
    """
    versions = sorted((_BACKEND / "alembic" / "versions").glob("*.py"))
    assert versions, "no migrations found"

    revisions: dict[str, str | None] = {}
    for path in versions:
        text = path.read_text()
        revision = re.search(r"^revision:\s*str\s*=\s*['\"]([^'\"]+)", text, re.MULTILINE)
        down = re.search(
            r"^down_revision:[^=]*=\s*(?:['\"]([^'\"]+)['\"]|None)", text, re.MULTILINE
        )
        assert revision, path.name
        assert down, path.name
        revisions[revision.group(1)] = down.group(1)

    parents = {down for down in revisions.values() if down is not None}
    heads = sorted(set(revisions) - parents)

    assert len(heads) == 1, f"alembic has {len(heads)} heads: {heads}"
