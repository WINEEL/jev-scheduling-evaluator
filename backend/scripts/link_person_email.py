"""Link an existing Person to the Google address they will sign in with.

**A local admin command, and still the tool for bulk work.** There is no public
registration, no invitation and no claim-your-profile page -- see
``docs/architecture/authentication.md``. A person can sign in when, and only
when, an administrator has already linked their address.

**Task 79 added a second way in, not a replacement.** An Admin can now link,
replace or remove an address from the People screen, through
:func:`app.services.person_directory.set_person_auth_link` -- which calls
:func:`app.auth.email_link.normalize_email` and
:func:`app.auth.email_link.find_person_by_email`, the same two functions this
command and the OAuth callback use, so the two paths cannot diverge. This
command remains because it does things the UI cannot: it runs against a
database from outside the application, it handles many people at once, and it
is the only way to link **the first Admin** -- who by definition cannot reach a
screen that requires an Admin to open.

Usage
-----
Every command is run from ``backend/`` with the virtualenv's Python::

    python -m scripts.link_person_email show   --person-id 42
    python -m scripts.link_person_email show   --email person@example.test
    python -m scripts.link_person_email find   --name "Synthetic Person"
    python -m scripts.link_person_email list
    python -m scripts.link_person_email link   --person-id 42 --email person@example.test
    python -m scripts.link_person_email link   --name "Synthetic Person" --email person@example.test
    python -m scripts.link_person_email link   --person-id 42 --email new@example.test --replace
    python -m scripts.link_person_email unlink --person-id 42

Three rules this tool will not bend
-----------------------------------
**Writes address a Person by internal id.** ``--name`` is a convenience for
*finding* that id, and when it is used for a write it must match exactly one
active-or-inactive Person after case-folding, or the command refuses. There is
no prefix matching, no fuzzy matching and no "did you mean". Two people
genuinely share a name, and guessing which one gets the account is not a
mistake that shows up until the wrong person is reading somebody's schedule.

**One address, one Person.** Linking an address another Person already holds is
refused, naming both ids so the operator can decide. The database enforces the
same rule through a unique index on ``lower(email)``, so a race between two
runs ends in an error rather than a duplicate.

**Changing an existing link requires ``--replace``.** Overwriting the address a
person currently signs in with is a different act from giving an address to
somebody who has none, and it locks the previous address out. It should have to
be said out loud.

On output, and on privacy
-------------------------
This command prints real names and real addresses -- that is what it is for, and
the operator is reading their own church's records. But its output is real
private data: it does not belong in a ticket, a chat message, a screenshot or a
commit. Nothing here writes a log file, and every example in this file and in
the tests uses a synthetic ``example.test`` identity.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.email_link import EmailFormatError, find_person_by_email, normalize_email
from app.config import get_settings, redact_url
from app.db import SessionLocal
from app.models.core import Person

#: Process exit codes. Distinct so a wrapper script can tell "refused" from
#: "crashed" without parsing text.
EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_NOT_FOUND = 3


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Shown before anything is read or written, and redacted to scheme, host and
    # database name -- never the credential. Linking is one of the few local
    # commands legitimately pointed at production, so which database it is
    # about to touch should never be a guess.
    settings = get_settings()
    print(f"Database: {redact_url(settings.database_url)}", file=sys.stderr)

    with SessionLocal() as session:
        try:
            exit_code = args.handler(session, args)
        except EmailFormatError as error:
            print(f"Refused: {error}.", file=sys.stderr)
            return EXIT_REFUSED

        if exit_code == EXIT_OK and getattr(args, "writes", False):
            session.commit()
        return exit_code


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _show(session: Session, args: argparse.Namespace) -> int:
    """Print one Person's identity and link state."""
    if args.email is not None:
        person = find_person_by_email(session, normalize_email(args.email))
        if person is None:
            print("No person carries that address.", file=sys.stderr)
            return EXIT_NOT_FOUND
    else:
        person = _person_by_id(session, args.person_id)
        if person is None:
            print(f"No person has id {args.person_id}.", file=sys.stderr)
            return EXIT_NOT_FOUND

    print(_describe(person))
    return EXIT_OK


def _find(session: Session, args: argparse.Namespace) -> int:
    """List every Person whose display name matches ``--name`` exactly.

    Exact after case-folding and trimming, and nothing looser. Several matches
    are listed rather than refused: finding the id is the whole point of this
    command, and seeing both candidates is how the operator picks the right one.
    """
    matches = _people_by_exact_name(session, args.name)
    if not matches:
        print("No person has that display name (matching is exact).", file=sys.stderr)
        return EXIT_NOT_FOUND

    if len(matches) > 1:
        print(
            f"{len(matches)} people share that display name."
            " Use --person-id to act on one of them.",
            file=sys.stderr,
        )
    for person in matches:
        print(_describe(person))
    return EXIT_OK


def _list(session: Session, args: argparse.Namespace) -> int:
    """Every Person who currently has an address, which is every Person who can sign in."""
    people = (
        session.execute(
            select(Person)
            .where(Person.email.isnot(None))
            .order_by(Person.display_name, Person.id)
        )
        .scalars()
        .all()
    )
    if not people:
        print("Nobody is linked yet, so nobody can sign in.", file=sys.stderr)
        return EXIT_OK

    for person in people:
        print(_describe(person))
    print(f"{len(people)} linked.", file=sys.stderr)
    return EXIT_OK


def _link(session: Session, args: argparse.Namespace) -> int:
    """Give a Person an address, or replace the one they have.

    The normalized address is what gets stored, so what an admin links and what
    the OAuth callback looks up are the same string produced by the same
    function.
    """
    email = normalize_email(args.email)

    person = _resolve_target(session, args)
    if person is None:
        return EXIT_NOT_FOUND

    holder = find_person_by_email(session, email)
    if holder is not None and holder.id != person.id:
        print(
            f"Refused: that address is already linked to person {holder.id}"
            f" ({holder.display_name}). One address belongs to one person;"
            " unlink it there first if it has moved.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    current = person.email
    if current is not None and current.casefold() == email:
        print(f"Already linked, unchanged.\n{_describe(person)}")
        return EXIT_OK

    if current is not None and not args.replace:
        print(
            f"Refused: person {person.id} is already linked to {current}."
            " Pass --replace to change it; the current address will stop"
            " working immediately.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    if person.deactivated_at is not None:
        # Allowed, but said out loud: the link is written and sign-in is still
        # refused at the callback, which checks the Person is active. Refusing
        # here would block preparing an account before somebody is reactivated.
        print(
            f"Note: person {person.id} is deactivated and will not be able to"
            " sign in until they are reactivated.",
            file=sys.stderr,
        )

    person.email = email
    try:
        session.flush()
    except IntegrityError:
        # The unique index on lower(email) caught what the lookup above could
        # not: another process linked this address between the two statements.
        session.rollback()
        print(
            "Refused: that address was linked to someone else while this"
            " command was running. Nothing was changed.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    was = current if current is not None else "(none)"
    print(f"Linked. Was: {was}\n{_describe(person)}")
    return EXIT_OK


def _unlink(session: Session, args: argparse.Namespace) -> int:
    """Remove a Person's address, which removes their ability to sign in.

    The Person row is untouched apart from that one column: no deletion, no
    deactivation, and every schedule, assignment and membership they have stays
    exactly as it was. Re-linking restores access.
    """
    person = _resolve_target(session, args)
    if person is None:
        return EXIT_NOT_FOUND

    if person.email is None:
        print(f"Person {person.id} has no linked address; nothing to remove.")
        return EXIT_OK

    removed = person.email
    person.email = None
    session.flush()
    print(f"Unlinked {removed}. They can no longer sign in.\n{_describe(person)}")
    return EXIT_OK


# --------------------------------------------------------------------------
# Lookup helpers
# --------------------------------------------------------------------------


def _resolve_target(session: Session, args: argparse.Namespace) -> Person | None:
    """The single Person a write is about, by id or by exact name.

    **Ambiguity is refused, never resolved.** When ``--name`` matches more than
    one Person this returns ``None`` and tells the operator to use ``--person-id``
    instead. Picking "the active one", "the first one" or "the one without an
    email" would each be a guess, and each would be wrong often enough to matter.
    """
    if args.person_id is not None:
        person = _person_by_id(session, args.person_id)
        if person is None:
            print(f"Refused: no person has id {args.person_id}.", file=sys.stderr)
        return person

    matches = _people_by_exact_name(session, args.name)
    if not matches:
        print(
            "Refused: no person has that display name. Matching is exact --"
            " no partial or approximate names are accepted.",
            file=sys.stderr,
        )
        return None
    if len(matches) > 1:
        ids = ", ".join(str(person.id) for person in matches)
        print(
            f"Refused: {len(matches)} people share that display name (ids:"
            f" {ids}). Re-run with --person-id to say which one is meant.",
            file=sys.stderr,
        )
        return None
    return matches[0]


def _person_by_id(session: Session, person_id: int) -> Person | None:
    return session.execute(
        select(Person).where(Person.id == person_id)
    ).scalar_one_or_none()


def _people_by_exact_name(session: Session, name: str) -> list[Person]:
    """Exact display-name matches, case-insensitive, ordered by id.

    ``lower()`` on both sides, so the comparison is the same one the database
    would use for an index and a stored name with unexpected capitalization
    still matches. Deactivated people are included: an operator may legitimately
    need to inspect or prepare one.
    """
    candidate = name.strip()
    if not candidate:
        return []
    return list(
        session.execute(
            select(Person)
            .where(func.lower(Person.display_name) == func.lower(candidate))
            .order_by(Person.id)
        )
        .scalars()
        .all()
    )


def _describe(person: Person) -> str:
    """One line per Person: id, name, link state, and whether they are active."""
    email = person.email if person.email is not None else "(not linked)"
    state = "active" if person.deactivated_at is None else "DEACTIVATED"
    admin = " admin" if person.is_admin else ""
    return f"  [{person.id}] {person.display_name} <{email}> {state}{admin}"


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.link_person_email",
        description=(
            "Link an existing Person to the Google address they sign in with."
            " Never creates a Person, never matches a name approximately."
        ),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    show = subcommands.add_parser("show", help="Show one person by id or address.")
    target = show.add_mutually_exclusive_group(required=True)
    target.add_argument("--person-id", type=int)
    target.add_argument("--email")
    show.set_defaults(handler=_show, writes=False)

    find = subcommands.add_parser(
        "find", help="Find people by exact display name (no fuzzy matching)."
    )
    find.add_argument("--name", required=True)
    find.set_defaults(handler=_find, writes=False)

    listing = subcommands.add_parser("list", help="List everyone who has a linked address.")
    listing.set_defaults(handler=_list, writes=False)

    link = subcommands.add_parser("link", help="Set or replace a person's address.")
    _add_target_arguments(link)
    link.add_argument("--email", required=True)
    link.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Required to change an address that is already set. The previous"
            " address stops working immediately."
        ),
    )
    link.set_defaults(handler=_link, writes=True)

    unlink = subcommands.add_parser(
        "unlink", help="Remove a person's address, revoking their sign-in."
    )
    _add_target_arguments(unlink)
    unlink.set_defaults(handler=_unlink, writes=True)

    return parser


def _add_target_arguments(subparser: argparse.ArgumentParser) -> None:
    """``--person-id`` or ``--name``, never both and never neither.

    Mutually exclusive and required, so a write always names exactly one way of
    identifying its target and the resolution rules above have one input.
    """
    target = subparser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--person-id", type=int, help="The internal Person id. Preferred for writes."
    )
    target.add_argument(
        "--name",
        help=(
            "Exact display name, case-insensitive. Refused if more than one"
            " person matches."
        ),
    )


if __name__ == "__main__":  # pragma: no cover - thin entry point
    raise SystemExit(main())
