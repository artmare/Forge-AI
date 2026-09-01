export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const apiUrl = process.env.INTERNAL_API_URL ?? "http://localhost:8000";
  const lastEventId = request.headers.get("last-event-id");
  const headers = new Headers({ Accept: "text/event-stream" });
  if (lastEventId) headers.set("Last-Event-ID", lastEventId);

  const upstream = await fetch(`${apiUrl}/api/v1/events/stream`, {
    headers,
    cache: "no-store",
    signal: request.signal,
  });
  if (!upstream.ok || !upstream.body) {
    return new Response("Event stream unavailable", { status: 503 });
  }
  return new Response(upstream.body, {
    headers: {
      "Cache-Control": "no-cache, no-transform",
      "Content-Type": "text/event-stream",
      Connection: "keep-alive",
    },
  });
}
