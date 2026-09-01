import { NextResponse } from "next/server";

export async function POST(
  _request: Request,
  { params }: { params: Promise<{ action: string }> },
) {
  const { action } = await params;
  if (action !== "pause" && action !== "resume") {
    return NextResponse.json(
      { error: { code: "not_found", message: "Unknown orchestrator action." } },
      { status: 404 },
    );
  }
  try {
    const apiUrl = process.env.INTERNAL_API_URL ?? "http://localhost:8000";
    const response = await fetch(`${apiUrl}/api/v1/orchestrator/${action}`, {
      method: "POST",
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return NextResponse.json(await response.json(), { status: response.status });
  } catch {
    return NextResponse.json(
      { error: { code: "orchestrator_unavailable", message: "Orchestrator is unavailable." } },
      { status: 503 },
    );
  }
}
