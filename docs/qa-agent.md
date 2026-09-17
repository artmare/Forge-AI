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

## STATIC_WEB contract

`STATIC_WEB` is an intentional exception to the Node/Python execution floor. It applies only when
profile detection finds static web entry points such as HTML/CSS/client JavaScript and no supported
Node or Python manifest that would provide deterministic test, lint, typecheck, or build actions.
For this profile, Forge may satisfy the implementation-evidence floor with a persisted deterministic
Product QA result. The Product QA record must have a `PASS` decision, contain its static inspection
dimensions and evidence, and leave no blocking or major issue. Each acceptance criterion must still
be mapped to durable passing evidence; render-dependent or otherwise unverified criteria block
`REVIEW`.

`GIT_STATUS` by itself is never implementation evidence. It proves only that the trusted repository
can be inspected, not that the product works or that an acceptance criterion is satisfied. A
`STATIC_WEB` Task is therefore blocked when Product QA is unavailable, fails, reports a blocking or
major issue, or cannot verify all required criteria. Node and Python profiles do not use this
exception: they require their discovered deterministic test/build/lint/typecheck actions to execute
successfully in addition to criterion evidence. This contract documents the existing Phase 09 gate;
it does not add browser or visual execution capabilities.

This gate does not prove semantic product correctness beyond the mapped evidence. It cannot infer
visual quality, accessibility, security, or business fitness unless a deterministic check encodes
those properties. Human review therefore remains mandatory after QA.
