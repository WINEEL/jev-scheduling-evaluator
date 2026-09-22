"""Recording audit events.

Implements the write side of ``docs/architecture/audit-event-data-model.md``
(revision 2). Section numbers in the comments refer to that document.

This module is **one function and a vocabulary**, not a framework:

- It does not introspect ORM objects. Callers pass the specific changed fields
  they want recorded, because only the caller knows which fields are the
  *business* change and which are bookkeeping (§7.2).
- It registers no ORM event listeners and hooks no flush. Audit rows are written
  explicitly by the domain operation that knows the actor, the action and the
  reason -- a listener could know none of those, and would fire during
  migrations and fixtures (§12).
- **It never commits, flushes or rolls back.** See :mod:`app.services` for the
  transaction convention.

It is emphatically not event sourcing (§3): the domain tables remain the
authoritative current state, and nothing reads these rows back to reconstruct
one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import (
    AUDIT_ACTION_PATTERN,
    AUDIT_ACTOR_TYPE_PERSON,
    AUDIT_ACTOR_TYPE_SYSTEM,
    AUDIT_TARGET_TABLES,
    AuditEvent,
)
from app.models.core import Person
from app.services.errors import InvalidOperationError

#: The centralized action vocabulary (§6.2).
#:
#: Action names live here so a typo is an ImportError rather than a new de-facto
#: action name spreading silently through service code. New actions are added
#: here as the services that raise them are built -- the database constrains the
#: *shape* of an action name, deliberately not the set of them, so adding one
#: never needs a migration.
ACTION_MINISTRY_HEAD_GRANTED = "MINISTRY_HEAD_GRANTED"
ACTION_MINISTRY_HEAD_REVOKED = "MINISTRY_HEAD_REVOKED"
#: The church-wide Person record and the ministry memberships hanging off it
#: (Task 79). Seven actions, and the split between the two families is the
#: point: ``PERSON_*`` is church-wide and Admin-only to write,
#: ``MINISTRY_MEMBERSHIP_*`` is scoped to one ministry and writable by that
#: ministry's Head. A reader scanning a history must be able to tell "left the
#: church" from "left the AV team" without opening the payload, and two target
#: tables plus two action families is what makes that true.
#:
#: There is deliberately no ``PERSON_DELETED``. Nothing in this application
#: deletes a Person, so an action name for it would describe an act that cannot
#: happen (core section 6).
ACTION_PERSON_CREATED = "PERSON_CREATED"
ACTION_PERSON_UPDATED = "PERSON_UPDATED"
ACTION_PERSON_DEACTIVATED = "PERSON_DEACTIVATED"
ACTION_PERSON_REACTIVATED = "PERSON_REACTIVATED"
#: Linking, replacing or removing the address a Person signs in with. One
#: action for all three, told apart by the summary and by the payload's
#: ``email_linked`` boolean -- **never by the address itself, which is
#: deliberately not recorded**. The current address is always readable on the
#: Person row; copying it into an immutable, permanently retained audit trail
#: would spread contact PII into a table that exists to explain authority
#: changes (audit section 7.2 keeps a payload to the changed *business* fact).
ACTION_PERSON_AUTH_LINK_CHANGED = "PERSON_AUTH_LINK_CHANGED"
#: An Admin stating whether somebody is a formal member of the church
#: (Task 79 §6). **A separate action from ``PERSON_UPDATED``**, because it is a
#: separate decision with a separate authority: editing a phone number is
#: clerical, and recording that somebody is or is not a member of this church
#: is governance. A reader scanning the history for the second must not have to
#: open every instance of the first.
#:
#: Never inferred from ministry participation, serving history or anything
#: else -- so every one of these rows is a person having said so, and the
#: payload carries the two statuses and nothing more.
ACTION_PERSON_CHURCH_MEMBERSHIP_STATUS_CHANGED = (
    "PERSON_CHURCH_MEMBERSHIP_STATUS_CHANGED"
)
#: Joining and leaving one ministry. ``ADDED`` covers both a brand-new
#: membership row and the reactivation of a previous one -- they are the same
#: act to a reader ("they are on the team again"), and the unique constraint on
#: ``(person_id, ministry_id)`` makes the second the only way somebody can
#: rejoin. ``CHANGED`` is the ministry-specific participation detail (notes,
#: joined_on), which is neither a join nor a leave.
ACTION_MINISTRY_MEMBERSHIP_ADDED = "MINISTRY_MEMBERSHIP_ADDED"
ACTION_MINISTRY_MEMBERSHIP_CHANGED = "MINISTRY_MEMBERSHIP_CHANGED"
ACTION_MINISTRY_MEMBERSHIP_REMOVED = "MINISTRY_MEMBERSHIP_REMOVED"
#: ``QUALIFICATION_REVOKED`` is used for every result of ``False`` -- both an
#: actual revocation of a prior ``True`` and a first-time explicit "not
#: qualified" -- told apart by the summary sentence
#: (:mod:`app.services.role_qualification`), not by a third action name.
ACTION_QUALIFICATION_APPROVED = "QUALIFICATION_APPROVED"
ACTION_QUALIFICATION_REVOKED = "QUALIFICATION_REVOKED"
ACTION_SCHEDULING_PERIOD_CREATED = "SCHEDULING_PERIOD_CREATED"
#: A ministry's own role definition (Task 53) -- distinct from
#: ``QUALIFICATION_*``, which is a membership's standing approval *for* a
#: role, not the role itself. Four actions, matching the shape
#: ``SERVING_LIMIT_*`` and ``SAME_DATE_EXCLUSION_*`` already use: creation,
#: an edit to name/description, and the two ends of the soft-deactivation
#: toggle are four different things to read in a history.
ACTION_MINISTRY_ROLE_CREATED = "MINISTRY_ROLE_CREATED"
ACTION_MINISTRY_ROLE_CHANGED = "MINISTRY_ROLE_CHANGED"
ACTION_MINISTRY_ROLE_DEACTIVATED = "MINISTRY_ROLE_DEACTIVATED"
ACTION_MINISTRY_ROLE_REACTIVATED = "MINISTRY_ROLE_REACTIVATED"
ACTION_EVENT_CREATED = "EVENT_CREATED"
ACTION_STAFFING_REQUIREMENT_CREATED = "STAFFING_REQUIREMENT_CREATED"
ACTION_STAFFING_REQUIREMENT_CHANGED = "STAFFING_REQUIREMENT_CHANGED"
ACTION_STAFFING_REQUIREMENT_REMOVED = "STAFFING_REQUIREMENT_REMOVED"
ACTION_AVAILABILITY_RECORDED = "AVAILABILITY_RECORDED"
ACTION_AVAILABILITY_CHANGED = "AVAILABILITY_CHANGED"
ACTION_AVAILABILITY_CLEARED = "AVAILABILITY_CLEARED"
ACTION_EXISTING_COMMITMENT_RECORDED = "EXISTING_COMMITMENT_RECORDED"
ACTION_EXISTING_COMMITMENT_CHANGED = "EXISTING_COMMITMENT_CHANGED"
ACTION_EXISTING_COMMITMENT_REMOVED = "EXISTING_COMMITMENT_REMOVED"
ACTION_AVAILABILITY_LOCKED = "AVAILABILITY_LOCKED"
#: A person-specific serving maximum for one membership in one scheduling
#: period (requirements §4.4.1). Three actions, not one with a nullable value:
#: recording a limit somebody did not have, revising one, and removing it
#: entirely are three different things to read in a history, and the third is
#: the one that restores "no maximum".
ACTION_SERVING_LIMIT_RECORDED = "SERVING_LIMIT_RECORDED"
ACTION_SERVING_LIMIT_CHANGED = "SERVING_LIMIT_CHANGED"
ACTION_SERVING_LIMIT_CLEARED = "SERVING_LIMIT_CLEARED"
#: A linked-pair same-date exclusion for two memberships in one scheduling
#: period (requirements §4.4.2). Two actions, not three: the rule has no value
#: to revise -- it exists or it does not -- so there is nothing a CHANGED
#: action could describe that RECORDED and CLEARED do not already say.
ACTION_SAME_DATE_EXCLUSION_RECORDED = "SAME_DATE_EXCLUSION_RECORDED"
ACTION_SAME_DATE_EXCLUSION_CLEARED = "SAME_DATE_EXCLUSION_CLEARED"
#: The ministry- and period-scoped event-gap rule -- how many of a ministry's
#: own events must fall between two assignments of the same person
#: (requirements §4.8). Three actions, matching ``SERVING_LIMIT_*`` rather than
#: the two-action shape of ``SAME_DATE_EXCLUSION_*``: this rule *has* a value
#: to revise, so introducing one, changing it and removing it are three
#: different things to read in a history, and the third is the one that
#: restores "no rule".
ACTION_EVENT_GAP_RULE_RECORDED = "EVENT_GAP_RULE_RECORDED"
ACTION_EVENT_GAP_RULE_CHANGED = "EVENT_GAP_RULE_CHANGED"
ACTION_EVENT_GAP_RULE_CLEARED = "EVENT_GAP_RULE_CLEARED"
#: A ministry's own member group -- a plain named category of its members
#: (Task 74). One action: a group is created, and nothing about it is later
#: revised here. Renaming a category is a different act that does not exist
#: yet, and inventing an action for it would claim a feature that is not built.
ACTION_MEMBER_GROUP_CREATED = "MEMBER_GROUP_CREATED"
#: Who is in one. Two actions, not three, matching ``SAME_DATE_EXCLUSION_*``:
#: group membership has no value to revise -- somebody is in the group or they
#: are not -- so there is nothing a CHANGED action could describe.
ACTION_MEMBER_GROUP_MEMBER_ADDED = "MEMBER_GROUP_MEMBER_ADDED"
ACTION_MEMBER_GROUP_MEMBER_REMOVED = "MEMBER_GROUP_MEMBER_REMOVED"
#: The per-event cap one scheduling period puts on one group (Task 74). Three
#: actions, matching ``SERVING_LIMIT_*`` rather than the two-action shape above:
#: this rule *has* a number to revise, so introducing it, changing it and
#: removing it are three different things to read in a history, and the third
#: is the one that restores "no cap".
ACTION_MEMBER_GROUP_LIMIT_RECORDED = "MEMBER_GROUP_LIMIT_RECORDED"
ACTION_MEMBER_GROUP_LIMIT_CHANGED = "MEMBER_GROUP_LIMIT_CHANGED"
ACTION_MEMBER_GROUP_LIMIT_CLEARED = "MEMBER_GROUP_LIMIT_CLEARED"
#: The per-subject, per-period same-event support requirement (Task 74). Three
#: actions for the same reason: the rule carries both a count and an approved
#: set, and both are revisable. The approved set travels in the payload rather
#: than in audit rows of its own, because it is only ever changed as part of
#: configuring the requirement -- one act, one history row.
ACTION_SUPPORT_REQUIREMENT_RECORDED = "SUPPORT_REQUIREMENT_RECORDED"
ACTION_SUPPORT_REQUIREMENT_CHANGED = "SUPPORT_REQUIREMENT_CHANGED"
ACTION_SUPPORT_REQUIREMENT_CLEARED = "SUPPORT_REQUIREMENT_CLEARED"
ACTION_SCHEDULE_CREATED = "SCHEDULE_CREATED"
ACTION_SCHEDULE_VERSION_CREATED = "SCHEDULE_VERSION_CREATED"
ACTION_ASSIGNMENT_ADDED = "ASSIGNMENT_ADDED"
ACTION_ASSIGNMENT_REMOVED = "ASSIGNMENT_REMOVED"
ACTION_ASSIGNMENT_OVERRIDE_APPLIED = "ASSIGNMENT_OVERRIDE_APPLIED"
ACTION_SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW = "SCHEDULE_VERSION_SUBMITTED_FOR_REVIEW"
ACTION_SCHEDULE_VERSION_FINALIZED = "SCHEDULE_VERSION_FINALIZED"

_ACTION_RE = re.compile(AUDIT_ACTION_PATTERN)


def record_audit_event(
    session: Session,
    *,
    action: str,
    target_table: str,
    target_id: int,
    summary: str,
    actor: Person | None = None,
    system_actor_label: str | None = None,
    ministry_id: int | None = None,
    reason: str | None = None,
    before_values: Mapping[str, Any] | None = None,
    after_values: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Build an :class:`AuditEvent` and add it to ``session``.

    The row joins whatever transaction ``session`` is already in, and is written
    when the caller commits. **This function never commits, flushes or rolls
    back** -- doing so would break the guarantee that a domain change and its
    audit row land together (§12).

    Exactly one actor form must be supplied, mirroring the database's two-way
    invariant (§4.1):

    - ``actor`` -- a Person, producing ``actor_type='PERSON'``. The historical
      ``actor_label`` is that Person's ``display_name``; ``user_account`` is
      never consulted, because it may legitimately be hard-deleted while Person
      may not.
    - ``system_actor_label`` -- a label such as ``"Scheduling engine"``,
      producing ``actor_type='SYSTEM'`` with no ``actor_person_id``. Supported
      because the schema supports it; no service-account infrastructure exists
      or is needed.

    Every value is validated here so a caller receives a meaningful
    :class:`InvalidOperationError` rather than an ``IntegrityError`` from a
    database CHECK at commit time -- by which point the failure is far from its
    cause and has aborted the whole transaction. The checks deliberately mirror
    the constraints in :mod:`app.models.audit` rather than adding rules of their
    own.

    :returns: the pending :class:`AuditEvent`, which has no ``id`` until flush.
    """
    actor_type, actor_person_id, actor_label = _resolve_actor(
        actor, system_actor_label
    )

    if not _ACTION_RE.match(action):
        raise InvalidOperationError(
            f"action {action!r} is not an uppercase snake-case identifier"
        )
    if target_table not in AUDIT_TARGET_TABLES:
        raise InvalidOperationError(f"{target_table!r} is not an audited table")
    # ``None`` is the realistic bad value here, not a negative number: it means
    # the caller passed an unpersisted object whose id has not been assigned.
    # Checked explicitly so that surfaces as a domain error rather than a
    # TypeError from the comparison below.
    if target_id is None or target_id <= 0:
        raise InvalidOperationError(
            f"target_id must be a positive id, got {target_id!r};"
            " the target must be persisted"
        )

    summary = _require_non_blank(summary, "summary")
    reason = _optional_non_blank(reason, "reason")

    before = _validate_payload(before_values, "before_values")
    after = _validate_payload(after_values, "after_values")
    if before is None and after is None:
        # A row recording neither a prior nor a resulting value describes
        # nothing (§4).
        raise InvalidOperationError(
            "at least one of before_values or after_values must be supplied"
        )

    audit_event = AuditEvent(
        actor_type=actor_type,
        actor_person_id=actor_person_id,
        actor_label=actor_label,
        action=action,
        target_table=target_table,
        target_id=target_id,
        ministry_id=ministry_id,
        summary=summary,
        reason=reason,
        before_values=before,
        after_values=after,
    )
    session.add(audit_event)
    return audit_event


def _resolve_actor(
    actor: Person | None, system_actor_label: str | None
) -> tuple[str, int | None, str]:
    """Enforce the database's two-way actor invariant in the service layer."""
    if (actor is None) == (system_actor_label is None):
        raise InvalidOperationError(
            "supply exactly one of actor or system_actor_label"
        )

    if actor is not None:
        if actor.id is None:
            # A pending Person has no id yet, so the foreign key could not be
            # written. Flushing it here would be the caller's decision to make.
            raise InvalidOperationError("actor Person must be persisted (id is None)")
        label = _require_non_blank(actor.display_name, "actor.display_name")
        return AUDIT_ACTOR_TYPE_PERSON, actor.id, label

    return (
        AUDIT_ACTOR_TYPE_SYSTEM,
        None,
        _require_non_blank(system_actor_label, "system_actor_label"),
    )


def _require_non_blank(value: str | None, field: str) -> str:
    if value is None or not value.strip():
        raise InvalidOperationError(f"{field} must not be blank")
    return value


def _optional_non_blank(value: str | None, field: str) -> str | None:
    """``None`` stays ``None``; a supplied value must not be whitespace-only."""
    if value is None:
        return None
    return _require_non_blank(value, field)


def _validate_payload(
    payload: Mapping[str, Any] | None, field: str
) -> dict[str, Any] | None:
    """Payloads are JSON objects, never arrays or bare scalars (§7.2).

    Copied into a plain dict so a caller mutating its own dict afterwards cannot
    change a row that is already pending.
    """
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise InvalidOperationError(f"{field} must be a mapping, got {type(payload).__name__}")
    return dict(payload)
