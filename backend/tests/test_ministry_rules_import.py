"""The local rule-configuration command (Task 74).

Offline: no database, no network. What is tested here is the part that decides
*what* to configure -- reading two small CSVs and turning them into specs -- and
the two properties that keep a local command holding real names safe: it refuses
a directory git can see, and it prints no name on any path.

The part that *writes* is the services, which have their own coverage offline
and against real PostgreSQL. It is not re-tested here.

Every name in this file is synthetic.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from scripts import import_ministry_rules as command


def _write(tmp_path: Path, name: str, rows: list[dict], columns: list[str]) -> Path:
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


# ==========================================================================
# member_groups.csv
# ==========================================================================


def test_a_group_row_becomes_a_spec(tmp_path):
    path = _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A", "max_per_event": "2",
          "members": "Ada Example;Bo Example"}],
        ["group", "max_per_event", "members"],
    )

    (spec,) = command.read_group_specs(path)

    assert spec.name == "Category A"
    assert spec.max_per_event == 2
    assert spec.member_names == ("Ada Example", "Bo Example")


def test_a_blank_cap_means_no_cap_never_zero(tmp_path):
    """Blank is "record the group, do not cap it". A zero would be a second
    spelling of a fact that already has one, and the database cannot store it.
    """
    path = _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A", "max_per_event": "", "members": "Ada Example"}],
        ["group", "max_per_event", "members"],
    )

    (spec,) = command.read_group_specs(path)

    assert spec.max_per_event is None


@pytest.mark.parametrize("value", ["0", "-1"])
def test_a_non_positive_cap_is_refused_with_the_row_number(tmp_path, value):
    path = _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A", "max_per_event": value, "members": "Ada Example"}],
        ["group", "max_per_event", "members"],
    )

    with pytest.raises(command.RuleImportError, match="row 2"):
        command.read_group_specs(path)


def test_a_non_numeric_cap_is_refused(tmp_path):
    path = _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A", "max_per_event": "two", "members": "Ada Example"}],
        ["group", "max_per_event", "members"],
    )

    with pytest.raises(command.RuleImportError, match="whole number"):
        command.read_group_specs(path)


def test_a_blank_group_name_is_refused(tmp_path):
    path = _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "  ", "max_per_event": "2", "members": "Ada Example"}],
        ["group", "max_per_event", "members"],
    )

    with pytest.raises(command.RuleImportError, match="group name is blank"):
        command.read_group_specs(path)


def test_a_missing_column_is_refused_by_name(tmp_path):
    path = _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A"}], ["group"],
    )

    with pytest.raises(command.RuleImportError, match="members"):
        command.read_group_specs(path)


def test_an_empty_member_list_is_allowed(tmp_path):
    """A group with nobody in it caps nothing, and recording it first and
    filling it later is a perfectly ordinary order to work in.
    """
    path = _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A", "max_per_event": "2", "members": ""}],
        ["group", "max_per_event", "members"],
    )

    (spec,) = command.read_group_specs(path)

    assert spec.member_names == ()


def test_duplicate_and_blank_names_are_collapsed(tmp_path):
    path = _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A", "max_per_event": "2",
          "members": " Ada Example ;;ada example;Bo Example"}],
        ["group", "max_per_event", "members"],
    )

    (spec,) = command.read_group_specs(path)

    assert spec.member_names == ("Ada Example", "Bo Example")


# ==========================================================================
# support_requirements.csv
# ==========================================================================


def test_a_support_row_becomes_a_spec(tmp_path):
    path = _write(
        tmp_path, command.SUPPORT_FILE,
        [{"subject": "Dee Example", "min_supporters": "1",
          "supporters": "Ada Example;Bo Example"}],
        ["subject", "min_supporters", "supporters"],
    )

    (spec,) = command.read_support_specs(path)

    assert spec.subject_name == "Dee Example"
    assert spec.min_supporters == 1
    assert spec.supporter_names == ("Ada Example", "Bo Example")


def test_a_blank_count_means_one(tmp_path):
    path = _write(
        tmp_path, command.SUPPORT_FILE,
        [{"subject": "Dee Example", "min_supporters": "",
          "supporters": "Ada Example"}],
        ["subject", "min_supporters", "supporters"],
    )

    (spec,) = command.read_support_specs(path)

    assert spec.min_supporters == 1


def test_fewer_supporters_than_the_count_is_refused(tmp_path):
    """It would mean the subject could never be scheduled -- a configuration
    mistake rather than a stricter rule.
    """
    path = _write(
        tmp_path, command.SUPPORT_FILE,
        [{"subject": "Dee Example", "min_supporters": "2",
          "supporters": "Ada Example"}],
        ["subject", "min_supporters", "supporters"],
    )

    with pytest.raises(command.RuleImportError, match="could never be scheduled"):
        command.read_support_specs(path)


def test_an_empty_supporter_set_is_refused(tmp_path):
    path = _write(
        tmp_path, command.SUPPORT_FILE,
        [{"subject": "Dee Example", "min_supporters": "", "supporters": ""}],
        ["subject", "min_supporters", "supporters"],
    )

    with pytest.raises(command.RuleImportError, match="could never be scheduled"):
        command.read_support_specs(path)


def test_the_subject_may_not_appear_in_their_own_set(tmp_path):
    path = _write(
        tmp_path, command.SUPPORT_FILE,
        [{"subject": "Dee Example", "min_supporters": "1",
          "supporters": "dee example;Ada Example"}],
        ["subject", "min_supporters", "supporters"],
    )

    with pytest.raises(command.RuleImportError, match="own supporter set"):
        command.read_support_specs(path)


def test_a_blank_subject_is_refused(tmp_path):
    path = _write(
        tmp_path, command.SUPPORT_FILE,
        [{"subject": "", "min_supporters": "1", "supporters": "Ada Example"}],
        ["subject", "min_supporters", "supporters"],
    )

    with pytest.raises(command.RuleImportError, match="subject name is blank"):
        command.read_support_specs(path)


# ==========================================================================
# The properties that keep a local command safe
# ==========================================================================


def test_the_command_refuses_a_directory_git_can_see(tmp_path, monkeypatch, capsys):
    """A file git can see is a file that can be committed by accident, and this
    one names real people.
    """
    _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A", "max_per_event": "2", "members": "Ada Example"}],
        ["group", "max_per_event", "members"],
    )
    monkeypatch.setattr(command, "git_ignored", lambda path: False)

    exit_code = command.main(
        [
            "--input-dir", str(tmp_path),
            "--church-name", "Synthetic Church",
            "--ministry-name", "Ministry One",
            "--period-name", "Q4 2026",
            "--actor-name", "Synthetic Admin",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "not git-ignored" in captured.err
    assert "Nothing was read" in captured.err


def test_a_directory_with_neither_file_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(command, "git_ignored", lambda path: True)

    exit_code = command.main(
        [
            "--input-dir", str(tmp_path),
            "--church-name", "Synthetic Church",
            "--ministry-name", "Ministry One",
            "--period-name", "Q4 2026",
            "--actor-name", "Synthetic Admin",
        ]
    )

    assert exit_code == 2
    assert "nothing to configure" in capsys.readouterr().err


def test_the_environment_guards_run_before_any_database_work(
    tmp_path, monkeypatch, capsys
):
    """The same fail-closed gate the roster import and the demo seed keep: no
    connection is opened until the target has been proved.
    """
    _write(
        tmp_path, command.GROUPS_FILE,
        [{"group": "Category A", "max_per_event": "2", "members": "Ada Example"}],
        ["group", "max_per_event", "members"],
    )
    monkeypatch.setattr(command, "git_ignored", lambda path: True)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        command, "create_engine",
        lambda *a, **k: pytest.fail("no engine may be created before the guards"),
    )

    exit_code = command.main(
        [
            "--input-dir", str(tmp_path),
            "--church-name", "Synthetic Church",
            "--ministry-name", "Ministry One",
            "--period-name", "Q4 2026",
            "--actor-name", "Synthetic Admin",
        ]
    )

    assert exit_code == 1
    assert "Refusing to configure" in capsys.readouterr().out


def test_the_report_prints_counts_and_ids_and_no_name(capsys):
    """Checked, not left to eye: this command reads a file full of real names
    and must never put one on a terminal or in a log.
    """
    summary = command.ImportSummary(
        ministry_id=7, period_id=7, groups_configured=1,
        group_members_added=8, groups_capped=1,
        support_requirements_configured=1, supporters_approved=3,
    )

    command._report(summary)

    printed = capsys.readouterr().out
    assert "7" in printed and "8" in printed
    for forbidden in ("Ada", "Bo", "Dee", "Example", "Category", "@"):
        assert forbidden not in printed


def test_no_reason_column_is_read_from_either_file():
    """The scheduler needs the condition and the approved set, never the
    circumstance behind them (requirements §4.7). A column for it here would be
    an invitation to store one.
    """
    source = Path(command.__file__).read_text()

    for forbidden in ('"reason"', "'reason'", '"why"', "'why'"):
        assert forbidden not in source


# ==========================================================================
# Identity resolution is exact-match only
# ==========================================================================


class _Membership:
    def __init__(self, membership_id: int) -> None:
        self.id = membership_id


class _Person:
    def __init__(self, display_name: str) -> None:
        self.display_name = display_name


class _RosterSession:
    """Answers the one query ``_resolve_memberships`` issues, with a roster."""

    def __init__(self, names: list[str]) -> None:
        self._rows = [
            (_Membership(200 + index), _Person(name))
            for index, name in enumerate(names)
        ]

    def execute(self, statement):  # noqa: ARG002 - the statement is not varied
        return self

    def all(self):
        return self._rows


def _resolve(names: list[str]) -> dict:
    return command._resolve_memberships(_RosterSession(names), ministry_id=3)


def test_resolution_is_case_insensitive_string_equality_and_nothing_else():
    """Exact match on a normalized string is the whole rule.

    Anything cleverer -- a prefix, a first name, an initials expansion, a
    fuzzy distance -- would be guessing which person a head meant, and guessing
    wrong here silently configures a rule about somebody else.
    """
    resolved = _resolve(["Ada Example", "Bo Example"])

    assert set(resolved) == {"ada example", "bo example"}


def test_a_first_name_alone_does_not_resolve_to_a_full_roster_entry():
    """The property this test exists for: a bare first name is *unresolved*,
    even when exactly one roster entry begins with it. Uniqueness by
    coincidence is not identity evidence.
    """
    resolved = _resolve(["Ada Example"])

    assert resolved.get("ada") is None
    assert resolved.get("ada example") is not None


def test_a_prefix_of_a_roster_entry_does_not_resolve():
    resolved = _resolve(["Alexander Example"])

    for prefix in ("alex", "alexander", "alexander exam"):
        assert resolved.get(prefix) is None
    assert resolved.get("alexander example") is not None


def test_initials_never_expand_to_a_full_name():
    """A two-letter label resolves only when the roster itself carries it as a
    person's own value -- never by matching the initials of a longer entry.
    """
    resolved = _resolve(["Jordan Palmer"])
    assert resolved.get("jp") is None

    # The roster carrying "JP" itself is a different and legitimate case: an
    # exact value, matched exactly.
    resolved = _resolve(["Jordan Palmer", "JP"])
    assert resolved.get("jp") is not None


def test_surrounding_whitespace_and_case_are_normalized_but_nothing_more():
    resolved = _resolve(["  Ada Example  "])

    assert resolved.get("ada example") is not None
    # Inner spacing is part of the value, not noise to be normalized away.
    assert resolved.get("adaexample") is None
    assert resolved.get("ada  example") is None


def test_a_name_two_people_share_is_dropped_rather_than_resolved():
    """An ambiguous match is reported as unmatched, which sends a head to fix
    their file instead of letting this command pick one of two people.
    """
    resolved = _resolve(["Ada Example", "Ada Example", "Bo Example"])

    assert resolved.get("ada example") is None
    assert resolved.get("bo example") is not None


def test_the_importer_calls_no_prefix_or_fuzzy_matching():
    """Asserted against the module's actual calls and imports, so a fallback
    added later fails here rather than quietly widening what counts as an
    identity.

    Deliberately an AST check rather than a text search: the module's own
    docstring *names* these techniques in order to rule them out, and a prose
    match would fail on the very sentence that promises they are absent.
    """
    import ast

    tree = ast.parse(Path(command.__file__).read_text())

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    } | {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    for forbidden in ("startswith", "endswith", "get_close_matches",
                      "SequenceMatcher", "ratio", "partial_ratio"):
        assert forbidden not in called, forbidden

    imported = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    for forbidden in ("difflib", "rapidfuzz", "fuzzywuzzy", "Levenshtein"):
        assert forbidden not in imported, forbidden
