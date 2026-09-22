"""Configured, fail-closed intake of one ministry's source workbook.

**The problem.** ``scripts/historical_setup/csv_source.py`` is a genuinely
generic reader, but it *detects* its shape: it looks at a sheet and decides
which of four grids it is. Detection is the right tool for a source that fits
one of those four and the wrong one for everything else -- pointed at AV's real
quarter file it returns ``detected_shape='unrecognized'``, and there is nowhere
to tell it otherwise. Four of AV's role columns carry no header at all, so no
amount of detection could name them.

**The change.** A ministry's source shape becomes a *declaration* rather than a
deduction. :mod:`scripts.ministry_intake.config` reads a small TOML file
stating which column is the date, which columns are volunteers, which column is
which role -- **by position where the header is missing or drifts** -- what the
availability tokens mean, and which columns the ministry has not yet explained.
Nothing is inferred from the data, and a column the declaration does not
account for stops the run.

**What it deliberately does not do.**

- It does not guess a mapping and accept it. An unconfigured column, an
  unknown role label, an unknown availability token and an unknown file shape
  are each a refusal with a report, never a default.
- It does not merge two people because their names look alike. Identity is an
  operator-written decision file (:mod:`scripts.ministry_intake.identity`);
  case-folding the operator's own keys is the only automatic step.
- It does not turn history into authority. Roles someone has served are
  reported as ``NON-AUTHORITATIVE`` reference material and can never become a
  qualification (:mod:`scripts.ministry_intake.qualifications`).
- It does not write to a database. :mod:`scripts.ministry_intake.dry_run`
  imports no session, no model and no service, and the CLI has no mode that
  does.
- It does not become ``if ministry == "AV"``. Everything here takes its
  ministry from a config file; AV's config is an example under
  ``scripts/intake_templates/``, and Kids' is the same file with the unknown
  parts left unfilled so it fails closed.

The output is a :class:`~scripts.ministry_intake.report.DryRunReport` and a
:class:`~scripts.ministry_intake.readiness.ReadinessVerdict`: blockers apart
from warnings, and ``READY`` only when every authoritative input actually
exists and validates.
"""

from __future__ import annotations

__all__ = [
    "config",
    "dry_run",
    "findings",
    "identity",
    "qualifications",
    "readiness",
    "reader",
    "report",
]
