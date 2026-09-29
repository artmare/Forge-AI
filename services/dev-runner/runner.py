from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any

QUEUE_ROOT = Path(os.getenv("RUNNER_QUEUE_ROOT", "/runner-queue"))
WORKSPACE_ROOT = Path(os.getenv("RUNNER_WORKSPACE_ROOT", "/workspaces"))
SANDBOX_ROOT = Path(os.getenv("RUNNER_SANDBOX_ROOT", "/sandboxes"))
CHANNEL = os.getenv("RUNNER_CHANNEL", "offline")
POLL_SECONDS = float(os.getenv("RUNNER_POLL_SECONDS", "0.1"))
WORKSPACE_PATTERN = re.compile(r"^[0-9a-f-]{36}/[0-9a-f-]{36}$")
FORBIDDEN = ("..", "|", ">", "<", "&&", "||", "$", "`", "\x00", "\n", "\r")
INSTALL_ACTIONS = {"NODE_INSTALL", "PYTHON_INSTALL"}
TRUSTED_GIT_ACTIONS = {"GIT_INIT", "GIT_STATUS", "GIT_DIFF", "GIT_LOG", "GIT_CHECKPOINT"}
RESERVED_GIT_NAMES = {".git", ".gitattributes", ".gitmodules"}
SANDBOX_EXECUTABLE = "/usr/local/bin/forge-sandbox-exec"


class BoundedCapture:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.total = 0
        self.head = bytearray()
        self.tail: deque[bytes] = deque()
        self.tail_size = 0

    def add(self, chunk: bytes) -> None:
        self.total += len(chunk)
        head_limit = self.limit // 2
        if len(self.head) < head_limit:
            take = min(head_limit - len(self.head), len(chunk))
            self.head.extend(chunk[:take])
            chunk = chunk[take:]
        if chunk:
            self.tail.append(chunk)
            self.tail_size += len(chunk)
            tail_limit = self.limit - len(self.head)
            while self.tail and self.tail_size > tail_limit:
                removed = self.tail.popleft()
                self.tail_size -= len(removed)

    def text(self) -> tuple[str, bool]:
        tail = b"".join(self.tail)
        allowed = max(self.limit - len(self.head), 0)
        tail = tail[-allowed:] if allowed else b""
        marker = b"\n...[output truncated]...\n" if self.total > len(self.head) + len(tail) else b""
        value = bytes(self.head) + marker + tail
        return value.decode("utf-8", errors="replace"), bool(marker)


def safe_workspace(relative: str) -> Path:
    if not WORKSPACE_PATTERN.fullmatch(relative):
        raise ValueError("invalid workspace scope")
    root = WORKSPACE_ROOT.resolve(strict=True)
    workspace = (root / relative).resolve(strict=True)
    workspace.relative_to(root)
    current = root
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("workspace symlink is not allowed")
    return workspace


def safe_target(arguments: dict[str, Any]) -> str | None:
    if set(arguments) - {"target"}:
        raise ValueError("unknown safe argument")
    target = arguments.get("target")
    if target is None:
        return None
    if not isinstance(target, str):
        raise ValueError("target must be text")
    target = target.strip().replace("\\", "/")
    if not target or target.startswith("/") or ":" in target[:3]:
        raise ValueError("target must be relative")
    if any(token in target for token in FORBIDDEN):
        raise ValueError("target contains prohibited syntax")
    return target


def require_file(workspace: Path, name: str) -> None:
    path = workspace / name
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"required manifest {name} is unavailable")


def is_reserved_git_path(relative: Path) -> bool:
    return any(part.casefold() in RESERVED_GIT_NAMES for part in relative.parts)


def _copy_allowed_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        relative = item.relative_to(source)
        if is_reserved_git_path(relative):
            continue
        target = destination / item.name
        if item.is_symlink():
            target.symlink_to(os.readlink(item), target_is_directory=item.is_dir())
        elif item.is_dir():
            shutil.copytree(
                item,
                target,
                symlinks=True,
                ignore=lambda _directory, names: [
                    name for name in names if name.casefold() in RESERVED_GIT_NAMES
                ],
            )
        else:
            shutil.copy2(item, target, follow_symlinks=False)


def prepare_untrusted_workspace(workspace: Path, request_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f-]{36}", request_id):
        raise ValueError("invalid request identity")
    root = SANDBOX_ROOT.resolve(strict=True)
    execution_workspace = root / request_id
    shutil.rmtree(execution_workspace, ignore_errors=True)
    _copy_allowed_tree(workspace, execution_workspace)
    return execution_workspace


def reserved_git_mutations(workspace: Path) -> list[str]:
    mutations: list[str] = []
    for directory, names, files in os.walk(workspace, followlinks=False):
        base = Path(directory)
        for name in [*names, *files]:
            path = base / name
            relative = path.relative_to(workspace)
            if is_reserved_git_path(relative):
                mutations.append(relative.as_posix())
                continue
            if path.is_symlink():
                target = Path(os.readlink(path))
                if is_reserved_git_path(target):
                    mutations.append(relative.as_posix())
    return sorted(set(mutations))


def sync_untrusted_outputs(source: Path, workspace: Path) -> None:
    """Copy additive/updated normal outputs back without propagating deletions or Git control files."""
    for directory, names, files in os.walk(source, followlinks=False):
        base = Path(directory)
        relative_directory = base.relative_to(source)
        if is_reserved_git_path(relative_directory):
            names[:] = []
            continue
        target_directory = workspace / relative_directory
        target_directory.mkdir(parents=True, exist_ok=True)
        names[:] = [name for name in names if name.casefold() not in RESERVED_GIT_NAMES]
        for name in files:
            relative = relative_directory / name
            if is_reserved_git_path(relative):
                continue
            item = base / name
            target = workspace / relative
            if item.is_symlink():
                if target.exists() or target.is_symlink():
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                target.symlink_to(os.readlink(item), target_is_directory=item.is_dir())
            else:
                if target.is_symlink() or target.is_dir():
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                shutil.copy2(item, target, follow_symlinks=False)


def command_for(request: dict[str, Any], workspace: Path) -> list[list[str]]:
    action = request["action"]
    arguments = request.get("safe_arguments", {})
    target = safe_target(arguments)
    if CHANNEL == "offline" and action in INSTALL_ACTIONS:
        raise ValueError("install actions require the isolated install runner")
    if CHANNEL == "install" and action not in INSTALL_ACTIONS:
        raise ValueError("install runner accepts dependency actions only")
    if action == "NODE_INSTALL":
        require_file(workspace, "package.json")
        require_file(workspace, "package-lock.json")
        return [["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"]]
    if action == "NODE_TEST":
        require_file(workspace, "package.json")
        return [["npm", "test", *( ["--", target] if target else [] )]]
    if action == "NODE_BUILD":
        require_file(workspace, "package.json")
        return [["npm", "run", "build"]]
    if action == "NODE_LINT":
        require_file(workspace, "package.json")
        return [["npm", "run", "lint"]]
    if action == "NODE_TYPECHECK":
        require_file(workspace, "package.json")
        return [["npm", "run", "typecheck"]]
    if action == "PYTHON_INSTALL":
        require_file(workspace, "requirements.txt")
        return [
            ["python3", "-m", "venv", ".forge-venv"],
            [".forge-venv/bin/python", "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements.txt"],
        ]
    if action == "PYTHON_TEST":
        python = ".forge-venv/bin/python" if (workspace / ".forge-venv/bin/python").is_file() else "python3"
        return [[python, "-m", "pytest", *( [target] if target else [] )]]
    if action == "PYTHON_LINT":
        python = ".forge-venv/bin/python" if (workspace / ".forge-venv/bin/python").is_file() else "python3"
        return [[python, "-m", "ruff", "check", target or "."]]
    if action == "GIT_INIT":
        return [["git", "init", "--initial-branch=main"]]
    if action == "GIT_STATUS":
        return [["git", "status", "--porcelain=v1", "--untracked-files=all"]]
    if action == "GIT_DIFF":
        return [["git", "diff", "--no-ext-diff", "--no-color", "--"], ["git", "diff", "--cached", "--no-ext-diff", "--no-color", "--"]]
    if action == "GIT_LOG":
        return [["git", "log", "-n", "20", "--pretty=format:%h %ad %s", "--date=iso-strict"]]
    if action == "GIT_CHECKPOINT":
        task_id = request["task_id"]
        if not re.fullmatch(r"[0-9a-f-]{36}", task_id):
            raise ValueError("invalid task identity")
        return [
            [
                "git",
                "add",
                "-A",
                "--",
                ".",
                ":(exclude).env",
                ":(exclude).env.*",
                ":(exclude)**/*.pem",
                ":(exclude)**/*.key",
                ":(exclude)**/*credentials*",
                ":(exclude)node_modules",
                ":(exclude).forge-venv",
            ],
            [
                "git",
                "commit",
                "--allow-empty",
                "-m",
                f"forge(task:{task_id}): development checkpoint",
            ],
        ]
    raise ValueError("unknown development action")


async def read_stream(stream: asyncio.StreamReader, capture: BoundedCapture) -> None:
    while chunk := await stream.read(8192):
        capture.add(chunk)


async def run_command(
    command: list[str],
    workspace: Path,
    timeout: float,
    output_limit: int,
    cancellation_path: Path,
) -> tuple[int, BoundedCapture, BoundedCapture, bool, bool]:
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp/forge-runner-home",
        "TMPDIR": "/tmp",
        "CI": "true",
        "NODE_ENV": "test",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "GIT_AUTHOR_NAME": "Forge",
        "GIT_AUTHOR_EMAIL": "forge@localhost",
        "GIT_COMMITTER_NAME": "Forge",
        "GIT_COMMITTER_EMAIL": "forge@localhost",
    }
    process = await asyncio.create_subprocess_exec(
        SANDBOX_EXECUTABLE,
        str(workspace),
        *command,
        cwd=workspace,
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    stdout = BoundedCapture(output_limit)
    stderr = BoundedCapture(output_limit)
    readers = [
        asyncio.create_task(read_stream(process.stdout, stdout)),  # type: ignore[arg-type]
        asyncio.create_task(read_stream(process.stderr, stderr)),  # type: ignore[arg-type]
    ]
    timed_out = False
    cancelled = False
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while process.returncode is None:
            if cancellation_path.exists():
                cancelled = True
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                await asyncio.wait_for(process.wait(), timeout=min(0.1, remaining))
            except TimeoutError:
                continue
    finally:
        if (timed_out or cancelled) and process.returncode is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
    if process.returncode is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
    await asyncio.gather(*readers)
    return process.returncode, stdout, stderr, timed_out, cancelled


async def execute(request: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    request_id = request.get("request_id", "")
    cancellation_path = QUEUE_ROOT / CHANNEL / "cancellations" / f"{request_id}.cancel"
    try:
        workspace = safe_workspace(str(request["workspace_relative"]))
        timeout = min(max(int(request["timeout_seconds"]), 1), 600)
        output_limit = min(max(int(request["output_limit_bytes"]), 1024), 1_000_000)
        action = str(request["action"])
        execution_workspace = workspace
        disposable_workspace: Path | None = None
        if action not in TRUSTED_GIT_ACTIONS:
            disposable_workspace = prepare_untrusted_workspace(workspace, str(request_id))
            execution_workspace = disposable_workspace
        commands = command_for(request, execution_workspace)
        combined_out = BoundedCapture(output_limit)
        combined_err = BoundedCapture(output_limit)
        exit_code = 0
        timed_out = False
        cancelled = cancellation_path.exists()
        for command in commands:
            if cancellation_path.exists():
                cancelled = True
                break
            remaining = timeout - (time.perf_counter() - started)
            if remaining <= 0:
                timed_out = True
                break
            exit_code, stdout, stderr, command_timeout, command_cancelled = await run_command(
                command, execution_workspace, remaining, output_limit, cancellation_path
            )
            out_text, _ = stdout.text()
            err_text, _ = stderr.text()
            combined_out.add(out_text.encode())
            combined_err.add(err_text.encode())
            timed_out = timed_out or command_timeout
            cancelled = cancelled or command_cancelled
            if timed_out or cancelled or exit_code != 0:
                break
        git_mutations = (
            reserved_git_mutations(disposable_workspace)
            if disposable_workspace is not None
            else []
        )
        metadata_denied = bool(git_mutations)
        if metadata_denied:
            exit_code = 126
            combined_err.add(
                b"Forge denied project-code mutation of reserved Git metadata.\n"
            )
        elif disposable_workspace is not None and not timed_out and not cancelled and exit_code == 0:
            sync_untrusted_outputs(disposable_workspace, workspace)
        if disposable_workspace is not None:
            shutil.rmtree(disposable_workspace, ignore_errors=True)
        stdout_text, stdout_truncated = combined_out.text()
        stderr_text, stderr_truncated = combined_err.text()
        status = "DENIED" if metadata_denied else "CANCELLED" if cancelled else "TIMED_OUT" if timed_out else "SUCCEEDED" if exit_code == 0 else "FAILED"
        return {
            "request_id": request_id,
            "status": status,
            "exit_code": exit_code,
            "stdout_excerpt": stdout_text,
            "stderr_excerpt": stderr_text,
            "stdout_bytes": combined_out.total,
            "stderr_bytes": combined_err.total,
            "truncated": stdout_truncated or stderr_truncated,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error_code": "DEVELOPMENT_GIT_METADATA_WRITE_DENIED" if metadata_denied else "DEVELOPMENT_EXECUTION_CANCELLED" if cancelled else "DEVELOPMENT_EXECUTION_TIMEOUT" if timed_out else ("DEVELOPMENT_COMMAND_FAILED" if exit_code else None),
            "error_message": "Project code attempted to modify Forge-controlled Git metadata." if metadata_denied else "Development action was cancelled." if cancelled else "Development action timed out." if timed_out else ("Development action exited non-zero." if exit_code else None),
        }
    except Exception as exc:
        return {
            "request_id": request_id,
            "status": "DENIED",
            "exit_code": None,
            "stdout_excerpt": "",
            "stderr_excerpt": "",
            "stdout_bytes": 0,
            "stderr_bytes": 0,
            "truncated": False,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "error_code": "DEVELOPMENT_REQUEST_DENIED",
            "error_message": str(exc)[:500],
        }
    finally:
        if "disposable_workspace" in locals() and disposable_workspace is not None:
            shutil.rmtree(disposable_workspace, ignore_errors=True)


async def main() -> None:
    requests = QUEUE_ROOT / CHANNEL / "requests"
    responses = QUEUE_ROOT / CHANNEL / "responses"
    cancellations = QUEUE_ROOT / CHANNEL / "cancellations"
    requests.mkdir(parents=True, exist_ok=True)
    responses.mkdir(parents=True, exist_ok=True)
    cancellations.mkdir(parents=True, exist_ok=True)
    while True:
        for request_path in sorted(requests.glob("*.json")):
            claimed = request_path.with_suffix(".running")
            try:
                os.replace(request_path, claimed)
            except FileNotFoundError:
                continue
            try:
                payload = json.loads(claimed.read_text(encoding="utf-8"))
                result = await execute(payload)
                output = responses / f"{payload.get('request_id', claimed.stem)}.json"
                temporary = output.with_suffix(".tmp")
                temporary.write_text(json.dumps(result, separators=(",", ":")), encoding="utf-8")
                os.replace(temporary, output)
            finally:
                claimed.unlink(missing_ok=True)
                (cancellations / f"{claimed.stem}.cancel").unlink(missing_ok=True)
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
