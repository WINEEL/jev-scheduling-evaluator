"""The offline test suite.

**Nothing here calls TypeSafe.** Every test that reaches the evaluator replaces
its one seam with a fake, and several actively sabotage ``TypeSafeClient`` so a
path that tried to go live would fail loudly rather than quietly succeed. The
suite needs no API key, no network and no vendor to be up.
"""
