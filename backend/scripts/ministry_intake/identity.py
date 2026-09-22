"""Identity reconciliation as an operator's written decision, never a guess.

The rule this module exists to enforce is the one from ADR 0001 and Task 77:
**one human is one canonical ``Person.id``**, and that identity is what
``/api/v1/me/schedule`` and the church-wide one-ministry-per-Sunday rule are
keyed on. Get it wrong by splitting one person in two and their schedule shows
up half-complete with no error anywhere; get it wrong by merging two people and
one volunteer sees another's commitments.

So nothing here compares names for similarity. There is no scorer, no
suggester, no "did you mean". A source name is reconciled because a person
wrote down which canonical id it is, in a file, in advance. The only automatic
step is case-folding the operator's own keys, so they need not reproduce a
spreadsheet's capitalization exactly -- and case-folding a key is not the same
as deciding two humans are one, because the row still states *which id*.

The file
--------
CSV, one row per source person::

    source_name,decision,canonical_person_id,existing_ministry,note
    A. Placeholder,map_to_existing,22,Setup,already serves in Setup
    B. Placeholder,create_new,,,new to the directory
    C. Placeholder,unresolved,,,two people could be meant

``decision`` is one of:

``map_to_existing``
    This source name is the canonical Person with this id. Requires the id.
    ``existing_ministry`` is optional and reporting-only -- it is how the
    dry-run can say "this many people are shared with another ministry".

``create_new``
    Nobody in the directory is this person yet. Requires the id to be blank,
    so a half-edited row cannot mean two things.

``unresolved``
    Known to be undecided. Requires a note saying why, and keeps the ministry
    blocked -- which is the point: an undecided identity should stop an import,
    loudly, rather than quietly become a new Person.

A source person with **no row at all** is treated exactly like ``unresolved``.
Silence is not consent to create somebody.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "IdentityFileError",
    "DECISIONS",
    "MAP_TO_EXISTING",
    "CREATE_NEW",
    "UNRESOLVED",
    "IdentityDecision",
    "IdentityDecisions",
    "Reconciliation",
    "load_decisions",
    "reconcile",
    "TEMPLATE_HEADER",
]


class IdentityFileError(RuntimeError):
    """A reconciliation file that cannot be trusted. Always fatal.

    Messages name the **source name** the operator wrote and the id they
    supplied, because those are what they have to fix. No message ever quotes
    a stored display name back, so a mistyped id cannot be used to read who
    that id belongs to.
    """


MAP_TO_EXISTING = "map_to_existing"
CREATE_NEW = "create_new"
UNRESOLVED = "unresolved"
DECISIONS = (MAP_TO_EXISTING, CREATE_NEW, UNRESOLVED)

SOURCE_NAME_COLUMN = "source_name"
DECISION_COLUMN = "decision"
PERSON_ID_COLUMN = "canonical_person_id"
MINISTRY_COLUMN = "existing_ministry"
NOTE_COLUMN = "note"

TEMPLATE_HEADER = (
    SOURCE_NAME_COLUMN,
    DECISION_COLUMN,
    PERSON_ID_COLUMN,
    MINISTRY_COLUMN,
    NOTE_COLUMN,
)

_REQUIRED_COLUMNS = {SOURCE_NAME_COLUMN, DECISION_COLUMN}


@dataclass(frozen=True, slots=True)
class IdentityDecision:
    """One line of the operator's file."""

    source_name: str
    match_key: str
    decision: str
    canonical_person_id: int | None = None
    existing_ministry: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class IdentityDecisions:
    """Every decision in one file, keyed by case-folded source name."""

    by_key: dict[str, IdentityDecision] = field(default_factory=dict)

    def get(self, match_key: str) -> IdentityDecision | None:
        return self.by_key.get(match_key)

    def __len__(self) -> int:
        return len(self.by_key)


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """What the file says about the people this source actually contains."""

    #: ``match_key -> canonical Person id``, for names the operator mapped.
    resolved: dict[str, int] = field(default_factory=dict)
    #: Source names the operator declared to be new people.
    to_create: tuple[str, ...] = ()
    #: Source names that are **not** reconciled: declared unresolved, or not
    #: mentioned in the file at all. Each carries the reason.
    unresolved: tuple[tuple[str, str], ...] = ()
    #: Rows naming somebody this source does not contain -- a typo, or a file
    #: left over from another source.
    not_in_source: tuple[str, ...] = ()
    #: ``match_key -> ministry`` for people the operator says already serve
    #: elsewhere. Reporting only; the id is what actually ties them together.
    cross_ministry: dict[str, str] = field(default_factory=dict)
    #: Source people whose display names differ only by case. Safe to call a
    #: duplicate -- case is the one difference that cannot mean two humans.
    case_duplicates: tuple[tuple[str, ...], ...] = ()

    @property
    def fully_reconciled(self) -> bool:
        return not self.unresolved and not self.not_in_source


def _clean(value: str | None) -> str:
    return (value or "").strip()


def load_decisions(path: Path, *, match_key) -> IdentityDecisions:
    """Parse the operator's file. Strict on every axis a mistake hides in.

    ``match_key`` is the config's own person-name normalizer, so the file is
    keyed exactly the way the source's roster columns are -- an operator should
    not have to reproduce a spreadsheet's spacing or capitalization.
    """
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as error:
        raise IdentityFileError(
            f"could not read the identity file: {error}"
        ) from None

    reader = csv.DictReader(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    if reader.fieldnames is None:
        raise IdentityFileError("the identity file is empty")
    header = {_clean(name).casefold() for name in reader.fieldnames}
    missing = _REQUIRED_COLUMNS - header
    if missing:
        raise IdentityFileError(
            f"the identity file needs the column(s): {', '.join(sorted(missing))}"
        )

    by_key: dict[str, IdentityDecision] = {}
    ids_seen: dict[int, str] = {}

    for line, raw in enumerate(reader, start=2):
        row = {
            _clean(key).casefold(): _clean(value) for key, value in raw.items() if key
        }
        name = row.get(SOURCE_NAME_COLUMN, "")
        decision = row.get(DECISION_COLUMN, "").casefold()
        person_id_text = row.get(PERSON_ID_COLUMN, "")
        note = row.get(NOTE_COLUMN, "")
        ministry = row.get(MINISTRY_COLUMN, "")

        if not any((name, decision, person_id_text, note, ministry)):
            continue  # a blank line is not a mistake

        if not name:
            raise IdentityFileError(f"line {line}: {SOURCE_NAME_COLUMN} is blank")
        if decision not in DECISIONS:
            raise IdentityFileError(
                f"line {line}: decision {decision!r} for {name!r} is not one of"
                f" {list(DECISIONS)}"
            )

        person_id: int | None = None
        if decision == MAP_TO_EXISTING:
            if not person_id_text:
                raise IdentityFileError(
                    f"line {line}: {name!r} is mapped to an existing person but"
                    f" {PERSON_ID_COLUMN} is blank"
                )
            if (
                not person_id_text.isascii()
                or not person_id_text.isdigit()
                or int(person_id_text) <= 0
            ):
                raise IdentityFileError(
                    f"line {line}: {PERSON_ID_COLUMN} {person_id_text!r} for"
                    f" {name!r} is not a positive whole number"
                )
            person_id = int(person_id_text)
        elif person_id_text:
            raise IdentityFileError(
                f"line {line}: {name!r} is {decision} but also supplies"
                f" {PERSON_ID_COLUMN} {person_id_text!r}. A half-edited row"
                " could mean either thing, so it means neither."
            )

        if decision == UNRESOLVED and not note:
            raise IdentityFileError(
                f"line {line}: {name!r} is unresolved but gives no note."
                " Say what is undecided, so somebody can decide it."
            )

        key = match_key(name)
        if not key:
            raise IdentityFileError(f"line {line}: {name!r} normalizes to nothing")
        if key in by_key:
            raise IdentityFileError(
                f"line {line}: {name!r} is decided twice. One source name is"
                " one person."
            )
        if person_id is not None:
            if person_id in ids_seen:
                raise IdentityFileError(
                    f"line {line}: person {person_id} is already mapped to"
                    f" {ids_seen[person_id]!r}. Mapping two source names onto"
                    " one id would merge two volunteers into one identity."
                )
            ids_seen[person_id] = name

        by_key[key] = IdentityDecision(
            source_name=name,
            match_key=key,
            decision=decision,
            canonical_person_id=person_id,
            existing_ministry=ministry,
            note=note,
        )

    return IdentityDecisions(by_key=by_key)


def reconcile(
    people,
    decisions: IdentityDecisions | None,
    *,
    also_known: frozenset[str] = frozenset(),
) -> Reconciliation:
    """Apply the file to the people this source actually contains.

    ``people`` is any iterable of objects with ``display_name`` and
    ``match_key`` -- :class:`~scripts.ministry_intake.reader.SourcePerson` in
    practice. Absent a file, **every** source person is unresolved: the default
    is not "create them all", because creating a Person is the step that cannot
    be undone by re-running.

    ``also_known`` holds match keys that are in the source but not in the
    roster block -- names that appear only in the prepared schedule. A row
    about one of those is a legitimate decision, not a typo, so it is not
    reported as naming nobody.
    """
    roster = list(people)
    resolved: dict[str, int] = {}
    to_create: list[str] = []
    unresolved: list[tuple[str, str]] = []
    cross_ministry: dict[str, str] = {}

    by_key = decisions.by_key if decisions is not None else {}

    for person in roster:
        decision = by_key.get(person.match_key)
        if decision is None:
            unresolved.append(
                (person.display_name, "no row in the identity reconciliation file")
            )
            continue
        if decision.decision == MAP_TO_EXISTING:
            resolved[person.match_key] = decision.canonical_person_id  # type: ignore[assignment]
            if decision.existing_ministry:
                cross_ministry[person.display_name] = decision.existing_ministry
        elif decision.decision == CREATE_NEW:
            to_create.append(person.display_name)
        else:
            unresolved.append((person.display_name, decision.note))

    roster_keys = {person.match_key for person in roster} | set(also_known)
    not_in_source = tuple(
        sorted(
            decision.source_name
            for key, decision in by_key.items()
            if key not in roster_keys
        )
    )

    # Case-only duplicates are the one collapse that is safe to make
    # automatically: two spellings differing by capitalization alone cannot be
    # two different humans. Anything less exact is left as two people.
    groups: dict[str, list[str]] = {}
    for person in roster:
        groups.setdefault(person.match_key, []).append(person.display_name)
    case_duplicates = tuple(
        tuple(sorted(names)) for names in groups.values() if len(set(names)) > 1
    )

    return Reconciliation(
        resolved=resolved,
        to_create=tuple(sorted(to_create)),
        unresolved=tuple(sorted(unresolved)),
        not_in_source=not_in_source,
        cross_ministry=cross_ministry,
        case_duplicates=case_duplicates,
    )
