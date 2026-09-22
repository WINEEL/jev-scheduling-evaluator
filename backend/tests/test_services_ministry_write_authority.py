"""Every authorization check in the service layer, pinned (Task 80).

**Why this file exists.** Task 79 narrowed people and membership management to
the active Head of a ministry and left every service written before it on the
wider rule, where ``is_admin=True`` alone conferred an operational write. Task
80 finished that: reads take
:func:`~app.services.authorization.require_ministry_reader`, writes take
:func:`~app.services.authorization.require_ministry_operator`, and the two are
never interchangeable. That is a decision about the whole product's write
surface, and a decision that large has to be *enforced* rather than remembered
-- one new ``set_...`` function reaching for the reader rule because it was
copied from the ``list_...`` beside it would reopen exactly the hole this task
closed, silently, with every test still green.

So the table below is the audit, as data: every service function that
authorizes anything, and which rule it uses. The test does three things with
it, and the third is the one that matters most:

1. **Every entry still holds** -- the named function still calls the named
   rule.
2. **The table is complete** -- no function authorizes anything without being
   listed, so a new endpoint cannot arrive unreviewed.
3. **No ministry-scoped write uses the read rule**, checked independently of
   the table by looking at what each function *does*.

Parsed, not executed: this is about which check each function contains, which
is a static fact. What those checks *decide* is
``test_services_authorization.py``'s subject, and what each service does once
authorized is its own module's.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SERVICES = pathlib.Path("app/services")

READER = "require_ministry_reader"
OPERATOR = "require_ministry_operator"
ADMIN = "require_active_admin"
DIRECTORY = "require_people_directory_reader"

#: (module, function) -> the authorization rules its body contains.
#:
#: Grouped by the families Task 80 §3 named. A function appearing with two
#: rules genuinely branches between them, and the comment says why.
EXPECTED: dict[tuple[str, str], tuple[str, ...]] = {
    # -- Roles ------------------------------------------------------------
    ("ministry_role", "list_ministry_roles"): (READER,),
    ("ministry_role", "create_ministry_role"): (OPERATOR,),
    ("ministry_role", "update_ministry_role"): (OPERATOR,),
    ("ministry_role", "deactivate_ministry_role"): (OPERATOR,),
    ("ministry_role", "reactivate_ministry_role"): (OPERATOR,),
    # -- Qualifications ---------------------------------------------------
    ("role_qualification", "list_role_qualifications"): (READER,),
    ("role_qualification", "set_role_qualification"): (OPERATOR,),
    # -- Staffing requirements --------------------------------------------
    ("staffing_requirement", "list_event_staffing_requirements"): (READER,),
    ("staffing_requirement", "set_staffing_requirement"): (OPERATOR,),
    # -- Availability, and the lock ---------------------------------------
    ("availability", "list_event_availability"): (READER,),
    ("availability", "set_availability"): (OPERATOR,),
    ("scheduling_period", "lock_availability"): (OPERATOR,),
    # -- Periods and their events -----------------------------------------
    ("scheduling_period", "create_scheduling_period"): (OPERATOR,),
    ("scheduling_period", "generate_sunday_events"): (OPERATOR,),
    ("scheduling_period", "list_period_events"): (READER,),
    ("schedule_entry", "list_ministry_scheduling_periods"): (READER,),
    # -- Serving limits ----------------------------------------------------
    ("serving_limit", "list_serving_limits"): (READER,),
    ("serving_limit", "set_serving_limit"): (OPERATOR,),
    # -- Min intervening events -------------------------------------------
    ("event_gap", "get_event_gap_rule"): (READER,),
    ("event_gap", "set_min_intervening_events"): (OPERATOR,),
    # -- Member groups and their per-event limits -------------------------
    ("member_group", "list_member_groups"): (READER,),
    ("member_group", "list_period_member_group_limits"): (READER,),
    ("member_group", "create_member_group"): (OPERATOR,),
    ("member_group", "set_member_group_membership"): (OPERATOR,),
    ("member_group", "set_member_group_event_limit"): (OPERATOR,),
    # -- Same-date exclusions ---------------------------------------------
    ("same_date_exclusion", "set_same_date_exclusion"): (OPERATOR,),
    ("same_date_exclusion", "clear_same_date_exclusion"): (OPERATOR,),
    # -- Same-event support -------------------------------------------------
    ("same_event_support", "list_support_requirements"): (READER,),
    ("same_event_support", "set_same_event_support_requirement"): (OPERATOR,),
    ("same_event_support", "clear_same_event_support_requirement"): (OPERATOR,),
    # -- Schedule versions, generation, manual assignment ------------------
    ("schedule_version_detail", "get_schedule_version_detail"): (READER,),
    ("schedule_version", "create_initial_schedule_version"): (OPERATOR,),
    ("schedule_version", "create_successor_schedule_version"): (OPERATOR,),
    ("schedule_generation", "generate_draft_schedule"): (OPERATOR,),
    # Development-only (Task 81), and listed here for exactly that reason: it
    # writes assignments, so it authorizes as an operator -- once per ministry
    # in the joined run, and all of them before any solving begins. Being
    # unreachable from the API is not a reason to leave a write unaudited.
    ("joined_schedule_generation", "generate_joined_draft_schedules"): (OPERATOR,),
    ("generated_assignment", "persist_generated_assignments"): (OPERATOR,),
    ("assignment", "assign_member"): (OPERATOR,),
    ("assignment", "remove_assignment"): (OPERATOR,),
    # -- The lifecycle itself ----------------------------------------------
    ("schedule_lifecycle", "submit_schedule_version_for_review"): (OPERATOR,),
    ("schedule_lifecycle", "finalize_schedule_version"): (OPERATOR,),
    # -- Existing commitments ----------------------------------------------
    # Branches on provenance: a commitment naming a source ministry belongs to
    # that ministry's head; one with no source ministry has no ministry to
    # derive authority from and is Admin-only.
    ("existing_commitment", "_require_authorized_actor"): (ADMIN, OPERATOR),
    ("existing_commitment", "remove_existing_commitment"): (ADMIN, OPERATOR),
    # -- Church-wide governance (Task 79, unchanged by Task 80) ------------
    ("ministry_authority", "grant_ministry_head"): (ADMIN,),
    ("ministry_authority", "revoke_ministry_head"): (ADMIN,),
    ("ministry_directory", "list_church_ministries"): (ADMIN,),
    ("ministry_membership", "add_person_to_ministry"): (OPERATOR,),
    ("ministry_membership", "update_membership"): (OPERATOR,),
    # Removing somebody who leads the ministry revokes head authority, which
    # is Admin-only however it is reached.
    ("ministry_membership", "remove_person_from_ministry"): (ADMIN, OPERATOR),
    ("ministry_membership", "set_ministry_head_authority"): (ADMIN,),
    ("person_directory", "list_people"): (DIRECTORY,),
    ("person_directory", "read_person"): (DIRECTORY,),
    ("person_directory", "list_person_memberships"): (DIRECTORY,),
    ("person_directory", "update_person"): (ADMIN,),
    ("person_directory", "deactivate_person"): (ADMIN,),
    ("person_directory", "reactivate_person"): (ADMIN,),
    ("person_directory", "set_person_auth_link"): (ADMIN,),
    ("person_directory", "set_church_membership_status"): (ADMIN,),
    # Creating an unattached Person is Admin-only; creating one straight into
    # a ministry is that ministry's head's act.
    ("person_directory", "_authorize_creation"): (ADMIN, OPERATOR),
}

#: The functions whose names announce a write. Used by the independent check
#: below, which does not consult :data:`EXPECTED` at all.
_WRITE_PREFIXES = (
    "set_",
    "create_",
    "update_",
    "clear_",
    "remove_",
    "add_",
    "deactivate_",
    "reactivate_",
    "generate_",
    "lock_",
    "persist_",
    "assign_",
    "grant_",
    "revoke_",
    "submit_",
    "finalize_",
    "carry_",
)

_ALL_RULES = (READER, OPERATOR, ADMIN, DIRECTORY)


def _rules_in(node: ast.FunctionDef) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in _ALL_RULES
            }
        )
    )


def _authorizing_functions() -> dict[tuple[str, str], tuple[str, ...]]:
    """Every top-level service function that contains an authorization call.

    Top-level only: a nested helper is reached through the function that
    defines it, which is already listed.
    """
    found: dict[tuple[str, str], tuple[str, ...]] = {}
    for path in sorted(SERVICES.glob("*.py")):
        if path.name in {"authorization.py", "__init__.py"}:
            continue
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.FunctionDef):
                continue
            rules = _rules_in(node)
            if rules:
                found[(path.stem, node.name)] = rules
    return found


ACTUAL = _authorizing_functions()


# ==========================================================================
# 1. Every entry still holds
# ==========================================================================


@pytest.mark.parametrize(
    ("key", "expected"),
    [(key, value) for key, value in sorted(EXPECTED.items())],
    ids=[f"{module}.{function}" for module, function in sorted(EXPECTED)],
)
def test_each_service_uses_the_rule_it_was_reviewed_with(key, expected):
    module, function = key
    assert key in ACTUAL, f"{module}.{function} no longer authorizes anything"
    assert ACTUAL[key] == tuple(sorted(expected)), (
        f"{module}.{function} changed which authorization rule it applies;"
        " if that is intended, the change is a governance decision and this"
        " table is where it is recorded"
    )


# ==========================================================================
# 2. The table is complete
# ==========================================================================


def test_no_service_authorizes_anything_unlisted():
    """A new endpoint cannot arrive without appearing in the audit above.

    This is the half that makes the table worth having: asserting the listed
    entries alone would let a new write be added beside them with the wrong
    rule and no test would notice.
    """
    unlisted = sorted(set(ACTUAL) - set(EXPECTED))

    assert unlisted == [], (
        "these service functions authorize something and are not in the"
        f" Task 80 audit table: {unlisted}"
    )


# ==========================================================================
# 3. No ministry write accepts the read rule -- checked without the table
# ==========================================================================


def test_no_write_shaped_function_uses_the_read_rule():
    """Derived from what the functions are called, not from :data:`EXPECTED`.

    Two independent statements of the same rule, so a mistaken edit has to be
    made twice to go unnoticed: the table could be updated to match a wrong
    change, and this could not, because it consults only the function's own
    name and body.
    """
    offenders = [
        f"{module}.{function}"
        for (module, function), rules in sorted(ACTUAL.items())
        if function.lstrip("_").startswith(_WRITE_PREFIXES) and READER in rules
    ]

    assert offenders == [], (
        "a ministry write must take require_ministry_operator; these take the"
        f" oversight read rule instead: {offenders}"
    )


def test_no_list_or_get_function_uses_the_write_rule():
    """The converse, and not a symmetry for its own sake: a read that demanded
    the operator rule would hide a ministry from the Admin overseeing it, which
    is the other half of core §4.3.1 and just as wrong.
    """
    offenders = [
        f"{module}.{function}"
        for (module, function), rules in sorted(ACTUAL.items())
        if function.lstrip("_").startswith(("list_", "get_", "read_"))
        and OPERATOR in rules
    ]

    assert offenders == [], (
        "an oversight read must take require_ministry_reader; these demand"
        f" operational authority: {offenders}"
    )


# ==========================================================================
# 4. The wider rule is gone, not merely unused
# ==========================================================================


def test_the_old_wider_rule_no_longer_exists():
    """``require_ministry_manager`` is not deprecated, it is removed.

    Leaving it in place would leave the hole reachable: the name reads like
    the right thing to call from a write, which is exactly how it came to
    guard eighteen of them.
    """
    import app.services.authorization as authorization

    assert not hasattr(authorization, "require_ministry_manager")

    # By name node, not by text search: the module docstring explains where
    # the old rule went and why, and that history is worth keeping.
    for path in sorted(SERVICES.glob("*.py")):
        names = {
            node.id
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Name)
        } | {
            alias.name
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        assert "require_ministry_manager" not in names, path.name


def test_the_operator_rule_has_no_admin_branch():
    """Pinned here as well as in ``test_services_authorization.py``, because
    this file is where somebody would come looking to loosen it in a hurry.
    """
    import inspect

    from app.services.authorization import require_ministry_operator

    body = inspect.getsource(require_ministry_operator).split('"""')[2]

    assert "is_admin" not in body
