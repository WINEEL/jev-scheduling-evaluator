"""The dry-run command's guards (Task 82).

Offline. The command is the only way a person reaches this code, so the two
properties that matter most are properties of the command: it has **no import
mode**, and it refuses to read a ministry's own records from a tracked
location.
"""

from __future__ import annotations

import pytest

from scripts import validate_ministry_intake as cli

CONFIG = "scripts/intake_templates/av_quarter_csv.example.toml"


def test_there_is_no_import_mode(capsys):
    """Importing is a separate, explicitly authorized action, not a flag here."""
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--config", CONFIG, "--source", "anything.csv"])
    assert exit_info.value.code == 2
    assert "--dry-run is required" in capsys.readouterr().err

    flags = {
        action.option_strings[0]
        for action in cli.build_parser()._actions
        if action.option_strings
    }
    assert "--import" not in flags
    assert "--write" not in flags
    assert "--commit" not in flags


def test_a_tracked_source_is_refused(tmp_path, capsys, monkeypatch):
    """A ministry's own file lives under a git-ignored path or it is not read."""
    source = tmp_path / "roster.csv"
    source.write_text("Date,A,B,Lead\n", encoding="utf-8")
    monkeypatch.setattr(cli, "git_ignored", lambda path: False)

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--config", CONFIG, "--source", str(source), "--dry-run"])
    assert exit_info.value.code == 2
    assert "NOT git-ignored" in capsys.readouterr().err


def test_a_missing_source_is_refused_before_anything_is_read(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(
            ["--config", CONFIG, "--source", str(tmp_path / "gone.csv"), "--dry-run"]
        )
    assert exit_info.value.code == 2
    assert "does not exist" in capsys.readouterr().err


def test_a_config_that_cannot_be_trusted_is_refused(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text('[ministry]\nlabel = "X"\n', encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--config", str(bad), "--source", "x.csv", "--dry-run"])
    assert exit_info.value.code == 2
    assert "REFUSED" in capsys.readouterr().err


def test_the_template_emitter_writes_one_column_per_declared_role(tmp_path, capsys):
    target = tmp_path / "approvals.csv"
    assert cli.main(
        ["--config", CONFIG, "--emit-qualification-template", str(target)]
    ) == 0
    text = target.read_text(encoding="utf-8")
    assert "source_name,AV Lead,Soundboard,Slides,Video,notes" in text
    assert "Shadow" not in text.splitlines()[-1]
    assert "approved_by" in text
    assert "Ministry Head" in capsys.readouterr().out


def test_the_template_emitter_never_overwrites(tmp_path, capsys):
    target = tmp_path / "approvals.csv"
    target.write_text("already here\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--config", CONFIG, "--emit-qualification-template", str(target)])
    assert exit_info.value.code == 2
    assert target.read_text(encoding="utf-8") == "already here\n"
