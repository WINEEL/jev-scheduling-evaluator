"""The scheduling engine's own package.

Deliberately free of SQLAlchemy, Sessions and ORM models. Everything here is
plain Python values, so the solver that arrives in a later task can be
developed, run and tested without a database -- and so the rules it optimizes
against are readable without knowing how any of it is stored.

Turning persisted state into these values is the *services* layer's job:
:mod:`app.services.scheduling_input_builder`.
"""

from app.scheduling.input import (
    AvailabilityState,
    CandidateInput,
    ExistingAssignmentInput,
    RequirementInput,
    SchedulingInput,
)
from app.scheduling.result import (
    ProposedAssignment,
    SchedulingResult,
    SolutionMetrics,
    UnfilledRequirement,
)
from app.scheduling.solver import (
    SchedulingEngineError,
    SchedulingInputError,
    SchedulingPolicy,
    solve_schedule,
)

__all__ = [
    # input
    "AvailabilityState",
    "CandidateInput",
    "ExistingAssignmentInput",
    "RequirementInput",
    "SchedulingInput",
    # result
    "ProposedAssignment",
    "SchedulingResult",
    "SolutionMetrics",
    "UnfilledRequirement",
    # solver
    "SchedulingEngineError",
    "SchedulingInputError",
    "SchedulingPolicy",
    "solve_schedule",
]
