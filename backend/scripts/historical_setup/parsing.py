"""Generic parsing helpers: dates, role names, availability tokens.

These are the pieces that are safe to write and test without the real
spreadsheet in hand, because their behaviour is defined by the *conventions*
Task 43 states, not by any one file's layout:

- role-name normalization onto the approved Setup model (requirements §9);
- schedule-date parsing for the handful of notations a Setup sheet uses;
- availability-token semantics -- **X = unavailable, an explicit available
  marker = available, blank = no row** -- expressed as a small configurable
  table so that if the real source turns out to differ, the runner stops and
  reports rather than silently guessing.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from app.scheduling.input import AvailabilityState

from scripts.historical_setup.model import (
    CANONICAL_ROLE_NAMES,
    LEAD_ROLE_NAME,
    VARIETY_ROLE_NAMES,
)

__all__ = [
    "UnknownRoleError",
    "AmbiguousDateError",
    "AvailabilitySemantics",
    "SETUP_AVAILABILITY_SEMANTICS",
    "normalize_role_name",
    "parse_schedule_date",
    "parse_availability_token",
]


class UnknownRoleError(ValueError):
    """A role label in the source is not one of the approved Setup roles."""


class AmbiguousDateError(ValueError):
    """A date string could not be resolved to a single calendar date."""


# --------------------------------------------------------------------------
# Role names
# --------------------------------------------------------------------------

_ROLE_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5,
    "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}
_LEAD_TOKENS = {"lead", "leader", "setuplead", "teamlead", "captain"}


def normalize_role_name(raw: str) -> str:
    """Map a source role label onto one canonical Setup role name.

    Accepts the spellings a human roster actually uses -- ``"Setup Lead"``,
    ``"lead"``, ``"Set-up 2"``, ``"Setup #3"``, ``"SETUP FOUR"`` -- and rejects
    anything outside the approved five-role model rather than guessing.
    """
    if raw is None:
        raise UnknownRoleError("role label is empty")
    cleaned = raw.strip().lower()
    if not cleaned:
        raise UnknownRoleError("role label is empty")

    compact = re.sub(r"[\s_#.\-]+", "", cleaned)
    if compact in _LEAD_TOKENS or compact in {"setup1", "setupone", "position1"}:
        return LEAD_ROLE_NAME

    digit_match = re.search(r"(\d+)", compact)
    number: int | None = None
    if digit_match:
        number = int(digit_match.group(1))
    else:
        for word, value in _ROLE_NUMBER_WORDS.items():
            if word in cleaned:
                number = value
                break

    if number == 1:
        return LEAD_ROLE_NAME
    if number in (2, 3, 4, 5):
        return f"Setup {number}"

    # Exact canonical match already (case-insensitive) as a last resort.
    for canonical in CANONICAL_ROLE_NAMES:
        if compact == re.sub(r"[\s_#.\-]+", "", canonical.lower()):
            return canonical

    raise UnknownRoleError(f"unrecognized Setup role label: {raw!r}")


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------

_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ],
        start=1,
    )
}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})


def parse_schedule_date(raw: str, *, year_hint: int) -> datetime.date:
    """Parse the date notations a Setup roster uses into a real date.

    Handles ``"2025-10-05"``, ``"10/5/2025"``, ``"10/5"``, ``"Oct 5"``,
    ``"October 5, 2025"`` and ``"5 Oct"``. ``year_hint`` supplies the year when
    the source omits it (a single-quarter roster usually does). A bare
    month/day whose implied date lands more than 6 months from the hinted year
    is rejected as ambiguous rather than silently rolled.
    """
    if raw is None:
        raise AmbiguousDateError("date string is empty")
    text = raw.strip()
    if not text:
        raise AmbiguousDateError("date string is empty")

    # ISO first.
    iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", text)
    if iso:
        return datetime.date(int(iso[1]), int(iso[2]), int(iso[3]))

    # Numeric M/D or M/D/Y (also accepts '-' or '.').
    numeric = re.match(r"^(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?$", text)
    if numeric:
        month, day = int(numeric[1]), int(numeric[2])
        year = _resolve_year(numeric[3], year_hint)
        return _checked(year, month, day, year_hint)

    # Month-name forms.
    tokens = re.findall(r"[A-Za-z]+|\d+", text)
    month = day = year = None
    for tok in tokens:
        low = tok.lower()
        if low in _MONTHS:
            month = _MONTHS[low]
        elif tok.isdigit():
            value = int(tok)
            if value > 31:
                year = value
            elif day is None:
                day = value
            else:
                year = value
    if month and day:
        return _checked(year or year_hint, month, day, year_hint)

    raise AmbiguousDateError(f"could not parse schedule date: {raw!r}")


def _resolve_year(group: str | None, year_hint: int) -> int:
    if not group:
        return year_hint
    value = int(group)
    if value < 100:
        return 2000 + value
    return value


def _checked(year: int, month: int, day: int, year_hint: int) -> datetime.date:
    parsed = datetime.date(year, month, day)
    # Guard against a bare month/day that clearly belongs to another year.
    if abs((parsed - datetime.date(year_hint, 6, 30)).days) > 400:
        raise AmbiguousDateError(
            f"parsed {parsed.isoformat()} is far from the {year_hint} period;"
            " supply an explicit year"
        )
    return parsed


# --------------------------------------------------------------------------
# Availability tokens
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AvailabilitySemantics:
    """Which cell values mean what, in one Setup availability grid.

    ``blank_is_row = False`` is the Setup convention: a blank writes **no
    availability row**, so the person is ``NO_RESPONSE`` and the run's
    ``allow_no_response=True`` policy decides what that means. The runner
    validates the real sheet against this table and stops if it sees tokens the
    table does not explain.
    """

    available_tokens: frozenset[str]
    unavailable_tokens: frozenset[str]
    blank_is_row: bool = False

    def classify(self, raw: str | None) -> AvailabilityState | None:
        """``AVAILABLE`` / ``UNAVAILABLE`` for a known token, ``None`` for a
        blank that must not become a row. Raises on an unrecognized token.
        """
        token = (raw or "").strip().lower()
        if token == "":
            return AvailabilityState.AVAILABLE if self.blank_is_row else None
        if token in self.unavailable_tokens:
            return AvailabilityState.UNAVAILABLE
        if token in self.available_tokens:
            return AvailabilityState.AVAILABLE
        raise ValueError(f"unrecognized availability token: {raw!r}")


#: The known historical Setup convention (requirements §9): ``X`` (or "no",
#: "away", "-") means unavailable; ``O`` / "yes" / a check means an explicit
#: yes; blank writes nothing.
SETUP_AVAILABILITY_SEMANTICS = AvailabilitySemantics(
    available_tokens=frozenset(
        {"o", "y", "yes", "available", "avail", "✓", "ok"}
    ),
    unavailable_tokens=frozenset(
        {"x", "n", "no", "unavailable", "unavail", "away", "off", "-", "--"}
    ),
    blank_is_row=False,
)


def parse_availability_token(
    raw: str | None, *, semantics: AvailabilitySemantics = SETUP_AVAILABILITY_SEMANTICS
) -> AvailabilityState | None:
    """Convenience wrapper over :meth:`AvailabilitySemantics.classify`."""
    return semantics.classify(raw)


# Re-exported for callers that build the variety set by name.
SUPPORT_ROLE_NAMES = VARIETY_ROLE_NAMES
