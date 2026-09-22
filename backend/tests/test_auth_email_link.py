"""Email normalization and the admin linking CLI (Task 76, Phases 1-2).

Offline: no PostgreSQL, no network. Every identity here is synthetic and uses
the reserved ``example.test`` domain -- no real address or name appears in this
file, and none may ever be added to it.

**Two things are being protected.**

*Normalization*, because the OAuth callback and this CLI must agree about what
"the same address" means. If they ever disagreed, the pilot would grow a class
of account that an admin had linked and nobody could sign in to. Both call
:func:`app.auth.email_link.normalize_email`, and the tests below pin what it
does -- and, just as deliberately, what it refuses to do.

*Ambiguity*, because the one genuinely dangerous thing this CLI could do is
write an address onto the wrong Person. Two people really do share a name, so
every path that could guess is tested to refuse instead.
"""

from __future__ import annotations

import argparse
import datetime

import pytest

from app.auth.email_link import EmailFormatError, normalize_email
from app.models.core import Person
from scripts import link_person_email as cli

UTC = datetime.timezone.utc
DEACTIVATED = datetime.datetime(2026, 1, 1, tzinfo=UTC)


def _person(person_id: int, name: str, *, email: str | None = None,
            deactivated: bool = False) -> Person:
    person = Person(display_name=name, church_id=1)
    person.id = person_id
    person.email = email
    if deactivated:
        person.deactivated_at = DEACTIVATED
    return person


class FakePeopleSession:
    """An in-memory stand-in for the people table.

    Matches the way the real queries behave -- case-insensitive on both the
    address and the display name -- so the CLI's refusals are exercised against
    the same semantics PostgreSQL would apply.
    """

    def __init__(self, *people: Person) -> None:
        self.people = list(people)
        self.flushes = 0

    def execute(self, statement):
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        if "lower(person.email)" in compiled:
            wanted = _literal(compiled, "lower(person.email) = lower(")
            return _Scalar(next(
                (p for p in self.people
                 if p.email is not None and p.email.casefold() == wanted.casefold()),
                None,
            ))
        if "lower(person.display_name)" in compiled:
            wanted = _literal(compiled, "lower(person.display_name) = lower(")
            return _Scalars([
                p for p in self.people
                if p.display_name.casefold() == wanted.casefold()
            ])
        if "person.email IS NOT NULL" in compiled:
            return _Scalars([p for p in self.people if p.email is not None])
        wanted_id = _literal(compiled, "person.id = ")
        return _Scalar(next((p for p in self.people if str(p.id) == wanted_id), None))

    def flush(self) -> None:
        self.flushes += 1

    def rollback(self) -> None: ...


def _literal(compiled: str, marker: str) -> str:
    rest = compiled.split(marker, 1)[1]
    if rest.startswith("'"):
        return rest[1:].split("'", 1)[0]
    return rest.split(")", 1)[0].split()[0].strip()


class _Scalar:
    def __init__(self, value): self._value = value
    def scalar_one_or_none(self): return self._value


class _Scalars:
    def __init__(self, rows): self._rows = rows
    def scalars(self): return self
    def all(self): return list(self._rows)


def _args(**fields) -> argparse.Namespace:
    defaults = {"person_id": None, "name": None, "email": None, "replace": False}
    return argparse.Namespace(**{**defaults, **fields})


# ==========================================================================
# 1-12 -- Normalization
# ==========================================================================


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("person@example.test", "person@example.test"),
        ("Person@Example.Test", "person@example.test"),
        ("PERSON@EXAMPLE.TEST", "person@example.test"),
        ("  person@example.test  ", "person@example.test"),
        ("\tperson@example.test\n", "person@example.test"),
    ],
)
def test_01_case_and_surrounding_whitespace_are_normalized(raw, expected):
    assert normalize_email(raw) == expected


def test_02_gmail_dots_are_deliberately_not_stripped():
    """**Under-normalizing fails safe; over-normalizing fails open.**

    Dot-insensitivity is Gmail's rule, not email's. Applying it here would
    resolve ``a.b@`` and ``ab@`` -- two different mailboxes at any domain that
    does not follow Google's convention -- to one Person, which is a way for
    somebody to be signed in as somebody else. The cost of not doing it is that
    an admin must link the exact address the person signs in with.
    """
    assert normalize_email("a.b@example.test") != normalize_email("ab@example.test")


def test_03_plus_tags_are_deliberately_not_stripped():
    assert normalize_email("person+ministry@example.test") == "person+ministry@example.test"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "person", "person@", "@example.test", "person@@example.test",
     "person@example", "person@.example.test", "person@example..test",
     "two people@example.test", "person@exa mple.test"],
)
def test_04_malformed_input_is_refused(raw):
    with pytest.raises(EmailFormatError):
        normalize_email(raw)


def test_05_non_text_is_refused():
    with pytest.raises(EmailFormatError):
        normalize_email(None)  # type: ignore[arg-type]


# ==========================================================================
# 6-14 -- Linking
# ==========================================================================


def test_06_linking_by_id_sets_the_normalized_address():
    person = _person(42, "Synthetic Person")
    session = FakePeopleSession(person)

    code = cli._link(session, _args(person_id=42, email="  Person@Example.TEST "))

    assert code == cli.EXIT_OK
    assert person.email == "person@example.test"


def test_07_linking_writes_by_id_even_when_found_by_name():
    """``--name`` finds the id; the id is what is written."""
    person = _person(42, "Synthetic Person")
    session = FakePeopleSession(person, _person(43, "Other Synthetic"))

    code = cli._link(session, _args(name="synthetic person", email="p@example.test"))

    assert code == cli.EXIT_OK
    assert person.email == "p@example.test"


def test_08_an_address_already_held_by_someone_else_is_refused(capsys):
    """One address, one Person -- the rule the database also enforces."""
    holder = _person(42, "Synthetic Person", email="shared@example.test")
    other = _person(43, "Other Synthetic")
    session = FakePeopleSession(holder, other)

    code = cli._link(session, _args(person_id=43, email="shared@example.test"))

    assert code == cli.EXIT_REFUSED
    assert other.email is None
    assert holder.email == "shared@example.test"
    assert "already linked to person 42" in capsys.readouterr().err


def test_09_a_duplicate_is_refused_case_insensitively(capsys):
    holder = _person(42, "Synthetic Person", email="shared@example.test")
    other = _person(43, "Other Synthetic")
    session = FakePeopleSession(holder, other)

    code = cli._link(session, _args(person_id=43, email="SHARED@EXAMPLE.TEST"))

    assert code == cli.EXIT_REFUSED
    assert other.email is None


def test_10_relinking_the_same_address_to_the_same_person_is_a_no_op(capsys):
    person = _person(42, "Synthetic Person", email="person@example.test")
    session = FakePeopleSession(person)

    code = cli._link(session, _args(person_id=42, email="Person@Example.test"))

    assert code == cli.EXIT_OK
    assert "Already linked" in capsys.readouterr().out
    assert session.flushes == 0


def test_11_changing_an_existing_address_requires_replace(capsys):
    """Overwriting the address somebody currently signs in with locks out the
    old one. It should have to be said out loud.
    """
    person = _person(42, "Synthetic Person", email="old@example.test")
    session = FakePeopleSession(person)

    code = cli._link(session, _args(person_id=42, email="new@example.test"))

    assert code == cli.EXIT_REFUSED
    assert person.email == "old@example.test"
    assert "--replace" in capsys.readouterr().err


def test_12_replace_changes_the_address_and_reports_the_previous_one(capsys):
    person = _person(42, "Synthetic Person", email="old@example.test")
    session = FakePeopleSession(person)

    code = cli._link(session, _args(person_id=42, email="new@example.test", replace=True))

    assert code == cli.EXIT_OK
    assert person.email == "new@example.test"
    assert "Was: old@example.test" in capsys.readouterr().out


def test_13_linking_a_deactivated_person_warns_but_proceeds(capsys):
    """Refusing would block preparing an account before a reactivation. The
    callback is what actually keeps them out, and it still does.
    """
    person = _person(42, "Synthetic Person", deactivated=True)
    session = FakePeopleSession(person)

    code = cli._link(session, _args(person_id=42, email="p@example.test"))

    assert code == cli.EXIT_OK
    assert person.email == "p@example.test"
    assert "deactivated" in capsys.readouterr().err


def test_14_a_malformed_address_is_refused_before_any_lookup():
    person = _person(42, "Synthetic Person")
    session = FakePeopleSession(person)

    with pytest.raises(EmailFormatError):
        cli._link(session, _args(person_id=42, email="not-an-address"))

    assert person.email is None


# ==========================================================================
# 15-20 -- Ambiguity and missing targets
# ==========================================================================


def test_15_an_ambiguous_name_is_refused_not_guessed(capsys):
    """**The one genuinely dangerous mistake this tool could make.**"""
    first = _person(42, "Synthetic Person")
    second = _person(43, "Synthetic Person")
    session = FakePeopleSession(first, second)

    code = cli._link(session, _args(name="Synthetic Person", email="p@example.test"))

    assert code == cli.EXIT_NOT_FOUND
    assert first.email is None and second.email is None
    error = capsys.readouterr().err
    assert "2 people share that display name" in error
    assert "42, 43" in error
    assert "--person-id" in error


def test_16_ambiguity_is_not_resolved_by_activity_state(capsys):
    """"The active one" would be a guess too, and would be wrong whenever the
    person being linked is the one on a break.
    """
    active = _person(42, "Synthetic Person")
    inactive = _person(43, "Synthetic Person", deactivated=True)
    session = FakePeopleSession(active, inactive)

    code = cli._link(session, _args(name="Synthetic Person", email="p@example.test"))

    assert code == cli.EXIT_NOT_FOUND
    assert active.email is None


def test_17_no_partial_or_fuzzy_name_matching_happens(capsys):
    person = _person(42, "Synthetic Person")
    session = FakePeopleSession(person)

    for attempt in ("Synthetic", "Synth", "synthetic per", "Synthetic  Person",
                    "Synthetic Persons", "Synthetik Person"):
        code = cli._link(session, _args(name=attempt, email="p@example.test"))
        assert code == cli.EXIT_NOT_FOUND, attempt
        assert person.email is None, attempt


def test_18_exact_name_matching_is_case_insensitive_and_trims():
    person = _person(42, "Synthetic Person")
    session = FakePeopleSession(person)

    assert cli._link(session, _args(name="  sYnThEtIc pErSoN  ", email="p@example.test")) == cli.EXIT_OK


def test_19_an_unknown_id_is_refused(capsys):
    session = FakePeopleSession(_person(42, "Synthetic Person"))

    code = cli._link(session, _args(person_id=999, email="p@example.test"))

    assert code == cli.EXIT_NOT_FOUND
    assert "no person has id 999" in capsys.readouterr().err


def test_20_no_person_is_ever_created_by_the_cli():
    """Checked against the module's code: `link` on an unknown target refuses,
    it does not invent somebody to link to.
    """
    import ast
    from pathlib import Path

    tree = ast.parse(Path(cli.__file__).read_text())
    constructed = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "Person" not in constructed
    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("add", "merge", "delete", "drop", "execute_many"):
        assert forbidden not in called, forbidden


# ==========================================================================
# 21-25 -- Unlinking and reading
# ==========================================================================


def test_21_unlinking_removes_the_address_and_nothing_else(capsys):
    person = _person(42, "Synthetic Person", email="person@example.test")
    person.is_admin = True
    session = FakePeopleSession(person)

    code = cli._unlink(session, _args(person_id=42))

    assert code == cli.EXIT_OK
    assert person.email is None
    # The Person is untouched otherwise: no deletion, no deactivation.
    assert person.display_name == "Synthetic Person"
    assert person.deactivated_at is None
    assert person.is_admin is True


def test_22_unlinking_someone_with_no_address_is_a_no_op(capsys):
    person = _person(42, "Synthetic Person")
    session = FakePeopleSession(person)

    assert cli._unlink(session, _args(person_id=42)) == cli.EXIT_OK
    assert "nothing to remove" in capsys.readouterr().out


def test_23_unlinking_refuses_an_ambiguous_name(capsys):
    first = _person(42, "Synthetic Person", email="a@example.test")
    second = _person(43, "Synthetic Person", email="b@example.test")
    session = FakePeopleSession(first, second)

    assert cli._unlink(session, _args(name="Synthetic Person")) == cli.EXIT_NOT_FOUND
    assert first.email == "a@example.test"
    assert second.email == "b@example.test"


def test_24_find_lists_every_exact_match_so_an_id_can_be_chosen(capsys):
    """Unlike a write, `find` is allowed to return several -- showing both
    candidates is exactly how the operator picks the right one.
    """
    session = FakePeopleSession(
        _person(42, "Synthetic Person"), _person(43, "Synthetic Person")
    )

    code = cli._find(session, _args(name="Synthetic Person"))

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert "[42]" in captured.out and "[43]" in captured.out
    assert "2 people share that display name" in captured.err


def test_25_list_shows_exactly_the_people_who_can_sign_in(capsys):
    session = FakePeopleSession(
        _person(42, "Synthetic Person", email="linked@example.test"),
        _person(43, "Unlinked Synthetic"),
    )

    code = cli._list(session, _args())

    captured = capsys.readouterr()
    assert code == cli.EXIT_OK
    assert "linked@example.test" in captured.out
    assert "Unlinked Synthetic" not in captured.out
    assert "1 linked." in captured.err


def test_26_show_reports_link_state_and_activity(capsys):
    session = FakePeopleSession(
        _person(42, "Synthetic Person", email="linked@example.test", deactivated=True)
    )

    cli._show(session, _args(person_id=42))

    out = capsys.readouterr().out
    assert "[42]" in out and "linked@example.test" in out and "DEACTIVATED" in out


def test_27_writes_are_marked_so_only_they_commit():
    """`main` commits only for commands that declare themselves writes, so a
    read can never leave a transaction open with unintended changes in it.
    """
    parser = cli._build_parser()

    assert parser.parse_args(["show", "--person-id", "1"]).writes is False
    assert parser.parse_args(["find", "--name", "X"]).writes is False
    assert parser.parse_args(["list"]).writes is False
    assert parser.parse_args(["link", "--person-id", "1", "--email", "a@example.test"]).writes is True
    assert parser.parse_args(["unlink", "--person-id", "1"]).writes is True


def test_28_the_cli_is_not_reachable_over_http():
    """It is a module under `scripts/`, imported by nothing in `app/`."""
    import ast
    from pathlib import Path

    # Checked against what each module **imports**, not against its text. A
    # docstring may legitimately point a reader at a scripts/ module -- Task 77
    # added exactly such a cross-reference, from app/services/my_schedule.py to
    # scripts/person_mapping.py, because the duplicate-person mechanism is the
    # thing a reader of that service most needs to know about. A substring
    # search would flag the documentation that explains the boundary.
    for module_file in Path("app").rglob("*.py"):
        module_tree = ast.parse(module_file.read_text())
        imported = [
            node.module or ""
            for node in ast.walk(module_tree)
            if isinstance(node, ast.ImportFrom)
        ] + [
            alias.name
            for node in ast.walk(module_tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        for name in imported:
            assert not name.startswith("scripts"), (module_file, name)

    # And it defines no route.
    tree = ast.parse(Path(cli.__file__).read_text())
    decorators = [
        ast.unparse(decorator)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        for decorator in node.decorator_list
    ]
    assert decorators == []


def test_29_every_example_address_in_the_tracked_files_is_synthetic():
    """Documentation and tests must never carry a real church address.

    Addresses are extracted with a pattern rather than searched for as
    substrings, so this cannot be satisfied -- or tripped up -- by the
    assertion text in this file itself.

    The rule checked is that every one ends in the **``.test`` top-level
    domain**, which RFC 6761 reserves and guarantees can never be resolved or
    registered. Checking the TLD rather than ``@example.test`` exactly is
    deliberate: this suite contains intentionally malformed inputs such as
    ``person@.example.test`` and ``person@example..test``, and both are still
    unmistakably synthetic. Any real address -- ``.com``, ``.org``, a school or
    a church domain -- fails this.
    """
    import re
    from pathlib import Path

    address = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    for path in (Path(cli.__file__), Path(__file__)):
        found = set(address.findall(path.read_text()))
        assert found, path.name  # the extraction works at all
        for candidate in found:
            assert candidate.casefold().endswith(".test"), (path.name, candidate)
