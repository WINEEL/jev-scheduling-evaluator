"""Why a sign-in was refused.

One exception type with a machine-readable ``reason``, rather than a family of
classes: every one of these ends the same way -- no session, back to the sign-in
page -- and the reason exists to pick the message the page shows, not to be
branched on by application logic.

**These strings reach the browser as a query parameter.** They are fixed
identifiers chosen here, never anything derived from the request, the token or
the database, so nothing about who exists or what they typed can travel in one.
"""

from __future__ import annotations

__all__ = ["SignInError", "REASONS"]

#: Every reason the callback may end with, and what it means.
REASONS = {
    # Google said the address is not verified, so it proves nothing about who
    # controls it.
    "email_not_verified": "unverified Google email",
    # Authentication succeeded and no Person carries this address. The pilot's
    # central rule: identity is linked by an admin in advance or not at all.
    "not_linked": "no linked person",
    # The Person exists but has been deactivated.
    "person_inactive": "linked person is deactivated",
    # State missing or wrong, the code replayed, Google returned an error, or
    # the response failed validation. Deliberately one reason: the difference
    # matters to the server log, not to the person looking at the page.
    "invalid_response": "sign-in could not be verified",
    # The deployment has no Google client configured.
    "not_configured": "Google sign-in is not configured",
}


class SignInError(Exception):
    """A sign-in attempt that must not produce a session.

    ``reason`` is one of :data:`REASONS`. The message is for the server-side
    log only and never rendered to the browser.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        if reason not in REASONS:
            raise ValueError(f"unknown sign-in reason: {reason!r}")
        self.reason = reason
        super().__init__(message or REASONS[reason])
