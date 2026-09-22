"""Test package.

``tests`` is a package so that ``tests.integration`` -- which needs its own
``factories`` module -- can be imported by its full, unambiguous name under
any pytest invocation. Without this file the ``integration`` subpackage's
first non-package ancestor is ``backend/tests``, so pytest puts *that* on
``sys.path`` and ``tests`` itself is not importable: ``pytest`` (the console
script) then fails at collection, while ``python -m pytest`` happens to work
only because it adds the working directory. Making the hierarchy a package
removes that difference.
"""
