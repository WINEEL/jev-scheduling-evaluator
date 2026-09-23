"""Shared test setup.

Deliberately almost empty, and that is worth stating: this project has no
database to point somewhere synthetic, no settings cache to clear and no
application state to reset between tests. The one thing every test needs is
the guarantee that none of them can reach TypeSafe, and that is enforced here
rather than remembered file by file.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def never_call_typesafe(monkeypatch):
    """Make constructing a real TypeSafe client an error, for every test.

    Autouse rather than opt-in: a test added later should be offline by
    default, not by somebody remembering. Tests that need a client pass a fake
    one explicitly, which is the seam
    :func:`app.soft_constraints.evaluator.evaluate_soft_constraints` exists to
    provide.
    """
    import typesafe_sdk

    def explode(*args, **kwargs):
        raise AssertionError(
            "a test tried to construct a real TypeSafeClient; the suite is offline"
        )

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", explode)
