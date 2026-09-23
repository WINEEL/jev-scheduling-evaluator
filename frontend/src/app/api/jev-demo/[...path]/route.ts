/**
 * The transport boundary: browser -> this app's server -> the backend.
 *
 * Why a proxy at all, rather than calling the backend from the browser: the
 * backend's address stays server-side, every request the page makes is
 * same-origin, and so the backend needs no CORS policy and no opinion about
 * who may call it.
 *
 * It is defined by what it refuses to do:
 *
 * - **No cookies, in either direction.** No `Cookie` is forwarded and no
 *   `Set-Cookie` is copied back, so nothing here can carry, establish or
 *   extend a session.
 * - **No redirects.** `Location` is not copied and `redirect: "manual"` is not
 *   set, so this handler can never be the first step of a navigation
 *   somewhere else.
 * - **One request header**, `Accept`, and it is not a credential.
 * - **GET and POST only** — the two verbs the API has. Everything else gets
 *   Next.js's own 405.
 *
 * `JEV_DEMO_API_URL` names the backend, defaulting to the port the documented
 * command uses, so the demo runs for somebody who has configured nothing.
 */

import { NextResponse, type NextRequest } from "next/server";

/** Where the backend listens by default. Not a secret. */
const DEFAULT_DEMO_API_URL = "http://127.0.0.1:8000";

/** The backend's address. Read on the server; never sent to the browser. */
export const JEV_DEMO_API_URL_ENV = "JEV_DEMO_API_URL";

/** Never cached: an evaluation is a fresh judgment every time. */
export const dynamic = "force-dynamic";

export async function GET(
  request: NextRequest,
  context: RouteContext<"/api/jev-demo/[...path]">,
) {
  return forward(request, (await context.params).path);
}

export async function POST(
  request: NextRequest,
  context: RouteContext<"/api/jev-demo/[...path]">,
) {
  return forward(request, (await context.params).path);
}

async function forward(request: NextRequest, path: string[]): Promise<Response> {
  const target = buildTargetUrl(request, path);

  // Exactly one request header, and it is not a credential.
  const headers = new Headers({ Accept: "application/json" });

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      cache: "no-store",
    });
  } catch {
    // The backend is not running. The message names the one command that
    // fixes it, because that is the commonest failure on a fresh clone.
    return NextResponse.json(
      {
        detail:
          "Could not reach the evaluation backend. Start it with: cd backend && python -m uvicorn app.main:app --reload --port 8000",
      },
      { status: 502 },
    );
  }

  // Status and body pass through untouched; `Content-Type` is carried over so
  // a JSON error body is still parsed as one. Nothing else is copied — no
  // cookie, no redirect, no upstream header this page has no use for.
  const payload = await upstream.text();
  const responseHeaders = new Headers();
  const upstreamType = upstream.headers.get("content-type");
  if (upstreamType) responseHeaders.set("content-type", upstreamType);

  return new Response(payload === "" ? null : payload, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

/**
 * Join the captured segments onto the demo backend's base.
 *
 * Segments are re-encoded individually so a value containing `/` or `?`
 * cannot escape the path it belongs in. The query string is dropped rather
 * than forwarded: the demo API takes no query parameters, and forwarding one
 * would be forwarding something nobody reads.
 */
function buildTargetUrl(request: NextRequest, path: string[]): string {
  const configured = process.env[JEV_DEMO_API_URL_ENV]?.trim();
  const base = (configured && configured !== "" ? configured : DEFAULT_DEMO_API_URL).replace(
    /\/+$/,
    "",
  );
  return `${base}/${path.map(encodeURIComponent).join("/")}`;
}
