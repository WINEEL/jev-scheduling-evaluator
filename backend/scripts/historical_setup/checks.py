"""Programmatic verification of a solver result against its input.

Task 43's hard-rule list. Each check re-derives the fact from the pure
``SchedulingInput`` and the ``SchedulingResult`` -- it does not trust the
solver -- and returns a list of human-readable violation strings. An empty
list from every check is the pass condition; any violation means the runner
stops BLOCKED.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.scheduling.input import AvailabilityState, SchedulingInput
from app.scheduling.result import SchedulingResult
from app.scheduling.solver import SchedulingPolicy

from scripts.historical_setup.adapter import AdaptedInput
from scripts.historical_setup.model import HistoricalDataset

__all__ = ["CheckReport", "run_all_checks"]


@dataclass(slots=True)
class CheckReport:
    unavailable_person_violations: list[str] = field(default_factory=list)
    no_response_violations: list[str] = field(default_factory=list)
    unqualified_lead_violations: list[str] = field(default_factory=list)
    church_conflict_violations: list[str] = field(default_factory=list)
    one_per_event_violations: list[str] = field(default_factory=list)
    capacity_violations: list[str] = field(default_factory=list)
    accounting_violations: list[str] = field(default_factory=list)
    non_staffing_requirement_violations: list[str] = field(default_factory=list)
    conflict_claim_violations: list[str] = field(default_factory=list)

    @property
    def all_violations(self) -> list[str]:
        out: list[str] = []
        for name in (
            "unavailable_person_violations",
            "no_response_violations",
            "unqualified_lead_violations",
            "church_conflict_violations",
            "one_per_event_violations",
            "capacity_violations",
            "accounting_violations",
            "non_staffing_requirement_violations",
            "conflict_claim_violations",
        ):
            out.extend(getattr(self, name))
        return out

    @property
    def ok(self) -> bool:
        return not self.all_violations


def run_all_checks(
    adapted: AdaptedInput,
    result: SchedulingResult,
    dataset: "HistoricalDataset | None" = None,
) -> CheckReport:
    si = adapted.scheduling_input
    policy = adapted.policy
    report = CheckReport()

    candidates_by_id = {c.membership_id: c for c in si.candidates}
    requirements_by_id = {r.requirement_id: r for r in si.requirements}

    # 1 & 2: availability -- never UNAVAILABLE; NO_RESPONSE only under policy.
    for proposal in result.proposed_assignments:
        req = requirements_by_id[proposal.requirement_id]
        cand = candidates_by_id[proposal.membership_id]
        state = cand.availability_for(req.event_id)
        if state is AvailabilityState.UNAVAILABLE:
            report.unavailable_person_violations.append(
                f"requirement {proposal.requirement_id}: membership"
                f" {proposal.membership_id} is UNAVAILABLE for event {req.event_id}"
            )
        if state is AvailabilityState.NO_RESPONSE and not policy.allow_no_response:
            report.no_response_violations.append(
                f"requirement {proposal.requirement_id}: membership"
                f" {proposal.membership_id} is NO_RESPONSE but allow_no_response"
                " is False"
            )

    # 3: proposed Lead assignments must be qualified.
    for proposal in result.proposed_assignments:
        req = requirements_by_id[proposal.requirement_id]
        cand = candidates_by_id[proposal.membership_id]
        if not cand.is_qualified_for(req.ministry_role_id):
            label = adapted.requirement_role_name.get(req.requirement_id, "?")
            report.unqualified_lead_violations.append(
                f"requirement {proposal.requirement_id} ({label}): membership"
                f" {proposal.membership_id} is not qualified for role"
                f" {req.ministry_role_id}"
            )

    # 4: no candidate church-wide blocked on that date (when conflict data
    #    exists).
    for proposal in result.proposed_assignments:
        req = requirements_by_id[proposal.requirement_id]
        cand = candidates_by_id[proposal.membership_id]
        if cand.is_blocked_on(req.event_date):
            report.church_conflict_violations.append(
                f"requirement {proposal.requirement_id}: membership"
                f" {proposal.membership_id} is church-conflicted on"
                f" {req.event_date.isoformat()}"
            )

    # 5: one candidate max per Event (this ministry).
    seen: dict[tuple[int, int], int] = {}
    for proposal in result.proposed_assignments:
        key = (proposal.membership_id, proposal.event_id)
        seen[key] = seen.get(key, 0) + 1
    for (membership_id, event_id), count in seen.items():
        if count > 1:
            report.one_per_event_violations.append(
                f"membership {membership_id} proposed {count} times in event"
                f" {event_id}"
            )

    # 6: no requirement exceeds capacity from solver proposals.
    placed: dict[int, int] = {}
    for proposal in result.proposed_assignments:
        placed[proposal.requirement_id] = placed.get(proposal.requirement_id, 0) + 1
    for rid, count in placed.items():
        required = requirements_by_id[rid].required_count
        if count > required:
            report.capacity_violations.append(
                f"requirement {rid}: {count} proposals for required_count"
                f" {required}"
            )

    # 7: filled + unfilled accounting is correct.
    total_required = si.total_required_positions
    filled = result.filled_count
    unfilled = result.unfilled_count
    if filled + unfilled != total_required:
        report.accounting_violations.append(
            f"filled ({filled}) + unfilled ({unfilled}) != total required"
            f" ({total_required})"
        )
    # Per-requirement: proposals + missing == required_count.
    missing_by_req = {
        u.requirement_id: u.missing_count for u in result.unfilled_requirements
    }
    for req in si.requirements:
        got = placed.get(req.requirement_id, 0) + missing_by_req.get(
            req.requirement_id, 0
        )
        if got != req.required_count:
            report.accounting_violations.append(
                f"requirement {req.requirement_id}: placed"
                f" {placed.get(req.requirement_id, 0)} + missing"
                f" {missing_by_req.get(req.requirement_id, 0)} !="
                f" required {req.required_count}"
            )

    # 8: a role the ministry records but never staffs (AV's Shadow) must not
    #    have become a requirement. If it had, every date without one would be
    #    reported as an unfilled position the ministry never actually needed.
    if dataset is not None:
        non_staffing = {
            r.name for r in dataset.roles if not r.is_staffing_position
        }
        for req in si.requirements:
            name = adapted.requirement_role_name.get(req.requirement_id)
            if name in non_staffing:
                report.non_staffing_requirement_violations.append(
                    f"requirement {req.requirement_id} on"
                    f" {req.event_date.isoformat()} staffs {name!r}, which this"
                    " ministry records but does not require"
                )

    # 9: never let a clean conflict result be read as *verified* when no
    #    cross-ministry data was supplied. Zero violations against an empty
    #    input is not evidence, and the two must not be able to drift apart.
    if dataset is not None and not dataset.conflict_data_available:
        claimed = sum(1 for c in si.candidates if c.blocked_dates)
        if claimed:
            report.conflict_claim_violations.append(
                f"{claimed} candidates carry blocked dates, but the dataset"
                " reports no cross-ministry conflict source -- the run would"
                " claim a check it cannot support"
            )

    return report
