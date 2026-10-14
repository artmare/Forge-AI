import { afterEach, describe, expect, it, vi } from "vitest";
import { forge, ForgeRequestError } from "./helpers";

afterEach(() => vi.unstubAllGlobals());

describe("Forge error boundary", () => {
  it("preserves canonical planner diagnostics", async () => {
    const error = { code: "INVALID_PLAN", message: "Acceptance criterion needs evidence.",
      validation_errors: [{ code: "UNVERIFIABLE_ACCEPTANCE_CRITERION", path: "tasks.0", message: "Name a target." }] };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ error }, { status: 422 })));
    await expect(forge("missions/id/plan", "POST", {})).rejects.toMatchObject({ code: error.code, message: error.message, details: error, status: 422 });
  });

  it.each(["<html>Bad gateway</html>", "null", '{"error":null}', '{"error":{"message":42}}'])(
    "reports HTTP failure for malformed error payload %s", async (body) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 502 })));
      const failure = await forge("missions").catch((error) => error);
      expect(failure).toBeInstanceOf(ForgeRequestError);
      if (!(failure instanceof ForgeRequestError)) throw new Error("Expected ForgeRequestError");
      expect(failure.message).toContain("HTTP 502");
      expect(failure.message).not.toContain("JSON");
      expect(failure.status).toBe(502);
    },
  );

  it("does not report successful HTTP with malformed JSON as a JSON input error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("broken", { status: 200 })));
    await expect(forge("missions")).rejects.toMatchObject({ code: "invalid_response", status: 200 });
  });
});
