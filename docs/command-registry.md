# Development command registry

`CommandRegistry` is the only mapping from model-visible development intent to executable argv. Each
`CommandDefinition` records action, description, supported project types, required permission,
timeout, network policy, output limit, enabled state, and risk level.

Supported Phase 09 actions are:

- Node: `NODE_INSTALL`, `NODE_TEST`, `NODE_BUILD`, `NODE_LINT`, `NODE_TYPECHECK`;
- Python: `PYTHON_INSTALL`, `PYTHON_TEST`, `PYTHON_LINT`;
- local Git: `GIT_INIT`, `GIT_STATUS`, `GIT_DIFF`, `GIT_LOG`, `GIT_CHECKPOINT`.

The model never supplies executable names. Most actions accept no arguments. Test/lint actions may
accept one validated relative `target`; absolute paths, traversal, pipes, redirects, shell operators,
command substitution, environment expansion, newlines, unknown fields, and arbitrary executable
names are rejected. The runner performs the same validation again across the process boundary.

Git actions are fixed local operations. `GIT_CHECKPOINT` creates a Forge-owned message containing the
Task UUID. There is no remote, fetch, pull, push, credential, hook, or arbitrary Git command path.

Project profiles are detected from trusted file presence, not task-title text: `package.json` selects
Node, `pyproject.toml` or `requirements.txt` selects Python, static HTML can select static web, and
ambiguous workspaces remain `UNKNOWN`. The profile determines which catalog actions are available;
the model cannot redefine it into arbitrary code execution.

