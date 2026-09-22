"""Local command: a git-ignored rule-configuration directory -> the development
database (Task 74).

    set -a && . ../.env.demo && set +a          # from backend/
    python scripts/import_ministry_rules.py \\
        --input-dir ../local_data/<your-ministry>/config \\
        --church-name "..." \\
        --ministry-name "..." \\
        --period-name "..." \\
        --actor-name "..."

**Why a command and not a screen.** Task 74's two generic hard rules have full
domain and API support, but no editor: picking a group, choosing who is in it,
capping it, and naming somebody's approved supporting members is a screen of its
own rather than a third box on the Scheduling rules page. Until that screen
exists, a ministry head's real configuration reaches a local database through
this command, and the product *shows* what is active (read-only) rather than
letting it be edited there.

**What it reads.** Two optional CSV files in ``--input-dir``, each git-ignored
like the roster they belong beside. Neither is required; supply either, both, or
neither.

``member_groups.csv`` -- one row per group::

    group,max_per_event,members
    Category A,2,Ada Example;Bo Example;Cy Example

- ``group`` is a plain label this ministry chose. Nothing in the application
  branches on it, and it must not describe the people in it beyond what the
  ministry itself calls the category.
- ``max_per_event`` is the number of that group's members who may serve one
  event. Blank means "no cap": the group is still recorded, and the limit is
  cleared if one existed.
- ``members`` is a ``;``-separated list of display names, matched
  case-insensitively against the ministry's own memberships.

``support_requirements.csv`` -- one row per constrained member::

    subject,min_supporters,supporters
    Dee Example,1,Ada Example;Bo Example

- ``subject`` is the member the rule constrains.
- ``min_supporters`` is how many of the approved members must serve the **same
  event**. Blank means 1.
- ``supporters`` is the approved set, ``;``-separated. It must hold at least
  ``min_supporters`` names, and must not name the subject.

**There is deliberately no reason column, in either file.** The scheduler needs
the condition and the approved set, never the circumstance behind them
(requirements §4.7). A column for it here would be an invitation to store one.

**What it will not do.** It writes only to the database whose host you name
explicitly, it reads only from a directory git proves is ignored, and it removes
nothing it was not asked to: a group not named in the file is left exactly as it
is, and so is a member not named in a group's list. **Nothing here deletes an
assignment**, ever -- configuring a rule a draft already breaks leaves the draft
alone and makes the version unfinalizable, which is the whole design.

**What it prints.** Counts and ids. No name, no email, no cell value, including
on every failure path -- the same rule
``scripts/import_local_ministry.py`` keeps, and for the same reason.

The same environment gate as the demo seed and the roster import, because it is
the same danger. All of these must be present in the process environment, and
none of them is committed:

    APP_ENV=development
    CHURCH_SCHEDULING_ALLOW_DEMO_SEED=1
    CHURCH_SCHEDULING_DEMO_DATABASE_HOST=<host of the development branch>
    DATABASE_URL=<connection string for that same branch>

Exit codes: 0 configured, 1 refused by the guards or by a service, 2 the input
could not be read without guessing.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Running as a script rather than a module, so make ``app`` and ``scripts``
# importable from the backend directory regardless of the working directory.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.models.core import Church, Ministry, MinistryMembership, Person  # noqa: E402
from app.models.scheduling_input import SchedulingPeriod  # noqa: E402
from app.services.errors import ServiceError  # noqa: E402
from app.services.member_group import (  # noqa: E402
    create_member_group,
    set_member_group_event_limit,
    set_member_group_membership,
)
from app.services.same_event_support import (  # noqa: E402
    set_same_event_support_requirement,
)
from scripts import demo_guards  # noqa: E402
from scripts.seed_demo import read_env_file  # noqa: E402

_REPO_ROOT = _BACKEND_DIR.parent

GROUPS_FILE = "member_groups.csv"
SUPPORT_FILE = "support_requirements.csv"


class RuleImportError(Exception):
    """The supplied configuration cannot be applied without guessing.

    Carries no name, no cell value and no row content -- only what is wrong and
    which row number it was on, so a reader can find it in their own file
    without this process printing anybody's details.
    """


@dataclass
class GroupSpec:
    name: str
    max_per_event: int | None
    member_names: tuple[str, ...]


@dataclass
class SupportSpec:
    subject_name: str
    min_supporters: int
    supporter_names: tuple[str, ...]


@dataclass
class ImportSummary:
    """Counts only. Nothing here can name a person or a category."""

    ministry_id: int = 0
    period_id: int = 0
    groups_configured: int = 0
    group_members_added: int = 0
    groups_capped: int = 0
    groups_uncapped: int = 0
    support_requirements_configured: int = 0
    supporters_approved: int = 0
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Reading the files
# --------------------------------------------------------------------------


def _rows(path: Path, required: tuple[str, ...]) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in required if name not in (reader.fieldnames or [])]
        if missing:
            raise RuleImportError(
                f"{path.name}: missing column(s) {', '.join(missing)}"
            )
        return [row for row in reader]


def _names(value: str | None) -> tuple[str, ...]:
    """A ``;``-separated list, trimmed, with blanks and duplicates dropped.

    Duplicates are collapsed rather than rejected: naming somebody twice is a
    typist's slip, not a different rule, and the services would collapse it
    anyway.
    """
    if not value:
        return ()
    seen: dict[str, str] = {}
    for part in value.split(";"):
        name = part.strip()
        if name and name.casefold() not in seen:
            seen[name.casefold()] = name
    return tuple(seen.values())


def _optional_positive(value: str | None, *, field_name: str, row: int,
                       default: int | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return default
    try:
        number = int(text)
    except ValueError:
        raise RuleImportError(
            f"row {row}: {field_name} must be a whole number or blank"
        ) from None
    if number <= 0:
        raise RuleImportError(
            f"row {row}: {field_name} must be positive, or blank for none."
            " Zero is not a rule -- it is the absence of one, which blank"
            " already says"
        )
    return number


def read_group_specs(path: Path) -> list[GroupSpec]:
    specs: list[GroupSpec] = []
    for index, row in enumerate(_rows(path, ("group", "members")), start=2):
        name = (row.get("group") or "").strip()
        if not name:
            raise RuleImportError(f"row {index}: group name is blank")
        specs.append(
            GroupSpec(
                name=name,
                max_per_event=_optional_positive(
                    row.get("max_per_event"), field_name="max_per_event",
                    row=index, default=None,
                ),
                member_names=_names(row.get("members")),
            )
        )
    return specs


def read_support_specs(path: Path) -> list[SupportSpec]:
    specs: list[SupportSpec] = []
    for index, row in enumerate(_rows(path, ("subject", "supporters")), start=2):
        subject = (row.get("subject") or "").strip()
        if not subject:
            raise RuleImportError(f"row {index}: subject name is blank")
        minimum = _optional_positive(
            row.get("min_supporters"), field_name="min_supporters",
            row=index, default=1,
        )
        supporters = _names(row.get("supporters"))
        if len(supporters) < minimum:
            raise RuleImportError(
                f"row {index}: {len(supporters)} supporter(s) named for a"
                f" requirement of {minimum}; this member could never be"
                " scheduled"
            )
        if subject.casefold() in {name.casefold() for name in supporters}:
            raise RuleImportError(
                f"row {index}: the subject appears in their own supporter set"
            )
        specs.append(
            SupportSpec(
                subject_name=subject, min_supporters=minimum,
                supporter_names=supporters,
            )
        )
    return specs


# --------------------------------------------------------------------------
# Applying them
# --------------------------------------------------------------------------


def _resolve_memberships(
    session: Session, *, ministry_id: int
) -> dict[str, MinistryMembership]:
    """``casefolded display name -> membership``, for this ministry only.

    Case-folding is the only normalisation applied. Anything cleverer --
    stripping middle names, matching initials, fuzzy distance -- would be
    guessing which person a head meant, and guessing wrong here silently
    configures a rule about somebody else.

    A name two people share is dropped rather than resolved: an ambiguous match
    is reported as unmatched, which sends a head to fix their file instead of
    letting this command pick one.
    """
    rows = session.execute(
        select(MinistryMembership, Person)
        .join(Person, Person.id == MinistryMembership.person_id)
        .where(MinistryMembership.ministry_id == ministry_id)
    ).all()
    by_name: dict[str, MinistryMembership | None] = {}
    for membership, person in rows:
        key = person.display_name.strip().casefold()
        by_name[key] = None if key in by_name else membership
    return {key: value for key, value in by_name.items() if value is not None}


def _require_one(session: Session, statement, *, what: str):
    """Exactly one row, or a domain error naming which lookup was ambiguous.

    Ambiguity is a real and ordinary state here: rehearsing an import creates a
    fresh church each time, so several churches hold a ministry called "Setup"
    and several people share a display name across them. Every lookup is
    therefore church-scoped, and a remaining ambiguity is reported as one --
    never resolved by picking a row, which would configure somebody else's
    schedule.
    """
    rows = session.execute(statement).scalars().all()
    if not rows:
        raise RuleImportError(f"no {what} matched the name supplied")
    if len(rows) > 1:
        raise RuleImportError(
            f"{len(rows)} rows matched the {what} name supplied; the name is"
            " ambiguous within this church, and this command will not choose"
            " one for you"
        )
    return rows[0]


def apply_rules(
    session: Session,
    *,
    actor: Person,
    ministry: Ministry,
    period: SchedulingPeriod,
    groups: list[GroupSpec],
    support: list[SupportSpec],
) -> ImportSummary:
    """Configure every supplied rule, in one transaction the caller commits.

    Never commits or rolls back, exactly like the services it calls: a run that
    raises part-way leaves the caller to discard the whole thing, so a
    half-configured ministry is not a state this can produce.
    """
    summary = ImportSummary(ministry_id=ministry.id, period_id=period.id)
    memberships = _resolve_memberships(session, ministry_id=ministry.id)

    def resolve(name: str, *, context: str) -> MinistryMembership:
        membership = memberships.get(name.strip().casefold())
        if membership is None:
            raise RuleImportError(
                f"{context}: a name did not match exactly one active or"
                " inactive membership of this ministry. Check the spelling in"
                " your file against the roster that was imported"
            )
        return membership

    for spec in groups:
        group = create_member_group(
            session, actor=actor, ministry=ministry, name=spec.name
        )
        session.flush()
        summary.groups_configured += 1
        for member_name in spec.member_names:
            membership = resolve(member_name, context=f"{GROUPS_FILE}")
            added = set_member_group_membership(
                session, actor=actor, member_group=group,
                membership=membership, is_member=True,
            )
            if added is not None:
                summary.group_members_added += 1
        session.flush()
        set_member_group_event_limit(
            session, actor=actor, member_group=group,
            scheduling_period=period, max_per_event=spec.max_per_event,
        )
        session.flush()
        if spec.max_per_event is None:
            summary.groups_uncapped += 1
        else:
            summary.groups_capped += 1

    for spec in support:
        subject = resolve(spec.subject_name, context=f"{SUPPORT_FILE}")
        supporters = [
            resolve(name, context=f"{SUPPORT_FILE}")
            for name in spec.supporter_names
        ]
        set_same_event_support_requirement(
            session, actor=actor, subject_membership=subject,
            scheduling_period=period, supporter_memberships=supporters,
            min_supporters=spec.min_supporters,
        )
        session.flush()
        summary.support_requirements_configured += 1
        summary.supporters_approved += len(supporters)

    return summary


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def git_ignored(path: Path) -> bool:
    """True when git would ignore ``path`` -- proven, never assumed.

    The same check every other local command makes, for the same reason: a file
    git can see is a file that can be committed by accident, and this one names
    real people.
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=path.parent if path.parent.exists() else Path.cwd(),
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-dir", required=True, type=Path,
        help=f"Directory holding {GROUPS_FILE} and/or {SUPPORT_FILE}."
             " Must be git-ignored.",
    )
    parser.add_argument(
        "--church-name", required=True,
        help="Every lookup is scoped to this church. Rehearsing an import"
             " creates a fresh church each time, so a ministry or a person"
             " name is only unambiguous within one.",
    )
    parser.add_argument("--ministry-name", required=True)
    parser.add_argument("--period-name", required=True)
    parser.add_argument(
        "--actor-name", required=True,
        help="The Admin or Ministry Head to act as. Every change is audited"
             " against them, exactly as it would be from the product.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    input_dir: Path = args.input_dir.resolve()
    if not input_dir.is_dir():
        print(f"BLOCKED: {args.input_dir} is not a directory.", file=sys.stderr)
        return 2
    if not git_ignored(input_dir):
        print(
            "BLOCKED: the input directory is not git-ignored, so reading it"
            " could put real records where they can be committed. Nothing was"
            " read.",
            file=sys.stderr,
        )
        return 2

    groups_path = input_dir / GROUPS_FILE
    support_path = input_dir / SUPPORT_FILE
    if not groups_path.is_file() and not support_path.is_file():
        print(
            f"BLOCKED: neither {GROUPS_FILE} nor {SUPPORT_FILE} is present in"
            " that directory, so there is nothing to configure.",
            file=sys.stderr,
        )
        return 2

    env = os.environ
    database_url = env.get(demo_guards.DATABASE_URL) or None
    outcome = demo_guards.evaluate(
        env,
        database_url=database_url,
        test_database_url=(
            env.get("TEST_DATABASE_URL")
            or read_env_file(_REPO_ROOT / ".env.test").get("TEST_DATABASE_URL")
        ),
    )
    if not outcome.allowed:
        print("Refusing to configure:")
        for reason in outcome.reasons:
            print(f"  - {reason}")
        print()
        print("Nothing was read and nothing was written.")
        return 1

    assert database_url is not None  # guaranteed by the guards above

    try:
        groups = read_group_specs(groups_path) if groups_path.is_file() else []
        support = read_support_specs(support_path) if support_path.is_file() else []
    except RuleImportError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        print("Nothing was written.", file=sys.stderr)
        return 2

    engine = create_engine(database_url, pool_pre_ping=True)
    try:
        with Session(engine) as session:
            try:
                church = _require_one(
                    session,
                    select(Church).where(Church.name == args.church_name),
                    what="church",
                )
                ministry = _require_one(
                    session,
                    select(Ministry).where(
                        Ministry.church_id == church.id,
                        Ministry.name == args.ministry_name,
                    ),
                    what="ministry in that church",
                )
                period = _require_one(
                    session,
                    select(SchedulingPeriod).where(
                        SchedulingPeriod.ministry_id == ministry.id,
                        SchedulingPeriod.name == args.period_name,
                    ),
                    what="scheduling period in that ministry",
                )
                actor = _require_one(
                    session,
                    select(Person).where(
                        Person.church_id == church.id,
                        Person.display_name == args.actor_name,
                    ),
                    what="person in that church to act as",
                )
                summary = apply_rules(
                    session, actor=actor, ministry=ministry, period=period,
                    groups=groups, support=support,
                )
            except (RuleImportError, ServiceError) as error:
                session.rollback()
                print(f"Refusing to continue: {error}")
                print("Nothing was written.")
                return 1
            session.commit()
    finally:
        engine.dispose()

    _report(summary)
    return 0


def _report(summary: ImportSummary) -> None:
    """Counts and ids only -- checked by a test, not by eye."""
    print("Rule configuration complete.")
    print()
    print(f"  Ministry id:          {summary.ministry_id}")
    print(f"  Scheduling period id: {summary.period_id}")
    print()
    print(
        f"  {summary.groups_configured} member group(s),"
        f" {summary.group_members_added} membership(s) added to them"
    )
    print(
        f"  {summary.groups_capped} group(s) capped per event,"
        f" {summary.groups_uncapped} left uncapped"
    )
    print(
        f"  {summary.support_requirements_configured} same-event support"
        f" requirement(s), {summary.supporters_approved} approved supporter(s)"
    )
    for warning in summary.warnings:
        print(f"  - {warning}")
    print()
    print("These rules are hard and non-overridable. A draft that already")
    print("breaks one is left exactly as it is and reported as unfinalizable;")
    print("nothing here deleted an assignment.")


if __name__ == "__main__":
    sys.exit(main())
