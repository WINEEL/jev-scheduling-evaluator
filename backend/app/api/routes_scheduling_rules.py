"""A scheduling period's own scheduling rules (Tasks 71 and 74).

``GET    /api/v1/scheduling-periods/{id}/scheduling-rules``
``PUT    /api/v1/scheduling-periods/{id}/scheduling-rules/min-intervening-events``
``DELETE /api/v1/scheduling-periods/{id}/scheduling-rules/min-intervening-events``
``PUT    /api/v1/scheduling-periods/{id}/scheduling-rules/member-group-limits/{member_group_id}``
``DELETE /api/v1/scheduling-periods/{id}/scheduling-rules/member-group-limits/{member_group_id}``
``PUT    /api/v1/scheduling-periods/{id}/scheduling-rules/same-event-support/{subject_membership_id}``
``DELETE /api/v1/scheduling-periods/{id}/scheduling-rules/same-event-support/{subject_membership_id}``
``GET    /api/v1/ministries/{ministry_id}/member-groups``
``POST   /api/v1/ministries/{ministry_id}/member-groups``
``PUT    /api/v1/member-groups/{member_group_id}/members/{ministry_membership_id}``
``DELETE /api/v1/member-groups/{member_group_id}/members/{ministry_membership_id}``

The HTTP surface over :mod:`app.services.event_gap`,
:mod:`app.services.member_group` and :mod:`app.services.same_event_support`,
each of which owns its own rule: what it means, that it is hard and
non-overridable, that it is ministry- and period-scoped, and that it never
carries into a later period. Nothing about any of that is decided here.

**One collection resource, one named rule under each.** ``scheduling-rules`` is
a period's rule configuration; each rule is a sibling under it rather than a new
top-level URL, and one ``GET`` reports all of them, because a head reads a
period's rules as one thing. The mutation verbs mirror
:mod:`app.api.routes_serving_limits` deliberately -- ``PUT`` a value, ``DELETE``
to clear -- so every management screen behaves the same way.

**Member groups themselves are a ministry resource, not a period one**, and sit
under ``/ministries/{id}/member-groups`` accordingly: a category of people
outlives any one quarter, while the cap a period puts on it does not.

**Every route stays thin.** Resolve the actor, resolve the URL's rows, call the
service, map the result. Who may act is
:func:`~app.services.authorization.require_ministry_operator`, scoped to the
ministry the rule belongs to, so a Head of a *different* ministry gets a 403;
Pydantic's own ``ge=1`` refuses a non-positive number before any service is
reached. No route commits: :func:`get_session` is the transaction boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_actor, get_session
from app.api.scheduling_rule_schemas import (
    CreateMemberGroupRequest,
    MemberGroupLimitEntry,
    MemberGroupResponse,
    MemberGroupsResponse,
    PeriodEventGapRuleResponse,
    SameEventSupportEntry,
    SetMemberGroupLimitRequest,
    SetMinInterveningEventsRequest,
    SetSameEventSupportRequest,
)
from app.models.core import Ministry, MinistryMembership, Person
from app.models.scheduling_input import MemberGroup, SchedulingPeriod
from app.services.authorization import can_operate_ministry
from app.services.event_gap import (
    PeriodEventGapRule,
    get_event_gap_rule,
    set_min_intervening_events,
)
from app.services.member_group import (
    create_member_group,
    list_member_groups,
    list_period_member_group_limits,
    set_member_group_event_limit,
    set_member_group_membership,
)
from app.services.same_event_support import (
    clear_same_event_support_requirement,
    list_support_requirements,
    set_same_event_support_requirement,
)

__all__ = ["router"]

router = APIRouter(tags=["scheduling rules"])

_PERIOD_NOT_FOUND = "Scheduling period not found."
_MINISTRY_NOT_FOUND = "Ministry not found."
_GROUP_NOT_FOUND = "Member group not found."
_MEMBERSHIP_NOT_FOUND = "Ministry membership not found."
_RULE_PATH = (
    "/scheduling-periods/{scheduling_period_id}/scheduling-rules"
    "/min-intervening-events"
)
_GROUP_LIMIT_PATH = (
    "/scheduling-periods/{scheduling_period_id}/scheduling-rules"
    "/member-group-limits/{member_group_id}"
)
_SUPPORT_PATH = (
    "/scheduling-periods/{scheduling_period_id}/scheduling-rules"
    "/same-event-support/{subject_membership_id}"
)
_GROUP_MEMBER_PATH = (
    "/member-groups/{member_group_id}/members/{ministry_membership_id}"
)


@router.get(
    "/scheduling-periods/{scheduling_period_id}/scheduling-rules",
    response_model=PeriodEventGapRuleResponse,
    summary="A scheduling period's configured scheduling rules",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period."},
    },
)
def read_scheduling_rules(
    scheduling_period_id: int = Path(
        ge=1, description="The period to show scheduling rules for."
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PeriodEventGapRuleResponse:
    """Read-only, and the one read behind the Scheduling rules screen.

    ``min_intervening_events`` is ``null`` when no event-gap rule is configured
    -- the same person may serve consecutive events of this ministry --
    otherwise the positive number of this ministry's own events that must fall
    between two assignments of the same person.

    ``member_group_limits`` lists **every** member group this ministry defines,
    each with its cap for this period or ``null`` for none: a group a head might
    want to cap is exactly what this screen exists to show.
    ``same_event_support_requirements`` lists only the requirements that are
    configured, because a requirement that does not exist is not a row anybody
    is looking at.

    Authorization is the service's, asked once and relied on by the other two
    reads below it: all three are scoped to this one period's ministry.
    """
    period = _require_period(session, scheduling_period_id)
    rule = get_event_gap_rule(session, actor=actor, scheduling_period=period)
    limits = list_period_member_group_limits(
        session, actor=actor, scheduling_period=period
    )
    support = list_support_requirements(
        session, actor=actor, scheduling_period=period
    )
    return _rule_response(rule, actor=actor, limits=limits, support=support)


@router.put(
    _RULE_PATH,
    response_model=PeriodEventGapRuleResponse,
    summary="Set how many events must be skipped between assignments",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period."},
    },
)
def put_min_intervening_events(
    scheduling_period_id: int = Path(
        ge=1, description="The period the rule applies to."
    ),
    body: SetMinInterveningEventsRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PeriodEventGapRuleResponse:
    """Record the rule if none exists, or change it if one does.

    ``min_intervening_events`` is validated positive by the request schema
    before this ever runs; ``0`` and ``null`` are not values this endpoint
    accepts -- see ``DELETE`` for clearing the rule, because "consecutive
    assignments are allowed" is the *absence* of the rule rather than a value
    of it.

    Idempotent for a repeated value that changes nothing (the service's own
    rule). Configuring a gap that an existing draft already breaks is permitted
    here and deliberately so -- nothing deletes an assignment;
    :mod:`app.services.finalization_readiness` reports the version as
    unfinalizable until a head repairs it.
    """
    period = _require_period(session, scheduling_period_id)
    set_min_intervening_events(
        session,
        actor=actor,
        scheduling_period=period,
        min_intervening_events=body.min_intervening_events,
    )
    return _rule_response(
        PeriodEventGapRule(
            scheduling_period_id=period.id,
            scheduling_period_name=period.name,
            ministry_id=period.ministry_id,
            min_intervening_events=body.min_intervening_events,
        ),
        actor=actor,
        limits=list_period_member_group_limits(
            session, actor=actor, scheduling_period=period
        ),
        support=list_support_requirements(
            session, actor=actor, scheduling_period=period
        ),
    )


@router.delete(
    _RULE_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear this period's event-gap rule",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period."},
    },
)
def delete_min_intervening_events(
    scheduling_period_id: int = Path(
        ge=1, description="The period to clear the rule from."
    ),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> None:
    """**204** whether or not a rule existed to remove: clearing an
    already-absent rule is the ordinary no-op the service defines, not a 404 --
    the period still exists, only the rule on it does not.
    """
    period = _require_period(session, scheduling_period_id)
    set_min_intervening_events(
        session, actor=actor, scheduling_period=period, min_intervening_events=None
    )


@router.put(
    _GROUP_LIMIT_PATH,
    response_model=PeriodEventGapRuleResponse,
    summary="Set how many members of one group may serve one event",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period or member group."},
    },
)
def put_member_group_limit(
    scheduling_period_id: int = Path(ge=1, description="The period the cap applies to."),
    member_group_id: int = Path(ge=1, description="The member group to cap."),
    body: SetMemberGroupLimitRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PeriodEventGapRuleResponse:
    """Record the cap if none exists, or change it if one does.

    ``max_per_event`` is validated positive by the request schema before this
    ever runs; ``0`` and ``null`` are not values this endpoint accepts -- see
    ``DELETE`` for clearing the cap, because "no limit" is the *absence* of the
    rule rather than a value of it.

    Idempotent for a repeated value that changes nothing. Setting a cap an
    existing draft already breaks is permitted here and deliberately so --
    nothing deletes an assignment; :mod:`app.services.finalization_readiness`
    reports the version as unfinalizable until a head repairs it.
    """
    period = _require_period(session, scheduling_period_id)
    group = _require_member_group(session, member_group_id)
    set_member_group_event_limit(
        session,
        actor=actor,
        member_group=group,
        scheduling_period=period,
        max_per_event=body.max_per_event,
    )
    return _current_rules(session, actor=actor, period=period)


@router.delete(
    _GROUP_LIMIT_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear one member group's per-event limit for this period",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period or member group."},
    },
)
def delete_member_group_limit(
    scheduling_period_id: int = Path(ge=1, description="The period to clear the cap from."),
    member_group_id: int = Path(ge=1, description="The member group to uncap."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> None:
    """**204** whether or not a cap existed to remove: clearing an
    already-absent cap is the ordinary no-op the service defines, not a 404 --
    the group and the period both still exist, only the rule between them does
    not.
    """
    period = _require_period(session, scheduling_period_id)
    group = _require_member_group(session, member_group_id)
    set_member_group_event_limit(
        session,
        actor=actor,
        member_group=group,
        scheduling_period=period,
        max_per_event=None,
    )


@router.put(
    _SUPPORT_PATH,
    response_model=PeriodEventGapRuleResponse,
    summary="Set one member's same-event support requirement",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period or ministry membership."},
    },
)
def put_same_event_support(
    scheduling_period_id: int = Path(ge=1, description="The period the rule applies to."),
    subject_membership_id: int = Path(
        ge=1, description="The membership the requirement constrains."
    ),
    body: SetSameEventSupportRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> PeriodEventGapRuleResponse:
    """Record the requirement if none exists, or revise it if one does.

    **The approved supporter set is replaced wholesale**, matching the service:
    the body states the set as it now stands. The subject may not appear in
    their own set, and fewer approved supporters than ``min_supporters`` is
    refused -- a requirement nobody could satisfy would mean this member could
    never be scheduled, which is a configuration mistake rather than a stricter
    rule.

    There is no ``reason`` in the body and none is stored (requirements §4.7).
    """
    period = _require_period(session, scheduling_period_id)
    subject = _require_membership(session, subject_membership_id)
    supporters = [
        _require_membership(session, membership_id)
        for membership_id in body.supporter_membership_ids
    ]
    set_same_event_support_requirement(
        session,
        actor=actor,
        subject_membership=subject,
        scheduling_period=period,
        supporter_memberships=supporters,
        min_supporters=body.min_supporters,
    )
    return _current_rules(session, actor=actor, period=period)


@router.delete(
    _SUPPORT_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear one member's same-event support requirement",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this period's ministry."},
        404: {"description": "No such scheduling period or ministry membership."},
    },
)
def delete_same_event_support(
    scheduling_period_id: int = Path(ge=1, description="The period to clear the rule from."),
    subject_membership_id: int = Path(ge=1, description="The constrained membership."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> None:
    """**204** whether or not a requirement existed to remove, for the same
    reason the other clearing endpoints answer 204.
    """
    period = _require_period(session, scheduling_period_id)
    subject = _require_membership(session, subject_membership_id)
    clear_same_event_support_requirement(
        session,
        actor=actor,
        subject_membership=subject,
        scheduling_period=period,
    )


@router.get(
    "/ministries/{ministry_id}/member-groups",
    response_model=MemberGroupsResponse,
    summary="A ministry's member groups and who is in each",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such ministry."},
    },
)
def read_member_groups(
    ministry_id: int = Path(ge=1, description="The ministry whose groups to list."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MemberGroupsResponse:
    """Read-only. Groups are ministry-scoped standing categories; the per-event
    cap on one belongs to a scheduling period and is read there.
    """
    ministry = _require_ministry(session, ministry_id)
    groups = list_member_groups(session, actor=actor, ministry=ministry)
    return MemberGroupsResponse(
        ministry_id=ministry_id,
        member_groups=[
            MemberGroupResponse(
                member_group_id=group.member_group_id,
                ministry_id=group.ministry_id,
                name=group.name,
                member_membership_ids=list(group.member_membership_ids),
            )
            for group in groups
        ],
        can_operate=can_operate_ministry(actor, ministry_id=ministry_id),
    )


@router.post(
    "/ministries/{ministry_id}/member-groups",
    response_model=MemberGroupResponse,
    summary="Define a member group for this ministry",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this ministry."},
        404: {"description": "No such ministry."},
    },
)
def create_group(
    ministry_id: int = Path(ge=1, description="The ministry to define the group for."),
    body: CreateMemberGroupRequest = Body(...),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> MemberGroupResponse:
    """**Idempotent by name, case-insensitively**: posting a name that already
    exists returns the existing group rather than a second one, and records no
    history, because nothing changed.

    Answers **200**, not 201, for that reason -- the response describes a group
    that may or may not have been created by this call, and claiming creation
    either way would be a lie one of the two times.
    """
    ministry = _require_ministry(session, ministry_id)
    group = create_member_group(
        session, actor=actor, ministry=ministry, name=body.name
    )
    session.flush()
    return MemberGroupResponse(
        member_group_id=group.id,
        ministry_id=group.ministry_id,
        name=group.name,
        member_membership_ids=_group_member_ids(session, actor=actor, ministry=ministry,
                                                member_group_id=group.id),
    )


@router.put(
    _GROUP_MEMBER_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Put one membership into a member group",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this group's ministry."},
        404: {"description": "No such member group or ministry membership."},
    },
)
def put_group_member(
    member_group_id: int = Path(ge=1, description="The group to add to."),
    ministry_membership_id: int = Path(ge=1, description="The membership to add."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> None:
    """**204**, and idempotent: adding somebody already in the group changes
    nothing and records no history.
    """
    group = _require_member_group(session, member_group_id)
    membership = _require_membership(session, ministry_membership_id)
    set_member_group_membership(
        session,
        actor=actor,
        member_group=group,
        membership=membership,
        is_member=True,
    )


@router.delete(
    _GROUP_MEMBER_PATH,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Take one membership out of a member group",
    responses={
        401: {"description": "No active actor could be established."},
        403: {"description": "The actor may not manage this group's ministry."},
        404: {"description": "No such member group or ministry membership."},
    },
)
def delete_group_member(
    member_group_id: int = Path(ge=1, description="The group to remove from."),
    ministry_membership_id: int = Path(ge=1, description="The membership to remove."),
    actor: Person = Depends(get_current_actor),
    session: Session = Depends(get_session),
) -> None:
    """**204** whether or not the membership was in the group: removing
    somebody who is not in it is the ordinary no-op the service defines.
    """
    group = _require_member_group(session, member_group_id)
    membership = _require_membership(session, ministry_membership_id)
    set_member_group_membership(
        session,
        actor=actor,
        member_group=group,
        membership=membership,
        is_member=False,
    )


def _group_member_ids(
    session: Session, *, actor: Person, ministry: Ministry, member_group_id: int
) -> list[int]:
    """The members of one group, read back through the authorized listing so
    this module keeps no query of its own."""
    for group in list_member_groups(session, actor=actor, ministry=ministry):
        if group.member_group_id == member_group_id:
            return list(group.member_membership_ids)
    return []


def _current_rules(
    session: Session, *, actor: Person, period: SchedulingPeriod
) -> PeriodEventGapRuleResponse:
    """The whole rule picture after a mutation, read back through the services.

    Re-read rather than patched together from the request body: a rule mutation
    can change what another rule's listing shows -- a newly capped group appears
    with its number, a revised requirement with its new set -- and building the
    response by hand would be maintaining a second, quieter copy of what the
    services already answer.
    """
    return _rule_response(
        get_event_gap_rule(session, actor=actor, scheduling_period=period),
        actor=actor,
        limits=list_period_member_group_limits(
            session, actor=actor, scheduling_period=period
        ),
        support=list_support_requirements(
            session, actor=actor, scheduling_period=period
        ),
    )


def _require_ministry(session: Session, ministry_id: int) -> Ministry:
    """The ministry the URL names, or 404, read on the request Session."""
    ministry = session.execute(
        select(Ministry).where(Ministry.id == ministry_id)
    ).scalar_one_or_none()
    if ministry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_MINISTRY_NOT_FOUND
        )
    return ministry


def _require_member_group(session: Session, member_group_id: int) -> MemberGroup:
    """The member group the URL names, or 404, read on the request Session."""
    group = session.execute(
        select(MemberGroup).where(MemberGroup.id == member_group_id)
    ).scalar_one_or_none()
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_GROUP_NOT_FOUND
        )
    return group


def _require_membership(
    session: Session, ministry_membership_id: int
) -> MinistryMembership:
    """The membership the URL or body names, or 404.

    Deliberately **not** filtered by activity: a deactivated membership must
    reach the service and be refused there, with its own clear message, rather
    than being reported as a membership that does not exist.
    """
    membership = session.execute(
        select(MinistryMembership).where(
            MinistryMembership.id == ministry_membership_id
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_MEMBERSHIP_NOT_FOUND
        )
    return membership


def _require_period(session: Session, scheduling_period_id: int) -> SchedulingPeriod:
    """The scheduling period the URL names, or 404, read on the request Session."""
    period = session.execute(
        select(SchedulingPeriod).where(SchedulingPeriod.id == scheduling_period_id)
    ).scalar_one_or_none()
    if period is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=_PERIOD_NOT_FOUND
        )
    return period


def _rule_response(
    rule: PeriodEventGapRule, *, actor: Person, limits=None, support=None
) -> PeriodEventGapRuleResponse:
    """Map the three services' value objects into one response.

    ``limits`` and ``support`` default to ``None`` so a caller that has only the
    gap rule to report still gets a well-formed response -- with the two lists
    empty, which is exactly what "this period configures neither" means.
    """
    return PeriodEventGapRuleResponse(
        scheduling_period_id=rule.scheduling_period_id,
        scheduling_period_name=rule.scheduling_period_name,
        ministry_id=rule.ministry_id,
        min_intervening_events=rule.min_intervening_events,
        member_group_limits=[
            MemberGroupLimitEntry(
                member_group_id=group.member_group_id,
                name=group.name,
                member_count=group.member_count,
                max_per_event=group.max_per_event,
            )
            for group in (limits.groups if limits is not None else ())
        ],
        same_event_support_requirements=[
            SameEventSupportEntry(
                subject_membership_id=entry.subject_membership_id,
                subject_display_name=entry.subject_display_name,
                min_supporters=entry.min_supporters,
                supporter_membership_ids=list(entry.supporter_membership_ids),
                supporter_display_names=list(entry.supporter_display_names),
            )
            for entry in (support.requirements if support is not None else ())
        ],
        can_operate=can_operate_ministry(actor, ministry_id=rule.ministry_id),
    )
