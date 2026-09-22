"""Fail-closed checks that decide whether the demo seed may run at all.

**The intended target is the existing Neon ``development`` branch.** There is
no separate demo branch: the project has exactly three Neon branches
(production, development, integration-test), the development branch is
currently empty, and it is where fictional local/demo data is meant to live.
This module does not know that by itself -- it proves it, by requiring the
caller to name the expected host explicitly and checking that ``DATABASE_URL``
actually points there.

**The problem this solves.** Seeding is the one local operation that writes a
lot of rows into whatever database happens to be configured. The cost of
getting that wrong is not a failed command -- it is fictional people appearing
in a real church's data, or a test branch quietly filling up. So the rule here
is that the seed runs only when it can *prove* it is pointed at the intended
target, and refuses in every other case, including every case it is unsure
about.

**What is, and is not, proved here.** This module cannot recognize "the
production branch" as such -- doing that would mean either hard-coding a
production hostname (never acceptable) or having a production URL available
locally to compare against (which it deliberately never receives). The safety
here is layered instead: an explicit opt-in, ``APP_ENV=development``, an
explicit expected-host match, and exclusion of the integration-test host when
that is locally known. None of those layers claims to detect a production
branch by shape or convention. Read this as "seeds only the branch you named,
and never the test branch", not as "can tell production apart from a hostname
alone".

**Nothing here is hard-coded.** The expected host comes from the environment,
so no hostname of anyone's belongs in this repository. The checks compare
hosts; they never print, log or return one, and never touch the credential
portion of a URL at all.

Every function is pure and takes its environment as an argument, so the rules
can be tested exactly as they will run without setting a single real variable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

#: Must be exactly this. Like the dev-auth flag, a dangerous switch turns on
#: for one value and nothing else -- not for "true", "yes" or "0".
ALLOW_DEMO_SEED = "CHURCH_SCHEDULING_ALLOW_DEMO_SEED"
ALLOW_DEMO_SEED_VALUE = "1"

#: The application's own environment name (``Settings.app_env``).
APP_ENV = "APP_ENV"
REQUIRED_APP_ENV = "development"

#: The host of the Neon branch the seed is allowed to write to -- in practice,
#: the existing ``development`` branch. Supplied locally; never committed.
DEMO_DATABASE_HOST = "CHURCH_SCHEDULING_DEMO_DATABASE_HOST"

DATABASE_URL = "DATABASE_URL"


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    """Why the seed may or may not proceed.

    ``reasons`` are written to be shown to a person and contain **no** hosts,
    URLs or credentials -- only the name of the setting that disagreed.
    """

    reasons: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.reasons


def host_of(url: str | None) -> str | None:
    """The hostname of a database URL, or ``None`` if there isn't one.

    Only the host is ever extracted. The user, password, database name and
    query string are not read, so they cannot end up in a comparison, a log
    line or an exception.
    """
    if not url:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    host = parsed.hostname
    return host.lower() if host else None


def evaluate(
    env: Mapping[str, str],
    *,
    database_url: str | None,
    test_database_url: str | None = None,
) -> GuardOutcome:
    """Decide whether seeding may proceed against ``database_url``.

    **The expected target is the development branch, and that is deliberate.**
    There is no separate demo branch to distinguish ``database_url`` from --
    the whole point is that ``database_url`` should equal the host named by
    ``CHURCH_SCHEDULING_DEMO_DATABASE_HOST``, which the caller sets to the
    development branch's own host. A match is success, not a warning sign.

    ``test_database_url`` is the *one* other database this project must
    actively keep the seed away from, read locally where it is available. It
    is optional because a machine may not have it configured -- but when it is
    available it is checked, because seeding the integration-test branch would
    corrupt every test that assumes an empty starting state.

    There is no comparable check against a production URL: none is ever passed
    in, and none should be. See the module docstring for what that does and
    does not prove.

    A refusal never explains *which* host was seen. Being told "the configured
    target host and DATABASE_URL disagree" is enough to fix the problem, and
    printing either one would put a hostname somewhere it does not belong.
    """
    reasons: list[str] = []

    if env.get(ALLOW_DEMO_SEED) != ALLOW_DEMO_SEED_VALUE:
        reasons.append(
            f"{ALLOW_DEMO_SEED} is not set to exactly \"{ALLOW_DEMO_SEED_VALUE}\"."
            " Demo seeding is opt-in and off by default."
        )

    app_env = env.get(APP_ENV, REQUIRED_APP_ENV)
    if app_env != REQUIRED_APP_ENV:
        reasons.append(
            f"{APP_ENV} is not \"{REQUIRED_APP_ENV}\". Demo data belongs only in a"
            " local development environment."
        )

    current_host = host_of(database_url)
    if current_host is None:
        reasons.append(
            f"{DATABASE_URL} is missing or has no host, so there is nothing to"
            " verify. Refusing rather than guessing."
        )

    expected_host = host_of_setting(env.get(DEMO_DATABASE_HOST))
    if expected_host is None:
        reasons.append(
            f"{DEMO_DATABASE_HOST} is not set. Set it to the host of the Neon"
            " development branch, so the seed can prove where it is writing."
        )

    if current_host is not None and expected_host is not None:
        if current_host != expected_host:
            reasons.append(
                f"{DATABASE_URL} does not point at the host named by"
                f" {DEMO_DATABASE_HOST}. Refusing: the demo seed writes only to"
                " the branch it was explicitly told about."
            )

    # The one database that must never receive demo data, checked whenever
    # this machine knows what it is. There is no equivalent check for the
    # development branch -- it is the intended target, not a forbidden one.
    test_host = host_of(test_database_url)
    if current_host is not None and test_host is not None and current_host == test_host:
        reasons.append(
            "DATABASE_URL points at the same host as TEST_DATABASE_URL."
            " The integration-test branch must not be seeded."
        )

    return GuardOutcome(reasons=tuple(reasons))


def host_of_setting(value: str | None) -> str | None:
    """Normalize the configured demo host.

    Accepts either a bare hostname or a full URL, because both are things a
    person reasonably pastes into an environment file, and a mismatch caused by
    formatting would be a confusing refusal rather than a useful one.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if "://" in candidate:
        return host_of(candidate)
    # Tolerate "host:5432" and a stray trailing slash.
    candidate = candidate.rstrip("/").split("/")[0]
    if candidate.count(":") == 1:
        candidate = candidate.split(":")[0]
    return candidate.lower() or None
