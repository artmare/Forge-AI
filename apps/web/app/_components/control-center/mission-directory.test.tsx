import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MissionDirectory } from "./mission-directory";
import type { Mission } from "./types";

const current: Mission = {
  id: "mission-current",
  company_id: "company-current",
  project_id: "project-current",
  title: "Current Product Mission",
  goal: "Ship the product",
  constraints: {},
  status: "COMPLETED",
  planning_attempts: 1,
  max_planning_attempts: 3,
  failure_reason: null,
  project_name: "Product Project",
  archived_at: null,
  created_at: "2026-08-20T10:00:00Z",
  progress: { total: 4, done: 4, review: 0, running: 0, queued: 0, blocked: 0, failed: 0 },
};

const archived: Mission = {
  ...current,
  id: "mission-archived",
  title: "Archived Validation Mission",
  status: "FAILED",
  archived_at: "2026-08-24T10:00:00Z",
  progress: { ...current.progress, done: 2, failed: 2 },
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("MissionDirectory", () => {
  it("separates current Missions and offers archive from a compact menu", async () => {
    const action = vi.fn(async () => true);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(
      <MissionDirectory
        current={[current]}
        archived={[archived]}
        scope="active"
        selectedId={current.id}
        busy={false}
        onScope={() => undefined}
        onOpen={() => undefined}
        onAction={action}
      />,
    );

    expect(screen.getByText("Current Product Mission")).toBeTruthy();
    expect(screen.queryByText("Archived Validation Mission")).toBeNull();
    await user.click(screen.getByLabelText("Actions for Current Product Mission"));
    await user.click(screen.getByRole("menuitem", { name: "Archive Mission" }));
    await waitFor(() => expect(action).toHaveBeenCalledWith("missions/mission-current/archive"));
  });

  it("shows durable archive metadata and requires typed deletion confirmation", async () => {
    const action = vi.fn(async () => true);
    vi.spyOn(window, "prompt").mockReturnValue(archived.title);
    const user = userEvent.setup();
    render(
      <MissionDirectory
        current={[current]}
        archived={[archived]}
        scope="archived"
        selectedId={archived.id}
        busy={false}
        onScope={() => undefined}
        onOpen={() => undefined}
        onAction={action}
      />,
    );

    expect(screen.getByText("Archived Validation Mission")).toBeTruthy();
    expect(screen.getByText("50%")).toBeTruthy();
    await user.click(screen.getByLabelText("Actions for Archived Validation Mission"));
    expect(screen.getByRole("menuitem", { name: "Restore Mission" })).toBeTruthy();
    await user.click(screen.getByRole("menuitem", { name: "Delete permanently" }));
    await waitFor(() => expect(action).toHaveBeenCalledWith(
      "missions/mission-archived",
      { confirmation: archived.title },
      "DELETE",
    ));
  });
});
