"""Fail-closed checks that decide whether a **production** import may run.

The mirror image of :mod:`scripts.demo_guards`, and deliberately a separate
module rather than a flag on that one. Task 76's rule is that the existing
development-only tooling keeps its guard exactly as it is -- loosening
``demo_guards`` to also permit production would mean the one command that must
never touch production and the one command that may would share a single set of
conditions, and a mistake in either direction would be a mistake in both.

**What makes production different, and harder.** ``demo_guards`` can refuse
anything it is unsure about, because there is always another day to seed a demo.
This module guards an operation somebody actually needs to perform against real
data, so it cannot simply refuse everything; instead it demands that the operator
prove intent four separate ways, and it refuses every case it is unsure about:

1. ``--confirm-production`` typed on the command line -- not an environment
   variable, so it cannot be left set in a shell and forgotten;
2. ``CHURCH_SCHEDULING_ALLOW_PRODUCTION_IMPORT`` set to exactly ``"1"``;
3. ``APP_ENV=production``, so the process agrees about where it is;
4. ``CHURCH_SCHEDULING_PRODUCTION_DATABASE_HOST`` naming the target host, and
   ``DATABASE_URL`` actually pointing there.

**And two exclusions, which are the part that matters most.** The development
and integration-test hosts are refused outright when this machine knows them.
``demo_guards`` excludes the test branch for the same reason; here the
development branch is excluded too, because "I meant production but my shell
still had the development URL exported" is the realistic accident, and it is the
one that would silently write a real church's roster into the wrong database and
report success.

**What this cannot prove.** Like ``demo_guards``, it cannot recognize a
production branch by its hostname -- no hostname of anyone's belongs in this
repository. It proves "you are writing to the host you explicitly named, and
that host is neither of the two non-production ones this machine knows about".
Read it as that, not as "can tell production apart from a hostname alone".

Every function is pure and takes its environment as an argument, and no
function here prints, logs or returns a host, a URL or a credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from scripts.demo_guards import host_of, host_of_setting

#: Must be exactly this. A dangerous switch turns on for one value and nothing
#: else -- not for "true", "yes" or "0".
ALLOW_PRODUCTION_IMPORT = "CHURCH_SCHEDULING_ALLOW_PRODUCTION_IMPORT"
ALLOW_PRODUCTION_IMPORT_VALUE = "1"

APP_ENV = "APP_ENV"
REQUIRED_APP_ENV = "production"

#: The host of the branch the import is allowed to write to. Supplied locally;
#: never committed.
PRODUCTION_DATABASE_HOST = "CHURCH_SCHEDULING_PRODUCTION_DATABASE_HOST"

DATABASE_URL = "DATABASE_URL"


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    """Why the import may or may not proceed.

    ``reasons`` are written to be shown to a person and contain **no** hosts,
    URLs or credentials -- only the name of the setting that disagreed.
    """

    reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.reasons


def evaluate(
    env: Mapping[str, str],
    *,
    database_url: str | None,
    confirmed: bool,
    development_database_url: str | None = None,
    test_database_url: str | None = None,
) -> GuardOutcome:
    """Decide whether a production import may proceed against ``database_url``.

    ``confirmed`` is the command-line flag, passed in rather than read from the
    environment on purpose: an environment variable can be exported once and
    inherited by every later shell, and the whole point of this one is that it
    is typed deliberately, each time.

    ``development_database_url`` and ``test_database_url`` are optional because
    a machine may not have either configured -- but when they are available they
    are checked, because writing a real roster into the development branch while
    believing it went to production is the accident this module exists for.

    A refusal never explains *which* host was seen. Being told that two settings
    disagree is enough to fix the problem; printing either would put a hostname
    somewhere it does not belong.
    """
    reasons: list[str] = []

    if not confirmed:
        reasons.append(
            "--confirm-production was not passed. A production import must be"
            " asked for explicitly on the command line, every time."
        )

    if env.get(ALLOW_PRODUCTION_IMPORT) != ALLOW_PRODUCTION_IMPORT_VALUE:
        reasons.append(
            f"{ALLOW_PRODUCTION_IMPORT} is not set to exactly"
            f' "{ALLOW_PRODUCTION_IMPORT_VALUE}". Production import is opt-in'
            " and off by default."
        )

    if env.get(APP_ENV) != REQUIRED_APP_ENV:
        reasons.append(
            f'{APP_ENV} is not "{REQUIRED_APP_ENV}". The process must agree that'
            " it is acting against production."
        )

    current_host = host_of(database_url)
    if current_host is None:
        reasons.append(
            f"{DATABASE_URL} is missing or has no host, so there is nothing to"
            " verify. Refusing rather than guessing."
        )

    expected_host = host_of_setting(env.get(PRODUCTION_DATABASE_HOST))
    if expected_host is None:
        reasons.append(
            f"{PRODUCTION_DATABASE_HOST} is not set. Set it to the host of the"
            " Neon production branch, so the import can prove where it is"
            " writing."
        )

    if current_host is not None and expected_host is not None:
        if current_host != expected_host:
            reasons.append(
                f"{DATABASE_URL} does not point at the host named by"
                f" {PRODUCTION_DATABASE_HOST}. Refusing: the import writes only"
                " to the branch it was explicitly told about."
            )

    # The two branches this command must never write to, checked whenever this
    # machine knows what they are.
    for label, other_url, variable in (
        ("development", development_database_url, DATABASE_URL),
        ("integration-test", test_database_url, "TEST_DATABASE_URL"),
    ):
        other_host = host_of(other_url)
        if current_host is not None and other_host is not None and current_host == other_host:
            reasons.append(
                f"{DATABASE_URL} points at the same host as the {label} branch."
                " That is not production; refusing before anything is read."
            )

    return GuardOutcome(reasons=tuple(reasons))
