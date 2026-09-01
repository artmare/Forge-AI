# Isolated development runner

`forge-dev-runner` executes build, test, lint, typecheck, and local Git actions outside the API and
agent-worker containers. `forge-dev-install-runner` is a separate dependency-install channel. Both
run as fixed unprivileged UID/GID `10001`, with a read-only container root, dropped Linux capabilities,
`no-new-privileges`, no Docker socket, bounded PIDs, memory and CPU, and a tmpfs for temporary files.
A one-shot initializer assigns the workspace and queue volumes to that runtime identity.

The offline runner has `network_mode: none`. The install runner alone joins a dedicated network and
accepts only `NODE_INSTALL` or `PYTHON_INSTALL`. Neither image receives PostgreSQL, Redis, OpenAI, or
other Forge credentials. The scrubbed child environment contains only fixed runtime variables such
as `PATH`, `CI`, and local Git author metadata.

Although the runner controller mounts the workspace volume to locate a requested project, every
untrusted child is launched through a fail-closed Linux Landlock wrapper. The wrapper permits
read/write access only to the resolved `/workspaces/{company_id}/{project_id}`, `/tmp`, and the system
runtime files needed to execute Node, Python, and Git. Sibling workspaces and `/runner-queue` are not
visible to generated code. If Landlock cannot be enforced, execution exits with code 126 instead of
running without isolation.

The controller validates UUID workspace scope and rejects symlink components. It uses
`asyncio.create_subprocess_exec` with an explicit argv; `shell=True` is never used. Timeout or
cancellation sends `SIGTERM` to the child process group, waits two seconds, then sends `SIGKILL`.
Cancellation markers bridge worker coroutine cancellation to the runner. Completed filesystem side
effects are not rolled back.

Dependency installation remains supply-chain risk: Node uses `npm ci --ignore-scripts` and requires
both `package.json` and `package-lock.json`; Python installs only `requirements.txt` into a local venv.
Python build backends and downloaded package contents can still be malicious. Installation therefore
requires a separate permission and network-bearing runner, is recorded durably, and does not grant
network access to later tests/builds.

