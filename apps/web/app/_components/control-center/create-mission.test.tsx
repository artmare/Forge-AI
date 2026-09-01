import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CreateMission } from "./create-mission";
import { ForgeRequestError } from "./helpers";

afterEach(() => cleanup());

describe("CreateMission", () => {
  it("submits a real create-and-start request with bounded planning inputs", async () => {
    const onCreate = vi.fn(async () => undefined);
    const user = userEvent.setup();
    render(<CreateMission open busy={false} stage={null} retrying={false} runtimePaused onClose={vi.fn()} onCreate={onCreate} />);

    await user.type(screen.getByLabelText("Mission title"), "Runtime verification");
    await user.type(screen.getByLabelText("Outcome"), "Persist, plan, activate, and expose the execution state.");
    await user.click(screen.getByRole("button", { name: "Create & start" }));

    await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
      title: "Runtime verification",
      goal: "Persist, plan, activate, and expose the execution state.",
      start_immediately: true,
      constraints: expect.objectContaining({ max_agents: 3, max_tasks: 8, internet_access: false, browser_access: false }),
    })));
  });

  it("supports draft-only creation, reports invalid JSON, and closes with Escape", async () => {
    const onClose = vi.fn();
    const onCreate = vi.fn(async () => undefined);
    const user = userEvent.setup();
    render(<CreateMission open busy={false} stage={null} retrying={false} runtimePaused={false} onClose={onClose} onCreate={onCreate} />);

    await user.click(screen.getByRole("checkbox", { name: /Plan and activate after creation/ }));
    expect(screen.getByRole("button", { name: "Create draft" })).toBeTruthy();
    await user.click(screen.getByRole("button", { name: /Advanced structured inputs/ }));
    await user.clear(screen.getByLabelText("Constraints JSON"));
    await user.type(screen.getByLabelText("Constraints JSON"), "not-json");
    await user.type(screen.getByLabelText("Mission title"), "Draft mission");
    await user.type(screen.getByLabelText("Outcome"), "Save this mission as a draft.");
    await user.click(screen.getByRole("button", { name: "Create draft" }));
    expect((await screen.findByRole("alert")).textContent).toContain("valid JSON");
    expect(onCreate).not.toHaveBeenCalled();

    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("shows normalized transient planner diagnostics and keeps Retry available", async () => {
    const onCreate = vi.fn(async () => {
      throw new ForgeRequestError(
        "The planning provider rate limit was reached.",
        "PLANNER_RATE_LIMITED",
        429,
        {
          category: "RATE_LIMIT",
          phase: "PROVIDER_REQUEST",
          retryable: true,
          retry_exhausted: true,
          provider_attempts: 3,
          max_provider_attempts: 3,
        },
      );
    });
    const user = userEvent.setup();
    render(<CreateMission open busy={false} stage={null} retrying runtimePaused={false} onClose={vi.fn()} onCreate={onCreate} />);
    await user.type(screen.getByLabelText("Mission title"), "Retryable mission");
    await user.type(screen.getByLabelText("Outcome"), "Create a safely retryable plan.");
    await user.click(screen.getByRole("button", { name: "Retry saved mission" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("PLANNER_RATE_LIMITED");
    expect(alert.textContent).toContain("Provider attempts: 3 / 3");
    expect(alert.textContent).toContain("automatic retry budget exhausted");
    expect((screen.getByRole("button", { name: "Retry saved mission" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("exposes quota cause safely and disables an inappropriate immediate retry", async () => {
    const onCreate = vi.fn(async () => {
      throw new ForgeRequestError(
        "The planning provider API credit balance is exhausted.",
        "PLANNER_QUOTA_EXHAUSTED",
        402,
        {
          category: "QUOTA",
          phase: "PROVIDER_REQUEST",
          retryable: false,
          provider_attempts: 1,
          max_provider_attempts: 3,
          provider_error_code: "credit_balance_exhausted",
        },
      );
    });
    const user = userEvent.setup();
    render(<CreateMission open busy={false} stage={null} retrying runtimePaused={false} onClose={vi.fn()} onCreate={onCreate} />);
    await user.type(screen.getByLabelText("Mission title"), "Quota mission");
    await user.type(screen.getByLabelText("Outcome"), "Expose the durable quota failure.");
    await user.click(screen.getByRole("button", { name: "Retry saved mission" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("PLANNER_QUOTA_EXHAUSTED");
    expect(alert.textContent).toContain("credit_balance_exhausted");
    expect((screen.getByRole("button", { name: "Retry unavailable" }) as HTMLButtonElement).disabled).toBe(true);
    expect(alert.textContent).not.toContain("test-key");
  });

  it("shows safe malformed Planner field evidence without raw values", async () => {
    const onCreate = vi.fn(async () => {
      throw new ForgeRequestError(
        "The planning provider returned a malformed structured response.",
        "PLANNER_MALFORMED_RESPONSE",
        502,
        {
          category: "RESPONSE_VALIDATION",
          phase: "PROVIDER_REQUEST",
          retryable: false,
          finish_reason: "STOP",
          response_shape: { type: "object", text_length: 9219 },
          validation_errors: [{
            field: "$.tasks[0].acceptance_criteria",
            expected: "array",
            received: { type: "string", length: 24 },
            error_type: "list_type",
          }],
        },
      );
    });
    const user = userEvent.setup();
    render(<CreateMission open busy={false} stage={null} retrying runtimePaused={false} onClose={vi.fn()} onCreate={onCreate} />);
    await user.type(screen.getByLabelText("Mission title"), "Malformed plan");
    await user.type(screen.getByLabelText("Outcome"), "Expose safe response diagnostics.");
    await user.click(screen.getByRole("button", { name: "Retry saved mission" }));

    const diagnostics = await screen.findByLabelText("Planner response diagnostics");
    expect(diagnostics.textContent).toContain("$.tasks[0].acceptance_criteria");
    expect(diagnostics.textContent).toContain("expected array");
    expect(diagnostics.textContent).toContain("received string (length 24)");
    expect(diagnostics.textContent).toContain("Gemini finish: STOP");
    expect(diagnostics.textContent).not.toContain("a-secret-response-value");
  });
});
