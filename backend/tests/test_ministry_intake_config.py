"""A ministry's source shape as a declaration (Task 82).

Offline: no spreadsheet, no database, no network.

The property every test here defends is the same one: **an unknown is a
refusal, not a default**. A shape the reader does not implement, a normalizer
that is not in the registry, a role alias pointing nowhere, a column given
both an index and a header -- each stops the run. The alternative, a config
that quietly accepts what it does not understand, is how a guess gets treated
as a mapping.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.ministry_intake.config import (
    IntakeConfigError,
    UnknownConfiguredRoleError,
    load_config,
    parse_config,
)

TEMPLATES = Path(__file__).resolve().parents[1] / "scripts" / "intake_templates"


def minimal(**overrides) -> dict:
    """The smallest config that loads. Tests break one thing at a time."""
    raw = {
        "ministry": {"label": "Example"},
        "roles": {
            "staffing": ["Lead", "Support"],
            "lead_role": "Lead",
        },
        "source": {
            "format": "csv",
            "shape": "date_major_grid",
            "year_hint": 2026,
            "grid": {
                "date_column": {"index": 0},
                "volunteer_columns": {
                    "mode": "explicit_range",
                    "first_index": 1,
                    "last_index": 3,
                },
                "role_columns": [
                    {"role": "Lead", "index": 4},
                    {"role": "Support", "index": 5},
                ],
            },
        },
        "availability": {"available": ["yes"], "unavailable": ["no"]},
    }
    for key, value in overrides.items():
        raw[key] = value
    return raw


def test_a_minimal_config_parses():
    config = parse_config(minimal())
    assert config.ministry_label == "Example"
    assert config.roles.staffing == ("Lead", "Support")
    assert config.shape == "date_major_grid"


# -- fail closed ---------------------------------------------------------


def test_an_unimplemented_shape_is_refused_rather_than_read_hopefully():
    raw = minimal()
    raw["source"]["shape"] = "whatever_this_file_is"
    with pytest.raises(IntakeConfigError, match="not a shape this reader"):
        parse_config(raw)


def test_a_ministry_with_no_role_list_cannot_be_configured():
    """Kids' exact situation: two spellings in circulation, neither approved."""
    raw = minimal()
    raw["roles"]["staffing"] = []
    raw["roles"]["lead_role"] = None
    raw["source"]["grid"]["role_columns"] = [{"role": "Lead", "index": 4}]
    with pytest.raises(IntakeConfigError, match="roles.staffing is empty"):
        parse_config(raw)


def test_an_unknown_normalizer_name_is_refused():
    raw = minimal()
    raw["normalizers"] = {"person_display": ["strip", "guess_the_surname"]}
    with pytest.raises(IntakeConfigError, match="unknown normalizer"):
        parse_config(raw)


def test_a_role_alias_pointing_at_a_role_the_ministry_lacks_is_refused():
    raw = minimal()
    raw["roles"]["aliases"] = {"sound": "Soundboard"}
    with pytest.raises(IntakeConfigError, match="does not have"):
        parse_config(raw)


def test_a_role_column_for_an_undeclared_role_is_refused():
    raw = minimal()
    raw["source"]["grid"]["role_columns"].append({"role": "Camera", "index": 6})
    with pytest.raises(IntakeConfigError, match="not in roles.staffing"):
        parse_config(raw)


def test_a_column_needs_exactly_one_of_index_or_header():
    raw = minimal()
    raw["source"]["grid"]["date_column"] = {"index": 0, "header": "Date"}
    with pytest.raises(IntakeConfigError, match="exactly one of index or header"):
        parse_config(raw)

    raw = minimal()
    raw["source"]["grid"]["date_column"] = {}
    with pytest.raises(IntakeConfigError, match="exactly one of index or header"):
        parse_config(raw)


def test_an_ignored_column_must_say_why():
    """'Ignored' with no reason is indistinguishable from 'forgotten'."""
    raw = minimal()
    raw["source"]["grid"]["ignore_columns"] = [{"index": 6}]
    with pytest.raises(IntakeConfigError, match="non-empty reason"):
        parse_config(raw)


def test_an_ambiguous_column_must_carry_the_question():
    raw = minimal()
    raw["source"]["grid"]["ambiguous_columns"] = [{"index": 6}]
    with pytest.raises(IntakeConfigError, match="non-empty question"):
        parse_config(raw)


def test_a_token_cannot_mean_two_things():
    raw = minimal()
    raw["availability"] = {"available": ["yes"], "unavailable": ["yes", "no"]}
    with pytest.raises(IntakeConfigError, match="both available and unavailable"):
        parse_config(raw)


def test_a_blank_cell_needs_a_declared_meaning():
    raw = minimal()
    raw["availability"]["blank"] = "probably fine"
    with pytest.raises(IntakeConfigError, match="availability.blank must be one of"):
        parse_config(raw)


def test_year_hint_has_no_default():
    """A quarter file omitting the year must not be read against 'now'."""
    raw = minimal()
    del raw["source"]["year_hint"]
    with pytest.raises(IntakeConfigError, match="year_hint"):
        parse_config(raw)


def test_an_unknown_key_is_refused_rather_than_ignored():
    raw = minimal()
    raw["source"]["grid"]["rolecolumns"] = []
    with pytest.raises(IntakeConfigError, match="unknown key"):
        parse_config(raw)


# -- role normalization --------------------------------------------------


def test_a_label_outside_the_vocabulary_fails_closed():
    config = parse_config(minimal())
    with pytest.raises(UnknownConfiguredRoleError):
        config.normalize_role_label("Soundboard")
    assert config.try_normalize_role_label("Soundboard") is None


def test_declared_aliases_normalize_and_nothing_else_does():
    raw = minimal()
    raw["roles"]["aliases"] = {"team lead": "Lead", "helper": "Support"}
    config = parse_config(raw)
    assert config.normalize_role_label("Team Lead") == "Lead"
    assert config.normalize_role_label("helper") == "Support"
    # Similar-looking is not the same as declared.
    assert config.try_normalize_role_label("leed") is None
    assert config.try_normalize_role_label("Team Leader") is None


def test_position_codes_and_service_times_are_stripped_when_declared():
    """The workbook's older tabs head a column '(AV1)\\nSound\\nboard\\n8:00 AM'."""
    raw = minimal()
    raw["roles"]["staffing"] = ["AV Lead", "Soundboard"]
    raw["roles"]["lead_role"] = "AV Lead"
    raw["roles"]["aliases"] = {"sound board": "Soundboard"}
    raw["source"]["grid"]["role_columns"] = [
        {"role": "AV Lead", "index": 4},
        {"role": "Soundboard", "index": 5},
    ]
    raw["normalizers"] = {
        "role_label": [
            "strip",
            "strip_position_code",
            "strip_time_of_day",
            "collapse_whitespace",
            "casefold",
        ]
    }
    config = parse_config(raw)
    assert config.normalize_role_label("(AV0)\nAV Lead\n8:00 AM") == "AV Lead"
    assert config.normalize_role_label("(AV1)\nSound\nboard\n8:00 AM") == "Soundboard"


def test_the_vocabulary_offered_to_the_shared_reader_excludes_non_staffing_roles():
    """A recorded-but-not-staffed role must never become a requirement."""
    raw = minimal()
    raw["roles"]["recorded_non_staffing"] = ["Shadow"]
    raw["source"]["grid"]["role_columns"].append({"role": "Shadow", "index": 6})
    config = parse_config(raw)
    vocabulary = config.vocabulary()
    assert vocabulary.canonical_names == ("Lead", "Support")
    assert "Shadow" not in vocabulary.canonical_names
    assert vocabulary.default_headcount == 2


# -- the shipped templates ----------------------------------------------


def test_the_av_example_configs_load_and_name_avs_four_positions():
    for name in ("av_quarter_csv.example.toml", "av_workbook.example.toml"):
        config = load_config(TEMPLATES / name)
        assert config.ministry_label == "AV"
        assert config.roles.staffing == ("AV Lead", "Soundboard", "Slides", "Video")
        assert config.roles.recorded_non_staffing == ("Shadow",)
        assert config.roles.variety_roles == ()


def test_the_av_example_configs_are_not_attested_because_nobody_has_confirmed_them():
    """Task 81 read this vocabulary off the sheets. That is not the Head saying so."""
    config = load_config(TEMPLATES / "av_quarter_csv.example.toml")
    assert config.roles.attestation.present is False


def test_the_kids_config_refuses_to_load_until_its_role_list_exists():
    """Two spellings are in circulation; picking one would be us deciding."""
    with pytest.raises(IntakeConfigError, match="roles.staffing is empty"):
        load_config(TEMPLATES / "kids_source_config.unfinished.toml")


def test_the_shipped_configs_carry_no_person_name():
    """Committable by construction: column positions and role names only."""
    for path in TEMPLATES.glob("*.toml"):
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        assert "ministry" in raw
        # No config key anywhere holds a roster: people enter through
        # git-ignored files, never through a committed one.
        assert "people" not in raw
        assert "volunteers" not in raw
        assert "qualifications" not in raw
