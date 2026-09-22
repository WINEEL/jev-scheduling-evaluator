# The scheduling API, for Cloud Run.
#
# Built from the **repository root** (see cloudbuild.yaml), not from backend/,
# for one reason: app/config.py resolves the repository root from the module's
# own location and looks for a .env two directories up. Keeping the same
# directory shape in the image means that lookup behaves in the container
# exactly as it does locally -- it simply finds no .env, which is correct,
# because every deployed value arrives from the environment.
#
# **Nothing secret is built in.** No .env, no credentials, no database URL, no
# church data. A container image is not a private place: anyone who can pull it
# can read every layer. Configuration arrives at run time from Cloud Run, and
# secrets from Secret Manager.

FROM python:3.13-slim

# - PYTHONUNBUFFERED so log lines reach Cloud Logging as they happen rather
#   than sitting in a buffer until the process exits.
# - PYTHONDONTWRITEBYTECODE because the filesystem is ephemeral and .pyc files
#   would only make the image bigger.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# The installable package -- pyproject.toml, its README, and app/ -- then the
# install.
#
# **app/ must be copied before the install, not after.** pyproject.toml declares
# `packages = ["app"]`, so setuptools reads that directory while generating the
# package metadata; installing first and copying second fails at
# `error: package directory 'app' does not exist`. That is not a theoretical
# ordering nicety: it failed the first real Cloud Build, in the metadata step,
# before a single dependency was downloaded.
#
# The cost is honest and small: a change to app/ now invalidates the dependency
# layer too, so an ordinary code deployment re-runs pip. Splitting the two would
# mean either installing dependencies from a generated requirements file or
# staging an empty app/ to satisfy the metadata build -- both more machinery
# than a pilot that deploys a few times a week needs.
#
# The README is copied because pyproject.toml's metadata refers to it; without
# it the install fails on a file that has nothing to do with anything.
COPY backend/pyproject.toml backend/README.md ./backend/
COPY backend/app ./backend/app
RUN python -m pip install --upgrade pip \
 && python -m pip install ./backend

# Everything else the image needs, copied after the install because none of it
# is part of the installed package (`packages = ["app"]`) -- so changing a
# migration or an admin script does not re-run pip.
#
# **alembic/ and alembic.ini are load-bearing in production, not a
# convenience** (Task 80 deployment follow-up). This image has two entry
# points: the default CMD serves the API, and
# `python -m alembic upgrade head` migrates the database. Cloud Build runs the
# second one against this exact image before the first one is given traffic,
# which is what makes "the schema the code expects" and "the schema that is
# there" the same statement rather than two things kept in step by hand.
# Removing either of these COPY lines would leave the image able to start and
# unable to migrate -- so `tests/test_deployment_pipeline.py` asserts they are
# here.
COPY backend/alembic ./backend/alembic
COPY backend/alembic.ini ./backend/
# scripts/ carries the admin CLIs -- email linking, and the import tools that
# keep their own production guards. They are included so an operator can run
# `docker run ... python -m scripts.link_person_email` against this exact
# image; none of them is reachable over HTTP.
COPY backend/scripts ./backend/scripts

WORKDIR /srv/backend

# Not root. Cloud Run does not require it, but a process that never needs to
# write to its own filesystem should not be able to.
RUN useradd --create-home --uid 10001 appuser \
 && chown -R appuser:appuser /srv
USER appuser

# Cloud Run supplies PORT and it is not always 8080. Honouring it is what makes
# the container portable to a local `docker run -e PORT=9000` as well.
ENV PORT=8080
EXPOSE 8080

# Shell form, deliberately: ${PORT} has to be expanded, and exec form would
# pass the literal string. `exec` keeps uvicorn as PID 1 so Cloud Run's SIGTERM
# reaches it and shutdown is graceful rather than a 10-second kill.
#
# One worker: Cloud Run scales by adding instances, not by adding processes
# inside one, and a second worker would only compete for the same CPU
# allocation.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1
