"""Who says a list is the ministry's, and when they said it.

Two fields, and they are the difference between *a list* and *the list*. A role
vocabulary and a qualification matrix are both just rows of text; what makes
either authoritative is a named person having said so on a date. So both carry
one of these, both are checked the same way, and neither can be attested by
leaving a template's placeholder in place.

Deliberately not a signature, a login or an audit row: this is a development
intake path, and the claim it makes is only "a human filled this in on
purpose". The product's own authority model (Ministry Head grants, Task 79/80)
is what governs the database; this governs whether a file is ready to be shown
to it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

__all__ = ["Attestation", "PLACEHOLDERS"]

#: Values the shipped templates carry. Left in place they are not an
#: attestation -- they are the template still being a template.
PLACEHOLDERS = frozenset(
    {
        "",
        "<name>",
        "<the ministry head's name>",
        "<date, as yyyy-mm-dd>",
        "<date>",
        "yyyy-mm-dd",
        "tbd",
        "todo",
        "unknown",
    }
)


@dataclass(frozen=True, slots=True)
class Attestation:
    """A named person, and the date they approved something."""

    approved_by: str = ""
    approved_on: str = ""

    @property
    def present(self) -> bool:
        by = self.approved_by.strip().casefold()
        on = self.approved_on.strip().casefold()
        if by in PLACEHOLDERS or on in PLACEHOLDERS:
            return False
        try:
            datetime.date.fromisoformat(self.approved_on.strip())
        except ValueError:
            return False
        return True

    def describe(self, *, what: str = "this list") -> str:
        if self.present:
            return f"approved by {self.approved_by} on {self.approved_on}"
        return (
            f"NOT ATTESTED -- {what} carries no approved_by / approved_on,"
            " so it is a draft rather than the ministry's approved list"
        )
