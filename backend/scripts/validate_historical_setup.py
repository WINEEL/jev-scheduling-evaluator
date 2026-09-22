"""Local runner: real historical Setup period -> real solver -> local report.

    python scripts/validate_historical_setup.py \
        --input-dir ../local_data/historical_setup \
        --report-dir ../local_data/reports \
        --period-label "Setup Q4 2025" \
        --year-hint 2025

Task 43. Answers the project stakeholder's question -- *have we actually tried solving the real
scheduling problem?* -- by running the genuine ``app.scheduling.solve_schedule``
against a recent real Setup period, with **no database**.

**Privacy.** ``--input-dir`` must resolve inside a git-ignored location and the
runner refuses otherwise. The readable roster (real names) is written only
under ``--report-dir`` (also git-ignored). Standard output is the privacy-safe
summary only -- counts, ranges, runtime -- never a name.

Exit code 0 = every hard-rule check passed. Non-zero = BLOCKED: a check failed,
a source file could not be read without guessing, or a real path is not
ignored.
"""

from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from app.scheduling.solver import solve_schedule  # noqa: E402

from scripts.historical_setup.adapter import build_scheduling_input  # noqa: E402
from scripts.historical_setup.checks import run_all_checks  # noqa: E402
from scripts.historical_setup.csv_source import (  # noqa: E402
    SourceConfig,
    SourceError,
    inspect_sources,
    load_dataset,
)
from scripts.historical_setup.parsing import parse_schedule_date  # noqa: E402
from scripts.historical_setup.report import (  # noqa: E402
    compare_to_historical,
    render_privacy_safe_summary,
    render_readable_schedule,
)


def _git_ignored(path: Path) -> bool:
    """True when git would ignore ``path`` (proven, not assumed)."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=path.parent if path.parent.exists() else Path.cwd(),
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _fail(message: str) -> None:
    print(f"BLOCKED: {message}", file=sys.stderr)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--period-label", default="unlabelled Setup period")
    parser.add_argument("--year-hint", type=int, default=time.gmtime().tm_year)
    parser.add_argument("--availability-file", default=None)
    parser.add_argument("--schedule-file", default=None)
    parser.add_argument("--lead-file", default=None)
    parser.add_argument("--conflicts-file", default=None)
    parser.add_argument("--headcount", type=int, default=None)
    parser.add_argument(
        "--lead-from-schedule",
        action="store_true",
        help=(
            "TEMPORARY PROXY: treat everyone who appears in the final"
            " schedule's Setup Lead column as Lead-qualified. Evidence only"
            " -- not the Ministry Head's authoritative qualification list."
        ),
    )
    parser.add_argument(
        "--exclude-date",
        action="append",
        default=[],
        metavar="DATE[=REASON]",
        help=(
            "read this date but hold it out of the solve, e.g."
            " --exclude-date '10/17/26=ad-hoc special event'. Repeatable."
        ),
    )
    parser.add_argument(
        "--only-date",
        action="append",
        default=[],
        metavar="DATE",
        help="restrict the run to this date. Repeatable.",
    )
    parser.add_argument(
        "--requirements-from-filled-cells",
        action="store_true",
        help=(
            "take each event's required positions from the roles the final"
            " schedule actually staffed. For an ad-hoc event only -- on a"
            " recurring Sunday an empty cell is an unfilled position, not a"
            " position that was never required."
        ),
    )
    parser.add_argument(
        "--allow-no-response",
        choices=("auto", "true", "false"),
        default="auto",
        help=(
            "auto (default): permit scheduling non-responders only if the"
            " source actually contains blank availability cells, which is the"
            " only thing that can evidence its blank-cell convention."
        ),
    )
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="print source structure and stop (no solve, no report)",
    )
    args = parser.parse_args(argv)

    input_dir: Path = args.input_dir.resolve()
    report_dir: Path = args.report_dir.resolve()

    if not input_dir.is_dir():
        _fail(f"input dir does not exist: {input_dir}")
    if not _git_ignored(input_dir):
        _fail(
            f"input dir {input_dir} is NOT git-ignored -- refusing to read real"
            " data from a tracked location"
        )

    exclude_dates: dict[datetime.date, str] = {}
    for raw in args.exclude_date:
        text, _, reason = raw.partition("=")
        try:
            day = parse_schedule_date(text, year_hint=args.year_hint)
        except ValueError as exc:
            _fail(f"--exclude-date {raw!r}: {exc}")
        exclude_dates[day] = reason.strip() or "held out by the caller"

    only_dates: set[datetime.date] = set()
    for raw in args.only_date:
        try:
            only_dates.add(parse_schedule_date(raw, year_hint=args.year_hint))
        except ValueError as exc:
            _fail(f"--only-date {raw!r}: {exc}")

    config = SourceConfig(
        period_label=args.period_label,
        year_hint=args.year_hint,
        availability_file=args.availability_file,
        schedule_file=args.schedule_file,
        lead_qualification_file=args.lead_file,
        conflicts_file=args.conflicts_file,
        headcount_per_sunday=args.headcount,
        exclude_dates=exclude_dates,
        lead_from_schedule=args.lead_from_schedule,
        only_dates=frozenset(only_dates),
        requirements_from_filled_cells=args.requirements_from_filled_cells,
    )

    print("== Source files (structure only) ==")
    structures = inspect_sources(input_dir, config)
    if not structures:
        _fail(f"no CSV/TSV files found in {input_dir}")
    for struct in structures:
        print(f"  {struct.path}: {struct.rows} data rows, shape={struct.detected_shape}")
        print(f"    columns: {struct.columns}")
        if struct.redacted_columns:
            print(
                f"    ({struct.redacted_columns} volunteer-name headers withheld"
                " from stdout)"
            )
        for note in struct.notes:
            print(f"    - {note}")

    if args.inspect_only:
        return 0

    try:
        dataset = load_dataset(input_dir, config)
    except SourceError as exc:
        _fail(str(exc))

    for note in dataset.source_notes:
        print(f"  - {note}")

    n_lead = sum(1 for v in dataset.volunteers if v.lead_qualified)
    if n_lead == 0:
        print(
            "\n  WARNING: no Setup-Lead-qualified volunteers were found in the"
            " source.\n  Every Setup Lead position will come back UNFILLED"
            " (NO_QUALIFIED_CANDIDATES).\n  Supply --lead-file or a 'Lead' column"
            " in the availability grid if Lead qualification data exists.\n"
        )

    if args.allow_no_response == "auto":
        allow_no_response = dataset.availability_blank_cells > 0
        basis = (
            f"auto: {dataset.availability_blank_cells} blank availability cells"
            " in the source"
        )
    else:
        allow_no_response = args.allow_no_response == "true"
        basis = "set explicitly by the caller"
    print(f"  - allow_no_response = {allow_no_response} ({basis})")

    adapted = build_scheduling_input(dataset, allow_no_response=allow_no_response)

    start = time.monotonic()
    result = solve_schedule(adapted.scheduling_input, policy=adapted.policy)
    runtime = time.monotonic() - start

    checks = run_all_checks(adapted, result)
    comparison = (
        compare_to_historical(adapted, result, dataset)
        if dataset.historical_assignments
        else None
    )

    # -- Reports -------------------------------------------------------
    report_dir.mkdir(parents=True, exist_ok=True)
    if not _git_ignored(report_dir):
        _fail(
            f"report dir {report_dir} is NOT git-ignored -- refusing to write a"
            " roster containing real names to a tracked location"
        )

    readable_path = report_dir / "setup_generated_schedule.txt"
    readable_path.write_text(
        render_readable_schedule(adapted, result, dataset), encoding="utf-8"
    )

    summary = render_privacy_safe_summary(
        adapted, result, checks, dataset,
        runtime_seconds=runtime, overlap=comparison,
    )
    summary_path = report_dir / "setup_privacy_safe_summary.txt"
    summary_path.write_text(summary, encoding="utf-8")

    print("\n== Privacy-safe summary ==")
    print(summary)

    if result.unfilled_requirements:
        print("== Unresolved positions ==")
        for u in result.unfilled_requirements:
            role = adapted.requirement_role_name[u.requirement_id]
            date = adapted.requirement_date[u.requirement_id]
            print(
                f"  {date} {role}: missing {u.missing_count};"
                f" reasons {', '.join(u.diagnostic_codes)}"
            )

    print(f"\nReadable roster (real names, local only): {readable_path}")
    print(f"Privacy-safe summary:                    {summary_path}")

    if not checks.ok:
        print("\n== HARD-RULE VIOLATIONS ==", file=sys.stderr)
        for v in checks.all_violations:
            print(f"  - {v}", file=sys.stderr)
        _fail("hard-rule checks failed -- see violations above")

    print("\nAll hard-rule checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
