"""ORM models.

Importing this package registers every mapped class against ``app.db.Base``, so
``Base.metadata`` is complete afterwards. Alembic imports this package
explicitly (see ``alembic/env.py``) rather than relying on an API or service
module happening to pull the models in as a side effect.

Four slices so far:

- :mod:`app.models.core` — church-wide identity and ministry structure.
- :mod:`app.models.scheduling_input` — the solver's input concepts.
- :mod:`app.models.schedule_output` — the schedule itself and its versions.
- :mod:`app.models.audit` — the history of who changed what, and why.

Later slices (constraint/preference configuration, shadow assignments) get their
own modules alongside these and are re-exported here in the same way.
"""

from app.models.audit import (
    AUDIT_ACTION_PATTERN,
    AUDIT_ACTOR_TYPE_PERSON,
    AUDIT_ACTOR_TYPE_SYSTEM,
    AUDIT_TARGET_TABLES,
    AuditEvent,
)
from app.models.core import (
    Church,
    Ministry,
    MinistryMembership,
    MinistryRole,
    Person,
    RoleQualification,
    UserAccount,
)
from app.models.schedule_output import (
    SCHEDULE_VERSION_STATUS_DRAFT,
    SCHEDULE_VERSION_STATUS_FINALIZED,
    SCHEDULE_VERSION_STATUS_REVIEW,
    Assignment,
    Schedule,
    ScheduleVersion,
    ScheduleVersionRequirement,
)
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_BACKUP,
    AVAILABILITY_UNAVAILABLE,
    EVENT_KIND_SPECIAL,
    EVENT_KIND_SUNDAY_SERVICE,
    Availability,
    Event,
    ExistingCommitment,
    MemberGroup,
    MemberGroupEventLimit,
    MemberGroupMember,
    MembershipSameDateExclusion,
    MembershipServingLimit,
    MembershipSupportRequirement,
    MembershipSupportSupporter,
    SchedulingPeriod,
    StaffingRequirement,
)

__all__ = [
    # audit
    "AuditEvent",
    # audit value sets
    "AUDIT_ACTION_PATTERN",
    "AUDIT_ACTOR_TYPE_PERSON",
    "AUDIT_ACTOR_TYPE_SYSTEM",
    "AUDIT_TARGET_TABLES",
    # core
    "Church",
    "Ministry",
    "MinistryMembership",
    "MinistryRole",
    "Person",
    "RoleQualification",
    "UserAccount",
    # scheduling input
    "Availability",
    "MemberGroup",
    "MemberGroupEventLimit",
    "MemberGroupMember",
    "MembershipSameDateExclusion",
    "MembershipServingLimit",
    "MembershipSupportRequirement",
    "MembershipSupportSupporter",
    "Event",
    "ExistingCommitment",
    "SchedulingPeriod",
    "StaffingRequirement",
    # scheduling input value sets
    "AVAILABILITY_AVAILABLE",
    "AVAILABILITY_BACKUP",
    "AVAILABILITY_UNAVAILABLE",
    "EVENT_KIND_SPECIAL",
    "EVENT_KIND_SUNDAY_SERVICE",
    # schedule output
    "Assignment",
    "Schedule",
    "ScheduleVersion",
    "ScheduleVersionRequirement",
    # schedule output value sets
    "SCHEDULE_VERSION_STATUS_DRAFT",
    "SCHEDULE_VERSION_STATUS_FINALIZED",
    "SCHEDULE_VERSION_STATUS_REVIEW",
]
