"""Domain exceptions, mapped to status codes once.

The service layer raises exactly two errors deliberately
(:mod:`app.services.errors`), and each answers a different question for the
caller, so each gets its own status code:

- :class:`~app.services.errors.AuthorizationError` -- *you* may not do this.
  **403 Forbidden.** Identity was established; permission was not. It is never
  401, which would invite a client to re-authenticate over a decision that will
  not change.
- :class:`~app.services.errors.InvalidOperationError` -- this cannot be done to
  *this object* in its current state, by anyone. **409 Conflict.** Not 400: the
  request was well-formed and the client could not reasonably have known the
  version had been superseded, or the schedule had gone stale. 409 says "the
  state disagrees with you", which is exactly what happened.

One exception from the pure scheduling package is mapped too:

- :class:`~app.scheduling.solver.SchedulingInputError` -- the values given
  cannot be scheduled at all. **422 Unprocessable Entity.** The request parsed
  as JSON and matched the schema, so 400 would be wrong; what failed is the
  meaning of the values, which is precisely what 422 is for. Its sibling
  ``SchedulingEngineError`` is deliberately **not** mapped: that one means the
  solver itself failed, which is a server fault and must stay a 500 rather
  than be dressed up as the caller's mistake.

Mapping it here rather than in :mod:`app.scheduling` is the point: the
scheduling package knows nothing about HTTP and must not start to.

**Registered centrally so endpoints do not repeat themselves.** Without this,
every future endpoint would wrap its service call in the same ``try``/``except``
and one of them would eventually get it wrong.

**Two things this deliberately does not do.** It does not register a handler for
the ``ServiceError`` base class -- a domain error that is neither of the two
above is an error nobody has decided the meaning of, and it should surface as a
500 rather than be guessed into a plausible-looking 4xx. And it catches no
general ``Exception``: unexpected failures continue through FastAPI's normal
error handling, which logs them and returns a bare 500 without a stack trace in
the body.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.scheduling.solver import SchedulingInputError
from app.services.errors import AuthorizationError, InvalidOperationError

__all__ = ["register_exception_handlers"]


async def authorization_error_handler(
    request: Request, exc: AuthorizationError
) -> JSONResponse:
    """403. The domain messages are written for humans and name no internals."""
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)}
    )


async def invalid_operation_error_handler(
    request: Request, exc: InvalidOperationError
) -> JSONResponse:
    """409."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)}
    )


async def scheduling_input_error_handler(
    request: Request, exc: SchedulingInputError
) -> JSONResponse:
    """422. Raised by the pure scheduling layer, mapped only here."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content={"detail": str(exc)}
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the handlers to ``app``.

    ``{"detail": "..."}`` matches the shape FastAPI's own ``HTTPException``
    already produces, so a client parses one error format rather than two.
    """
    app.add_exception_handler(AuthorizationError, authorization_error_handler)
    app.add_exception_handler(InvalidOperationError, invalid_operation_error_handler)
    app.add_exception_handler(SchedulingInputError, scheduling_input_error_handler)
