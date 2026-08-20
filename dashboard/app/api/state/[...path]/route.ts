/**
 * Server-side proxy to the engine's monitoring API.
 *
 * The reason this file exists rather than the browser calling the API directly:
 * the bearer token guards the kill switch, and anything the browser holds is
 * readable by anyone who opens devtools. So the token lives in the Next.js server
 * process, and the browser talks only to this route.
 *
 * The allow-list is not decoration either. Without it, a path parameter is an
 * open proxy into whatever else is reachable from the server — and the one thing
 * this proxy fronts is a service with a button that halts trading.
 */

import { NextRequest, NextResponse } from "next/server";

const API_URL = process.env.ALGO_API_URL ?? "http://127.0.0.1:8000";
const API_TOKEN = process.env.ALGO_API_TOKEN ?? "";

/** Every path the dashboard is permitted to reach, and nothing else. */
const READABLE = new Set([
  "health",
  "equity",
  "positions",
  "trades",
  "signals",
  "notes",
  "kill-switch",
]);

/** The only path that may be written to. */
const WRITABLE = new Set(["kill-switch"]);

function target(path: string[], search: string): string | null {
  const head = path[0];
  if (path.length !== 1 || !head || !READABLE.has(head)) return null;
  return `${API_URL}/${head}${search}`;
}

function missingToken(): NextResponse {
  return NextResponse.json(
    {
      error:
        "ALGO_API_TOKEN is not set on the dashboard server. Copy .env.example to " +
        ".env.local and set it to the same value the engine uses.",
    },
    { status: 500 },
  );
}

export async function GET(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  if (!API_TOKEN) return missingToken();

  const { path } = await context.params;
  const url = target(path, request.nextUrl.search);
  if (!url) {
    return NextResponse.json({ error: `not a readable endpoint: ${path.join("/")}` }, { status: 404 });
  }

  try {
    const upstream = await fetch(url, {
      headers: { Authorization: `Bearer ${API_TOKEN}` },
      cache: "no-store",
    });
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: { "content-type": "application/json" },
    });
  } catch (error) {
    // The engine being unreachable is a normal state to render, not a crash.
    // A monitoring page that goes blank when the thing it monitors goes down is
    // exactly backwards.
    return NextResponse.json(
      { error: `engine unreachable at ${API_URL}: ${String(error)}` },
      { status: 503 },
    );
  }
}

export async function POST(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
): Promise<NextResponse> {
  if (!API_TOKEN) return missingToken();

  const { path } = await context.params;
  const head = path[0];
  if (path.length !== 1 || !head || !WRITABLE.has(head)) {
    return NextResponse.json({ error: `not a writable endpoint: ${path.join("/")}` }, { status: 405 });
  }

  try {
    const upstream = await fetch(`${API_URL}/${head}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${API_TOKEN}`,
        "content-type": "application/json",
      },
      body: await request.text(),
    });
    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: { "content-type": "application/json" },
    });
  } catch (error) {
    return NextResponse.json(
      { error: `engine unreachable at ${API_URL}: ${String(error)}` },
      { status: 503 },
    );
  }
}
