"""Google sign-in: identity, session, and the link between the two.

Four modules, split by the question each answers:

- :mod:`app.auth.email_link` -- *which Person is this address?* The normalization
  rule and the exact lookup, shared by the OAuth callback and the admin CLI so
  the two can never disagree about what "the same email" means.
- :mod:`app.auth.google` -- *is this really them?* The OAuth client and the
  claims check.
- :mod:`app.auth.session` -- *how is that remembered?* Reading and writing the
  signed session cookie.
- :mod:`app.auth.errors` -- the refusals the first three raise.

Authorization is deliberately absent. These modules establish **who** the caller
is and stop there; **what they may do** stays in
:mod:`app.services.authorization`, exactly where it was before sign-in existed.
"""
