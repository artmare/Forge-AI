import { afterEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "./route";

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Forge proxy", () => {
  it("allows the bounded planning retry and repair window", async () => {
    const timeout = vi.spyOn(AbortSignal, "timeout");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ status: "SUCCEEDED" })));
    const response = await POST(new Request("http://localhost/api/forge/missions/abc/plan", { method: "POST", body: "{}" }), { params: Promise.resolve({ path: ["missions", "abc", "plan"] }) });
    expect(response.status).toBe(200);
    expect(timeout).toHaveBeenCalledWith(600_000);
  });

  it("retains the short timeout for ordinary reads", async () => {
    const timeout = vi.spyOn(AbortSignal, "timeout");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json([])));
    await GET(new Request("http://localhost/api/forge/missions"), { params: Promise.resolve({ path: ["missions"] }) });
    expect(timeout).toHaveBeenCalledWith(35_000);
  });

  it("distinguishes a timeout from an unavailable backend without claiming planning failed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("timeout", "TimeoutError")));
    const response = await POST(new Request("http://localhost/api/forge/missions/abc/plan", { method: "POST", body: "{}" }), { params: Promise.resolve({ path: ["missions", "abc", "plan"] }) });
    expect(response.status).toBe(504);
    expect(await response.json()).toMatchObject({ error: { code: "backend_timeout", retryable: true } });
  });

  it("keeps unexposed paths denied", async () => {
    const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
    const response = await GET(new Request("http://localhost/api/forge/private"), { params: Promise.resolve({ path: ["private"] }) });
    expect(response.status).toBe(403);
    expect(fetch).not.toHaveBeenCalled();
  });
});
