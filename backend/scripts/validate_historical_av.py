"""Local runner: real historical AV quarter -> real solver -> local report.

    python scripts/validate_historical_av.py \
        --input-dir ../local_data/historical_av \
        --report-dir ../local_data/reports \
        --quarter-csv av_2026_oct_dec.csv \
        --workbook av_schedule.xlsx \
        --period-label "AV Oct-Dec 2026"

Task 44. Asks whether the generic scheduling architecture, having handled
Setup, also handles a **second ministry with specialized roles** -- by running
the genuine ``app.scheduling.solve_schedule`` against a real AV quarter, with
no database. The pipeline is Task 43's; only AV's source shape, role
vocabulary and eligibility proxy are new.

**Privacy.** ``--input-dir`` must resolve inside a git-ignored location and the
runner refuses otherwise. The readable roster (real names) is written only
under ``--report-dir`` (also git-ignored). Standard output carries counts,
never a name.

Exit code 0 = every hard-rule check passed. Non-zero = BLOCKED.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from app.scheduling.solver import solve_schedule  # noqa: E402

from scripts.historical_av.roles import AV_STAFFING_ROLE_NAMES  # noqa: E402
from scripts.historical_av.source import (  # noqa: E402
    AvSourceError,
    build_dataset,
    build_observed_eligibility,
    classify_workbook_tabs,
    read_quarter_from_csv,
    read_quarter_from_sheet,
)
from scripts.historical_av.workbook import Workbook, WorkbookError  # noqa: E402
from scripts.historical_setup.adapter import build_scheduling_input  # noqa: E402
from scripts.historical_setup.checks import run_all_checks  # noqa: E402
from scripts.historical_setup.report import (  # noqa: E402
    compare_to_historical,
    render_privacy_safe_summary,
    render_readable_schedule,
)


def _git_ignored(path: Path) -> bool:
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
    parser.add_argument("--quarter-csv", default=None)
    parser.add_argument("--quarter-tab", default=None)
    parser.add_argument("--workbook", default=None)
    parser.add_argument("--period-label", default="unlabelled AV quarter")
    parser.add_argument(
        "--eligibility-scope",
        choices=("current-quarter", "full-history"),
        default="full-history",
        help=(
            "which schedules the observed role-eligibility proxy is mined from."
            " current-quarter uses only the quarter being validated, which is"
            " narrower but circular; full-history also uses every usable"
            " workbook tab."
        ),
    )
    parser.add_argument(
        "--allow-no-response",
        choices=("auto", "true", "false"),
        default="auto",
        help=(
            "auto (default): permit scheduling non-responders only if blank"
            " cells carry an evidenced meaning. AV's sheet states none, so auto"
            " resolves to false."
        ),
    )
    parser.add_argument(
        "--target",
        type=int,
        default=None,
        help=(
            "DIAGNOSTIC ONLY: run with this soft target per volunteer. AV has"
            " no documented target, so the validation run passes none; this"
            " flag exists to measure what a target would change, not to supply"
            " one."
        ),
    )
    parser.add_argument(
        "--no-balance",
        action="store_true",
        help=(
            "switch off candidate load balancing. Only for reproducing the"
            " pre-Task-45 behaviour; a normal run balances loads without"
            " needing a numeric target."
        ),
    )
    parser.add_argument("--inspect-only", action="store_true")
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
    for path in sorted(input_dir.iterdir()):
        if path.is_file() and not _git_ignored(path):
            _fail(f"source file {path} is NOT git-ignored")

    # -- Structure inspection -------------------------------------------
    print("== Source files (structure only) ==")
    workbook: Workbook | None = None
    verdicts = []
    if args.workbook:
        try:
            workbook = Workbook(input_dir / args.workbook)
        except WorkbookError as exc:
            _fail(str(exc))
        verdicts = classify_workbook_tabs(workbook)
        usable = [v for v in verdicts if v.usable]
        print(f"  {args.workbook}: {len(workbook.sheet_names)} tabs")
        print(f"    usable for the eligibility proxy: {len(usable)}")
        for v in usable:
            print(f"      {v.name!r}: {v.shape}, {v.dated_rows} dated rows")
        for v in verdicts:
            if not v.usable:
                print(f"      REFUSED {v.name!r}: {v.reason}")

    try:
        if args.quarter_csv:
            quarter = read_quarter_from_csv(
                input_dir / args.quarter_csv, label=args.quarter_csv
            )
        elif workbook is not None and args.quarter_tab:
            quarter = read_quarter_from_sheet(workbook, args.quarter_tab)
        else:
            _fail("supply --quarter-csv, or --workbook with --quarter-tab")
    except (AvSourceError, WorkbookError) as exc:
        _fail(str(exc))

    staffed = {role for (_d, role) in quarter.assignments}
    print(f"  current quarter: {quarter.label}")
    print(f"    dates:      {len(quarter.dates)}"
          f" ({quarter.dates[0]} .. {quarter.dates[-1]})")
    print(f"    weekdays:   {sorted({d.strftime('%A') for d in quarter.dates})}")
    print(f"    volunteers: {len(quarter.volunteers)}")
    print(f"    availability tokens: {sorted(quarter.tokens_seen)}"
          f"; blank cells: {quarter.blank_cells}")
    print(f"    staffing roles used: {sorted(staffed)}")
    print(f"    prepared assignments: {len(quarter.assignments)}")
    print(f"    shadow records: {len(quarter.shadows)}"
          f" on {len({s.event_date for s in quarter.shadows})} dates")
    per_date = {}
    for (day, _role) in quarter.assignments:
        per_date[day] = per_date.get(day, 0) + 1
    sizes = sorted(set(per_date.values()))
    print(f"    staffed positions per date: {sizes}"
          + ("  (uniform)" if len(sizes) == 1 else "  (DATE-SPECIFIC)"))

    if args.inspect_only:
        return 0

    # -- Observed role-eligibility proxy --------------------------------
    roster = set(quarter.volunteers)
    if args.eligibility_scope == "full-history" and workbook is not None:
        observed, evidence, used = build_observed_eligibility(
            workbook, verdicts, roster
        )
        scope = f"{len(used)} usable workbook tabs"
    else:
        observed = {}
        for (_day, role), person in quarter.assignments.items():
            observed.setdefault(person, set()).add(role)
        observed = {k: frozenset(v) for k, v in observed.items()}
        evidence = {}
        scope = "the current quarter only"
    eligibility_source = (
        f"OBSERVED ROLE-ELIGIBILITY PROXY -- roles each volunteer has actually"
        f" been assigned in {scope}; NOT Frank's authoritative AV qualification"
        f" matrix, and it cannot show who else is qualified"
    )
    print(f"\n  - {eligibility_source}")
    print(f"  - volunteers with >=1 observed role: {len(observed)} /"
          f" {len(quarter.volunteers)}")
    for role in AV_STAFFING_ROLE_NAMES:
        n = sum(1 for roles in observed.values() if role in roles)
        print(f"      {role:<12} observed-eligible: {n}")

    dataset = build_dataset(
        quarter, observed,
        period_label=args.period_label,
        eligibility_source=eligibility_source,
    )
    for note in dataset.source_notes:
        print(f"  - {note}")

    if args.allow_no_response == "auto":
        allow_no_response = False
        basis = (
            f"auto: the sheet states no meaning for a blank cell"
            f" ({dataset.availability_blank_cells} blanks present)"
        )
    else:
        allow_no_response = args.allow_no_response == "true"
        basis = "set explicitly by the caller"
    print(f"  - allow_no_response = {allow_no_response} ({basis})")

    # -- Policy ---------------------------------------------------------
    # AV volunteers specialize, so neither of Setup's fairness preferences is
    # carried over: no documented AV target exists, and rotating a specialist
    # across roles would fight the ministry's practice.
    adapted = build_scheduling_input(
        dataset,
        allow_no_response=allow_no_response,
        target_assignments_per_candidate=args.target,
        balance_candidate_loads=not args.no_balance,
        optimize_role_variety=False,
    )
    print(f"  - policy: target_assignments_per_candidate={args.target},"
          f" balance_candidate_loads={not args.no_balance},"
          " role variety OFF (AV volunteers specialize)")
    if args.target is not None:
        print("    NOTE: --target is a diagnostic probe. AV has no documented"
              " target; this run is not the validation baseline.")

    start = time.monotonic()
    result = solve_schedule(adapted.scheduling_input, policy=adapted.policy)
    runtime = time.monotonic() - start

    checks = run_all_checks(adapted, result, dataset)
    comparison = (
        compare_to_historical(adapted, result, dataset)
        if dataset.historical_assignments
        else None
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    if not _git_ignored(report_dir):
        _fail(
            f"report dir {report_dir} is NOT git-ignored -- refusing to write a"
            " roster containing real names to a tracked location"
        )

    readable_path = report_dir / "av_generated_schedule.txt"
    readable_path.write_text(
        render_readable_schedule(adapted, result, dataset), encoding="utf-8"
    )
    summary = render_privacy_safe_summary(
        adapted, result, checks, dataset,
        runtime_seconds=runtime, overlap=comparison,
    )
    summary_path = report_dir / "av_privacy_safe_summary.txt"
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

    print("\nCross-ministry conflict validation: NOT AVAILABLE FROM SOURCE")
    print("  (no other-ministry commitment data was supplied; a zero conflict")
    print("   count here is not evidence that the church-wide rule was tested)")

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
