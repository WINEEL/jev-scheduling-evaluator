"""Seed the Neon ``development`` branch with fictional data for the local UI demo.

    python scripts/seed_demo.py

**There is no separate demo branch.** This project has exactly three Neon
branches -- production, development, integration-test -- and the empty
development branch is the intended home for this fictional data. This script
does not treat "development" as automatically safe, though: it still refuses
unless it can *prove* ``DATABASE_URL`` points at the host you explicitly named
as the target, and it still refuses if that host is the integration-test
branch.

**Read** ``scripts/demo_guards.py`` **before running this**, and see its module
docstring for exactly what these checks do and do not prove -- in particular,
none of them can recognize a production branch by its hostname; the boundary
is the explicit opt-in and explicit host match, not shape-matching.

Required, all of them, and none committed:

    APP_ENV=development
    CHURCH_SCHEDULING_ALLOW_DEMO_SEED=1
    CHURCH_SCHEDULING_DEMO_DATABASE_HOST=<host of the development branch>
    DATABASE_URL=<connection string for that same branch>

None of these is read from a file implicitly -- not the root ``.env``, not
anything else. They must be present in the process environment when this
script runs, so seeding is always a deliberate act, never an accident of
whatever ``.env`` happened to be loaded.

Run ``alembic upgrade head`` against that environment first; this script
expects the current schema and creates no migration.

**It never deletes anything.** There is no reset, no truncate and no
downgrade. Running it twice is safe and creates nothing the second time. For a
clean slate, recreate the Neon **development** branch -- which is both simpler
and much safer than teaching a script to remove church data.

Output is limited to integer ids and the fictional names this repository
already contains. No URL, host, user, password or personal data is printed,
including on the failure paths.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

# Running as a script rather than a module, so make ``app`` and ``scripts``
# importable from the backend directory regardless of the working directory.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from scripts import demo_guards  # noqa: E402
from scripts.demo_dataset import (  # noqa: E402
    CHURCH_NAME,
    MINISTRY_NAME,
    PERIOD_NAME,
    DemoDataError,
    seed_demo_data,
    verify_demo_data,
)

_REPO_ROOT = _BACKEND_DIR.parent


def read_env_file(path: Path) -> dict[str, str]:
    """Minimal ``KEY=value`` reader, used only to *discover* the one database
    this seed must refuse to touch: the integration-test branch, named in the
    git-ignored ``.env.test``.

    Deliberately not the application's settings loader: the guards must be able
    to look at ``.env.test`` without ``app.config`` learning that it exists.
    This is never used to find ``DATABASE_URL`` itself -- see
    :func:`resolve_database_url`.
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


def resolve_database_url(env: Mapping[str, str]) -> str | None:
    """The database this run would write to.

    The process environment only, with **no fallback to any file** -- not the
    root ``.env``, not anywhere else. ``DATABASE_URL`` now legitimately *is*
    the development branch for both ordinary backend work and this seed, so a
    silent file fallback here would make seeding something that could happen
    by accident. Requiring the caller to export it explicitly (see the
    ``.env.demo`` workflow in ``backend/README.md``) keeps seeding a
    deliberate act every time.
    """
    url = env.get(demo_guards.DATABASE_URL)
    return url if url else None


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    database_url = resolve_database_url(env)

    outcome = demo_guards.evaluate(
        env,
        database_url=database_url,
        test_database_url=(
            env.get("TEST_DATABASE_URL")
            or read_env_file(_REPO_ROOT / ".env.test").get("TEST_DATABASE_URL")
        ),
    )

    if not outcome.allowed:
        print("Refusing to seed demo data:")
        for reason in outcome.reasons:
            print(f"  - {reason}")
        print()
        print("Nothing was written. See scripts/seed_demo.py for the required setup.")
        return 1

    assert database_url is not None  # guaranteed by the guards above

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            try:
                result = seed_demo_data(session)
            except DemoDataError as error:
                session.rollback()
                print(f"Refusing to continue: {error}")
                print("Nothing was written.")
                return 1
            session.commit()

            verification = verify_demo_data(session)
    finally:
        engine.dispose()

    if verification.problems:
        print("Demo data was written but did not verify:")
        for problem in verification.problems:
            print(f"  - {problem}")
        return 1

    if result.already_existed:
        print("Demo data already exists on this database; nothing was created.")
    else:
        print("Demo seed complete.")

    print()
    print(f"  Demo actor Person ID: {result.demo_actor_person_id}")
    print(f"  Church:               {CHURCH_NAME} (id {result.church_id})")
    print(f"  Ministry:             {MINISTRY_NAME} (id {result.ministry_id})")
    print(f"  Period:               {PERIOD_NAME} (id {result.period_id})")
    print(f"  Admin Person ID:      {result.admin_person_id}  (optional, for the admin view)")
    print()
    print(
        f"  {verification.people} people, {verification.roles} roles,"
        f" {verification.events} Sundays, {verification.staffing_requirements} positions,"
        f" {verification.availability_rows} availability answers"
        f" ({verification.unavailable_answers} unavailable)."
    )
    print("  Availability is locked. No schedule exists yet -- start it from the UI.")
    print()
    print("Next: set CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID to the demo actor id")
    print("above in frontend/.env.local, then run the backend and frontend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
