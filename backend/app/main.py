"""The application. One router, one credential, no configuration.

    python -m uvicorn app.main:app --reload --port 8000

**One environment variable, and it is ``TYPESAFE_API_KEY``** -- read by the
TypeSafe SDK itself, at the moment an evaluation is requested. Nothing is
needed to start this server -- no connection string, no credential, not even
``APP_ENV``. Without the key the server still starts and the
scenario menu still works; only ``/evaluate`` fails, and it says so.

**Production is refused, and that is the one guard here.**
``APP_ENV=production`` makes this module refuse to build at all. Not because
the demo leaks anything -- every volunteer in it is invented and no database is
read -- but because this is an experiment with uncalibrated thresholds, and a
process serving a fiction should not quietly become part of somebody's
infrastructure.

**It imports no database, no ORM and no settings framework**, which is what
makes "no stored record is involved anywhere in this project" a checkable
statement rather than a claim in a docstring. ``tests/test_main.py`` asserts
the whole import graph in a clean subprocess with the environment stripped
bare.

There is deliberately no ``/health``, no sign-in, and no endpoint but the two
this demo needs.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

from app.api import routes

__all__ = ["app", "build_app", "is_production_environment"]

logger = logging.getLogger(__name__)

#: Names the environment this process thinks it is in. The only variable this
#: module reads, and it is read only to refuse.
APP_ENV_VAR = "APP_ENV"


def is_production_environment(app_env: str) -> bool:
    """Whether ``app_env`` names the production environment.

    Trimmed and compared case-insensitively, so ``"Production"`` and
    ``" production "`` both count. A rule that *withdraws* a capability must be
    impossible to switch off by accident, so every plausible spelling of
    "production" counts as production.
    """
    return app_env.strip().lower() == "production"


def build_app() -> FastAPI:
    """The demo application, or a refusal to start in production.

    Raising rather than serving an empty app is deliberate: a demo server that
    started and then answered 404 on every path would look like a bug in the
    frontend. The message names the setting, never a value.
    """
    app_env = os.environ.get(APP_ENV_VAR, "")
    if is_production_environment(app_env):
        raise RuntimeError(
            f"Refusing to start the synthetic Jev demo with {APP_ENV_VAR}"
            "=production. This server answers with invented data and has no"
            " authentication; nothing about it is fit for a production"
            " deployment."
        )

    logger.warning(
        "Serving the synthetic Jev soft-constraint demo only. Invented data,"
        " no database, no authentication, no scheduling API. Set"
        " TYPESAFE_API_KEY to run a live evaluation."
    )

    demo = FastAPI(
        title="Synthetic Jev soft-constraint demo",
        description=(
            "Three invented scheduling drafts, judged by TypeSafe's Jev and "
            "decided by deterministic Python. Not the scheduling API."
        ),
    )
    demo.include_router(routes.router, prefix="/api/v1")
    return demo


#: Built at import so ``uvicorn app.main:app`` works with no
#: ``--factory`` flag. Safe to evaluate here precisely because the default is
#: to run: the only thing that raises is an explicit ``APP_ENV=production``,
#: which is a refusal somebody needs to see at start-up rather than on the
#: first request.
app = build_app()
