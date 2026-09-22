"""The explicit canonical-person mapping (Task 77, items 9-11).

Offline: no PostgreSQL, no network. Every identity is synthetic.

**What this protects.** The importer creates a new Person per volunteer, so
importing a second ministry duplicates anybody who serves in both -- and since
``/api/v1/me/schedule`` aggregates through ``person_id``, a duplicate splits one
volunteer's schedule into two halves that each look complete.

The mapping file is the operator's assertion that a named volunteer is an
existing Person. These tests pin the two properties that make it safe to have:
**nothing is ever inferred from a name**, and **any doubt refuses the whole
import** rather than applying half of it.
"""

from __future__ import annotations

import pathlib

import pytest

from scripts.person_mapping import (
    PersonMap,
    PersonMappingError,
    load_person_map,
    resolve_person_map,
)


@pytest.fixture
def write(tmp_path):
    def _write(text: str) -> pathlib.Path:
        path = tmp_path / "person_map.csv"
        path.write_text(text, encoding="utf-8")
        return path
    return _write


# ==========================================================================
# 1-6 -- Parsing what the operator wrote
# ==========================================================================


def test_01_a_well_formed_map_is_read(write):
    result = load_person_map(
        write("source_name,person_id\nAlice Example,22\nBob Example,23\n")
    )

    assert result.by_source_name == {"alice example": 22, "bob example": 23}
    assert len(result) == 2


def test_02_lookup_is_case_and_whitespace_insensitive(write):
    """The operator should not have to reproduce a spreadsheet's capitalization.

    This case-folds a key *they wrote next to an explicit id* -- which is not
    the same as deciding two humans are the same person by name.
    """
    result = load_person_map(write("source_name,person_id\nAlice Example,22\n"))

    assert result.person_id_for("  ALICE EXAMPLE  ") == 22
    assert result.person_id_for("Alice Example") == 22
    assert result.person_id_for("Alice") is None


def test_03_blank_lines_are_not_mistakes(write):
    result = load_person_map(
        write("source_name,person_id\nAlice Example,22\n\n\nBob Example,23\n")
    )

    assert len(result) == 2


def test_04_a_missing_header_is_refused(write):
    with pytest.raises(PersonMappingError, match="needs the column"):
        load_person_map(write("Alice Example,22\n"))


@pytest.mark.parametrize("value", ["+1", "1_0", "1.0", "abc", "0", "-3", "٣", "1e2"])
def test_05_an_id_that_is_not_a_positive_whole_number_is_refused(write, value):
    """Strict, like every other id this project parses: ``int()`` accepts
    several of these and none of them is an id anybody meant to type. ``"٣"``
    is an Arabic-Indic three -- ``str.isdigit()`` is True for it, which is why
    the ASCII check is doing real work.
    """
    with pytest.raises(PersonMappingError, match="positive whole number"):
        load_person_map(write(f"source_name,person_id\nAlice Example,{value}\n"))


@pytest.mark.parametrize("value", [" 22", "22 ", "  22  "])
def test_05b_surrounding_whitespace_around_an_id_is_tolerated(write, value):
    """Ordinary spreadsheet padding, not a malformed id -- the same tolerance
    the dev-actor header parser has for the same reason.
    """
    result = load_person_map(write(f"source_name,person_id\nAlice Example,{value}\n"))

    assert result.person_id_for("Alice Example") == 22


def test_06_a_half_filled_row_is_refused_rather_than_skipped(write):
    """Silently skipping it would leave a duplicate the operator believed they
    had prevented.
    """
    with pytest.raises(PersonMappingError, match="is blank"):
        load_person_map(write("source_name,person_id\nAlice Example,\n"))


# ==========================================================================
# 7-8 -- Ambiguity is refused, never resolved
# ==========================================================================


def test_07_one_source_name_mapped_twice_is_refused(write):
    with pytest.raises(PersonMappingError, match="mapped twice"):
        load_person_map(
            write("source_name,person_id\nAlice Example,22\nalice example,23\n")
        )


def test_08_two_source_names_mapped_to_one_person_is_refused(write):
    """**The dangerous direction.** This would merge two volunteers into one
    identity, which is worse than the duplicate the mapping exists to prevent.
    """
    with pytest.raises(PersonMappingError, match="already mapped"):
        load_person_map(
            write("source_name,person_id\nAlice Example,22\nBob Example,22\n")
        )


# ==========================================================================
# 9-11 -- Resolution against the database
# ==========================================================================


def test_09_an_empty_map_resolves_to_nothing_and_touches_no_database(write):
    """The ordinary case. Absent a map the importer behaves exactly as before,
    and this must not so much as query.
    """
    class ExplodingSession:
        def execute(self, *_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("an empty map must not query the database")

    assert resolve_person_map(
        ExplodingSession(),
        PersonMap(by_source_name={}),
        church_id=1,
        ministry_id=1,
        source_names=["Alice Example"],
    ) == {}


def test_10_a_mapped_name_absent_from_the_source_is_refused(write):
    """A typo. Ignoring it would silently create the duplicate the operator was
    trying to prevent -- checked before any row is read.
    """
    class ExplodingSession:
        def execute(self, *_args, **_kwargs):  # pragma: no cover
            raise AssertionError("the source check must happen first")

    with pytest.raises(PersonMappingError, match="not in this source file"):
        resolve_person_map(
            ExplodingSession(),
            PersonMap(by_source_name={"alyce example": 22}),
            church_id=1,
            ministry_id=1,
            source_names=["Alice Example"],
        )


def test_11_no_message_ever_reveals_a_stored_display_name():
    """A mistyped id must not become a way to read back who that id belongs to.

    Checked against the module's source: every message is built from the source
    name the operator wrote and the id they supplied, never from ``person``.
    """
    import ast

    from scripts import person_mapping

    tree = ast.parse(pathlib.Path(person_mapping.__file__).read_text())
    raised = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
    ]
    assert raised
    for message in raised:
        assert "display_name" not in message
        assert "person.email" not in message


def test_12_the_module_never_searches_for_a_person_by_name():
    """**The rule Task 77 states outright: never auto-merge people by name.**

    Checked against the module's *identifiers and calls*, never its prose --
    the docstrings legitimately discuss name matching in order to explain why
    it is absent, so a substring search over the raw text would flag the very
    explanation that documents the rule.
    """
    import ast

    from scripts import person_mapping

    tree = ast.parse(pathlib.Path(person_mapping.__file__).read_text())

    # No comparison anywhere involves a stored display name.
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            assert "display_name" not in ast.unparse(node), ast.unparse(node)

    # No fuzzy-matching helper is imported or called.
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    for banned in ("difflib", "rapidfuzz", "fuzzywuzzy", "Levenshtein", "re"):
        assert banned not in imported, banned

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for banned in ("ilike", "like", "contains", "match", "startswith", "ratio"):
        assert banned not in called, banned

    # The only Person lookup is by primary key.
    person_wheres = [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "where"
        and "Person" in ast.unparse(node)
    ]
    assert person_wheres == ["select(Person).where(Person.id == person_id)"]
