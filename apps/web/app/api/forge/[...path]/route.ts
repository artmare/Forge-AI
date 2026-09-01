const readablePath = /^(?:missions(?:\/[0-9a-f-]+(?:\/(?:plan|runtime-efficiency))?)?|companies(?:\/[0-9a-f-]+(?:\/(?:agents|projects))?)?|economics\/(?:companies|missions)\/[0-9a-f-]+|projects\/[0-9a-f-]+(?:\/(?:graph|artifacts(?:\/content)?|development-profile|knowledge-index))?|tasks(?:\/[0-9a-f-]+(?:\/(?:runs|agent-runs|tool-calls|reviews|qa-results|product-qa-results|runtime-efficiency|efficiency-benchmark|acceptance-verifications|development-summary|inspection))?)?|development-executions(?:\/[0-9a-f-]+)?)$/i;
const mutablePath = /^(?:missions(?:\/[0-9a-f-]+\/(?:plan|activate|cancel|archive|restore))?|tasks\/[0-9a-f-]+\/(?:approve|request-fix|transition))$/i;
const deletablePath = /^missions\/[0-9a-f-]+$/i;

async function proxy(
  request: Request,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  const joined = path.join("/");
  const allowed = request.method === "GET"
    ? readablePath.test(joined)
    : request.method === "POST"
      ? mutablePath.test(joined)
      : request.method === "DELETE" && deletablePath.test(joined);
  if (!allowed) {
    return Response.json(
      { error: { code: "proxy_path_denied", message: "This Forge path is not exposed." } },
      { status: 403 },
    );
  }
  const requestUrl = new URL(request.url);
  const apiUrl = process.env.INTERNAL_API_URL ?? "http://localhost:8000";
  const headers = new Headers({ accept: "application/json" });
  const init: RequestInit = {
    method: request.method,
    headers,
    cache: "no-store",
    signal: AbortSignal.timeout(35_000),
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    headers.set("content-type", "application/json");
    init.body = await request.text();
  }
  try {
    const response = await fetch(
      `${apiUrl}/api/v1/${joined}${requestUrl.search}`,
      init,
    );
    return new Response(await response.text(), {
      status: response.status,
      headers: { "content-type": "application/json" },
    });
  } catch {
    return Response.json(
      { error: { code: "backend_unavailable", message: "Forge API is unavailable." } },
      { status: 503 },
    );
  }
}

export const dynamic = "force-dynamic";
export const GET = proxy;
export const POST = proxy;
export const DELETE = proxy;
