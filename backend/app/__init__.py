"""A small experiment in soft-constraint evaluation with a System One model.

Three layers, and the separation between them is the whole subject:

- hard constraints -> a deterministic engine (assumed already satisfied here)
- soft constraints -> TypeSafe's Jev, returning probabilities and nothing else
- final status     -> ordinary deterministic Python

:mod:`app.soft_constraints` holds the evaluator; :mod:`app.api` is the HTTP
surface over three fixed synthetic scenarios; :mod:`app.main` is the server.
"""
