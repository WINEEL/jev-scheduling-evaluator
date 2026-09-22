"""The bounded override-blocker vocabulary, and nothing else.

Task 22 defined five checks a manual ``override_reason`` might bypass and named
each with a short code. **Four of them still may.** The fifth -- the church-wide
same-Sunday cross-ministry conflict -- was made absolute in Task 79's final
correction: one Person serves at most one ministry per Sunday is a hard rule of
this church, and a rule that a reason can talk its way past is not hard. Its
code remains defined and recognised here, because it is persisted history; what
it no longer does is authorize anything. Those codes were private to
:mod:`app.services.assignment` while it was their only user. They are no
longer private state: :mod:`app.services.assignment_rules` **writes them into
AuditEvent history** (``after_values["overridden_blockers"]``) on behalf of
both assignment writers, and :mod:`app.services.finalization_readiness` reads
those stored values back to decide whether a current blocker was ever
authorized. Two genuine consumers of one persisted vocabulary is the point at
which a shared definition stops being premature.

**These strings are persisted history and must never be renamed.** Audit rows
written before any rename would silently stop matching, and an assignment
whose override was properly authorized would start reading as unauthorized --
a change in meaning, not a refactor. Adding a code is a product decision that
belongs with the check that raises it.

This module is deliberately **constants and descriptions only**. No policy
objects, no rule registry, no evaluation engine: the *checks* stay with the
operation that performs them, because when a rule is violated depends on what
that operation is doing. Assignment evaluates them against a proposed row and
refuses or overrides; finalization readiness evaluates the current world
against history and reports. Sharing the evaluation would force those two
different questions into one shape.

Task 63 did make the *assignment* side's evaluation shared, in
:mod:`app.services.assignment_rules` -- but only between manual assignment and
generated-schedule persistence, which ask the identical question about the
identical proposed row. That is the case this paragraph always allowed for;
readiness still has its own, because it is asking something else.
"""

from __future__ import annotations

#: The role the requirement snapshot names has been deactivated since.
BLOCKER_ROLE_DEACTIVATED = "role_deactivated"
#: No RoleQualification row, or one with ``is_qualified=False``. Deliberately
#: one code for both: core §8 keeps "never assessed" and "assessed no"
#: distinct in the *data*, but assignment treats them as the same scheduling
#: outcome, and finalization must not invent a distinction Task 22 never made.
BLOCKER_NOT_QUALIFIED = "not_qualified"
#: An explicit ``UNAVAILABLE`` Availability row. A missing row is "no
#: response" and is not a blocker.
BLOCKER_UNAVAILABLE = "unavailable"
#: A church-wide cross-ministry conflict (ADR 0002/0003) on the requirement's
#: **snapshot** date: this Person is already committed to a *different*
#: ministry that day.
#:
#: **No longer overridable, and this is the correction Task 79's review
#: required.** One Person serves at most one ministry per Sunday is a hard
#: church-wide rule, not a fact about the world a head may judge differently
#: on one Sunday -- so it left :data:`OVERRIDABLE_BLOCKERS` and became an
#: absolute rule in :func:`app.services.assignment_rules.require_absolute_rules`
#: and a standalone gate in :mod:`app.services.finalization_readiness`.
#:
#: **The string survives, and must.** Audit rows written while it *was*
#: overridable still carry it, and a payload containing it has to keep reading
#: as a well-formed record of what somebody approved at the time -- see
#: :data:`KNOWN_BLOCKERS`. What changed is that it authorizes nothing: the
#: conflict is now refused before any override is consulted, so no reading of
#: any audit row can let it through.
BLOCKER_SUNDAY_CONFLICT = "sunday_conflict"
#: The requirement is already staffed to its ``required_count``.
BLOCKER_CAPACITY_FULL = "capacity_full"

#: The codes an ``override_reason`` may actually authorize. **Four, since the
#: same-Sunday cross-ministry conflict was made absolute.**
#:
#: This set is what a stored payload is *intersected with* before it is allowed
#: to excuse anything, so a historical row naming ``sunday_conflict`` excuses
#: exactly the other blockers it names and no more.
OVERRIDABLE_BLOCKERS = frozenset(
    {
        BLOCKER_ROLE_DEACTIVATED,
        BLOCKER_NOT_QUALIFIED,
        BLOCKER_UNAVAILABLE,
        BLOCKER_CAPACITY_FULL,
    }
)

#: Every code this vocabulary has ever contained, for **validating** payloads
#: read back out of audit history. A stored value outside this set is not an
#: unknown-but-tolerable code: Task 22's writers only ever wrote these, so
#: anything else means the payload is corrupt and authorizes nothing.
#:
#: **Wider than** :data:`OVERRIDABLE_BLOCKERS` **on purpose.** Validation asks
#: "is this record intact?" and authorization asks "what does it permit?".
#: Collapsing them would make every override written before the Sunday rule was
#: locked read as *corrupt* rather than as a truthful record of a decision that
#: is no longer allowed -- and would report the wrong problem to whoever has to
#: fix the schedule.
KNOWN_BLOCKERS = OVERRIDABLE_BLOCKERS | {BLOCKER_SUNDAY_CONFLICT}

BLOCKER_DESCRIPTIONS = {
    BLOCKER_ROLE_DEACTIVATED: "the role has been deactivated since this version was created",
    BLOCKER_NOT_QUALIFIED: "the member is not currently qualified for this role",
    BLOCKER_UNAVAILABLE: "the member is marked unavailable for this event",
    BLOCKER_SUNDAY_CONFLICT: "the member has a church-wide Sunday conflict on this date",
    BLOCKER_CAPACITY_FULL: "this requirement is already fully staffed",
}
