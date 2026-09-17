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

Although the runner controller mounts the workspace volume to locate a requested project, it never
exposes that authoritative tree to model-authored Node, Python, build, test, or install processes.
For each such action it copies ordinary project files, excluding `.git`, `.gitattributes`, and
`.gitmodules`, into a request-scoped `/sandboxes/{request_id}` tree. The untrusted child is launched
through a fail-closed Linux Landlock wrapper that permits read/write access only to that disposable
tree, `/tmp`, and the system runtime files needed to execute Node or Python. The authoritative project
workspace, its Forge-controlled Git administration state, sibling workspaces, and `/runner-queue`
are outside the child's OS-visible write boundary. If Landlock cannot be enforced, execution exits
with code 126 instead of running without isolation.

After a successful command, the controller copies additive or updated normal project outputs back to
the authoritative workspace. It never propagates deletions or Git control paths. Creation of `.git`,
`.gitattributes`, or `.gitmodules` at any depth in the disposable tree is a fail-closed
`DEVELOPMENT_GIT_METADATA_WRITE_DENIED` result and nothing from that action is synchronized. Fixed
Forge-owned `GIT_INIT`, `GIT_STATUS`, `GIT_DIFF`, `GIT_LOG`, and `GIT_CHECKPOINT` argv execute against
the authoritative workspace through a distinct trusted branch; model-authored commands never enter
that branch.

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
