"""Local command: which source names are already people, and which are not.

    set -a && . ../.env.demo && set +a          # from backend/
    python scripts/reconcile_person_identities.py \
        --source-file ../local_data/<ministry>/source/<roster>.csv \
        --names-from first-column \
        --church-name "..." \
        --ministry-name "..." \
        --output ../local_data/reports/<ministry>_person_reconciliation.csv

**What this is for.** Task 77 established that one human is one Person, and
that a Person may hold several ministry memberships. So importing a *second*
ministry must reuse the Person rows the first import created, and the only
sanctioned way to say "this source name is Person 22" is the operator-written
map :mod:`scripts.person_mapping` reads. That file has to be written by hand,
and writing it against a 50-name roster with no help is where mistakes get
made.

This command is the help. It is a **worksheet generator**, not a matcher.

**It decides nothing, and it can decide nothing.** Every row it emits is a
question for a person to answer, and the only row type it can emit without a
question attached is one the operator has *already* answered in a person map.
Specifically:

- it never writes to the database -- every statement it issues is a SELECT;
- it never writes a person map, and its output is not one: the output carries a
  ``classification`` column a person reads, in a shape deliberately unlike the
  two-column ``source_name,person_id`` file the importer accepts;
- it never matches on a *similar* name. No initials, no nicknames, no edit
  distance, no "Josh is probably Joshua", and no history. The one comparison it
  makes is **exact equality after case-folding**, which is the same comparison
  :class:`scripts.person_mapping.PersonMap` already applies to keys the operator
  typed, and even that only ever *raises a candidate for review* -- it is
  reported as ``needs-human-confirmation``, never as a match.

That last restraint is the whole point. Guessing that two rows are the same
human is how one volunteer ends up reading another's schedule; guessing they
are different merely creates a duplicate, which is visible and fixable. So this
errs, always and only, toward asking.

**The four classifications.**

``explicitly-mapped``
    The operator's person map already names an id for this source name, and
    that id survives every check the importer itself would apply: the Person
    exists, is in this church, is active, and is not already a member of the
    ministry about to be imported. Nothing further is needed.
``new-person``
    No active Person in this church bears this name. The import will create
    one, which is the correct outcome for somebody genuinely new.
``needs-human-confirmation``
    Exactly one active Person in this church bears this name. That is a
    coincidence until a human says otherwise -- two people really can share a
    name -- so the candidate's **id** is reported for the operator to confirm
    or reject. Confirming means adding a row to the person map by hand.
``conflict-ambiguous``
    Something a person must resolve before importing at all: the name appears
    more than once in the source file, or more than one active Person bears it,
    or the person map names an id that fails one of the importer's checks.

**What it prints, and what it writes.** Counts to the terminal, never a name.
Names appear only in the ``--output`` file, which must be git-ignored and is
refused if it is not -- it pairs real names with internal ids, so it belongs
beside the roster rather than anywhere it could be committed.

Exit codes: 0 report written, 1 refused, 2 the source could not be read.
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# Running as a script rather than a module, so make ``app`` and ``scripts``
# importable from the backend directory regardless of the working directory.
_BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.models.core import (  # noqa: E402
    Church,
    Ministry,
    MinistryMembership,
    Person,
)
from scripts.person_mapping import (  # noqa: E402
    PersonMappingError,
    load_person_map,
)

EXPLICITLY_MAPPED = "explicitly-mapped"
NEW_PERSON = "new-person"
NEEDS_CONFIRMATION = "needs-human-confirmation"
CONFLICT = "conflict-ambiguous"

#: The order the report is written in, and the order the counts are printed in.
#: Conflicts first because they are the rows that stop an import.
CLASSIFICATIONS = (CONFLICT, NEEDS_CONFIRMATION, NEW_PERSON, EXPLICITLY_MAPPED)

OUTPUT_COLUMNS = (
    "classification",
    "source_name",
    "candidate_person_id",
    "note",
)


@dataclass(frozen=True, slots=True)
class Row:
    """One source name and the question, if any, it raises."""

    classification: str
    source_name: str
    candidate_person_id: str
    note: str


def git_ignored(path: Path) -> bool:
    """True when git would ignore ``path`` -- proven, never assumed.

    The same check every other local command makes, for the same reason: a file
    git can see is a file that can be committed by accident, and both this
    command's input and its output name real people.
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


def read_source_names(path: Path, names_from: str) -> tuple[list[str], int]:
    """The roster's names in file order, and how many cells were left behind.

    Two shapes, because the two pilot datasets genuinely have two: one lists
    volunteers down the first column with dates across the header, the other
    lists volunteers across the header with dates down the first column. The
    orientation is stated on the command line rather than sniffed -- guessing
    wrong here would read a column of dates as a roster.

    **A blank cell ends the roster, and the rest of the line is not read.** One
    of the pilot sheets carries the prepared assignments in the same rows as the
    availability answers, separated from them by an empty column, so its header
    line is a block of volunteer names followed by a block of *role* labels.
    Reading straight through would offer ``Soundboard`` as a person to reconcile
    -- and a role label that reached a person map would create a Person named
    after a job.

    Stopping at the separator is a rule about the shape of a spreadsheet, not an
    inference about anybody's identity: it is stated here, applied the same way
    every time, and the number of cells it skipped is reported so an operator
    can see it happened rather than discover a short roster later.

    The leading corner cell of a transposed grid is blank by construction and is
    skipped before any of this, so it never ends the roster on the first cell.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as error:
        raise SourceReadError(f"could not read the source file: {error}") from None

    rows = list(csv.reader(text.splitlines()))
    if not rows:
        raise SourceReadError("the source file is empty")

    if names_from == "header":
        cells = rows[0][1:]
    else:
        cells = [row[0] if row else "" for row in rows[1:]]

    names: list[str] = []
    for index, cell in enumerate(cells):
        if cell.strip() == "":
            return names, len(cells) - index
        names.append(cell.strip())
    return names, 0


class SourceReadError(RuntimeError):
    """The source could not be read without guessing at its shape."""


def classify(
    session: Session,
    *,
    source_names: list[str],
    church_name: str,
    ministry_name: str,
    person_map,
) -> list[Row]:
    """One :class:`Row` per distinct source name, in file order.

    Read-only: every statement issued from here is a SELECT.
    """
    church_id = session.execute(
        select(Church.id).where(Church.name == church_name)
    ).scalar_one_or_none()
    if church_id is None:
        # Not an error. A church that does not exist yet means a first import,
        # where nobody can already be a Person -- so every name is new, and
        # saying so is more useful than refusing.
        return [
            Row(NEW_PERSON, name, "", "no such church yet; this import creates it")
            for name in _distinct(source_names)
        ]

    ministry_id = session.execute(
        select(Ministry.id).where(
            Ministry.church_id == church_id, Ministry.name == ministry_name
        )
    ).scalar_one_or_none()

    duplicated = {
        folded
        for folded, count in Counter(
            name.strip().casefold() for name in source_names
        ).items()
        if count > 1
    }

    rows: list[Row] = []
    for name in _distinct(source_names):
        rows.append(
            _classify_one(
                session,
                name=name,
                church_id=church_id,
                ministry_id=ministry_id,
                person_map=person_map,
                duplicated=duplicated,
            )
        )
    return rows


def _distinct(names: list[str]) -> list[str]:
    """Source names, first spelling wins, order preserved."""
    seen: set[str] = set()
    kept: list[str] = []
    for name in names:
        folded = name.strip().casefold()
        if folded in seen:
            continue
        seen.add(folded)
        kept.append(name.strip())
    return kept


def _classify_one(
    session: Session,
    *,
    name: str,
    church_id: int,
    ministry_id: int | None,
    person_map,
    duplicated: set[str],
) -> Row:
    """Where one source name falls, and why.

    The person map is consulted **first**: an operator who has already answered
    the question must not be asked it again, and a mapped row that fails a check
    is a conflict worth reporting loudly rather than a candidate to re-review.
    """
    mapped_id = None if person_map is None else person_map.person_id_for(name)
    if mapped_id is not None:
        return _verify_mapping(
            session,
            name=name,
            person_id=mapped_id,
            church_id=church_id,
            ministry_id=ministry_id,
        )

    if name.strip().casefold() in duplicated:
        return Row(
            CONFLICT,
            name,
            "",
            "this name appears more than once in the source file",
        )

    candidates = _active_people_named(session, name=name, church_id=church_id)

    if not candidates:
        return Row(NEW_PERSON, name, "", "no active Person in this church bears this name")

    if len(candidates) > 1:
        return Row(
            CONFLICT,
            name,
            " ".join(str(person_id) for person_id in candidates),
            f"{len(candidates)} active people bear this name; only a human can say which,"
            " if either",
        )

    return Row(
        NEEDS_CONFIRMATION,
        name,
        str(candidates[0]),
        "one active Person bears this name -- confirm or reject by hand; a shared"
        " name is not an identity",
    )


def _active_people_named(session: Session, *, name: str, church_id: int) -> list[int]:
    """Ids of active people in this church whose name case-folds equal.

    **Exact equality, case-folded, and nothing else.** No trimming of middle
    names, no prefix matching, no nickname table. The comparison exists to
    surface a candidate for a human, and a looser one would surface the wrong
    human just as confidently.
    """
    statement = (
        select(Person.id)
        .where(
            Person.church_id == church_id,
            Person.deactivated_at.is_(None),
            func.lower(Person.display_name) == name.strip().casefold(),
        )
        .order_by(Person.id)
    )
    return list(session.execute(statement).scalars())


def _verify_mapping(
    session: Session,
    *,
    name: str,
    person_id: int,
    church_id: int,
    ministry_id: int | None,
) -> Row:
    """Re-apply the importer's own checks to one mapped id, before import day.

    Deliberately the same four conditions
    :func:`scripts.person_mapping.resolve_person_map` enforces, so a map that
    passes here is a map the importer will accept. Finding out at the worksheet
    stage costs a minute; finding out mid-import costs a refused run.
    """
    person = session.execute(
        select(Person).where(Person.id == person_id)
    ).scalar_one_or_none()

    if person is None:
        return Row(CONFLICT, name, str(person_id), "mapped to an id that does not exist")
    if person.church_id != church_id:
        return Row(CONFLICT, name, str(person_id), "mapped to a Person in another church")
    if person.deactivated_at is not None:
        return Row(CONFLICT, name, str(person_id), "mapped to a deactivated Person")

    if ministry_id is not None:
        already = session.execute(
            select(MinistryMembership.id).where(
                MinistryMembership.person_id == person_id,
                MinistryMembership.ministry_id == ministry_id,
            )
        ).scalar_one_or_none()
        if already is not None:
            return Row(
                CONFLICT,
                name,
                str(person_id),
                "mapped to a Person who is already a member of this ministry",
            )

    return Row(EXPLICITLY_MAPPED, name, str(person_id), "operator-declared and verified")


def write_report(path: Path, rows: list[Row]) -> None:
    """The worksheet, conflicts first.

    Written with :mod:`csv` rather than by joining strings because a display
    name may legitimately contain a comma, and a report that quietly corrupts
    one name is worse than no report.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    order = {name: index for index, name in enumerate(CLASSIFICATIONS)}
    ordered = sorted(rows, key=lambda row: (order[row.classification], row.source_name))

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(OUTPUT_COLUMNS)
        for row in ordered:
            writer.writerow(
                [row.classification, row.source_name, row.candidate_person_id, row.note]
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-file",
        required=True,
        type=Path,
        help="The roster CSV to reconcile. Must be git-ignored.",
    )
    parser.add_argument(
        "--names-from",
        choices=("first-column", "header"),
        default="first-column",
        help=(
            "Where the volunteer names are. Stated, never sniffed: guessing"
            " wrong reads a column of dates as a roster."
        ),
    )
    parser.add_argument("--church-name", required=True)
    parser.add_argument(
        "--ministry-name",
        required=True,
        help="The ministry about to be imported, so existing membership can be flagged.",
    )
    parser.add_argument(
        "--person-map",
        type=Path,
        default=None,
        help=(
            "An existing source_name,person_id map, if one has been started."
            " Rows it already answers are reported as explicitly-mapped and"
            " re-verified. Must be git-ignored."
        ),
    )
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="Where to write the worksheet. Must be git-ignored; it names real people.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    source_file: Path = args.source_file.resolve()
    if not source_file.is_file():
        print(f"BLOCKED: {args.source_file} is not a file.", file=sys.stderr)
        return 2
    for label, path in (("source file", source_file), ("output", args.output.resolve())):
        if not git_ignored(path):
            print(
                f"BLOCKED: the {label} is not git-ignored. It names real people, so"
                " it belongs beside the roster, not where it can be committed."
                " Nothing was read and nothing was written.",
                file=sys.stderr,
            )
            return 1

    person_map = None
    if args.person_map is not None:
        map_path: Path = args.person_map.resolve()
        if not git_ignored(map_path):
            print("BLOCKED: the person map is not git-ignored.", file=sys.stderr)
            return 1
        try:
            person_map = load_person_map(map_path)
        except PersonMappingError as error:
            print(f"BLOCKED: {error}", file=sys.stderr)
            return 1

    try:
        source_names, skipped = read_source_names(source_file, args.names_from)
    except SourceReadError as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2
    if not source_names:
        print("BLOCKED: no names found. Check --names-from.", file=sys.stderr)
        return 2

    engine = create_engine(get_settings().database_url, pool_pre_ping=True)
    with Session(engine) as session:
        rows = classify(
            session,
            source_names=source_names,
            church_name=args.church_name,
            ministry_name=args.ministry_name,
            person_map=person_map,
        )
        # Read-only by construction; the rollback states it rather than relying
        # on nothing above having written.
        session.rollback()

    write_report(args.output, rows)

    counts = Counter(row.classification for row in rows)
    print(f"Source names read:        {len(source_names)}")
    print(f"Distinct source names:    {len(rows)}")
    if skipped:
        print(
            f"Cells after the first blank, not read: {skipped}"
            " (the roster block ends there)"
        )
    for classification in CLASSIFICATIONS:
        print(f"  {classification:<26} {counts.get(classification, 0)}")
    print()
    print(f"Worksheet written to {args.output} (git-ignored). No name was printed here,")
    print("nothing was written to the database, and no match was accepted.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
