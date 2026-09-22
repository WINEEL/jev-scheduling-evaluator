"""Domain/service exceptions.

Deliberately three types, not a hierarchy. Each answers a different question the
caller has to act on differently:

- :class:`AuthorizationError` -- *you* may not do this.
- :class:`InvalidOperationError` -- this cannot be done to *this object*, in its
  current state, by anyone.

Both derive from :class:`ServiceError` so a caller may catch the whole category
when that is genuinely what it wants.

There is no ``NotFoundError``: the operations in this slice receive already
loaded ORM objects, so "not found" is the caller's problem and never arises
inside a service. One will be added when a service actually looks something up.

**These carry no HTTP semantics.** Mapping them to status codes is the API
layer's job, and the API layer is a later slice.
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for every error a domain service raises deliberately."""


class AuthorizationError(ServiceError):
    """The actor is not permitted to perform this operation.

    Raised *before* any mutation, so a caller that catches this knows nothing
    was written -- no domain change, and no audit row.
    """


class InvalidOperationError(ServiceError):
    """The operation is not valid against the target's current state.

    Used for domain rules the caller could not reasonably have known were
    violated -- granting head authority to a deactivated membership, or
    supplying a blank reason. Raised before any mutation.
    """
