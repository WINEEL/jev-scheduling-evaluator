"""The HTTP boundary.

Two endpoints over :mod:`app.soft_constraints`, and nothing else. They accept
one scenario name from a closed set of three, build the state server-side, and
return the model's probabilities and the local policy's status as separate
parts of one response -- because the relationship between those two is what the
project is about.
"""
