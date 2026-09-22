"""One observation about a source, and how serious it is.

Three severities, and the distinction between the first two is the whole point
of the module: a **blocker** is a fact that must be known before a real import
may happen, a **warning** is something worth a human's eye that does not stop
one. Readiness is computed from blockers alone
(:mod:`scripts.ministry_intake.readiness`), so mislabelling a warning as a
blocker stalls a ministry and mislabelling a blocker as a warning imports a
guess. They are returned in separate lists rather than in one list with a flag,
because every caller so far wants exactly one of the two.

A finding carries a ``category`` as well as a severity. Two of those categories
are the words a Ministry Head has to see verbatim --
``NEEDS MINISTRY-HEAD DECISION`` and ``NEEDS MINISTRY-HEAD DEFINITION`` -- so
they are constants here rather than strings typed at each site.

**Findings are printed.** A source person's name is source structure the
operator needs in order to fix their own file, so unresolved names do appear;
nothing else about a person does, and no finding ever carries an availability
answer, an email or a schedule row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "Severity",
    "NEEDS_HEAD_DECISION",
    "NEEDS_HEAD_DEFINITION",
    "SOURCE_STRUCTURE",
    "IDENTITY",
    "QUALIFICATION",
    "CONTRADICTION",
    "CROSS_MINISTRY",
    "Finding",
    "blocker",
    "warning",
    "info",
]


class Severity(Enum):
    """How much a finding is allowed to stop."""

    BLOCKER = "BLOCKER"
    WARNING = "WARNING"
    INFO = "INFO"


#: A church fact only the Ministry Head can settle -- which of two
#: contradicting records is right.
NEEDS_HEAD_DECISION = "NEEDS MINISTRY-HEAD DECISION"
#: A church fact only the Ministry Head can *define* -- what a column means.
#: Distinct from a decision: nobody is choosing between known options, the
#: meaning itself is unknown.
NEEDS_HEAD_DEFINITION = "NEEDS MINISTRY-HEAD DEFINITION"

SOURCE_STRUCTURE = "SOURCE STRUCTURE"
IDENTITY = "IDENTITY"
QUALIFICATION = "QUALIFICATION"
CONTRADICTION = "CONTRADICTION"
CROSS_MINISTRY = "CROSS-MINISTRY"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing observed about a source, at one severity."""

    severity: Severity
    #: Stable machine-readable identifier, screaming snake case. Tests assert
    #: on this rather than on ``message``, so a message can be reworded for a
    #: human without breaking the suite.
    code: str
    category: str
    message: str
    #: Extra lines shown indented under the message. Usually the specific
    #: source names, dates or column indexes the operator has to go and fix.
    detail: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> list[str]:
        lines = [f"[{self.severity.value}] {self.category}: {self.message}"]
        lines.extend(f"    {line}" for line in self.detail)
        return lines


def _make(severity: Severity):
    def build(
        code: str, category: str, message: str, detail: tuple[str, ...] | list[str] = ()
    ) -> Finding:
        return Finding(
            severity=severity,
            code=code,
            category=category,
            message=message,
            detail=tuple(detail),
        )

    return build


blocker = _make(Severity.BLOCKER)
warning = _make(Severity.WARNING)
info = _make(Severity.INFO)
