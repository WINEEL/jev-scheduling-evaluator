"""Local command: a git-ignored roster directory -> the development database.

    set -a && . ../.env.demo && set +a          # from backend/
    python scripts/import_local_ministry.py \
        --input-dir ../local_data/<your-ministry>/source \
        --church-name "..." \
        --ministry-name "..." \
        --period-name "..." \
        --head-name "..." \
        --staffing-file ../local_data/<your-ministry>/staffing.csv \
        --year-hint 2026

**Why a command and not a screen.** The product has no endpoint that creates a
Person, a Ministry, a membership or a scheduling period, so a real roster
cannot be put in front of the UI any other way. This exists so a local demo
environment can be populated in one step; it is development tooling and is not
part of the installed package.

**What it will not do.** It writes only to the database whose host you name
explicitly, it reads only from a directory git proves is ignored, and it never
deletes or overwrites anything -- importing into a ministry name that already
exists is refused. Run it again under a different ministry name to rehearse
again.

**What it prints.** Counts, ids and dates. No name, no email, no cell value,
including on every failure path. The roster it reads stays in the git-ignored
directory it came from and in the local database; nothing about it is written
back to this repository.

The same environment gate as the demo seed, because it is the same danger --
a script that writes a lot of rows into whatever database happens to be
configured. All of these must be present in the process environment, and none
of them is committed:

    APP_ENV=development
    CHURCH_SCHEDULING_ALLOW_DEMO_SEED=1
    CHURCH_SCHEDULING_DEMO_DATABASE_HOST=<host of the development branch>
    DATABASE_URL=<connection string for that same branch>

Read ``scripts/demo_guards.py`` for exactly what those checks do and do not
prove -- in particular, none of them can recognize a production branch by its
hostname.

Exit codes: 0 imported, 1 refused by the guards or by the importer, 2 the
source could not be read without guessing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

# Running as a script rather than a module, so make ``app`` and ``scripts``
# importable from the backend directory regardless of the working directory.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from scripts import demo_guards  # noqa: E402
from scripts.historical_setup.csv_source import (  # noqa: E402
    SourceConfig,
    SourceError,
    load_dataset,
)
from scripts.person_mapping import (  # noqa: E402
    PersonMappingError,
    load_person_map,
)
from scripts.local_ministry_import import (  # noqa: E402
    ImportPlan,
    ImportSummary,
    LocalImportError,
    import_dataset,
    load_staffing_overrides,
)
from scripts.seed_demo import read_env_file  # noqa: E402

_REPO_ROOT = _BACKEND_DIR.parent


def git_ignored(path: Path) -> bool:
    """True when git would ignore ``path`` -- proven, never assumed.

    The same check the historical validation runners make, for the same
    reason: a roster that git can see is a roster that can be committed by
    accident, and this command must refuse to read one.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=path.parent if path.parent.exists() else Path.cwd(),
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def build_parser() -> argparse.ArgumentParser:
    """Every church-specific value is an argument, deliberately.

    No default names anything. A default church, ministry or head would be
    this repository carrying a real organization's details in tracked code,
    which is the one thing this whole design exists to avoid.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir",
        required=True,
        type=Path,
        help="Directory of roster CSVs. Must be git-ignored.",
    )
    parser.add_argument("--church-name", required=True)
    parser.add_argument("--ministry-name", required=True)
    parser.add_argument("--period-name", required=True)
    parser.add_argument(
        "--head-name",
        required=True,
        help=(
            "The volunteer on this roster to make Ministry Head. This is the"
            " person the frontend acts as; the command prints their Person id."
        ),
    )
    parser.add_argument(
        "--admin-name",
        default="Local Import Admin",
        help=(
            "Bootstrap administrator, created if absent. Not one of the roster's"
            " own people: promoting a volunteer would invent an authority"
            " nobody granted."
        ),
    )
    parser.add_argument("--year-hint", type=int, default=time.gmtime().tm_year)
    parser.add_argument("--availability-file", default=None)
    parser.add_argument("--schedule-file", default=None)
    parser.add_argument(
        "--lead-from-schedule",
        action="store_true",
        help=(
            "PROXY: treat everyone appearing in the source schedule's lead"
            " column as lead-qualified. Evidence, never the Ministry Head's"
            " authoritative list -- so it is opt-in and reported as a caveat."
        ),
    )
    parser.add_argument(
        "--also-lead",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Treat this person as lead-qualified whatever the source says."
            " How a current correction to an older roster is applied without"
            " editing the source or this code. Repeatable."
        ),
    )
    parser.add_argument(
        "--staffing-file",
        default=None,
        type=Path,
        metavar="PATH",
        help=(
            "CSV of the head's own staffing rule: columns event_date, role,"
            " required_count. A date listed there takes exactly those roles and"
            " counts; a date not listed keeps the source's shape. Must be"
            " git-ignored, like the roster itself. Without it, staffing comes"
            " from the source, which states how many columns a spreadsheet has"
            " rather than how many people the ministry needs."
        ),
    )
    parser.add_argument(
        "--person-map",
        type=Path,
        default=None,
        help=(
            "CSV of source_name,person_id declaring that named volunteers are"
            " people who ALREADY EXIST. Without it every volunteer becomes a"
            " new Person, which duplicates anybody who also serves in a"
            " ministry imported earlier. Never a name match: the file states"
            " which internal id, and every row is verified before anything is"
            " written. See scripts/person_mapping.py."
        ),
    )
    parser.add_argument(
        "--lock-availability",
        action="store_true",
        help=(
            "Close availability at the end of the import. Required before a"
            " schedule can be started, and one-way: there is no unlock. Leave"
            " it off to demonstrate the availability screen as editable, then"
            " lock it from the scheduling-periods screen."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

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
    database_url = env.get(demo_guards.DATABASE_URL) or None
    outcome = demo_guards.evaluate(
        env,
        database_url=database_url,
        test_database_url=(
            env.get("TEST_DATABASE_URL")
            or read_env_file(_REPO_ROOT / ".env.test").get("TEST_DATABASE_URL")
        ),
    )
    if not outcome.allowed:
        print("Refusing to import:")
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
                session.rollback()
                print(f"Refusing to continue: {error}")
                print("Nothing was written.")
                return 1
            session.commit()
    finally:
        engine.dispose()

    _report(summary, dataset_caveats=dataset.source_notes, seconds=time.monotonic() - started)
    return 0


def _report(
    summary: ImportSummary, *, dataset_caveats: tuple[str, ...], seconds: float
) -> None:
    """Counts, ids and dates only -- checked by a test, not by eye."""
    print("Import complete.")
    print()
    print(f"  Ministry Head Person ID: {summary.head_person_id}")
    print(f"  Ministry id:             {summary.ministry_id}")
    print(f"  Scheduling period id:    {summary.period_id}")
    print(f"  Church id:               {summary.church_id}")
    print(f"  Admin Person ID:         {summary.admin_person_id}")
    print()
    print(
        f"  {summary.people_reused} of them attached to people who already"
        " existed (from --person-map); the rest were created"
    )
    print(
        f"  {summary.people} people, {summary.roles} roles,"
        f" {summary.sunday_events} Sundays, {summary.special_events} special event(s),"
        f" {summary.staffing_requirements} positions"
    )
    print(
        "  Staffing from the head's own rule on"
        f" {summary.events_with_overridden_staffing} event(s); from the source"
        " elsewhere"
    )
    print(
        f"  {summary.qualifications_granted} role qualifications granted,"
        f" {summary.qualifications_declined} declined"
    )
    print(f"  {summary.availability_rows} availability answers recorded")
    print(
        f"  Period: {summary.period_start.isoformat()} to"
        f" {summary.period_end.isoformat()}"
    )
    print(
        "  Availability: "
        + ("locked" if summary.availability_locked else "open (lock it before scheduling)")
    )
    print(f"  Took {seconds:.1f}s")

    if dataset_caveats:
        print()
        print("  How the source was read:")
        for note in dataset_caveats:
            print(f"    - {note}")

    print()
    print("Next: set CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID to the Ministry")
    print("Head Person ID above in frontend/.env.local, then run the backend and")
    print("frontend and open the ministry from the home page.")


if __name__ == "__main__":
    sys.exit(main())
