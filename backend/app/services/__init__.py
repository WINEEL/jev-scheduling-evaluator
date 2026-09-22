"""Domain services: the application's write operations.

Each service function performs one domain operation -- validating authorization,
applying the change, and recording the audit event that explains it.

Transaction ownership
---------------------
**A service never begins, commits or rolls back a transaction. The caller owns
the boundary.** A service *may* call ``session.flush()`` -- never ``commit()``
or ``rollback()`` -- when it genuinely needs an intra-transaction effect a
later statement in the same operation depends on, such as obtaining the
identity of a row it just inserted so an audit event can reference it (Task 14,
:func:`app.services.role_qualification.set_role_qualification`). A flush sends
pending SQL within the caller's already-open transaction; it commits nothing
and is invisible to anyone outside the transaction until the caller commits.

Services receive an explicit :class:`~sqlalchemy.orm.Session` and join whatever
transaction it is already in. SQLAlchemy begins one implicitly on first use, so
a service needs no ceremony to participate:

.. code-block:: python

    with SessionLocal() as session, session.begin():
        grant_ministry_head(session, actor=admin, membership=membership)
        # commit happens here, once, on the way out

Why this convention, and not a service that commits its own work:

- **It is the only shape that keeps a domain change and its audit row atomic.**
  A service that committed internally could not be composed -- two operations in
  one request would be two transactions, and a failure in the second would leave
  the first permanently written. Head authority is the sharpest case: the audit
  row is the *only* record that a promotion happened (core §5), so a commit that
  saved the flag without the row would erase the history silently.
- **It matches request-scoped sessions.** A later FastAPI dependency will open a
  session per request and commit once at the end; services written this way need
  no change to work inside it.
- **It has no hidden nested commits.** There is exactly one commit, and it is in
  code the caller can see. A permitted flush is not a commit: nothing it writes
  is durable, and a rollback after a flush discards it exactly as it would
  discard an un-flushed change.

Two obligations this places on callers, both deliberate:

1. **Commit exactly once**, after the operation returns.
2. **Roll back if it raises.** Services raise before mutating wherever they can,
   but once a change is applied to a persistent object it lives in the session
   until the transaction ends. ``session.begin()`` as a context manager does
   this automatically, which is why it is the recommended form above.

What is deliberately absent
---------------------------
No repository interfaces wrapping ``Session.get``, no base service class, no
unit-of-work abstraction, no event bus, and no async. Services use SQLAlchemy
directly and take their collaborators as arguments; there is nothing here to
learn beyond Python and the ORM.
"""

from app.services.audit import (
    ACTION_ASSIGNMENT_ADDED,
    ACTION_ASSIGNMENT_OVERRIDE_APPLIED,
    ACTION_ASSIGNMENT_REMOVED,
    ACTION_AVAILABILITY_CHANGED,
    ACTION_AVAILABILITY_CLEARED,
    ACTION_AVAILABILITY_LOCKED,
    ACTION_AVAILABILITY_RECORDED,
    ACTION_EVENT_CREATED,
    ACTION_EXISTING_COMMITMENT_CHANGED,
    ACTION_EXISTING_COMMITMENT_RECORDED,
    ACTION_EXISTING_COMMITMENT_REMOVED,
    ACTION_MEMBER_GROUP_CREATED,
    ACTION_MEMBER_GROUP_LIMIT_CHANGED,
    ACTION_MEMBER_GROUP_LIMIT_CLEARED,
    ACTION_MEMBER_GROUP_LIMIT_RECORDED,
    ACTION_MEMBER_GROUP_MEMBER_ADDED,
    ACTION_MEMBER_GROUP_MEMBER_REMOVED,
    ACTION_MINISTRY_HEAD_GRANTED,
    ACTION_MINISTRY_HEAD_REVOKED,
    ACTION_MINISTRY_ROLE_CHANGED,
    ACTION_MINISTRY_ROLE_CREATED,
    ACTION_MINISTRY_ROLE_DEACTIVATED,
    ACTION_MINISTRY_ROLE_REACTIVATED,
    ACTION_QUALIFICATION_APPROVED,
    ACTION_QUALIFICATION_REVOKED,
    ACTION_SCHEDULE_CREATED,
    ACTION_SCHEDULE_VERSION_CREATED,
    ACTION_SCHEDULE_VERSION_FINALIZED,
    ACTION_SAME_DATE_EXCLUSION_CLEARED,
    ACTION_SAME_DATE_EXCLUSION_RECORDED,
    ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW,
    ACTION_SCHEDULING_PERIOD_CREATED,
    ACTION_STAFFING_REQUIREMENT_CHANGED,
    ACTION_STAFFING_REQUIREMENT_CREATED,
    ACTION_STAFFING_REQUIREMENT_REMOVED,
    ACTION_SUPPORT_REQUIREMENT_CHANGED,
    ACTION_SUPPORT_REQUIREMENT_CLEARED,
    ACTION_SUPPORT_REQUIREMENT_RECORDED,
    record_audit_event,
)
from app.services.assignment import assign_member, remove_assignment
from app.services.assignment_carry_forward import carry_forward_assignment
from app.services.authorization import (
    can_operate_ministry,
    require_active_admin,
    require_ministry_operator,
    require_ministry_reader,
    require_people_directory_reader,
)
from app.services.availability import (
    EventAvailability,
    MembershipAvailability,
    list_event_availability,
    set_availability,
)
from app.services.errors import (
    AuthorizationError,
    InvalidOperationError,
    ServiceError,
)
from app.services.existing_commitment import (
    remove_existing_commitment,
    set_existing_commitment,
)
from app.services.finalization_readiness import (
    FinalizationIssue,
    FinalizationReadinessResult,
    get_finalization_readiness,
)
from app.services.member_group import (
    MEMBER_GROUP_EVENT_LIMIT_CONFLICT,
    MemberGroupCapConfig,
    MemberGroupSummary,
    PeriodMemberGroupLimits,
    create_member_group,
    list_member_groups,
    list_period_member_group_limits,
    load_member_group_caps,
    set_member_group_event_limit,
    set_member_group_membership,
)
from app.services.ministry_authority import (
    grant_ministry_head,
    revoke_ministry_head,
)
from app.services.ministry_role import (
    create_ministry_role,
    deactivate_ministry_role,
    list_ministry_roles,
    reactivate_ministry_role,
    update_ministry_role,
)
from app.services.role_qualification import (
    MembershipQualification,
    RoleQualifications,
    list_role_qualifications,
    set_role_qualification,
)
from app.services.same_date_exclusion import (
    SAME_DATE_LINKED_MEMBER_CONFLICT,
    clear_same_date_exclusion,
    get_linked_membership_ids,
    get_same_date_exclusion_pairs,
    set_same_date_exclusion,
)
from app.services.same_event_support import (
    SAME_EVENT_SUPPORT_CONFLICT,
    PeriodSupportRequirements,
    SupportRequirementConfig,
    clear_same_event_support_requirement,
    list_support_requirements,
    load_support_requirements,
    set_same_event_support_requirement,
)
from app.services.schedule_generation import (
    DraftGenerationResult,
    generate_draft_schedule,
)
from app.services.schedule_lifecycle import (
    finalize_schedule_version,
    submit_schedule_version_for_review,
)
from app.services.schedule_staleness import (
    RequirementFingerprint,
    ScheduleVersionStalenessResult,
    get_schedule_version_staleness,
)
from app.services.scheduling_input_builder import build_scheduling_input
from app.services.schedule_version import (
    create_initial_schedule_version,
    create_successor_schedule_version,
)
from app.services.scheduling_period import (
    EventSummary,
    create_scheduling_period,
    generate_sunday_events,
    list_period_events,
    lock_availability,
)
from app.services.staffing_requirement import (
    EventStaffing,
    RoleStaffing,
    list_event_staffing_requirements,
    set_staffing_requirement,
)
from app.services.sunday_conflict import SundayConflictResult, get_person_sunday_conflicts

__all__ = [
    # errors
    "AuthorizationError",
    "InvalidOperationError",
    "ServiceError",
    # authorization
    "can_operate_ministry",
    "require_active_admin",
    "require_ministry_operator",
    "require_ministry_reader",
    "require_people_directory_reader",
    # audit
    "ACTION_ASSIGNMENT_ADDED",
    "ACTION_ASSIGNMENT_OVERRIDE_APPLIED",
    "ACTION_ASSIGNMENT_REMOVED",
    "ACTION_AVAILABILITY_CHANGED",
    "ACTION_AVAILABILITY_CLEARED",
    "ACTION_AVAILABILITY_LOCKED",
    "ACTION_AVAILABILITY_RECORDED",
    "ACTION_EVENT_CREATED",
    "ACTION_EXISTING_COMMITMENT_CHANGED",
    "ACTION_EXISTING_COMMITMENT_RECORDED",
    "ACTION_EXISTING_COMMITMENT_REMOVED",
    "ACTION_MEMBER_GROUP_CREATED",
    "ACTION_MEMBER_GROUP_LIMIT_CHANGED",
    "ACTION_MEMBER_GROUP_LIMIT_CLEARED",
    "ACTION_MEMBER_GROUP_LIMIT_RECORDED",
    "ACTION_MEMBER_GROUP_MEMBER_ADDED",
    "ACTION_MEMBER_GROUP_MEMBER_REMOVED",
    "ACTION_MINISTRY_HEAD_GRANTED",
    "ACTION_MINISTRY_HEAD_REVOKED",
    "ACTION_MINISTRY_ROLE_CHANGED",
    "ACTION_MINISTRY_ROLE_CREATED",
    "ACTION_MINISTRY_ROLE_DEACTIVATED",
    "ACTION_MINISTRY_ROLE_REACTIVATED",
    "ACTION_QUALIFICATION_APPROVED",
    "ACTION_QUALIFICATION_REVOKED",
    "ACTION_SCHEDULE_CREATED",
    "ACTION_SCHEDULE_VERSION_CREATED",
    "ACTION_SCHEDULE_VERSION_FINALIZED",
    "ACTION_SAME_DATE_EXCLUSION_CLEARED",
    "ACTION_SAME_DATE_EXCLUSION_RECORDED",
    "ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW",
    "ACTION_SCHEDULING_PERIOD_CREATED",
    "ACTION_STAFFING_REQUIREMENT_CHANGED",
    "ACTION_STAFFING_REQUIREMENT_CREATED",
    "ACTION_STAFFING_REQUIREMENT_REMOVED",
    "ACTION_SUPPORT_REQUIREMENT_CHANGED",
    "ACTION_SUPPORT_REQUIREMENT_CLEARED",
    "ACTION_SUPPORT_REQUIREMENT_RECORDED",
    "record_audit_event",
    # ministry authority
    "grant_ministry_head",
    "revoke_ministry_head",
    # ministry role
    "create_ministry_role",
    "deactivate_ministry_role",
    "list_ministry_roles",
    "reactivate_ministry_role",
    "update_ministry_role",
    # role qualification
    "MembershipQualification",
    "RoleQualifications",
    "list_role_qualifications",
    "set_role_qualification",
    # member groups and their per-event caps (Task 74)
    "MEMBER_GROUP_EVENT_LIMIT_CONFLICT",
    "MemberGroupCapConfig",
    "MemberGroupSummary",
    "PeriodMemberGroupLimits",
    "create_member_group",
    "list_member_groups",
    "list_period_member_group_limits",
    "load_member_group_caps",
    "set_member_group_event_limit",
    "set_member_group_membership",
    # same-event support requirements (Task 74)
    "SAME_EVENT_SUPPORT_CONFLICT",
    "PeriodSupportRequirements",
    "SupportRequirementConfig",
    "clear_same_event_support_requirement",
    "list_support_requirements",
    "load_support_requirements",
    "set_same_event_support_requirement",
    # linked-pair same-date exclusion
    "SAME_DATE_LINKED_MEMBER_CONFLICT",
    "clear_same_date_exclusion",
    "get_linked_membership_ids",
    "get_same_date_exclusion_pairs",
    "set_same_date_exclusion",
    # schedule version
    "create_initial_schedule_version",
    "create_successor_schedule_version",
    # schedule generation
    "DraftGenerationResult",
    "generate_draft_schedule",
    # schedule lifecycle
    "finalize_schedule_version",
    "submit_schedule_version_for_review",
    # finalization readiness
    "FinalizationIssue",
    "FinalizationReadinessResult",
    "get_finalization_readiness",
    # schedule staleness
    "RequirementFingerprint",
    "ScheduleVersionStalenessResult",
    "get_schedule_version_staleness",
    # scheduling input (solver)
    "build_scheduling_input",
    # scheduling period
    "EventSummary",
    "create_scheduling_period",
    "generate_sunday_events",
    "list_period_events",
    "lock_availability",
    # staffing requirement
    "EventStaffing",
    "RoleStaffing",
    "list_event_staffing_requirements",
    "set_staffing_requirement",
    # availability
    "EventAvailability",
    "MembershipAvailability",
    "list_event_availability",
    "set_availability",
    # existing commitment
    "set_existing_commitment",
    "remove_existing_commitment",
    # sunday conflict
    "SundayConflictResult",
    "get_person_sunday_conflicts",
    # assignment
    "assign_member",
    "remove_assignment",
    "carry_forward_assignment",
]
