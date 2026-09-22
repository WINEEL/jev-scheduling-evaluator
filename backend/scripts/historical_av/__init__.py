"""Task 44 -- AV ministry validation against real historical AV schedules.

Reuses the Task 43 pipeline (``scripts.historical_setup``: the neutral
``HistoricalDataset`` IR, the adapter onto ``SchedulingInput``, the hard-rule
checks and the reports) and supplies only what is genuinely AV-specific: AV's
role vocabulary, the combined availability+assignment sheet shape, the
workbook reader, and the observed role-eligibility proxy.

The shared core still lives under ``scripts/historical_setup/`` because that
is where Task 43 built it; the package name is now narrower than its contents.
Renaming it is a tidy-up for its own task, not something to fold into a
validation run.
"""
