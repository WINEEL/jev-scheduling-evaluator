# The scheduling UI, for Cloud Run.
#
# Built from the **repository root** (see cloudbuild.yaml) so that one
# .dockerignore governs both services' build contexts -- and, in particular, so
# that the rule keeping local_data/ out of any upload is written once.
#
# Two stages. The builder needs the full dependency tree and the toolchain; the
# runtime image needs neither, and every megabyte of it is cold-start time on a
# service that scales to zero.
#
# **Nothing secret is built in**, and for this service that is not only about
# credentials. Next.js inlines any NEXT_PUBLIC_* variable into the JavaScript
# it ships to the browser -- so a secret given to the build would be *published*,
# not merely stored. This app deliberately uses no NEXT_PUBLIC_* variable at
# all: the backend's address and the pilot configuration are read on the server,
# per request, from Cloud Run's environment.

# --- Stage 1: build -------------------------------------------------------
FROM node:22-slim AS builder

WORKDIR /app

# Lockfile first, so the install layer is reused when only source changes.
# `npm ci` rather than `npm install`: it installs exactly what the lockfile
# says and fails if the two disagree, which is what makes a build repeatable.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

# NODE_ENV=production here is what makes `isDevActorForwardingAllowed` refuse:
# the development actor header cannot be forwarded from an image built this
# way, whatever is later set in the environment. See src/lib/devAuth.ts.
ENV NODE_ENV=production
RUN npm run build

# --- Stage 2: run ---------------------------------------------------------
FROM node:22-slim AS runtime

ENV NODE_ENV=production \
    PORT=8080 \
    HOSTNAME=0.0.0.0

WORKDIR /app

# Not root, for the same reason as the API image.
RUN useradd --create-home --uid 10001 appuser

# The standalone output carries its own minimal node_modules and server.js.
# `static` and `public` are not traced into it and have to be copied beside it,
# or every stylesheet and script 404s -- a failure that looks like a broken
# stylesheet rather than a missing file, so it is worth stating.
COPY --from=builder --chown=appuser:appuser /app/.next/standalone ./
COPY --from=builder --chown=appuser:appuser /app/.next/static ./.next/static

USER appuser
EXPOSE 8080

# HOSTNAME=0.0.0.0 above is required: the standalone server binds to localhost
# by default, and a container that only listens on localhost fails Cloud Run's
# health check with no obvious reason why.
CMD ["node", "server.js"]
