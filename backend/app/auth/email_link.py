"""The link between a Google address and an existing Person.

**One rule, written once.** The OAuth callback and the admin CLI both need to
answer "which Person is this address?", and if they answered it even slightly
differently the pilot would have a class of account nobody could sign in to: an
address the CLI accepted as linked and the callback failed to find. So both call
:func:`find_person_by_email`, and both normalize through :func:`normalize_email`.

**No Person is ever created here, and no name is ever matched.** A lookup either
finds exactly one existing Person carrying the address or it finds nobody, and
finding nobody is a refusal rather than an invitation to make someone up. Name
matching -- exact, fuzzy, or by display name -- is absent by design: two people
genuinely share a name, and a scheduling system that let the wrong one sign in
because the right one was on holiday is worse than one nobody can sign in to.

Normalization, and its limits
-----------------------------
:func:`normalize_email` case-folds and trims, and does nothing else. It does
**not** strip Gmail's dots or ``+tags``, and that restraint is the point. Those
rules are Gmail's, not email's; applying them would mean
``a.b@example.org`` and ``ab@example.org`` -- two different mailboxes at a
domain that does not follow Google's conventions -- resolved to one Person.
Under-normalizing fails safe (an admin links the exact address the person signs
in with); over-normalizing fails open (someone signs in as somebody else).

This matches the database exactly. ``person.email`` carries a unique index on
``lower(email)`` where the value is not null (core §7.1), so the uniqueness this
module relies on is enforced by PostgreSQL rather than by remembering to check.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.core import Person

__all__ = ["normalize_email", "find_person_by_email", "EmailFormatError"]


class EmailFormatError(ValueError):
    """The supplied text is not usable as an email address.

    Raised only by :func:`normalize_email`, and only for input a human typed --
    the CLI. Google's own ``email`` claim is not validated against this: it is
    already an address Google controls, and second-guessing its format here
    would reject real mailboxes for failing a rule this module invented.
    """


def normalize_email(raw: str) -> str:
    """The canonical form of ``raw``, for storing and for comparing.

    Case-folded and trimmed. Nothing else -- see the module docstring for why
    provider-specific normalization is deliberately absent.

    ``casefold()`` rather than ``lower()``: they agree on every ASCII address,
    and where they differ (the Kelvin sign, the German sharp s) casefold is the
    one defined for caseless comparison. The database index uses ``lower()``,
    which is what makes the two agree on the ASCII addresses that actually
    occur; :func:`find_person_by_email` therefore compares with SQL ``lower()``
    on both sides rather than relying on the stored value's case.

    :raises EmailFormatError: the value is blank, carries whitespace, or is not
        a single ``local@domain`` pair.
    """
    if not isinstance(raw, str):
        raise EmailFormatError("email must be text")

    candidate = raw.strip()
    if not candidate:
        raise EmailFormatError("email must not be blank")

    # Internal whitespace is never valid in an address as typed, and an address
    # containing it is far more likely to be two addresses pasted together.
    if any(character.isspace() for character in candidate):
        raise EmailFormatError("email must not contain whitespace")

    if candidate.count("@") != 1:
        raise EmailFormatError("email must contain exactly one '@'")

    local, _, domain = candidate.partition("@")
    if not local:
        raise EmailFormatError("email must have a local part before '@'")
    if not domain:
        raise EmailFormatError("email must have a domain after '@'")
    if "." not in domain:
        raise EmailFormatError("email domain must contain a '.'")
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise EmailFormatError("email domain is malformed")

    return candidate.casefold()


def find_person_by_email(session: Session, email: str) -> Person | None:
    """The single Person carrying ``email``, or ``None``.

    Compares ``lower(person.email)`` against ``lower(:email)`` so the database's
    unique index on ``lower(email)`` is the thing being queried -- the lookup
    uses the index rather than merely trusting that stored values were
    normalized on the way in. A row written by hand in psql with a capitalized
    address is therefore still found.

    ``scalar_one_or_none`` rather than ``first()`` on purpose: the index makes a
    second match impossible, and if one ever existed -- a botched restore, a
    migration run twice -- this raises instead of silently signing somebody in
    as whichever row sorted first.

    **Deactivated people are returned, not filtered.** Whether a deactivated
    Person may sign in is an authentication decision, and it is made once in
    :mod:`app.api.routes_auth` where it can be reported as its own reason. If
    this function hid them, a deactivated member and an unknown address would be
    indistinguishable to the admin CLI too, which needs to tell them apart.
    """
    normalized = email.strip().casefold()
    if not normalized:
        return None

    return session.execute(
        select(Person).where(func.lower(Person.email) == func.lower(normalized))
    ).scalar_one_or_none()
