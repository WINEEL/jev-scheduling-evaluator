"""The HTTP boundary.

Everything here translates between HTTP and the domain, and does nothing else:
no scheduling rules, no authorization decisions about ministries, no queries a
service already owns. The layering is deliberate --

- :mod:`app.api.dependencies` answers *who is making this request* and hands out
  the request's database Session;
- :mod:`app.api.schemas` defines what goes out on the wire, separately from the
  ORM;
- :mod:`app.api.errors` turns the established domain exceptions into status
  codes once, centrally, instead of a ``try``/``except`` in every endpoint;
- :mod:`app.api.v1` is the versioned product API.

**Authorization stays in the services.** The boundary establishes identity; the
domain decides what that identity may do. Those are different questions, and
keeping them apart is what lets :mod:`app.services.authorization` hold the whole
answer to the second one -- `require_ministry_reader` for an oversight read,
`require_ministry_operator` for an operational write (Task 80).

**A route may *report* an authorization answer without *making* one.** Read
endpoints return a ``can_operate`` field, which is
:func:`~app.services.authorization.can_operate_ministry` -- the operator rule
asked as a question rather than enforced as a gate -- so a client can render the
controls that are real for its caller. It authorizes nothing: every write
re-checks in the service, on every request, and a client that ignores the field
gets a 403 exactly as if it had never read it.
"""
