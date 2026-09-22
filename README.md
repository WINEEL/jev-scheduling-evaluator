# Church Scheduling App

A configurable church ministry scheduling application. It helps coordinators build
volunteer schedules for ministries based on people, roles, availability, and
constraints. The Setup ministry is the first complete validation case, but the
architecture is intended to be configurable and not hard-coded for any single
ministry.

## Current status

The database schema, the domain service layer, and the OR-Tools scheduling
engine are implemented and tested, and the API and frontend now cover both
halves of a Ministry Head's work. Running the backend and frontend locally, a
Ministry Head can:

- **configure a ministry** — its roles, each event's staffing requirements, who
  is qualified for which role, everyone's availability (available, backup, or
  unavailable), and each person's maximum assignments for the period;
- **close availability** for the period, which is what starting a schedule
  requires — the one step in the middle of this flow that used to have no
  interface at all;
- **schedule** — pick a scheduling period, start its first schedule, generate
  it, and review the assignments along with anything left unfilled and why.

**Google sign-in is implemented** (Task 76), and the app is deployable to Cloud
Run. A person signs in with Google, and is let in only if an administrator has
already linked their email address to an existing `Person` — there is no
registration, no invitation and no self-service onboarding, by design. Signing
in establishes *who* somebody is; the existing Admin and Ministry Head rules
still decide *what* they may do. See
[`docs/architecture/authentication.md`](docs/architecture/authentication.md) and
[`docs/deployment/cloud-run.md`](docs/deployment/cloud-run.md).

What is still missing, and it is not small: no way to create a person, a
ministry, a membership or a scheduling period from the product; no screen or
endpoint for submitting a schedule for review, finalizing it, or editing
assignments by hand; and nothing for a volunteer to look at — the pilot is for
ministry leaders only.

**The immediate milestone is feedback**, not more features: the next step is to
put real candidate schedules and the eligibility assumptions behind them in
front of ministry leads before any further scheduling rules are added.

For what the product is meant to do and exactly what is built, see
[`docs/requirements/v1-requirements-and-status.md`](docs/requirements/v1-requirements-and-status.md)
— §11 for status, §16 for known limitations, §17 for the milestone.

## Running it locally

Two terminals. See [`backend/README.md`](backend/README.md) and
[`frontend/README.md`](frontend/README.md) for the details, including the
local-development environment variables each needs.

```bash
# Terminal 1 — backend (from backend/, virtualenv active)
uvicorn app.main:app --reload --port 8000

# Terminal 2 — frontend (from frontend/)
npm install && npm run dev
```

Then open <http://localhost:3000> and sign in with Google.

**Use `localhost`, not `127.0.0.1`.** They are different cookie hosts to a
browser, and the OAuth redirect URI registered with Google names `localhost` —
opening the other one signs you in to an origin the session cookie was not set
for.

Sign-in needs four values in the repository-root `.env`
(`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `SESSION_SECRET`,
`OAUTH_REDIRECT_URL`) and an address linked to your `Person` row. See
[`docs/architecture/authentication.md`](docs/architecture/authentication.md)
§8 for the setup and §9 for the linking command.

To work on the UI without a Google round trip, the development identity is
still available — add `CHURCH_SCHEDULING_DEV_AUTH=1` to the backend command and
set `CHURCH_SCHEDULING_FRONTEND_DEV_AUTH=1` with
`CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID` in `frontend/.env.local`.

To put data in front of it — fictional, or a real roster read from a
git-ignored directory and written only to a local development database — see
`backend/README.md` §4c and §4d. To show the whole flow to somebody, see
[`docs/demo/local-demo-walkthrough.md`](docs/demo/local-demo-walkthrough.md);
to explain what it is and is not, for someone who will not be at a keyboard,
see [`docs/demo/pilot-brief.md`](docs/demo/pilot-brief.md).

> The development identity mechanism is **not authentication** and cannot be
> used in a deployed environment: `APP_ENV=production` disables it outright,
> whatever else is set. A real session always takes precedence over it.

## Approved technology stack (high level)

| Area              | Choice                                |
| ----------------- | ------------------------------------- |
| Frontend          | Next.js + TypeScript                  |
| Backend           | Python + FastAPI                      |
| Database          | PostgreSQL (Neon)                     |
| ORM               | SQLAlchemy                            |
| Migrations        | Alembic                              |
| Scheduling engine | Google OR-Tools CP-SAT               |
| Authentication    | Google Sign-In / OAuth (added later) |
| Repository        | Single private monorepo              |

## Repository layout

| Path          | Purpose                                                        |
| ------------- | ------------------------------------------------------------- |
| `frontend/`   | Next.js + TypeScript web client — the Ministry Head scheduling screens |
| `backend/`    | Python + FastAPI service and scheduling engine               |
| `docs/`       | Project documentation                                        |
| `docs/requirements/` | What V1 must do, and what is implemented              |
| `docs/demo/`  | How to demonstrate the product locally, with no private data  |
| `docs/architecture/` | Data-model and architecture designs                   |
| `docs/adr/`   | Architecture Decision Records                                |
| `docs/validation/` | How the engine was validated against real ministry data — method and privacy-safe results only |
| `docs/intake/` | Getting a *second* ministry's real records in: source configuration, dry-run validation, and the production-readiness gate |
| `sample-data/`| Synthetic / anonymized sample data only                      |

## Deployment

Deployment is intentionally deferred. There is no deployment, container, or CI/CD
configuration in this repository yet, and none should be added until explicitly
approved.

## Repository visibility

This repository is intended to remain **private** for now.

## Data and secrets - important

- Never commit real church data (member names, contact details, rosters, etc.).
- Never commit secrets: `.env` files, database credentials, OAuth/client secrets,
  or any production credentials.
- Anything under `sample-data/` must be synthetic or fully anonymized.
- Real inputs and any generated roster live only under a git-ignored
  `local_data/` directory. The validation runners refuse to read an input
  directory or write a report directory that is not ignored, and print counts
  rather than names. Documentation records aggregate findings only.
- Keep development, test, and production data environments separated. Do not point
  local development at production data, and do not copy production data into
  development or test environments.

Use `.env.example` as the template for local configuration; copy it to `.env`
(which is git-ignored) and fill in your own local values.

## Documentation

| Document | Purpose |
| --- | --- |
| `docs/requirements/v1-requirements-and-status.md` | Product requirements, and what is implemented vs deferred |
| `docs/demo/local-demo-walkthrough.md` | How to run a five-minute end-to-end demonstration locally |
| `docs/demo/pilot-brief.md` | A one-page, generic explanation of what the app helps with, what it does not do yet, and what a ministry head would need to provide |
| `docs/architecture/` | Data-model and architecture designs, one per schema slice |
| `docs/adr/` | Architecture Decision Records |
| `docs/validation/` | Validation method and privacy-safe results for the Setup and AV ministries |
| `docs/intake/` | What a second ministry still needs before it can go live, and the short list of questions only a Ministry Head can answer |

Significant architecture and technology decisions are recorded as ADRs under
`docs/adr/`. See `docs/adr/README.md` for the format and naming convention.
