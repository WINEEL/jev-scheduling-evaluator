# Frontend

The Ministry Head's view: configure a ministry, then schedule it. Next.js
(App Router) and TypeScript, talking to the FastAPI backend in `../backend`.

It now covers both the scheduling flow and the configuration that flow reads
from. It still has **no sign-in**, nothing volunteer-facing, no review/finalize
actions, and no admin screens — and it cannot create a ministry, a person, a
membership or a scheduling period, because no endpoint does.

## What it does

| Screen | Path | Shows |
| --- | --- | --- |
| Home | `/` | The ministries you lead, each linking to its periods and its roles. Where a ministry's configuration has been validated with its own ministry head (configuration, see below), that is shown here and nowhere else |
| Roles | `/ministries/{id}/roles` | A ministry's roles: create, rename, deactivate, reactivate. Deactivated ones are hidden behind one checkbox. `display_order` is shown, never edited |
| Qualifications | `/ministries/{id}/roles/{roleId}/qualifications` | Every membership of the ministry, with its qualified / not-qualified decision for this role |
| Scheduling periods | `/ministries/{id}/periods` | Each period, whether availability is ready, whether scheduling has started — its five configuration screens as a numbered workflow, then the one action that starts or opens the schedule, and the action that closes availability collection when a period is not ready yet |
| Staffing | `/ministries/{id}/periods/{periodId}/staffing` | One matrix: every event down the side, every active role across the top, how many people each cell needs. A row total per event, a column total per role, and the period's whole staffing figure in the corner |
| Availability | `/ministries/{id}/periods/{periodId}/availability` | One matrix: every member down the side, every event across the top, each answer in one control — Available, "If need be", Unavailable, or no response. Says in words whether collection is still open or finished |
| Serving limits | `/ministries/{id}/periods/{periodId}/serving-limits` | Each member's maximum assignments for this period. "No limit" is its own state and never means zero |
| Scheduling rules | `/ministries/{id}/periods/{periodId}/scheduling-rules` | **Rules in force**: all five rule families, what each is set to for this period, and where each one is set — including the two this screen cannot list, marked as such rather than shown as "none". Then the event-gap rule (editable) and the member group limits and same-event support requirements (read-only) |
| Schedule | `/schedule-versions/{id}` | In this order: a short summary, the generate controls, the **draft schedule matrix**, assignments by person, the schedule checks, and what happens next |

Two things the configuration screens are careful about, because both are easy
to get wrong in a UI:

- **"Backup" is a generic lower-priority availability tier**, not any one
  ministry's word for standby. It means *"I can serve, but schedule an
  ordinarily available person first"* — the page says so rather than assuming
  it is self-explanatory.
- **A serving limit is scoped to one ministry and one period.** That is stated
  on the page from a single shared string, so the number is never ambiguous,
  and the absence of a limit renders as "No limit" rather than as a number.
- **The event gap is counted in events, not in days.** The scheduling-rules
  page says so in its own shared string, and names no ministry and no weekday:
  every event this ministry holds counts, so two events with nothing between
  them are in a row however far apart the dates are. An empty box is "No rule",
  never a zero.
- **Closing availability is confirmed before it happens.** The domain has no
  unlock, so one misplaced click would leave a period that can never accept
  another answer. The first click explains exactly what is about to happen —
  answers kept, non-answers kept as non-answers, no further changes, no undo —
  and the second does it.
- **The schedule screen says what a draft is.** A draft has been sent to
  nobody and no volunteer can see it; the screen says so rather than leaving a
  "Draft" badge to carry the point on its own, because "the ministry head
  reviews this before anything is published" is the part a reader is most
  likely to assume the opposite of.
- **The schedule comes before the analysis of it.** A ministry head opens the
  review screen asking "what is the schedule?", not "what is each person's
  statistical breakdown?", so the draft matrix is the page's one emphasized
  element and the per-person table sits below it. `structure.test.ts` asserts
  that order, so it cannot drift back.

### Look and feel

Light is the unconditional default; dark is reachable only through the toggle
in the header, and both are legible. The palette is a bright green
(`--accent-bright`, used for edges, rules and active states, never behind
text), a darker green (`--accent`, which carries every white-on-green label
and every link), charcoal text and white / light neutral ground. Colour is
never the only signal: every coloured badge, cell and figure carries the word
too.

**There is no logo asset in this repository.** The header is text branding, in
one constant apiece (`lib/brand.ts`); if a logo file is added later, the header
is the only place that has to learn about it.

## Running it locally

Two terminals. The backend must be running first.

**Terminal 1 — backend** (from `../backend`, with its virtualenv active):

```bash
CHURCH_SCHEDULING_DEV_AUTH=1 uvicorn app.main:app --reload
```

**Terminal 2 — frontend** (from this directory):

```bash
npm install
cp .env.example .env.local     # then edit .env.local
npm run dev
```

Open <http://localhost:3000>.

### Local environment variables

Set these in `.env.local`, which is git-ignored:

```
CHURCH_SCHEDULING_API_URL=http://127.0.0.1:8000
CHURCH_SCHEDULING_FRONTEND_DEV_AUTH=1
CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID=<YOUR_TEST_PERSON_ID>
CHURCH_SCHEDULING_FRONTEND_PILOT_VALIDATED_MINISTRIES=<comma-separated ministry names, or unset>
```

`<YOUR_TEST_PERSON_ID>` is the `person.id` of an **active** Person in your
development database — for these screens to show anything, one who heads a
ministry. Both sides must be configured: the backend's own
`CHURCH_SCHEDULING_DEV_AUTH=1` is required independently, and without it every
request is unauthenticated however this app is set up.

> **Development identity is temporary, and must never be used in a deployed
> environment.** It is not authentication. Anyone who can reach a server
> configured this way acts as whichever Person the environment names. It
> exists only so these screens can be built before Google sign-in is
> implemented, and it is refused outright when `NODE_ENV` is `production` —
> a production build sends no actor header at all, whatever the environment
> says.

There is deliberately **no fallback Person**. If the id is missing, blank, or
not a positive integer, no header is sent and the backend answers 401.

`CHURCH_SCHEDULING_FRONTEND_PILOT_VALIDATED_MINISTRIES` is **presentation
only** and is not required. It names the ministries whose scheduling
configuration has been checked against how that ministry actually schedules,
with its own ministry head; the home page then marks those, and marks the
others as examples awaiting the same review. The backend stores no such fact,
which is exactly why this is configuration rather than code. Leave it unset
and the home page says nothing either way — the honest default, since an
unearned claim in either direction is worse than silence. Matching ignores
case and surrounding space. It is read on the server, never through a
`NEXT_PUBLIC_` variable (this app defines none at all).

The database URL is never read here and must not be given to this app.

## Trying it with demo data

To use these screens end to end you need a database with a ministry, a period,
volunteers and locked availability. `backend/scripts/seed_demo.py` creates all
of that as fictional data on the existing Neon **`development`** branch — there
is no separate demo branch. See "Demo data for the local UI walkthrough" in
[`../backend/README.md`](../backend/README.md) for the setup and the safety
checks the seed enforces before it writes anything.

Once it has been seeded, it prints a **demo actor Person ID**. Then:

**Terminal 1 — backend**, pointed at the development branch:

```bash
cd backend
set -a && . ../.env.demo && set +a     # DATABASE_URL for the development branch
CHURCH_SCHEDULING_DEV_AUTH=1 uvicorn app.main:app --reload
```

**Terminal 2 — frontend**, with `.env.local` holding:

```
CHURCH_SCHEDULING_API_URL=http://127.0.0.1:8000
CHURCH_SCHEDULING_FRONTEND_DEV_AUTH=1
CHURCH_SCHEDULING_FRONTEND_DEV_ACTOR_PERSON_ID=<the demo actor id the seed printed>
```

```bash
cd frontend && npm run dev
```

### What to check, in order

1. Open <http://localhost:3000>. The name shown is the fictional demo head.
2. **Ministries you lead** lists `Setup Demo`. Open it.
3. `October 2026 Demo` is listed, showing **Availability ready**, with no
   schedule yet.
4. The second period, if present, shows **Availability not locked** and its
   Start button is disabled with the reason.
5. Click **Start schedule**. The draft page opens.
6. Staffing shows **20 positions needed**, 0 filled.
7. Optionally type `3` into "Aim for this many turns per person".
8. Click **Generate schedule**.
9. Each Sunday lists its five roles with a person against each.
10. Nobody appears on a Sunday they answered "unavailable" for. The demo data
    has six such answers across four Sundays.
11. **25 October is deliberately short-staffed** — only four of the seven
    volunteers are free — so one position shows as **Unfilled**, and "Schedule
    checks" lists it. That is the demo showing an unresolved position clearly,
    not a fault.
12. Reloading the page shows the same schedule: the assignments were saved.

The configuration screens work against the same demo data: **Manage roles** from
the home screen, **Manage qualifications** from a role, and **Manage staffing**,
**Manage availability**, **Manage serving limits** and **Manage scheduling
rules** from the periods screen.

One thing to expect rather than debug: the seed **locks** `October 2026 Demo`'s
availability, so the availability screen shows the existing answers and refuses
to change them. That refusal is the lock working. Serving limits, staffing,
roles and qualifications are all still editable on the same period, because the
lock governs availability only.

## How it talks to the backend

The browser calls this app's own origin at `/api/backend/...`; the route
handler in `src/app/api/backend/[...path]` forwards to FastAPI and returns the
answer unchanged. Two consequences, both intended:

- **No CORS configuration is needed**, and none was added to the backend. Every
  request the browser makes is same-origin.
- **The development actor header is attached on the server**, so no Person id
  reaches the browser, and a header set by a client is ignored.

A route handler is used rather than a `next.config` rewrite because the header
decision depends on `NODE_ENV` and must fail closed; a rewrite's headers are
static configuration and could not express that.

## Known limitations

- **Role variety is not offered.** The backend supports preferring a spread
  across roles, and `GET /ministries/{id}/roles` now exists to choose them
  from, but the generate form does not expose the setting: it always sends
  `role_variety_role_ids: null`. The backend capability is untouched, and the
  missing piece is now UI work rather than a missing endpoint.
- **Administrators see only ministries they personally head.** `GET /me`
  reports head memberships, not everything an Admin may manage, and no
  endpoint lists the latter. An Admin who heads nothing is told so rather than
  shown a guessed list.
- **No sign-in, and no review or finalize actions.** Both are later work.
- **Nothing creates a ministry, a person, a membership or a scheduling
  period**, and nothing locks a period's availability. Those endpoints do not
  exist; the demo seed described above is how a usable database gets populated
  today.
- **No screen for the linked-pair same-date exclusion.** The rule is enforced
  by the solver, by manual assignment and at finalization, but it has no HTTP
  endpoint, so there is nothing for a screen to call.
- **How many placements drew on Backup is not shown.** The solver counts it;
  the generation response does not carry the number, so this app cannot
  display it.
- **An unrecognized diagnostic renders as its raw code.** `diagnosticText` and
  `readinessLabel` translate a fixed vocabulary and fall back to the code
  itself, so a code the backend adds later shows up as something a person can
  search for rather than silently disappearing. Readiness issues also show the
  backend's own `message`; generation diagnostics have no such fallback.
- **Whether a ministry has been validated is configuration, not data.** The
  backend records no such fact, so the home page shows it only where the
  environment says so, and says nothing at all when it does not.
- **Nothing here is volunteer-facing.** A volunteer cannot see their own
  schedule, their own availability, or the constraints recorded about them.

## Commands

```bash
npm run dev        # development server
npm run build      # production build
npm run test       # Vitest
npm run lint       # ESLint
npm run typecheck  # tsc --noEmit
```

## Tests

Vitest, no browser. The API client, the development-auth guard, the proxy
handler and the pure display helpers are covered directly; the rules worth
protecting were written as plain functions so they could be.
