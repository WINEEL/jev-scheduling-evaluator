"""Local admin command: a git-ignored roster directory -> the PRODUCTION database.

    set -a && . ../.env.production-import && set +a    # from backend/
    python scripts/import_production_ministry.py \
        --confirm-production \
        --input-dir ../local_data/<ministry>/source \
        --staffing-file ../local_data/<ministry>/staffing.csv \
        --church-name "..." \
        --ministry-name "..." \
        --period-name "..." \
        --head-name "..." \
        --year-hint 2026 \
        --lead-from-schedule

**Why this exists as a second command.** :mod:`scripts.import_local_ministry`
does exactly the same import, and its guard refuses to run anywhere but a named
development branch. Task 76 requires that guard to stay exactly as it is, so the
production path is a separate command with its own, stricter guard rather than a
flag that relaxes the existing one. The *import itself* is not duplicated: both
commands call the same :func:`scripts.local_ministry_import.import_dataset`, so
production and development get identical validation, identical services and
identical results.

**There is no HTTP equivalent and there must never be one.** The product has no
endpoint that creates a Person, a Ministry, a membership or a scheduling period,
and this command is deliberately not a step toward one -- it runs from a trusted
admin machine, against a database whose host the operator names out loud.

**What it will not do.**

- It will not run without ``--confirm-production`` typed on the command line.
- It will not write to any database but the one whose host is named in
  ``CHURCH_SCHEDULING_PRODUCTION_DATABASE_HOST``.
- It will not write to the development or integration-test branch, even if you
  name one of them as the target -- see :mod:`scripts.production_guards`.
- It will not read a directory git does not prove is ignored.
- **It never resets, drops, truncates or overwrites anything.** Importing into a
  ministry name that already exists is refused by the importer itself, so a
  second run cannot quietly double a roster or replace a schedule.

**What it prints.** Counts, ids and dates. No name, no email, no address, no
cell value, on every path including every failure path. The roster it reads
stays in the git-ignored directory it came from.

Required in the process environment, none of it committed:

    APP_ENV=production
    CHURCH_SCHEDULING_ALLOW_PRODUCTION_IMPORT=1
    CHURCH_SCHEDULING_PRODUCTION_DATABASE_HOST=<host of the production branch>
    DATABASE_URL=<connection string for that same branch>

Exit codes: 0 imported, 1 refused by the guards or by the importer, 2 the
source could not be read without guessing.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from scripts import production_guards  # noqa: E402
from scripts.historical_setup.csv_source import (  # noqa: E402
    SourceConfig,
    SourceError,
    load_dataset,
)
from scripts.import_local_ministry import (  # noqa: E402
    _report,
    build_parser,
    git_ignored,
)
from scripts.person_mapping import (  # noqa: E402
    PersonMappingError,
    load_person_map,
)
from scripts.local_ministry_import import (  # noqa: E402
    ImportPlan,
    LocalImportError,
    import_dataset,
    load_staffing_overrides,
)
from scripts.seed_demo import read_env_file  # noqa: E402

_REPO_ROOT = _BACKEND_DIR.parent


def main(argv: list[str] | None = None) -> int:
    # The development command's parser, plus the one flag that separates the
    # two. Sharing it means the two commands cannot drift into accepting
    # different arguments for the same import.
    parser = build_parser()
    parser.prog = "python scripts/import_production_ministry.py"
    parser.description = __doc__
    parser.add_argument(
        "--confirm-production",
        action="store_true",
        help=(
            "Required. Confirms that this import is meant to write to the"
            " production database. Deliberately a command-line flag and not an"
            " environment variable, so it cannot be exported once and inherited"
            " by every later shell."
        ),
    )
    args = parser.parse_args(argv)

    input_dir: Path = args.input_dir.resolve()
    if not input_dir.is_dir():
        print(f"BLOCKED: {args.input_dir} is not a directory.", file=sys.stderr)
        return 2
    if not git_ignored(input_dir):
        print(
            "BLOCKED: the input directory is not git-ignored, so reading it could"
            " put real records where they can be committed. Nothing was read.",
            file=sys.stderr,
        )
        return 2

    env = os.environ
    database_url = env.get(production_guards.DATABASE_URL) or None

    # The two branches this command must refuse, read locally where available.
    # Neither file is committed; only the host is ever extracted from them.
    repo_env = read_env_file(_REPO_ROOT / ".env")
    test_env = read_env_file(_REPO_ROOT / ".env.test")

    outcome = production_guards.evaluate(
        env,
        database_url=database_url,
        confirmed=bool(args.confirm_production),
        development_database_url=repo_env.get("DATABASE_URL"),
        test_database_url=env.get("TEST_DATABASE_URL") or test_env.get("TEST_DATABASE_URL"),
    )
    if not outcome.allowed:
        print("Refusing to import into production:")
        for reason in outcome.reasons:
            print(f"  - {reason}")
        print()
        print("Nothing was read and nothing was written.")
        return 1

    assert database_url is not None  # guaranteed by the guards above

    config = SourceConfig(
        period_label=args.period_name,
        year_hint=args.year_hint,
        availability_file=args.availability_file,
        schedule_file=args.schedule_file,
        lead_from_schedule=args.lead_from_schedule,
    )
    try:
        dataset = load_dataset(input_dir, config)
    except SourceError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 2

    staffing_overrides: dict = {}
    if args.staffing_file is not None:
        staffing_path: Path = args.staffing_file.resolve()
        if not git_ignored(staffing_path):
            print(
                "BLOCKED: the staffing rule file is not git-ignored. It names"
                " this ministry's own events, so it belongs beside the roster,"
                " not where it can be committed.",
                file=sys.stderr,
            )
            return 2
        try:
            staffing_overrides = load_staffing_overrides(
                staffing_path,
                known_roles=[role.name for role in dataset.roles],
                known_dates=dataset.sundays,
                year_hint=args.year_hint,
            )
        except LocalImportError as error:
            print(f"BLOCKED: {error}", file=sys.stderr)
            print("Nothing was written.", file=sys.stderr)
            return 2

    # The same explicit-reuse mechanism as the development command. It matters
    # more here: production is where a second ministry's import would otherwise
    # give a volunteer a second Person and split their My Schedule in two.
    person_map = None
    if args.person_map is not None:
        map_path: Path = args.person_map.resolve()
        if not git_ignored(map_path):
            print(
                "BLOCKED: the person map is not git-ignored. It pairs real"
                " names with internal ids, so it belongs beside the roster,"
                " not where it can be committed.",
                file=sys.stderr,
            )
            return 2
        try:
            person_map = load_person_map(map_path)
        except PersonMappingError as error:
            print(f"BLOCKED: {error}", file=sys.stderr)
            print("Nothing was written.", file=sys.stderr)
            return 2

    plan = ImportPlan(
        person_map=person_map,
        church_name=args.church_name,
        ministry_name=args.ministry_name,
        period_name=args.period_name,
        head_name=args.head_name,
        admin_name=args.admin_name,
        also_lead_names=frozenset(args.also_lead),
        lock_availability_when_done=args.lock_availability,
        staffing_overrides=staffing_overrides,
    )

    engine = create_engine(database_url, pool_pre_ping=True)
    started = time.monotonic()
    try:
        with Session(engine) as session:
            try:
                summary = import_dataset(session, dataset=dataset, plan=plan)
            except LocalImportError as error:
                # The importer's own refusals -- an existing ministry of this
                # name, a head who is not on the roster, and so on. Rolled back,
                # so a refused production import leaves nothing behind.
                session.rollback()
                print(f"Refusing to continue: {error}")
                print("Nothing was written.")
                return 1
            session.commit()
    finally:
        engine.dispose()

    _report(summary, dataset_caveats=dataset.source_notes, seconds=time.monotonic() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
