# Backend

The FastAPI service and scheduling engine for the Church Scheduling App.

Current scope: the full domain model and its migrations, the domain service
layer, the OR-Tools CP-SAT scheduling engine, an audit trail for every domain
mutation, and the `/api/v1` endpoints a Ministry Head's screens use — for
configuration (roles, staffing requirements, role qualifications, availability,
serving limits, per-period scheduling rules) and for scheduling (list periods,
start a schedule, generate it,
review it).

**There is no authentication.** Identity comes from a development-only header
that must never be enabled in a deployed environment; see §4b below. Several
implemented services still have no endpoint — creating a scheduling period,
manual assignment, submitting for review, finalizing, and
configuring a linked-pair same-date exclusion. `docs/requirements/v1-requirements-and-status.md`
§11 and §16 are the authoritative account of what is and is not reachable.

## Database configuration

The backend reads `DATABASE_URL` from the process environment or, as a
fallback, the repository-root `.env` (git-ignored). Local development uses the
Neon development branch. The URL must use the synchronous psycopg 3 driver:

```
postgresql+psycopg://USER:PASSWORD@HOST/DBNAME?sslmode=require
```

There is no default URL; the app fails with a clear error if `DATABASE_URL` is
missing. See `.env.example` for the template.

## Requirements

- Python 3.13+

All commands below are run from this `backend/` directory.

## 1. Create the virtual environment

```bash
python3 -m venv .venv
```

## 2. Activate it (macOS / Linux)

```bash
source .venv/bin/activate
```

## 3. Install backend + development dependencies

```bash
pip install --upgrade pip
pip install -e ".[dev]"
```

## 4. Run the tests

There are two suites. The offline one is the default and the one to run
constantly; the PostgreSQL one is opt-in and needs a dedicated database.

### Offline suite (no database, no network)

```bash
pytest -m "not integration"
```

These tests use a synthetic `DATABASE_URL` (see `tests/conftest.py`); they
need no `.env`, no PostgreSQL, and no internet. Plain `pytest` also works: the
integration tests skip themselves when the variables below are absent.

### PostgreSQL integration suite (opt-in)

```bash
pytest -m integration
```

The tests in `tests/integration/` execute against a **real** PostgreSQL
database and prove what the offline suite deliberately cannot: that the
migrations apply, that composite foreign keys and the `NULLS NOT DISTINCT`
unique index are enforced, that a rollback discards a domain row and its audit
row together, that `Session.delete()` really deletes, and that the
authoritative-version and staleness queries return the right rows.

They require **both** of these, supplied either in the environment or in a
git-ignored repository-root `.env.test`:

```
TEST_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/DBNAME?sslmode=require
CHURCH_SCHEDULING_RUN_PG_INTEGRATION=1
```

**Never point `TEST_DATABASE_URL` at the development or production database.**
Use a dedicated Neon branch created for testing and nothing else. The harness
refuses to run if the URL resolves to the same host and database as the
development `DATABASE_URL`, and there is deliberately **no fallback**: without
`TEST_DATABASE_URL` the suite skips rather than borrowing `DATABASE_URL`.

`.env.test` is git-ignored and must stay that way. Never commit it, paste its
contents into a report, or copy a real URL into `.env.example`.

What the harness does and does not do:

- it runs `alembic upgrade head` against `TEST_DATABASE_URL` once per session;
- it never downgrades, drops or truncates anything;
- every test runs inside a transaction that is rolled back, so no test leaves
  committed rows behind and pre-existing rows are never deleted.

## 4b. Development-only actor header

There is no authentication yet. To exercise `GET /api/v1/me` (and, later, other
API endpoints) while developing locally, start the server with:

```bash
CHURCH_SCHEDULING_DEV_AUTH=1 uvicorn app.main:app --reload
```

and send the id of the Person to act as:

```bash
curl -H 'X-Dev-Actor-Person-Id: 42' http://127.0.0.1:8000/api/v1/me
```

Only the exact value `1` enables it; anything else — including `true`, `yes` and
`0` — leaves it off, which is the default. With it off the header is ignored and
every request is unauthenticated.

> **Never enable this in a deployed environment.** It is not authentication:
> anyone who can reach the service can act as any Person, including an Admin, by
> naming their id. It exists only so the API and UI can be built before Google
> sign-in is implemented.

### The first-pass flow, end to end

Four calls, in order: find a period, start its schedule, fill it, look at it.
The ministry's roles, the period's staffing requirements, role qualifications,
availability and serving limits all have their own endpoints — see "The
endpoints, in full," below — and availability is treated as fixed once the
period is locked, which
`POST /scheduling-periods/{id}/availability-lock` does. What still has **no**
endpoint at this stage is creating the ministry, the people, the memberships
and the scheduling period itself; those come from `scripts/seed_demo.py`,
`scripts/import_local_ministry.py`, or a developer session today.

**1. Which periods can I schedule?**

```bash
curl -H 'X-Dev-Actor-Person-Id: 42' \
  http://127.0.0.1:8000/api/v1/ministries/5/scheduling-periods
```

Each period reports whether availability is locked (`availability_locked_at`)
and whether scheduling has started (`schedule`, or `null`). When it has, the
`schedule` object names the one version to open — the newest one — so a client
never has to reason about version history.

**2. Start the first schedule for one of them.**

```bash
curl -X POST \
  -H 'X-Dev-Actor-Person-Id: 42' \
  -H 'Content-Type: application/json' \
  -d '{"notes": "Initial Q4 schedule"}' \
  http://127.0.0.1:8000/api/v1/scheduling-periods/12/schedule-versions
```

`201` with the new `schedule_version_id` to use for the next two calls, and
`requirement_snapshot_count` — how many required positions it froze in place.
The body is optional; `notes` is the only field it accepts. This creates an
**empty** first schedule and nothing else: it does not fill it, submit it, or
finalize it. Availability must be locked first (`409` if not), and a period
that already has a schedule is `409` too — this endpoint starts a first
schedule only.

**3. Generate it**, and **4. review it** — the two sections below.

### Generating a draft schedule

Fills the schedule started above, using the same server:

```bash
curl -X POST \
  -H 'X-Dev-Actor-Person-Id: 42' \
  -H 'Content-Type: application/json' \
  -d '{"allow_no_response": false, "target_assignments_per_candidate": 2}' \
  http://127.0.0.1:8000/api/v1/schedule-versions/7/generate
```

The body is optional — `{}` or no body at all runs with the default policy
(fill as much as possible, no soft preferences). The response reports what was
created, what could not be filled and why, and how evenly the work landed.

Three things worth knowing:

- **200 does not mean "complete".** A run that could not fill every position
  still succeeds and returns the assignments it did make, with the rest listed
  under `unfilled_requirements`. Check `is_complete`.
- **The whole request is one transaction.** If any placement is refused, the
  request fails and *nothing* from that run is written. Nothing is left
  half-generated.
- **Not every preference is yours to set.** The body configures three of them —
  `target_assignments_per_candidate`, `balance_candidate_loads` and
  `role_variety_role_ids`, plus `allow_no_response`. Preferring an ordinarily
  `AVAILABLE` candidate over one who answered `BACKUP` is **always on** and has
  no flag: it is what the tier means, not a ministry's choice. It still cannot
  cost a filled position.

The policy fields are supplied per request as a temporary measure; a scheduling
policy really belongs to a Ministry, and configuring and storing one is later
work.

### Reviewing a schedule version

The last step of the flow above, and read-only. Returns the version, its
period, every snapshot requirement and assignment, staffing totals, and both
diagnostics:

```bash
curl -H 'X-Dev-Actor-Person-Id: 42' \
  http://127.0.0.1:8000/api/v1/schedule-versions/7
```

Readable by an Admin, or by an active Head of that version's ministry —
anyone else gets 403. Two things the response deliberately separates:

- **`staleness`** says whether current staffing configuration still matches the
  version's immutable snapshot. The requirement rows always report the
  snapshot's own dates and counts; when the two disagree, the drift is listed
  rather than the history rewritten.
- **`finalization_readiness.is_ready`** says nothing about this version's
  *contents* would block finalization. It is **not** permission to finalize:
  that also requires the version to be in REVIEW and to be the latest one, and
  those are rules of the transition endpoints, not of this read. A stale or
  unready version is still returned with 200 and its diagnostics attached.

### The endpoints, in full

Every one is under `/api/v1`, takes the acting Person from the development
header above, and is guarded by the shared "active Admin, or active Head of
this specific ministry" rule — except `GET /me`, which only needs an actor.

**Identity**

| Method and path | Purpose |
| --- | --- |
| `GET /me` | Who the caller is, and which ministries they head |

**Ministry configuration** (Tasks 53–57)

| Method and path | Purpose |
| --- | --- |
| `GET /ministries/{id}/roles` | A ministry's roles; `?include_inactive` to see deactivated ones |
| `POST /ministries/{id}/roles` | Create a role |
| `PATCH /ministry-roles/{id}` | Rename or re-describe a role |
| `POST /ministry-roles/{id}/deactivate` | Deactivate a role |
| `POST /ministry-roles/{id}/reactivate` | Reactivate one |
| `GET /ministry-roles/{id}/qualifications` | Every membership of the role's ministry, with its decision for this role |
| `PUT /ministry-roles/{id}/qualifications/{membership_id}` | Approve or decline one |
| `GET /scheduling-periods/{id}/events` | The period's events in date order, cancelled ones included and marked — the index the staffing and availability screens page through |
| `GET /events/{id}/staffing-requirements` | How many people each active role needs at this event |
| `PUT /events/{id}/staffing-requirements/{role_id}` | Set a `required_count` |
| `DELETE /events/{id}/staffing-requirements/{role_id}` | Clear one |
| `GET /events/{id}/availability` | Each member's answer for this event, plus the period's lock state |
| `PUT /events/{id}/availability/{membership_id}` | Record `AVAILABLE`, `BACKUP`, or `UNAVAILABLE` |
| `DELETE /events/{id}/availability/{membership_id}` | Clear back to no response |
| `POST /scheduling-periods/{id}/availability-lock` | Close availability collection for the period — the state starting a schedule requires |
| `GET /scheduling-periods/{id}/serving-limits` | Each member's hard maximum for this period, or none |
| `PUT /scheduling-periods/{id}/serving-limits/{membership_id}` | Set one |
| `DELETE /scheduling-periods/{id}/serving-limits/{membership_id}` | Clear one |
| `GET /scheduling-periods/{id}/scheduling-rules` | The rules this period sets for itself — today, `min_intervening_events`, or `null` when none is set |
| `PUT /scheduling-periods/{id}/scheduling-rules/min-intervening-events` | Set how many of the ministry's own events must pass before the same person serves again |
| `DELETE /scheduling-periods/{id}/scheduling-rules/min-intervening-events` | Clear the rule back to "no rule" |

Behaviours worth knowing before calling these:

- **Availability writes are refused once the period is locked** (409). The
  `GET` reports `availability_locked_at` so a client can say so before trying.
  That rule lives in exactly one place, the availability service.
- **Locking is idempotent and one-way.** Locking an already-locked period is
  `200` with the original instant, not a `409` — a repeated click is not an
  error about a state that is already what the caller asked for. Nothing
  unlocks it: reopening availability after a schedule has been built from it
  is a lifecycle question nobody has specified, so there is no endpoint and no
  service for it. Locking checks no completeness of any kind; "no response" is
  a valid permanent input state.
- **A qualification decision is never cleared**, only changed between qualified
  and not qualified. "No row" means never assessed, and that is a different
  fact the system does not let you go back to.
- **`include_inactive=true`** widens the roles, availability and serving-limit
  listings to deactivated roles or memberships. The default is the active set,
  which is what a head managing a live ministry wants to see.
- **The event-gap rule is counted in events, never in days**, and it holds at
  both ends of a period: the events just before and just after the window being
  scheduled are read too, so a quarter already published on either side
  constrains this one. Every event the ministry holds counts, including one-off
  ones, so two events with nothing between them are consecutive whether they
  are a week or a day apart. `null`
  means "no rule"; `0` is not a value the column or the API accepts, because
  "consecutive is allowed" is exactly what the absence of the rule already
  says. It is hard and non-overridable — see `app/services/event_gap.py`.
- **The `DELETE`s take an optional `?reason=`**, recorded on the audit row, and
  answer `204` whether or not there was anything to remove. Clearing something
  already absent is a no-op, not a `404`.

**Scheduling**

| Method and path | Purpose |
| --- | --- |
| `GET /ministries/{id}/scheduling-periods` | Periods, lock state, and the latest version of each |
| `POST /scheduling-periods/{id}/schedule-versions` | Start the period's first schedule |
| `POST /schedule-versions/{id}/generate` | Generate |
| `GET /schedule-versions/{id}` | Review, with staleness and finalization-readiness diagnostics |

**Not exposed over HTTP**, though implemented as services: creating a
scheduling period and its events, locking availability, manual assignment /
removal / bounded override, DRAFT → REVIEW, REVIEW → FINALIZED, successor
versions, carry-forward, linked-pair same-date exclusions, and anything that
creates a Person, a Ministry or a membership.

### How generation performs, and why it is shaped this way

The CP-SAT solve is not the expensive part; **round trips to a hosted database
are**. Two paths were restructured so their query counts stop scaling with the
number of assignments:

- **Church-wide Sunday conflicts** are fetched for every (person, date) pair a
  run needs in **two set-based queries** (`get_sunday_conflicts_for`), instead
  of two per pair. Both that form and the one-person form are built from the
  same statement builders, so there is one definition of what counts as a
  conflict.
- **Persisting a generated schedule** reads its facts in **one prefetch** and
  writes the rows together, instead of eleven queries per row. The rules
  themselves were extracted into `app/services/assignment_rules.py`, which
  reads nothing, so manual assignment and batch generation apply *the same*
  rules from the same definition — the only difference is how the facts were
  gathered. No rule was relaxed for generation.

Facts a run can change itself — how full each requirement is, how much each
member is carrying, which events they are in, who is on each date — are seeded
from the database and advanced in memory as rows are accepted, so the last
placement is judged against all the ones before it. The version's writability is
re-read one round trip before the insert, which narrows but does not eliminate
the race with a concurrent finalize.

Any specific timing figure in this repository's history is a **development
measurement against a test environment, not a product guarantee**. The durable
claim is the shape: those two paths no longer cost queries per assignment.

## 4c. Demo data for the local UI walkthrough

`scripts/seed_demo.py` fills the existing Neon **`development`** branch with
fictional people and events so the frontend can be used end to end. It is
development tooling, not a production seed, and it is not part of the
installed package.

**There is no separate demo branch.** This project has exactly three Neon
branches — production, development, integration-test — and the development
branch is currently empty; it is the intended home for this data. The seed
does not treat "development" as automatically safe, though: it still refuses
unless `DATABASE_URL`'s host **exactly matches** a host you name explicitly, and
it still refuses if that host is the integration-test branch. See
`scripts/demo_guards.py` for exactly what is and is not proved by that — in
particular, nothing here can recognize a production branch by its hostname;
there is no production URL available locally to compare against, and none
should be. The boundary is the explicit opt-in and the explicit host match, not
shape-matching a hostname.

### Set it up, once

1. Copy `.env.demo.example` (repository root) to `.env.demo` and fill it in
   with the **development** branch's own `DATABASE_URL` and host.
   `.env.demo` is git-ignored; never commit it. It is not a fourth database —
   it is a local, explicit way to load the development connection *and* opt
   into demo seeding at the same time, so seeding is never triggered by
   accident from an ordinary `.env`.
2. Apply the schema to that branch, if you have not already:

   ```bash
   set -a && . ../.env.demo && set +a
   alembic upgrade head
   ```

### Seed it

```bash
set -a && . ../.env.demo && set +a     # from backend/
python scripts/seed_demo.py
```

It prints the **demo actor Person ID** you will need for the frontend, plus the
ministry and period it created. It prints no URL, host, user or password.

### What it refuses to do

The seed writes nothing unless all of these hold, and says which one failed
without naming any host:

- `CHURCH_SCHEDULING_ALLOW_DEMO_SEED` is exactly `1`;
- `APP_ENV` is `development`;
- `DATABASE_URL` is set, explicitly, in the process environment — never read
  from `.env` or any other file as a fallback, so seeding is always deliberate;
- `CHURCH_SCHEDULING_DEMO_DATABASE_HOST` is set, explicitly, to the
  development branch's own host;
- `DATABASE_URL`'s host **matches** that configured host;
- that host is **not** the `TEST_DATABASE_URL` host, when this machine knows it.

Matching the development branch is the **success** case here, not something to
refuse — there is nothing else it could legitimately match.

### There is no reset command, deliberately

Nothing here drops, truncates, deletes or downgrades. Running the seed twice
creates nothing the second time and reports the same ids. **To start over,
recreate the Neon development branch** — simpler and far safer than teaching a
script to remove church data. If the demo data is found partially present, the
seed fails and says so rather than guessing at a repair.

### What it creates

A fictional church: `Demo Church`, ministry `Setup Demo`, roles Setup Lead and
Setup 2–5, period `October 2026 Demo` covering the four Sundays of October
2026, one person per role per Sunday (20 positions), and seven volunteers with
`@demo.invalid` addresses. Two of them are approved for Setup Lead; everyone is
approved for Setup 2–5. Every volunteer has answered for every Sunday, and
availability is **locked**.

**No schedule is created.** Starting it is the button being demonstrated.

Production remains completely untouched by this workflow, and the
integration-test branch remains test-only; the development branch is where
this fictional local/demo data lives from now on.

## 4d. Importing a real roster into a local database

`scripts/import_local_ministry.py` is the same idea as the demo seed, pointed
at a real ministry instead of a fictional one. A ministry's own roster files —
availability grid, final schedule — are read out of a directory **git proves
is ignored**, and become a ministry, its people, roles, period, events,
staffing requirements, qualifications and availability answers in the local
development database. It exists because the product has no endpoint that
creates any of those, so there is otherwise no way to put a real roster in
front of the UI.

```bash
set -a && . ../.env.demo && set +a          # from backend/
python scripts/import_local_ministry.py \
    --input-dir ../local_data/<your-ministry>/source \
    --staffing-file ../local_data/<your-ministry>/staffing.csv \
    --church-name "..." \
    --ministry-name "..." \
    --period-name "..." \
    --head-name "..." \
    --year-hint 2026
```

It prints the **Ministry Head Person ID** to put in
`frontend/.env.local`, plus counts. It prints no name, no email and no cell
value, on success or on any failure path.

Three properties are the whole design:

- **Nothing church-specific is in the tracked code.** There is no default
  church, ministry, period, head or role list; every one is a required
  argument. A test asserts this of both files, because it is the property that
  makes the tooling committable while the records are not.
- **The input directory must be git-ignored**, checked with `git check-ignore`
  before a single file is opened — the same rule the historical validation
  runners follow.
- **It never deletes or overwrites.** Importing into a ministry name that
  already exists is refused outright. To rehearse again, import under a
  different ministry name, or (for a genuinely clean slate) recreate the Neon
  development branch.

### Staffing is stated, never inferred

`--staffing-file` is the one flag to think about rather than copy. **How many
people an event needs is the Ministry Head's rule, and no roster file states
it.** A source grid says how many role columns somebody drew — usually the
full team on every date — and a filled schedule says who actually served.
Reading a requirement off either is inference: a column nobody filled becomes
a position the ministry never asked for, and a week somebody was away becomes
a smaller team than the head wants.

So the rule lives in a git-ignored CSV of three generic columns:

```csv
event_date,role,required_count
2031-03-02,<role label>,1
2031-03-02,<another role label>,1
```

- **A listed date takes exactly those roles.** A date not listed keeps the
  source's shape. Merging was rejected deliberately: a smaller team has to be
  expressible, and it is expressed by leaving roles out.
- **`required_count` must be at least 1.** "This event does not need this
  role" is what an omitted row already says, and one fact with two spellings
  is a bug waiting to be written — the same reasoning as the event-gap rule
  rejecting `0`.
- Dates may be written `2031-03-02`, `3/2/31` or `Mar 2 2031`; role labels
  match ignoring case and spacing.
- A date the source does not list, a role the ministry does not have, a
  non-positive or non-numeric count, or the same (date, role) twice all stop
  the import before it writes anything. The file must be git-ignored too — it
  names the ministry's own events.

The run reports `Staffing from the head's own rule on N event(s)`, so a
manifest that silently matched nothing is visible rather than assumed.

### Two more flags deserve reading before use

- `--lead-from-schedule` takes lead qualification from who *appears* in the
  source schedule's lead column. That is evidence, never a Ministry Head's
  authoritative list, so it is opt-in and reported back as a caveat.
  `--also-lead NAME` (repeatable) applies a current correction to an older
  source without editing either the source or this code.
- `--lock-availability` closes availability at the end of the import. Off by
  default, because locking is one-way and leaving it open is what lets the
  availability screen be demonstrated as an editable screen; the UI can close
  it later.

It refuses rather than guesses: a non-Sunday date with no note to name it, an
availability answer for somebody the roster does not list, a head name that
matches zero or two people, or any of the staffing-manifest problems above.
Those stop the import with nothing written.

## 5. Check database connectivity (explicit, hits Neon)

Runs a read-only `SELECT 1` against the configured Neon development database:

```bash
python -m app.db_check
```

## 6. Alembic migrations (for later schema work)

Alembic is configured in `alembic.ini` / `alembic/env.py` and takes the
database URL from the application configuration (no credential in `alembic.ini`).
`target_metadata` is the app's declarative `Base.metadata`, populated by the
explicit `import app.models` in `alembic/env.py` — autogenerate sees a model
only if its module is reachable from `app/models/__init__.py`.

Migrations so far, in order:

| Revision | Schema |
| --- | --- |
| `d90e274c4dfa` | Core identity and ministry (seven tables) |
| `0c48b083c39e` | Scheduling input (five tables) |
| `e0bfb11f05b4` | Schedule output and versioning (four tables, plus an additive parent key on `event`) |
| `a949a16195e3` | Audit event (one table) |
| `b7f2c41d83ae` | `membership_serving_limit` — the per-person, per-period hard maximum, plus a widening of `audit_event`'s `target_table` CHECK |
| `c31d8a4f7b62` | `membership_same_date_exclusion` — the linked-pair rule, plus the same CHECK widened again |
| `615c88b0a6f3` | Widens `availability.availability_state` from two stored values to three, adding `BACKUP` |

Run migrations against the Neon **development** branch only:

**Production migrates itself, and that constrains what you may write.** Since
the Task 80 deployment follow-up, Cloud Build runs `alembic upgrade head`
against production from the newly built API image, *before* that image is given
traffic (`cloudbuild.yaml`, step `migrate-database`). Two things follow for
anybody adding a revision:

- **You do not apply it to production by hand.** Committing and pushing it is
  applying it. There is no second step to forget — which is the point: Task 79
  shipped a migration nobody ran, and the first sign-in afterwards returned a
  500.
- **It must be readable by the code already running**, because the previous
  revision keeps serving while it runs. Adding a nullable column, a table, an
  index or a non-violated constraint is safe. Dropping or renaming anything,
  adding a `NOT NULL` column with no default, or narrowing a type is **not**,
  and needs the two-deployment expand/contract sequence in
  `docs/deployment/cloud-run.md` section 6 — which also explains why a rollback
  depends on the same rule.

```bash
alembic revision --autogenerate -m "message"   # generate a migration
alembic upgrade head                            # apply migrations
alembic downgrade -1                            # revert the last migration
alembic history                                 # list migrations
alembic current                                 # show the applied revision
alembic check                                   # models vs database: any drift?
```

Always review an autogenerated migration before applying it. Autogenerate does
not reliably render every construct — in particular, a partial index whose
`postgresql_where` is a bare ORM attribute is emitted as an unparseable object
reference, so index predicates are written as `text(...)` in the models.

Autogenerate also gets *ordering* wrong when a revision both alters an existing
table and creates tables depending on that alteration: it emits every
`create_table` first and the standalone `ALTER` last. `e0bfb11f05b4` adds
`UNIQUE (id, scheduling_period_id)` to `event` and then creates
`schedule_version_requirement`, whose foreign key references it, so that
constraint is created first and dropped last by hand. Check operation order, not
just the operations themselves.

## 7. Start the local FastAPI server

```bash
uvicorn app.main:app --reload
```

## 8. Access the health endpoint

With the server running:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok"}
```

## 9. Stop the server

Press `Ctrl+C` in the terminal running Uvicorn. Run `deactivate` to leave the
virtual environment.

## Domain services and the transaction convention

Write operations live in `app/services/` as plain functions that take a
SQLAlchemy `Session` explicitly. There is no repository layer, no base service
class and no unit-of-work abstraction.

**A service never begins, commits or rolls back. The caller owns the
transaction boundary:**

```python
with SessionLocal() as session, session.begin():
    grant_ministry_head(session, actor=admin, membership=membership)
    # exactly one commit, here, on the way out
```

This is what keeps a domain change and the audit row explaining it atomic — they
are pending in one session, so one commit writes both and one rollback discards
both. It also means services work unchanged inside a request-scoped session
later. Two obligations on callers: commit once after the call, and roll back if
it raises (`session.begin()` as a context manager does the second for you).

A service **may** call `session.flush()` — never `commit()` or `rollback()` —
when it genuinely needs an identity a later statement in the same operation
depends on. `set_role_qualification` is the first example: creating a new
`RoleQualification` needs its id before the audit row referencing it can be
built, so it flushes once, right after adding the row, and only on that path —
never on an update, and never on the idempotent no-op path. A flush sends
pending SQL within the caller's still-open transaction; it commits nothing, and
a rollback after it discards the flushed row exactly as it would an unflushed
one.

Audit rows are written explicitly by the service that knows the actor, the
action and the reason — never by an ORM event listener, which could know none of
them and would fire during migrations and fixtures.

Three services now share the same "Ministry manager" authorization rule --
active Admin, or an active Ministry Head of the specific ministry involved --
extracted into `app/services/authorization.require_ministry_manager` once a
third genuine use appeared. Ministry Head *authority itself*
(`grant_ministry_head` / `revoke_ministry_head`) is a stricter, Admin-only
rule — a head must never be able to promote another head, even in their own
ministry — so it uses the sibling `require_active_admin` instead, with no
Ministry Head fallback at all. The same Admin-only helper covers a
church-wide `ExistingCommitment` that has no source ministry to derive
management from. Both live in `app/services/authorization.py`: two small
functions, not a framework — no policy objects, no decorators, no permission
registry.

Several operations compose naturally under one transaction boundary, since none
of them commits on its own:

```python
with SessionLocal() as session, session.begin():
    period = create_scheduling_period(
        session, actor=admin, ministry=setup, name="Setup Q4 2026",
        start_date=date(2026, 10, 4), end_date=date(2026, 12, 27),
    )
    events = generate_sunday_events(session, actor=admin, period=period)
    # one commit, once every row and its audit event is pending
```

`create_initial_schedule_version` is the first operation to need **more than
one** identity-producing flush in a single call — up to two, one for a newly
created `Schedule` and one for the new `ScheduleVersion` — because the
requirement-snapshot rows that follow are built from the version's `id`. The
~65 snapshot rows themselves are never flushed individually; they are added to
the session and left for the caller's own commit, exactly like every other
batch in this project (`generate_sunday_events`'s Events above):

```python
with SessionLocal() as session, session.begin():
    lock_availability(session, actor=admin, period=period)  # Task 19
    version = create_initial_schedule_version(session, actor=admin, period=period)
    # Schedule (if new) and the Version each got their own flush along the
    # way; the requirement snapshot did not. One commit closes all of it.
```

`assign_member` introduces this project's one **bounded override**: a
non-blank `override_reason` may bypass exactly five judgment-call checks — a
deactivated role, missing/declined qualification, explicit `UNAVAILABLE`, a
church-wide Sunday conflict (reusing Task 21's query), and a fully staffed
requirement. It can never bypass version immutability, a Ministry mismatch,
deactivated Membership/Person, a cancelled Event, or the one-position-per-event
rule — those are structural facts about what the row would mean, not warnings
a head can judgment-call past. Supplying `override_reason` when nothing
actually needed overriding is itself rejected, so `is_override=true` on a row
always corresponds to a real bypassed check.

**Two further hard rules are outside that catalogue on purpose**: a person's
per-period serving maximum, and a linked pair's same-date exclusion. Neither is
one of the five, so no `override_reason` reaches either and no historical
`overridden_blockers` payload excuses one at finalization. Each records
something people agreed between themselves rather than a fact about the world a
head can judge, so the remedy is to change the recorded constraint — which is
audited — and then assign.

### Two Assignment writers, one copy of the rules

`assign_member` is no longer the only thing that creates an `Assignment`.
`app/services/generated_assignment.py` persists a whole solver result at once,
because per-row writing cost eleven queries a row. The rules both writers apply
live in `app/services/assignment_rules.py`, which **reads nothing** — every
rule is a function of facts a `PairFactSource` hands over — so the only
difference between the two paths is how those facts were gathered:
per-row reads for a head's single considered change, one prefetch for a
generation run. A test pins that module's database-independence, which is what
stops the two writers drifting apart. Only `assign_member` can carry an
`override_reason`; generation's is always `None`.
