"""Run the Task 81 joined multi-ministry trial and print what it decided.

    cd backend
    python scripts/joined_trial.py

Development tooling. **It opens no database, reads no file and writes
nothing** -- the scenario is built in code by
``tests/joined_trial_fixture.py``, so this command is reproducible on any
machine and cannot reach anyone's data. Every person, ministry, role and date
it prints is synthetic.

What it is for: seeing the joined solve's behaviour without reading a test
report. The same facts are asserted in ``tests/test_scheduling_joined.py``;
this prints them.

Options::

    --kids-helpers N        demand on the last Sunday of the Kids-like
                            ministry (default 9; raise it past the roster to
                            watch an unfillable demand be reported rather than
                            resolved by breaking the church-wide rule)
    --setup-helpers N       helpers per Sunday in the Setup-like ministry
    --separate              additionally solve each ministry on its own, and
                            report where the two approaches differ

Exit codes: 0 the run completed (a schedule with unfilled positions is a
completed run), 1 the engine refused the input.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.scheduling.joined import solve_joined_schedule  # noqa: E402
from app.scheduling.solver import (  # noqa: E402
    SchedulingInputError,
    solve_schedule,
)
from tests.joined_trial_fixture import build_joined_scenario  # noqa: E402

_MINISTRY_LABEL = {1: "Setup-like", 2: "AV-like", 3: "Kids-like"}


def _label(ministry_id: int) -> str:
    return f"{ministry_id} ({_MINISTRY_LABEL.get(ministry_id, 'ministry')})"


def _report(scenario, result, elapsed: float) -> None:
    print("JOINED MULTI-MINISTRY TRIAL")
    print("=" * 62)
    print(f"ministries      : {len(scenario)}")
    print(f"solve time      : {elapsed:.2f}s")
    print(f"positions filled: {result.filled_count}")
    print(f"positions open  : {result.unfilled_count}")
    print()

    for entry in scenario:
        scheduling_input = entry.scheduling_input
        ministry_id = scheduling_input.ministry_id
        outcome = result.results_by_ministry[ministry_id]
        required = scheduling_input.total_required_positions
        print(f"ministry {_label(ministry_id)}")
        print(f"  required / new placements / open : "
              f"{required} / {outcome.filled_count} / {outcome.unfilled_count}")
        print(f"  people carrying work             : "
              f"{len(outcome.metrics.load_by_membership)}")
        print(f"  backup placements                : "
              f"{outcome.metrics.backup_placement_total}")
        print(f"  target excess / fairness / variety: "
              f"{outcome.metrics.target_excess_total} /"
              f" {outcome.metrics.fairness_cost} /"
              f" {outcome.metrics.role_variety_cost}")
        for unfilled in outcome.unfilled_requirements:
            print(f"  OPEN requirement {unfilled.requirement_id}:"
                  f" {unfilled.missing_count} missing"
                  f" -- {', '.join(unfilled.diagnostic_codes) or 'no diagnostic'}")
        print()

    print("church-wide person/date contentions"
          " (two or more ministries wanted one person that date)")
    if not result.contentions:
        print("  none")
    for contention in result.contentions:
        got = (
            f"ministry {contention.scheduled_ministry_id}"
            if contention.scheduled_ministry_id is not None
            else "nobody"
        )
        others = ", ".join(str(m) for m in contention.contending_ministry_ids)
        print(f"  person {contention.person_id} on {contention.event_date}:"
              f" {got}; also wanted by {others}")
    print()
    print("No church-wide ministry priority exists, and none was applied."
          " Where the rule forced a choice it was broken by which schedule"
          " fills more positions overall, and every such choice is listed"
          " above so it can be taken to the ministry leads rather than"
          " settled here.")


def _report_separate(scenario, joined_result) -> None:
    """Solve each ministry alone and say where that differs.

    A separate solve knows nothing of the other ministries in the run, so it
    will happily place one person in two of them on one Sunday -- which is the
    comparison worth printing, because it is exactly what the joined path
    exists to prevent.
    """
    print()
    print("-" * 62)
    print("the same three ministries, solved separately")
    total = 0
    placed: dict[tuple[int, object], set[int]] = {}
    for entry in scenario:
        scheduling_input = entry.scheduling_input
        alone = solve_schedule(scheduling_input, policy=entry.policy)
        total += alone.filled_count
        person_of = {
            candidate.membership_id: candidate.person_id
            for candidate in scheduling_input.candidates
        }
        dates = {
            requirement.requirement_id: requirement.event_date
            for requirement in scheduling_input.requirements
        }
        for proposal in alone.proposed_assignments:
            key = (person_of[proposal.membership_id], dates[proposal.requirement_id])
            placed.setdefault(key, set()).add(scheduling_input.ministry_id)
        print(f"  ministry {_label(scheduling_input.ministry_id)}:"
              f" {alone.filled_count} placed, {alone.unfilled_count} open")

    breaches = sorted(key for key, ministries in placed.items() if len(ministries) > 1)
    print(f"  total placed separately : {total}")
    print(f"  total placed jointly    : {joined_result.filled_count}")
    print(f"  church-wide rule breaches in the separate runs: {len(breaches)}")
    for person_id, event_date in breaches:
        print(f"    person {person_id} placed in"
              f" {sorted(placed[(person_id, event_date)])} on {event_date}")
    print("  (the separate runs above are given no cross-ministry data at"
          " all, which is the point: solved in isolation nothing stops them,"
          " and in production that gap is closed by blocking dates from"
          " already-finalized commitments -- which a joined solve does not"
          " need because it sees every ministry at once.)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kids-helpers", type=int, default=9)
    parser.add_argument("--setup-helpers", type=int, default=2)
    parser.add_argument("--separate", action="store_true")
    args = parser.parse_args(argv)

    scenario = build_joined_scenario(
        setup_helpers_per_sunday=args.setup_helpers,
        kids_final_sunday_helpers=args.kids_helpers,
    )
    started = time.monotonic()
    try:
        result = solve_joined_schedule(scenario)
    except SchedulingInputError as error:
        print(f"the joined input was refused: {error}", file=sys.stderr)
        return 1
    _report(scenario, result, time.monotonic() - started)

    if args.separate:
        _report_separate(scenario, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
