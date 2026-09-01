# QA Agent and deterministic gate

Phase 09 QA is independent of the Developer's final claim. Forge selects an active persisted Agent
whose role is `QA` and verifies that `development.execute` and `git.read` are currently granted. QA
does not receive filesystem write or Git write by default and does not modify production code.

The initial QA implementation is a deterministic application-code gate. It detects the project
profile, records a separate deterministic QA AgentRun, inspects local Git status, and runs each
configured lint, typecheck, test, and build action through the same isolated runner. It persists a
structured `QAResult` with decision, summary, checks, blocking issues, non-blocking issues, evidence,
and execution IDs.

QA passes only when at least one deterministic check ran, every check succeeded, and every acceptance
criterion for the iteration has evidence-backed `PASSED` state. Missing QA configuration,
permissions, command evidence, or an unmapped criterion is blocking and fails closed. A PASS moves the
Task to `REVIEW`; a FAIL moves it through `FIX_REQUIRED` and requeues it while fix attempts remain.

This gate does not prove semantic product correctness beyond the mapped evidence. It cannot infer
visual quality, accessibility, security, or business fitness unless a deterministic check encodes
those properties. Human review therefore remains mandatory after QA.

