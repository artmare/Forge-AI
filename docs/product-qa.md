# Product and Visual QA (Phase 09.5)

Product QA adds a separate persisted quality layer after deterministic development validation. It
does not add browser automation. Forge inspects bounded project source files using deterministic
rules and records a `ProductQAResult` for the Task iteration.

## Dimensions and evidence

Each result keeps FUNCTIONAL, TESTS, ACCESSIBILITY, RESPONSIVENESS, VISUAL, USABILITY and SECURITY
separate. A dimension is `PASS`, `FAIL`, or `UNVERIFIED` with evidence. Functional/test status is
linked to deterministic Phase 09 commands. Static source rules can establish issues such as a
fixed-width overflow risk or missing label. Render-dependent visual/usability quality remains
`UNVERIFIED` when no approved screenshot/render evidence exists. Forge never fabricates a browser
run or screenshot.

The default viewport contract is:

- desktop: 1440x900
- tablet: 768x1024
- narrow/extension side panel: 390x844

## Deterministic rules

The bounded checker records structured issues for fixed widths that can exceed the narrow
viewport, root horizontal scrolling, text below the product floor, visible inputs without
associated labels, and absent semantic heading hierarchy. Each issue includes category, dimension,
severity, file, description, suggested fix and evidence. `BLOCKING` and `MAJOR` findings join the
existing targeted QA fix loop. `MINOR` findings remain visible without independently blocking.

Product acceptance criteria containing measurable responsiveness/accessibility terms are mapped to
the corresponding persisted dimension. If runtime evidence is unavailable, the criterion remains
UNVERIFIED; Forge does not convert absence of evidence into a pass.

Planner guidance asks for measurable frontend criteria covering overflow/wrapping, labels, keyboard
focus and applicable loading/empty/error states while acknowledging browser checks are unavailable.

## API and UI

`GET /tasks/{id}/product-qa-results` returns iteration history. Task inspection shows dimensions,
issues, viewports, evidence and the explicit screenshot availability state. The layout collapses to
one column on narrow screens.

## Intentional limitation

Phase 09.5 is screenshot-ready and user-evidence-ready, but it does not capture pages, inspect a
live browser, perform pixel comparisons, or claim rendered appearance. Those capabilities remain
out of scope rather than being simulated.

