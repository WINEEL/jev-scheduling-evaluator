"""The importer's role vocabulary as an injected value (Task 81).

Offline: no spreadsheet, no database.

**What this change is, and what it is not.** The shared source reader
(``scripts/historical_setup/csv_source.py``) is the reader the production
import path uses, and it reached for Setup's role names through module-level
imports -- so every ministry's source had to be Setup's. The vocabulary is now
a :class:`~scripts.role_vocabulary.RoleVocabulary` on ``SourceConfig``,
defaulting to Setup's.

That generalizes the **code**. It supplies no **data**: a vocabulary names the
positions a ministry staffs, a caller who knows those positions provides it,
nothing infers one from a sheet, an unknown label still fails closed, and who
may serve a position remains the Ministry Head's list and is nowhere here.

These tests hold both halves: that Setup reads exactly as it did, and that a
second ministry's vocabulary is genuinely usable by the same reader.
"""

from __future__ import annotations

import pytest

from scripts.historical_av.roles import (
    AV_LEAD,
    AV_STAFFING_ROLE_NAMES,
    AV_VOCABULARY,
    SHADOW_ROLE_NAME,
    UnknownAvRoleError,
    normalize_av_role_name,
)
from scripts.historical_setup.csv_source import SourceConfig, build_roles
from scripts.historical_setup.model import (
    CANONICAL_ROLE_NAMES,
    LEAD_ROLE_NAME,
    VARIETY_ROLE_NAMES,
)
from scripts.historical_setup.parsing import UnknownRoleError, normalize_role_name
from scripts.role_vocabulary import (
    SETUP_VOCABULARY,
    RoleVocabulary,
    build_roles_for,
)


# --------------------------------------------------------------------------
# Setup is unchanged
# --------------------------------------------------------------------------


def test_setup_is_still_the_default_vocabulary():
    """Every caller written before vocabularies existed reads what it read
    before, without being edited.
    """
    assert SourceConfig().role_vocabulary is SETUP_VOCABULARY
    assert SETUP_VOCABULARY.canonical_names == CANONICAL_ROLE_NAMES
    assert SETUP_VOCABULARY.lead_role_name == LEAD_ROLE_NAME
    assert SETUP_VOCABULARY.variety_role_names == VARIETY_ROLE_NAMES
    assert SETUP_VOCABULARY.normalize is normalize_role_name


def test_build_roles_still_produces_the_approved_five_setup_roles():
    roles = build_roles()

    assert tuple(role.name for role in roles) == CANONICAL_ROLE_NAMES
    assert tuple(role.role_id for role in roles) == (1, 2, 3, 4, 5)
    lead = [role for role in roles if role.is_lead]
    assert [role.name for role in lead] == [LEAD_ROLE_NAME]
    assert {role.name for role in roles if role.in_variety_set} == set(
        VARIETY_ROLE_NAMES
    )


def test_setups_generous_spelling_still_only_applies_to_role_columns():
    """``normalize`` reads a bare ``"3"`` as ``Setup 3`` -- right for a column
    already known to be a role. ``names_a_role`` is the separate, stricter
    question, and it still answers no for a person column.
    """
    assert SETUP_VOCABULARY.normalize("3") == "Setup 3"
    assert SETUP_VOCABULARY.names_a_role("Setup 3") is True
    assert SETUP_VOCABULARY.names_a_role("Volunteer 3") is False
    assert SETUP_VOCABULARY.names_a_role("3") is False


# --------------------------------------------------------------------------
# A second ministry's vocabulary works in the same reader
# --------------------------------------------------------------------------


def test_av_has_a_vocabulary_the_shared_reader_can_take():
    assert AV_VOCABULARY.canonical_names == AV_STAFFING_ROLE_NAMES
    assert AV_VOCABULARY.lead_role_name == AV_LEAD
    assert AV_VOCABULARY.normalize is normalize_av_role_name
    assert AV_VOCABULARY.unknown_role_error is UnknownAvRoleError
    config = SourceConfig(role_vocabulary=AV_VOCABULARY)
    assert config.role_vocabulary.canonical_names == AV_STAFFING_ROLE_NAMES


def test_shadow_is_not_a_position_the_reader_may_staff():
    """``Shadow`` is recorded by AV and is never a required position. A
    vocabulary lists what a Sunday must staff, so it is deliberately absent --
    which is what keeps ``default_headcount`` at four rather than five.
    """
    assert SHADOW_ROLE_NAME not in AV_VOCABULARY.canonical_names
    assert AV_VOCABULARY.default_headcount == 4


def test_av_has_no_variety_roles_and_that_is_a_decision_not_a_gap():
    """AV volunteers specialize; rotating them fights how the ministry works.
    The empty set means the preference cannot be switched on by accident.
    """
    assert AV_VOCABULARY.variety_role_names == ()
    assert not any(role.in_variety_set for role in build_roles_for(AV_VOCABULARY))


def test_the_two_vocabularies_do_not_read_each_others_sheets():
    """The property the separate normalizers were written for, preserved: a
    reader given one ministry's vocabulary cannot silently accept the other's
    column as a role.
    """
    assert SETUP_VOCABULARY.try_normalize("Slides") is None
    assert AV_VOCABULARY.try_normalize("Setup 3") is None
    assert AV_VOCABULARY.try_normalize("Slides") == "Slides"
    assert SETUP_VOCABULARY.try_normalize("Setup 3") == "Setup 3"


def test_an_unknown_label_still_fails_closed():
    """Generalizing which roles exist does not relax the rule that a label
    outside them stops the run rather than being guessed at.
    """
    with pytest.raises(UnknownRoleError):
        SETUP_VOCABULARY.normalize("Soundboard")
    with pytest.raises(UnknownAvRoleError):
        AV_VOCABULARY.normalize("Setup 4")


def test_each_vocabulary_declares_the_error_its_normalizer_raises():
    """``try_normalize`` catches exactly that error and nothing wider, so a
    genuine bug in a normalizer surfaces instead of being reported as an
    unrecognized column.
    """
    def broken(raw: str) -> str:
        raise ZeroDivisionError("a bug, not an unknown label")

    vocabulary = RoleVocabulary(
        canonical_names=("Only",), normalize=broken, role_words=("only",)
    )
    with pytest.raises(ZeroDivisionError):
        vocabulary.try_normalize("Only")


# --------------------------------------------------------------------------
# A vocabulary cannot describe something incoherent
# --------------------------------------------------------------------------


def test_a_vocabulary_needs_at_least_one_role():
    with pytest.raises(ValueError, match="at least one role"):
        RoleVocabulary(canonical_names=(), normalize=str, role_words=())


def test_role_names_must_be_unique():
    with pytest.raises(ValueError, match="unique"):
        RoleVocabulary(
            canonical_names=("A", "A"), normalize=str, role_words=("a",)
        )


def test_the_lead_must_be_one_of_the_roles():
    with pytest.raises(ValueError, match="not one of this"):
        RoleVocabulary(
            canonical_names=("A",), normalize=str, role_words=("a",),
            lead_role_name="B",
        )


def test_variety_roles_must_be_roles():
    with pytest.raises(ValueError, match="not roles of this"):
        RoleVocabulary(
            canonical_names=("A",), normalize=str, role_words=("a",),
            variety_role_names=("B",),
        )


def test_a_ministry_may_have_no_lead_at_all():
    """``None`` is a real answer: a ministry of distinct specialisms has no
    lead-qualified flag for a sheet to carry.
    """
    vocabulary = RoleVocabulary(
        canonical_names=("One", "Two"), normalize=str, role_words=("one", "two")
    )
    assert vocabulary.lead_role_name is None
    assert not any(role.is_lead for role in build_roles_for(vocabulary))


def test_unknown_role_names_sort_last_rather_than_raising():
    """Ordering is used to lay out a day's requirements; a stray name there is
    a reporting question, not a reason to abandon a run.
    """
    assert SETUP_VOCABULARY.order_of(LEAD_ROLE_NAME) == 0
    assert SETUP_VOCABULARY.order_of("Soundboard") > SETUP_VOCABULARY.order_of(
        CANONICAL_ROLE_NAMES[-1]
    )
