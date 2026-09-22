"""Application configuration.

Only the values needed for the database foundation are defined here. Settings
are read from the process environment and, as a fallback, from the
repository-root ``.env`` file. This module never emits the database
credentials: use :func:`redact_url` whenever a URL needs to be shown.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[2] is the repository root. Resolving from the
# module location means the root ``.env`` is found no matter what the current
# working directory is (e.g. running from ``backend/``).
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseSettings):
    """Backend settings for the database foundation."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # No default: the application must be told which database to use.
    database_url: str
    app_env: str = "development"

    # -- Google sign-in (Task 76) ------------------------------------------
    #
    # All three are optional *as configuration* and required *as a runtime
    # precondition*: the application must import, and the test suite must run,
    # on a machine that has never seen a Google client. What must not happen is
    # a deployment that silently starts with sign-in broken, so
    # :meth:`require_google_oauth` refuses at the point of use instead, and
    # :func:`app.main` refuses at startup in production.
    #
    #: The OAuth web client's id. Public by nature -- it is sent to the browser
    #: as a query parameter on the way to Google -- but configured through the
    #: same channel as the secret so there is one procedure, not two.
    google_oauth_client_id: str = ""
    #: The OAuth web client's secret. **Never** leaves the server: it is used
    #: only to authenticate this service to Google's token endpoint.
    google_oauth_client_secret: str = ""
    #: Signing key for the session cookie. Rotating it invalidates every live
    #: session, which is the intended emergency logout-everybody lever.
    session_secret: str = ""
    #: The absolute, public ``https://`` URL Google redirects back to, which
    #: must match an Authorized redirect URI on the OAuth client *exactly*.
    #: Left blank locally, where the request's own base URL is correct and
    #: stable; set explicitly in production because Cloud Run terminates TLS
    #: upstream and the request FastAPI sees claims to be plain ``http``.
    oauth_redirect_url: str = ""
    #: **Local development only.** When set to exactly ``"1"``, the API accepts
    #: an ``X-Dev-Actor-Person-Id`` header as the acting Person, so the UI and
    #: API can be worked on before Google authentication exists.
    #:
    #: **This must never be enabled in a deployed environment.** It lets anyone
    #: who can reach the service act as any Person, including an Admin, simply
    #: by naming their id. It is a stand-in for authentication, not
    #: authentication.
    #:
    #: A plain string rather than ``bool`` on purpose: pydantic's bool parsing
    #: accepts ``true``/``yes``/``on``, and a mechanism this dangerous should
    #: turn on for exactly one value and nothing else. See
    #: :attr:`dev_actor_auth_enabled`.
    church_scheduling_dev_auth: str = ""

    @property
    def is_production(self) -> bool:
        """Whether this process considers itself a production deployment.

        Compared case-insensitively and after trimming, unlike
        :attr:`dev_actor_auth_enabled`'s exact-match rule, and the asymmetry is
        deliberate in both directions. The dev-auth flag *grants* a dangerous
        power, so it must be impossible to switch on by accident: exactly one
        spelling works. This property *withdraws* one, so it must be impossible
        to switch **off** by accident: ``"Production"`` and ``" production "``
        are plainly someone naming the production environment, and reading them
        as "not production" would re-enable the header on the one deployment
        that must never have it.
        """
        return self.app_env.strip().lower() == "production"

    @property
    def dev_actor_auth_enabled(self) -> bool:
        """Whether the development actor header is accepted.

        Fails closed twice over, and the second gate is the one Task 76 added:

        1. the flag must be the exact string ``"1"`` -- unset, empty, ``"0"``,
           ``"true"``, ``" 1"`` and every other value mean disabled;
        2. **and this must not be a production deployment.**

        The second condition is not redundant with "don't set the flag in
        production". Anyone who can set an environment variable on the service
        could set the flag too -- through a misapplied Cloud Run revision, a
        copied deployment command, or a ``.env`` that reaches a container it
        was never meant to. Making ``APP_ENV=production`` *override* the flag
        means that deployment authenticates through Google or not at all, and
        no combination of environment variables can talk it out of that.
        """
        if self.is_production:
            return False
        return self.church_scheduling_dev_auth == "1"

    @property
    def google_oauth_configured(self) -> bool:
        """Whether Google sign-in has everything it needs to run.

        Blank-safe rather than merely None-safe: an unset Cloud Run environment
        variable and one set to the empty string arrive identically here, and
        neither is a credential.
        """
        return bool(
            self.google_oauth_client_id.strip()
            and self.google_oauth_client_secret.strip()
            and self.session_secret.strip()
        )

    def missing_google_oauth_settings(self) -> list[str]:
        """The names -- never the values -- of the absent sign-in settings.

        Returned as names so a startup failure or an operator-facing message
        can say what to set without any chance of echoing a secret into a log,
        a terminal or an exception a request handler might render.
        """
        missing = []
        if not self.google_oauth_client_id.strip():
            missing.append("GOOGLE_OAUTH_CLIENT_ID")
        if not self.google_oauth_client_secret.strip():
            missing.append("GOOGLE_OAUTH_CLIENT_SECRET")
        if not self.session_secret.strip():
            missing.append("SESSION_SECRET")
        return missing


def redact_url(url: str) -> str:
    """Return ``url`` with any user/password portion removed.

    Safe to include in logs and error messages. Keeps scheme, host, port and
    database path so the target is still identifiable.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<unparseable database url>"
    if not parts.hostname:
        return "<database url with no host>"
    netloc = parts.hostname
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def load_settings(env_file: Path | str | None = ENV_FILE) -> Settings:
    """Build :class:`Settings`, raising a clear error when required values are absent.

    The raised ``RuntimeError`` deliberately does not chain the underlying
    validation error so that no configured value can leak into a traceback.
    """
    try:
        return Settings(_env_file=env_file)  # type: ignore[call-arg]
    except ValidationError as exc:
        missing = sorted(
            str(err["loc"][0]).upper()
            for err in exc.errors()
            if err.get("type") == "missing"
        )
        if missing:
            raise RuntimeError(
                f"Missing required configuration: {', '.join(missing)}. "
                f"Set it in the environment or in {ENV_FILE}."
            ) from None
        raise RuntimeError(
            "Invalid backend configuration; check the values in the environment "
            f"or {ENV_FILE}."
        ) from None


@lru_cache
def get_settings() -> Settings:
    """Return the cached application settings."""
    return load_settings()
