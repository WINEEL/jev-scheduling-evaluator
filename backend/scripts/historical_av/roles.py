"""AV's role vocabulary, and the rule that an unknown role fails closed.

AV's roles are **not** Setup's. Setup has one Lead and four interchangeable
support positions; AV has four distinct specialisms, and a volunteer is
typically qualified for one or two of them. Nothing here is shared with
:mod:`scripts.historical_setup.parsing` on purpose -- a normalizer that
accepted both vocabularies would happily read an AV sheet's ``Slides`` column
as a Setup role and vice versa.

``Shadow`` is deliberately *not* a staffing position. It is training that
rides along with a real assignment: the church records who shadowed, but a
Sunday without a shadow is not short-staffed. Modelling it as a required role
would invent shortfalls that never existed.
"""

from __future__ import annotations

import re

from scripts.historical_setup.model import HistoricalRole
from scripts.role_vocabulary import RoleVocabulary

__all__ = [
    "UnknownAvRoleError",
    "AV_LEAD",
    "AV_VOCABULARY",
    "SHADOW_ROLE_NAME",
    "AV_STAFFING_ROLE_NAMES",
    "AV_ALL_ROLE_NAMES",
    "normalize_av_role_name",
    "build_av_roles",
]


class UnknownAvRoleError(ValueError):
    """A label in an AV source is not one of AV's known roles."""


AV_LEAD = "AV Lead"
SHADOW_ROLE_NAME = "Shadow"

#: The positions an AV Sunday must staff, in display order.
AV_STAFFING_ROLE_NAMES: tuple[str, ...] = (
    AV_LEAD,
    "Soundboard",
    "Slides",
    "Video",
)
#: Everything AV records, staffing or not.
AV_ALL_ROLE_NAMES: tuple[str, ...] = (*AV_STAFFING_ROLE_NAMES, SHADOW_ROLE_NAME)

#: Spellings seen across a decade of AV workbook tabs. The sheets write the
#: lead column as plain ``Lead``, and older tabs prefix a position code and
#: append a service time (``(AV1)\nSound\nboard\n8:00 AM``), so codes and
#: times are stripped before matching.
_SPELLINGS: dict[str, str] = {
    "lead": AV_LEAD,
    "avlead": AV_LEAD,
    "audiovisuallead": AV_LEAD,
    "soundboard": "Soundboard",
    "sound": "Soundboard",
    "audio": "Soundboard",
    "slides": "Slides",
    "slide": "Slides",
    "projection": "Slides",
    "video": "Video",
    "camera": "Video",
    "shadow": SHADOW_ROLE_NAME,
    "training": SHADOW_ROLE_NAME,
}

_POSITION_CODE = re.compile(r"\(\s*av\s*\d+\s*\)", re.I)
_TIME = re.compile(r"\d{1,2}\s*:\s*\d{2}\s*[ap]\.?m\.?", re.I)


def _compact(raw: str) -> str:
    text = _POSITION_CODE.sub(" ", raw or "")
    text = _TIME.sub(" ", text)
    return re.sub(r"[^a-z]", "", text.lower())


def normalize_av_role_name(raw: str) -> str:
    """Map an AV source label onto one canonical AV role name.

    Raises :class:`UnknownAvRoleError` for anything else -- including labels
    that are real columns in the historical workbook but not AV roles we
    model, such as ``Setup`` / ``AV setup``. Failing closed is the point: a
    label read as the wrong role would silently grant someone eligibility for
    work they have never done.
    """
    compact = _compact(raw)
    if not compact:
        raise UnknownAvRoleError("role label is empty")
    try:
        return _SPELLINGS[compact]
    except KeyError:
        raise UnknownAvRoleError(f"unrecognized AV role label: {raw!r}") from None


def try_normalize_av_role_name(raw: str) -> str | None:
    """:func:`normalize_av_role_name`, or ``None`` for an unknown label."""
    try:
        return normalize_av_role_name(raw)
    except UnknownAvRoleError:
        return None


def build_av_roles() -> tuple[HistoricalRole, ...]:
    """AV's five recorded roles with synthetic ids 1..5.

    ``AV Lead`` is the lead role. **None of them is in the variety set**: AV
    volunteers specialize, so rotating someone across Soundboard and Slides
    would fight the ministry's actual practice rather than serve it. Variety
    is switched off for AV at the policy level too; leaving the set empty here
    means it cannot be turned on by accident.

    ``Shadow`` is present so the source's shadow column can be read and
    reported, and is marked non-staffing so it can never become a requirement.
    """
    roles: list[HistoricalRole] = []
    for i, name in enumerate(AV_ALL_ROLE_NAMES, start=1):
        roles.append(
            HistoricalRole(
                role_id=i,
                name=name,
                is_lead=name == AV_LEAD,
                in_variety_set=False,
                is_staffing_position=name != SHADOW_ROLE_NAME,
            )
        )
    return tuple(roles)


#: AV's vocabulary in the shape the shared source reader takes (Task 81), so
#: an AV sheet can be read by :mod:`scripts.historical_setup.csv_source`
#: instead of by a reader of its own.
#:
#: **Only the four staffing positions.** ``Shadow`` is recorded by
#: :func:`build_av_roles` and must never become a requirement, so it is not a
#: position the reader may staff -- ``default_headcount`` is four, which is
#: what an AV Sunday actually needs.
#:
#: The variety set is deliberately empty: AV volunteers specialize, and
#: rotating a specialist fights the ministry's practice. Leaving it empty here
#: means the preference cannot be switched on by accident.
#:
#: **This changes what the importer can read, not what anyone is qualified
#: for.** Naming AV's positions is not the same as knowing who may fill them,
#: which remains the Ministry Head's list and is still absent.
AV_VOCABULARY = RoleVocabulary(
    canonical_names=AV_STAFFING_ROLE_NAMES,
    normalize=normalize_av_role_name,
    unknown_role_error=UnknownAvRoleError,
    # A header naming an AV role says so outright; there is no equivalent of
    # Setup's bare ``"3"``, so the marker words are just the role words
    # themselves plus the spellings the workbook uses for them.
    role_words=(
        "lead", "sound", "audio", "slide", "projection", "video", "camera",
    ),
    lead_role_name=AV_LEAD,
    variety_role_names=(),
)
