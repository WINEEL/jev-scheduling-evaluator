"""The FastAPI application.

Two things live at the root: the unversioned ``/health`` probe, and the
versioned product API mounted under ``/api/v1`` -- one router per resource,
each mounted at the same prefix. Health stays where it is and
outside the version prefix on purpose -- a liveness check is infrastructure, not
product, and moving it would break whatever already watches it for the sake of
tidiness.

**No CORS middleware is configured, and none is needed.** The browser never
calls this service directly: the Next.js frontend proxies every request on its
own origin (``/api/backend/...``), so every request that arrives here is
same-origin from the browser's point of view and there is no cross-origin
policy to relax. That is a deliberate topology choice, not an omission -- it is
also what lets the session cookie be an ordinary ``SameSite=Lax`` first-party
cookie rather than a cross-site one.

**Authentication is Google sign-in over a signed session cookie** (Task 76).
The middleware that carries the session is installed here; the flow that fills
it lives in :mod:`app.api.routes_auth`, and the point where a request becomes an
actor is :func:`app.api.dependencies.get_current_actor`.
"""

from __future__ import annotations

import logging
import secrets
import sys

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app.api import (
    routes_auth,
    routes_availability,
    routes_scheduling_rules,
    routes_ministries,
    routes_ministry_roles,
    routes_people,
    routes_role_qualifications,
    routes_schedule_entry,
    routes_schedule_versions,
    routes_serving_limits,
    routes_staffing_requirements,
    v1,
)
from app.api.errors import register_exception_handlers
from app.auth.session import SESSION_COOKIE_NAME, SESSION_MAX_AGE_SECONDS
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


def configure_application_logging() -> None:
    """Make this application's own INFO records actually reach the log.

    **Found by the first real sign-in, not by a test.** Sign-in records one line
    per outcome -- who signed in, and that somebody was refused -- at INFO.
    Under uvicorn those lines never appeared: uvicorn configures its *own*
    loggers and leaves the root logger at its default WARNING with no handler,
    so every ``logger.info`` in ``app.*`` was created and then dropped. The
    warnings came through, which is exactly what made it easy to miss.

    The offline tests did not catch it because ``caplog.at_level(DEBUG)``
    *forces* the level for the duration of a test, proving the call is made but
    not that the configuration lets it out. Hence
    :func:`test_34_the_application_logger_emits_info_in_a_default_process`,
    which asserts the configuration rather than the call.

    Two deliberately narrow decisions:

    - Only the ``app`` logger's level is set. Turning the *root* logger down to
      INFO would also unleash every third-party library's INFO chatter --
      SQLAlchemy, httpx, Authlib -- into Cloud Logging.
    - A handler is added only if nothing else has installed one. Under uvicorn
      the root logger has none, so one is needed; under pytest the logging
      plugin has already installed its own, and adding a second would duplicate
      every captured record.

    stdout, because that is what Cloud Run collects.
    """
    logging.getLogger("app").setLevel(logging.INFO)
    if not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter("%(levelname)s:     %(name)s - %(message)s")
        )
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.WARNING)


def session_middleware_options(settings: Settings) -> dict[str, object]:
    """How the session cookie is configured, as a value a test can inspect.

    Split out from the ``add_middleware`` call because these five settings *are*
    the cookie's security, and asserting on them through an HTTP round trip
    would test the ``Set-Cookie`` header's formatting as much as the policy.

    - ``https_only`` follows ``APP_ENV``. In production the cookie carries
      ``Secure``, so a browser will not send it over plain HTTP at all. It is
      off locally for the one good reason: ``http://localhost`` would otherwise
      never receive it, and no developer would be able to sign in.
    - ``same_site="lax"`` is the strongest value the flow permits, and the
      reasoning is worth keeping. ``strict`` would break sign-in outright: the
      OAuth callback is a top-level navigation arriving *from
      accounts.google.com*, and a strict cookie is withheld on exactly that, so
      the ``state`` Authlib stored before the redirect would be missing when it
      came back. ``lax`` sends the cookie on top-level GET navigations -- which
      is the callback -- and withholds it from cross-site POSTs, which is what
      makes CSRF on the mutation endpoints and on logout a non-issue.
    - ``max_age`` is enforced cryptographically by itsdangerous against the
      signature's own timestamp, not by trusting the browser to honour the
      cookie's expiry.
    - ``httponly`` is not listed because Starlette sets it unconditionally;
      there is no option to turn it off, which is the right default.
    """
    return {
        "secret_key": _session_secret(settings),
        "session_cookie": SESSION_COOKIE_NAME,
        "max_age": SESSION_MAX_AGE_SECONDS,
        "same_site": "lax",
        "https_only": settings.is_production,
    }


def _session_secret(settings: Settings) -> str:
    """The signing key, or a refusal to start.

    **Production fails closed and loudly.** A deployment with no
    ``SESSION_SECRET`` must not start, because the alternatives are both worse
    than a crash: an empty key signs sessions anybody can forge, and a generated
    one would silently sign every user out whenever Cloud Run started a new
    instance -- which, at minimum instances 0, is constantly.

    **Locally, a random per-process key is generated instead.** Running the API
    to look at a page should not require configuring authentication first. The
    cost is that sessions do not survive a restart, which is the correct
    trade-off for a process that restarts on every file save.

    Only setting *names* appear in the error. Nothing here reads, logs or
    interpolates a secret's value.
    """
    configured = settings.session_secret.strip()
    if configured:
        return configured

    if settings.is_production:
        missing = ", ".join(settings.missing_google_oauth_settings())
        raise RuntimeError(
            "Refusing to start a production deployment without Google sign-in"
            f" configured. Missing: {missing}."
        )

    logger.warning(
        "SESSION_SECRET is not set; generating a temporary key for this process."
        " Sessions will not survive a restart. This is local-development"
        " behaviour and is refused when APP_ENV=production."
    )
    return secrets.token_urlsafe(32)


configure_application_logging()

app = FastAPI(title="Church Scheduling App API")

_settings = get_settings()

# Outermost middleware, so the session is decoded before any route or
# dependency looks for an actor.
app.add_middleware(SessionMiddleware, **session_middleware_options(_settings))

if _settings.is_production and not _settings.google_oauth_configured:
    # Reached only when SESSION_SECRET is set but a Google credential is not --
    # _session_secret() already refuses the other combinations. Named settings
    # only; never a value.
    raise RuntimeError(
        "Refusing to start a production deployment without Google sign-in"
        f" configured. Missing: {', '.join(_settings.missing_google_oauth_settings())}."
    )

register_exception_handlers(app)
app.include_router(routes_auth.router, prefix="/api/v1")
app.include_router(v1.router, prefix="/api/v1")
app.include_router(routes_schedule_entry.router, prefix="/api/v1")
app.include_router(routes_schedule_versions.router, prefix="/api/v1")
app.include_router(routes_ministry_roles.router, prefix="/api/v1")
app.include_router(routes_ministries.router, prefix="/api/v1")
app.include_router(routes_people.router, prefix="/api/v1")
app.include_router(routes_staffing_requirements.router, prefix="/api/v1")
app.include_router(routes_role_qualifications.router, prefix="/api/v1")
app.include_router(routes_availability.router, prefix="/api/v1")
app.include_router(routes_serving_limits.router, prefix="/api/v1")
app.include_router(routes_scheduling_rules.router, prefix="/api/v1")


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the application is alive."""
    return {"status": "ok"}
