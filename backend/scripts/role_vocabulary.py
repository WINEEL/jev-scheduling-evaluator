"""One ministry's role vocabulary, as a value the source reader is given.

**The problem this solves.** ``scripts/historical_setup/csv_source.py`` is a
genuinely generic spreadsheet reader -- it detects grid shapes, parses dates,
checks legends, and refuses anything it cannot explain -- and it is the reader
the production import path uses. But it reached for Setup's role names through
module-level imports, so *every* ministry's source had to be Setup's. AV's role
vocabulary already existed (``scripts/historical_av/roles.py``) and the
importer could not be told about it; there was nowhere to put it.

So the vocabulary becomes a parameter. A :class:`RoleVocabulary` says which
role names a ministry has, which one (if any) is its lead, which take part in a
role-variety preference, how a source label is normalized onto them, and which
words in a column header mark it as naming a role at all. Everything else about
reading a sheet is unchanged and shared.

**This generalizes the code and guesses no data.** A vocabulary is supplied by
a caller who knows the ministry's approved roles; nothing here infers one from
a spreadsheet, and an unknown label still fails closed exactly as it did.
Setup's vocabulary is the default, so every existing caller reads the same
sheets the same way and gets the same answer.

**A vocabulary is not a qualification list.** It names the *positions* a
ministry staffs. Who may serve each of them is the Ministry Head's decision and
lives nowhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from scripts.historical_setup.model import (
    CANONICAL_ROLE_NAMES,
    LEAD_ROLE_NAME,
    VARIETY_ROLE_NAMES,
    HistoricalRole,
)
from scripts.historical_setup.parsing import UnknownRoleError, normalize_role_name

__all__ = ["RoleVocabulary", "SETUP_VOCABULARY", "build_roles_for"]


@dataclass(frozen=True, slots=True)
class RoleVocabulary:
    """The approved roles of one ministry, and how to recognize them."""

    #: Every position this ministry staffs, in display order. Ids 1..N are
    #: assigned from this order, so it is part of the vocabulary rather than
    #: an accident of iteration.
    canonical_names: tuple[str, ...]
    #: How a source label is mapped onto one of ``canonical_names``. Must
    #: raise when the label is not one of them -- failing closed is the
    #: property every ministry's normalizer shares and the only one this
    #: module depends on.
    normalize: Callable[[str], str]
    #: Words that make a column header a *role* label rather than a person's
    #: name. Needed because a normalizer is deliberately generous about
    #: spelling once it already knows a column is a role, and that generosity
    #: is the wrong test for deciding whether it is one.
    role_words: tuple[str, ...]
    #: The exception :attr:`normalize` raises for a label outside this
    #: vocabulary. Carried explicitly because each ministry's normalizer has
    #: its own: the reader has to *skip* an unrecognized header while
    #: classifying a sheet and *stop* on one in a role cell, and it can only
    #: tell those apart if it knows precisely which exception means "not one of
    #: ours". Catching everything instead would swallow a genuine bug in a
    #: normalizer and report it as an unrecognized column.
    unknown_role_error: type[Exception] = UnknownRoleError
    #: The lead position, when this ministry has one. ``None`` is a real
    #: answer, not a missing value: a ministry of four distinct specialisms
    #: has no "lead-qualified" flag to read from a sheet.
    lead_role_name: str | None = None
    #: The roles a role-variety preference should spread work across. Empty
    #: means the ministry has none -- rotating a specialist fights how some
    #: ministries work, and an empty tuple says exactly that.
    variety_role_names: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.canonical_names:
            raise ValueError("a role vocabulary must name at least one role")
        if len(set(self.canonical_names)) != len(self.canonical_names):
            raise ValueError("role names must be unique within a vocabulary")
        if self.lead_role_name is not None and (
            self.lead_role_name not in self.canonical_names
        ):
            raise ValueError(
                f"lead role {self.lead_role_name!r} is not one of this"
                " vocabulary's roles"
            )
        unknown = set(self.variety_role_names) - set(self.canonical_names)
        if unknown:
            raise ValueError(
                f"variety roles {sorted(unknown)} are not roles of this"
                " vocabulary"
            )

    def names_a_role(self, header: str) -> bool:
        """True only when a header actually spells out one of these roles."""
        lowered = " ".join(str(header or "").split()).lower()
        if not any(word in lowered for word in self.role_words):
            return False
        return self.try_normalize(header) is not None

    def try_normalize(self, raw: str) -> str | None:
        """:attr:`normalize`, or ``None`` for a label outside this vocabulary.

        For the places a reader is *asking* whether a header names a role.
        Where an unrecognized label must stop the run instead, the reader calls
        :attr:`normalize` directly and lets it raise.
        """
        try:
            return self.normalize(raw)
        except self.unknown_role_error:
            return None

    def order_of(self, role_name: str) -> int:
        """Sort key for a role name; unknown names sort last, never raise."""
        try:
            return self.canonical_names.index(role_name)
        except ValueError:
            return len(self.canonical_names) + 1

    @property
    def default_headcount(self) -> int:
        """How many positions a Sunday staffs when the source does not say.

        The whole vocabulary, which is the only answer available without
        inventing a number for a ministry that has not stated one.
        """
        return len(self.canonical_names)


def build_roles_for(vocabulary: RoleVocabulary) -> tuple[HistoricalRole, ...]:
    """The vocabulary's roles as IR values, with synthetic ids 1..N.

    Ids follow ``canonical_names`` order, so the same vocabulary always
    produces the same ids and a dataset is reproducible.
    """
    return tuple(
        HistoricalRole(
            role_id=index,
            name=name,
            is_lead=name == vocabulary.lead_role_name,
            in_variety_set=name in vocabulary.variety_role_names,
        )
        for index, name in enumerate(vocabulary.canonical_names, start=1)
    )


#: Setup's approved five roles (requirements §9), unchanged. The default, so
#: every caller written before vocabularies existed reads exactly what it read
#: before.
SETUP_VOCABULARY = RoleVocabulary(
    canonical_names=CANONICAL_ROLE_NAMES,
    normalize=normalize_role_name,
    role_words=("setup", "set up", "lead", "position", "slot", "role", "captain"),
    lead_role_name=LEAD_ROLE_NAME,
    variety_role_names=VARIETY_ROLE_NAMES,
)
