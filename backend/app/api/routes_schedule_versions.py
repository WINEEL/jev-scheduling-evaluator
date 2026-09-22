"""Schedule-version endpoints: read one, generate it, and move it through its
lifecycle.

**This is the first endpoint that changes anything**, and it is deliberately
thin. Everything it does is translation --

1. who is acting (:func:`get_current_actor`),
2. which ScheduleVersion the URL names,
3. the request body into a :class:`SchedulingPolicy`,
4. :func:`generate_draft_schedule`, which owns the whole operation,
5. the result into a response model.

**No scheduling or domain rule is repeated here.** Whether the actor may
manage the ministry, whether the version is a DRAFT, whether it is the latest,
whether the snapshot has gone stale, and whether each individual placement is
allowed are all decided in the service and the layers beneath it. A copy of any
of those checks in this module would be a second opinion that could disagree
with the first.

**The commit is not here either.** The request Session is a unit of work owned
by :func:`app.api.dependencies.get_session`: this function returns, and the
transaction commits; it raises, and everything the run wrote -- every
Assignment and every AuditEvent -- rolls back together. That is what makes
generation all-or-nothing over HTTP, and it is why there is no ``try`` block
here undoing partial work by hand.

**The two lifecycle transitions (Task 80) are the same thinness applied to the
most consequential writes in the product.** ``POST .../submit-for-review`` and
``POST .../finalize`` each resolve the version, hand it to
:mod:`app.services.schedule_lifecycle`, and map the result -- and that is all.
The legal transition graph, the staleness gate, the readiness gate, the
one-ministry-per-Sunday rule, idempotency and the audit row are the service's,
stated once there. A copy of any of them here would be a second opinion that
could disagree with the first, and the one it guards is the moment a schedule
becomes binding on volunteers.

**Why they are two paths and not one ``PATCH`` with a status.** A body naming a
target state invites a client to ask for a transition the domain does not
offer -- ``FINALIZED`` straight from ``DRAFT``, or back to ``REVIEW`` from
``FINALIZED`` -- and makes the endpoint's meaning depend on its payload. Two
named actions can only express the two things that exist.

**Who may call what.** Reading is oversight: an Admin sees any ministry's
version, whether or not they head it. Generating, submitting and finalizing are
operational writes and take the active Head of *that* ministry -- an
Admin-only actor gets 403, from the service, on every one of them (core
§4.3.1). The read reports which of the two the caller is in ``can_operate``,
so a client can render the controls that are real for them; see
:mod:`app.api.capabilities` for why that field authorizes nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.schedule_version_schemas import (
    AssignmentDetailResponse,
    FinalizationIssueResponse,
    FinalizationReadiness,
    LifecycleTransitionRequest,
    PeriodSummary,
    RequirementDetailResponse,
    RequirementFingerprintResponse,
    ScheduleVersionDetailResponse,
    ScheduleVersionStaleness,
    ScheduleVersionSummary,
    StaffingSummary,
)
from app.api.schemas import (
    AssignmentLoad,
    CreatedAssignment,
    GenerateDraftScheduleRequest,
    GenerateDraftScheduleResponse,
    GenerationMetrics,
    UnfilledRequirementReport,
)
from app.models.core import Person
from app.models.schedule_output import ScheduleVersion
from app.scheduling.solver import SchedulingPolicy
from app.services.authorization import can_operate_ministry
from app.services.schedule_generation import DraftGenerationResult, generate_draft_schedule
from app.services.schedule_lifecycle import (
    finalize_schedule_version,
    submit_schedule_version_for_review,
)
from app.services.schedule_staleness import (
    RequirementFingerprint,
    ScheduleVersionStalenessResult,
)
from app.services.schedule_version_detail import (
    ScheduleVersionDetail,
    get_schedule_version_detail,
)

__all__ = ["router"]

router = APIRouter(tags=["schedule versions"])

_VERSION_NOT_FOUND = "Schedule version not found."


@router.get(
    "/schedule-versions/{schedule_version_id}",
    response_model=ScheduleVersionDetailResponse,
    summary="Everything in one schedule version",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such schedule version."},
    },
)
def read_schedule_version(
    schedule_version_id: int = Path(
        ge=1, description="The schedule version to inspect."
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> ScheduleVersionDetailResponse:
    """What is in this version right now, and whether it is stale or ready.

    Read-only. It writes nothing, changes no status, and creates no audit row;
    the request transaction opened by :func:`get_session` closes having changed
    nothing.

    **Diagnostics are never errors here.** A stale snapshot and an unready
    version both come back with **200** and the details attached -- that is the
    answer the caller asked for. Turning either into a 4xx would leave a
    reviewer unable to see what needs fixing.

    Who may read it is the domain's decision: an active Admin, or an active
    Head of this version's ministry. A version that does not exist is 404, and
    that check happens before authorization only in the sense that both must
    pass -- neither response reveals anything the other does not.
    """
    version = _require_schedule_version(session, schedule_version_id)
    detail = get_schedule_version_detail(session, actor=actor, version=version)
    return _detail_response(detail, actor=actor)


@router.post(
    "/schedule-versions/{schedule_version_id}/generate",
    response_model=GenerateDraftScheduleResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate assignments for a draft schedule",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such schedule version."},
        409: {"description": "The version cannot be generated in its current state."},
        422: {"description": "The request body is not a usable scheduling policy."},
    },
)
def generate_schedule_version(
    schedule_version_id: int = Path(
        ge=1, description="The DRAFT schedule version to fill."
    ),
    body: GenerateDraftScheduleRequest = Body(
        default_factory=GenerateDraftScheduleRequest,
        description="Scheduling preferences for this run. Omit for the defaults.",
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> GenerateDraftScheduleResponse:
    """Fill a draft schedule's open positions, and persist what was decided.

    **200 even when the schedule comes back incomplete.** Positions nobody
    could fill are listed in ``unfilled_requirements`` with the reasons; that
    is a result to act on, not a failed request, and returning an error would
    throw away the assignments the run legitimately made.

    A version that does not exist is **404**. The service's own refusals keep
    their meanings: 403 when the actor may not manage the ministry, 409 when
    the version is in the wrong state.

    Running it twice is safe. The second run finds nothing left to fill and
    creates nothing -- ``created_count`` is ``0`` -- because the assignments
    from the first are inputs to the second.
    """
    version = _require_schedule_version(session, schedule_version_id)

    result = generate_draft_schedule(
        session,
        actor=actor,
        version=version,
        policy=_policy_from_request(body),
    )

    return _response_from_result(result, schedule_version_id=schedule_version_id)


@router.post(
    "/schedule-versions/{schedule_version_id}/submit-for-review",
    response_model=ScheduleVersionDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Submit a draft schedule for review",
    responses={
        401: {"description": "No active actor could be established."},
        403: {
            "description": (
                "The actor is not an active Ministry Head of this version's"
                " ministry. Church-wide Admin authority does not suffice."
            )
        },
        404: {"description": "No such schedule version."},
        409: {
            "description": (
                "The version cannot be submitted from its current state:"
                " it is FINALIZED, has been superseded by a newer version,"
                " or its requirement snapshot has gone stale."
            )
        },
    },
)
def submit_schedule_version(
    schedule_version_id: int = Path(
        ge=1, description="The DRAFT schedule version to submit."
    ),
    body: LifecycleTransitionRequest = Body(
        default_factory=LifecycleTransitionRequest,
        description="Optionally, a note to record on the audit event.",
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> ScheduleVersionDetailResponse:
    """DRAFT -> REVIEW. Marks the schedule as ready for a human to check.

    **Nothing becomes visible to volunteers.** REVIEW is still a working
    state: a version in it is not authoritative, and My Schedule continues to
    show its assignments to nobody but the ministry's own head and an Admin.
    Publication happens once, at finalization, and only there.

    **A version may enter REVIEW with positions still unfilled.** That is the
    domain's decision (schedule-output §6), not an oversight: a head submits a
    schedule *so that* the gaps get looked at, and refusing an incomplete one
    would make the state useless. Completeness is finalization's gate.

    Submitting a version already in REVIEW is a **200 no-op** -- provided it is
    still the latest version of its schedule. A superseded one is a 409, because
    answering "yes, done" for a version an amendment has replaced would misstate
    which version the ministry is working on.

    The response is the same body :func:`read_schedule_version` returns, so a
    review screen gets the new status and the current checks in one round trip
    rather than immediately re-reading.
    """
    version = _require_schedule_version(session, schedule_version_id)
    submit_schedule_version_for_review(
        session, actor=actor, version=version, reason=body.reason
    )
    return _detail_response(
        get_schedule_version_detail(session, actor=actor, version=version),
        actor=actor,
    )


@router.post(
    "/schedule-versions/{schedule_version_id}/finalize",
    response_model=ScheduleVersionDetailResponse,
    status_code=status.HTTP_200_OK,
    summary="Finalize a reviewed schedule, making it authoritative",
    responses={
        401: {"description": "No active actor could be established."},
        403: {
            "description": (
                "The actor is not an active Ministry Head of this version's"
                " ministry. Church-wide Admin authority does not suffice."
            )
        },
        404: {"description": "No such schedule version."},
        409: {
            "description": (
                "The version is not in REVIEW, has been superseded by a newer"
                " version, or is not ready: positions unfilled, a stale"
                " snapshot, or a readiness issue such as a person scheduled"
                " in two ministries on one Sunday."
            )
        },
    },
)
def finalize_schedule_version_endpoint(
    schedule_version_id: int = Path(
        ge=1, description="The REVIEW schedule version to finalize."
    ),
    body: LifecycleTransitionRequest = Body(
        default_factory=LifecycleTransitionRequest,
        description="Optionally, a note to record on the audit event.",
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> ScheduleVersionDetailResponse:
    """REVIEW -> FINALIZED. **This is where a schedule becomes real.**

    From the moment this commits, the version is the authoritative schedule for
    its period: volunteers see their own assignments in My Schedule, and every
    other ministry's conflict query reads these assignments as commitments
    already made. It is the most consequential write in the product, which is
    why the gate beneath it is absolute rather than advisory.

    **It fails closed.** :func:`~app.services.finalization_readiness.
    get_finalization_readiness` is re-run by the service against *current*
    state, and anything it reports is a **409** with nothing mutated -- there
    is no partial finalization and no repair on the way out. Among the things
    that stop it: an unfilled required position, a stale requirement snapshot,
    a cancelled event, a deactivated person or membership, a breached serving
    limit or scheduling rule, and a person already committed to another
    ministry that Sunday.

    **That last one cannot be overridden, by anybody** (ADR 0002/0003, Task
    79). The one-ministry-per-person-per-Sunday rule is checked with the
    absolute structural rules, before override history is consulted at all, so
    no audit payload -- including a legacy one naming ``sunday_conflict`` from
    when the conflict was briefly overridable -- authorizes a contradictory
    schedule into FINALIZED.

    **There is no reverse.** Nothing un-finalizes a version: doing so would
    retroactively free people other ministries have already scheduled around.
    A finalized schedule is corrected by creating a *successor* version and
    finalizing that, which supersedes this one by query semantics alone
    (ADR 0003) and leaves this version's own row untouched as history.

    Finalizing a version already FINALIZED is a **200 no-op** that keeps the
    original ``finalized_at`` exactly, and deliberately does **not** re-run
    readiness: what was published stays published, and a later unrelated change
    must not make an idempotent call start failing. A *superseded* finalized
    version is a 409, for the same reason submission refuses one.
    """
    version = _require_schedule_version(session, schedule_version_id)
    finalize_schedule_version(
        session, actor=actor, version=version, reason=body.reason
    )
    return _detail_response(
        get_schedule_version_detail(session, actor=actor, version=version),
        actor=actor,
    )


def _require_schedule_version(session: Session, version_id: int) -> ScheduleVersion:
    """The version the URL names, or 404.

    **404 rather than the domain's 409.** A URL that names nothing is a
    different failure from one that names something in an unusable state, and
    a client cannot fix the first by waiting or retrying. Loaded on the request
    Session, so the service that is about to act sees the same object and the
    same transactional view -- not a second copy from a second Session.
    """
    version = session.execute(
        select(ScheduleVersion).where(ScheduleVersion.id == version_id)
    ).scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_VERSION_NOT_FOUND
        )
    return version


def _policy_from_request(body: GenerateDraftScheduleRequest) -> SchedulingPolicy:
    """The request body as the domain's own policy value.

    A field-by-field construction, so an added transport field cannot silently
    become a scheduling preference. ``SchedulingPolicy`` validates itself and
    raises ``SchedulingInputError`` for values Pydantic accepts but scheduling
    cannot use -- a negative target, a role id of zero -- which the API maps
    to 422.
    """
    return SchedulingPolicy(
        allow_no_response=body.allow_no_response,
        target_assignments_per_candidate=body.target_assignments_per_candidate,
        balance_candidate_loads=body.balance_candidate_loads,
        role_variety_role_ids=body.role_variety_role_ids,
    )


def _response_from_result(
    result: DraftGenerationResult, *, schedule_version_id: int
) -> GenerateDraftScheduleResponse:
    """The domain result as the response body.

    Built field by field from the persisted Assignment rows and the pure
    scheduling result. Nothing an ORM row carries beyond these four ids can
    escape, and the AuditEvents the run wrote are not described here at all --
    they are an internal record, not part of the API.
    """
    scheduling_result = result.scheduling_result
    metrics = scheduling_result.metrics

    return GenerateDraftScheduleResponse(
        schedule_version_id=schedule_version_id,
        is_complete=result.is_complete,
        created_count=result.created_count,
        created_assignments=[
            CreatedAssignment(
                assignment_id=assignment.id,
                requirement_id=assignment.schedule_version_requirement_id,
                membership_id=assignment.ministry_membership_id,
                event_id=assignment.event_id,
            )
            for assignment in result.created_assignments
        ],
        unfilled_requirements=[
            UnfilledRequirementReport(
                requirement_id=unfilled.requirement_id,
                missing_count=unfilled.missing_count,
                diagnostic_codes=list(unfilled.diagnostic_codes),
            )
            for unfilled in scheduling_result.unfilled_requirements
        ],
        metrics=GenerationMetrics(
            assignment_loads=[
                AssignmentLoad(membership_id=membership_id, assignment_count=load)
                for membership_id, load in sorted(metrics.load_by_membership.items())
            ],
            target_excess_total=metrics.target_excess_total,
            fairness_cost=metrics.fairness_cost,
            role_variety_cost=metrics.role_variety_cost,
        ),
    )


# --------------------------------------------------------------------------
# Detail response mapping
# --------------------------------------------------------------------------


def _detail_response(
    detail: ScheduleVersionDetail, *, actor: Person
) -> ScheduleVersionDetailResponse:
    """The domain view as the response body, field by field.

    Nothing is serialized from an ORM object or a dataclass automatically: each
    field is named, so a column added to a model later cannot appear in this
    contract by accident.

    ``can_operate`` is the one field not read from ``detail``: it is asked of
    :func:`~app.services.authorization.can_operate_ministry`, the same rule the
    lifecycle endpoints above enforce, so the answer a screen renders and the
    answer a write is judged by come from one function. It reports the caller's
    authority and authorizes nothing -- see :mod:`app.api.capabilities`.
    """
    version = detail.version
    period = detail.period

    return ScheduleVersionDetailResponse(
        schedule_version=ScheduleVersionSummary(
            id=version.id,
            schedule_id=version.schedule_id,
            scheduling_period_id=version.scheduling_period_id,
            version_number=version.version_number,
            status=version.status,
            finalized_at=version.finalized_at,
            amends_version_id=version.amends_version_id,
            amendment_reason=version.amendment_reason,
            notes=version.notes,
        ),
        period=PeriodSummary(
            id=period.id,
            name=period.name,
            ministry_id=detail.ministry.id,
            ministry_name=detail.ministry.name,
            start_date=period.start_date,
            end_date=period.end_date,
            availability_locked_at=period.availability_locked_at,
        ),
        requirements=[
            RequirementDetailResponse(
                requirement_id=requirement.requirement_id,
                event_id=requirement.event_id,
                event_date=requirement.event_date,
                event_name=requirement.event_name,
                event_kind=requirement.event_kind,
                role_id=requirement.role_id,
                role_name=requirement.role_name,
                required_count=requirement.required_count,
                assigned_count=requirement.assigned_count,
            )
            for requirement in detail.requirements
        ],
        assignments=[
            AssignmentDetailResponse(
                assignment_id=assignment.assignment_id,
                requirement_id=assignment.requirement_id,
                event_id=assignment.event_id,
                membership_id=assignment.membership_id,
                person_id=assignment.person_id,
                person_display_name=assignment.person_display_name,
                role_id=assignment.role_id,
                role_name=assignment.role_name,
                is_override=assignment.is_override,
                override_reason=assignment.override_reason,
            )
            for assignment in detail.assignments
        ],
        summary=StaffingSummary(
            required_positions=detail.required_positions,
            assigned_positions=detail.assigned_positions,
            unfilled_positions=detail.unfilled_positions,
            is_fully_staffed=detail.is_fully_staffed,
        ),
        can_operate=can_operate_ministry(actor, ministry_id=detail.ministry.id),
        staleness=_staleness_response(detail.readiness.staleness),
        finalization_readiness=FinalizationReadiness(
            is_ready=detail.readiness.is_ready,
            issues=[
                FinalizationIssueResponse(
                    code=issue.code,
                    message=issue.message,
                    assignment_id=issue.assignment_id,
                    schedule_version_requirement_id=(
                        issue.schedule_version_requirement_id
                    ),
                )
                for issue in detail.readiness.issues
            ],
        ),
    )


def _staleness_response(
    staleness: ScheduleVersionStalenessResult,
) -> ScheduleVersionStaleness:
    """Task 23's frozensets, mapped explicitly and given a stable order.

    The domain compares sets, which have no order at all; a JSON array does.
    Sorting the two differences on their own fields makes the response
    reproducible for the same database state, which a client diffing two reads
    -- or a test -- depends on.
    """
    return ScheduleVersionStaleness(
        is_stale=staleness.is_stale,
        current_only=_fingerprints(staleness.current_only),
        snapshot_only=_fingerprints(staleness.snapshot_only),
    )


def _fingerprints(
    fingerprints: frozenset[RequirementFingerprint],
) -> list[RequirementFingerprintResponse]:
    return [
        RequirementFingerprintResponse(
            event_id=fingerprint.event_id,
            event_date=fingerprint.event_date,
            ministry_role_id=fingerprint.ministry_role_id,
            required_count=fingerprint.required_count,
        )
        for fingerprint in sorted(
            fingerprints,
            key=lambda f: (
                f.event_date,
                f.event_id,
                f.ministry_role_id,
                f.required_count,
            ),
        )
    ]
