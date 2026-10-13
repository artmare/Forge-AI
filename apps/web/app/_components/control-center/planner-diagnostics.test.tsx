import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { PlannerResponseDiagnostics } from "./planner-diagnostics";

afterEach(() => cleanup());

describe("PlannerResponseDiagnostics", () => {
  it("renders schema response-shape validation evidence", () => {
    render(<PlannerResponseDiagnostics error={{
      failure_category: "SCHEMA_PARSE",
      validation_errors: [{
        field: "$.tasks[0].acceptance_criteria",
        expected: "array",
        received: { type: "string", length: 24 },
        error_type: "list_type",
      }],
    }}/>);

    const diagnostics = screen.getByLabelText("Planner response diagnostics");
    expect(diagnostics.textContent).toContain("$.tasks[0].acceptance_criteria");
    expect(diagnostics.textContent).toContain("expected array");
    expect(diagnostics.textContent).toContain("received string (length 24)");
  });

  it("renders canonical validation code, path, and message", () => {
    render(<PlannerResponseDiagnostics error={{
      failure_category: "CANONICAL_VALIDATION",
      validation_errors: [{
        code: "UNVERIFIABLE_ACCEPTANCE_CRITERION",
        path: "tasks.1.acceptance_criteria.2",
        message: "Acceptance criterion must name an observable target.",
      }],
    }}/>);

    const diagnostics = screen.getByLabelText("Planner response diagnostics");
    expect(diagnostics.textContent).toContain("tasks.1.acceptance_criteria.2");
    expect(diagnostics.textContent).toContain("UNVERIFIABLE_ACCEPTANCE_CRITERION");
    expect(diagnostics.textContent).toContain(
      "Acceptance criterion must name an observable target.",
    );
    expect(diagnostics.textContent).not.toContain("unknown shape");
    expect(diagnostics.textContent).not.toContain("expected undefined");
  });
});
