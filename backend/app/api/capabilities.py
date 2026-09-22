"""The one capability field ministry-scoped reads carry, described once.

Every read endpoint scoped to a single ministry returns ``can_operate``: whether
**this caller** may perform operational writes on **that ministry**. It is
:func:`app.services.authorization.can_operate_ministry` -- the same rule every
write enforces, asked as a question instead of applied as a gate.

**Why the field exists at all.** Task 80 split read authority from write
authority: an Admin who heads no ministry may open any ministry's roles,
availability, limits, rules and schedules, and may change none of them. A client
therefore cannot work out which controls are real from ``is_admin`` and
``headed_ministries`` without reimplementing the server's rule -- and a
reimplemented authorization rule is one that will eventually disagree with the
original. The server answers instead.

**It authorizes nothing, and no client may treat it as though it did.** Every
write re-checks with
:func:`~app.services.authorization.require_ministry_operator`, in the service,
on every request. A caller that ignores ``can_operate: false`` and posts anyway
gets 403 exactly as if the field had never been read; a caller that forges it
locally changes nothing. What it buys is a UI that tells the truth -- no button
that leads only to a refusal, and no read-only screen pretending to be
editable.

**Read authority is not reported.** There is no ``can_read`` companion, because
a response the caller is holding is already proof of it: a reader who may not
see the ministry got a 403 instead of this body.
"""

from __future__ import annotations

__all__ = ["CAN_OPERATE_DESCRIPTION"]

CAN_OPERATE_DESCRIPTION = (
    "Whether the signed-in caller may perform operational writes on this"
    " ministry — that is, whether they are an active Ministry Head of it."
    " An Admin who does not head this ministry reads it with `false`."
    " Advisory only: every write is authorized again on the server."
)
