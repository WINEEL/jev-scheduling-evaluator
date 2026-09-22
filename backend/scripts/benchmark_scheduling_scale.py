"""Local benchmark: the balance pass at the scale of a real ministry quarter.

    python scripts/benchmark_scheduling_scale.py
    python scripts/benchmark_scheduling_scale.py --events 8 9 10 11 12 13

Task 68. Times ``solve_schedule`` against the synthetic quarter in
``tests.scheduling_scale_fixture`` -- no private data, no database -- and
reports one line per pass so a regression shows up as the pass that caused it.

``--default-strategy`` re-runs the same input with CP-SAT's general-purpose
strategy instead of the named one the engine ships -- that is, the behaviour
from before Task 68 -- so the before and after print side by side. The optima
must be identical either way; they are what proves the speed-up changed only
the search, not the answer.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
if str(_HERE.parent / "tests") not in sys.path:
    sys.path.insert(0, str(_HERE.parent / "tests"))

from ortools.sat.python import cp_model  # noqa: E402

from app.scheduling import solver as solver_module  # noqa: E402
from app.scheduling.solver import SchedulingPolicy, solve_schedule  # noqa: E402

from scheduling_scale_fixture import build_scale_input  # noqa: E402

POLICY = SchedulingPolicy(
    allow_no_response=False,
    target_assignments_per_candidate=None,
    balance_candidate_loads=True,
)


def _timed_passes():
    """Wrap ``_solve_pass`` so each pass reports its own wall time."""
    original = solver_module._solve_pass
    times: list[tuple[str, float]] = []

    def wrapper(solver, model, label, *args, **kwargs):
        start = time.perf_counter()
        original(solver, model, label, *args, **kwargs)
        times.append((label, time.perf_counter() - start))

    return original, wrapper, times


class _DefaultStrategySolver(cp_model.CpSolver):
    """CP-SAT's own default strategy: what the engine used before Task 68."""

    def Solve(self, model, *args, **kwargs):  # noqa: N802 - CP-SAT's spelling
        self.parameters.subsolvers.clear()
        return super().Solve(model, *args, **kwargs)


def run(*, events: int, default_strategy: bool, seed: int) -> None:
    scheduling_input = build_scale_input(seed=seed, events=events)
    original, wrapper, times = _timed_passes()
    solver_module._solve_pass = wrapper
    original_solver = solver_module.cp_model.CpSolver
    if default_strategy:
        solver_module.cp_model.CpSolver = _DefaultStrategySolver

    try:
        start = time.perf_counter()
        result = solve_schedule(scheduling_input, policy=POLICY)
        elapsed = time.perf_counter() - start
    finally:
        solver_module._solve_pass = original
        solver_module.cp_model.CpSolver = original_solver

    required = scheduling_input.total_required_positions
    filled = len(result.proposed_assignments)
    label = f"{events:>2} events x 10 roles = {required:>3} positions"
    strategy = "CP-SAT default" if default_strategy else solver_module._SEARCH_STRATEGY
    print(f"\n{label}   strategy={strategy}")
    for name, seconds in times:
        print(f"    {name:<20} {seconds:9.3f}s")
    print(f"    {'TOTAL':<20} {elapsed:9.3f}s")
    print(f"    filled {filled}/{required}   backup "
          f"{result.metrics.backup_placement_total}   fairness "
          f"{result.metrics.fairness_cost}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, nargs="+", default=[13])
    parser.add_argument(
        "--default-strategy", action="store_true",
        help="also time CP-SAT's default strategy (the pre-Task-68 behaviour)",
    )
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    modes = [False, True] if args.default_strategy else [False]
    for default_strategy in modes:
        for events in args.events:
            run(events=events, default_strategy=default_strategy, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
