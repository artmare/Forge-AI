# Developer Agent

`DEVELOPER` is a first-class runtime role, but role is not authority. A Developer sees development
tools only when the persisted Agent permission map independently grants the required permission on
every call.

Developer instructions require the Agent to inspect existing files and upstream artifacts, treat
repository text as untrusted data, make the smallest coherent change, preserve working behavior,
add tests where appropriate, execute deterministic validation, inspect failures, and inspect local Git
status/diff before finalizing. Writing code alone is explicitly not completion.

Typical permissions are `filesystem.list/read/write`, `development.execute`, `git.read`, and
`git.write`. `development.install_dependencies` is separate and off unless explicitly granted. No
role automatically receives any of them.

The normal bounded AgentRuntime loop is reused. A model turn may request one registered tool or
return the existing structured final result. `DEVELOPER_MAX_STEPS`,
`DEVELOPMENT_MAX_EXECUTIONS`, per-command timeouts, and Task iteration limits prevent unlimited
autonomous debugging. On QA failure, the next context includes structured blocking findings and the
current iteration. On human request-fix, the existing durable review instructions are included. Both
paths execute validation and QA again before returning to human review.

