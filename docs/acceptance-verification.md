# Acceptance verification

Acceptance criteria are requirements, not proof. Phase 09 persists a separate
`AcceptanceVerification` for each criterion and iteration with one of `UNVERIFIED`, `PASSED`,
`FAILED`, or `NOT_APPLICABLE`, plus verifier identity, evidence summary, related execution IDs, and a
timestamp.

The deterministic QA gate maps build/test/lint/typecheck language to the matching successful command
record. A criterion naming a safe relative file can be verified by regular-file existence in the
project workspace. Criteria without a deterministic mapping remain `UNVERIFIED`; they are not shown
with a green check and block automated QA PASS.

Verification history is iteration-scoped. QA replaces only the current iteration's records, leaving
previous evidence available for audit. APIs expose results by Task, and the project graph embeds them
for the dashboard. The Development/Human Review panel shows the actual status and evidence for every
criterion. It never derives success from the criterion's mere presence.

Current evidence mapping is deliberately conservative. Future phases may add typed test-to-criterion
links, coverage evidence, security scanners, or human verification, but Phase 09 does not fabricate
confidence when objective evidence is absent.

