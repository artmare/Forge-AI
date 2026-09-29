from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from app.tool_system.errors import ToolSystemError


@dataclass(frozen=True)
class SelfDevelopmentWorkspace:
    path: Path
    branch: str
    base_revision: str


class SelfDevelopmentWorkspaceManager:
    """Forge-controlled isolated Git worktrees for self-development.

    This service is orchestration infrastructure, not a model tool. Models only see
    the already-confined project workspace created here.
    """

    _IDENTIFIER = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

    def __init__(self, repository: Path, worktree_root: Path) -> None:
        if not repository.is_absolute() or not worktree_root.is_absolute():
            raise ToolSystemError("INVALID_WORKTREE_PATH", "Absolute configured paths required")
        for path in (repository, worktree_root):
            if any(parent.is_symlink() for parent in (path, *path.parents)):
                raise ToolSystemError("INVALID_WORKTREE_PATH", "Symlink paths are forbidden")
        self.repository = repository.resolve(strict=True)
        self.worktree_root = worktree_root.resolve(strict=False)
        if (
            self.worktree_root == self.repository
            or self.repository in self.worktree_root.parents
            or self.worktree_root in self.repository.parents
        ):
            raise ToolSystemError(
                "INVALID_WORKTREE_PATH", "Repository and worktrees must be disjoint"
            )

    async def create(
        self, identifier: str, *, base_revision: str = "HEAD", target_name: str | None = None
    ) -> SelfDevelopmentWorkspace:
        if not self._IDENTIFIER.fullmatch(identifier) or (
            target_name is not None and not self._IDENTIFIER.fullmatch(target_name)
        ):
            raise ToolSystemError("INVALID_WORKTREE_ID", "Worktree identifier is invalid")
        if not (self.repository / ".git").exists():
            raise ToolSystemError(
                "SELF_DEVELOPMENT_REPOSITORY_INVALID", "Source is not a Git repository"
            )
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        root = self.worktree_root.resolve(strict=True)
        target = (root / (target_name or identifier)).resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ToolSystemError(
                "PATH_OUTSIDE_WORKSPACE", "Worktree target escaped its root"
            ) from exc
        if target.exists():
            raise ToolSystemError(
                "WORKTREE_ALREADY_EXISTS", "Self-development worktree already exists"
            )
        status = await self._git("status", "--porcelain", "--untracked-files=no")
        if status.strip():
            raise ToolSystemError(
                "SELF_DEVELOPMENT_SOURCE_DIRTY",
                "Tracked source changes must be checkpointed before creating a worktree",
            )
        base = (
            await self._git(
                "rev-parse", "--verify", "--end-of-options", base_revision + "^{commit}"
            )
        ).strip()
        current_branch = (await self._git("branch", "--show-current")).strip()
        branch = f"forge-dev/{identifier}"
        if current_branch == branch:
            raise ToolSystemError(
                "STABLE_BRANCH_PROTECTED", "Cannot reuse the active source branch"
            )
        await self._git("worktree", "add", "-b", branch, str(target), base)
        return SelfDevelopmentWorkspace(path=target, branch=branch, base_revision=base)

    async def inspect(self, workspace: SelfDevelopmentWorkspace) -> dict[str, str]:
        path = workspace.path
        if path.is_symlink() or path.resolve() != path or path.parent != self.worktree_root:
            raise ToolSystemError("INVALID_WORKTREE_PATH", "Worktree path does not match root")
        branch = (await self._git_at(path, "branch", "--show-current")).strip()
        common = Path((await self._git_at(path, "rev-parse", "--git-common-dir")).strip())
        if not common.is_absolute():
            common = path / common
        expected = Path((await self._git("rev-parse", "--git-common-dir")).strip())
        if not expected.is_absolute():
            expected = self.repository / expected
        if (
            branch != workspace.branch
            or not branch.startswith("forge-dev/")
            or (common.resolve() != expected.resolve())
        ):
            raise ToolSystemError("STABLE_BRANCH_PROTECTED", "Worktree identity mismatch")
        return {
            "branch": branch,
            "worktree": str(path),
            "status": await self._git_at(path, "status", "--porcelain", "--untracked-files=all"),
            "diff_summary": await self._git_at(
                path, "diff", "--no-ext-diff", "--stat", workspace.base_revision, "--"
            ),
            "head": (await self._git_at(path, "rev-parse", "HEAD")).strip(),
        }

    async def checkpoint(self, workspace: SelfDevelopmentWorkspace) -> str:
        await self.inspect(workspace)
        await self._git_at(
            workspace.path,
            "add",
            "-A",
            "--",
            ".",
            ":(exclude).env",
            ":(exclude).env.*",
            ":(exclude)**/*.pem",
            ":(exclude)**/*.key",
        )
        staged = await self._git_at(workspace.path, "diff", "--cached", "--name-only")
        if staged.strip():
            await self._git_at(
                workspace.path,
                "-c",
                "user.name=Forge",
                "-c",
                "user.email=forge@localhost",
                "commit",
                "-m",
                f"Forge checkpoint {workspace.branch}",
            )
        return (await self._git_at(workspace.path, "rev-parse", "HEAD")).strip()

    async def cleanup(self, workspace: SelfDevelopmentWorkspace) -> None:
        state = await self.inspect(workspace)
        if state["status"].strip():
            raise ToolSystemError(
                "UNCHECKPOINTED_WORK", "Checkpoint or explicitly recover dirty work"
            )
        # No --force and no branch deletion: checkpoint remains reviewable after cleanup.
        await self._git("worktree", "remove", str(workspace.path))

    async def _git(self, *arguments: str) -> str:
        return await self._git_at(self.repository, *arguments)

    async def _git_at(self, path: Path, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(path),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *arguments,
            env={
                "PATH": os.defpath,
                "HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise ToolSystemError(
                "SELF_DEVELOPMENT_GIT_FAILED",
                (stderr.decode("utf-8", errors="replace").strip() or "Git operation failed")[:2000],
            )
        return stdout.decode("utf-8", errors="replace")
