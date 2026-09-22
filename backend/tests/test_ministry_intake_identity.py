"""Identity reconciliation: written down, never inferred (Task 82).

Offline, synthetic names only.

The asymmetry these tests defend is the one from ADR 0001 and Task 77. Failing
to reconcile somebody produces a duplicate Person -- visible, inert, fixable.
Reconciling somebody wrongly merges two humans into one identity and mixes
their schedules, and neither of them can tell from inside the app. So a
mapping is accepted only when it cannot be wrong, and **similarity is never a
reason for anything**.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from scripts.ministry_intake.identity import (
    IdentityFileError,
    load_decisions,
    reconcile,
)


def match_key(raw: str) -> str:
    return " ".join((raw or "").split()).casefold()


@dataclass(frozen=True)
class Person:
    display_name: str

    @property
    def match_key(self) -> str:
        return match_key(self.display_name)


def write(tmp_path, text: str):
    path = tmp_path / "identity.csv"
    path.write_text(text, encoding="utf-8")
    return path


HEADER = "source_name,decision,canonical_person_id,existing_ministry,note\n"


def test_a_well_formed_file_maps_creates_and_leaves_unresolved(tmp_path):
    path = write(
        tmp_path,
        HEADER
        + "Ann Placeholder,map_to_existing,101,Setup,also serves in Setup\n"
        + "Ben Placeholder,create_new,,,new\n"
        + "Cal Placeholder,unresolved,,,first name only\n",
    )
    decisions = load_decisions(path, match_key=match_key)
    result = reconcile(
        [Person("Ann Placeholder"), Person("Ben Placeholder"), Person("Cal Placeholder")],
        decisions,
    )
    assert result.resolved == {"ann placeholder": 101}
    assert result.to_create == ("Ben Placeholder",)
    assert result.unresolved == (("Cal Placeholder", "first name only"),)
    assert result.cross_ministry == {"Ann Placeholder": "Setup"}
    assert result.fully_reconciled is False


def test_one_person_may_belong_to_two_ministries_under_one_canonical_id(tmp_path):
    """Setup and AV importing the same human must reach the same Person.id."""
    path = write(tmp_path, HEADER + "Ann Placeholder,map_to_existing,101,Setup,\n")
    decisions = load_decisions(path, match_key=match_key)
    setup_side = reconcile([Person("Ann Placeholder")], decisions)
    av_side = reconcile([Person("ANN  PLACEHOLDER")], decisions)
    assert setup_side.resolved["ann placeholder"] == 101
    assert av_side.resolved["ann placeholder"] == 101


# -- nothing is ever merged by resemblance -------------------------------


def test_two_similar_names_are_two_people(tmp_path):
    """A bare first name and a full name beginning with it stay separate."""
    path = write(
        tmp_path,
        HEADER
        + "Ann,unresolved,,,bare first name -- could be either of two people\n"
        + "Ann Placeholder,map_to_existing,101,,\n",
    )
    decisions = load_decisions(path, match_key=match_key)
    result = reconcile([Person("Ann"), Person("Ann Placeholder")], decisions)
    assert result.resolved == {"ann placeholder": 101}
    assert [name for name, _reason in result.unresolved] == ["Ann"]


def test_case_is_the_only_difference_that_may_be_collapsed():
    result = reconcile([Person("Ann Placeholder"), Person("ANN PLACEHOLDER")], None)
    assert result.case_duplicates == (("ANN PLACEHOLDER", "Ann Placeholder"),)


def test_mapping_two_source_names_onto_one_person_is_refused(tmp_path):
    path = write(
        tmp_path,
        HEADER
        + "Ann Placeholder,map_to_existing,101,,\n"
        + "Anne Placeholder,map_to_existing,101,,\n",
    )
    with pytest.raises(IdentityFileError, match="merge two volunteers"):
        load_decisions(path, match_key=match_key)


def test_deciding_one_source_name_twice_is_refused(tmp_path):
    path = write(
        tmp_path,
        HEADER
        + "Ann Placeholder,map_to_existing,101,,\n"
        + "ann placeholder,create_new,,,\n",
    )
    with pytest.raises(IdentityFileError, match="decided twice"):
        load_decisions(path, match_key=match_key)


# -- silence is not consent ---------------------------------------------


def test_no_file_at_all_leaves_everybody_unresolved():
    result = reconcile([Person("Ann Placeholder"), Person("Ben Placeholder")], None)
    assert result.resolved == {}
    assert result.to_create == ()
    assert len(result.unresolved) == 2
    assert result.fully_reconciled is False


def test_a_person_with_no_row_is_unresolved_not_created(tmp_path):
    path = write(tmp_path, HEADER + "Ann Placeholder,create_new,,,\n")
    decisions = load_decisions(path, match_key=match_key)
    result = reconcile([Person("Ann Placeholder"), Person("Ben Placeholder")], decisions)
    assert result.to_create == ("Ann Placeholder",)
    assert [name for name, _ in result.unresolved] == ["Ben Placeholder"]


def test_a_row_naming_nobody_in_this_source_is_reported(tmp_path):
    path = write(
        tmp_path,
        HEADER
        + "Ann Placeholder,map_to_existing,101,,\n"
        + "Zoe Placeholder,map_to_existing,102,,\n",
    )
    decisions = load_decisions(path, match_key=match_key)
    result = reconcile([Person("Ann Placeholder")], decisions)
    assert result.not_in_source == ("Zoe Placeholder",)
    assert result.fully_reconciled is False


def test_a_row_about_somebody_who_appears_only_in_the_prepared_schedule_is_fine(
    tmp_path,
):
    path = write(
        tmp_path,
        HEADER
        + "Ann Placeholder,map_to_existing,101,,\n"
        + "Zoe Placeholder,unresolved,,,appears only in the roster block\n",
    )
    decisions = load_decisions(path, match_key=match_key)
    result = reconcile(
        [Person("Ann Placeholder")],
        decisions,
        also_known=frozenset({"zoe placeholder"}),
    )
    assert result.not_in_source == ()


# -- the file itself is strict ------------------------------------------


@pytest.mark.parametrize(
    "row, message",
    [
        ("Ann Placeholder,map_to_existing,,,\n", "canonical_person_id is blank"),
        ("Ann Placeholder,map_to_existing,abc,,\n", "not a positive whole number"),
        ("Ann Placeholder,map_to_existing,0,,\n", "not a positive whole number"),
        ("Ann Placeholder,create_new,101,,\n", "could mean either thing"),
        ("Ann Placeholder,unresolved,,,\n", "gives no note"),
        ("Ann Placeholder,probably_fine,,,\n", "is not one of"),
        (",map_to_existing,101,,\n", "source_name is blank"),
    ],
)
def test_a_half_edited_row_is_refused(tmp_path, row, message):
    with pytest.raises(IdentityFileError, match=message):
        load_decisions(write(tmp_path, HEADER + row), match_key=match_key)


def test_a_file_missing_its_columns_is_refused(tmp_path):
    with pytest.raises(IdentityFileError, match="needs the column"):
        load_decisions(write(tmp_path, "name,person\nAnn,1\n"), match_key=match_key)


def test_comment_lines_and_blank_rows_are_not_mistakes(tmp_path):
    path = write(
        tmp_path,
        "# how to fill this in\n" + HEADER + "\nAnn Placeholder,create_new,,,\n\n",
    )
    decisions = load_decisions(path, match_key=match_key)
    assert len(decisions) == 1
