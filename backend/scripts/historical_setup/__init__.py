"""Task 43 -- real historical validation of the scheduling engine (local only).

This package is **local validation tooling**, not shipped application code. It
exists to answer one question: *have we actually tried solving the real Setup
scheduling problem?* It takes a recent, real Setup scheduling period, runs it
through the genuine ``app.scheduling`` engine with no database, and reports how
the engine did against the ministry's own historical roster.

**Privacy.** Real church and member data must never be committed. Nothing in
this package embeds a real name, and every runner writes anything containing
real names only under ``local_data/`` (git-ignored). The modules here operate
on a neutral in-memory intermediate representation (:mod:`.model`); the thin
CSV reader that fills it (:mod:`.csv_source`) is the only part that touches a
real spreadsheet, and it prints structure (columns, counts, ranges), never
contents.

Pipeline::

    real CSVs  ->  csv_source  ->  HistoricalDataset (IR)
               ->  adapter     ->  SchedulingInput + SchedulingPolicy
               ->  solve_schedule()  (the real CP-SAT engine, unmodified)
               ->  checks      ->  hard-rule assertions
               ->  report      ->  local readable roster + privacy-safe summary
"""
