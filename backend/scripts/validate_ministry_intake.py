"""Dry-run validation of one ministry's source file. Writes to no database.

    cd backend
    python scripts/validate_ministry_intake.py \
        --config scripts/intake_templates/av_source_config.example.toml \
        --source ../local_data/historical_av/av_2026_oct_dec.csv \
        --identity ../local_data/historical_av/av_identity.csv \
        --qualifications ../local_data/historical_av/av_qualifications.csv \
        --dry-run

``--dry-run`` is required and is the **only** mode this command has. There is
deliberately no ``--import``: a real import is a separate, later, explicitly
authorized action through ``scripts/import_local_ministry.py`` (development) or
``scripts/import_production_ministry.py`` (production, four proofs of intent),
and neither is reachable from here. Nothing in this file or in
``scripts.ministry_intake`` opens a session.

**Privacy.** Every file this reads must be git-ignored, and the command refuses
otherwise -- the same rule the Setup and AV validators use, for the same
reason. Names do appear on stdout, but only for rows a person has to go and
fix: an unresolved identity, a contradicted assignment, a missing approval.
"3 unresolved people" is not something anybody can act on. ``--no-names``
renders those as counts where the terminal must stay name-free.

Exit codes: ``0`` ready for production import, ``1`` read but not ready,
``2`` refused or unreadable.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from scripts.ministry_intake.config import (  # noqa: E402
    IntakeConfigError,
    load_config,
)
from scripts.ministry_intake.dry_run import run_dry_run  # noqa: E402
from scripts.ministry_intake.qualifications import template_text  # noqa: E402
from scripts.ministry_intake.readiness import (  # noqa: E402
    evaluate_readiness,
    render_verdict,
)
from scripts.ministry_intake.report import render  # noqa: E402


def git_ignored(path: Path) -> bool:
    """Whether git would ignore this path. Absent git, nothing is ignored."""
    parent = path.parent if path.parent.exists() else Path.cwd()
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=parent,
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def _fail(message: str) -> None:
    print(f"REFUSED: {message}", file=sys.stderr)
    raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        help=(
            "TOML declaring this ministry's roles and its source's shape."
            " Committable: it names columns, never people."
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="The ministry's own file. Must be git-ignored.",
    )
    parser.add_argument(
        "--tab",
        default=None,
        help="Which worksheet to read, for an .xlsx source. Required for one.",
    )
    parser.add_argument(
        "--identity",
        type=Path,
        default=None,
        help=(
            "CSV of source_name,decision,canonical_person_id declaring which"
            " existing Person each source name is. Never a name match."
        ),
    )
    parser.add_argument(
        "--qualifications",
        type=Path,
        default=None,
        help=(
            "The Ministry Head's approval matrix. Historical placements are"
            " never promoted into it."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Required. Validate and report; write nothing, anywhere.",
    )
    parser.add_argument(
        "--no-names",
        action="store_true",
        help="Render per-person lines as counts, for a name-free terminal.",
    )
    parser.add_argument(
        "--report-file",
        type=Path,
        default=None,
        help="Also write the report here. Must be git-ignored.",
    )
    parser.add_argument(
        "--emit-qualification-template",
        type=Path,
        default=None,
        help=(
            "Write a blank approval matrix for this config's roles and exit."
            " The file to send the Ministry Head."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except IntakeConfigError as error:
        _fail(str(error))

    if args.emit_qualification_template is not None:
        target: Path = args.emit_qualification_template
        if target.exists():
            _fail(f"{target} already exists; not overwriting it")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(template_text(config), encoding="utf-8")
        print(f"Blank {config.ministry_label} approval matrix written to {target}")
        print("Send it to the Ministry Head. Until both attestation lines are")
        print("filled in, the dry run treats it as a draft.")
        return 0

    if not args.dry_run:
        _fail(
            "--dry-run is required. This command has no import mode: importing"
            " is a separate, explicitly authorized action."
        )
    if args.source is None:
        _fail("--source is required for a dry run")

    for label, path in (
        ("source", args.source),
        ("identity file", args.identity),
        ("qualification matrix", args.qualifications),
    ):
        if path is None:
            continue
        resolved = Path(path).resolve()
        if not resolved.is_file():
            _fail(f"{label} does not exist: {resolved.name}")
        if not git_ignored(resolved):
            _fail(
                f"{label} {resolved.name} is NOT git-ignored -- refusing to read"
                " a ministry's own records from a tracked location"
            )

    report = run_dry_run(
        config=config,
        source=args.source,
        tab=args.tab,
        identity_file=args.identity,
        qualification_file=args.qualifications,
    )
    verdict = evaluate_readiness(report)

    text = "\n".join(
        [render(report, show_names=not args.no_names), "", render_verdict(verdict)]
    )
    print(text)

    if args.report_file is not None:
        destination = Path(args.report_file).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not git_ignored(destination):
            _fail(
                f"report file {destination.name} is NOT git-ignored -- refusing"
                " to write a report naming real people to a tracked location"
            )
        destination.write_text(text + "\n", encoding="utf-8")
        print(f"\nReport written to {destination}")

    return 0 if verdict.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
