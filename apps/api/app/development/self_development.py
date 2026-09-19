from __future__ import annotations

import asyncio
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
        self.repository = repository.resolve(strict=True)
        self.worktree_root = worktree_root.resolve(strict=False)

    async def create(
        self, identifier: str, *, base_revision: str = "HEAD"
    ) -> SelfDevelopmentWorkspace:
        if not self._IDENTIFIER.fullmatch(identifier):
            raise ToolSystemError("INVALID_WORKTREE_ID", "Worktree identifier is invalid")
        if not (self.repository / ".git").exists():
            raise ToolSystemError(
                "SELF_DEVELOPMENT_REPOSITORY_INVALID", "Source is not a Git repository"
            )
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        root = self.worktree_root.resolve(strict=True)
        target = (root / identifier).resolve(strict=False)
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
        base = (await self._git("rev-parse", "--verify", base_revision)).strip()
        current_branch = (await self._git("branch", "--show-current")).strip()
        branch = f"forge-dev/{identifier}"
        if current_branch == branch:
            raise ToolSystemError(
                "STABLE_BRANCH_PROTECTED", "Cannot reuse the active source branch"
            )
        await self._git("worktree", "add", "-b", branch, str(target), base)
        return SelfDevelopmentWorkspace(path=target, branch=branch, base_revision=base)

    async def _git(self, *arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(self.repository),
            *arguments,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise ToolSystemError(
                "SELF_DEVELOPMENT_GIT_FAILED",
                (stderr.decode("utf-8", errors="replace").strip() or "Git operation failed")[:2000],
            )
        return stdout.decode("utf-8", errors="replace")
