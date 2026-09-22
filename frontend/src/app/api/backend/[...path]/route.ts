/**
 * The development boundary between this app and FastAPI.
 *
 * The browser calls `/api/backend/<backend path>` on this app's own origin;
 * this handler forwards it to the backend and returns the answer unchanged.
 *
 * **Why a route handler rather than a `next.config` rewrite.** A rewrite
 * would forward the request just as well, but it cannot make the one decision
 * this file exists to make: whether to attach the development actor header,
 * which depends on `NODE_ENV` and two environment variables and must fail
 * closed. A rewrite's `headers` are static configuration -- it would either
 * always send the header or never send it, and "always" is exactly the
 * behaviour that must not survive into a production build. Keeping it here
 * also means the guard is ordinary code with ordinary tests.
 *
 * **What it deliberately is not.** It is not a general reverse proxy. Only
 * GET, POST, PATCH, PUT and DELETE exist, because those are the only methods
 * the backend contract this app calls uses (Task 53 added PATCH, for renaming
 * a ministry role; Task 54 added PUT and DELETE, for setting and clearing a
 * staffing requirement); anything else gets Next.js's own 405. It rewrites
 * nothing, retries nothing, and interprets nothing: a 401, 403, 404, 409 or
 * 422 from the backend arrives at the browser as that status with its body
 * intact, and a 204 arrives with none, because the whole point of the
 * client's error handling is to react to what the domain actually said.
 *
 * **Task 76: it now also carries the sign-in session, and that is what makes
 * the whole authentication topology work.** Three headers pass through, and
 * each is here for one reason:
 *
 * - `Cookie`, browser -> backend, so FastAPI can read the session it signed.
 * - `Set-Cookie`, backend -> browser, so the session FastAPI establishes lands
 *   on *this* app's origin. Copied with `getSetCookie()` rather than `get()`,
 *   because a single `Set-Cookie` string is what you get from `get()` when
 *   several were sent, and the flow sends two (the session, and Authlib's
 *   transient OAuth state).
 * - `Location`, backend -> browser, so the 302 to Google's consent screen and
 *   the 303 back into the app actually go somewhere. `redirect: "manual"`
 *   above is what keeps them intact for the browser to follow, rather than
 *   having `fetch` chase them server-side -- which would take the server, not
 *   the person, to Google.
 *
 * Because the browser only ever sees this app's origin, the session cookie is
 * an ordinary first-party `SameSite=Lax` cookie. There is no cross-site cookie
 * anywhere in the flow, which is the fragile arrangement this design exists to
 * avoid.
 */

import { NextResponse, type NextRequest } from "next/server";

import { resolveApiUrl, resolveDevActorHeaders } from "@/lib/devAuth";

/** Never cached: every one of these reads live domain state. */
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest, context: RouteContext<"/api/backend/[...path]">) {
  return forward(request, await context.params);
}

export async function POST(request: NextRequest, context: RouteContext<"/api/backend/[...path]">) {
  return forward(request, await context.params);
}

export async function PATCH(request: NextRequest, context: RouteContext<"/api/backend/[...path]">) {
  return forward(request, await context.params);
}

export async function PUT(request: NextRequest, context: RouteContext<"/api/backend/[...path]">) {
  return forward(request, await context.params);
}

export async function DELETE(request: NextRequest, context: RouteContext<"/api/backend/[...path]">) {
  return forward(request, await context.params);
}

async function forward(
  request: NextRequest,
  params: { path: string[] },
): Promise<Response> {
  const target = buildTargetUrl(request, params.path);

  const headers = new Headers({ Accept: "application/json" });
  const contentType = request.headers.get("content-type");
  if (contentType) headers.set("content-type", contentType);

  // The session cookie, and during sign-in the transient OAuth state cookie.
  // Forwarded verbatim: this handler does not parse, validate or trust it --
  // the backend signed it and the backend is what verifies it.
  const cookie = request.headers.get("cookie");
  if (cookie) headers.set("cookie", cookie);

  // Set last, so that if a dev-actor header were ever somehow present in the
  // incoming request it is the server's value that is sent, not the caller's.
  for (const [name, value] of Object.entries(resolveDevActorHeaders(process.env))) {
    headers.set(name, value);
  }

  // Read the body as text rather than streaming it: these payloads are a
  // handful of fields, and a duplex stream would need Node-specific options
  // for no benefit.
  const body = request.method === "GET" ? undefined : await request.text();

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: body === "" ? undefined : body,
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    // The backend is not running, or the URL is wrong. Answered in the shape
    // the client already understands, so it reports "could not reach the
    // server" rather than parsing an HTML error page.
    return NextResponse.json(
      { detail: "Could not reach the scheduling API." },
      { status: 502 },
    );
  }

  // Status and body pass through untouched. Content-Type is carried over so a
  // JSON error body is still parsed as one.
  const payload = await upstream.text();
  const responseHeaders = new Headers();
  const upstreamType = upstream.headers.get("content-type");
  if (upstreamType) responseHeaders.set("content-type", upstreamType);

  // Every Set-Cookie, not just the first: sign-in sets the session and clears
  // the OAuth state in one response.
  for (const setCookie of upstream.headers.getSetCookie()) {
    responseHeaders.append("set-cookie", setCookie);
  }

  // Redirects are the browser's to follow, not this handler's.
  const location = upstream.headers.get("location");
  if (location) responseHeaders.set("location", location);

  return new Response(payload === "" ? null : payload, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

/**
 * Join the captured path segments onto the backend base, preserving the query
 * string. Segments are re-encoded individually so a value containing `/` or
 * `?` cannot escape the path it belongs in.
 */
function buildTargetUrl(request: NextRequest, path: string[]): string {
  const suffix = path.map(encodeURIComponent).join("/");
  const query = request.nextUrl.search;
  return `${resolveApiUrl(process.env)}/${suffix}${query}`;
}
