"""Explicitly reusing an existing Person when importing another ministry.

The problem, stated plainly
---------------------------
:func:`scripts.local_ministry_import._people_and_memberships` creates a **new
Person for every volunteer in every import**, unconditionally. It never looks
for an existing one -- deliberately, because the source files carry names and
nothing else, and matching humans by name is exactly the mistake that puts one
person's schedule in front of another.

That is correct and safe in isolation, and wrong the moment a second ministry is
imported: somebody who serves in both Setup and AV gets **two Person rows**, one
per import. The development database shows this happening nine times over -- nine
identically-named rows from nine rehearsal imports.

Why it matters more now than it did
-----------------------------------
Before Task 77 a duplicated Person was untidy. Now it is a correctness bug in a
user-facing screen: ``/api/v1/me/schedule`` aggregates through the canonical
``person_id`` (ADR 0001), so two rows for one human split their schedule in two
-- and **each half looks complete**. The volunteer sees their Setup commitments,
no error, and no hint that their AV commitments exist under another id. There is
no way to notice that from inside the application.

What this module does, and refuses to do
----------------------------------------
It reads an **operator-written** file saying "this name in this source file is
already Person 22". That is a human assertion recorded in advance, not an
inference: the importer never compares names to decide, and nothing here
searches for candidates, scores similarity, or offers suggestions.

**Fail-safe in both directions**, which is the property worth having:

- Absent mapping, the importer behaves exactly as before -- a new Person per
  volunteer. Under-mapping produces a duplicate, which is visible, inert, and
  fixable by mapping it and re-importing into a fresh ministry.
- Every *supplied* mapping must be provably correct before a row is written: the
  Person must exist, be in the same church, be active, be named only once, and
  not already be a member of the ministry being imported. Any doubt is a refusal.

The asymmetry is the design. Getting a mapping wrong merges two humans into one
identity and mixes their schedules, which is far worse than the duplicate it was
meant to prevent -- so a mapping is accepted only when it cannot be wrong, and
everything else is refused before anything is read.

**There is no merge tool here**, and that is deliberate too. Re-pointing existing
assignments, availability, qualifications and serving limits from one Person to
another is a data migration with its own audit story; inventing it as a side
effect of an import command is how history gets quietly rewritten. Mapping
prevents duplicates at the point they would be created. Repairing ones that
already exist is a separate, later, deliberate piece of work.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.core import MinistryMembership, Person

__all__ = [
    "PersonMappingError",
    "load_person_map",
    "resolve_person_map",
]

#: The two columns the file must have, in its header row.
SOURCE_NAME_COLUMN = "source_name"
PERSON_ID_COLUMN = "person_id"


class PersonMappingError(RuntimeError):
    """A mapping that cannot be proven correct. Always fatal, never a warning.

    Messages name the **source name** the operator wrote and the **Person id**
    they supplied, because those are what they need to fix. No message ever
    includes a Person's stored display name, so a typo'd id cannot be used to
    read back who that id belongs to.
    """


@dataclass(frozen=True, slots=True)
class PersonMap:
    """Source names the operator has declared to be existing people.

    Keyed by the **case-folded** source name, because that is how the importer
    already matches a volunteer to ``--head-name`` and the operator should not
    have to reproduce a spreadsheet's capitalization exactly. Case-folding a key
    the operator wrote is not the same as matching two humans by name: the
    mapping still says *which id*, and that is never guessed.
    """

    by_source_name: dict[str, int]

    def person_id_for(self, source_name: str) -> int | None:
        return self.by_source_name.get(source_name.strip().casefold())

    def __len__(self) -> int:
        return len(self.by_source_name)


def load_person_map(path: Path) -> PersonMap:
    """Parse the operator's mapping file.

    Two columns, ``source_name`` and ``person_id``. Parsing is strict on every
    axis a mistake could hide in -- a missing header, a blank name, a
    non-numeric or non-positive id, a name listed twice, an id listed twice --
    because a mapping file is short, hand-written, and entirely made of things
    that must be exactly right.

    :raises PersonMappingError: on any malformed or ambiguous row.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise PersonMappingError(f"could not read the person map: {error}") from None

    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise PersonMappingError("the person map is empty")

    header = {(name or "").strip().casefold() for name in reader.fieldnames}
    missing = {SOURCE_NAME_COLUMN, PERSON_ID_COLUMN} - header
    if missing:
        raise PersonMappingError(
            f"the person map needs the column(s): {', '.join(sorted(missing))}"
        )

    by_name: dict[str, int] = {}
    seen_ids: dict[int, str] = {}

    for line, row in enumerate(reader, start=2):
        normalized = {
            (key or "").strip().casefold(): (value or "").strip()
            for key, value in row.items()
        }
        raw_name = normalized.get(SOURCE_NAME_COLUMN, "")
        raw_id = normalized.get(PERSON_ID_COLUMN, "")

        if not raw_name and not raw_id:
            continue  # a blank line is not a mistake

        if not raw_name:
            raise PersonMappingError(f"line {line}: {SOURCE_NAME_COLUMN} is blank")
        if not raw_id:
            raise PersonMappingError(
                f"line {line}: {PERSON_ID_COLUMN} is blank for {raw_name!r}."
                " Remove the row rather than leaving it half-filled."
            )
        # Strict, like every other id this project parses: int() would accept
        # "+5", " 5" and "5_0", none of which anybody meant to type.
        if not raw_id.isascii() or not raw_id.isdigit() or int(raw_id) <= 0:
            raise PersonMappingError(
                f"line {line}: {PERSON_ID_COLUMN} {raw_id!r} is not a positive"
                " whole number"
            )

        key = raw_name.casefold()
        person_id = int(raw_id)

        if key in by_name:
            raise PersonMappingError(
                f"line {line}: {raw_name!r} is mapped twice. One source name"
                " means one person."
            )
        if person_id in seen_ids:
            raise PersonMappingError(
                f"line {line}: person {person_id} is already mapped to"
                f" {seen_ids[person_id]!r}. Mapping two source names to one"
                " person would merge two volunteers into one identity."
            )

        by_name[key] = person_id
        seen_ids[person_id] = raw_name

    return PersonMap(by_source_name=by_name)


def resolve_person_map(
    session: Session,
    person_map: PersonMap,
    *,
    church_id: int,
    ministry_id: int,
    source_names: list[str],
) -> dict[str, Person]:
    """Check every mapping against the database, or refuse the whole import.

    Returns ``{case-folded source name: Person}``, and raises rather than
    returning a partial answer -- a half-applied mapping would import some
    volunteers onto existing people and duplicate the rest, which is the worst
    of both outcomes and the hardest to spot.

    Five checks, each closing a way a hand-written file goes wrong:

    1. **The Person exists.** A mistyped id is caught here rather than becoming
       a foreign-key error halfway through the import.
    2. **The Person is in this church.** Cross-church reuse is never meant, and
       the schema would not stop it.
    3. **The Person is active.** Importing a ministry onto somebody who has left
       is far more likely to be a stale mapping file than an intention.
    4. **The Person is not already in this ministry.** They would end up with two
       memberships in one ministry -- which the unique constraint refuses anyway,
       but late and with a database error rather than an explanation.
    5. **Every mapped name appears in the source.** A name that matches nothing
       is a typo, and silently ignoring it would leave a duplicate the operator
       believed they had prevented.

    :raises PersonMappingError: if any single mapping fails any check.
    """
    if not person_map.by_source_name:
        return {}

    available = {name.strip().casefold() for name in source_names}
    unmatched = sorted(set(person_map.by_source_name) - available)
    if unmatched:
        raise PersonMappingError(
            "the person map names volunteers that are not in this source file:"
            f" {', '.join(repr(name) for name in unmatched)}."
            " Check the spelling -- an unmatched row would silently create a"
            " duplicate person."
        )

    resolved: dict[str, Person] = {}
    for source_name, person_id in sorted(
        person_map.by_source_name.items(), key=lambda item: item[1]
    ):
        person = session.execute(
            select(Person).where(Person.id == person_id)
        ).scalar_one_or_none()

        if person is None:
            raise PersonMappingError(
                f"person {person_id} (mapped from {source_name!r}) does not exist"
            )
        if person.church_id != church_id:
            raise PersonMappingError(
                f"person {person_id} (mapped from {source_name!r}) belongs to a"
                " different church"
            )
        if person.deactivated_at is not None:
            raise PersonMappingError(
                f"person {person_id} (mapped from {source_name!r}) is"
                " deactivated. Reactivate them deliberately, or remove the"
                " mapping."
            )

        already = session.execute(
            select(func.count())
            .select_from(MinistryMembership)
            .where(
                MinistryMembership.person_id == person_id,
                MinistryMembership.ministry_id == ministry_id,
            )
        ).scalar_one()
        if already:
            raise PersonMappingError(
                f"person {person_id} (mapped from {source_name!r}) is already a"
                " member of this ministry. Importing again would give them a"
                " second membership in it."
            )

        resolved[source_name] = person

    return resolved
