"""Populate a *local* development database from a git-ignored roster directory.

**Nothing church-specific is in this file.** It contains no name, no ministry,
no date and no role list: every one of those comes either from the CLI the
operator types or from files under a directory git proves is ignored. That is
the whole point -- the tooling is reusable and committable, the records it
reads are neither.

**Why this exists.** The product has no screen and no endpoint for creating a
Person, a Ministry, a membership or a scheduling period, so the only way to
put a real ministry's roster in front of the UI is a local script. The
alternative -- typing a 29-person roster into a database by hand before a demo
-- is both slow and much easier to get wrong.

**What it reuses, and what it writes directly.** Reading is
:mod:`scripts.historical_setup.csv_source`, which already understands the
availability-grid and schedule-grid shapes and refuses to guess at anything it
cannot explain. Writing goes through the real domain services wherever one
exists -- role qualification, staffing requirements, availability, the
availability lock, and the Ministry Head grant -- so their validation and their
audit rows are the genuine ones. Five kinds of row are inserted directly,
because no public service creates them yet: Church, Person, Ministry,
MinistryRole and Event.

``Event`` is the one of those five that *does* have a creator,
:func:`app.services.scheduling_period.generate_sunday_events`, and it is
deliberately not used here. That function generates **every** calendar Sunday
between a period's two dates and cannot express a special event at all. A
source roster states exactly which dates the ministry meets on -- typically
most Sundays plus the occasional one-off -- and inventing the Sundays it left
out, or dropping the one-off it listed, would both be the importer making up
data. Mirroring the source exactly is the only faithful option.

**Staffing is the head's rule, not the spreadsheet's shape.** A roster grid
says how many columns somebody drew and a filled schedule says who actually
served; neither states how many people the ministry *needs*. So an optional
manifest -- three generic columns, ``event_date``, ``role``,
``required_count`` -- lets the head state it per event, and the source's own
shape is used only where the manifest is silent. See
:func:`parse_staffing_overrides`.

**It never deletes anything.** There is no reset and no overwrite: importing
into a ministry name that already exists is refused. To rehearse twice, import
again under a different ministry name, which costs seconds and leaves the
first import intact.

Every count this module reports is a count. No name, email or cell value is
returned, logged or raised from here.
"""

from __future__ import annotations

import csv
import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from scripts.person_mapping import PersonMap, resolve_person_map  # noqa: E402
from app.models.core import Church, Ministry, MinistryMembership, MinistryRole, Person
from app.models.scheduling_input import (
    AVAILABILITY_AVAILABLE,
    AVAILABILITY_BACKUP,
    AVAILABILITY_UNAVAILABLE,
    EVENT_KIND_SPECIAL,
    EVENT_KIND_SUNDAY_SERVICE,
    Event,
    SchedulingPeriod,
)
from app.scheduling.input import AvailabilityState
from app.services.availability import set_availability
from app.services.ministry_authority import grant_ministry_head
from app.services.role_qualification import set_role_qualification
from app.services.scheduling_period import create_scheduling_period, lock_availability
from app.services.staffing_requirement import set_staffing_requirement

from scripts.historical_setup.model import HistoricalDataset, HistoricalVolunteer
from scripts.historical_setup.parsing import AmbiguousDateError, parse_schedule_date

__all__ = [
    "LocalImportError",
    "ImportPlan",
    "ImportSummary",
    "IMPORT_REASON",
    "STAFFING_OVERRIDE_COLUMNS",
    "availability_state_column",
    "load_staffing_overrides",
    "parse_staffing_overrides",
    "qualified_role_names_for",
    "import_dataset",
]


#: Stamped on the free-text fields the schema already has, so anyone who opens
#: this database later can see at a glance where the rows came from. No column
#: was added for provenance -- the schema is untouched.
IMPORT_REASON = "Local import from a git-ignored roster directory"


class LocalImportError(RuntimeError):
    """The import cannot proceed without guessing, so it does not proceed.

    Raised rather than repaired, for the same reason the demo seed raises: a
    half-imported ministry has no single correct fix, and picking one risks
    writing rows nobody asked for.

    Messages here name **settings, counts and dates only**. A date is source
    structure, not personal data; a display name is, and never appears.
    """


@dataclass(frozen=True, slots=True)
class ImportPlan:
    """Everything the operator decides, supplied at run time.

    Kept separate from the dataset so that none of it can drift into this
    file as a default. There is no default church name, ministry name or head
    -- omitting one is an error, not a fallback onto somebody's real values.
    """

    church_name: str
    ministry_name: str
    period_name: str
    #: The volunteer who becomes Ministry Head, matched case-insensitively
    #: against the source roster. This is the person the frontend will act as.
    head_name: str
    #: The bootstrap Admin. Every service takes an actor and only an Admin may
    #: grant Ministry Head authority, so one has to exist before anything else
    #: can happen -- the same bootstrap problem the demo seed has.
    admin_name: str
    #: Volunteers to treat as qualified for the lead role regardless of what
    #: the source says. This is how a *current* correction to an older source
    #: is expressed without editing either the source or this code.
    also_lead_names: frozenset[str] = frozenset()
    #: Whether to close availability at the end of the import. Locking is
    #: required before a schedule can be started, but leaving it open is what
    #: lets the availability screen be demonstrated as an editable screen.
    lock_availability_when_done: bool = False
    church_timezone: str = "UTC"
    #: The Ministry Head's own staffing rule, per event, as
    #: ``{date: {role_name: required_count}}``. A date present here has
    #: **exactly** these requirements; a date absent keeps whatever the source
    #: implied. Empty is the ordinary case -- see
    #: :func:`parse_staffing_overrides` for why this is stated rather than
    #: inferred.
    staffing_overrides: dict[datetime.date, dict[str, int]] = field(
        default_factory=dict
    )
    #: Source names the operator has **explicitly** declared to be people who
    #: already exist, as ``{case-folded source name: person_id}``.
    #:
    #: Empty is the ordinary case and preserves the original behaviour exactly:
    #: a new Person per volunteer. It is only when a *second* ministry is
    #: imported that reuse becomes necessary, because a volunteer who serves in
    #: two ministries would otherwise get two Person rows and, since Task 77,
    #: two half-schedules. See :mod:`scripts.person_mapping` -- including why
    #: this is an operator assertion and never a name match.
    person_map: "PersonMap | None" = None


@dataclass(frozen=True, slots=True)
class ImportSummary:
    """What was written, as counts and ids. Never a name.

    ``head_person_id`` is the only value an operator has to carry away: it is
    what ``CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID`` must be set to for
    the UI to show this ministry.
    """

    church_id: int
    ministry_id: int
    period_id: int
    head_person_id: int
    admin_person_id: int
    people: int
    roles: int
    sunday_events: int
    special_events: int
    staffing_requirements: int
    #: How many events took their staffing from the head's rule rather than
    #: from the source's own shape. Reported so a run cannot silently ignore a
    #: manifest that failed to match anything.
    events_with_overridden_staffing: int
    qualifications_granted: int
    qualifications_declined: int
    availability_rows: int
    #: How many volunteers were attached to an existing Person through the
    #: operator's map rather than created. Reported because the difference
    #: between "reused 12" and "reused 0" is the difference between one
    #: person's schedule and two halves of it, and a silent zero is exactly
    #: what a mistyped map produces.
    people_reused: int
    availability_locked: bool
    period_start: datetime.date
    period_end: datetime.date


#: The solver's availability vocabulary, mapped onto the column's. Written out
#: rather than derived from ``.value`` so that renaming either side is a
#: compile-time-visible change here instead of a silent mismatch.
_AVAILABILITY_COLUMN_VALUE: dict[AvailabilityState, str] = {
    AvailabilityState.AVAILABLE: AVAILABILITY_AVAILABLE,
    AvailabilityState.BACKUP: AVAILABILITY_BACKUP,
    AvailabilityState.UNAVAILABLE: AVAILABILITY_UNAVAILABLE,
}


def availability_state_column(state: AvailabilityState) -> str | None:
    """The stored ``availability_state`` for one parsed answer.

    ``NO_RESPONSE`` maps to ``None``, which
    :func:`app.services.availability.set_availability` reads as "clear the
    answer". That is right: no response is the *absence* of a row, and writing
    one would turn "never answered" into an answer the person never gave.
    """
    if state is AvailabilityState.NO_RESPONSE:
        return None
    try:
        return _AVAILABILITY_COLUMN_VALUE[state]
    except KeyError:  # pragma: no cover - unreachable while the enum is closed
        raise LocalImportError(
            f"no stored availability value corresponds to {state!r}"
        ) from None


#: The three columns a staffing-override file must have. Generic on purpose --
#: a date, a role label and a number. No ministry, date or role name of anyone's
#: appears in this file; they live in the operator's own ignored manifest.
STAFFING_OVERRIDE_COLUMNS = ("event_date", "role", "required_count")


def parse_staffing_overrides(
    rows: Iterable[Mapping[str, str]],
    *,
    known_roles: Iterable[str],
    known_dates: Iterable[datetime.date],
    year_hint: int,
) -> dict[datetime.date, dict[str, int]]:
    """Read a per-event staffing manifest into ``{date: {role: count}}``.

    **Why this exists.** How many people an event needs is the Ministry Head's
    rule, and a roster spreadsheet does not state it -- what a source grid
    carries is how many columns somebody drew, and what a filled schedule
    carries is who actually served. Reading a requirement off either is
    inference dressed up as data: a column nobody filled becomes a position the
    ministry never asked for, and a Sunday somebody was away becomes a smaller
    team than the head wants. So the rule is stated, by the person who owns it,
    in a file this reads.

    **A listed date is replaced outright, not merged.** Every role that event
    needs is listed, and a role left out is a role that event does not need --
    which is precisely how a smaller team is expressed. Merging would make
    "four people this week" impossible to say without also inventing a
    "requirement of zero", a second spelling of a fact the absence of a row
    already states.

    Every value is validated against what the source actually contains, and
    anything unrecognized stops the import: a date the roster does not hold, a
    role the ministry does not have, a count that is not a positive whole
    number, or the same (date, role) stated twice with no way to know which was
    meant.
    """
    roles_by_key = {_role_key(name): name for name in known_roles}
    dates = set(known_dates)

    overrides: dict[datetime.date, dict[str, int]] = {}
    for line, row in enumerate(rows, start=2):  # header is line 1
        missing = [c for c in STAFFING_OVERRIDE_COLUMNS if c not in row]
        if missing:
            raise LocalImportError(
                f"staffing override line {line} has no {missing[0]!r} column;"
                f" the columns are {', '.join(STAFFING_OVERRIDE_COLUMNS)}"
            )
        if not any((row[c] or "").strip() for c in STAFFING_OVERRIDE_COLUMNS):
            continue  # a blank line, not a statement

        event_date = _override_date(row["event_date"], line=line, year_hint=year_hint)
        if event_date not in dates:
            raise LocalImportError(
                f"staffing override line {line} names {event_date.isoformat()},"
                " which the roster source does not list as an event date"
            )

        role_name = roles_by_key.get(_role_key(row["role"] or ""))
        if role_name is None:
            raise LocalImportError(
                f"staffing override line {line} names a role this ministry does"
                " not have. Refusing rather than guessing which one was meant."
            )

        count = _override_count(row["required_count"], line=line)

        for_date = overrides.setdefault(event_date, {})
        if role_name in for_date:
            raise LocalImportError(
                f"staffing override line {line} states {event_date.isoformat()}"
                " twice for the same role; there is no way to know which count"
                " was meant"
            )
        for_date[role_name] = count

    return overrides


def load_staffing_overrides(
    path: Path, *, known_roles: Iterable[str], known_dates: Iterable[datetime.date],
    year_hint: int,
) -> dict[datetime.date, dict[str, int]]:
    """:func:`parse_staffing_overrides`, reading one CSV file.

    Split from the parsing so the rules above can be tested without a file, and
    so the only thing this adds is opening one.
    """
    try:
        text = path.read_text()
    except OSError as error:
        raise LocalImportError(
            f"the staffing override file could not be read: {error.strerror}"
        ) from None
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise LocalImportError("the staffing override file is empty")
    return parse_staffing_overrides(
        ({(k or "").strip(): (v or "") for k, v in row.items()} for row in reader),
        known_roles=known_roles,
        known_dates=known_dates,
        year_hint=year_hint,
    )


def _role_key(raw: str) -> str:
    """Match role labels the way a person would: ignoring case and spacing.

    Deliberately not the Setup-specific ``normalize_role_name`` -- that maps
    onto one ministry's approved five, and this has to work for a ministry
    whose roles are named anything at all.
    """
    return " ".join(raw.strip().lower().split())


def _override_date(raw: str, *, line: int, year_hint: int) -> datetime.date:
    try:
        return parse_schedule_date(raw, year_hint=year_hint)
    except (AmbiguousDateError, ValueError):
        raise LocalImportError(
            f"staffing override line {line} has a date that could not be read"
            " without guessing"
        ) from None


def _override_count(raw: str, *, line: int) -> int:
    value = (raw or "").strip()
    try:
        count = int(value)
    except ValueError:
        raise LocalImportError(
            f"staffing override line {line} has a required_count that is not a"
            " whole number"
        ) from None
    # Zero is not accepted, for the same reason the event-gap rule rejects it:
    # "this event does not need this role" is what leaving the row out already
    # says, and one fact with two spellings is a bug waiting to be written.
    if count < 1:
        raise LocalImportError(
            f"staffing override line {line} has a required_count below 1. Leave"
            " the role out of that date instead; an omitted role is a role the"
            " event does not need."
        )
    return count


def qualified_role_names_for(
    volunteer: HistoricalVolunteer,
    *,
    role_names: tuple[str, ...],
    lead_role_name: str,
    also_lead: bool,
) -> frozenset[str]:
    """Which of ``role_names`` this volunteer may serve.

    Three source shapes, in the order the reader can supply them:

    - ``qualified_role_names`` set -- authoritative and used as-is, **including
      when it is empty**. A ministry whose volunteers specialize states
      qualification per role, and an empty set is the real answer "none of
      them", not a missing one.
    - ``restricted_support_roles`` set -- the support roles this person may
      serve, with the lead role decided separately by ``lead_qualified``.
    - neither -- the common case: every support role, plus the lead role only
      if the source says so.

    ``also_lead`` is the operator's current correction and is applied last, so
    it wins over all three. It only ever *adds* the lead role; there is no
    switch here that takes a qualification away, because "the source is wrong
    about who cannot lead" is not a claim this tool has any way to check.
    """
    support_roles = tuple(name for name in role_names if name != lead_role_name)

    if volunteer.qualified_role_names is not None:
        qualified = set(volunteer.qualified_role_names) & set(role_names)
    else:
        if volunteer.restricted_support_roles:
            qualified = set(volunteer.restricted_support_roles) & set(support_roles)
        else:
            qualified = set(support_roles)
        if volunteer.lead_qualified:
            qualified.add(lead_role_name)

    if also_lead:
        qualified.add(lead_role_name)

    return frozenset(qualified)


def import_dataset(
    session: Session, *, dataset: HistoricalDataset, plan: ImportPlan
) -> ImportSummary:
    """Write ``dataset`` into the database ``session`` is connected to.

    The caller owns the transaction, exactly as with every domain service in
    this project: nothing here commits or rolls back, so a failure anywhere
    leaves the database as it was rather than half-populated.

    The order is forced by what each step needs from the one before it: the
    church, then the Admin who can grant authority, then the ministry and its
    roles, then the people and memberships, then the head, then qualifications,
    then the period and its events, then staffing, then availability -- and the
    lock last of all, because
    :func:`app.services.availability.set_availability` refuses to write once a
    period is locked.
    """
    if not dataset.sundays:
        raise LocalImportError("the source lists no event dates")
    if not dataset.roles:
        raise LocalImportError("the source lists no roles")
    if not dataset.volunteers:
        raise LocalImportError("the source lists no volunteers")

    church = _church(session, plan)
    admin = _admin(session, church=church, plan=plan)
    ministry = _ministry(session, church=church, plan=plan)
    roles = _roles(session, ministry=ministry, dataset=dataset)

    # Checked against the database *before* the first volunteer is written, so
    # a bad mapping refuses the whole import rather than half-applying it.
    reusable = resolve_person_map(
        session,
        plan.person_map or PersonMap(by_source_name={}),
        church_id=church.id,
        ministry_id=ministry.id,
        source_names=[volunteer.display_name for volunteer in dataset.volunteers],
    )

    memberships, people_reused = _people_and_memberships(
        session, church=church, ministry=ministry, dataset=dataset, reusable=reusable
    )
    head_membership = _head_membership(memberships, plan=plan)
    grant_ministry_head(
        session, actor=admin, membership=head_membership, reason=IMPORT_REASON
    )
    head = head_membership.person

    granted, declined = _qualifications(
        session, actor=head, dataset=dataset, roles=roles, memberships=memberships, plan=plan
    )

    period = create_scheduling_period(
        session,
        actor=head,
        ministry=ministry,
        name=plan.period_name,
        start_date=min(dataset.sundays),
        end_date=max(dataset.sundays),
    )
    events = _events(session, period=period, ministry=ministry, dataset=dataset)

    staffing = _staffing(
        session,
        actor=head,
        dataset=dataset,
        roles=roles,
        events=events,
        overrides=plan.staffing_overrides,
    )
    availability_rows = _availability(
        session, actor=head, dataset=dataset, events=events, memberships=memberships
    )

    if plan.lock_availability_when_done:
        lock_availability(session, actor=head, period=period, reason=IMPORT_REASON)
    session.flush()

    return ImportSummary(
        church_id=church.id,
        ministry_id=ministry.id,
        period_id=period.id,
        head_person_id=head.id,
        admin_person_id=admin.id,
        people=len(memberships),
        roles=len(roles),
        sunday_events=sum(
            1 for e in events.values() if e.event_kind == EVENT_KIND_SUNDAY_SERVICE
        ),
        special_events=sum(
            1 for e in events.values() if e.event_kind == EVENT_KIND_SPECIAL
        ),
        staffing_requirements=staffing,
        events_with_overridden_staffing=len(plan.staffing_overrides),
        qualifications_granted=granted,
        qualifications_declined=declined,
        availability_rows=availability_rows,
        people_reused=people_reused,
        availability_locked=period.availability_locked_at is not None,
        period_start=period.start_date,
        period_end=period.end_date,
    )


# -- Steps -----------------------------------------------------------------


def _church(session: Session, plan: ImportPlan) -> Church:
    """The named church, reused if it is already there.

    Reuse rather than refuse, because a second ministry of the same church is
    an ordinary thing to import. More than one row with the name is not
    something to pick between, so it stops.
    """
    churches = (
        session.execute(select(Church).where(Church.name == plan.church_name))
        .scalars()
        .all()
    )
    if len(churches) > 1:
        raise LocalImportError(
            f"found {len(churches)} churches with the configured name; expected at"
            " most one"
        )
    if churches:
        return churches[0]

    church = Church(name=plan.church_name, timezone=plan.church_timezone)
    session.add(church)
    session.flush([church])
    return church


def _admin(session: Session, *, church: Church, plan: ImportPlan) -> Person:
    """The bootstrap Admin, reused if a previous import already made one.

    Deliberately not one of the roster's own people: the roster describes
    volunteers, and quietly promoting one of them to a church-wide
    administrator would be this tool inventing an authority nobody granted.
    """
    existing = (
        session.execute(
            select(Person).where(
                Person.church_id == church.id,
                Person.display_name == plan.admin_name,
                Person.is_admin.is_(True),
            )
        )
        .scalars()
        .all()
    )
    if len(existing) > 1:
        raise LocalImportError(
            f"found {len(existing)} administrators with the configured name;"
            " expected at most one"
        )
    if existing:
        return existing[0]

    admin = Person(
        church_id=church.id, display_name=plan.admin_name, is_admin=True
    )
    session.add(admin)
    session.flush([admin])
    return admin


def _ministry(session: Session, *, church: Church, plan: ImportPlan) -> Ministry:
    """A new ministry, or a refusal.

    Never merged into an existing one. Two imports of overlapping rosters into
    one ministry would leave duplicate people and contradictory
    qualifications, and there is no way to tell from here which of the two was
    meant.
    """
    existing = session.execute(
        select(Ministry).where(
            Ministry.church_id == church.id, Ministry.name == plan.ministry_name
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise LocalImportError(
            "a ministry with the configured name already exists in this church."
            " Nothing was written. Import under a different ministry name, or"
            " recreate the development branch for a clean slate."
        )

    ministry = Ministry(
        church_id=church.id, name=plan.ministry_name, description=IMPORT_REASON
    )
    session.add(ministry)
    session.flush([ministry])
    return ministry


def _roles(
    session: Session, *, ministry: Ministry, dataset: HistoricalDataset
) -> dict[str, MinistryRole]:
    """One MinistryRole per role the source names, in the source's own order.

    Roles the source marks as not being staffing positions are skipped
    entirely rather than created and left unused: a role that never becomes a
    requirement would show up on the staffing screen asking a head for a
    number that means nothing.
    """
    roles: dict[str, MinistryRole] = {}
    order = 0
    for role in dataset.roles:
        if not role.is_staffing_position:
            continue
        row = MinistryRole(
            ministry_id=ministry.id,
            name=role.name,
            description=IMPORT_REASON,
            display_order=order,
        )
        session.add(row)
        roles[role.name] = row
        order += 1
    if not roles:
        raise LocalImportError("the source lists no roles that need staffing")
    session.flush(list(roles.values()))
    return roles


def _people_and_memberships(
    session: Session,
    *,
    church: Church,
    ministry: Ministry,
    dataset: HistoricalDataset,
    reusable: dict[str, Person] | None = None,
) -> tuple[dict[int, MinistryMembership], int]:
    """A MinistryMembership per volunteer, on a new Person or an existing one.

    Keyed by the source's own membership id so nothing downstream has to match
    on a name.

    **A new Person is the default, and reuse is only ever explicit.** A
    volunteer is attached to an existing Person when -- and only when -- the
    operator listed that source name in a ``--person-map`` file and every check
    in :func:`scripts.person_mapping.resolve_person_map` passed. Nothing here
    compares names to decide; ``reusable`` is already resolved to Person rows,
    and an absent entry means "create", never "go and look".

    That asymmetry is the safety property. Creating a duplicate is visible and
    reversible; attaching a volunteer to the wrong existing Person merges two
    humans and mixes their schedules, and since Task 77 that is something a
    volunteer would read on their own screen.

    **No email is written**, for either path. The source carries none, and
    ``person.email`` is the match key Google sign-in uses -- inventing an
    address would either collide with a real one or silently claim an identity.
    A *reused* Person keeps whatever email it already had, which is the whole
    point: that is the row somebody already signs in as.

    Returns the memberships and how many volunteers were reused rather than
    created.
    """
    reusable = reusable or {}
    memberships: dict[int, MinistryMembership] = {}
    reused = 0

    for volunteer in dataset.volunteers:
        person = reusable.get(volunteer.display_name.strip().casefold())
        if person is None:
            person = Person(church_id=church.id, display_name=volunteer.display_name)
            session.add(person)
            session.flush([person])
        else:
            reused += 1

        membership = MinistryMembership(person_id=person.id, ministry_id=ministry.id)
        session.add(membership)
        session.flush([membership])
        memberships[volunteer.membership_id] = membership

    return memberships, reused


def _head_membership(
    memberships: dict[int, MinistryMembership], *, plan: ImportPlan
) -> MinistryMembership:
    """The membership named by ``--head-name``, matched case-insensitively.

    Exactly one match is required. Zero means the name was mistyped or is not
    on this roster; more than one means the roster has two people with that
    name, and choosing either would be a coin toss. The error says how many
    matched, never who.
    """
    wanted = plan.head_name.strip().casefold()
    matches = [
        membership
        for membership in memberships.values()
        if membership.person.display_name.strip().casefold() == wanted
    ]
    if len(matches) != 1:
        raise LocalImportError(
            f"the configured head name matches {len(matches)} people on this"
            " roster; exactly one is required"
        )
    return matches[0]


def _qualifications(
    session: Session,
    *,
    actor: Person,
    dataset: HistoricalDataset,
    roles: dict[str, MinistryRole],
    memberships: dict[int, MinistryMembership],
    plan: ImportPlan,
) -> tuple[int, int]:
    """Every (person, role) decision, recorded explicitly as True or False.

    Both halves are written on purpose. "No row" means *never assessed*, which
    is a third state the product shows differently, and leaving the negatives
    out would make a roster that genuinely restricts a role look like one
    nobody has got round to assessing yet.
    """
    also_lead = {name.strip().casefold() for name in plan.also_lead_names}
    lead_role_name = next(
        (role.name for role in dataset.roles if role.is_lead), ""
    )
    role_names = tuple(roles)

    granted = 0
    declined = 0
    for volunteer in dataset.volunteers:
        membership = memberships[volunteer.membership_id]
        qualified = qualified_role_names_for(
            volunteer,
            role_names=role_names,
            lead_role_name=lead_role_name,
            also_lead=volunteer.display_name.strip().casefold() in also_lead,
        )
        for role_name, role in roles.items():
            is_qualified = role_name in qualified
            set_role_qualification(
                session,
                actor=actor,
                membership=membership,
                role=role,
                is_qualified=is_qualified,
                reason=IMPORT_REASON,
            )
            if is_qualified:
                granted += 1
            else:
                declined += 1
    return granted, declined


def _events(
    session: Session,
    *,
    period: SchedulingPeriod,
    ministry: Ministry,
    dataset: HistoricalDataset,
) -> dict[datetime.date, Event]:
    """One Event per date the source lists, and not one more.

    A Sunday becomes an ordinary ``SUNDAY_SERVICE``. Any other weekday becomes
    a ``SPECIAL`` event, which the schema requires to be named -- and the only
    name available is the note the source itself carried against that date. A
    non-Sunday with no note stops the import rather than being given a made-up
    label or quietly demoted to an ordinary service.
    """
    events: dict[datetime.date, Event] = {}
    for event_date in sorted(dataset.sundays):
        if event_date.weekday() == 6:
            row = Event(
                scheduling_period_id=period.id,
                ministry_id=ministry.id,
                event_date=event_date,
                event_kind=EVENT_KIND_SUNDAY_SERVICE,
            )
        else:
            note = (dataset.event_notes.get(event_date) or "").strip()
            if not note:
                raise LocalImportError(
                    f"{event_date.isoformat()} is not a Sunday and the source"
                    " carries no note to name it with. A special event must be"
                    " named; naming it here would be inventing data."
                )
            row = Event(
                scheduling_period_id=period.id,
                ministry_id=ministry.id,
                event_date=event_date,
                event_kind=EVENT_KIND_SPECIAL,
                name=note,
            )
        session.add(row)
        events[event_date] = row
    session.flush(list(events.values()))
    return events


def _staffing(
    session: Session,
    *,
    actor: Person,
    dataset: HistoricalDataset,
    roles: dict[str, MinistryRole],
    events: dict[datetime.date, Event],
    overrides: Mapping[datetime.date, Mapping[str, int]],
) -> int:
    """How many people each event needs, per role.

    An event named in ``overrides`` takes **exactly** what the head's rule
    says and nothing from the source; every other event takes the source's own
    pairs. Roles and dates that end up unpaired are left with no requirement at
    all, which is the correct reading of an event that simply does not ask for
    that position.
    """
    wanted: dict[datetime.date, dict[str, int]] = {}
    for requirement in dataset.requirements:
        if requirement.event_date in overrides:
            continue  # stated by the head below; the source does not get a vote
        if requirement.role_name not in roles or requirement.event_date not in events:
            raise LocalImportError(
                "the source states a requirement for a role or date it does not"
                f" otherwise list ({requirement.event_date.isoformat()})"
            )
        wanted.setdefault(requirement.event_date, {})[requirement.role_name] = (
            requirement.required_count
        )

    for event_date, by_role in overrides.items():
        if event_date not in events:
            raise LocalImportError(
                f"the staffing rule names {event_date.isoformat()}, which is not"
                " one of this period's events"
            )
        for role_name in by_role:
            if role_name not in roles:
                raise LocalImportError(
                    f"the staffing rule for {event_date.isoformat()} names a role"
                    " this ministry does not have"
                )
        wanted[event_date] = dict(by_role)

    written = 0
    for event_date in sorted(wanted):
        for role_name, required_count in sorted(wanted[event_date].items()):
            set_staffing_requirement(
                session,
                actor=actor,
                event=events[event_date],
                role=roles[role_name],
                required_count=required_count,
                reason=IMPORT_REASON,
            )
            written += 1
    return written


def _availability(
    session: Session,
    *,
    actor: Person,
    dataset: HistoricalDataset,
    events: dict[datetime.date, Event],
    memberships: dict[int, MinistryMembership],
) -> int:
    """Only the answers the source actually marked.

    A cell the source left blank is absent from ``dataset.availability`` and
    stays absent here: no response is a real, permanent input state, and
    turning it into "available" would put words in somebody's mouth.
    """
    written = 0
    for (membership_id, event_date), state in sorted(
        dataset.availability.items(), key=lambda item: (item[0][1], item[0][0])
    ):
        column_value = availability_state_column(state)
        if column_value is None:
            continue
        membership = memberships.get(membership_id)
        event = events.get(event_date)
        if membership is None or event is None:
            raise LocalImportError(
                "the source states an availability answer for a person or date it"
                f" does not otherwise list ({event_date.isoformat()})"
            )
        set_availability(
            session,
            actor=actor,
            membership=membership,
            event=event,
            availability_state=column_value,
            reason=IMPORT_REASON,
        )
        written += 1
    return written
